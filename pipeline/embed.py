#!/usr/bin/env python3
"""Stage 2: embed user episodes. Incremental: WHERE embedding IS NULL.
Default: local Ollama (see README for `ollama pull`)."""
import os, sys, struct, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import db, embed_batch

BATCH = 32


def main():
    conn = db(); cur = conn.cursor()
    rows = cur.execute(
        "SELECT content_hash, user_text, asst_context FROM episodes "
        "WHERE embedding IS NULL AND role = 'user'").fetchall()
    print(f"to embed: {len(rows)}")
    t0 = time.time(); done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        texts = [(r[1] + "\n---\n" + (r[2] or ""))[:3000] for r in chunk]
        try:
            embs = embed_batch(texts)
        except Exception as e:
            print(f"batch {i} FAILED: {e}")
            break
        for (h, _, _), e in zip(chunk, embs):
            cur.execute("UPDATE episodes SET embedding=? WHERE content_hash=?",
                        (struct.pack(f"{len(e)}f", *e), h))
        conn.commit()
        done += len(chunk)
        if done % 320 < BATCH:
            rate = done / (time.time() - t0)
            print(f"  {done}/{len(rows)} ({rate:.0f}/s)")
    print(f"embedded {done} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
