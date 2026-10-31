# The Most Practical Method

Strip away everything else and the whole thing reduces to this:

> **Build the negotiation loop in one file, with zero AI, on a hardcoded scenario. If three plain Python functions can reach consensus in 2 turns and log it, you understand the system. Everything after that is upgrading one piece at a time.**

If you cannot do that, LLMs will not save you — they will just make the failure harder to see.

---

## 1. The principle behind the method

Most teams do this and it fails:

```
design architecture → build all layers → try to run → nothing works → debug everywhere
```

The practical method is the reverse:

```
build the smallest thing that runs → see it work → grow one piece at a time
```

Every step below leaves you with **a working thing you can show someone**. Not a plan. Not a diagram. Something that runs and produces a decision.

---

## 2. Step 1 — Play it by hand (30 minutes, no code)

Before touching a keyboard, three people sit at a table. One plays ATC, one plays Pilot, one plays Ground. Put a piece of paper in the middle.

- **ATC** says out loud: "I propose the plane holds 6 minutes and lands on 27R."
- **Pilot** looks at a number written on a card (fuel reserve = 38, floor = 30, so after 6 minutes it's 32) and says "Accept."
- **Ground** looks at a number on a card (gate free at 19:05, landing now 19:12) and says "Accept."
- **Someone** writes it on the paper: `turn 1 — hold 6 — pilot fuel 32 pass — ground gate pass — ACCEPT`.

Then do a hard one:

- **ATC**: "hold 10 minutes."
- **Pilot**: "Fuel after = 28, floor is 30. **Reject.** I counter with hold 6."
- **ATC**: "I propose hold 6."
- **Ground**: "Gate K12 occupied until 19:05, landing 19:12, fine. Accept."
- **Pilot**: "Accept."
- **Someone writes it down.**

Now do the impossible one:

- **ATC**: "hold 12."
- **Pilot**: "Fuel 26. Reject."
- **ATC**: "hold 6, switch to 27R."
- **Ground**: "Gate taken. Reject."
- **Someone reads the fallback card**: "Extend hold? Fuel says no. Change runway? Gate says no. Ground hold + K15? Gate free, crew B available. **Do that.**"
- **Someone writes that down.**

**You have just implemented the entire system by hand.** If three people can do this at a table in 20 minutes, three functions can do it in code. If they *cannot* — if the rules are vague, if nobody agrees on who speaks when, if the fallback order is unclear — that is the bug you fix first, on paper, before any code.

**What this step gives you:**

- Proof the flow is followable
- A concrete list of the numbers and rules you actually need
- A shared mental model across the team (this matters more than it sounds)
- The exact log format, hand-written, that the code will produce

Most hackathon teams skip this. It is the cheapest 30 minutes in the project.

---

## 3. Step 2 — One Python file, no AI, hardcoded scenario (~half a day)

Write a single file, maybe 150 lines. No CSV, no YAML, no imports beyond standard library. Just a dict with numbers and three functions.

Here is the actual shape. Read it — it *is* the system.

```python
# rdcp_one_file.py

# ---- the scenario: hardcoded, no data loading ----
SITUATION = {
    "flight": "AA1847",
    "runway_closed": "27L",
    "closure_min": 40,
    "fuel_reserve_min": 38,      # fuel if landing right now
    "fuel_floor": 30,
    "gate_occupied_until": 19*60 + 5,   # 19:05 in minutes
    "crew_shift_end": 19*60 + 30,
    "turnaround_min": 55,
    "expected_arrival": 19*60 + 10,     # 19:10
}

# ---- the three rulebooks: pure functions, no AI ----
def atc_check(proposal, s):
    if proposal["hold_min"] > 12:
        return False, "hold exceeds sector capacity", None
    return True, "ok", {"wake_sep": True, "sector_load": 0.89}

def pilot_check(proposal, s):
    fuel_after = s["fuel_reserve_min"] - proposal["hold_min"]
    passed = fuel_after >= s["fuel_floor"]
    return passed, f"fuel_after={fuel_after} floor={s['fuel_floor']}", {"fuel_after": fuel_after}

def ground_check(proposal, s):
    arrival = s["expected_arrival"] + proposal["hold_min"]
    gate_free = arrival >= s["gate_occupied_until"]
    return gate_free, f"arrival={arrival} gate_free={s['gate_occupied_until']}", {"arrival": arrival}

CHECKS = {"ATC": atc_check, "PILOT": pilot_check, "GROUND": ground_check}

# ---- the preparation desk: find legal plans ----
def legal_plans(s):
    plans = []
    for hold in [4, 6, 8, 10, 12]:
        p = {"hold_min": hold, "runway": "27R"}
        results = {role: fn(p, s) for role, fn in CHECKS.items()}
        if all(r[0] for r in results.values()):
            plans.append({"plan": p, "checks": results})
    return plans

# ---- the meeting ----
def run_meeting(s):
    log = []
    plans = legal_plans(s)

    if not plans:
        return emergency_card(s, log, reason="no legal plans")

    proposal = plans[0]["plan"]          # ATC picks the first legal one
    log.append({"turn": 1, "event": "PROPOSE", "proposal": proposal})

    # all three check independently
    failures = []
    for role, fn in CHECKS.items():
        ok, reason, values = fn(proposal, s)
        log.append({"turn": 1, "event": f"CHECK_{role}", "ok": ok, "reason": reason})
        if not ok:
            failures.append((role, reason))

    if failures:
        log.append({"turn": 1, "event": "HARD_REJECT", "by": failures})
        return emergency_card(s, log, reason=f"hard reject by {failures[0][0]}")

    # all accept (turn 1) — done
    log.append({"turn": 1, "event": "CONSENSUS", "proposal": proposal})
    return proposal, log

# ---- the emergency card: fixed order, no AI ----
def emergency_card(s, log, reason):
    # F1: extend hold to 15
    p = {"hold_min": 15, "runway": "27R"}
    if all(fn(p, s)[0] for fn in CHECKS.values()):
        log.append({"turn": 3, "event": "FALLBACK", "path": "F1", "reason": reason})
        return p, log

    # F2: change runway (in this toy, 27R isn't available — simulate)
    log.append({"turn": 3, "event": "FALLBACK", "path": "F2", "reason": "27R unavailable"})

    # F3: ground hold + gate reassign (always allowed in this toy)
    p = {"hold_min": 0, "runway": "27R", "ground_hold": 15, "gate": "K15"}
    log.append({"turn": 3, "event": "FALLBACK", "path": "F3", "reason": reason})
    return p, log

# ---- run it ----
if __name__ == "__main__":
    decision, log = run_meeting(SITUATION)
    print("DECISION:", decision)
    for entry in log:
        print(entry)
```

Run it. You get:

```
DECISION: {'hold_min': 6, 'runway': '27R'}
{'turn': 1, 'event': 'PROPOSE', 'proposal': {'hold_min': 6, 'runway': '27R'}}
{'turn': 1, 'event': 'CHECK_ATC', 'ok': True, 'reason': 'ok'}
{'turn': 1, 'event': 'CHECK_PILOT', 'ok': True, 'reason': 'fuel_after=32 floor=30'}
{'turn': 1, 'event': 'CHECK_GROUND', 'ok': True, 'reason': 'arrival=1156 gate_free=1145'}
{'turn': 1, 'event': 'CONSENSUS', 'proposal': {'hold_min': 6, 'runway': '27R'}}
```

Now change `fuel_reserve_min` from 38 to 32 and run it again. The Pilot check fails. The fallback fires. You have just built and tested the two most important paths of the entire system in one file.

**What this step gives you:**

- A running system
- A log format you now know is workable
- Proof the flow is real
- Confidence that the concept works before you invest in infrastructure

This is the step most teams skip, and it is the one that teaches you the most.

---

## 4. Step 3 — Split into files (one day)

Now, and only now, take the one file and split it:

```
core/constraints.py    ← the three check functions
core/fallback.py       ← the emergency card
core/logger.py         ← writing to the log
protocol/engine.py     ← run_meeting()
data/scenario.py       ← SITUATION, loaded from a real row
```

**Rule: after the split, the system must behave identically.** Same input, same output, same log. If it does not, you broke something in the split, and you catch it because you have the working one-file version to compare against.

This is why you build the one file first. It is your regression test.

**What this step gives you:** the layered structure, without any of the risk.

---

## 5. Step 4 — Real data (half a day)

Replace the hardcoded `SITUATION` with:

- One real row from the DOT 2015 CSV (filter: `WEATHER_DELAY > 15` or `LATE_AIRCRAFT_DELAY > 20`)
- One YAML file with the operational numbers (runway, closure, fuel, gate, crew) that the DOT data does not contain

**Rule: nothing else changes.** The engine, checkers, and fallback take the same dict shape. If they do not, your dict shape was wrong — fix it here, not in ten other places.

**What this step gives you:** the same working system, now fed by real data. Every number in the log points to either a CSV row or a YAML line.

---

## 6. Step 5 — Two-turn negotiation (a few hours)

So far turn 1 always produces consensus or fallback. There is no turn 2. That is fine — turn 2 only exists when the first proposal needs a counter.

Add turn 2 to `run_meeting`:

- If turn 1 got an Accept from everyone → done (as now)
- If turn 1 got a Counter → ATC picks a *different* legal plan that addresses the counter → repeat checks → done
- If turn 2 also fails → fallback

**Rule: after this step, the meeting ends in 1, 2, or 3 turns. No other option.** Write a test that runs 100 random scenarios and asserts every one ends with turn ≤ 3 and a decision. If any run fails that, you have a bug.

**What this step gives you:** the real meeting behavior, and the guarantee the rubric asks for.

---

## 7. Step 6 — The AI, last (half a day)

Replace exactly one thing: **the line `proposal = plans[0]["plan"]`** (ATC picks the first legal plan) with a call to the LLM.

The AI receives the **legal plans list only**. It returns:

```json
{"chosen_plan_index": 1, "justification": "Hold-6 preserves pilot fuel at 32 min..."}
```

Parse it. If it names an index that is not in the list, or the JSON is invalid, **use `plans[0]` and move on**. Log which path was taken.

That is the entire AI integration. One function, one call, one fallback on failure.

**Rule: after this step, run every test from Steps 2–5 again.** If any fails, the AI integration is wrong — not the system. Roll back the AI, fix, try again.

**What this step gives you:** the LLM proposer, without touching the safety layer.

---

## 8. Step 7 — Show the log (half a day)

Take the log entries the system already produces and render them on a page:

- A list, one row per event, in order
- Click a number → see where it came from (`assumptions.yaml:17` or `DOT row`)
- Failed checks shown in red with the exact numbers
- Fallback shown with its reason
- The whole log hash-chained (each row carries a stamp that depends on the row above)

That is the "traceable log" and the "audit viewer." It is not a separate system. It is a rendering of what the code already produces.

---

## 9. What to skip

| Skip this | Why |
|---|---|
| Microservices, message brokers, containers | The demo runs in one Python process. Splitting now is pure cost |
| A real message bus | An in-memory list of dicts is enough. You can swap the transport later |
| A database | The log is a JSON file. Read it with `json.load` |
| Nice-to-have UI features (charts, filters, dashboards) | One page, one list, clickable sources |
| More than 3 agents | Three is the challenge. Four is not more impressive, it is more to explain |
| Real-time data feeds | The row is frozen. The demo is a replay |
| Multi-scenario negotiation | One flight per meeting for the demo. Batches are a slide in "future work" |
| An elaborate LLM prompt framework | One prompt per role, in a text file, is enough |

Every one of these is something a team might build because it *feels* like real engineering. None of them earns rubric points, and every one of them costs a day you cannot spare.

---

## 10. How to divide the work

The whole system after Step 3 splits cleanly into four independent tracks. Each can be built and tested in parallel.

| Track | Owner | Builds | Test it passes |
|---|---|---|---|
| **Safety** | Person A | `core/constraints.py`, `core/fallback.py` | Feed each checker good and bad numbers; assert the right answer; assert the fallback walks F1→F4 |
| **Coordination** | Person B | `protocol/engine.py`, `core/logger.py` | Run meeting with stub proposers; assert it ends in ≤3 turns with a decision; assert every event is logged |
| **Data** | Person C | `data/scenario.py`, `assumptions.yaml` | Load the DOT row; assert every field in the scenario dict has a `source` field |
| **Presentation** | Person D | `ui/viewer.py`, the pitch deck | Render the log from a saved run; assert every number is clickable |

Each track has a **runnable test** it can show the others. They meet once a day, show their test, agree the interfaces have not drifted, and go back.

The LLM integration (Step 6) is done by Person B (who owns the engine), **after** A, C, and D have their tracks green. Not before.

---

## 11. How to explain the system in 60 seconds

Once you have done Steps 1–6, you can explain the whole thing to a judge in under a minute:

> "We start with three rulebooks and a list of every plan that passes all three. The AI picks one and explains why. Each of the three agents checks the pick against its own rulebook, independently. If they all agree, we're done in one turn. If someone counters, we go one more turn. If that fails, a fixed emergency card picks the safe fallback — no AI involved. Every step writes to a sealed log as it happens. The AI chooses and explains; the rules decide. Here's the import test that proves the AI can't reach the rules."

**That is the whole system.** Everything else in the design is a detail behind one of those sentences.

If you can say that, and the code backs it up, you have already scored well on the rubric's three biggest categories (technical soundness, honest evaluation, trust). The 3-minute pitch is a rendering of those six sentences plus one live run.

---

## 12. The one-sentence answer

**Build the whole loop on paper, then in one file with no AI, then split it, then feed it real data, then add two turns, then add the AI last, then render the log.**

Each step leaves a working thing. Each step is testable on its own. Nothing is built until the thing below it works. That is the method.

If you want the next concrete artifact, I can write out the full `rdcp_one_file.py` with turn-2 support and the fallback path, ready to run, so your team has the working reference before anyone touches a real architecture.
