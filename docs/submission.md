# Hackathon MVP: launch and demonstrate

The app is runnable now. Use the commands in [README](../README.md); provider keys
and the operator token stay in local `.env`.

## Presentation route, under three minutes

1. Start `uv run python -m app.demo --offline --third-turn` for a reliable,
   explicitly labelled fixture-agent demonstration with the real rules and review gate.
   Start without those flags to use the configured live provider.
2. Open `http://127.0.0.1:8000`, enter the operator token and choose D01.
   Show the real historical AA1057 trigger, filtered record 47, and workbook sources.
3. Start the run. Show each role's offer, the injected Turn 2 dissent in offline
   mode, and three fresh Turn 3 accepts of the revised plan hash.
4. Compare the plan with B0/B1 and inspect the 15 checks. In the third-turn fixture,
   `L1320-G1-long` adds two minutes of modeled holding and removes 35 minutes of
   ground-crew overtime relative to B0. These are simulated trade-offs.
5. Enter a review reason and approve. The UI shows `APPLIED_SIMULATION` and resource
   revision 2. The transaction reserves runway, gate, crew and equipment together.
6. Select D06 or the missing-data sample to show a non-executable outcome. Mention
   the live failures below and finish with the narrow scope and next validation step.

## Evidence retained

| Run | Observed result | Interpretation |
|---|---|---|
| Offline frozen holdout | 12/12 contract cases passed; zero model calls | Deterministic integration/fault coverage, not LLM accuracy |
| Initial live holdout, roles-v6 | 1/6 nominal cases reached consensus; 24 model calls | Two timeouts, one information refusal, one unknown evidence reference and one offer-policy violation used checked fallback |
| Development debug iteration, roles-v7 | 0/4 nominal consensus; 12 calls | Three prompt-adapter errors and one concession-policy violation; retained, not hidden. The adapter field omission was then fixed |
| Focused live UI smoke, roles-v7 | Three calls, timeout, checked fallback in 12,301 ms | Live provider latency remains a demo risk |
| Focused offline UI flow, roles-v7 | Three turns, nine fixture invocations, unanimous revised-plan votes, human approval applied | Working MVP and atomic application verified in the browser; no live-agent claim |

Machine-readable summaries are in [evals/results](../evals/results/). Full traces
remain under local `artifacts/evals/` and the isolated demo databases. Earlier
failures remain in their original directories. Prompt changes after the initial
holdout mean its model rate is historical evidence, not a new benchmark for the
final prompt. No new broad evaluation was added after the decision to prioritize delivery.

## Exact source and rule mapping

- `flights.csv`: 358,774 rows, all 2015 and all satisfying weather delay >15 or
  late-aircraft delay >20; 38,616,129 bytes. SHA-256:
  `bfb7fc2112a1a98b748fa1c5bbba57f6f8f14acbd6419ea4cf84f4470ed5d79e`.
- Workbook SHA-256:
  `f889ae9e012c36cbbf95a5b0b70a0ae81c1fa9090f07be5f9d0db81520e78211`.
- D01 MIA limits: `airport_capacity!A22:K22`; 3 active runways, 70 gates,
  60 aggregate movements/hour, 35-minute turnaround, 3 spare gates.
- Wake categories/times/distances: `wake_separation!A3:D11`.
- Fuel and crew rules: `fuel_and_crew!A1:C9`; 30 holding minutes reserve,
  5% trip contingency, 2400/6500 kg/hour holding burn, 45 holding minutes spare fuel,
  13-hour flight duty, eight-hour ground shift, 1.5× overtime, 20-minute gate notice.

The simulator models one runway and two candidate spare gates. Traffic, fuel
state, trip fuel, aircraft kind, report times, routes, equipment, occupancy and
connections are declared synthetic facts. The workbook's departure-time wake
rule is an explicitly declared arrival-simulation proxy; distance separation is
not modeled. Flight duty is checked through gate arrival and gate-change notice
through landing. This is a simulation, not real-world safety certification.

## Submission boundary

Code, workbook, normalized scenarios, quick-start instructions and retained result
summaries are ready to package. The flight CSV is supplied separately or included
in the local submission archive. Credentials, databases and caches are excluded.
The team still needs to upload the deliverable and any required video to its
hackathon portal; this repository does not establish a submission timestamp.
