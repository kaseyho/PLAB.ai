"use strict";

// Arrival disruption desk. Two data sources:
//   ?fixture=<name>  labelled sample payloads in fixtures/ (no backend needed)
//   ?run=<run_id>    live run from Developer 2's API, polled once per second
// All text from payloads (including model rationale) is set with textContent, never HTML.

const params = new URLSearchParams(location.search);
let runParam = params.get("run");
const fixtureName = params.get("fixture") || params.get("sample");
const LIVE = !fixtureName;
const TERMINAL_APPROVAL = ["APPLIED_SIMULATION", "REJECTED", "STALE", "EXPIRED", "CANCELLED"];
const ROLES = ["ATC", "PILOT", "GROUND"];
const ICON = { PASS: "✓ PASS", FAIL: "✗ FAIL", UNKNOWN: "? UNKNOWN" };
const ERRORS = {
  JUDGE_SESSION_REQUIRED: "Reload this page to start your judge session.",
  JUDGE_SESSION_EXPIRED: "Your judge session expired. Reload to start a new one.",
  JUDGE_RUN_LIMIT: "The team's demo run budget has been reached. You can still inspect the saved examples.",
  JUDGE_DESK_BUSY: "Other judges are running negotiations. Please try again in a minute.",
  REVIEWER_SESSION_REQUIRED: "Operator token missing or wrong.",
  ORIGIN_NOT_ALLOWED: "This page address is not the configured public origin.",
  REVIEW_BINDING_MISMATCH: "The plan on screen is out of date. Reload before deciding.",
  RECOMMENDATION_EXPIRED: "The recommendation expired. Refresh and rerun.",
  REVIEW_NOT_PENDING: "This recommendation is already closed.",
  FALLBACK_ACKNOWLEDGEMENT_REQUIRED: "Acknowledge that no consensus was obtained first.",
  IDEMPOTENCY_CONFLICT: "This decision conflicts with one already recorded.",
  REQUIRED_CHECK_FAIL: "The plan no longer passes every hard check against current resources.",
  REQUIRED_CHECK_UNKNOWN: "A required hard check could not be assessed.",
  RESOURCE_STATE_INVALID: "The current resource record needs correction before review.",
  ALREADY_APPLIED: "This plan is already applied.",
  STATE_CHANGED: "Airport resources changed. Refresh the scenario and rerun.",
  INVALID_INPUT: "The request was rejected as invalid.",
};

let run = null;
let lastSeq = 0;
let pollTimer = null;
let pendingKey = null; // reused if the same decision is retried, so the backend applies it once
let busy = false;
let sessionConfigured = false;
let publicDemo = false;
let scenarioList = [];
let reviewResult = null;
let loadPromise = null;
let evaluationReport = null;
let evaluationLoaded = false;
let fuelInputRevision = null;
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
let motionPaused = false;
let motionRunId = null;
let motionWasActive = false;

const $ = (id) => document.getElementById(id);

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function fact(list, label, value, tag) {
  const row = el("div");
  row.append(el("dt", label));
  const dd = el("dd", value === null || value === undefined || value === "" ? "—" : value);
  if (tag) dd.append(el("span", tag, "tag"));
  row.append(dd);
  list.append(row);
}

function planView(plan, checks = [], hash = null) {
  if (!plan) return null;
  return {...plan, plan_hash: hash,
    added_holding_min: plan.impacts.additional_landing_delay_s / 60,
    fuel_margin_kg: plan.impacts.reserve_margin_kg,
    crew_overtime_min: plan.impacts.crew_overtime_min,
    feasible: checks.length > 0 && checks.every((check) => check.status === "PASS")};
}

function proceduralOrder(a, b) {
  for (const key of ["landing_time_s", "gate_in_time_s", "gate_id", "taxi_route_id", "candidate_id"]) {
    if (a.plan[key] !== b.plan[key]) return a.plan[key] < b.plan[key] ? -1 : 1;
  }
  return 0;
}

function view(raw, events = []) {
  const snapshot = raw.snapshot;
  const fields = snapshot.source.original_fields;
  const catalog = raw.catalog?.candidates || [];
  const feasible = catalog.filter((entry) => entry.checks.length && entry.checks.every((c) => c.status === "PASS"));
  const baseline = raw.baseline_b0 || feasible.sort(proceduralOrder)[0];
  const jointOrder = (a, b) => {
    const key = (entry) => {
      const costs = ROLES.map((role) => Number(entry.plan.costs[role]));
      return [Math.max(...costs), costs.reduce((sum, value) => sum + value, 0),
        entry.plan.landing_time_s, entry.plan.gate_in_time_s, entry.plan.gate_id,
        entry.plan.taxi_route_id, entry.plan.candidate_id];
    };
    const x = key(a), y = key(b);
    for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1;
    return 0;
  };
  const baselineB1 = raw.baseline_b1 || [...feasible].sort(jointOrder)[0];
  const ids = new Set([...(raw.catalog?.shortlist_ids || []), raw.candidate?.candidate_id]);
  const source = {natural_identity: snapshot.source.identity, dataset_file: snapshot.source.dataset_id,
    record_ordinal: snapshot.source.record_ordinal, fields,
    qualifies_by: `Weather delay ${fields.WEATHER_DELAY ?? "unknown"} min; late-aircraft delay ${fields.LATE_AIRCRAFT_DELAY ?? "unknown"} min`};
  const blockers = {...(raw.blockers || {})};
  if (!raw.blockers) for (const entry of catalog) for (const check of entry.checks) {
    if (check.status !== "PASS") blockers[check.rule_id] = (blockers[check.rule_id] || 0) + 1;
  }
  const expired = LIVE && raw.approval_state === "PENDING" && raw.expires_at && Date.now() >= Date.parse(raw.expires_at);
  return {...raw, source, approval_state: expired ? "EXPIRED" : raw.approval_state,
    scenario: {scenario_id: snapshot.scenario_id, simulation_epoch_utc: snapshot.simulation_epoch},
    scenario_revision: snapshot.revision, resource_revision: snapshot.resource_revision,
    ruleset_version: snapshot.ruleset_version, policy_version: snapshot.policy_version,
    candidate: planView(raw.candidate, raw.checks, raw.plan_hash),
    candidates: catalog.filter((entry) => ids.has(entry.plan.candidate_id)).map((entry) => planView(entry.plan, entry.checks)),
    catalog_feasible_count: raw.catalog_feasible_count ?? feasible.length,
    baseline_b0: baseline ? planView(baseline.plan, baseline.checks) : null,
    baseline_b1: baselineB1 ? planView(baselineB1.plan, baselineB1.checks) : null,
    missing_facts: raw.catalog?.blocking_reasons || [], blockers,
    events, review_result: reviewResult,
    resource_revision_after: events.findLast((event) => event.event_type === "SIMULATION_APPLIED")?.payload.resource_revision};
}

function emptyRun(snapshot) {
  return view({snapshot, run_id: null, processing_state: "READY", negotiation_outcome: null,
    approval_state: "NOT_APPLICABLE", candidate: null, checks: [], round_count: 0, model_call_count: 0,
    agent_invocation_count: 0, catalog: null, offers: {}, votes: {}});
}

function load() {
  if (!loadPromise) loadPromise = loadRun().finally(() => { loadPromise = null; });
  return loadPromise;
}

async function loadRun() {
  if (LIVE && runParam) {
    const response = await fetch(`/api/runs/${encodeURIComponent(runParam)}`, {
      credentials: "same-origin", signal: AbortSignal.timeout(30000),
    });
    if (response.status === 404 && publicDemo) {
      runParam = null;
      lastSeq = 0;
      run = null;
      history.replaceState(null, "", location.pathname);
      return loadRun();
    }
    if (!response.ok) throw new Error(`Run request failed (${response.status})`);
    const fresh = await response.json();
    const events = await fetch(
      `/api/runs/${encodeURIComponent(runParam)}/events?after_seq=${lastSeq}`, {
        credentials: "same-origin", signal: AbortSignal.timeout(30000),
      });
    const newEvents = events.ok ? await events.json() : [];
    if (!events.ok) throw new Error(`Event request failed (${events.status})`);
    const combined = [...(run ? run.events : []), ...newEvents];
    if (newEvents.length) lastSeq = newEvents[newEvents.length - 1].sequence;
    run = view(fresh, combined);
  } else if (!LIVE) {
    const response = await fetch(`/static/fixtures/${encodeURIComponent(fixtureName)}.json`);
    if (!response.ok) throw new Error(`Sample ${fixtureName} not found`);
    const sample = await response.json();
    run = view(sample, sample.events || []);
  } else {
    const snapshot = scenarioList.find((s) => s.scenario_id === $("scenario-select").value);
    if (!snapshot) throw new Error("No flights are loaded. The server could not read its flight data file; open /health for details.");
    run = emptyRun(snapshot);
  }
  render();
  const keepPolling = LIVE && runParam && (run.processing_state !== "TERMINAL" || run.approval_state === "PENDING");
  clearTimeout(pollTimer);
  if (keepPolling) pollTimer = setTimeout(() => load().catch(showError), 1000);
}

// ---------- Plain-English presentation ----------

const ROLE_NAMES = { ATC: "Air traffic control", PILOT: "Pilot", GROUND: "Ground operations" };
const ROLE_NOTES = {
  ATC: "Runway and aircraft spacing",
  PILOT: "Fuel and flight-crew limits",
  GROUND: "Gates and handling crews",
};
const RULE_NAMES = {
  "SYSTEM-FRESH-01": "Plan matches the latest data",
  "SYSTEM-OPS-01": "Uses the organisers' airport limits",
  "ATC-RWY-01": "Runway is free",
  "ATC-SEP-01": "Safe spacing from other aircraft",
  "ATC-TAXI-01": "Taxi route is open",
  "ATC-CAP-01": "Airport stays within hourly capacity",
  "PILOT-FUEL-01": "Fuel stays above the reserve",
  "PILOT-ROUTE-01": "Arrival route allowed in this weather",
  "PILOT-DUTY-01": "Flight crew within duty hours",
  "GROUND-GATE-01": "Gate fits this aircraft",
  "GROUND-OCC-01": "Gate is free",
  "GROUND-CREW-01": "Ground crew within shift limit",
  "GROUND-EQUIP-01": "Handling equipment available",
  "GROUND-NOTICE-01": "Enough notice for the gate change",
  "GROUND-TURN-01": "Enough time to turn the aircraft around",
};
const FALLBACK_REASONS = {
  PROVIDER_NOT_CONFIGURED: "No AI service was configured, so no agent was asked for a decision.",
  AGENT_TIMEOUT: "A required reply did not arrive before the request deadline. A timeout is not a rejection or an acceptance.",
  PROVIDER_ERROR: "An agent request failed in the model service or its adapter. No valid decision was available from that request.",
  PROVIDER_INCOMPLETE_OR_REFUSED: "The model service returned an incomplete response or refused the request, so it could not count as a decision.",
  INVALID_AGENT_SCHEMA: "A reply did not match the required structured message format and was rejected.",
  INVALID_SCHEMA: "The reply did not match the required structured message format and was rejected.",
  UNKNOWN_AGENT_EVIDENCE: "The reply cited an evidence ID outside the references supplied to that agent. Its claim could not be verified, so the reply was rejected.",
  UNKNOWN_OBJECTION_REFERENCE: "An objection referenced an unknown candidate, rule or evidence ID and could not be verified.",
  INITIAL_POLICY_VIOLATION: "An offer included an option outside that role's allowed initial preference set. Passing the safety checks does not automatically put an option inside that set.",
  CONCESSION_POLICY_VIOLATION: "An agent offered a compromise outside its permitted concession budget. The offer was rejected rather than silently widening that budget.",
  OFFER_RANKING_MISMATCH: "The ranked choices did not match the agent's offered options in the required order.",
  DUPLICATE_OFFER_ID: "An offer repeated an option ID, making the structured offer invalid.",
  PREFERRED_CANDIDATE_MISMATCH: "The stated first choice was not in the agent's acceptable set.",
  EMPTY_OFFER_HAS_PREFERENCE: "The agent named a first choice while offering no acceptable option.",
  VOTE_PLAN_HASH_MISMATCH: "A vote referred to a different version of the plan. It cannot count as agreement on the current proposal.",
  NO_MUTUAL_OFFER: "The agents' acceptable options and offered concessions had no option in common.",
  MISSING_ROLE_OFFER: "A required role had no valid offer, so there was no complete set of options to reconcile.",
  FINAL_VOTE_DISSENT: "A required vote was missing or asked for more information. An information request stops this run; it cannot count as agreement.",
  NO_UNTRIED_MUTUAL_OFFER: "After a rejection, no different option remained in all three agents' offered sets. There was no common revised plan to put to a third vote.",
  THREE_TURN_NO_CONSENSUS: "The third and final turn ended without three explicit accepts of the same plan. No fourth turn is allowed.",
  INSUFFICIENT_TURN_BUDGET: "Too little execution time remained to collect all three fresh replies for a revised plan.",
  NEGOTIATION_DEADLINE: "The negotiation time budget ran out before another required request could start.",
  REQUIRED_CHECK_FAIL: "A deterministic recheck failed. Agent agreement cannot override a failed rule.",
  REQUIRED_CHECK_UNKNOWN: "A required rule could not be verified. Unknown does not count as a pass.",
  RUN_DEADLINE: "The overall run deadline was reached before a checked result could be completed.",
  STATE_CHANGED: "The scenario or airport resources changed during the run, so the old proposal could no longer be used.",
  SNAPSHOT_EXPIRED: "The input snapshot expired during the run and must be refreshed.",
};
const FACT_NAMES = {
  "fuel.projected_fuel_at_earliest_kg": "fuel on board at the earliest landing time",
  "fuel.hold_burn_kg_per_min": "fuel burn while holding",
  "fuel.taxi_burn_kg_per_min": "fuel burn while taxiing",
  "fuel.reserve_floor_kg": "minimum fuel reserve",
  "fuel.uncertainty_allowance_kg": "fuel safety allowance",
  "aircraft.aircraft_class": "aircraft size class",
  "aircraft.wake_category": "aircraft wake category",
  "runway.focal_occupancy_s": "time the aircraft occupies the runway",
};
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function render() {
  const openDetails = [...document.querySelectorAll("details[id][open]")].map((node) => node.id);
  const focusedDetail = document.activeElement?.tagName === "SUMMARY" ? document.activeElement.parentElement.id : null;
  renderMode();
  renderSteps();
  renderFlight();
  renderInputs();
  renderNegotiation();
  renderRunMotion();
  renderReview();
  renderControls();
  renderRunFacts();
  renderCandidates();
  renderChecks();
  renderScenario();
  renderEvidence();
  renderEvaluation();
  for (const id of openDetails) if ($(id)) $(id).open = true;
  if (focusedDetail) $(focusedDetail)?.querySelector("summary")?.focus({preventScroll: true});
}

function simClock(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const epoch = Date.parse(run.snapshot.simulation_epoch);
  const t = new Date(epoch + seconds * 1000);
  return `${String(t.getUTCHours()).padStart(2, "0")}:${String(t.getUTCMinutes()).padStart(2, "0")}`;
}

const minutes = (value) => Math.round(Number(value) * 10) / 10;

function planById(id) {
  for (const entry of run.catalog?.candidates || []) if (entry.plan.candidate_id === id) return entry.plan;
  if (run.candidate?.candidate_id === id) return run.candidate;
  return null;
}

function describe(id) {
  if (!id) return "no option";
  const plan = planById(id);
  if (plan) return `land ${simClock(plan.landing_time_s)} at gate ${plan.gate_id}`;
  const match = /^L(\d+)-/.exec(id);
  return match ? `land ${simClock(Number(match[1]))} (option ${id})` : `option ${id}`;
}

function step() {
  if (!run.run_id) return 1;
  return run.processing_state !== "TERMINAL" ? 2 : 3;
}

function renderMode() {
  const fixture = run.provider?.mode === "FIXTURE";
  const recorded = !LIVE && run.provider?.mode === "LIVE";
  $("mode-note").hidden = LIVE && !fixture;
  $("mode-note").textContent = recorded
    ? `Recorded live run · ${run.provider.model}, ${run.provider.prompt_version}. Read-only; no new model calls.`
    : !LIVE
    ? "Scripted example · real rule checks. Choose “Interactive demo” to try it yourself."
    : "Offline demo · scripted agents, real checks and human approval.";
  $("fixture-select").value = fixtureName || "";
  $("start-row").hidden = !LIVE;
  $("session-hint").hidden = !LIVE;
  $("session-row").hidden = publicDemo;
  $("scenario-select").disabled = busy;
  if (LIVE) $("session-hint").textContent = publicDemo
    ? "No API key needed. Your own simulation."
    : sessionConfigured
    ? "Kept in this browser tab only. Needed to start, stop or decide."
    : "No access code is set up on the server. Add PLAB_SESSION_TOKEN to .env and restart.";
}

function renderSteps() {
  const current = step();
  const finished = current === 3 && ["APPLIED_SIMULATION", "REJECTED", "CANCELLED"].includes(run.approval_state);
  for (const li of $("steps").children) {
    const n = Number(li.dataset.step);
    li.className = n < current || finished ? "done" : n === current ? "current" : "";
    if (n === current && !finished) li.setAttribute("aria-current", "step");
    else li.removeAttribute("aria-current");
  }
  for (const [index, id] of ["step-1", "step-2", "review-card"].entries()) {
    $(id).classList.toggle("current", current === index + 1 && !finished);
  }
  $("step-2").classList.toggle("dim", current < 2);
  $("review-card").classList.toggle("dim", current < 3);
}

function renderFlight() {
  const f = run.source.fields || {};
  const box = $("flight-summary");
  box.replaceChildren();
  const route = el("span", null, "route");
  route.append(document.createTextNode(f.ORIGIN_AIRPORT || "?"), el("span", "→", "arrow"),
    document.createTextNode(f.DESTINATION_AIRPORT || "?"));
  const date = f.YEAR ? `${Number(f.DAY)} ${MONTHS[Number(f.MONTH) - 1]} ${f.YEAR}` : "";
  const reasons = [];
  if (Number(f.WEATHER_DELAY) > 15) reasons.push(`${f.WEATHER_DELAY} min by weather`);
  if (Number(f.LATE_AIRCRAFT_DELAY) > 20) reasons.push(`${f.LATE_AIRCRAFT_DELAY} min by a late incoming aircraft`);
  box.append(route, el("span", `${f.AIRLINE || ""} ${f.FLIGHT_NUMBER || ""} · ${date}`, "meta"),
    el("span", reasons.length ? `Delayed ${reasons.join(" and ")}` : "", "delay"));
}

function snapshotFact(id) {
  return run.snapshot.facts.find((item) => item.fact_id === id);
}

function renderInputs() {
  const ops = snapshotFact("overlay.operations")?.value;
  const list = $("input-facts");
  list.replaceChildren();
  fact(list, "Historical trigger · real", `${run.source.natural_identity} · row ${run.source.record_ordinal}`);
  if (ops) {
    const p = ops.policy;
    fact(list, "Airport inventory · synthetic workbook", `${p.airport_code}: ${p.active_runways} runways, ${p.gates} gates`);
    fact(list, "Capacity / turnaround", `${p.max_ops_per_hour} movements/h · ${p.min_turnaround_s / 60} min`);
    fact(list, "Fuel reserve / contingency", `${p.reserve_hold_min} holding min + ${p.contingency_percent}% trip fuel`);
    fact(list, "Flight duty / ground shift", `${p.flight_crew_duty_s / 3600} h / ${p.ground_shift_s / 3600} h`);
    fact(list, "Modeled resource subset", `${ops.modeled_runway_ids.length} runway · ${snapshotFact("overlay.gates")?.value?.length ?? "unknown"} candidate gates`);
    fact(list, "Workbook cells", `${p.source_cells.airport}; ${p.source_cells.wake}; fuel_and_crew!A1:C9`);
  }
  const fuel = snapshotFact("overlay.fuel")?.value;
  fact(list, "Aircraft fuel state · simulated", fuel?.projected_fuel_at_earliest_kg == null
    ? "Unknown — required before a plan can be verified" : `${fuel.projected_fuel_at_earliest_kg} kg at earliest landing`);
  $("what-if").hidden = !LIVE || !fuel;
  const revision = `${run.snapshot.scenario_id}:${run.snapshot.revision}`;
  if (fuel && revision !== fuelInputRevision) {
    $("fuel-input").value = fuel.projected_fuel_at_earliest_kg ?? "";
    $("fuel-unknown").checked = fuel.projected_fuel_at_earliest_kg == null;
    fuelInputRevision = revision;
  }
}

function renderRunProof() {
  const list = $("run-proof");
  list.replaceChildren();
  const total = run.catalog_total_count ?? run.catalog?.candidates?.length;
  fact(list, "Checked options", total == null ? "Not started" : `${run.catalog_feasible_count} feasible / ${total} checked`);
  fact(list, "Agent turns", run.run_id ? `${run.round_count} / 3 turns · ${run.agent_invocation_count} / 9 requests` : "3 roles · up to 3 turns");
  fact(list, "Real model calls", run.run_id ? run.model_call_count : "Not started");
  fact(list, "Execution time", run.elapsed_ms == null ? "45 s run limit · 12 s per request" : `${minutes(run.elapsed_ms / 1000)} s · 45 s limit`);
}

function renderNegotiation() {
  renderRunProof();
  const status = $("negotiation-status");
  const n = run.round_count;
  const outcome = run.negotiation_outcome;
  if (!run.run_id) status.textContent = "Choose a flight and start. The three agents will compare plans.";
  else if (run.processing_state !== "TERMINAL") status.textContent = n ? `Turn ${n} of 3 · waiting for the agents’ replies…` : "Checking the available plans…";
  else if (outcome === "FEASIBLE_CONSENSUS") status.textContent = `All three agents agreed on the same plan in ${n} ${n === 1 ? "turn" : "turns"}.`;
  else if (outcome === "FEASIBLE_FALLBACK") {
    status.textContent = n ? `Negotiation stopped during turn ${n}. A checked backup plan is ready for review.`
      : "No agent turns ran. A checked backup plan is ready for review.";
  } else if (outcome === "CANCELLED") status.textContent = "The negotiation was stopped.";
  else status.textContent = run.agent_invocation_count ? "The run ended. See the outcome below." : "No agent calls were needed. See the outcome below.";

  const box = $("agent-cards");
  box.replaceChildren();
  for (const role of ROLES) {
    const card = el("article", null, "desk");
    card.dataset.role = role;
    const heading = el("div", null, "desk-heading");
    const icon = el("span", null, "role-icon");
    icon.setAttribute("aria-hidden", "true");
    const symbol = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    symbol.setAttribute("viewBox", "0 0 24 24");
    symbol.setAttribute("fill", "none");
    symbol.setAttribute("stroke", "currentColor");
    symbol.setAttribute("stroke-width", "1.4");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `/static/roles.svg#${role.toLowerCase()}`);
    symbol.append(use);
    icon.append(symbol);
    heading.append(icon, el("h4", ROLE_NAMES[role]));
    card.append(heading, el("p", ROLE_NOTES[role], "role-note"));
    const offer = (run.offers || {})[role];
    const previous = run.previous_votes?.[role];
    const vote = (run.votes || {})[role];
    const progress = agentProgress(role);
    card.append(el("p", progress.label, `desk-status ${progress.tone}`));
    if (progress.detail) card.append(el("p", progress.detail, "agent-explanation"));
    if (progress.rationale) {
      card.append(el("p", "Agent's stated reason", "turn-label"), el("blockquote", progress.rationale, "agent-reason"));
    }
    const messages = el("details", null, "agent-messages");
    messages.id = `messages-${role}`;
    messages.append(el("summary", "View agent messages"));
    if (offer) {
      const turn = el("div", null, "turn");
      turn.append(el("div", "Turn 1 · offer", "turn-label"),
        el("p", offer.preferred_candidate_id ? `First choice: ${describe(offer.preferred_candidate_id)}` : "Refused every option"));
      const others = (offer.concession_candidate_ids || []).filter((id) => id !== offer.preferred_candidate_id).length;
      if (others) turn.append(el("p", `Would also accept ${others} other ${others === 1 ? "option" : "options"}`, "why"));
      if (offer.rationale) turn.append(el("p", offer.rationale, "why"));
      messages.append(turn);
    }
    if (previous) messages.append(voteBlock(2, previous, votedPlan(previous)?.candidate_id || run.previous_candidate_id));
    if (vote) messages.append(voteBlock(run.round_count, vote, votedPlan(vote)?.candidate_id));
    if (offer || previous || vote) card.append(messages);
    box.append(card);
  }
  renderConsensusExplanation();
}

function votedPlan(vote) {
  const proposal = run.events.findLast((event) => event.event_type === "PROPOSAL_SELECTED"
    && event.payload.plan_hash === vote.plan_hash);
  if (proposal) return planById(proposal.payload.candidate_id) || {candidate_id: proposal.payload.candidate_id};
  if (run.plan_hash === vote.plan_hash) return run.candidate;
  // Older scripted projections omit proposal hashes but retain this explicit binding.
  if (run.previous_candidate_id && Object.values(run.previous_votes || {}).some((prior) => prior.plan_hash === vote.plan_hash)) {
    return planById(run.previous_candidate_id) || {candidate_id: run.previous_candidate_id};
  }
  return null;
}

function agentProgress(role) {
  const vote = run.votes?.[role];
  const offer = run.offers?.[role];
  const start = run.events.findLast((event) => event.event_type === "AGENT_REQUEST_STARTED" && event.payload.role === role);
  const response = start?.payload.request_id && run.events.findLast((event) =>
    event.event_type === "AGENT_RESPONSE_RECEIVED" && event.payload.request_id === start.payload.request_id);
  const error = run.events.findLast((event) => event.event_type === "AGENT_PROTOCOL_ERROR"
    && event.payload.role === role && (!start || event.sequence > start.sequence));
  const terminal = run.processing_state === "TERMINAL";
  const pending = !terminal && start?.payload.round === run.round_count && !response;
  const result = {label: run.run_id ? "Not run" : "Ready", tone: "", detail: "", rationale: "", code: null,
    pending, responseStatus: response?.payload.status};
  if (vote) {
    const accepted = vote.decision === "ACCEPT";
    result.label = accepted ? "Accepted proposal" : vote.decision === "REJECT" ? "Rejected proposal" : "Needs information";
    result.tone = accepted ? "accepted" : "attention";
    result.code = vote.decision;
    if (!accepted) result.rationale = vote.rationale;
  } else if (error) {
    result.code = error.payload.reason_code;
    result.label = "Reply rejected";
    result.tone = "attention";
    result.detail = FALLBACK_REASONS[error.payload.reason_code] || `The recorded reply failed validation (${error.payload.reason_code}).`;
  } else if (response?.payload.status === "TIMEOUT") {
    result.code = "AGENT_TIMEOUT";
    result.label = "Timed out";
    result.tone = "attention";
    result.detail = `Turn ${start.payload.round}: no reply before the request deadline. No preference or vote can be inferred.`;
  } else if (response?.payload.status === "ERROR") {
    result.code = "PROVIDER_ERROR";
    result.label = "Request failed";
    result.tone = "attention";
    result.detail = "The model service or adapter failed. No valid decision was received.";
  } else if (response?.payload.status === "CANCELLED") {
    result.label = "Request stopped";
    result.detail = "This pending request was cancelled when the coordinator stopped the turn. It is not a rejection.";
  } else if (offer && (!start || start.payload.round === 1)) {
    result.label = "Offer received";
    if (terminal) result.detail = "An offer is a preference, not a final acceptance of a shared plan.";
    if (run.fallback_reason === "NO_MUTUAL_OFFER" || run.fallback_reason === "NO_UNTRIED_MUTUAL_OFFER") {
      result.rationale = offer.rationale;
      result.detail = `${offer.acceptable_candidate_ids.length} initial choices and ${offer.concession_candidate_ids.length} offered concessions.`;
    }
  } else if (start) {
    result.label = terminal ? "No valid reply" : "Waiting for reply";
    result.detail = terminal ? "No validated decision was recorded before the run stopped." : "";
  }
  return result;
}

// Keep these nodes in place: polling must not restart an in-flight animation.
function setMotionText(id, value) {
  if ($(id).textContent !== value) $(id).textContent = value;
}

function syncMotionPreference() {
  const paused = motionPaused || reducedMotion.matches || document.hidden;
  $("run-visual").dataset.motion = paused ? "paused" : "playing";
  const button = $("motion-toggle");
  button.disabled = reducedMotion.matches;
  button.setAttribute("aria-pressed", String(motionPaused || reducedMotion.matches));
  button.textContent = reducedMotion.matches ? "Reduced motion" : motionPaused ? "Resume motion" : "Pause motion";
  button.title = reducedMotion.matches ? "Your device prefers reduced motion. Status updates stay active."
    : `${motionPaused ? "Resume" : "Pause"} the animation. The run keeps going.`;
}

function renderRunMotion() {
  const panel = $("run-visual");
  panel.hidden = !run?.run_id;
  if (!run?.run_id) {
    panel.dataset.active = "false";
    motionRunId = null;
    motionWasActive = false;
    return;
  }
  const active = LIVE && run.processing_state !== "TERMINAL";
  const sameRun = motionRunId === run.run_id;
  const mayAnimate = sameRun && !motionPaused && !reducedMotion.matches && !document.hidden;
  const stages = {
    CREATED: ["Preparing", "Starting the run"],
    VALIDATING: ["Checking", "Checking inputs"],
    PREPARING: ["Checking", "Checking available plans"],
    ROUND_1: ["Offers", "Comparing offers"],
    RECONCILING: ["Matching", "Finding a shared option"],
    ROUND_2: ["Voting", "Voting on one plan"],
    REVISING: ["Revising", "Finding another option"],
    ROUND_3: ["Re-voting", "Voting on the revised plan"],
    FINAL_VALIDATION: ["Checking", "Rechecking the chosen plan"],
    FALLBACK: ["Backup", "Rules are choosing a backup", "warn"],
  };
  const outcomes = {
    FEASIBLE_CONSENSUS: ["Agreed", "Agents agreed · your review next", "ok"],
    FEASIBLE_FALLBACK: ["Backup", "No agreement · review the backup", "warn"],
    NO_FEASIBLE_OPTION_IN_SEARCH_SPACE: ["Blocked", "No plan passed every check", "bad"],
    INSUFFICIENT_DATA: ["Missing data", "Required data is missing", "warn"],
    CANCELLED: ["Stopped", "Run stopped · nothing applied", "neutral"],
    INTERRUPTED: ["Interrupted", "Run interrupted · nothing applied", "warn"],
    SYSTEM_ERROR: ["Error", "Run failed · nothing applied", "bad"],
  };
  const decisions = {
    APPLIED_SIMULATION: ["Applied", "Approved and applied to simulation", "ok"],
    REJECTED: ["Rejected", "You rejected the plan · nothing applied", "neutral"],
    STALE: ["Out of date", "Resources changed · run again", "warn"],
    EXPIRED: ["Expired", "Review window closed · run again", "warn"],
    CANCELLED: ["Stopped", "Run stopped · nothing applied", "neutral"],
  };
  const [title, caption, tone = "neutral"] = decisions[run.approval_state]
    || (run.processing_state === "TERMINAL" ? outcomes[run.negotiation_outcome] : stages[run.processing_state])
    || ["Waiting", "Waiting for run status", "neutral"];
  const voting = /^ROUND_[123]$/.test(run.processing_state);
  setMotionText("hub-label", voting ? `Turn ${run.round_count} / 3` : "Plan");
  setMotionText("hub-state", title);
  setMotionText("motion-stage", caption);
  const replies = run.round_count === 1 ? Object.keys(run.offers || {}).length : Object.keys(run.votes || {}).length;
  setMotionText("motion-count", active && voting ? `${replies} / 3 valid replies`
    : run.elapsed_ms != null ? `${minutes(run.elapsed_ms / 1000)} s · ${run.round_count} / 3 turns` : "");
  setMotionText("motion-mode", !LIVE ? "Saved run" : run.provider?.mode === "FIXTURE" ? "Scripted run"
    : run.provider?.mode === "UNAVAILABLE" ? "Rule-based run" : "Live run");
  panel.dataset.active = String(active);
  panel.dataset.tone = tone;
  $("motion-toggle").hidden = !active;
  for (const role of ROLES) {
    const progress = agentProgress(role);
    const node = $(`signal-${role}`);
    const state = active && progress.pending ? "waiting" : progress.tone === "accepted" ? "accepted"
      : progress.tone === "attention" ? "attention" : progress.label === "Offer received" ? "offered" : "idle";
    const label = state === "waiting" ? "Thinking" : state === "accepted" ? "Accepted proposal"
      : progress.code === "NEEDS_INFORMATION" ? "Needs info"
      : progress.responseStatus === "RECEIVED" && progress.label === "Waiting for reply" ? "Checking reply" : progress.label;
    if (mayAnimate && node.dataset.state === "waiting" && ["accepted", "offered", "attention"].includes(state)) {
      node.querySelector(".signal-icon").animate([
        {transform: "scale(1)"}, {transform: "scale(1.16)"}, {transform: "scale(1)"},
      ], {duration: 420, easing: "ease-out"});
    }
    node.dataset.state = state;
    $(`signal-path-${role}`).dataset.state = state;
    setMotionText(`signal-status-${role}`, label);
  }
  if (mayAnimate && motionWasActive && !active) {
    $("plan-hub").animate([{opacity: .35}, {opacity: 1}], {duration: 450, easing: "ease-out"});
  }
  motionRunId = run.run_id;
  motionWasActive = active;
  syncMotionPreference();
}

function renderConsensusExplanation() {
  const box = $("consensus-explanation");
  box.replaceChildren();
  box.hidden = true;
  if (!run.run_id || run.processing_state !== "TERMINAL" || run.negotiation_outcome === "CANCELLED") return;
  const consensus = run.negotiation_outcome === "FEASIBLE_CONSENSUS";
  const priorDissent = Object.entries(run.previous_votes || {}).filter(([, vote]) => vote.decision !== "ACCEPT");
  if (consensus && !priorDissent.length) return;
  box.hidden = false;
  box.className = `consensus-explanation ${consensus ? "resolved" : ""}`;
  box.append(el("h3", consensus ? "Why a third turn was needed" : run.round_count ? "Why agreement was not reached" : "Why negotiation did not start"));
  if (consensus) {
    for (const [role, vote] of priorDissent) {
      box.append(el("p", `${ROLE_NAMES[role]} ${vote.decision === "REJECT" ? "rejected" : "asked for information on"} the turn 2 proposal (${describe(run.previous_candidate_id)}).`),
        el("blockquote", vote.rationale, "agent-reason"));
    }
    box.append(el("p", "The coordinator proposed a different plan. All three agents then explicitly accepted its new hash in turn 3."));
    return;
  }
  const reason = run.fallback_reason || run.reason_code;
  let explanation = FALLBACK_REASONS[reason];
  const failureRoles = ROLES.filter((role) => {
    const code = agentProgress(role).code;
    return code === reason || (reason === "INVALID_AGENT_SCHEMA" && code === "INVALID_SCHEMA");
  });
  if (failureRoles.length && explanation) explanation = `${failureRoles.map((role) => ROLE_NAMES[role]).join(" and ")}: ${explanation}`;
  const informationRoles = ROLES.filter((role) => run.votes?.[role]?.decision === "NEEDS_INFORMATION");
  if (reason === "FINAL_VOTE_DISSENT" && informationRoles.length) {
    explanation = `${informationRoles.map((role) => ROLE_NAMES[role]).join(" and ")} needed more information and did not accept. All three agents must accept.`;
  }
  if (run.negotiation_outcome === "INSUFFICIENT_DATA") {
    explanation = "Required data was missing. No plan could be checked, so the agents did not negotiate.";
  } else if (run.negotiation_outcome === "NO_FEASIBLE_OPTION_IN_SEARCH_SPACE") {
    explanation = "No available plan passed every rule. There was nothing feasible for the agents to agree on.";
  }
  box.append(el("p", explanation || "The run stopped without a complete, validated agreement. The recorded cause is shown below; no more specific cause was captured."));
  const proposalEvent = run.events.findLast((event) => event.event_type === "PROPOSAL_SELECTED");
  const proposal = proposalEvent && planById(proposalEvent.payload.candidate_id);
  if (proposal) {
    const proposalHash = proposalEvent.payload.plan_hash
      || (proposal.candidate_id === run.candidate?.candidate_id ? run.plan_hash : null);
    const accepted = Object.values(run.votes || {}).filter((vote) => vote.decision === "ACCEPT" && vote.plan_hash === proposalHash).length;
    box.append(el("p", `${accepted} of 3 agents accepted: ${describe(proposal.candidate_id)}.`));
    const recheck = run.events.findLast((event) => event.event_type === "PLAN_REVALIDATED" && event.payload.candidate_id === proposal.candidate_id);
    const checks = recheck?.payload.checks
      || run.catalog?.candidates.find((entry) => entry.plan.candidate_id === proposal.candidate_id)?.checks || [];
    const invalidated = ["STATE_CHANGED", "SNAPSHOT_EXPIRED", "REQUIRED_CHECK_FAIL", "REQUIRED_CHECK_UNKNOWN"].includes(reason);
    if (!invalidated && checks.length && checks.every((check) => check.status === "PASS")) {
      box.append(el("p", `${checks.length} rule checks passed. The agents' stated concerns are not verified facts.`, "small"));
    }
  } else if (run.round_count) {
    box.append(el("p", "The run stopped before a common proposal reached a voting turn."));
  }
  if (run.negotiation_outcome === "FEASIBLE_FALLBACK") {
    box.append(el("p", "Rules chose the backup plan below. It still needs your approval."));
  }
  if (reason) box.append(el("p", `Recorded cause: ${reason}`, "muted small"));
}

function voteBlock(turnNumber, vote, candidateId) {
  const turn = el("div", null, "turn");
  const words = { ACCEPT: "Agreed", REJECT: "Rejected", NEEDS_INFORMATION: "Asked for more information" };
  const yes = vote.decision === "ACCEPT";
  turn.append(el("div", `Turn ${turnNumber} · vote on ${candidateId ? describe(candidateId) : "the recorded proposal"}`, "turn-label"),
    el("p", words[vote.decision] || vote.decision, `vote ${yes ? "yes" : "no"}`));
  if (vote.rationale) turn.append(el("p", vote.rationale, "why"));
  if (vote.reason_codes?.length) turn.append(el("p", `Reason codes: ${vote.reason_codes.join(", ")}`, "small muted"));
  if (vote.evidence_ids?.length) turn.append(el("p", `Evidence: ${vote.evidence_ids.join(", ")}`, "small muted"));
  return turn;
}

function reviewState() {
  const outcome = run.negotiation_outcome;
  const approval = run.approval_state;
  if (!run.run_id) return ["info", "Your plan will appear here", "Review the checks, then approve or reject with a reason."];
  if (approval === "APPLIED_SIMULATION") return ["ok", "Applied to simulation", "The runway, gate, crew and equipment are booked. Your decision is recorded in the audit trail."];
  if (approval === "REJECTED") return ["info", "Rejected", "Nothing was changed."];
  if (approval === "CANCELLED") return ["info", "Stopped", "Nothing was changed."];
  if (approval === "STALE") return ["warn", "This plan is out of date", "Airport resources changed. Run again to check an updated plan."];
  if (approval === "EXPIRED") return ["warn", "Review window closed", "This plan’s two-minute review window has ended. Run again for an updated plan."];
  if (run.processing_state && run.processing_state !== "TERMINAL") return ["info", "Negotiating…", `Turn ${Math.max(run.round_count, 1)} of 3. The plan will appear here when the agents finish.`];
  if (outcome === "FEASIBLE_CONSENSUS") return ["info", "Ready for your review", "All three agents accepted this plan. Your approval is still required."];
  if (outcome === "FEASIBLE_FALLBACK") return ["warn", "Backup plan · review required", "The agents did not agree. Fixed rules chose the earliest plan that passes every check."];
  if (outcome === "NO_FEASIBLE_OPTION_IN_SEARCH_SPACE") return ["bad", "No plan passes the checks", "None of the options in the search window is feasible. Choose another flight, or inspect the reasons below."];
  if (outcome === "INSUFFICIENT_DATA") return ["warn", "Required information is missing", "No plan can be approved. See what’s missing below, or choose another flight."];
  if (outcome === "SYSTEM_ERROR") return ["bad", "Something went wrong", "The checks could not be completed, so nothing can be approved."];
  return ["info", "Waiting", ""];
}

function renderReview() {
  const [tone, title, detail] = reviewState();
  const banner = $("review-state");
  banner.className = `banner ${tone}`;
  if (banner.dataset.state !== title + detail) {
    banner.replaceChildren(el("strong", title), el("span", detail));
    banner.dataset.state = title + detail;
  }

  const box = $("plan-summary");
  box.replaceChildren();
  const plan = run.candidate;
  if (plan) {
    box.append(el("p", `Land at ${simClock(plan.landing_time_s)} and park at gate ${plan.gate_id}.`, "summary"));
    const b0 = run.baseline_b0;
    if (b0) {
      const hold = minutes(plan.added_holding_min - b0.added_holding_min);
      const overtime = minutes(plan.crew_overtime_min - b0.crew_overtime_min);
      const parts = [];
      if (hold) parts.push(`${Math.abs(hold)} min ${hold > 0 ? "more" : "less"} holding in the air`);
      if (overtime) parts.push(`${Math.abs(overtime)} min ${overtime > 0 ? "more" : "less"} ground-crew overtime`);
      box.append(el("p", parts.length
        ? `Compared with the earliest safe option: ${parts.join(", ")}.`
        : "This is the same as the earliest safe option.", "tradeoff"));
    }
    const stats = el("div", null, "stats");
    for (const [label, value] of [
      ["Landing", simClock(plan.landing_time_s)],
      ["At the gate", simClock(plan.gate_in_time_s)],
      ["Extra holding", `${minutes(plan.added_holding_min)} min`],
      ["Fuel to spare", `${Number(plan.fuel_margin_kg)} kg`],
      ["Crew overtime", `${minutes(plan.crew_overtime_min)} min`],
    ]) {
      const cell = el("div", null, "stat");
      cell.append(el("div", label, "label"), el("div", value, "value"));
      stats.append(cell);
    }
    box.append(stats);
    box.append(el("p", "Times use the scenario clock. Fuel and overtime are modelled.", "time-note"));
  } else if (run.run_id && run.processing_state === "TERMINAL") {
    const reasons = el("ul", null, "reasons");
    for (const fact of run.missing_facts || []) reasons.append(el("li", `Missing: ${FACT_NAMES[fact] || fact.replaceAll("_", " ")}`));
    const total = run.catalog_total_count ?? run.catalog?.candidates?.length;
    for (const [rule, count] of Object.entries(run.blockers || {}).sort((a, b) => b[1] - a[1])) {
      reasons.append(el("li", `${RULE_NAMES[rule] || rule}: fails for ${count}${total ? ` of ${total}` : ""} options`));
    }
    if (reasons.children.length) box.append(el("p", "Why:", "muted small"), reasons);
  }
  renderSafety();
  renderBaselines();

  const result = $("review-result");
  if (run.review_result) {
    result.hidden = false;
    result.className = `result ${run.review_result.http_status === 200 ? "ok" : "bad"}`;
    result.textContent = run.review_result.message;
  } else {
    result.hidden = true;
  }
}

function renderBaselines() {
  const box = $("baseline-comparison");
  box.replaceChildren();
  box.hidden = !run.candidate;
  if (!run.candidate) return;
  box.append(el("h3", "Compared with the same-input baselines"));
  const wrap = el("div", null, "table-wrap");
  wrap.tabIndex = 0;
  wrap.setAttribute("role", "region");
  wrap.setAttribute("aria-label", "Baseline comparison; scroll horizontally for all measures");
  const table = el("table", null, "baseline-table");
  const head = el("tr");
  for (const label of ["Policy", "Landing / gate", "Extra hold", "Fuel used", "Fuel spare", "Crew overtime"]) {
    const cell = el("th", label);
    cell.scope = "col";
    head.append(cell);
  }
  const thead = el("thead");
  thead.append(head);
  const tbody = el("tbody");
  for (const [label, plan] of [
    [run.negotiation_outcome === "FEASIBLE_CONSENSUS" ? "Selected · agent agreement" : "Selected · fallback", run.candidate],
    ["B0 · earliest feasible", run.baseline_b0],
    ["B1 · balanced cost", run.baseline_b1],
  ]) {
    const row = el("tr");
    if (plan === run.candidate) row.className = "selected";
    const name = el("th", label);
    name.scope = "row";
    row.append(name);
    if (plan) {
      for (const value of [`${simClock(plan.landing_time_s)} / ${plan.gate_id}`, `${minutes(plan.added_holding_min)} min`,
        `${minutes(plan.impacts.modeled_fuel_used_kg)} kg`, `${minutes(plan.fuel_margin_kg)} kg`, `${minutes(plan.crew_overtime_min)} min`]) {
        row.append(el("td", value));
      }
    } else {
      const missing = el("td", "No comparable feasible result was retained.");
      missing.colSpan = 5;
      row.append(missing);
    }
    tbody.append(row);
  }
  table.append(thead, tbody);
  wrap.append(table);
  box.append(wrap, el("p", `All rows use scenario revision ${run.scenario_revision}, the same rules and full catalog. B1 minimises the highest role cost, then total cost. These are modeled impacts, not historical savings or measured coordination time.`, "small muted"));
}

function friendlyMargin(check) {
  if (check.margin === null || check.margin === undefined) return "";
  const value = Number(check.margin);
  if (value === 0) return "right at the limit";
  if (check.units === "s") return Math.abs(value) >= 120 ? `${Math.round(value / 60)} min spare` : `${value} s spare`;
  if (check.units === "kg") return `${value} kg spare`;
  return `${value} ${check.units} spare`;
}

function renderSafety() {
  const box = $("safety-summary");
  box.replaceChildren();
  const checks = run.checks || [];
  if (!checks.length) return;
  const passed = checks.filter((c) => c.status === "PASS").length;
  const all = passed === checks.length;
  const details = el("details", null, "safety");
  details.id = "safety-details";
  const summary = el("summary");
  summary.append(el("span", all ? `✓ All ${checks.length} safety checks passed` : `✗ ${checks.length - passed} of ${checks.length} safety checks did not pass`, all ? "pass" : "fail"),
    el("span", " · view checks", "muted small"));
  details.append(summary);
  const list = el("ul");
  for (const check of checks) {
    const item = el("li");
    const ok = check.status === "PASS";
    item.append(el("span", ok ? "✓" : check.status === "FAIL" ? "✗" : "?", `mark ${ok ? "pass" : "fail"}`),
      el("span", RULE_NAMES[check.rule_id] || check.rule_id), el("span", ok ? friendlyMargin(check) : check.reason_code.replaceAll("_", " ").toLowerCase(), "extra"));
    list.append(item);
  }
  details.append(list);
  box.append(details);
}

function renderControls() {
  const plan = run.candidate;
  const token = $("session-token").value.trim();
  const authorized = LIVE && sessionConfigured && (publicDemo || token);
  const running = LIVE && run.run_id && run.processing_state !== "TERMINAL";
  const fuelValid = !snapshotFact("overlay.fuel")?.value || $("fuel-unknown").checked || ($("fuel-input").value.trim() !== ""
    && Number.isFinite(Number($("fuel-input").value)) && Number($("fuel-input").value) >= 0
    && Number($("fuel-input").value) <= 1000000);
  $("fuel-input").disabled = !authorized || busy || Boolean(running) || $("fuel-unknown").checked;
  $("fuel-unknown").disabled = !authorized || busy || Boolean(running);
  $("run-inputs-btn").hidden = !run.run_id;
  $("run-inputs-btn").disabled = !authorized || busy || Boolean(running) || !fuelValid;
  $("start-btn").disabled = !authorized || busy || Boolean(running) || !fuelValid;
  $("start-btn").hidden = Boolean(run.run_id) && !running;
  $("start-btn").textContent = running ? "Negotiating…" : busy ? "Starting…" : "Start negotiation";
  $("cancel-btn").disabled = !authorized || busy || !(running || (LIVE && run.approval_state === "PENDING"));
  $("cancel-btn").hidden = !running;

  const reviewable = run.approval_state === "PENDING" && plan && run.checks.length > 0
    && run.checks.every((c) => c.status === "PASS");
  const fallback = run.negotiation_outcome === "FEASIBLE_FALLBACK";
  const closed = TERMINAL_APPROVAL.includes(run.approval_state);
  $("ack-row").hidden = !(reviewable && fallback);
  $("review-controls").hidden = !run.run_id || (!reviewable && !closed && run.processing_state !== "TERMINAL");
  const canAct = reviewable && authorized && !busy;
  const hasReason = $("reason-input").value.trim().length > 0;
  $("approve-btn").disabled = !canAct || !hasReason || (fallback && !$("ack-box").checked);
  $("reject-btn").disabled = !canAct || !hasReason;
  $("reason-input").disabled = !canAct;
  $("approve-btn").hidden = $("reject-btn").hidden = $("reason-input").parentElement.hidden = !reviewable;
  $("rerun-btn").disabled = !authorized || busy || !fuelValid;
  $("rerun-btn").hidden = !LIVE || run.processing_state !== "TERMINAL" || reviewable;
  $("review-hint").textContent = !LIVE
    ? "Saved example · choose “Interactive demo” to start your own run."
    : !reviewable
    ? (closed || run.processing_state === "TERMINAL" ? "Run again with current resources, or choose another flight above." : "")
      : !publicDemo && !token ? "Enter the access code in step 1 to decide."
        : !hasReason ? "Add a reason to enable approval or rejection."
          : "Approval rechecks the plan and records your decision.";
}

// ---------- Technical details ----------

function renderRunFacts() {
  const list = $("run-facts");
  list.replaceChildren();
  fact(list, "Processing state", run.processing_state);
  fact(list, "Outcome", run.negotiation_outcome);
  fact(list, "Approval state", run.approval_state);
  fact(list, "Turns", `${run.round_count} of 3`);
  fact(list, "Model calls", `${run.model_call_count} of 9`);
  fact(list, "Agent invocations", run.agent_invocation_count);
  if (run.provider) fact(list, "Provider", `${run.provider.mode} · ${run.provider.model}`, run.provider.prompt_version);
  if (run.planning_mode) fact(list, "Planner", run.planning_mode);
  if (run.elapsed_ms != null) fact(list, "Elapsed", `${run.elapsed_ms} ms`);
  if (run.fallback_reason) fact(list, "Fallback reason", run.fallback_reason);
  if (run.reason_code) fact(list, "Reason code", run.reason_code);
  if (run.candidate) fact(list, "Plan", run.candidate.candidate_id);
  if (run.plan_hash) fact(list, "Plan hash", `${run.plan_hash.slice(0, 16)}…`);
  if (run.expires_at) fact(list, "Review expires", new Date(run.expires_at).toLocaleString());
  if (run.resource_revision_after) fact(list, "Resource version after approval", run.resource_revision_after);
  if (run.run_id) fact(list, "Run ID", run.run_id);
}

function renderCandidates() {
  const body = $("candidate-table").querySelector("tbody");
  body.replaceChildren();
  const selected = run.candidate && run.candidate.candidate_id;
  for (const c of run.candidates || []) {
    const row = el("tr");
    if (c.candidate_id === selected) row.className = "selected";
    const labels = [];
    if (c.candidate_id === selected) labels.push("chosen");
    if (run.baseline_b0 && c.candidate_id === run.baseline_b0.candidate_id) labels.push("B0 earliest safe");
    if (run.baseline_b1 && c.candidate_id === run.baseline_b1.candidate_id) labels.push("B1 joint optimum");
    const cells = [
      c.candidate_id + (labels.length ? ` (${labels.join(", ")})` : ""),
      simClock(c.landing_time_s), simClock(c.gate_in_time_s), `${c.gate_id} via ${c.taxi_route_id}`,
      `${minutes(c.added_holding_min)} min`, `${Number(c.fuel_margin_kg)} kg`, `${minutes(c.crew_overtime_min)} min`,
      `${Number(c.costs.ATC).toFixed(3)} / ${Number(c.costs.PILOT).toFixed(3)} / ${Number(c.costs.GROUND).toFixed(3)}`,
    ];
    for (const text of cells) row.append(el("td", text));
    row.append(el("td", c.feasible ? "✓ passes" : "✗ blocked", `status-${c.feasible ? "PASS" : "FAIL"}`));
    body.append(row);
  }
  $("cand-note").textContent = (run.candidates || []).length
    ? `Shortlist from ${run.catalog_total_count ?? run.catalog.candidates.length} checked options, ${run.catalog_feasible_count} of which pass every safety rule. B0 is the earliest feasible option. B1 minimises the highest role cost, then total cost, over the same full catalog. Costs are simulation policy scores, lower is better.`
    : "No options to compare for this outcome.";
}

function renderChecks() {
  const box = $("check-list");
  box.replaceChildren();
  if (!run.checks || !run.checks.length) {
    box.append(el("p", "No plan was checked for this outcome.", "muted small"));
    return;
  }
  for (const check of run.checks) {
    const item = el("details", null, "check");
    item.id = `check-${check.rule_id}`;
    const summary = el("summary");
    summary.append(el("span", ICON[check.status], `status-${check.status}`));
    summary.append(el("span", `${check.rule_id} · ${RULE_NAMES[check.rule_id] || check.reason_code}`));
    const margin = check.margin === null || check.margin === undefined ? "" : `margin ${check.margin} ${check.units}`;
    summary.append(el("span", margin, "muted"));
    item.append(summary);
    item.append(el("pre", JSON.stringify({
      actual: check.actual, operator: check.operator, required: check.required, units: check.units,
      reason: check.reason_code, evidence: check.evidence_ids, rule_version: check.rule_version,
    }, null, 2)));
    box.append(item);
  }
}

function renderScenario() {
  const list = $("scenario-facts");
  list.replaceChildren();
  const source = run.source || {};
  const fields = source.fields || {};
  fact(list, "Historical flight", source.natural_identity, "real");
  fact(list, "Qualifies by", source.qualifies_by, "real");
  fact(list, "Source row", source.record_ordinal ? `${source.dataset_file} record ${source.record_ordinal}` : null);
  fact(list, "Dataset hash", `${run.snapshot.source.dataset_sha256.slice(0, 12)}…`);
  fact(list, "Scheduled", fields.SCHEDULED_DEPARTURE ? `dep ${fields.SCHEDULED_DEPARTURE}, arr ${fields.SCHEDULED_ARRIVAL} (local)` : null, "real");
  fact(list, "Scenario", run.scenario ? `${run.scenario.scenario_id} revision ${run.scenario_revision}` : null, "simulated");
  fact(list, "Simulation start", run.scenario && run.scenario.simulation_epoch_utc, "simulated");
  fact(list, "Rules / policy", `${run.ruleset_version} / ${run.policy_version}`);
  fact(list, "Airport record version", run.resource_revision);
  const ops = run.snapshot.facts.find((f) => f.fact_id === "overlay.operations")?.value;
  if (ops) {
    const p = ops.policy;
    fact(list, "Organisers' workbook", `${p.airport_code} · ${p.workbook_sha256.slice(0, 12)}…`);
    fact(list, "Airport limits", `${p.active_runways} runways · ${p.gates} gates · ${p.max_ops_per_hour} movements/hour`);
    fact(list, "Modelled here", `${ops.modeled_runway_ids.length} runway, up to ${p.spare_gates} spare gates`);
    fact(list, "Turnaround / gate notice", `${p.min_turnaround_s / 60} / ${p.gate_notice_s / 60} min`);
    fact(list, "Fuel policy", `${p.reserve_hold_min} min reserve + ${p.contingency_percent}% contingency`);
    fact(list, "Crew policy", `${p.flight_crew_duty_s / 3600} h flight duty, ${p.ground_shift_s / 3600} h ground shift, ${p.overtime_multiplier}× overtime`);
  }
  $("operational-notes").textContent = ops ? ops.assumptions.join(" ") : "Illustrative test scenario.";
  $("source-details").textContent = JSON.stringify({source: run.snapshot.source, facts: run.snapshot.facts}, null, 2);
}

function eventSummary(event) {
  const payload = event.payload || {};
  const keys = ["state", "decision", "reason", "candidate_id", "outcome", "role", "round", "turn", "status", "resource_revision", "reason_code"];
  return keys.filter((key) => payload[key] != null).map((key) => `${key}=${payload[key]}`).join(", ");
}

function renderEvidence() {
  const list = $("event-list");
  list.replaceChildren();
  for (const event of run.events || []) {
    const item = el("li");
    item.append(el("strong", event.event_type), el("span", ` ${event.actor} `, "actor"), el("span", eventSummary(event)));
    list.append(item);
  }
  const link = $("export-link");
  link.hidden = !run.run_id;
  link.href = LIVE ? `/api/runs/${encodeURIComponent(run.run_id)}/export` : `/static/fixtures/${encodeURIComponent(fixtureName)}.json`;
  link.textContent = LIVE ? "Download full trace (JSON)" : "Download this example (JSON)";
  link.setAttribute("download", `${run.run_id}.json`);
  $("evaluation").textContent = !LIVE
    ? run.provider?.mode === "LIVE"
      ? "This is one recorded historical live run. See the complete evaluation and version limits below."
      : "This scripted example demonstrates behavior. It is not a model evaluation; see the measured results below."
    : !evaluationReport || evaluationReport.status === "NOT_YET_EVALUATED"
      ? "No evaluation report is loaded."
      : `${evaluationReport.summary || "Measured report available below."} Report prompt: ${evaluationReport.provider?.prompt_version || "not recorded"}. See the results page for version limits.`;
}

const RECORDED_TRACES = {
  "H01-2c9e299c-aa88-457e-b960-a23777a533d0.json": {sample: "live-H01", reason: "ATC timed out. Ground offered options; Pilot's pending request was cancelled. No shared proposal reached a vote."},
  "H02-8ef9e2f3-7f7e-44ef-a8e1-8f74b6ef3c72.json": {sample: "live-H02", reason: "ATC timed out. Pilot and Ground offered options, but two offers could not replace three valid role messages."},
  "H03-dabd713f-7a89-4405-9b71-1cf01e5bc39e.json": {sample: "live-H03", reason: "ATC and Ground accepted. Pilot requested CREW-G1 availability evidence instead of accepting. All 15 deterministic checks passed, but an information request could not count as consent."},
  "H04-660feb8f-c0da-4df5-97c8-dc006bab9c64.json": {sample: "live-H04", reason: "Ground cited unknown evidence IDs, so its reply failed validation. This did not establish an operational gate conflict."},
  "H05-a7bf87c5-f349-42ad-9eca-06e2c7153eb7.json": {sample: "live-H05", reason: "ATC offered an option outside its permitted initial preference set. Its offer failed validation before voting."},
};
const FAILURE_NEXT_STEPS = {
  AGENT_TIMEOUT: "Investigate provider latency and response size against the declared deadline, then repeat the frozen evaluation after a fix.",
  FINAL_VOTE_DISSENT: "Resolve the requested evidence with the reviewer, then start a new run. An information request must never become an implicit acceptance.",
  UNKNOWN_AGENT_EVIDENCE: "Correct the agent's evidence references and re-evaluate validation behavior on a new run.",
  INITIAL_POLICY_VIOLATION: "Correct the offered candidate set to match the supplied role policy, then re-evaluate. Keep the original failure in the report.",
};

function renderEvaluation() {
  const box = $("evaluation-results");
  box.replaceChildren();
  const report = evaluationReport;
  if (!report?.cases?.length) {
    box.append(el("p", evaluationLoaded
      ? "No measured report is available from this server. No performance rate can be claimed."
      : "Loading the retained evaluation report…", "muted small"));
    const link = el("a", "Inspect a recorded live failure and its reason", "text-link");
    link.href = "/?sample=live-H03#step-2";
    box.append(link);
    return;
  }
  const cases = report.cases;
  const live = cases.filter((item) => item.expected === "FEASIBLE" && item.provider?.mode === "LIVE");
  const consensus = live.filter((item) => item.outcome === "FEASIBLE_CONSENSUS").length;
  const passed = cases.filter((item) => item.passed).length;
  const fixtureCount = cases.filter((item) => item.provider?.mode === "FIXTURE").length;
  const latency = live.map((item) => item.elapsed_ms).filter(Number.isFinite).sort((a, b) => a - b);
  const median = latency.length ? (latency[Math.floor((latency.length - 1) / 2)] + latency[Math.floor(latency.length / 2)]) / 2000 : null;
  const calls = cases.reduce((sum, item) => sum + item.model_calls, 0);
  const measured = el("dl", null, "guide-metrics");
  for (const [label, value, note] of [
    ["Live runs with agreement", live.length ? `${consensus} / ${live.length}` : "Not evaluated", `${live.length - consensus} did not reach agreement`],
    ["Tests passed", `${passed} / ${cases.length}`, `Includes ${fixtureCount} scripted tests; not AI accuracy`],
    ["Median live run", median == null ? "Not measured" : `${minutes(median)} s`, `${calls} real requests across this report`],
  ]) {
    const cell = el("div");
    cell.append(el("dt", label), el("dd", value), el("p", note));
    measured.append(cell);
  }
  box.append(el("p", `${new Date(report.created_at).toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"})} · ${report.provider?.prompt_version} · ${live.length} live runs + ${fixtureCount} scripted tests`, "small muted"), measured);
  const comparable = live.filter((item) => Object.keys(item.comparison?.vs_B0 || {}).length);
  const unchanged = comparable.filter((item) => Object.values(item.comparison.vs_B0).every((value) => Number(value) === 0));
  box.append(el("p", comparable.length
    ? `${unchanged.length} of ${comparable.length} live runs matched the earliest feasible plan (B0) on every measured impact. ${unchanged.length === comparable.length ? "No improvement was shown." : "See each comparison below."}`
    : "No live comparison was recorded. No improvement can be claimed.", "evaluation-finding"));
  const version = run.provider?.prompt_version;
  box.append(el("p", "Small historical test with simulated airport, fuel and crew data. It does not evaluate later versions or new inputs.", "small muted"));

  const failure = live.find((item) => item.fallback_reason === "FINAL_VOTE_DISSENT" && RECORDED_TRACES[item.trace])
    || live.find((item) => item.fallback_reason && RECORDED_TRACES[item.trace]);
  if (failure) {
    const example = el("aside", null, "evaluation-failure");
    example.append(el("h3", `Where agreement failed · ${failure.case_id}`),
      el("p", RECORDED_TRACES[failure.trace].reason));
    const link = el("a", "See why each agent agreed or disagreed", "text-link");
    link.href = `/?sample=${RECORDED_TRACES[failure.trace].sample}#step-2`;
    example.append(link);
    box.append(example);
  }
  const details = el("details", null, "evaluation-cases");
  details.id = "evaluation-cases";
  details.append(el("summary", `All ${cases.length} tests, methods and limits`));
  details.append(el("p", `Frozen ${report.split} · ${report.provider?.model} · ${report.provider?.prompt_version}. ${version && version !== report.provider?.prompt_version ? `This run uses ${version}. ` : ""}These results do not evaluate new fuel edits or a changed prompt. The small suite reuses scenario templates and does not establish performance across real airports.`, "small muted"));
  if (failure) details.append(el("p", `Next step for ${failure.case_id}: ${FAILURE_NEXT_STEPS[failure.fallback_reason] || "Inspect the recorded cause and correct it before a new evaluation."}`, "small muted"));
  details.append(el("p", "B0 = earliest feasible; B1 = minimum highest role cost, then total cost. Both search the same full catalog under identical source/rule versions. Deltas are selected plan minus B0; positive holding/fuel/overtime means more, negative means less. A missing comparison stays unavailable.", "small muted"));
  const wrap = el("div", null, "table-wrap");
  wrap.tabIndex = 0;
  wrap.setAttribute("role", "region");
  wrap.setAttribute("aria-label", "Evaluation cases; scroll horizontally for all measures");
  const table = el("table");
  const thead = el("thead");
  const headings = el("tr");
  for (const title of ["Case / input", "Mode", "Outcome / reason", "Contract", "Time", "Δ hold", "Δ fuel", "Δ overtime"]) {
    const cell = el("th", title);
    cell.scope = "col";
    headings.append(cell);
  }
  thead.append(headings);
  const body = el("tbody");
  const delta = (value, unit, divisor = 1) => value == null ? "Unavailable" : `${Number(value) > 0 ? "+" : ""}${minutes(Number(value) / divisor)} ${unit}`;
  for (const item of cases) {
    const row = el("tr");
    const name = el("th", `${item.case_id} · ${item.variant.replaceAll("_", " ")}`);
    name.scope = "row";
    const outcome = ({FEASIBLE_CONSENSUS: "Consensus", FEASIBLE_FALLBACK: "Fallback",
      INSUFFICIENT_DATA: "Missing data", NO_FEASIBLE_OPTION_IN_SEARCH_SPACE: "No feasible plan"})[item.outcome] || item.outcome;
    const result = el("td", item.stale_approval_blocked ? `${outcome}; stale approval blocked` : outcome);
    if (item.fallback_reason) result.append(el("span", item.fallback_reason, "case-reason"));
    if (RECORDED_TRACES[item.trace]) {
      const link = el("a", "Inspect recording", "text-link");
      link.href = `/?sample=${RECORDED_TRACES[item.trace].sample}#step-2`;
      result.append(link);
    }
    const d = item.comparison?.vs_B0;
    row.append(name, el("td", item.provider.mode === "LIVE" ? "Live model" : "Fixture"), result,
      el("td", item.passed ? "Passed" : "Failed"), el("td", item.elapsed_ms == null ? "Unavailable" : `${minutes(item.elapsed_ms / 1000)} s`),
      el("td", delta(d?.additional_landing_delay_s, "min", 60)), el("td", delta(d?.modeled_fuel_used_kg, "kg")), el("td", delta(d?.crew_overtime_min, "min")));
    body.append(row);
  }
  table.append(thead, body);
  wrap.append(table);
  details.append(wrap);
  for (const limitation of report.limitations || []) details.append(el("p", limitation, "small muted"));
  box.append(details);
}

async function post(path, body) {
  const fingerprint = path + JSON.stringify(body);
  if (pendingKey?.fingerprint !== fingerprint) pendingKey = {fingerprint, key: crypto.randomUUID()};
  const response = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json",
      ...(!publicDemo ? {"Authorization": "Bearer " + $("session-token").value.trim()} : {}),
      "Idempotency-Key": pendingKey.key},
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (response.status < 500) pendingKey = null;
  if (!response.ok) {
    const code = data.error?.code;
    throw new Error(code ? `${ERRORS[code] || "Request refused."} (${code})` : `Request failed (${response.status})`);
  }
  return data;
}

async function operate(action) {
  if (busy) return;
  busy = true;
  clearTimeout(pollTimer);
  if (loadPromise) await loadPromise.catch(() => {});
  clearTimeout(pollTimer);
  reviewResult = null;
  render();
  try {
    await action();
  } catch (error) {
    reviewResult = {status: "Action blocked", message: error.message, http_status: 409};
  } finally {
    busy = false;
    await load().catch(showError);
  }
}

async function decide(decision) {
  const body = {
    decision, plan_hash: run.plan_hash, scenario_revision: run.scenario_revision,
    resource_revision: run.resource_revision, ruleset_version: run.ruleset_version,
    policy_version: run.policy_version, expires_at: run.expires_at,
    reason: $("reason-input").value.trim(),
    acknowledge_fallback: $("ack-box").checked,
  };
  await post(`/api/runs/${encodeURIComponent(run.run_id)}/decision`, body);
}

async function startRun(refresh = false) {
  const id = $("scenario-select").value;
  const response = await fetch(`/api/scenarios/${encodeURIComponent(id)}`);
  if (!response.ok) throw new Error("Scenario is unavailable");
  let snapshot = await response.json();
  const fuel = snapshot.facts.find((item) => item.fact_id === "overlay.fuel");
  let fuelChanged = false;
  if (fuel?.value) {
    const value = $("fuel-unknown").checked ? null : Number($("fuel-input").value);
    if (value !== null && ($("fuel-input").value.trim() === "" || !Number.isFinite(value) || value < 0 || value > 1000000)) {
      throw new Error("Enter a fuel quantity from 0 to 1,000,000 kg, or mark it unknown.");
    }
    fuelChanged = value !== fuel.value.projected_fuel_at_earliest_kg;
    if (fuelChanged) {
      fuel.value = {...fuel.value, projected_fuel_at_earliest_kg: value};
      fuel.provenance = "synthetic_scenario";
      fuel.source_reference = "browser-input: fuel quantity at earliest landing; explicitly simulated";
      fuel.source_version = `browser-fuel-revision-${snapshot.revision + 1}`;
    }
  }
  const resourceResponse = await fetch(`/api/resources/${encodeURIComponent(snapshot.resource_id)}`);
  if (!resourceResponse.ok) throw new Error("Current resources are unavailable");
  const resource = await resourceResponse.json();
  if (refresh || fuelChanged || resource.revision !== snapshot.resource_revision) {
    snapshot = await post(`/api/scenarios/${encodeURIComponent(id)}/revisions`,
      {...snapshot, revision: snapshot.revision + 1, resource_revision: resource.revision + 1});
  }
  scenarioList = scenarioList.map((item) => item.scenario_id === id ? snapshot : item);
  const created = await post("/api/runs", {
    scenario_id: id, scenario_revision: snapshot.revision,
    parent_run_id: run?.snapshot.resource_id === snapshot.resource_id ? run.run_id : null,
  });
  runParam = created.run_id;
  run = view(created);
  lastSeq = 0;
  $("ack-box").checked = false;
  $("reason-input").value = "";
  history.replaceState(null, "", `?run=${encodeURIComponent(runParam)}`);
  render();
}

function showError(error) {
  $("run-visual").dataset.active = "false";
  $("run-visual").dataset.tone = "warn";
  $("motion-toggle").hidden = true;
  setMotionText("hub-label", "Plan");
  setMotionText("hub-state", "Unconfirmed");
  setMotionText("motion-stage", "Connection lost · reload to check this run");
  setMotionText("motion-count", "");
  motionWasActive = false;
  for (const role of ROLES) {
    if ($(`signal-${role}`).dataset.state === "waiting") {
      $(`signal-${role}`).dataset.state = "idle";
      $(`signal-path-${role}`).dataset.state = "idle";
      setMotionText(`signal-status-${role}`, "Unconfirmed");
    }
  }
  const state = $("review-state");
  delete state.dataset.state;
  state.className = "banner bad";
  state.replaceChildren(el("strong", "Could not load the data"), el("span", error.message));
  clearTimeout(pollTimer);
  for (const id of ["start-btn", "cancel-btn", "approve-btn", "reject-btn", "rerun-btn"]) $(id).disabled = true;
}

$("motion-toggle").addEventListener("click", () => {
  motionPaused = !motionPaused;
  syncMotionPreference();
});
reducedMotion.addEventListener("change", syncMotionPreference);
document.addEventListener("visibilitychange", syncMotionPreference);

$("fixture-select").addEventListener("change", (event) => {
  const query = event.target.value ? `?sample=${encodeURIComponent(event.target.value)}` : "";
  location.assign(`${location.pathname}${query}#workspace`);
});
$("scenario-select").addEventListener("change", async () => {
  if (loadPromise) await loadPromise.catch(() => {});
  clearTimeout(pollTimer);
  runParam = null;
  run = null;
  lastSeq = 0;
  reviewResult = null;
  history.replaceState(null, "", location.pathname);
  load().catch(showError);
});
$("session-token").addEventListener("input", () => { if (run) render(); });
$("fuel-input").addEventListener("input", renderControls);
$("fuel-unknown").addEventListener("change", renderControls);
$("run-inputs-btn").addEventListener("click", () => operate(() => startRun(true)));
$("ack-box").addEventListener("change", () => renderControls());
$("reason-input").addEventListener("input", () => renderControls());
$("approve-btn").addEventListener("click", () => operate(() => decide("APPROVE")));
$("reject-btn").addEventListener("click", () => {
  if (!$("reason-input").value.trim()) {
    $("review-hint").textContent = "Write a short reason before rejecting.";
    $("reason-input").focus();
    return;
  }
  operate(() => decide("REJECT"));
});
$("cancel-btn").addEventListener("click", () => operate(async () => {
  await post(`/api/runs/${encodeURIComponent(run.run_id)}/cancel`, {});
}));
$("start-btn").addEventListener("click", () => operate(() => startRun()));
$("rerun-btn").addEventListener("click", () => operate(() => startRun(true)));

async function init() {
  const reportPromise = fetch("/api/evaluations/latest", {signal: AbortSignal.timeout(8000)})
    .then((response) => response.ok ? response.json() : null).catch(() => null);
  if (LIVE) {
    const sessionResponse = await fetch("/api/session", {credentials: "same-origin"});
    if (!sessionResponse.ok) throw new Error("Could not open a judge session. Please try again shortly.");
    publicDemo = (await sessionResponse.json()).public_demo;
    const [healthResponse, scenariosResponse] = await Promise.all([fetch("/health"), fetch("/api/scenarios")]);
    if (!healthResponse.ok || !scenariosResponse.ok) throw new Error("Backend is unavailable");
    const health = await healthResponse.json();
    sessionConfigured = health.session_configured;
    scenarioList = await scenariosResponse.json();
    for (const snapshot of scenarioList) {
      const f = snapshot.source.original_fields;
      const option = el("option", `${f.AIRLINE}${f.FLIGHT_NUMBER} ${f.ORIGIN_AIRPORT}→${f.DESTINATION_AIRPORT} · ${snapshot.scenario_id.split(":").at(-1)}`);
      option.value = snapshot.scenario_id;
      $("scenario-select").append(option);
    }
  }
  await load();
  if (runParam) $("scenario-select").value = run.snapshot.scenario_id;
  evaluationReport = await reportPromise;
  evaluationLoaded = true;
  renderEvaluation();
  renderEvidence();
  // Judge-guide links open the relevant evidence after the run has rendered.
  revealAnchor();
}

function revealAnchor() {
  const target = $(location.hash.slice(1));
  if (target) {
    for (let node = target; node; node = node.parentElement) {
      if (node.tagName === "DETAILS") node.open = true;
    }
    target.scrollIntoView({block: "start"});
  }
}
window.addEventListener("hashchange", revealAnchor);
init().catch(showError);
