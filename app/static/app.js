"use strict";

// Duty manager dashboard. Two data sources:
//   ?fixture=<name>  labelled sample payloads in fixtures/ (no backend needed)
//   ?run=<run_id>    live run from Developer 2's API, polled once per second
// All text from payloads (including model rationale) is set with textContent, never HTML.

const params = new URLSearchParams(location.search);
const runParam = params.get("run");
const fixtureName = params.get("fixture") || (runParam ? null : "consensus");
const LIVE = Boolean(runParam);
const TERMINAL_APPROVAL = ["APPLIED_SIMULATION", "REJECTED", "STALE", "EXPIRED", "CANCELLED"];
const ROLES = ["ATC", "PILOT", "GROUND"];
const ICON = { PASS: "✓ PASS", FAIL: "✗ FAIL", UNKNOWN: "? UNKNOWN" };

let run = null;
let lastSeq = 0;
let pollTimer = null;
let pendingKey = null; // reused if the same decision is retried, so the backend applies it once

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

async function load() {
  if (LIVE) {
    const response = await fetch(`/api/runs/${encodeURIComponent(runParam)}`, { credentials: "same-origin" });
    if (!response.ok) throw new Error(`Run request failed (${response.status})`);
    const fresh = await response.json();
    const events = await fetch(
      `/api/runs/${encodeURIComponent(runParam)}/events?after_seq=${lastSeq}`, { credentials: "same-origin" });
    const newEvents = events.ok ? await events.json() : [];
    fresh.events = [...(run ? run.events : []), ...newEvents];
    if (newEvents.length) lastSeq = newEvents[newEvents.length - 1].seq;
    run = fresh;
  } else {
    const response = await fetch(`fixtures/${encodeURIComponent(fixtureName)}.json`);
    if (!response.ok) throw new Error(`Sample ${fixtureName} not found`);
    run = await response.json();
  }
  render();
  const keepPolling = LIVE && (run.processing_state !== "TERMINAL" || run.approval_state === "PENDING");
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
  badge.textContent = LIVE ? `Live run ${run.run_id}` : "Sample data";
  $("mode-note").hidden = LIVE;
  if (!LIVE) $("mode-note").textContent = run.mode_note || "Sample payload, not a live run.";
  $("fixture-picker").hidden = LIVE;
  if (!LIVE) $("fixture-select").value = fixtureName;
}

function renderScenario() {
  const list = $("scenario-facts");
  list.replaceChildren();
  const source = run.source || {};
  const fields = source.fields || {};
  fact(list, "Historical flight", source.natural_identity, "historical");
  fact(list, "Qualifies by", source.qualifies_by, "historical");
  fact(list, "Source row", source.record_ordinal ? `${source.dataset_file} record ${source.record_ordinal}` : null);
  fact(list, "Scheduled", fields.SCHEDULED_DEPARTURE ? `dep ${fields.SCHEDULED_DEPARTURE}, arr ${fields.SCHEDULED_ARRIVAL} (local, as recorded)` : null, "historical");
  fact(list, "Scenario", run.scenario ? `${run.scenario.scenario_id} rev ${run.scenario_revision}` : null, "synthetic");
  fact(list, "Simulation epoch", run.scenario && run.scenario.simulation_epoch_utc, "synthetic");
  fact(list, "Rules / policy", `${run.ruleset_version} / ${run.policy_version}`, "illustrative");
  fact(list, "Resource revision", run.resource_revision);
}

function renderRun() {
  const list = $("run-facts");
  list.replaceChildren();
  fact(list, "Processing", run.processing_state);
  fact(list, "Outcome", run.negotiation_outcome);
  fact(list, "Rounds", `${run.round_count} of 2`);
  fact(list, "Model calls", `${run.model_call_count} of 6`);
  fact(list, "Approval", run.approval_state);
  if (run.fallback_reason) fact(list, "Fallback reason", run.fallback_reason);
  const running = LIVE && run.processing_state !== "TERMINAL";
  $("start-btn").disabled = !LIVE;
  $("cancel-btn").disabled = !(running || (LIVE && run.approval_state === "PENDING"));
  $("start-btn").title = LIVE ? "" : "Needs the backend";
}

function reviewState() {
  const outcome = run.negotiation_outcome;
  const approval = run.approval_state;
  if (approval === "APPLIED_SIMULATION") return ["state-ok", "Applied to simulation", `Resource revision is now ${run.resource_revision_after ?? "updated"}.`];
  if (approval === "REJECTED") return ["state-info", "Rejected", "The simulated schedule was not changed."];
  if (approval === "CANCELLED") return ["state-info", "Cancelled", "The simulated schedule was not changed."];
  if (approval === "STALE") return ["state-bad", "Stale: approval blocked", "Airport resources changed after this plan was made. Start a new run."];
  if (approval === "EXPIRED") return ["state-bad", "Expired: approval blocked", "The recommendation was not approved in time. Start a new run."];
  if (outcome === "FEASIBLE_CONSENSUS") return ["state-ok", "Consensus awaiting review", "All three agents accepted the plan shown below. Their agreement does not apply it; you decide."];
  if (outcome === "FEASIBLE_FALLBACK") return ["state-warn", "Fallback awaiting review", "The agents did not agree. This plan comes from the deterministic fallback (earliest feasible plan), with no AI involved."];
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
  const canAct = reviewable && LIVE;
  $("approve-btn").disabled = !canAct || (fallback && !$("ack-box").checked);
  $("reject-btn").disabled = !canAct;
  $("reason-input").disabled = !canAct;
  $("rerun-btn").disabled = !LIVE;
  $("review-hint").textContent = !reviewable
    ? (TERMINAL_APPROVAL.includes(run.approval_state) ? "This recommendation is closed. Start a new run to decide again." : "")
    : LIVE ? "The server re-checks every hard rule against current airport state before applying."
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
      card.append(el("p", "Round 2 vote", "label"));
      card.append(el("p", vote.decision, `status status-${vote.decision === "ACCEPT" ? "PASS" : "FAIL"}`));
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
    ];
    for (const text of cells) row.append(el("td", text));
    const status = c.feasible ? "PASS" : "FAIL";
    row.append(el("td", c.feasible ? "✓ all pass" : "✗ blocked", `status status-${status}`));
    body.append(row);
  }
  $("cand-note").textContent = (run.candidates || []).length
    ? "Shortlist only. Fallback and baselines search the full catalog." : "No candidates to compare.";
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

function renderEvidence() {
  const list = $("event-list");
  list.replaceChildren();
  for (const event of run.events || []) {
    const item = el("li");
    item.append(el("strong", event.type), el("span", ` ${event.actor} `, "actor"), el("span", event.summary || ""));
    list.append(item);
  }
  const link = $("export-link");
  link.href = LIVE ? `/api/runs/${encodeURIComponent(run.run_id)}/export` : `fixtures/${encodeURIComponent(fixtureName)}.json`;
  link.setAttribute("download", `${run.run_id}.json`);
  $("evaluation").textContent = run.evaluation ? run.evaluation.summary : "Not yet evaluated";
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  return { ok: response.ok, status: response.status, data };
}

async function decide(decision) {
  const plan = run.candidate;
  pendingKey = pendingKey && pendingKey.decision === decision ? pendingKey : { decision, key: crypto.randomUUID() };
  const body = {
    run_id: run.run_id,
    decision,
    idempotency_key: pendingKey.key,
    expected: {
      plan_hash: plan.plan_hash,
      scenario_revision: plan.scenario_revision,
      resource_revision: plan.resource_revision,
      ruleset_version: plan.ruleset_version,
      policy_version: plan.policy_version,
    },
    reason: $("reason-input").value.trim() || null,
    acknowledge_no_consensus: $("ack-box").checked,
  };
  $("approve-btn").disabled = true;
  $("reject-btn").disabled = true;
  const { data, status } = await post(`/api/runs/${encodeURIComponent(run.run_id)}/decision`, body);
  run.review_result = { status: data.status || `HTTP ${status}`, message: data.message || "", http_status: status, failed_rules: data.failed_rules || [] };
  pendingKey = null;
  await load();
}

function showError(error) {
  const state = $("review-state");
  state.className = "state state-bad";
  state.replaceChildren(el("span", "Could not load run data"), el("small", error.message));
}

$("fixture-select").addEventListener("change", (event) => {
  location.search = `?fixture=${encodeURIComponent(event.target.value)}`;
});
$("ack-box").addEventListener("change", () => renderReview());
$("approve-btn").addEventListener("click", () => decide("APPROVE").catch(showError));
$("reject-btn").addEventListener("click", () => {
  if (!$("reason-input").value.trim()) {
    $("review-hint").textContent = "Enter a short reason before rejecting.";
    $("reason-input").focus();
    return;
  }
  decide("REJECT").catch(showError);
});
$("cancel-btn").addEventListener("click", async () => {
  await post(`/api/runs/${encodeURIComponent(run.run_id)}/cancel`, {});
  await load().catch(showError);
});
$("start-btn").addEventListener("click", async () => {
  const { ok, data } = await post("/api/runs", {
    scenario_id: run.scenario.scenario_id, scenario_revision: run.scenario_revision, idempotency_key: crypto.randomUUID(),
  });
  if (ok && data.run_id) location.search = `?run=${encodeURIComponent(data.run_id)}`;
});
$("rerun-btn").addEventListener("click", () => $("start-btn").click());

load().catch(showError);
