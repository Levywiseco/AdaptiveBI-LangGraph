# Metric planning evaluation: metric-sales-v1

- Mode: `scripted`; candidate recall: `hybrid`; today: 2026-09-25T10:00:00 (Asia/Shanghai)
- Passed: **40/43** (93%)
- Gold metric among candidates: 93% of 41 answerable cases
- Tokens: n/a; latency p50 10.3 ms, p95 11.9 ms

| Category | Passed |
| --- | --- |
| basic | 4/4 |
| dimension | 4/4 |
| filter | 8/8 |
| refusal | 2/2 |
| synonym | 6/9 |
| time_absolute | 6/6 |
| time_relative | 10/10 |

## Failures

| Case | Question | Outcome | Detail |
| --- | --- | --- | --- |
| syn-discount-paraphrase | 8月一共让利了多少钱 | refusal | gold `discount_total` not recalled; candidates none |
| syn-units-paraphrase | 8月卖出去多少件 | refusal | gold `units_sold` not recalled; candidates none |
| syn-refund-paraphrase | 上个月退了多少钱 | refusal | gold `refund_amount` not recalled; candidates none |
