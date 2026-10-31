# Judging Rubrics

## Application note: organizer clarification, 31 October 2026

The source rubric and its six-criterion weighted table below are unchanged. The
[new challenge clarification](organizer_updates.md) changes the evidence needed
to demonstrate this project:

- **Technical soundness (25%):** one structured message from each independent
  ATC/Pilot/Ground agent per turn; consensus within three turns, otherwise
  deterministic fallback. Show actual turn/message/request counts and the
  configured profile. Current implementation uses two turns/six requests inside
  the three-turn/nine-request ceiling. Fallback uses zero model calls.
- **Problem realism (20%):** use a qualifying row from the organizer-reported
  358,774-row 2015 release and disclose actual source/hash. DOT data contains no
  gates, runways, fuel or crew; the filtered cohort cannot establish complete
  airport traffic or aircraft rotations.
- **Honest evaluation (20%):** freeze and compare identical CSV/scenario/workbook
  or cited-rule versions across B0/B1/system. Label workbook figures synthetic,
  retain failures and report actual denominators. Neither source row count nor
  unit-test count is an agent benchmark or evidence of operational benefit.
- **Trust and traceability (20%):** show workbook sheet/cell/hash or exact real
  citation, units, applicability and extra scenario assumptions. Require three
  current same-plan accepts and subsequent human approval with fresh checks;
  fallback also requires human review.
- **Impact (10%):** report paired modeled trade-offs against fair baselines;
  synthetic operational figures do not establish real-world savings.
- **Time discipline (5%):** record actual submission and keep the presentation
  within three minutes; this is separate from the three agent-turn limit.

The workbook, `challenge2_operational_constraints.xlsx`, is not yet available for
local inspection and the updated CSV has not been independently profiled here.
Existing D01 figures and development smokes
must remain labelled legacy/illustrative. These notes apply the supplied rubric;
they do not introduce a new judging criterion or change its weights.

## Supplied rubric text

Each criterion is scored from 1 to 5. Below, each one has a short explanation of what judges look for and
what separates a strong entry from a weak one.
Problem realism Specific user, credible pain, clear status quo
Technical soundness Right tool for each job, working end to end
Honest evaluation Tested on something, failure cases shown
Trust and traceability Evidence visible, uncertainty handled, human decides
Impact Measured or reasoned improvement over today

Problem realism 1-5
Is this a real problem for a real person, or a solution looking for a use case?
Judges want a named user (a duty manager, a B1 technician, a supply planner), a clear picture of what they do
today without the tool, and a believable cost in time, money or risk.
Strong: a narrow workflow, a user who would plausibly
adopt it, a status quo the team clearly understands.

Weak: "an AI assistant for aviation", or a problem
described only in general terms.

Technical soundness 1-5
Does it work, and is each part of the system doing the job it is suited for?
Does the prototype run end to end, and did the team pick the right method for each task? Language models
handle reading, retrieval, explanation and interaction. Solvers, rules and statistics handle optimisation, compliance
logic and detection.
Strong: a working build that handles input beyond the
demo script, with a clear LLM / non-LLM split.

Weak: an LLM doing arithmetic, scheduling or legality
checks, or a polished interface over a hard-coded
scenario.

Honest evaluation 1-5
Did the team test their work, and do they know where it breaks?
Aerospace values knowing a system's limits as much as its capabilities. Teams should say what they tested on,
report numbers where possible, and show at least one case where the system is wrong, uncertain or declines to
answer.
Strong: stated metrics, a visible failure case, and a clear
account of why it fails and what would fix it.

Weak: only success stories, accuracy claims with no test
behind them, or synthetic data with no discussion of
real-data performance.

Acacia Hackathon Society | 31 October 2026 | NUS 5 / 6
Judging Rubric

Acacia Hackathon Society | 31 October 2026 | NUS 1 / 2
Six criteria. Score each from 1 to 5; apply the weights below. Total weight: 100%.
Problem realism
20%

Technical soundness
25%

Honest evaluation
20%

Trust and traceability
20%

Impact
10%

Time discipline
5%

Problem realism 20% | 1-5
Does this solve a specific aerospace workflow problem?
Name the user, current process and cost in time, money or risk. Explain the constraints of the chosen
challenge, rather than describing aviation in general.
STRONG
A credible user, narrow workflow and clear status quo.
WEAK
A generic aviation chatbot or a solution looking for a
problem.

Technical soundness 25% | 1-5
Does the prototype work, with the right method for each task?
Show an end-to-end build. Use LLMs for reading and explanation; use solvers, rules or statistics for
scheduling, constraints and detection. Test beyond a scripted demo.
STRONG
Working inputs and outputs; explicit constraints and a
sound LLM / non-LLM split.

WEAK
LLM-only legality checks, opaque risk scores or a
hard-coded demo.

Honest evaluation 20% | 1-5
What was tested, against what baseline, and where does it fail?
State metrics, test data and results. Label synthetic data and its limits. Show one case where the system is
wrong, uncertain or declines, and explain how the user knows.
STRONG
A baseline, measured results and a visible failure with a
reason.

WEAK
Only success cases, unsupported accuracy claims or
hidden data limits.

AI & Aerospace Technology Track

AUTONOMOUS SG 2026
AI & AEROSPACE TRACK

Judging Rubric
AI & Aerospace Technology Track | Draft for discussion with Airbus

Each criterion is scored from 1 to 5. Below, each one has a short explanation of what judges look for and
what separates a strong entry from a weak one.
Problem realism Specific user, credible pain, clear status quo
Technical soundness Right tool for each job, working end to end
Honest evaluation Tested on something, failure cases shown
Trust and traceability Evidence visible, uncertainty handled, human decides
Impact Measured or reasoned improvement over today

Problem realism 1-5
Is this a real problem for a real person, or a solution looking for a use case?
Judges want a named user (a duty manager, a B1 technician, a supply planner), a clear picture of what they do
today without the tool, and a believable cost in time, money or risk.
Strong: a narrow workflow, a user who would plausibly
adopt it, a status quo the team clearly understands.

Weak: "an AI assistant for aviation", or a problem
described only in general terms.

Technical soundness 1-5
Does it work, and is each part of the system doing the job it is suited for?
Does the prototype run end to end, and did the team pick the right method for each task? Language models
handle reading, retrieval, explanation and interaction. Solvers, rules and statistics handle optimisation, compliance
logic and detection.
Strong: a working build that handles input beyond the
demo script, with a clear LLM / non-LLM split.

Weak: an LLM doing arithmetic, scheduling or legality
checks, or a polished interface over a hard-coded
scenario.

Honest evaluation 1-5
Did the team test their work, and do they know where it breaks?
Aerospace values knowing a system's limits as much as its capabilities. Teams should say what they tested on,
report numbers where possible, and show at least one case where the system is wrong, uncertain or declines to
answer.
Strong: stated metrics, a visible failure case, and a clear
account of why it fails and what would fix it.

Weak: only success stories, accuracy claims with no test
behind them, or synthetic data with no discussion of
real-data performance.

Judging Rubric
AI & Aerospace Technology Track | Criteria and scoring guidance

Trust and traceability 20% | 1-5
Can a qualified person verify the output before acting?
Trace claims to a document, revision and section, a timestamped signal or an applied rule. Handle
applicability and missing evidence. Keep the qualified person in control.
STRONG
Checkable sources, visible uncertainty and a clear
refusal when needed.

WEAK
Confident unsourced answers or a claim to replace
expert judgement.

Impact 10% | 1-5
What improves over the current process, and by how much?
Compare against the stated baseline: delay minutes, supplier impact, retrieval quality or false alarms, as
relevant. Distinguish measured improvement from estimates.
STRONG
A credible before/after measure and realistic adoption
constraints.

WEAK
Large benefit claims with no baseline or unstated
assumptions.

Time discipline 5% | 1-5
Was the project submitted early and presented efficiently?
Earlier valid submission is better, using the recorded submission time. Also assess a clear, focused
presentation completed within 3 minutes. Give one combined 1-5 score.
STRONG
Earlier submission and a focused, complete
presentation within 3 minutes.

WEAK
Later submission and a rushed, unfocused or over-time
presentation.

SCORING AND JUDGES' NOTES
Scale: 1 = weak; 2 = limited; 3 = credible; 4 = strong; 5 = exceptional evidence.
Weighted total / 100 = sum of (criterion score / 5 x criterion weight).
Example: technical soundness scored 4/5 earns 20 of its 25 points.
Score criteria separately. Ask: "Where does this fail, and how would your user know?"
Reward sound restraint, not presentation polish. Each project is evaluated by 3 judges.
Time is only 5%: earlier submission does not substitute for quality or evidence.
