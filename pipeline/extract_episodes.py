#!/usr/bin/env python3
"""Stage 1: extract episodes (user msgs + substantive assistant prose, with
cross-side context) from Claude Code transcripts into SQLite.
Incremental via content_hash — safe to re-run anytime.

Reads ~/.claude/projects/**/*.jsonl directly (override: LEARN_TRANSCRIPTS).
"""
import json, os, re, sys, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import db, TRANSCRIPTS

SKIP_PREFIX = ("<command-message>", "<command-name>", "<local-command", "<system-reminder>",
               "Caveat:", "<task-notification", "<teammate-message", "Stop hook feedback:",
               "A session-scoped Stop hook", "[Request interrupted", "Base directory for this skill:",
               "Another Claude session sent a message",
               "This session is being continued")
SKIP_EXACT = re.compile(r"^(say hi|hi|ok|yes|proceed|continue|what model are you|reply (with )?exactly[ :].{0,60})$", re.I)


def norm(t, n):
    return re.sub(r"\s+", " ", t or "").strip()[:n]


# Assistant-side episodes: only substantive prose is worth an episode — pure
# tool narration ("Let me read that file…") is noise. Length gate + must
# contain sentences, not just a heading into tool calls.
ASST_MIN_LEN = 220


def asst_kind_hint(t):
    tl = t.lower()
    if re.search(r"\b(i (was wrong|guessed|made that up)|retracting|you'?re right to call)\b", tl): return "admission"
    if re.search(r"\bi (need to correct|missed|only ever looked)\b", tl): return "self-correction"
    if tl.startswith(("i'll", "let me", "now i")): return "narration"
    return "prose"


def kind_hint(t):
    tl = t.lower()
    if re.search(r"\b(no[,.!]|wrong|why did you|you forgot|stop\b|don'?t|revert|still (broken|wrong|failing)|not what)\b", tl): return "correction?"
    if re.search(r"(audit|deep dive|investigat)", tl): return "audit"
    if re.search(r"(implement|fix|build|create|add)\b", tl): return "implement"
    if re.search(r"(handle|review).*(pr|pull request)|pr.*comment", tl): return "pr-ops"
    if tl.endswith("?") or tl.startswith(("why", "what", "how", "can you", "is it", "does")): return "question"
    return "instruction"


def project_name(slug):
    # Claude Code encodes the cwd as the dir name: "-" + path with / → -
    return slug.lstrip("-").replace("-", "/", 2) if slug.startswith("-") else slug


def main():
    conn = db()
    cur = conn.cursor()
    n_new = n_skip = 0
    for proj in sorted(os.listdir(TRANSCRIPTS)):
        pdir = os.path.join(TRANSCRIPTS, proj)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".jsonl"):
                continue
            sid = fn[:-6]
            path = os.path.join(pdir, fn)
            asst_recent = []   # last assistant texts (max 3 kept)
            tool_recent = []   # recent tool outcomes
            last_user = ""     # last user text, context for assistant episodes
            try:
                f = open(path, "r", errors="replace")
            except Exception:
                continue
            with f:
                for li, line in enumerate(f):
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    rtype = rec.get("type"); msg = rec.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, str):
                        content = [{"type": "text", "text": content}]
                    if not isinstance(content, list):
                        continue
                    if rtype == "assistant":
                        atexts = []
                        for c in content:
                            if not isinstance(c, dict):
                                continue
                            if c.get("type") == "text" and c.get("text", "").strip():
                                atexts.append(c["text"])
                                asst_recent.append(norm(c["text"], 500))
                            elif c.get("type") == "tool_use":
                                tool_recent.append(f"{c.get('name', '?')}→")
                        at = norm("\n".join(atexts), 1500)
                        if len(at) >= ASST_MIN_LEN and not rec.get("isMeta"):
                            h = hashlib.sha1(f"{sid}:{rec.get('uuid') or li}:a".encode()).hexdigest()
                            cur.execute("""INSERT OR IGNORE INTO episodes
                                (content_hash, session_id, project, ts, kind_hint, user_text, asst_context, tool_outcome, role)
                                VALUES (?,?,?,?,?,?,?,?,'assistant')""",
                                (h, sid, proj, rec.get("timestamp"), asst_kind_hint(at), at,
                                 norm(last_user, 600), None))
                            if cur.rowcount:
                                n_new += 1
                        asst_recent = asst_recent[-3:]; tool_recent = tool_recent[-6:]
                    elif rtype == "user":
                        txts = []
                        for c in content:
                            if not isinstance(c, dict):
                                continue
                            if c.get("type") == "text":
                                txts.append(c.get("text", ""))
                            elif c.get("type") == "tool_result":
                                ok = "✗" if c.get("is_error") else "✓"
                                if tool_recent:
                                    tool_recent[-1] = tool_recent[-1] + ok
                        t = "\n".join(txts).strip()
                        if not t:
                            continue
                        if t.startswith(SKIP_PREFIX) or SKIP_EXACT.match(t):
                            n_skip += 1; continue
                        if rec.get("isMeta"):
                            n_skip += 1; continue
                        h = hashlib.sha1(f"{sid}:{rec.get('uuid') or li}".encode()).hexdigest()
                        episode_text = norm(t, 1500)
                        last_user = episode_text
                        ctx = " ||| ".join(asst_recent)[:1200]
                        tools = " ".join(tool_recent)[:200]
                        cur.execute("""INSERT OR IGNORE INTO episodes
                            (content_hash, session_id, project, ts, kind_hint, user_text, asst_context, tool_outcome)
                            VALUES (?,?,?,?,?,?,?,?)""",
                            (h, sid, proj, rec.get("timestamp"), kind_hint(t), episode_text, ctx, tools))
                        if cur.rowcount:
                            n_new += 1
                        asst_recent = []; tool_recent = []
    conn.commit()
    total = cur.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    print(f"new episodes: {n_new} (skipped noise: {n_skip}); total in db: {total}")


if __name__ == "__main__":
    main()
