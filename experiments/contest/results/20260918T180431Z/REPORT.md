# Contest report

Run: 20260918T180431Z

| Task | Slot | Model | Passed | Failed | Err | Time s | Tokens in/out | Cost $ |
|---|---|---|---|---|---|---|---|---|
| a1-lru | kimi | moonshotai/kimi-k2.7-code | 16 | 1 | 0 | 211.2 | 685/11673 | 0.04544904 |
| a1-lru | sonnet | anthropic/claude-sonnet-5 | 17 | 0 | 0 | 31.3 | 841/3491 | 0.036592 |
| a2-cron | kimi | moonshotai/kimi-k2.7-code | 24 | 0 | 0 | 4.9 | 714/1220 | 0.0055583 |
| a2-cron | sonnet | anthropic/claude-sonnet-5 | 24 | 0 | 0 | 66.8 | 836/7548 | 0.077152 |
| a6-migrate | kimi | moonshotai/kimi-k2.7-code | — | — | contract: code block not found or not unique | 907.1 | 601/44703 | — |
| a6-migrate | sonnet | anthropic/claude-sonnet-5 | 12 | 0 | 0 | 26.1 | 701/2951 | 0.030912 |

## Итог по слотам

- **kimi**: задач решено 1/3, тестов 40 passed / 41 total
- **sonnet**: задач решено 3/3, тестов 53 passed / 53 total
