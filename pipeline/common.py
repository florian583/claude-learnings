#!/usr/bin/env python3
"""Shared: config (env-driven), DB schema, LLM + embedding clients.

Everything is configurable via environment variables — see README.md.

LLM: any OpenAI-compatible chat-completions endpoint (Ollama /v1, Vercel AI
Gateway, OpenRouter, LiteLLM, OpenCode Go, ...). Default is fully local Ollama.
Embeddings: Ollama /api/embed by default; any OpenAI-style /v1/embeddings
endpoint also works (detected by URL path).
"""
import json, os, sqlite3, time

HOME = os.path.expanduser("~")

# --- paths ---
TRANSCRIPTS = os.path.expanduser(os.environ.get(
    "LEARN_TRANSCRIPTS", "~/.claude/projects"))
OUT = os.path.expanduser(os.environ.get(
    "LEARN_OUT", "~/.claude-learnings"))
DB = os.path.join(OUT, "episodes.db")

# --- LLM (OpenAI-compatible chat completions) ---
LLM_BASE_URL = os.environ.get("LEARN_LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LEARN_LLM_API_KEY", "ollama")
LLM_MODEL = os.environ.get("LEARN_LLM_MODEL", "qwen3:8b")
# Per-stage overrides. Classify is a cheap taxonomy task — a tiny local model
# (e.g. LFM2.5-VL-3B, ~1.7GB, benchmarked 3x faster than cloud deepseek on this
# workload) is plenty. Suggest is quality-sensitive: give it your best model.
CLASSIFY_MODEL = os.environ.get("LEARN_CLASSIFY_MODEL", LLM_MODEL)
SUGGEST_MODEL = os.environ.get("LEARN_SUGGEST_MODEL", LLM_MODEL)

# --- embeddings ---
EMBED_URL = os.environ.get("LEARN_EMBED_URL", "http://localhost:11434/api/embed")
EMBED_MODEL = os.environ.get("LEARN_EMBED_MODEL", "qwen3-embedding:4b")
EMBED_API_KEY = os.environ.get("LEARN_EMBED_API_KEY", "")  # only for hosted endpoints

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
  content_hash TEXT PRIMARY KEY,
  session_id TEXT, project TEXT, ts TEXT,
  kind_hint TEXT,
  user_text TEXT,
  asst_context TEXT,
  tool_outcome TEXT,
  embedding BLOB,
  labels TEXT,
  cluster_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ep_cluster ON episodes(cluster_id);
"""

# Migrations for DBs created by older versions (IF NOT EXISTS won't add columns).
MIGRATIONS = (
    "ALTER TABLE episodes ADD COLUMN role TEXT DEFAULT 'user'",
    "ALTER TABLE episodes ADD COLUMN analyzed_run INTEGER",
    """CREATE TABLE IF NOT EXISTS runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      started TEXT, finished TEXT, model TEXT,
      episodes_user INTEGER, episodes_assistant INTEGER,
      n_suggestions INTEGER, status TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS suggestions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id INTEGER, created TEXT,
      category TEXT, title TEXT, detail TEXT,
      episode_ids TEXT,
      UNIQUE(category, title)
    )""",
)


def db():
    os.makedirs(OUT, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=60)
    conn.executescript(SCHEMA)
    for sql in MIGRATIONS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # column/table already present
    return conn


# ---------- embeddings ----------

def embed_batch(texts, retries=3):
    """Embed a list of texts. Ollama /api/embed by default; if EMBED_URL looks
    like an OpenAI endpoint (*/v1/embeddings) speak that protocol instead."""
    import urllib.request
    openai_style = EMBED_URL.rstrip("/").endswith("/embeddings")
    if openai_style:
        payload = {"model": EMBED_MODEL, "input": texts}
    else:
        payload = {"model": EMBED_MODEL, "input": texts}
    headers = {"Content-Type": "application/json"}
    if EMBED_API_KEY:
        headers["Authorization"] = f"Bearer {EMBED_API_KEY}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                EMBED_URL, data=json.dumps(payload).encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.load(r)
            if openai_style:
                return [e["embedding"] for e in sorted(d["data"], key=lambda x: x["index"])]
            return [e for e in d["embeddings"]]
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))


# ---------- LLM ----------

def _extract_json(text):
    """Pull the first balanced {...} JSON object out of an LLM response."""
    start = text.find("{")
    if start == -1:
        raise ValueError("no json object in response")
    depth = 0; in_str = False; esc = False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': in_str = False
        else:
            if ch == '"': in_str = True
            elif ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i + 1])
    raise ValueError("unbalanced json in response")


def _strip_thinking(text):
    """Drop <think>...</think> blocks that reasoning models may inline."""
    import re
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def chat_json(messages, max_tokens=4000, retries=3, deadline=240, model=None):
    """OpenAI-compatible chat-completions call expecting a JSON object back.
    model defaults to LLM_MODEL; stages pass their per-stage override.
    Hard wall-clock deadline guards against drip-streaming stalls (urllib's
    timeout only covers per-socket-ops, a slowly-streaming body never trips it).

    Socket timeout is 90s: reasoning models on dense batches can sit >30s
    before the first byte. If you front this with a proxy/gateway, keep the
    client timeout ABOVE the proxy's upstream header timeout — aborting early
    cancels the proxy request mid-flight and, on proxies with circuit
    breakers, can cascade into error storms for other clients.
    """
    import urllib.request
    body = {"model": model or LLM_MODEL, "max_tokens": max_tokens,
            "temperature": 0.1, "messages": messages}
    url = f"{LLM_BASE_URL}/chat/completions"
    for attempt in range(retries):
        t0 = time.time()
        try:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {LLM_API_KEY}"})
            with urllib.request.urlopen(req, timeout=90) as r:
                chunks = []
                while True:
                    if time.time() - t0 > deadline:
                        raise TimeoutError(f"response exceeded {deadline}s wall-clock")
                    b = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
                    if not b:
                        break
                    chunks.append(b)
            d = json.loads(b"".join(chunks))
            msg = d["choices"][0]["message"]
            text = msg.get("content") or ""
            return _extract_json(_strip_thinking(text))
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
