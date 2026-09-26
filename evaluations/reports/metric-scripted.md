# Metric planning evaluation: metric-sales-v1

- Mode: `scripted`; candidate recall: `hybrid`; today: 2026-09-25T10:00:00 (Asia/Shanghai)
- Passed: **44/47** (94%)
- Gold metric among candidates: 93% of 44 answerable cases
- Model calls: 41 (repairs 0, passed after repair 0); clarifications 0
- Tokens: n/a; latency p50 18.1 ms, p95 21.7 ms

| Category | Passed |
| --- | --- |
| basic | 4/4 |
| dimension | 4/4 |
| filter | 8/8 |
| follow_up | 3/3 |
| refusal | 3/3 |
| synonym | 6/9 |
| time_absolute | 6/6 |
| time_relative | 10/10 |

## Failures

| Case | Question | Outcome | Detail |
| --- | --- | --- | --- |
| syn-discount-paraphrase | 8月一共让利了多少钱 | refusal | gold `discount_total` not recalled; candidates none |
| syn-units-paraphrase | 8月卖出去多少件 | refusal | gold `units_sold` not recalled; candidates none |
| syn-refund-paraphrase | 上个月退了多少钱 | refusal | gold `refund_amount` not recalled; candidates none |
