# Repository working instructions

- This is the independent LangGraph migration workspace copied from our own Adaptive project at commit 55df5be1fb8bef1b2d912e545fecf4f484565d38.
- Use the code in this repository, the user's requirements, and official documentation of the selected technologies as design inputs.
- Do not fetch, browse, sync, or use the original dataease/SQLBot repository or its product documentation as reference material. Do not configure an upstream remote pointing there.
- Preserve existing license and copyright notices in copied code.
- Follow docs/langgraph/MIGRATION_PLAN.md. Distinguish planned capabilities from implemented and tested capabilities.
- Do not copy local secrets, customer data, .env files, or runtime logs into Git.
- Use separate test data and environments. Do not apply migrations to the running original project's database as part of experiments.
