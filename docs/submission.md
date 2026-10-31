# Hackathon MVP: launch and demonstrate

The app is runnable now at [the public demo](https://plab-ai.onrender.com/), with
team-provided model access and no visitor API key. The
[judge guide](https://plab-ai.onrender.com/static/judges.html) maps the problem
statement and all six rubric criteria to working features and evidence.
For local use, follow [README](../README.md); credentials stay in `.env`.

## How the submission answers the rubric

The app now links the three highlighted criteria directly to observable evidence:
**Problem realism → the duty-manager workflow**, **Technical soundness → rules,
agents and human authority**, and **Honest evaluation → the measured report**.
The workflow includes actual request counts and an immediately visible B0/B1
comparison of holding, fuel and overtime for the selected plan.

For an unscripted input check, open **Change simulated fuel** in Step 1. Enter a
different nonnegative quantity, or mark it unknown, and start a run. The browser
submits a new scenario revision with explicit synthetic provenance through the
existing API. Unknown fuel produces a non-executable missing-data result; fuel
below the required reserve can produce no feasible option. Both paths execute the
real deterministic planner and can stop before any model call. Pending plans are
invalidated by the new revision, and committed reservations remain present.
Changing fuel never changes the reserve, contingency or workbook policy.

The evaluation panel reads `/api/evaluations/latest`; it does not invent or launch
an evaluation. It shows the report's model/prompt/date, separate live and fixture
denominators, measured latency, every case and its B0 deltas, a linked recorded
failure, and a proposed next validation step. Unavailable comparisons stay
unavailable. The recorded results do not evaluate browser fuel edits or establish
performance for a changed prompt. No additional broad evaluation was run for
these UI improvements.

| Criterion | Weight | Concrete answer and evidence |
|---|---:|---|
| Problem realism | 20% | A duty manager coordinates one disrupted arrival across ATC, Pilot and Ground. D01 is the qualifying historical AA1057 flight; workbook limits and additional simulation inputs are explicitly distinguished. |
| Technical soundness | 25% | Separate structured role invocations, at most three turns/nine requests, deterministic candidate checks and selection, zero-call fallback, persisted API results and atomic simulator application. |
| Honest evaluation | 20% | Frozen inputs and B0/B1 comparisons; 1/6 initial live nominal consensus versus 12/12 contract cases, including fixture faults. Later development failures remain visible. These are different measures. |
| Trust and traceability | 20% | Source hashes/cells, check margins, same-plan votes, human reason and fallback acknowledgement. Approval rechecks freshness and resources; missing data and stale plans block application. |
| Impact | 10% | Paired holding, fuel and overtime comparisons. All six initial nominal live cases matched B0's modeled impacts; no live improvement was demonstrated. The scripted third-turn example illustrates a 2-minute holding / 35-minute overtime trade-off, not real-world savings. |
| Time discipline | 5% | A timed three-minute walkthrough is published. A valid submission timestamp must be established by the hackathon portal. |

The guide links directly to the agent histories, checks, source facts, approval
trace and refusal examples. Saved examples are labelled scripted and read-only;
they do not stand in for live-agent performance. No judging score is self-awarded.

## Presentation route, under three minutes

1. Start `uv run python -m app.demo --offline --third-turn` for a reliable,
   explicitly labelled fixture-agent demonstration with the real rules and review gate.
   Start without those flags to use the configured live provider.
2. Open the public demo, or `http://127.0.0.1:8000` for the local presentation.
   Only the local version requires the operator token. Choose D01.
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
