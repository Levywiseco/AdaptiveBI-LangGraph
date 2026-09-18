# Kimi live synthetic validation — 2026-09-18

## Scope

The user authorized reuse of the existing local Kimi configuration. Read only the configured model and its legacy encryption material from the local development metadata database. Decryption and temporary test credentials stayed in process memory. No original backend startup, migrations, customer datasource queries, or credential files were copied into this repository.

The isolated harness reused the test metadata SQLite and real HTTP backend/graph stack. The actual LLMFactory and model-config loader ran against the isolated metadata database (temporary model ID 10). The upstream provider was real, not a stub: api.moonshot.cn, configured model `kimi-k3`. This verifies that configured API identifier was accepted; it does not establish a model quality ranking or provider implementation details.

## Results

| Attempt | Run ID | Result | Input tokens | Output tokens | Total | End-to-end ms |
| --- | --- | --- | --- | --- | --- | --- |
| Before value descriptions | bcabc196-da3b-4b50-ac18-e012fd56d848 | Query completed, rows [[null]], expected-value check failed | 156 | 201 | 357 | 12642.91 |
| After value descriptions | 71f50297-0f36-415c-bac6-0b5b086da63f | Query completed, rows [[770]], expected-value check passed | 203 | 99 | 302 | 6468.72 |

Each attempt reported one model call. Total reported tokens: 659. No automatic retries were enabled. Timing represents these two samples only, not a benchmark. A successful single query does not establish general accuracy.

The original schema prompt omitted month representation and region codes. Added `YYYY-MM` and the east/东部, west/西部 mappings consistently to the backend gateway prompt and synthetic graph schema. The first SQL was not retained, so its precise predicate error is not established; the second request passed after this metadata improvement.

## Environment limits

The enclosing PowerShell process reported an out-of-memory error during parallel verification, although the second live-smoke JSON reported passed=true with the expected rows and actual usage. The shell exit was abnormal and is not counted as a clean script exit. No test Python services remained after termination. The affected offline gateway regression was rerun separately; no additional live request was made for that rerun.

This is an isolated gateway/graph integration result. It does not validate full legacy application startup, real customer data, UI integration, production deployment, or persistent model configuration in the new application. The reused provider key remains in the old local configuration; this test did not create a new on-disk credential configuration.
