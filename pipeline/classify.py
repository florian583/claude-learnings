#!/usr/bin/env python3
"""Stage 3: classify user episodes via the configured LLM.
Incremental: WHERE labels IS NULL."""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import db, chat_json, CLASSIFY_MODEL

BATCH = 8
WORKERS = int(os.environ.get("LEARN_CLASSIFY_WORKERS", "4"))

PROMPT = """You classify episodes from AI pair-programming session transcripts. Each episode = a user message plus the assistant's preceding context.

For EACH episode return:
- "i": the episode index (integer, copy it)
- "kind": one of "correction","instruction","question","audit-request","pr-ops","orchestration","smalltalk","automation-noise","other"
- "correction": null OR one of "scope-drift","wrong-facts","incomplete-recon","forgot-context","still-broken","style-preference","process-violation","tool-misuse"
- "severity": null OR "nit","rework","blocked"
- "skill_candidate": null OR a short kebab-case name if this task recurs and deserves a skill/command
- "friction": 0-10 (how much friction/frustration this episode shows)
- "summary": <=12 words describing what the user wanted

Return ONLY JSON: {"episodes": [...]}

EPISODES:
"""


def fmt(ep):
    h, user, ctx, tools = ep
    return f"### idx={{IDX}}\nUSER: {user[:900]}\nASSISTANT-BEFORE: {(ctx or '')[:500]}\nTOOLS: {tools or ''}"


def classify_chunk(chunk):
    msgs = [{"role": "user", "content": PROMPT + "\n\n".join(
        fmt(e).replace("{IDX}", str(j)) for j, e in enumerate(chunk))}]
    out = chat_json(msgs, model=CLASSIFY_MODEL)
    res = out.get("episodes", [])
    by_i = {}
    for r in res:
        try:
            by_i[int(r["i"])] = r
        except Exception:
            pass
    return [(chunk[j][0], json.dumps(by_i[j])) for j in range(len(chunk)) if j in by_i]


def main():
    conn = db(); cur = conn.cursor()
    rows = cur.execute(
        "SELECT content_hash, user_text, asst_context, tool_outcome FROM episodes "
        "WHERE labels IS NULL AND role = 'user'").fetchall()
    conn.close()
    print(f"to classify: {len(rows)}", flush=True)
    chunks = [rows[i:i + BATCH] for i in range(0, len(rows), BATCH)]
    t0 = time.time(); done = 0; failed = 0
    wconn = db(); wcur = wconn.cursor()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(classify_chunk, c): c for c in chunks}
        for fut in as_completed(futs):
            try:
                results = fut.result(timeout=300)  # hard cap per chunk
                for h, labels in results:
                    wcur.execute("UPDATE episodes SET labels=? WHERE content_hash=?", (labels, h))
                    done += 1
                wconn.commit()  # commit per chunk: no lost work on kill
            except Exception as e:
                failed += len(futs[fut])
                if failed <= 40:
                    print(f"chunk failed: {str(e)[:150]}", flush=True)
            if done % 200 < BATCH:
                print(f"  {done}/{len(rows)} ({done / max(time.time() - t0, 1):.1f}/s, {failed} failed)", flush=True)
    wconn.commit(); wconn.close()
    print(f"classified {done}, failed {failed}, in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
