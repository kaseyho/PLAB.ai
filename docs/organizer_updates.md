# Organizer clarification: Challenge #2 — Air traffic decisions

Recorded on 31 October 2026 from the organizer update supplied by the project owner. This clarification supersedes earlier wording about missing data and “under three turns.” The judging weights remain unchanged.

## Authoritative changes

| Topic | Organizer clarification | Product requirement |
|---|---|---|
| Flight data | `flights.csv` added: **358,774 rows from 2015** with `WEATHER_DELAY > 15 OR LATE_AIRCRAFT_DELAY > 20` | Use a qualifying supplied row; preserve the actual file hash, row identity, original values and eligibility calculation |
| Turn definition | Each of the three agents sends **one structured message per turn** | Three independent role messages per completed turn; parallel calls are permitted |
| Termination | Consensus **within three turns**, otherwise deterministic fallback decides | At most three agent turns and nine agent generation requests; fallback adds no model turn or model call |
| Operational limits | DOT data contains no gates, runways, fuel or crew | Supply these from the new workbook; cited real figures are also accepted; label their origin and applicability |
| New workbook | `challenge2_operational_constraints.xlsx` | Covers per-airport gates, runways, movements per hour and turnaround, plus wake separation, fuel and crew rules |

The release size and workbook coverage above are organizer-reported. They are not new local inspection results or measured system performance.

## Actual repository status

The local `data/flights.csv` is still the earlier 592,406,591-byte, 5,819,079-row file, identified by SHA-256 `afa4e42188b113dd69427a3fbd00a4b6979c6235c866f963c2899d18a3403a83` in the previous inspection. Its 376,125 delay-eligible rows and 358,774 lookup-resolved eligible rows describe that older file. A matching count does not prove it is byte-identical to the new organizer release.

The workbook was not available in the repository or Downloads during this documentation update. Its sheets, cells, exact values, units and hash have not been inspected. The current service loads JSON scenarios; it has no workbook importer. Existing D01 values remain explicitly illustrative and are not attributed to the workbook.

The implemented protocol uses two turns: three offers followed by three votes, with at most six started model requests. This is a stricter execution profile within the clarified three-turn ceiling. It falls back early on disagreement or failure and does not exercise a third turn. Supporting an optional third turn requires coordinated changes to contracts, storage guards, coordinator, prompts, UI and tests; changing prose or a display label does not enable it.

## Turn accounting and human control

A completed agent turn has one validated message per role: ATC, Pilot and Ground. Log every started request, invalid/missing reply and early termination. A failed or partial turn never supplies implicit acceptance. Fixture invocations are reported separately from actual model calls. Existing `round` and `round_count` fields correspond to agent turns; they do not count individual messages, reconciliation, deterministic checks, fallback or human review.

Consensus requires three current explicit accepts of the same complete plan hash and versions, followed by passing deterministic checks. No consensus by the three-turn ceiling triggers deterministic selection over the checked full catalog. Invalid output, missing facts, exhausted time or the configured shorter profile can terminate earlier. If no plan can be validated, fallback returns a non-executable outcome with blockers.

The agents negotiate autonomously. A human then approves or rejects simulated application; fallback also requires review and explicit acknowledgement. Consensus never authorizes a resource write.

## Data and operational evidence requirements

1. Profile the newly supplied CSV separately and persist its hash and actual counts. Revalidate the strict predicate rather than trusting a filename or release description.
2. Re-extract scenario locators using date, airline, flight number and airports. An ordinal is meaningful only with its dataset hash: old D01 record 71 must not silently identify a different row in a filtered file.
3. The filtered disrupted-flight cohort is not a complete airport movement schedule or aircraft rotation. Do not derive total airport throughput, the full arrival queue or the next aircraft leg from it.
4. Inspect the workbook before mapping values. Preserve filename/hash, sheet, cell or table row, airport/aircraft/crew applicability, original value/unit and deterministic conversion. Synthetic workbook values remain synthetic.
5. A cited real replacement records publisher, document/URL, revision/date, section and applicability. Describe mixed sources field by field; a citation does not make the whole scenario operationally validated.
6. Separate airport totals and rules from the modeled resource subset. The one-runway/two-gate slice must fit documented totals, while fixed background movements and external load remain explicit. A movements/hour limit needs an explicit time-window convention and arrival/departure scope.
7. Wake spacing needs ordered categories, operation type and units; fuel rules need applicability, supplied fuel state and burn/uncertainty inputs; crew rules need duty/rest interpretation, availability and task duration. A rule alone does not supply a flight's fuel or a crew's current state.
8. Taxi routes, blockages, gate occupancy, equipment, permissions and connection groups remain scenario inputs unless a verified source actually supplies them. Missing critical data produces `INSUFFICIENT_DATA` or `UNKNOWN`, never an agent guess.
9. Source or policy changes create new scenario/ruleset versions and invalidate old reviews. Rerun hand-checked boundaries and B0/B1/system comparisons with identical versioned facts. Preserve pre-update results as historical development evidence.

## Three-developer update responsibilities

| Owner | Required follow-up |
|---|---|
| Developer 1 / A | Show actual turn limit/count, operational evidence and source labels; preserve reasoned approval, fallback acknowledgement and stale-plan guards |
| Developer 2 / B | Enforce one message per role per turn and truthful counters; retain the tested two-turn profile unless a fully tested three-turn profile is introduced; carry versioned evidence into prompts and exports |
| Developer 3 / C | Inspect and profile the new CSV/workbook, rebind source rows, map constraints and units, validate the resource subset, and rerun the frozen suite and both baselines |

This update changes documentation. It does not claim a new dataset import, workbook integration, third-turn implementation or completed evaluation report. See [the PRD](technical_prd.md), [work plan](developer_work_plan.md) and [backend handoff](backend_handoff.md).
