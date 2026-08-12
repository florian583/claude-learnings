#!/bin/sh
# Full incremental pipeline run. Safe to re-run anytime (content-hash dedup + NULL-column staging).
cd "$(dirname "$0")"
echo "=== stage 1: extract ===";  python3 extract_episodes.py || exit 1
echo "=== stage 2: embed ===";     python3 embed.py || exit 1
echo "=== stage 3: classify ===";  python3 classify.py || exit 1
echo "=== stage 4: cluster ===";   python3 cluster.py
echo "=== stage 5: suggest ===";   python3 suggest.py
