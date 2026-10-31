# PLAB.ai developer handoff

The current app integrates the supplied historical flight data and operational
workbook, three-turn coordination, deterministic fallback, the duty-desk UI and
atomic human approval. Start with [README](README.md) and [the demo guide](docs/submission.md).

The public app is now [Vercel](https://plab-ai-alpha.vercel.app/), using
Vercel Hobby and Neon Free. The browser holds a streamed execution request open
for each negotiation; keep the tab open until it finishes. Shared Postgres state
preserves runs and atomic review across function instances. Local demos retain
SQLite. See [deployment details](docs/deployment.md) before changing hosting.

Run `uv run python -m app.demo` with local credentials and an operator token.
Use --offline for fixture agents, or --offline --third-turn for a revised proposal
and fresh votes. Each launch creates an isolated local database.

The limit is three turns/nine requests, with zero fallback model calls and no
hidden retries. A human decides whether a validated recommendation is applied.
The transaction books runway, gate, crew and equipment. Stale, expired or rejected
plans cannot apply.

The CSV was verified as 358,774 eligible 2015 records. Workbook imports retain
source cells and hashes. Changed files block new runs and approvals. AA1057 moved
from legacy ordinal 71 to filtered ordinal 47.

The one-runway/two-gate subset fits the airport totals. Current traffic, fuel and
crew/resource state remain synthetic. Departure wake times are a declared arrival
proxy; distance separation and operational certification are outside this MVP.

| Developer | Owns |
|---|---|
| 1 / A | Dashboard, evidence, human review and presentation |
| 2 / B | Contracts, API/store, provider adapter, all three agents, coordinator and trace |
| 3 / C | Source import, rules/catalog/fallback and B0/B1 comparisons |

All three software agents belong to B; developers are not assigned one aviation
agent each. Integration work spans these boundaries to deliver the MVP.
The immediate priority is the working demo, not expanding the evaluation suite.

The first live evaluation (roles-v6) reached consensus in 1/6 nominal cases:
two timeouts, one information refusal and two protocol violations returned checked fallback plans.
Failures are retained. Subsequent prompt changes and focused smokes are reported
separately in the demo guide, not mixed into the original denominator.

For a product walkthrough, show a plan, a reasoned human decision, one failure
and the measured trade-offs. Keep source provenance and evaluation limitations
visible when explaining results.
