# Governed self-learning

LedgerGuard uses AEF Core's `InMemoryMemoryStore`, `RuleBasedConsolidator` and
`InMemoryKnowledgeStore` to group repeated outcome signals into learning candidates.
This is deterministic outcome consolidation, not model retraining or autonomous
self-modification. No provider, network, database reader or persistent memory runs.

```sh
uv run azure-sql-agent learn --outcomes config/learning-example.json
```

Inputs: up to 500 outcomes, each with a random run UUID, agent, enumerated control
and success/failure. No free-form prompts, SQL, schema, customer identifiers,
credentials or business data are accepted. Use fresh de-identified run UUIDs.
Different agents remain isolated. Repeated copies of the same run do not inflate
support. At least two distinct runs are required. Input provenance is operator
supplied, not independently attested; candidates are never proof of compliance.

Output: review-required candidates and evidence IDs. It cannot sign approvals,
change policy, update skills, edit the graph or execute a schema proposal. Human
maintainers review evidence, propose a separate source change, run regressions and
approve the release. Existing AEF safety holds remain intact.

The public website explains this workflow; its interactive graph is a static
illustration. It does not execute AEF, accept outcomes or connect to a database.
