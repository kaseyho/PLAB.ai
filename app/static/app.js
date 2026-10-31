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
    const response = await fetch(`/api/runs/${encodeURIComponent(runParam)}`, { credentials: "same-origin" });
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
      `/api/runs/${encodeURIComponent(runParam)}/events?after_seq=${lastSeq}`, { credentials: "same-origin" });
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
  PROVIDER_NOT_CONFIGURED: "the AI service is not connected",
  PROVIDER_TIMEOUT: "a desk did not reply in time",
  NO_COMMON_OFFER: "no option was acceptable to all three desks",
  FINAL_DISSENT: "a desk rejected the final proposal",
  PROTOCOL_ERROR: "a desk sent a reply that broke the rules",
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
  renderNegotiation();
  renderReview();
  renderControls();
  renderRunFacts();
  renderCandidates();
  renderChecks();
  renderScenario();
  renderEvidence();
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
  $("mode-note").hidden = LIVE && !fixture;
  $("mode-note").textContent = !LIVE
    ? "Saved example · scripted agent messages with real rule checks. Choose “Interactive demo” to start your own run."
    : "Offline demo · scripted agent messages. Rule checks and human approval are active.";
  $("fixture-select").value = fixtureName || "";
  $("start-row").hidden = !LIVE;
  $("session-hint").hidden = !LIVE;
  $("session-row").hidden = publicDemo;
  $("scenario-select").disabled = busy;
  if (LIVE) $("session-hint").textContent = publicDemo
    ? "No API key needed. Your simulation is separate from other visitors."
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

function renderNegotiation() {
  const status = $("negotiation-status");
  const n = run.round_count;
  const outcome = run.negotiation_outcome;
  if (!run.run_id) status.textContent = "Start a negotiation to compare the three agents’ proposals.";
  else if (run.processing_state !== "TERMINAL") status.textContent = n ? `Turn ${n} of 3 · waiting for the agents’ replies…` : "Checking the available plans…";
  else if (outcome === "FEASIBLE_CONSENSUS") status.textContent = `All three agents agreed on the same plan in ${n} ${n === 1 ? "turn" : "turns"}.`;
  else if (outcome === "FEASIBLE_FALLBACK") {
    const why = FALLBACK_REASONS[run.fallback_reason] || run.fallback_reason?.toLowerCase().replaceAll("_", " ");
    status.textContent = `No agreement${why ? `: ${why}` : ""}. A checked fallback is ready below.`;
  } else if (outcome === "CANCELLED") status.textContent = "The negotiation was stopped.";
  else status.textContent = run.agent_invocation_count ? "The run ended. See the outcome below." : "No agent calls were needed. See the outcome below.";

  const box = $("agent-cards");
  box.replaceChildren();
  for (const role of ROLES) {
    const card = el("article", null, "desk");
    card.append(el("h3", ROLE_NAMES[role]), el("p", ROLE_NOTES[role], "role-note"));
    const offer = (run.offers || {})[role];
    const previous = run.previous_votes?.[role];
    const vote = (run.votes || {})[role];
    const requested = run.events.some((event) => event.event_type === "AGENT_REQUEST_STARTED" && event.payload?.role === role);
    const accepted = vote?.decision === "ACCEPT" && vote.plan_hash === run.plan_hash;
    const waiting = run.run_id && run.processing_state !== "TERMINAL";
    let label = run.run_id ? "Not run" : "Ready";
    let tone = "";
    if (vote) {
      label = accepted ? "Agreed" : vote.decision === "REJECT" ? "Declined" : "Needs review";
      tone = accepted ? "accepted" : "attention";
    } else if (offer) label = "Offer received";
    else if (waiting) label = "Waiting for reply";
    else if (requested) {
      label = "No valid reply";
      tone = "attention";
    }
    card.append(el("p", label, `desk-status ${tone}`));
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
    if (previous) messages.append(voteBlock(2, previous, run.previous_candidate_id, false));
    if (vote) messages.append(voteBlock(run.round_count, vote, run.candidate?.candidate_id, vote.plan_hash !== run.plan_hash));
    if (offer || previous || vote) card.append(messages);
    box.append(card);
  }
}

function voteBlock(turnNumber, vote, candidateId, mismatch) {
  const turn = el("div", null, "turn");
  const words = { ACCEPT: "Agreed", REJECT: "Rejected", NEEDS_INFORMATION: "Asked for more information" };
  const yes = vote.decision === "ACCEPT" && !mismatch;
  turn.append(el("div", `Turn ${turnNumber} · vote on ${describe(candidateId)}`, "turn-label"),
    el("p", `${words[vote.decision] || vote.decision}${mismatch ? " (but on a different version of the plan)" : ""}`, `vote ${yes ? "yes" : "no"}`));
  if (vote.rationale) turn.append(el("p", vote.rationale, "why"));
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
  if (outcome === "FEASIBLE_FALLBACK") return ["warn", "Fallback plan · review required", "Fixed rules selected the earliest plan that passes the checks. The agents did not reach agreement."];
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

  const result = $("review-result");
  if (run.review_result) {
    result.hidden = false;
    result.className = `result ${run.review_result.http_status === 200 ? "ok" : "bad"}`;
    result.textContent = run.review_result.message;
  } else {
    result.hidden = true;
  }
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
  $("start-btn").disabled = !authorized || busy || Boolean(running);
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
  $("rerun-btn").disabled = !authorized || busy;
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
  const keys = ["state", "decision", "reason", "candidate_id", "outcome", "role", "round", "resource_revision", "reason_code"];
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
    ? "This scripted example demonstrates behavior. It is not a model evaluation; see the measured results below."
    : !evaluationReport || evaluationReport.status === "NOT_YET_EVALUATED"
      ? "No evaluation report is loaded."
      : `${evaluationReport.summary || "Measured report available below."} Report prompt: ${evaluationReport.provider?.prompt_version || "not recorded"}. See the results page for version limits.`;
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
  const resourceResponse = await fetch(`/api/resources/${encodeURIComponent(snapshot.resource_id)}`);
  if (!resourceResponse.ok) throw new Error("Current resources are unavailable");
  const resource = await resourceResponse.json();
  if (refresh || resource.revision !== snapshot.resource_revision) {
    snapshot = await post(`/api/scenarios/${encodeURIComponent(id)}/revisions`,
      {...snapshot, revision: snapshot.revision + 1, resource_revision: resource.revision + 1});
  }
  const created = await post("/api/runs", {
    scenario_id: id, scenario_revision: snapshot.revision,
    parent_run_id: run?.snapshot.resource_id === snapshot.resource_id ? run.run_id : null,
  });
  runParam = created.run_id;
  run = null;
  lastSeq = 0;
  $("ack-box").checked = false;
  $("reason-input").value = "";
  history.replaceState(null, "", `?run=${encodeURIComponent(runParam)}`);
}

function showError(error) {
  const state = $("review-state");
  delete state.dataset.state;
  state.className = "banner bad";
  state.replaceChildren(el("strong", "Could not load the data"), el("span", error.message));
  clearTimeout(pollTimer);
  for (const id of ["start-btn", "cancel-btn", "approve-btn", "reject-btn", "rerun-btn"]) $(id).disabled = true;
}

$("fixture-select").addEventListener("change", (event) => {
  location.search = event.target.value ? `?sample=${encodeURIComponent(event.target.value)}` : "";
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
    const reportResponse = await fetch("/api/evaluations/latest");
    if (reportResponse.ok) evaluationReport = await reportResponse.json();
  }
  await load();
  if (runParam) $("scenario-select").value = run.snapshot.scenario_id;
  // Judge-guide links open the relevant evidence after the run has rendered.
  const target = $(location.hash.slice(1));
  if (target) {
    for (let node = target; node; node = node.parentElement) {
      if (node.tagName === "DETAILS") node.open = true;
    }
    target.scrollIntoView({block: "start"});
  }
}
init().catch(showError);
