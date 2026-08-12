#!/usr/bin/env python3
"""Stage 4: cluster embeddings (numpy-only greedy cosine-threshold union-find).
Discovers unknown patterns without an LLM."""
import os, sys, struct
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import db

THRESH = float(os.environ.get("LEARN_CLUSTER_THRESHOLD", "0.80"))
CHUNK = 2048


def main():
    conn = db(); cur = conn.cursor()
    rows = cur.execute("SELECT content_hash, embedding FROM episodes WHERE embedding IS NOT NULL").fetchall()
    n = len(rows)
    print(f"clustering {n} episodes (cos sim >= {THRESH})")
    if n < 10:
        print("not enough data")
        return
    hashes = [r[0] for r in rows]
    dim = len(rows[0][1]) // 4
    E = np.empty((n, dim), dtype=np.float32)
    for i, (_, blob) in enumerate(rows):
        E[i] = struct.unpack(f"{dim}f", blob)
    E /= np.linalg.norm(E, axis=1, keepdims=True) + 1e-9

    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i0 in range(0, n, CHUNK):
        i1 = min(i0 + CHUNK, n)
        S = E[i0:i1] @ E.T
        for a in range(i1 - i0):
            sims = S[a]
            for b in np.nonzero(sims >= THRESH)[0]:
                if b > i0 + a:
                    union(i0 + a, int(b))
        print(f"  pass rows {i0}-{i1}")

    clusters = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)
    cur.execute("UPDATE episodes SET cluster_id=NULL")
    sizes = []
    for ci, (root, members) in enumerate(sorted(clusters.items(), key=lambda kv: -len(kv[1]))):
        for m in members:
            cur.execute("UPDATE episodes SET cluster_id=? WHERE content_hash=?", (ci, hashes[m]))
        sizes.append((ci, len(members)))
    conn.commit()
    multi = [(c, s) for c, s in sizes if s >= 3]
    print(f"clusters: {len(sizes)} total, {len(multi)} with >=3 members, singletons: {sum(1 for _, s in sizes if s == 1)}")

    print("\n== top clusters ==")
    for ci, s in multi[:25]:
        texts = cur.execute("""SELECT user_text, COUNT(*) as c FROM episodes WHERE cluster_id=?
                               GROUP BY substr(user_text,1,80) ORDER BY c DESC LIMIT 2""", (ci,)).fetchall()
        print(f"\n[cluster {ci}] {s} episodes")
        for t, c in texts:
            print(f"   ({c}x) {t[:140]}")


if __name__ == "__main__":
    main()
