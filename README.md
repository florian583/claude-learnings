# claude-learnings

Mine your Claude Code transcripts for **learning suggestions**: skills worth creating,
rules that keep getting violated, recurring friction, workflow improvements.

The pipeline reads your local session transcripts, extracts episodes (your messages +
substantive assistant answers, each with cross-side context), embeds and clusters them,
and asks an LLM for **suggestions only** — appended to a markdown review queue with the
evidence episode ids behind each one. Nothing is ever auto-implemented.

```
transcripts (~/.claude/projects)
        │
        ▼  1 extract        SQLite (episodes.db) — incremental, content-hash dedup
        ▼  2 embed          vectors for clustering (Ollama by default)
        ▼  3 classify       kind / correction-type / friction per user episode (LLM)
        ▼  4 cluster        numpy cosine-threshold union-find — discovers unknown patterns
        ▼  5 suggest        LLM → SUGGESTIONS.md  (per-run sections, evidence ids)
```

## Quickstart

```sh
git clone <this repo> && cd claude-learnings
pip3 install numpy            # only dependency; everything else is stdlib

# Embeddings run locally on Ollama by default:
ollama pull qwen3-embedding:4b      # ~2.5GB (0.6b variant works too, lighter)
ollama pull qwen3:8b                # default analysis model — or point at any
                                    # OpenAI-compatible endpoint instead (below)

python3 pipeline/doctor.py          # verifies transcripts + embeddings + LLM
./pipeline/run.sh                   # full incremental run
open ~/.claude-learnings/SUGGESTIONS.md
```

Re-running is always safe: stages only touch rows that are missing
(`embedding IS NULL`, `labels IS NULL`, `analyzed_run IS NULL`), and extraction
dedups by content hash. Run it on a schedule (see below) and it keeps up incrementally.

## Using any OpenAI-compatible endpoint

Everything is env-driven. The LLM just needs a chat-completions API:

```sh
# Ollama local (default — fully private, zero cost)
export LEARN_LLM_BASE_URL=http://localhost:11434/v1
export LEARN_LLM_MODEL=qwen3:8b

# Ollama Cloud
export LEARN_LLM_BASE_URL=https://ollama.com/v1
export LEARN_LLM_API_KEY=<ollama cloud key>
export LEARN_LLM_MODEL=deepseek-v4-flash

# Vercel AI Gateway
export LEARN_LLM_BASE_URL=https://ai-gateway.vercel.sh/v1
export LEARN_LLM_API_KEY=<gateway key>
export LEARN_LLM_MODEL=deepseek/deepseek-v4-flash

# OpenRouter
export LEARN_LLM_BASE_URL=https://openrouter.ai/api/v1
export LEARN_LLM_API_KEY=<key>
export LEARN_LLM_MODEL=deepseek/deepseek-v4-flash

# …same shape for LiteLLM, OpenCode Go, OpenAI itself, etc.
```

### Cheap/no-cost ways to feed it

- **[VibeProxy](https://github.com/automazeio/vibeproxy)** — local proxy that lets you use
  your existing **Codex / Claude / other subscriptions** as OpenAI-compatible endpoints,
  instead of paying per-token API rates. Point `LEARN_LLM_BASE_URL` at it and the
  pipeline runs on your subscription.
- **[OpenCode Go](https://opencode.ai/go)** (~$10 plan) — OpenAI/Anthropic-compatible
  endpoint with current open models (deepseek, kimi, glm…), works well as the
  classify/suggest model.
- **Ollama** local or cloud — the default; zero marginal cost if you already run it.

Embeddings default to **local Ollama** (`http://localhost:11434/api/embed`) — transcripts
never leave your machine for the embedding stage. Any OpenAI-style embeddings endpoint
also works (detected by a `/v1/embeddings` URL):

```sh
export LEARN_EMBED_URL=http://localhost:11434/api/embed     # default, local
export LEARN_EMBED_MODEL=qwen3-embedding:4b

# hosted variant:
export LEARN_EMBED_URL=https://ai-gateway.vercel.sh/v1/embeddings
export LEARN_EMBED_API_KEY=<key>
export LEARN_EMBED_MODEL=<embedding model>
```

## Configuration reference

| Var | Default | Purpose |
|---|---|---|
| `LEARN_TRANSCRIPTS` | `~/.claude/projects` | Where session `.jsonl` transcripts live |
| `LEARN_OUT` | `~/.claude-learnings` | Output dir (`episodes.db`, `SUGGESTIONS.md`) |
| `LEARN_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible base URL |
| `LEARN_LLM_API_KEY` | `ollama` | Bearer token for the LLM endpoint |
| `LEARN_LLM_MODEL` | `qwen3:8b` | Model for classify + suggest |
| `LEARN_CLASSIFY_MODEL` | `LEARN_LLM_MODEL` | Override for the classify stage — a tiny local model works great (e.g. `lfm25vl` / LFM2.5-VL-3B: ~1.7GB, benchmarked 3x faster than cloud deepseek on this taxonomy task) |
| `LEARN_SUGGEST_MODEL` | `LEARN_LLM_MODEL` | Override for the suggest stage — quality-sensitive, use your best model |
| `LEARN_EMBED_URL` | `http://localhost:11434/api/embed` | Embeddings endpoint |
| `LEARN_EMBED_MODEL` | `qwen3-embedding:4b` | Embedding model |
| `LEARN_MAX_CALLS` | `25` | Max LLM calls per suggest run (backfill: `LEARN_MAX_CALLS=200`) |
| `LEARN_CLUSTER_THRESHOLD` | `0.80` | Cosine similarity to merge a cluster |
| `LEARN_CLASSIFY_WORKERS` | `4` | Parallel classify calls |

Reasoning models (deepseek, qwen3 thinking) are supported — thinking blocks are stripped
and the token budget leaves headroom. Keep your client socket timeout **above** any
proxy's upstream header timeout; aborting mid-flight can trip proxy circuit breakers.

### Tiny local model for the classify stage

The classify stage is a simple taxonomy task — a ~3B local model handles it well and
runs 3x faster than a cloud call. [LFM2.5-VL-3B](https://huggingface.co/LiquidAI/LFM2.5-VL-3B-GGUF)
is a good pick (also vision-capable if you extend the pipeline to screenshots):

```sh
# Ollama doesn't ship a lfm2.5-vl tag yet — create it from the GGUF:
curl -L -o /tmp/lfm.gguf https://huggingface.co/LiquidAI/LFM2.5-VL-3B-GGUF/resolve/main/LFM2.5-VL-3B-Q4_K_M.gguf
(cd /tmp && printf 'FROM ./lfm.gguf\n' > Modelfile && ollama create lfm25vl -f Modelfile)

export LEARN_CLASSIFY_MODEL=lfm25vl     # classify: local, free, fast
export LEARN_SUGGEST_MODEL=deepseek-v4-flash   # suggest: keep a strong model
```

Small models occasionally drop an episode or two from a batch — harmless here:
unreturned episodes stay unlabeled and are retried on the next run.

## Scheduling

```sh
# cron example — every 4 hours
0 */4 * * *  /path/to/claude-learnings/pipeline/run.sh >> ~/.claude-learnings/run.log 2>&1
```

On macOS a LaunchAgent with `StartInterval` works too — it fires on wake if the
laptop was asleep at a scheduled tick. (One team pattern: run the pipeline on an
always-on machine and rsync transcripts in / the `suggestions/` tree out —
`--include='*/' --include='*.md' --exclude='*'` if you want only markdown.)

## Reading the output

The SQLite DB is the source of truth; the markdown tree is a regenerated view
(deleted and rewritten each run — the only thing preserved is your `Status:` edits):

```
~/.claude-learnings/suggestions/
├── INDEX.md                                  ← start here: one table per project
├── myapp/
│   ├── skill--pr-quality-gate.md             ← one stable file per finding
│   ├── problem--glm-empty-review-bodies.md
│   └── ...
└── personal/
    └── ...
```

`INDEX.md` groups findings **per project** (projects never mix — LLM batches are
project-homogeneous and the dedup key is `(project, category, title)`):

| Finding | Category | Runs | First → last seen | Status |
|---|---|---|---|---|
| [pr-quality-gate](myapp/skill--pr-quality-gate.md) | new-skill | 5 | 08-01 → 08-12 | open |

Each finding file carries the latest detail, occurrence count, and its evidence
ids (`<session8>:<hash6>`, newest first). Mark a finding `done` / `dismissed` by
editing the `Status:` line — the regenerator keeps it.

Project names come from the working directory of each session. The generic
heuristic (first path component after `projects/` or `worktrees/`, home dir →
`personal`) usually does the right thing; override with
`~/.claude-learnings/projects.json`:

```json
{
  "myapp": ["myrepo", "myapp-worktrees"],
  "client-x": ["clientx"],
  "personal": []
}
```

Changed the mapping after runs already happened? Reclassify history and
re-render the tree:

```sh
python3 pipeline/backfill_projects.py
```

Resolve an evidence id:

```sh
sqlite3 ~/.claude-learnings/episodes.db \
  "select user_text from episodes where content_hash like '9f8e7d%';"
```

The DB itself is plain SQLite — query clusters, labels, friction scores directly:

```sql
-- biggest recurring clusters
SELECT cluster_id, COUNT(*) c FROM episodes
WHERE cluster_id IS NOT NULL GROUP BY 1 ORDER BY c DESC LIMIT 20;

-- what the classify stage saw as corrections
SELECT json_extract(labels,'$.correction'), COUNT(*) FROM episodes
WHERE labels IS NOT NULL GROUP BY 1 ORDER BY 2 DESC;
```

## Privacy

Transcripts are read locally; the DB and markdown stay local. The only network calls
are the ones you configure — default setup is fully local via Ollama. If you point the
LLM at a cloud endpoint, episode text (your messages + assistant prose excerpts) is sent
there for classify/suggest stages. Embeddings stay local either way unless you override
`LEARN_EMBED_URL`.
