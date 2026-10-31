"use strict";

// Duty manager dashboard. Two data sources:
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

function clock(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const m = Math.floor(seconds / 60);
  const s = String(seconds % 60).padStart(2, "0");
  return `T+${m}:${s} (${seconds} s)`;
}

const num = (value, unit) => (value === null || value === undefined ? "—" : `${value} ${unit}`);

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
    if (!snapshot) throw new Error("No scenarios are loaded. Check the dataset path in /health.");
    run = emptyRun(snapshot);
  }
  render();
  const keepPolling = LIVE && runParam && (run.processing_state !== "TERMINAL" || run.approval_state === "PENDING");
  clearTimeout(pollTimer);
  if (keepPolling) pollTimer = setTimeout(() => load().catch(showError), 1000);
}

function render() {
  renderMode();
  renderScenario();
  renderRun();
  renderReview();
  renderAgents();
  renderCandidates();
  renderChecks();
  renderEvidence();
}

function renderMode() {
  const badge = $("mode-badge");
  badge.textContent = !LIVE ? "Sample data" : !run.run_id ? "Ready" : `${run.provider.mode} provider · ${run.planning_mode} planner`;
  $("mode-note").hidden = LIVE && run.provider?.mode !== "FIXTURE";
  $("mode-note").textContent = run.mode_note || "Deterministic fixture agents; no live model calls.";
  $("fixture-picker").hidden = false;
  $("fixture-select").value = fixtureName || "";
  $("scenario-picker").hidden = !LIVE;
  $("scenario-select").disabled = busy;
  $("session-row").hidden = !LIVE;
  $("session-hint").hidden = !LIVE;
  if (LIVE) $("session-hint").textContent = sessionConfigured
    ? "Held in this tab only. Required to start, cancel, or review a run."
    : "Configure PLAB_SESSION_TOKEN locally and restart the service to enable actions.";
}

function renderScenario() {
  const list = $("scenario-facts");
  list.replaceChildren();
  const source = run.source || {};
  const fields = source.fields || {};
  fact(list, "Historical flight", source.natural_identity, "historical");
  fact(list, "Qualifies by", source.qualifies_by, "historical");
  fact(list, "Source row", source.record_ordinal ? `${source.dataset_file} record ${source.record_ordinal}` : null);
  fact(list, "Dataset hash", `${run.snapshot.source.dataset_sha256.slice(0, 12)}…`);
  fact(list, "Scheduled", fields.SCHEDULED_DEPARTURE ? `dep ${fields.SCHEDULED_DEPARTURE}, arr ${fields.SCHEDULED_ARRIVAL} (local, as recorded)` : null, "historical");
  fact(list, "Scenario", run.scenario ? `${run.scenario.scenario_id} rev ${run.scenario_revision}` : null, "synthetic");
  fact(list, "Simulation epoch", run.scenario && run.scenario.simulation_epoch_utc, "synthetic");
  fact(list, "Rules / policy", `${run.ruleset_version} / ${run.policy_version}`, "illustrative");
  fact(list, "Resource revision", run.resource_revision);
  $("source-details").textContent = JSON.stringify({source: run.snapshot.source, facts: run.snapshot.facts}, null, 2);
}

function renderRun() {
  const list = $("run-facts");
  list.replaceChildren();
  fact(list, "Processing", run.processing_state);
  fact(list, "Outcome", run.negotiation_outcome);
  fact(list, "Rounds", `${run.round_count} of 2`);
  fact(list, "Model calls", `${run.model_call_count} of 6`);
  fact(list, "Agent invocations", run.agent_invocation_count);
  if (run.provider) fact(list, "Provider", `${run.provider.mode} · ${run.provider.model}`, run.provider.prompt_version);
  if (run.planning_mode) fact(list, "Planner", run.planning_mode);
  if (run.elapsed_ms != null) fact(list, "Elapsed", `${run.elapsed_ms} ms`);
  fact(list, "Approval", run.approval_state);
  if (run.fallback_reason) fact(list, "Fallback reason", run.fallback_reason);
  if (run.reason_code) fact(list, "Reason code", run.reason_code);
  const running = LIVE && run.run_id && run.processing_state !== "TERMINAL";
  const authorized = LIVE && sessionConfigured && $("session-token").value.trim();
  $("start-btn").disabled = !authorized || busy || Boolean(running);
  $("cancel-btn").disabled = !authorized || busy || !(running || (LIVE && run.approval_state === "PENDING"));
  $("start-btn").title = LIVE ? "" : "Needs the backend";
}

function reviewState() {
  const outcome = run.negotiation_outcome;
  const approval = run.approval_state;
  if (!run.run_id) return ["state-info", "Ready to coordinate", "Select a scenario and start a run."];
  if (approval === "APPLIED_SIMULATION") return ["state-ok", "Applied to simulation", `Resource revision is now ${run.resource_revision_after ?? "updated"}.`];
  if (approval === "REJECTED") return ["state-info", "Rejected", "The simulated schedule was not changed."];
  if (approval === "CANCELLED") return ["state-info", "Cancelled", "The simulated schedule was not changed."];
  if (approval === "STALE") return ["state-bad", "Stale: approval blocked", "Airport resources changed after this plan was made. Start a new run."];
  if (approval === "EXPIRED") return ["state-bad", "Expired: approval blocked", "The recommendation was not approved in time. Start a new run."];
  if (outcome === "FEASIBLE_CONSENSUS") return ["state-ok", "Consensus awaiting review", "All three agents accepted the plan shown below. Their agreement does not apply it; you decide."];
  if (outcome === "FEASIBLE_FALLBACK") return ["state-warn", "Fallback awaiting review", "No agent consensus was obtained. Deterministic rules selected the earliest feasible plan."];
  if (outcome === "NO_FEASIBLE_OPTION_IN_SEARCH_SPACE") return ["state-bad", "No feasible option in the searched window", "Every candidate failed at least one hard check. Needs qualified review outside the simulator."];
  if (outcome === "INSUFFICIENT_DATA") return ["state-bad", "Input review required", "Required facts are missing, so no plan can be checked. Nothing is guessed."];
  if (outcome === "SYSTEM_ERROR") return ["state-bad", "System error", "Checks could not be completed reliably. No plan can be approved."];
  if (run.processing_state && run.processing_state !== "TERMINAL") return ["state-info", "Negotiating", `Round ${run.round_count} of 2.`];
  return ["state-info", outcome || "Waiting", ""];
}

function renderReview() {
  const [cls, title, detail] = reviewState();
  const state = $("review-state");
  state.className = `state ${cls}`;
  state.replaceChildren(el("span", title), el("small", detail));

  const list = $("plan-facts");
  list.replaceChildren();
  const plan = run.candidate;
  if (plan) {
    fact(list, "Plan", plan.candidate_id);
    fact(list, "Landing", clock(plan.landing_time_s));
    fact(list, "Gate-in", clock(plan.gate_in_time_s));
    fact(list, "Gate / route", `${plan.gate_id} via ${plan.taxi_route_id}`);
    fact(list, "Handling complete", clock(plan.service_end_s));
    fact(list, "Fuel margin", num(plan.fuel_margin_kg, "kg"), "modelled");
    fact(list, "Crew overtime", num(plan.crew_overtime_min, "min"), "modelled");
    const b0 = run.baseline_b0;
    if (b0) {
      const hold = plan.added_holding_min - b0.added_holding_min;
      const overtime = plan.crew_overtime_min - b0.crew_overtime_min;
      fact(list, "vs B0 (earliest feasible)", `${signed(hold)} min hold, ${signed(overtime)} min overtime`);
    }
    fact(list, "Plan hash", plan.plan_hash ? `${plan.plan_hash.slice(0, 12)}…` : null);
    fact(list, "Review expires", run.expires_at ? new Date(run.expires_at).toLocaleString() : null);
  } else {
    if (run.missing_facts && run.missing_facts.length) fact(list, "Missing facts", run.missing_facts.join(", "));
    const blockers = Object.entries(run.blockers || {});
    if (blockers.length) fact(list, "Blocking rules (candidates)", blockers.map(([rule, n]) => `${rule}: ${n}`).join("; "));
    if (run.search_bounds) {
      const b = run.search_bounds;
      fact(list, "Searched", `${clock(b.earliest_landing_s)} to ${clock(b.latest_landing_s)}, ${b.raw_candidate_count} candidates`);
    }
  }

  const reviewable = run.approval_state === "PENDING" && plan && run.checks.length > 0
    && run.checks.every((c) => c.status === "PASS");
  const fallback = run.negotiation_outcome === "FEASIBLE_FALLBACK";
  $("ack-row").hidden = !(reviewable && fallback);
  $("review-controls").hidden = !reviewable && !TERMINAL_APPROVAL.includes(run.approval_state);
  const canAct = reviewable && LIVE && sessionConfigured && $("session-token").value.trim() && !busy;
  const hasReason = $("reason-input").value.trim().length > 0;
  $("approve-btn").disabled = !canAct || !hasReason || (fallback && !$("ack-box").checked);
  $("reject-btn").disabled = !canAct || !hasReason;
  $("reason-input").disabled = !canAct;
  $("rerun-btn").disabled = !LIVE || busy || !sessionConfigured || !$("session-token").value.trim();
  $("review-hint").textContent = !reviewable
    ? (TERMINAL_APPROVAL.includes(run.approval_state) ? "This recommendation is closed. Start a new run to decide again." : "")
    : LIVE ? "Enter a reason for your decision. The server re-checks every hard rule against current airport state before applying."
      : "Sample data: approval needs the backend. The approved and stale samples show the real approval logic's results.";

  const result = $("review-result");
  if (run.review_result) {
    result.hidden = false;
    result.className = `result ${run.review_result.http_status === 200 ? "state-ok" : "state-bad"}`;
    const rules = run.review_result.failed_rules && run.review_result.failed_rules.length
      ? ` Failed: ${run.review_result.failed_rules.join(", ")}.` : "";
    result.textContent = `${run.review_result.status}: ${run.review_result.message}${rules}`;
  } else {
    result.hidden = true;
  }
}

const signed = (value) => (value > 0 ? `+${value}` : `${value}`);

function renderAgents() {
  const box = $("agent-cards");
  box.replaceChildren();
  for (const role of ROLES) {
    const card = el("article", null, "agent");
    card.append(el("h3", role));
    const offer = (run.offers || {})[role];
    const vote = (run.votes || {})[role];
    if (!offer && !vote) card.append(el("p", "No message", "label"));
    if (offer) {
      card.append(el("p", "Round 1 offer", "label"));
      card.append(el("p", `Prefers ${offer.preferred_candidate_id ?? "none (refused)"}`));
      if (offer.concession_candidate_ids && offer.concession_candidate_ids.length) {
        card.append(el("p", `Could concede to ${offer.concession_candidate_ids.join(", ")}`));
      }
      card.append(el("p", offer.rationale));
    }
    if (vote) {
      const matches = vote.plan_hash === run.plan_hash;
      card.append(el("p", "Round 2 vote", "label"));
      card.append(el("p", `${vote.decision} (${matches ? "same" : "different"} plan hash)`,
        `status status-${vote.decision === "ACCEPT" && matches ? "PASS" : "FAIL"}`));
      card.append(el("p", vote.rationale));
    }
    box.append(card);
  }
}

function renderCandidates() {
  const body = $("candidate-table").querySelector("tbody");
  body.replaceChildren();
  const selected = run.candidate && run.candidate.candidate_id;
  for (const c of run.candidates || []) {
    const row = el("tr");
    if (c.candidate_id === selected) row.className = "selected";
    const cells = [
      c.candidate_id + (c.candidate_id === selected ? " (proposed)" : "") +
        (run.baseline_b0 && c.candidate_id === run.baseline_b0.candidate_id ? " (B0)" : ""),
      clock(c.landing_time_s), clock(c.gate_in_time_s), `${c.gate_id} / ${c.taxi_route_id}`,
      num(c.added_holding_min, "min"), num(c.fuel_margin_kg, "kg"), num(c.crew_overtime_min, "min"),
      `${Number(c.costs.ATC).toFixed(3)} / ${Number(c.costs.PILOT).toFixed(3)} / ${Number(c.costs.GROUND).toFixed(3)}`,
    ];
    for (const text of cells) row.append(el("td", text));
    const status = c.feasible ? "PASS" : "FAIL";
    row.append(el("td", c.feasible ? "✓ all pass" : "✗ blocked", `status status-${status}`));
    body.append(row);
  }
  $("cand-note").textContent = (run.candidates || []).length
    ? `Shortlist from ${run.catalog_total_count ?? run.catalog.candidates.length} checked candidates, ${run.catalog_feasible_count} feasible. B0 uses the full catalog. Costs are illustrative policy scores.` : "No candidates to compare.";
}

function renderChecks() {
  const box = $("check-list");
  box.replaceChildren();
  if (!run.checks || !run.checks.length) {
    box.append(el("p", "No plan was checked for this outcome.", "hint"));
    return;
  }
  for (const check of run.checks) {
    const item = el("details", null, "check");
    const summary = el("summary");
    summary.append(el("span", ICON[check.status], `status status-${check.status}`));
    summary.append(el("span", `${check.rule_id} · ${check.reason_code}`));
    const margin = check.margin === null || check.margin === undefined ? "" : `margin ${check.margin} ${check.units}`;
    summary.append(el("span", margin, "margin"));
    item.append(summary);
    item.append(el("pre", JSON.stringify({
      actual: check.actual, operator: check.operator, required: check.required, units: check.units,
      evidence: check.evidence_ids, rule_version: check.rule_version,
    }, null, 2)));
    box.append(item);
  }
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
    item.append(el("strong", `${event.sequence}. ${event.event_type}`), el("span", ` ${event.actor} `, "actor"),
      el("span", eventSummary(event)));
    list.append(item);
  }
  const link = $("export-link");
  link.hidden = !run.run_id;
  link.href = LIVE ? `/api/runs/${encodeURIComponent(run.run_id)}/export` : `/static/fixtures/${encodeURIComponent(fixtureName)}.json`;
  link.textContent = LIVE ? "Download trace" : "Download sample";
  link.setAttribute("download", `${run.run_id}.json`);
  $("evaluation").textContent = !LIVE || !evaluationReport || evaluationReport.status === "NOT_YET_EVALUATED"
    ? "Not yet evaluated" : evaluationReport.summary || "Measured report available at /api/evaluations/latest.";
}

async function post(path, body) {
  const fingerprint = path + JSON.stringify(body);
  if (pendingKey?.fingerprint !== fingerprint) pendingKey = {fingerprint, key: crypto.randomUUID()};
  const response = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json",
      "Authorization": "Bearer " + $("session-token").value.trim(),
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
  state.className = "state state-bad";
  state.replaceChildren(el("span", "Could not load run data"), el("small", error.message));
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
$("ack-box").addEventListener("change", () => renderReview());
$("reason-input").addEventListener("input", () => renderReview());
$("approve-btn").addEventListener("click", () => operate(() => decide("APPROVE")));
$("reject-btn").addEventListener("click", () => {
  if (!$("reason-input").value.trim()) {
    $("review-hint").textContent = "Enter a short reason before rejecting.";
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
    const [healthResponse, scenariosResponse] = await Promise.all([fetch("/health"), fetch("/api/scenarios")]);
    if (!healthResponse.ok || !scenariosResponse.ok) throw new Error("Backend is unavailable");
    const health = await healthResponse.json();
    sessionConfigured = health.session_configured;
    scenarioList = await scenariosResponse.json();
    for (const snapshot of scenarioList) {
      const option = el("option", `${snapshot.scenario_id} · revision ${snapshot.revision}`);
      option.value = snapshot.scenario_id;
      $("scenario-select").append(option);
    }
    const reportResponse = await fetch("/api/evaluations/latest");
    if (reportResponse.ok) evaluationReport = await reportResponse.json();
  }
  await load();
  if (runParam) $("scenario-select").value = run.snapshot.scenario_id;
}
init().catch(showError);
