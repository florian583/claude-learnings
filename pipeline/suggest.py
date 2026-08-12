#!/usr/bin/env python3
"""Stage 5: LLM learning suggestions over unanalyzed episodes. Incremental.

Consumes episodes where analyzed_run IS NULL (user + assistant prose), asks the
configured LLM for SUGGESTIONS ONLY — skills to create, rules to reinforce,
recurring problems, workflow improvements — each grounded in the episode ids it
was derived from.

Isolation: episodes are classified per project (common.classify_project) and
LLM batches are project-homogeneous, so a finding from Well can never merge
evidence from Peetchr or personal sessions. Dedup key: (project, category,
title).

Output (a regenerated view of the DB — the DB is the source of truth):
  <OUT>/suggestions/INDEX.md                     — tables per project
  <OUT>/suggestions/<project>/<tag>--<slug>.md   — one file per finding
Never implements anything; the tree is a review queue for a human.
"""
import json, os, re, sys, time
from collections import defaultdict
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import db, chat_json, SUGGEST_MODEL, OUT, classify_project, slugify

BATCH = 40            # episodes per LLM call
# Bound LLM calls per run; the rest waits for the next run. Default keeps up
# with ~740 new episodes/day (25 calls × 40). Override: SUGGEST_MAX_CALLS=200.
MAX_CALLS = int(os.environ.get("SUGGEST_MAX_CALLS", "25"))
TREE = os.path.join(OUT, "suggestions")
INDEX = os.path.join(TREE, "INDEX.md")
LEGACY_MD = os.path.join(OUT, "SUGGESTIONS.md")

CATEGORY_TAGS = {
    "new-skill": "skill",
    "reinforce": "reinforce",
    "recurring-problem": "problem",
    "workflow": "workflow",
    "hygiene": "hygiene",
}

PROMPT = """You analyze episodes from AI pair-programming transcripts (user messages AND assistant prose) and extract LEARNING SUGGESTIONS for the human who runs these sessions.

All episodes in this batch come from ONE project: "{project}". Do not assume anything about other projects.

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

PRIOR suggestion titles for project "{project}" (reuse if same pattern, else ignore):
{prior}

Return ONLY JSON:
{{"suggestions": [{{"category": "...", "title": "...", "detail": "<=3 sentences: what happens, why it matters, what to consider doing", "episodes": ["..."]}}]}}

EPISODES:
"""


def fmt(row):
    h, sid, ts, kind, role, text, ctx, proj = row
    eid = f"{(sid or '?')[:8]}:{h[:6]}"
    return (f"### id={eid} role={role} kind={kind or '?'} ts={(ts or '')[:16]}\n"
            f"TEXT: {text[:900]}\nCONTEXT: {(ctx or '')[:400]}")


def eid_of(row):
    return f"{(row[1] or '?')[:8]}:{row[0][:6]}"


# --------------------------------------------------------------------------
# Markdown tree rendering (full regeneration from DB every run)

def _finding_path(project, category, title):
    tag = CATEGORY_TAGS.get(category, slugify(category, 12))
    return os.path.join(TREE, slugify(project, 30), f"{tag}--{slugify(title)}.md")


def _resolve_evidence(cur, episode_ids):
    """'sid8:hash6' → [(eid, role, date), ...] newest first (uncapped)."""
    out = []
    for eid in episode_ids:
        sid, _, h = eid.partition(":")
        row = cur.execute(
            "SELECT role, ts FROM episodes WHERE session_id LIKE ? AND content_hash LIKE ? LIMIT 1",
            (f"{sid}%", f"{h}%")).fetchone() if sid and h else None
        out.append((eid, row[0] if row else "?", (row[1] or "")[:10] if row else "?"))
    out.sort(key=lambda e: e[2], reverse=True)
    return out


def render_tree(conn, last_run_line=""):
    """Regenerate suggestions/ (INDEX.md + one file per finding) from the DB."""
    cur = conn.cursor()
    rows = cur.execute(
        """SELECT s.id, s.category, s.title, s.detail, s.episode_ids, s.project,
                  s.occurrences, s.created, r.started
           FROM suggestions s LEFT JOIN runs r ON r.id = s.run_id
           ORDER BY s.project, r.started DESC""").fetchall()
    os.makedirs(TREE, exist_ok=True)

    written, by_project, records = set(), defaultdict(list), []
    for sid, cat, title, detail, ids_json, project, occ, created, last_seen in rows:
        project = project or "personal"
        path = _finding_path(project, cat, title)
        rel = os.path.relpath(path, TREE)
        written.add(rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        status = "open"
        if os.path.exists(path):
            m = re.search(r"^- Status:\s*(\S+)", open(path).read(), re.M)
            if m:
                status = m.group(1)

        try:
            ids = json.loads(ids_json or "[]")
        except Exception:
            ids = []
        ev_all = _resolve_evidence(cur, ids)
        ev, hidden = ev_all[:15], max(0, len(ev_all) - 15)
        ev_lines = "\n".join(f"- `{e}` — {role} — {date}" for e, role, date in ev) \
            or "- (evidence ids no longer resolvable)"
        if hidden:
            ev_lines += f"\n- … +{hidden} more (see DB or index.json)"

        first = (created or "")[:10]
        last = (last_seen or created or "")[:10]
        with open(path, "w") as f:
            f.write(f"# [{cat}] {title}\n\n"
                    f"- Project: {project}\n"
                    f"- Category: {cat}\n"
                    f"- Status: {status}   <!-- edit: open | in-progress | done | dismissed -->\n"
                    f"- Occurrences: {occ or 1} run(s) · first seen {first} · last seen {last}\n\n"
                    f"## Detail\n\n{detail or '(none)'}\n\n"
                    f"## Evidence (newest first)\n\n{ev_lines}\n\n"
                    f"Resolve an id `<session8>:<hash6>` with `ctraces show <session8>` or "
                    f"`sqlite3 episodes.db \"SELECT user_text FROM episodes WHERE content_hash LIKE '<hash6>%'\"`.\n")
        item = dict(title=title, cat=cat, rel=rel, occ=occ or 1,
                    last=last, first=first, status=status)
        by_project[project].append(item)
        records.append({
            "id": sid, "project": project, "category": cat, "title": title,
            "detail": detail or "", "status": status, "occurrences": occ or 1,
            "first_seen": first, "last_seen": last, "file": rel,
            "evidence": [{"id": e, "role": role, "date": date} for e, role, date in ev_all],
        })

    # Index — one table per project, newest activity first.
    total = sum(len(v) for v in by_project.values())
    lines = ["# Learning suggestions index", "",
             f"_{total} findings across {len(by_project)} project(s)."
             + (f" {last_run_line}" if last_run_line else "") + "_", "",
             "Files are regenerated from `episodes.db` — edit only the `Status:` line "
             "inside a finding file; everything else is overwritten each run.", ""]
    for proj in sorted(by_project, key=lambda p: -len(by_project[p])):
        items = sorted(by_project[proj], key=lambda i: i["last"], reverse=True)
        lines += [f"## 📁 {proj} ({len(items)})", "",
                  "| Finding | Category | Runs | First → last seen | Status |",
                  "|---|---|---|---|---|"]
        for i in items:
            lines.append(f"| [{i['title']}]({i['rel']}) | {i['cat']} | {i['occ']} "
                         f"| {i['first']} → {i['last']} | {i['status']} |")
        lines.append("")
    with open(INDEX, "w") as f:
        f.write("\n".join(lines))

    # Machine-readable mirror for agents: filter with jq by project, category,
    # status, dates — no sqlite3 needed on the machine reading it.
    with open(os.path.join(TREE, "index.json"), "w") as f:
        json.dump({
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_run": last_run_line,
            "n_findings": len(records),
            "projects": {p: len(v) for p, v in
                         sorted(by_project.items(), key=lambda kv: -len(kv[1]))},
            "findings": sorted(records, key=lambda r: (r["project"], r["last_seen"]),
                               reverse=True),
        }, f, indent=1, ensure_ascii=False)
        f.write("\n")

    # Drop files for findings that no longer exist (dedupe merges, deletions).
    for root, _dirs, files in os.walk(TREE):
        for name in files:
            if not name.endswith(".md") or name == "INDEX.md":
                continue
            rel = os.path.relpath(os.path.join(root, name), TREE)
            if rel not in written:
                os.remove(os.path.join(root, name))
    # Prune emptied project dirs.
    for name in os.listdir(TREE):
        d = os.path.join(TREE, name)
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
    return total


# --------------------------------------------------------------------------

def main():
    conn = db(); cur = conn.cursor()
    # Balanced mix: assistant prose dwarfs user messages (~5:1), so a plain
    # newest-first window would starve the user side (where corrections live).
    per_role = (BATCH * MAX_CALLS) // 2
    rows = []
    for role in ("user", "assistant"):
        rows += cur.execute(
            "SELECT content_hash, session_id, ts, kind_hint, role, user_text, asst_context, project "
            "FROM episodes WHERE analyzed_run IS NULL AND role = ? ORDER BY ts DESC LIMIT ?",
            (role, per_role)).fetchall()
    # One side may be exhausted — top up from the other.
    if len(rows) < BATCH * MAX_CALLS:
        have = {r[0] for r in rows}
        rows += [r for r in cur.execute(
            "SELECT content_hash, session_id, ts, kind_hint, role, user_text, asst_context, project "
            "FROM episodes WHERE analyzed_run IS NULL ORDER BY ts DESC LIMIT ?",
            (BATCH * MAX_CALLS * 2,)).fetchall() if r[0] not in have][: BATCH * MAX_CALLS - len(rows)]
    if not rows:
        print("nothing new to analyze")
        render_tree(conn)
        return
    n_user = sum(1 for r in rows if r[4] == "user")
    n_asst = len(rows) - n_user

    # Project-homogeneous batches, round-robin across projects for fairness:
    # a finding is always derived from a single project's evidence.
    by_proj = defaultdict(list)
    for r in rows:
        by_proj[classify_project(r[7])].append(r)
    chunk_lists = {p: [rs[i:i + BATCH] for i in range(0, len(rs), BATCH)]
                   for p, rs in by_proj.items()}
    chunks = []
    while any(chunk_lists.values()):
        for p in sorted(chunk_lists):
            if chunk_lists[p]:
                chunks.append((p, chunk_lists[p].pop(0)))
    chunks = chunks[:MAX_CALLS]

    started = datetime.now(timezone.utc)
    run_id = cur.execute(
        "INSERT INTO runs (started, model, episodes_user, episodes_assistant, status) VALUES (?,?,?,?,?)",
        (started.isoformat(timespec="seconds"), SUGGEST_MODEL, n_user, n_asst, "running")).lastrowid
    conn.commit()

    priors = {}
    all_sugs, analyzed_hashes, failed = [], [], 0
    ci = calls = 0
    while chunks:
        proj, chunk = chunks.pop(0)
        ci += 1
        calls += 1
        if proj not in priors:
            prior = [r[0] for r in cur.execute(
                "SELECT title FROM suggestions WHERE project = ? ORDER BY id DESC LIMIT 40",
                (proj,)).fetchall()]
            priors[proj] = "\n".join(f"- {t}" for t in prior) or "(none yet)"
        prompt = (PROMPT.replace("{project}", proj).replace("{prior}", priors[proj])
                  + "\n\n".join(fmt(r) for r in chunk))
        try:
            # Reasoning models consume max_tokens for thinking: leave headroom.
            out = chat_json([{"role": "user", "content": prompt}], max_tokens=8000, deadline=420, model=SUGGEST_MODEL)
            sugs = out.get("suggestions", [])
            by_id = {eid_of(r): r for r in chunk}
            for s in sugs:
                s["_evidence"] = [by_id[e] for e in s.get("episodes", []) if e in by_id]
                s["_project"] = proj
                if s.get("category") and s.get("title"):
                    all_sugs.append(s)
            analyzed_hashes += [r[0] for r in chunk]
            print(f"  batch {ci} [{proj}]: {len(sugs)} suggestions", flush=True)
        except Exception as e:
            if len(chunk) > 8 and calls < MAX_CALLS * 2:
                # Dense batches can exhaust the text budget on reasoning:
                # split and retry once; the halves re-enter the queue.
                mid = len(chunk) // 2
                chunks.insert(0, (proj, chunk[mid:]))
                chunks.insert(0, (proj, chunk[:mid]))
                calls -= 1  # the failed attempt is retried via the halves
                print(f"  batch {ci} [{proj}]: failed ({e}) — splitting {len(chunk)} → {mid}+{len(chunk) - mid}", flush=True)
            else:
                failed += 1
                # Failed batches stay unanalyzed so the next run retries them.
                print(f"  batch {ci} [{proj}] FAILED permanently ({len(chunk)} eps): {e}", flush=True)

    # Persist: new suggestions insert; repeats (same project+category+title)
    # merge new evidence ids and bump the occurrence counter.
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    merged = 0
    for s in all_sugs:
        ids = json.dumps([eid_of(r) for r in s["_evidence"]])
        try:
            cur.execute(
                "INSERT INTO suggestions (run_id, created, category, title, detail, episode_ids, project, occurrences) "
                "VALUES (?,?,?,?,?,?,?,1)",
                (run_id, now, s["category"], s["title"], s.get("detail", ""), ids, s["_project"]))
        except Exception:
            old = cur.execute(
                "SELECT episode_ids, occurrences FROM suggestions WHERE project=? AND category=? AND title=?",
                (s["_project"], s["category"], s["title"])).fetchone()
            old_ids = set(json.loads(old[0])) if old and old[0] else set()
            old_ids.update(json.loads(ids))
            cur.execute(
                "UPDATE suggestions SET episode_ids=?, run_id=?, occurrences=COALESCE(occurrences,1)+1 "
                "WHERE project=? AND category=? AND title=?",
                (json.dumps(sorted(old_ids)), run_id, s["_project"], s["category"], s["title"]))
            merged += 1

    if analyzed_hashes:
        cur.execute("UPDATE episodes SET analyzed_run=? WHERE content_hash IN (%s)"
                    % ",".join("?" * len(analyzed_hashes)), [run_id] + analyzed_hashes)
    cur.execute("UPDATE runs SET finished=?, n_suggestions=?, status=? WHERE id=?",
                (now, len(all_sugs), "ok" if failed == 0 else f"partial ({failed} batches failed)", run_id))
    conn.commit()

    # One-time migration of the old append-only log.
    if os.path.exists(LEGACY_MD):
        os.rename(LEGACY_MD, os.path.join(OUT, "SUGGESTIONS-archive-v1.md"))

    last_run = (f"Last run {started.strftime('%Y-%m-%d %H:%M')}Z: "
                f"{len(analyzed_hashes)}/{len(rows)} episodes ({n_user} user, {n_asst} assistant), "
                f"{len(all_sugs)} suggestions, {merged} recurrences merged.")
    total = render_tree(conn, last_run)
    print(f"run {run_id}: {len(all_sugs)} suggestions ({merged} merged), "
          f"{len(analyzed_hashes)}/{len(rows)} episodes marked analyzed")
    print(f"→ {INDEX} ({total} findings)")


if __name__ == "__main__":
    main()
