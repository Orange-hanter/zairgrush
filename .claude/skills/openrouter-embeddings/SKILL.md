---
name: openrouter-embeddings
description: OpenRouter usage for this project — the embeddings endpoint that powers swarm memory vectors, the $10 hard budget, and the measured model choice. Read before any OpenRouter call.
---

# OpenRouter — embeddings + budget discipline

Key: `OPENROUTER_API_KEY` (in `~/.zshenv`). **Hard budget: $10 total — spend wisely.** Every response carries `usage.cost` in USD; helpers record it as `cost_usd` in helper metrics. Check spend before large batches.

## Embeddings (the reason this account exists here)

`POST https://openrouter.ai/api/v1/embeddings` (OpenAI-compatible):

```json
{"model": "qwen/qwen3-embedding-8b", "input": "<text>", "dimensions": 2048}
```

- Measured 2026-08-18: works, dim 2048, **$0.0000006 per lesson** — the entire swarm-memory corpus costs well under a cent.
- `dimensions: 2048` is the Matryoshka trim; the Orakul/graphify bench measured 2048 as **lossless vs 4096** (identical hit@1/@5/MRR, half the index size). Keep 2048.
- Response shape: `{"data": [{"embedding": [...]}], "usage": {"cost": ...}}`.

## House wrapper

`helpers.embed_text(text, "openrouter:qwen/qwen3-embedding-8b@2048")` — the `openrouter:` prefix routes transport, `@2048` becomes `dimensions`. Scrub, breaker, fail-open, and cost metrics included. Swarm config key: `memory_embed_model`.

Live proof of value: a Russian paraphrase query with zero shared stems was found by the vector net when FTS returned nothing (`backend=fts+vec` in `.swarm/memory/queries.jsonl`).

## Chat models on OpenRouter

Available but NOT the default path — Ollama Cloud chat is subscription-covered and probe-validated for the fill-executor role. Use OpenRouter chat only when a needed model is absent on Ollama, and mind the $10 cap.
