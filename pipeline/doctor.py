#!/usr/bin/env python3
"""Sanity check: transcripts found, embedding endpoint reachable, LLM endpoint reachable.
Run this first after cloning. Exits non-zero if anything is missing."""
import json, os, sys, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common


def check(name, ok, detail=""):
    print(f"{'✓' if ok else '✗'} {name}" + (f" — {detail}" if detail else ""))
    return ok


ok = True

# 1. transcripts
n = len(glob.glob(os.path.join(common.TRANSCRIPTS, "*", "*.jsonl")))
ok &= check("transcripts", n > 0,
            f"{n} .jsonl under {common.TRANSCRIPTS}" if n else
            f"nothing under {common.TRANSCRIPTS} — set LEARN_TRANSCRIPTS")

# 2. embeddings
try:
    embs = common.embed_batch(["hello world"])
    ok &= check("embeddings", True,
                f"{common.EMBED_URL} model={common.EMBED_MODEL} dim={len(embs[0])}")
except Exception as e:
    ok &= check("embeddings", False,
                f"{common.EMBED_URL} model={common.EMBED_MODEL}: {e} "
                f"(ollama pull {common.EMBED_MODEL}?)")

# 3. LLM
try:
    out = common.chat_json([{"role": "user", "content":
                             'Reply with ONLY this JSON: {"ok": true}'}],
                           max_tokens=2000, deadline=120)
    ok &= check("llm", bool(out.get("ok")),
                f"{common.LLM_BASE_URL} model={common.LLM_MODEL}")
except Exception as e:
    ok &= check("llm", False,
                f"{common.LLM_BASE_URL} model={common.LLM_MODEL}: {e}")

print(f"\noutput dir: {common.OUT}")
sys.exit(0 if ok else 1)
