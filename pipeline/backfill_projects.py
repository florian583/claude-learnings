#!/usr/bin/env python3
"""One-off: (re)classify the project of EXISTING suggestions from their
evidence episodes, then merge rows that collide on (project, category, title)
after reclassification. Safe to re-run (e.g. after editing projects.json).

Run after deploying the classify_project-enabled common.py. Finishes by
regenerating the markdown tree (suggestions/INDEX.md + finding files).
"""
import json, os, sys
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import db, classify_project


def evidence_project(cur, episode_ids):
    """Dominant classified project across resolvable evidence episodes."""
    projs = []
    for eid in episode_ids:
        sid, _, h = eid.partition(":")
        if not (sid and h):
            continue
        row = cur.execute(
            "SELECT project FROM episodes WHERE session_id LIKE ? AND content_hash LIKE ? LIMIT 1",
            (f"{sid}%", f"{h}%")).fetchone()
        if row:
            projs.append(classify_project(row[0]))
    if not projs:
        return None
    return Counter(projs).most_common(1)[0][0]


def main():
    conn = db(); cur = conn.cursor()
    rows = cur.execute(
        "SELECT id, category, title, episode_ids, project FROM suggestions").fetchall()
    print(f"{len(rows)} suggestions to classify")

    changed = unchanged = unresolved = 0
    for sid, cat, title, ids_json, old_proj in rows:
        try:
            ids = json.loads(ids_json or "[]")
        except Exception:
            ids = []
        proj = evidence_project(cur, ids)
        if proj is None:
            # No resolvable evidence: keep what we have, default 'personal'.
            proj = old_proj or "personal"
            unresolved += 1
        if proj != old_proj:
            cur.execute("UPDATE suggestions SET project=? WHERE id=?", (proj, sid))
            changed += 1
        else:
            unchanged += 1
    print(f"classified: {changed} changed, {unchanged} unchanged, {unresolved} without resolvable evidence")

    # Reclassification can create (project, category, title) collisions —
    # merge them: union evidence, sum occurrences, keep the newest run_id.
    dupes = cur.execute(
        """SELECT project, category, title, COUNT(*) c FROM suggestions
           GROUP BY project, category, title HAVING c > 1""").fetchall()
    for proj, cat, title, c in dupes:
        group = cur.execute(
            """SELECT id, episode_ids, occurrences, run_id FROM suggestions
               WHERE project IS ? AND category=? AND title=? ORDER BY id""",
            (proj, cat, title)).fetchall()
        keep = group[0]
        ids, occ, run = set(), 0, keep[3]
        for gid, ids_json, g_occ, g_run in group:
            try:
                ids.update(json.loads(ids_json or "[]"))
            except Exception:
                pass
            occ += g_occ or 1
            run = max(run, g_run) if run and g_run else (run or g_run)
        cur.execute("UPDATE suggestions SET episode_ids=?, occurrences=?, run_id=? WHERE id=?",
                    (json.dumps(sorted(ids)), occ, run, keep[0]))
        for gid, *_ in group[1:]:
            cur.execute("DELETE FROM suggestions WHERE id=?", (gid,))
        print(f"  merged {c} rows: [{proj}/{cat}] {title}")
    conn.commit()

    from suggest import render_tree
    total = render_tree(conn, "Tree regenerated after project backfill.")
    final = cur.execute(
        "SELECT project, COUNT(*) FROM suggestions GROUP BY project ORDER BY 2 DESC").fetchall()
    print(f"→ {total} findings rendered; per project: " +
          ", ".join(f"{p}={n}" for p, n in final))


if __name__ == "__main__":
    main()
