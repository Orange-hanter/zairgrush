---
name: ollama-cloud-api
description: Non-standard Ollama Cloud API contract for this project (subscription account) — measured facts, correct call shape, what exists and what does not. Read BEFORE any code that talks to ollama.com; do not assume OpenAI compatibility or local-ollama parity.
---

# Ollama Cloud API — measured contract (this account)

All facts below are **measured on the live account** (OLLAMA-1 experiment 2026-08-08, probes 2026-08-18), not taken from docs. Re-probe before relying on anything not listed here. Key: `OLLAMA_API_KEY` (in `~/.zshenv`). Subscription — calls are not metered per token for us.

## Call shape that works

`POST https://ollama.com/api/chat` (native API, NOT `/v1`):

```json
{"model": "<name>", "stream": false, "think": false,
 "options": {"num_predict": <max_out>, "temperature": 0.0, "top_k": 1,
             "repeat_penalty": 1.0, "seed": 17},
 "messages": [{"role": "user", "content": "..."}]}
```

- **`think: false` works ONLY on the native endpoint.** On `/v1` it is silently ignored and reasoning models burn the whole `num_predict` thinking, returning empty content (679 vs 15 tokens on the same task — ADR-004).
- `options` (num_predict/seed/temperature) exist only natively. `num_ctx`/`truncate`/`shift` are **ignored on cloud** — do not send them; context is always model-max.
- `format` (JSON schema) is **ignored** — response structure is your responsibility; validate mechanically.
- Errors can arrive as `{"error": ...}` in a HTTP-200 body — check before parsing.
- `done_reason == "length"` is the only honest truncation signal; a truncated answer is worse than none — discard it.
- Auth: `Authorization: Bearer $OLLAMA_API_KEY`. `x-request-id` response header is the only support handle.

## What EXISTS on this account

`GET /api/tags` lists ~19 **chat** models, including: `kimi-k2.7-code`, `kimi-k3`, `deepseek-v4-flash:0731`, `deepseek-v4-pro:*`, `glm-5.1`, `glm-5.2`, `gpt-oss:120b`, `gpt-oss:20b`, `gemma4:31b`, `qwen3.5:397b`, `minimax-m3`, `nemotron-3-*`, `mistral-large-3:675b`.

Measured fill-executor capability (probe 2026-08-18): **kimi-k2.7-code, deepseek-v4-flash:0731, glm-5.1, gpt-oss:120b, gemma4:31b all pass** a strict full-file completion contract — one fenced block, no extra prose, signatures preserved, contracts implemented, no elision at ~100 lines, feedback rounds converge. Fastest: kimi-k2.7-code (2–6 s), deepseek-v4-flash (2–5 s).

## What does NOT exist

- **No embeddings.** `/api/embed` → `{"error": "unauthorized"}` even with a valid key; `/api/embeddings` → path not found; zero embedding models in `/api/tags`. Use OpenRouter for embeddings (see the `openrouter-embeddings` skill).
- No agentic harness — these are bare chat completions. Anything needing tools/file-editing must be orchestrated externally (the swarm's fill-executor writes files itself after extracting the fence).

## House wrappers

Use `tools/swarm/swarm/helpers.py` (`ollama_chat`, `embed_text`) instead of raw urllib where possible: they carry the secret scrub (text goes to an external API), the circuit breaker, fail-open semantics, and metrics. `embed_text` routes `openrouter:`-prefixed models to OpenRouter automatically.

## Probe protocol (when touching anything new)

One measured call per question, mechanical checks (ast-parse / exact fields), findings recorded in `experiments/findings.jsonl` before building on the behavior. Probe scripts precedent: `~/.claude/jobs/*/tmp/probe_fill*.py`.
