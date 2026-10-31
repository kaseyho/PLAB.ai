# Airbus Challenge #2: Air traffic decisions

The organizer MVP is implemented: verified filtered data, workbook imports,
three turns/nine requests, deterministic fallback, B0/B1 comparisons and human
approval of runway/gate/crew/equipment bookings. See [the submission guide](submission.md)
for commands, measured results and simulation assumptions. The delivery priority
is the working demo; broader evaluation is optional.

“How might AI help air traffic controllers, pilots and ground operations staff make faster and better decisions?”

This consolidated brief incorporates the organizer clarification supplied on 31 October 2026. See [the update record](organizer_updates.md) for provenance, implementation status and data migration requirements.

## Problem

When a runway closes, weather delays occur or ground equipment fails unexpectedly, coordination relies on human phone calls and manual software updates. Disruptions can cause cascading delays, excessive holding and fuel consumption. The challenge asks for AI assistants that communicate and negotiate trade-offs across organizational boundaries.

## Required system

Build a multi-agent communication framework with three independently invoked agents:

| Agent | Responsibilities |
|---|---|
| ATC | Runway wake separation, taxiway blockage and sector throughput |
| Pilot | Fuel reserves, flight safety, turbulence and passenger connections |
| Ground | Gate utilization, ground-crew overtime and turnaround delays |

Agents exchange structured data, evaluate cross-domain constraints and negotiate autonomously until a terminal recommendation. Demonstrate revised arrival time, gate assignment or flight-plan adjustment for a qualifying real historical flight. Include explicit constraint checks, a traceable consensus log and a deterministic non-AI fallback.

## Clarified turn rule

**One turn means each of ATC, Pilot and Ground sends one structured message. Consensus must be reached within three turns; otherwise deterministic fallback decides.** The ceiling permits nine agent messages/generation requests, with no hidden repair calls or automatic model retries. Fallback is deterministic and is not a fourth agent turn. Earlier termination on invalid data, model failure or a declared shorter execution profile remains visible.

The implementation supports all three turns. A revised third-turn plan requires three fresh accepts of its exact hash. Human review occurs after negotiation and before simulated application, including fallback.

## Supplied historical data

The organizers report a new `flights.csv` containing **358,774 records from 2015** satisfying:

```text
WEATHER_DELAY > 15 OR LATE_AIRCRAFT_DELAY > 20
```

Choose a qualifying row and pass the allowed historical parameters to all three agents. Preserve missing values, strict threshold behavior, source identity and file hash. Historical outcome fields are reference evidence, not live operational state or a counterfactual benefit baseline.

The filtered cohort does not provide complete airport traffic or full aircraft rotations. The older full CSV still present locally has a different profile; revalidate source locators before adopting the new release.

## Operational data and constraints

The DOT flight data has **no gates, runways, fuel or crew**. Use **`challenge2_operational_constraints.xlsx`**, whose values are synthetic; cited real figures are also accepted. The workbook provides:

- Per-airport gates and runways.
- Movements per hour and turnaround.
- Wake separation.
- Fuel and crew rules.

Record exact workbook sheet/cell references, units, applicability and file version/hash after inspection. Cited real substitutions need document/URL, revision and relevant section. Label workbook values as synthetic. Additional operational state absent from the source must be explicitly simulated or left unknown.

The workbook is inspected and imported. Additional synthetic state remains separately labelled.

## What to avoid

- Combining all three roles into one prompt call.
- Endless agent conversations, hidden extra turns or uncounted retries.
- Allowing agreement to override failed or unknown hard checks.
- Inventing operational facts from historical delay values.
- Treating agent consensus or deterministic fallback as authorization to apply a plan.
- Reporting workbook-derived results or evaluation improvements before producing the corresponding evidence.
