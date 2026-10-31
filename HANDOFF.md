# Handoff: Airbus Track, Challenge 2

Autonomous SG 2026 (Acacia Hackathon Society x NUS Product Club). Full brief: `Autonomous SG 2026_Airbus Track.pdf`, page 2. Judging rubric: pages 7-8.

## The challenge in plain English

When something goes wrong at an airport (weather, runway closure, broken ground equipment), ATC, pilots and ground crew sort it out by phone and radio today. It's slow and error-prone.

We build three separate AI agents, one per group, that negotiate a fix for a real delayed flight within 3 turns:

| Agent | Looks after |
|---|---|
| ATC | Wake separation, taxiway blockage, runway throughput |
| Pilot | Fuel reserve, safety, turbulence, passenger connections |
| Ground | Gate use, ground-crew overtime, turnaround delay |

The output is an agreed fix: a revised arrival time, a gate assignment or a flight plan change.

## Hard requirements from the brief

- Three independent agents. One prompt playing three roles is explicitly marked as a fail.
- Agents exchange structured messages (JSON), not free chat.
- Safety rules are checked by plain code, not by the LLM. An agent must never be able to accept an unsafe option.
- A turn limit of 3, with convergence logic. No endless loops.
- A deterministic non-AI fallback if they don't agree.
- A traceable consensus log covering every message, every rule check (pass or fail, with the reason) and the final decision.

## How we're judged

| Criterion | Weight | What earns it |
|---|---|---|
| Technical soundness | 25% | Works end to end; LLM for reading and explaining, code for rules and scheduling; works on more than one scripted case |
| Problem realism | 20% | Named user, real workflow, clear "today" process |
| Honest evaluation | 20% | Tested on many cases, numbers reported, at least one failure shown and explained |
| Trust and traceability | 20% | Every decision traceable to a rule or data point; a human approves at the end |
| Impact | 10% | Before/after comparison against a baseline |
| Time discipline | 5% | Early submission; pitch of 3 minutes or less |

## Data

All files are in this folder.

| File | What it is |
|---|---|
| `flights.csv` | 5,819,079 US domestic flights from 2015, 31 columns, 592 MB. Too big for Excel, so use pandas. |
| `airports.csv` | 322 airports with coordinates |
| `airlines.csv` | 14 airline codes |

Source: https://www.kaggle.com/datasets/usdot/flight-delays. The Kaggle copy is byte-identical to the organisers' files.

**The brief's filter:** `WEATHER_DELAY > 15` or `LATE_AIRCRAFT_DELAY > 20` keeps 376,125 rows.

**Known data problems:**
- October uses 5-digit numbers for airports instead of codes like ATL. Skip October.
- Time columns contain `2400`. Replace it with `0000`.
- Delay-cause columns are blank on many rows. The filter above only keeps rows where they're filled.

**What the data gives the agents:**
- ATC: the real queue of other flights arriving at the same airport around the same time
- Ground: the same aircraft's next flight (by `TAIL_NUMBER`) and its turnaround slack
- Aircraft type: possible via the FAA registry, joined on tail number (https://www.faa.gov/licenses_certificates/aircraft_certification/aircraft_registry/releasable_aircraft_download/). Needed for wake separation.

**What the data doesn't have (we make it up and label it as made up):** fuel on board and reserve, gate list and occupancy, crew shift times, passenger connections, and airfield faults or runway closures.

### Worked example (real row)

DL 2213, MLB → ATL, 26 Jun 2015, tail N333NB. Scheduled to arrive 15:50, arrived 113 min late (58 min weather, 51 min late aircraft).
- 137 other flights were due into ATL between 14:50 and 16:50.
- The same aircraft's next flight was DL 994, ATL → TLH, at 19:59, which leaves about 2 h of turnaround slack after the late arrival.
- ATL handled 1,140 arrivals and 1,138 departures that day.

## Our angle

One of us served in RSAF 508 SQN (airfield lighting repair, arrester cable maintenance, runway works). That gives us:
- **Ground agent realism:** credible repair times and what a taxiway or runway closure means in practice. This maps to "ground equipment fails unexpectedly" and "taxiway blockage" in the brief.
- **Problem story for the pitch:** today, every change needs a long radio call and a read-back, it's hard for beginners, it's prone to error and it ties up ATC manpower. Structured messages between agents replace that.
- **Possible pitch hook:** the RSAF F-16 crash inside Tengah Air Base on 8 May 2024 (MINDEF: https://www.mindef.gov.sg/news-and-events/latest-releases/08may24_nr/). Stick to publicly reported facts.

Keep RSAF content at public level: no real callsigns, taxiway names or internal procedures. The demo uses a US commercial airport because that's what the data is.

## Status

Done:
- Brief and rubric reviewed
- Dataset downloaded and checked (row counts and filter above)

**Not decided yet:**
- Whether to add an airfield-closure "what if" scenario on top of the weather / late-aircraft rows. The dataset doesn't record closures, so this would be simulated on real traffic.
- Stack, UI and who owns what
- The named user for the pitch (e.g. airline ops duty manager, or ground ops controller)

Nothing has been built yet.

## Proposed build order

1. **Scenario builder (no AI).** Turn one filtered row into a JSON scenario: the flight, the arrival queue around it, the aircraft's next flight, plus labelled made-up values for fuel, gates and crew.
2. **Rules and message format (no AI).** Write each agent's hard limits as numbers, its soft goals, and the JSON schema for propose / accept / reject-with-reason / counter.
3. **Deterministic solver (no AI).** Picks the best option that passes every rule. It serves as the fallback and as the baseline for comparison.
4. **Three LLM agents** on top, each with its own prompt and context, and a hard limit of 3 turns. Every proposal goes through the rule checker.
5. **Batch evaluation** on about 100 or more filtered rows: share that agree within 3 turns, share that need the fallback, unsafe proposals blocked (target: none accepted), and delay compared with the baseline. Pick one failure to show.
6. **UI:** a timeline of the negotiation log plus an approve button for the human duty manager.
