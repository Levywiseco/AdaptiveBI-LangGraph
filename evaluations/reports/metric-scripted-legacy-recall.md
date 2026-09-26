# Metric planning evaluation: metric-sales-v1

- Mode: `scripted`; candidate recall: `legacy exact match`; today: 2026-09-25T10:00:00 (Asia/Shanghai)
- Passed: **38/43** (88%)
- Gold metric among candidates: 88% of 41 answerable cases
- Tokens: n/a; latency p50 11.0 ms, p95 12.3 ms

| Category | Passed |
| --- | --- |
| basic | 4/4 |
| dimension | 4/4 |
| filter | 8/8 |
| refusal | 2/2 |
| synonym | 4/9 |
| time_absolute | 6/6 |
| time_relative | 10/10 |

## Failures

| Case | Question | Outcome | Detail |
| --- | --- | --- | --- |
| syn-buyer-paraphrase | 上个月下单的客户有多少 | refusal | gold `buyer_count` not recalled; candidates none |
| syn-discount-paraphrase | 8月一共让利了多少钱 | refusal | gold `discount_total` not recalled; candidates none |
| syn-cancelled-paraphrase | 8月被取消的订单有几单 | refusal | gold `cancelled_orders` not recalled; candidates none |
| syn-units-paraphrase | 8月卖出去多少件 | refusal | gold `units_sold` not recalled; candidates none |
| syn-refund-paraphrase | 上个月退了多少钱 | refusal | gold `refund_amount` not recalled; candidates none |
