#!/usr/bin/env python3
"""Stage 5: LLM learning suggestions over unanalyzed episodes. Incremental.

Consumes episodes where analyzed_run IS NULL (user + assistant prose), asks the
configured LLM for SUGGESTIONS ONLY — skills to create, rules to reinforce,
recurring problems, workflow improvements — each grounded in the episode ids it
was derived from. Appends one run section to SUGGESTIONS.md.
Never implements anything; the markdown is a review queue for a human.
"""
import json, os, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import db, chat_json, SUGGEST_MODEL, OUT

BATCH = 40            # episodes per LLM call
# Bound LLM calls per run; the rest waits for the next run.
# Override: LEARN_MAX_CALLS=200 for a big backfill.
MAX_CALLS = int(os.environ.get("LEARN_MAX_CALLS", "25"))
MD = os.path.join(OUT, "SUGGESTIONS.md")

PROMPT = """You analyze episodes from AI pair-programming transcripts (user messages AND assistant prose) and extract LEARNING SUGGESTIONS for the human who runs these sessions.

Each episode has: id, role (user|assistant), kind hint, text, and context (the other side's preceding message).

Return ONLY suggestions worth a human's review — quality over quantity. Categories:
- "new-skill": a task done repeatedly by hand that deserves a skill/command (name it kebab-case)
- "reinforce": an EXISTING skill/rule/CLAUDE.md instruction that sessions keep violating — name which one
- "recurring-problem": the same friction/failure/misunderstanding showing up across sessions
- "workflow": a better way to structure the work itself (sequencing, tooling, handoffs)
- "hygiene": security/credential/cost hygiene issues

Rules:
- SUGGESTIONS ONLY. Never write implementations, code, or skill bodies.
- Every suggestion MUST cite the episode ids it is based on ("episodes": ["id", ...]) — 1 to 5 ids. No id, no suggestion.
- Prefer patterns seen 2+ times; one-offs only when severity is high (security, data loss).
- If a suggestion matches one of the PRIOR titles below, reuse that exact title and give only the NEW evidence ids (it will be merged as recurrence).
- Max 8 suggestions per batch.

PRIOR suggestion titles (reuse if same pattern, else ignore):
{prior}

Return ONLY JSON:
{{"suggestions": [{{"category": "...", "title": "...", "detail": "<=3 sentences: what happens, why it matters, what to consider doing", "episodes": ["..."]}}]}}

EPISODES:
"""


def fmt(row):
    h, sid, ts, kind, role, text, ctx = row
    eid = f"{(sid or '?')[:8]}:{h[:6]}"
    return (f"### id={eid} role={role} kind={kind or '?'} ts={(ts or '')[:16]}\n"
            f"TEXT: {text[:900]}\nCONTEXT: {(ctx or '')[:400]}")


def main():
    conn = db(); cur = conn.cursor()
    # Balanced mix: assistant prose dwarfs user messages (~5:1), so a plain
    # newest-first window would starve the user side (where corrections live).
    per_role = (BATCH * MAX_CALLS) // 2
    rows = []
    for role in ("user", "assistant"):
        rows += cur.execute(
            "SELECT content_hash, session_id, ts, kind_hint, role, user_text, asst_context "
            "FROM episodes WHERE analyzed_run IS NULL AND role = ? ORDER BY ts DESC LIMIT ?",
            (role, per_role)).fetchall()
    # One side may be exhausted — top up from the other.
    if len(rows) < BATCH * MAX_CALLS:
        have = {r[0] for r in rows}
        rows += [r for r in cur.execute(
            "SELECT content_hash, session_id, ts, kind_hint, role, user_text, asst_context "
            "FROM episodes WHERE analyzed_run IS NULL ORDER BY ts DESC LIMIT ?",
            (BATCH * MAX_CALLS * 2,)).fetchall() if r[0] not in have][: BATCH * MAX_CALLS - len(rows)]
    if not rows:
        print("nothing new to analyze")
        return
    n_user = sum(1 for r in rows if r[4] == "user")
    n_asst = len(rows) - n_user

    prior = [r[0] for r in cur.execute(
        "SELECT title FROM suggestions ORDER BY id DESC LIMIT 40").fetchall()]
    prior_txt = "\n".join(f"- {t}" for t in prior) or "(none yet)"

    started = datetime.now(timezone.utc)
    run_id = cur.execute(
        "INSERT INTO runs (started, model, episodes_user, episodes_assistant, status) VALUES (?,?,?,?,?)",
        (started.isoformat(timespec="seconds"), SUGGEST_MODEL, n_user, n_asst, "running")).lastrowid
    conn.commit()

    all_sugs, analyzed_hashes, failed = [], [], 0
    chunks = [rows[i:i + BATCH] for i in range(0, len(rows), BATCH)]
    ci = calls = 0
    while chunks:
        chunk = chunks.pop(0)
        ci += 1
        calls += 1
        prompt = PROMPT.replace("{prior}", prior_txt) + "\n\n".join(fmt(r) for r in chunk)
        try:
            # Reasoning models (deepseek, qwen3 with thinking) consume
            # max_tokens on thinking: leave ample headroom for the text budget.
            out = chat_json([{"role": "user", "content": prompt}], max_tokens=8000, deadline=240, model=SUGGEST_MODEL)
            sugs = out.get("suggestions", [])
            by_id = {f"{(r[1] or '?')[:8]}:{r[0][:6]}": r for r in chunk}
            for s in sugs:
                s["_evidence"] = [by_id[e] for e in s.get("episodes", []) if e in by_id]
                if s.get("category") and s.get("title"):
                    all_sugs.append(s)
            analyzed_hashes += [r[0] for r in chunk]
            print(f"  batch {ci}: {len(sugs)} suggestions", flush=True)
        except Exception as e:
            if len(chunk) > 8 and calls < MAX_CALLS * 2:
                # Dense batches can exhaust the text budget on reasoning:
                # split and retry once; the halves re-enter the queue.
                mid = len(chunk) // 2
                chunks.insert(0, chunk[mid:])
                chunks.insert(0, chunk[:mid])
                calls -= 1  # the failed attempt is retried via the halves
                print(f"  batch {ci}: failed ({e}) — splitting {len(chunk)} → {mid}+{len(chunk) - mid}", flush=True)
            else:
                failed += 1
                # Failed batches stay unanalyzed so the next run retries them.
                print(f"  batch {ci} FAILED permanently ({len(chunk)} eps): {e}", flush=True)

    # Persist: new suggestions insert; repeats merge new evidence ids.
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    merged = 0
    for s in all_sugs:
        ids = json.dumps([f"{(r[1] or '?')[:8]}:{r[0][:6]}" for r in s["_evidence"]])
        try:
            cur.execute(
                "INSERT INTO suggestions (run_id, created, category, title, detail, episode_ids) "
                "VALUES (?,?,?,?,?,?)",
                (run_id, now, s["category"], s["title"], s.get("detail", ""), ids))
        except Exception:
            # UNIQUE(category, title) hit → recurrence: fold new evidence in.
            old = cur.execute("SELECT episode_ids FROM suggestions WHERE category=? AND title=?",
                              (s["category"], s["title"])).fetchone()
            old_ids = set(json.loads(old[0])) if old and old[0] else set()
            old_ids.update(json.loads(ids))
            cur.execute("UPDATE suggestions SET episode_ids=?, run_id=? WHERE category=? AND title=?",
                        (json.dumps(sorted(old_ids)), run_id, s["category"], s["title"]))
            merged += 1

    if analyzed_hashes:
        cur.execute("UPDATE episodes SET analyzed_run=? WHERE content_hash IN (%s)"
                    % ",".join("?" * len(analyzed_hashes)), [run_id] + analyzed_hashes)
    cur.execute("UPDATE runs SET finished=?, n_suggestions=?, status=? WHERE id=?",
                (now, len(all_sugs), "ok" if failed == 0 else f"partial ({failed} batches failed)", run_id))
    conn.commit()

    # Markdown run section — newest runs at the top.
    lines = [f"## Run {started.strftime('%Y-%m-%d %H:%M')}Z — {len(analyzed_hashes)}/{len(rows)} episodes analyzed "
             f"({n_user} user, {n_asst} assistant) — {len(all_sugs)} suggestions"
             + (f" ({merged} recurrences merged)" if merged else "")]
    for s in all_sugs:
        ev = ", ".join(f"`{(r[1] or '?')[:8]}:{r[0][:6]}` ({r[4]}, {(r[2] or '')[:10]})"
                       for r in s["_evidence"]) or "(ids not resolvable)"
        lines.append(f"\n### [{s['category']}] {s['title']}\n{s.get('detail', '')}\nEvidence: {ev}")
    section = "\n".join(lines) + "\n\n"
    prev = ""
    if os.path.exists(MD):
        with open(MD) as f:
            prev = f.read()
    header = ("# Learning suggestions (auto-generated by pipeline/suggest.py)\n\n"
              "Suggestions only — nothing here is implemented. Review, pick, then hand to a session.\n"
              "Evidence ids are `<session8>:<hash6>`; resolve with "
              "`sqlite3 episodes.db 'select * from episodes where content_hash like \"<hash6>%\"'`.\n\n")
    with open(MD, "w") as f:
        f.write(header + section + prev.replace(header, ""))
    print(f"run {run_id}: {len(all_sugs)} suggestions ({merged} merged), {len(analyzed_hashes)}/{len(rows)} episodes marked analyzed")
    print(f"→ {MD}")


if __name__ == "__main__":
    main()
