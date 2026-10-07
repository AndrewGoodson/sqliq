# AI value, cost and company policy

SQLIQ uses Codex/Claude for orchestration, interpreting evidence, design tradeoffs,
performance recommendations and drafting compliance narratives. Deterministic code
handles rule inventory, validation, source integrity, signatures, replay protection,
DDL templates and report rendering. Models never decide whether an approval is valid.

Token policy for agent hosts:

- Route to one relevant skill; load referenced material only as needed.
- Use local code to filter and aggregate approved evidence before model review.
- Keep full control coverage in the register; send bounded relevant excerpts, IDs and
  evidence references to the model. Never hide missing evidence through summarization.
- Cache reviewed summaries by source/evidence hash, classification, target and scope;
  invalidate on changes. No cross-tenant cache or sensitive learning memory.
- Set an explicit input/output budget before each task. Use routine reasoning for
  simple explanation; escalate for ambiguous evidence or high-impact proposals.
- Measure actual host usage when available. Do not invent token counts or savings.
  These are host instructions; this repository does not enforce model token quotas.

Company policy governs AI use. Verify contracts, training/retention terms, residency,
classification and authorized providers before supplying context. Agent hosts can
transmit context; the deterministic CLI makes no LLM call. An AI prohibition needs an
approved exception, not a workaround. Synthetic offline evaluation is the first gate.

Generate the generic, public IT/board briefing without an AI service:

```sh
mkdir -p output/pdf
uv run azure-sql-agent board-report --output output/pdf/sqliq-it-board-briefing.pdf
```

The PDF includes Azure/customer/shared responsibility, inheritance evidence,
AI governance, token efficiency and adoption decisions. It is not a customer audit.
Use the separate compliance HTML report for supplied STIG findings. Have authorized
IT/compliance owners review applicability and evidence before distributing any
customer report. No report is sent automatically.

## Co-branded board reports

Every page includes the SQLIQ logo. Add the organization's own logo and display name:

```sh
uv run azure-sql-agent board-report \
  --company-logo /private/company-logo.png \
  --company-name "Example Financial Group" \
  --output /private/sqliq-board-briefing.pdf
```

Supply a local PNG or JPEG (maximum 2 MiB and 4 million pixels). Artwork keeps its
aspect ratio. Remote URLs, SVG and executable content are not supported. Logos are
embedded locally; no upload or AI call occurs. Keep company artwork and branded
reports outside the public checkout. Branding identifies the organization; it does
not change this generic briefing into a company assessment or certification.
