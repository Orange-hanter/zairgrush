# Contest report

Run: 20260918T175345Z

| Task | Slot | Model | Passed | Failed | Err | Time s | Tokens in/out | Cost $ |
|---|---|---|---|---|---|---|---|---|
| a1-lru | kimi | moonshotai/kimi-k2.7-code | 16 | 1 | 0 | 173.2 | 625/14005 | 0.0424603125 |
| a1-lru | sonnet | anthropic/claude-sonnet-5 | 16 | 1 | 0 | 30.1 | 762/2874 | 0.030264 |
| a2-cron | kimi | moonshotai/kimi-k2.7-code | — | — | contract: code block not found or not unique | 168.3 | 712/32000 | 0.1206052 |
| a2-cron | sonnet | anthropic/claude-sonnet-5 | 24 | 0 | 0 | 120.6 | 836/13266 | 0.134332 |
| a6-migrate | kimi | moonshotai/kimi-k2.7-code | — | — | contract: code block not found or not unique | 73.1 | 557/32000 | 0.11239547 |
| a6-migrate | sonnet | anthropic/claude-sonnet-5 | 12 | 0 | 0 | 32.5 | 701/3396 | 0.035362 |

## Итог по слотам

- **sonnet**: задач решено 2/3, тестов 52 passed / 53 total
- **kimi**: задач решено 0/3, тестов 16 passed / 17 total
