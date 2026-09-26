# SQLBot Adaptive evaluation harness

This directory keeps the question set separate from generated results. A run never treats SQL text equality as the main correctness signal: it compares outcome, metric/version provenance, columns and result rows with configurable ordering and numeric tolerance.

`cases.example.yaml` is a synthetic schema example. Copy it to `cases.local.yaml` for real, desensitized business cases. Keep the holdout split away from prompts, terminology, SQL samples, memories and tuning decisions.

```powershell
Set-Location D:\python\SQLBot-adaptive\backend
uv run --no-sync python ..\evaluations\run.py `
  --cases ..\evaluations\cases.example.yaml `
  --actual ..\evaluations\results.example.json `
  --report ..\evaluations\reports\example.md
```

The first real baseline still needs a read-only business datasource, a frozen data snapshot and questions reviewed by the metric owner. Store credentials outside this directory.

## Case contract

- `split`: `dev` may guide implementation; `holdout` is only for acceptance.
- `category`: use stable failure categories such as `metric`, `table_selection`, `sql`, `permission`, `context`, `ambiguity` and `refusal`.
- `expected.outcome`: `success`, `refusal` or `error`.
- `expected.metric`: optional required metric code and version.
- `expected.rows`: JSON-like records or arrays. Set `ordered: true` only when order is part of the answer.
- `float_tolerance`: absolute numeric tolerance for this case.

The adapter that calls a configured SQLBot instance will be added when a test model and datasource are available. Until then, captured results can be compared deterministically with this harness.

## Governed metric suite

`metric_suite/` is a fictional sales catalog for the governed metric path:

- 937 orders in `sales_orders.csv` and 9 published metrics
- 43 questions in `cases.yaml`, covering plain metrics, absolute and relative periods, dimensions, dimension-value filters (channel codes with Chinese labels), synonyms or paraphrases, and refusals

Edit `metric_suite/catalog.py` and run `python metric_suite/build.py` to regenerate. Each case's expected rows come from a plain-Python oracle applied to its gold plan, independently of the SQL compiler.

`metric_eval.py` runs the production path on in-memory databases:

1. candidate recall
2. known dimension values
3. planning prompt
4. model
5. strict plan parsing from graph-service
6. plan re-validation and the metric compiler
7. SQL on SQLite, compared by rows

```bash
cd backend
uv run --no-sync python ../evaluations/metric_eval.py --mode scripted --report ../evaluations/reports/metric-scripted.md
uv run --no-sync python ../evaluations/metric_eval.py --mode scripted --legacy-recall
EVAL_MODEL_BASE_URL=... EVAL_MODEL_API_KEY=... EVAL_MODEL_NAME=... \
  uv run --no-sync python ../evaluations/metric_eval.py --mode live --output ../evaluations/reports/metric-live.json
```

In `scripted` mode, the model returns each gold plan. That measures candidate recall and checks the compiler against the answer key; it says nothing about model quality. `live` mode calls an OpenAI-compatible model through the gateway's provider policy and reports accuracy, tokens and latency. `tests/test_metric_eval_suite.py` keeps the generated files in sync and fails if a scripted miss is anything other than a recall miss that needs embeddings.
