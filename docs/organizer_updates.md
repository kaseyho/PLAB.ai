# Organizer clarification: Challenge #2 — Air traffic decisions

The organizer MVP is implemented: verified filtered data, workbook imports,
three turns/nine requests, deterministic fallback, B0/B1 comparisons and human
approval of runway/gate/crew/equipment bookings. See [the submission guide](submission.md)
for commands, measured results and simulation assumptions. The delivery priority
is the working demo; broader evaluation is optional.

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

The delivered CSV contains 358,774 eligible 2015 rows (38,616,129 bytes), hash
bfb7fc2112a1a98b748fa1c5bbba57f6f8f14acbd6419ea4cf84f4470ed5d79e.
The workbook hash is f889ae9e012c36cbbf95a5b0b70a0ae81c1fa9090f07be5f9d0db81520e78211.
Its airport_capacity, wake_separation, fuel_and_crew and rules_of_play sheets were
inspected. Normalized cases retain exact cells and natural flight identities.

All three turns are implemented. Previous two-turn smokes remain historical
evidence. File hashes are checked before new operational runs and approvals.

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
| Developer 2 / B | Enforce one message per role per turn and truthful counters; enforce fresh votes in the implemented third-turn protocol; carry versioned evidence into prompts and exports |
| Developer 3 / C | Inspect and profile the new CSV/workbook, rebind source rows, map constraints and units, validate the resource subset, and rerun the frozen suite and both baselines |

The runtime implements the clarified rules; see the submission guide for commands and scope limits.
