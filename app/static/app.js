"use strict";

// Duty manager dashboard for Developer B's API (served at / by app.main).
//   /?run=<run_id>      live run, polled once per second while it can still change
//   /?sample=<name>     labelled sample captured from the real API (fixtures/)
//   /                   pick a scenario and start a run
// Payload text, including model rationale, is only ever set with textContent.

const params = new URLSearchParams(location.search);
const sampleName = params.get("sample");
const runId = params.get("run");
const LIVE = !sampleName;
const ROLES = ["ATC", "PILOT", "GROUND"];
const ICON = { PASS: "✓ PASS", FAIL: "✗ FAIL", UNKNOWN: "? UNKNOWN" };
const CLOSED = ["APPLIED_SIMULATION", "REJECTED", "STALE", "EXPIRED", "CANCELLED"];
const ERRORS = {
  REVIEWER_SESSION_REQUIRED: "Operator token missing or wrong.",
  ORIGIN_NOT_ALLOWED: "This page's address is not the configured public origin.",
  REVIEW_BINDING_MISMATCH: "The plan on screen is out of date. Reload before deciding.",
  RECOMMENDATION_EXPIRED: "The recommendation expired. Run again.",
  REVIEW_NOT_PENDING: "This recommendation is already closed.",
  FALLBACK_ACKNOWLEDGEMENT_REQUIRED: "Tick the no-consensus acknowledgement first.",
  IDEMPOTENCY_CONFLICT: "This decision conflicts with one already recorded.",
  REQUIRED_CHECK_FAIL: "The plan no longer passes every hard check against the current airport state.",
  REQUIRED_CHECK_UNKNOWN: "A hard check could not be assessed against the current airport state.",
  ALREADY_APPLIED: "This plan is already applied.",
  STATE_CHANGED: "Airport resources changed since this scenario revision. A revised scenario is needed before a new run.",
  REVIEW_NOT_CONNECTED: "The review hook is not connected on the server.",
  PLANNING_NOT_CONNECTED: "The planner is not connected on the server.",
  INVALID_INPUT: "The request was rejected as invalid.",
};

let view = null; // { run, events, resource, evaluation, label }
let pollTimer = null;
let decisionKey = null; // reused when the same decision is retried, so it applies once

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
  return `T+${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")} (${seconds} s)`;
}

const num = (value, unit) => (value === null || value === undefined ? "—" : `${Number(value)} ${unit}`);

function token() {
  return $("token-input").value.trim();
}

function rememberToken() {
  try { sessionStorage.setItem("plab-token", token()); } catch { /* storage may be unavailable */ }
}

function restoreToken() {
  try { $("token-input").value = sessionStorage.getItem("plab-token") || ""; } catch { /* ignore */ }
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  const data = await response.json().catch(() => ({}));
  return { ok: response.ok, status: response.status, data };
}

function errorText(result) {
  const code = result.data && result.data.error && result.data.error.code;
  return code ? `${ERRORS[code] || "Request refused."} (${code})` : `Request failed (HTTP ${result.status}).`;
}

async function loadScenarios() {
  const select = $("scenario-select");
  if (!LIVE) {
    select.replaceChildren(el("option", "D01 (sample)"));
    select.disabled = true;
    return;
  }
  const result = await api("/api/scenarios");
  select.replaceChildren();
  for (const s of result.ok ? result.data : []) {
    const option = el("option", `${s.scenario_id} rev ${s.revision} · ${s.source.identity}`);
    option.value = JSON.stringify({ scenario_id: s.scenario_id, scenario_revision: s.revision });
    select.append(option);
  }
  if (!select.options.length) select.append(el("option", "No scenarios loaded"));
}

async function load() {
  if (!LIVE) {
    const result = await api(`/static/fixtures/${encodeURIComponent(sampleName)}.json`);
    if (!result.ok) throw new Error(`Sample "${sampleName}" not found`);
    view = { ...result.data, label: result.data.sample_label };
  } else if (runId) {
    const run = await api(`/api/runs/${encodeURIComponent(runId)}`);
    if (!run.ok) throw new Error(errorText(run));
    const after = view && view.events.length ? view.events[view.events.length - 1].sequence : 0;
    const events = await api(`/api/runs/${encodeURIComponent(runId)}/events?after_seq=${after}`);
    const evaluation = view ? { data: view.evaluation } : await api("/api/evaluations/latest");
    const resource = await api(`/api/resources/${encodeURIComponent(run.data.snapshot.resource_id)}`);
    // Overlapping refreshes (poll + decision) can fetch the same events; keep one per sequence.
    const merged = new Map([...(view ? view.events : []), ...(events.ok ? events.data : [])].map((e) => [e.sequence, e]));
    view = {
      run: run.data,
      events: [...merged.values()].sort((a, b) => a.sequence - b.sequence),
      resource: resource.ok ? resource.data : null,
      evaluation: evaluation.data,
      label: null,
    };
  } else {
    view = null;
  }
  render();
  clearTimeout(pollTimer);
  const run = view && view.run;
  const changing = LIVE && run && (run.processing_state !== "TERMINAL" || run.approval_state === "PENDING");
  if (changing) pollTimer = setTimeout(() => load().catch(showError), 1000);
}

function render() {
  $("mode-badge").textContent = !LIVE ? "Sample data" : view ? liveBadge(view.run) : "No run";
  $("mode-note").hidden = !view || !view.label;
  if (view && view.label) $("mode-note").textContent = view.label;
  $("sample-select").value = sampleName || "";
  const run = view && view.run;
  $("start-btn").disabled = !LIVE;
  $("cancel-btn").disabled = !(LIVE && run && (run.processing_state !== "TERMINAL" || run.approval_state === "PENDING"));
  for (const id of ["scenario-h", "run-h", "review-card", "agents-h", "cands-h", "checks-h", "evidence-h"]) {
    $(id).closest("section").hidden = !run;
  }
  if (!run) {
    $("session-hint").textContent = "Enter the operator token, choose a scenario and start a run. Or open a sample.";
    return;
  }
  renderScenario(run);
  renderRun(run);
  renderReview(run);
  renderAgents(run);
  renderShortlist(run);
  renderChecks(run);
  renderEvidence(run);
}

function liveBadge(run) {
  const provider = run.provider || {};
  return provider.mode === "LIVE" ? `Live model · ${provider.model}` : `Provider: ${provider.mode}`;
}

function renderScenario(run) {
  const list = $("scenario-facts");
  list.replaceChildren();
  const snap = run.snapshot;
  const fields = snap.source.original_fields || {};
  fact(list, "Historical flight", snap.source.identity, "historical");
  fact(list, "Qualifying delays", `weather ${fields.WEATHER_DELAY ?? "blank"} min, late aircraft ${fields.LATE_AIRCRAFT_DELAY ?? "blank"} min`, "historical");
  fact(list, "Source row", `${snap.source.dataset_id} record ${snap.source.record_ordinal}`);
  fact(list, "Dataset hash", `${snap.source.dataset_sha256.slice(0, 12)}…`);
  fact(list, "Scenario", `${snap.scenario_id} rev ${snap.revision}`, "synthetic overlay");
  fact(list, "Simulation epoch", snap.simulation_epoch, "synthetic");
  fact(list, "Airport resource", `${snap.resource_id} rev ${snap.resource_revision}`);
  fact(list, "Rules / policy", `${snap.ruleset_version} / ${snap.policy_version}`, "illustrative");
}

function renderRun(run) {
  const list = $("run-facts");
  list.replaceChildren();
  const provider = run.provider || {};
  fact(list, "Processing", run.processing_state);
  fact(list, "Outcome", run.negotiation_outcome);
  fact(list, "Rounds", `${run.round_count} of 2`);
  fact(list, "Model calls", `${run.model_call_count} of 6`);
  fact(list, "Agent invocations", `${run.agent_invocation_count} of 6`);
  fact(list, "Provider", `${provider.mode} · ${provider.model}`, provider.prompt_version);
  fact(list, "Planner", run.planning_mode);
  fact(list, "Elapsed", run.elapsed_ms === null ? null : `${run.elapsed_ms} ms`);
  if (run.fallback_reason) fact(list, "Fallback reason", run.fallback_reason);
  if (run.reason_code) fact(list, "Reason code", run.reason_code);
}

function reviewState(run) {
  const outcome = run.negotiation_outcome;
  const approval = run.approval_state;
  const revision = view.resource ? view.resource.revision : null;
  if (approval === "APPLIED_SIMULATION") return ["state-ok", "Applied to simulation", `Airport resource is now at revision ${revision}.`];
  if (approval === "REJECTED") return ["state-info", "Rejected", "The simulated schedule was not changed."];
  if (approval === "CANCELLED") return ["state-info", "Cancelled", "The simulated schedule was not changed."];
  if (approval === "STALE") return ["state-bad", "Stale: approval blocked", `Airport resources changed after this plan was made (now revision ${revision}). Run again.`];
  if (approval === "EXPIRED") return ["state-bad", "Expired: approval blocked", "The recommendation was not approved in time. Run again."];
  if (run.processing_state !== "TERMINAL") return ["state-info", "Negotiating", `${run.processing_state}, round ${run.round_count} of 2.`];
  if (outcome === "FEASIBLE_CONSENSUS") return ["state-ok", "Consensus awaiting review", "All three agents accepted the plan below. Agreement does not apply it; you decide."];
  if (outcome === "FEASIBLE_FALLBACK") return ["state-warn", "Fallback awaiting review", "The agents did not reach agreement. This plan is the earliest plan passing every hard check, chosen without AI."];
  if (outcome === "NO_FEASIBLE_OPTION_IN_SEARCH_SPACE") return ["state-bad", "No feasible option in the searched window", "Every candidate failed at least one hard check. Needs qualified review outside the simulator."];
  if (outcome === "INSUFFICIENT_DATA") return ["state-bad", "Input review required", "Required facts are missing, so nothing was planned and no agent was asked. Nothing is guessed."];
  if (outcome === "SYSTEM_ERROR") return ["state-bad", "System error", "Checks could not be completed reliably. No plan can be approved."];
  return ["state-info", outcome || "Waiting", ""];
}

function renderReview(run) {
  const [cls, title, detail] = reviewState(run);
  const state = $("review-state");
  state.className = `state ${cls}`;
  state.replaceChildren(el("span", title), el("small", detail));

  const list = $("plan-facts");
  list.replaceChildren();
  const plan = run.candidate;
  if (plan) {
    const i = plan.impacts;
    fact(list, "Plan", plan.candidate_id);
    fact(list, "Landing", clock(plan.landing_time_s));
    fact(list, "Gate-in", clock(plan.gate_in_time_s));
    fact(list, "Gate / route", `${plan.gate_id} via ${plan.taxi_route_id}`);
    fact(list, "Handling complete", clock(plan.service_end_s));
    fact(list, "Added hold", num(i.additional_landing_delay_s / 60, "min"), "modelled");
    fact(list, "Fuel margin", num(i.reserve_margin_kg, "kg"), "modelled");
    fact(list, "Crew overtime", num(i.crew_overtime_min, "min"), "modelled");
    fact(list, "Expires", run.expires_at ? new Date(run.expires_at).toLocaleTimeString() : null);
    fact(list, "Plan hash", `${run.plan_hash.slice(0, 12)}…`);
  } else {
    const reasons = (run.catalog && run.catalog.blocking_reasons) || [];
    if (reasons.length) fact(list, "Blocking reasons", reasons.join("; "));
    const missing = missingFacts(run);
    if (missing.length) fact(list, "Missing facts", missing.join(", "));
    const summary = catalogSummary(run.catalog);
    if (summary) {
      fact(list, "Searched", `${summary.candidates} candidates, ${summary.feasible} feasible`);
      const rules = Object.entries(summary.blockers).map(([rule, n]) => `${rule}: ${n}`).join("; ");
      if (rules) fact(list, "Candidates blocked by rule", rules);
    }
  }

  const reviewable = run.approval_state === "PENDING" && plan && run.checks.length > 0
    && run.checks.every((c) => c.status === "PASS");
  const fallback = run.negotiation_outcome === "FEASIBLE_FALLBACK";
  $("ack-row").hidden = !(reviewable && fallback);
  $("review-controls").hidden = !reviewable && !CLOSED.includes(run.approval_state);
  const canAct = reviewable && LIVE;
  const hasReason = $("reason-input").value.trim().length > 0;
  $("approve-btn").disabled = !canAct || !hasReason || (fallback && !$("ack-box").checked);
  $("reject-btn").disabled = !canAct || !hasReason;
  $("reason-input").disabled = !canAct;
  $("rerun-btn").disabled = !LIVE;
  $("review-hint").textContent = !reviewable
    ? (CLOSED.includes(run.approval_state) ? "This recommendation is closed. Run again to decide on a fresh plan." : "")
    : !LIVE ? "Sample data: decisions need a live run."
      : "Enter a reason to approve or reject. The server re-checks every hard rule against the current airport state before applying.";
}

function missingFacts(run) {
  const facts = Object.fromEntries(run.snapshot.facts.map((f) => [f.fact_id, f.value]));
  const fuel = facts["overlay.fuel"] || {};
  return Object.entries(fuel).filter(([, v]) => v === null).map(([k]) => `fuel.${k}`);
}

function renderAgents(run) {
  const box = $("agent-cards");
  box.replaceChildren();
  for (const role of ROLES) {
    const card = el("article", null, "agent");
    card.append(el("h3", role));
    const offer = (run.offers || {})[role];
    const vote = (run.votes || {})[role];
    if (!offer && !vote) card.append(el("p", "No message recorded", "label"));
    if (offer) {
      card.append(el("p", "Round 1 offer", "label"));
      card.append(el("p", `Prefers ${offer.preferred_candidate_id ?? "none (refused)"}`));
      if (offer.concession_candidate_ids.length) {
        card.append(el("p", `Would concede to ${offer.concession_candidate_ids.join(", ")}`));
      }
      if (offer.objections.length) card.append(el("p", `${offer.objections.length} objection(s) cited`));
      card.append(el("p", offer.rationale));
    }
    if (vote) {
      const matches = vote.plan_hash === run.plan_hash;
      card.append(el("p", "Round 2 vote", "label"));
      card.append(el("p", `${vote.decision}${matches ? " (same plan hash)" : " (different plan hash)"}`,
        `status status-${vote.decision === "ACCEPT" && matches ? "PASS" : "FAIL"}`));
      card.append(el("p", vote.rationale));
    }
    box.append(card);
  }
}

function renderShortlist(run) {
  const body = $("candidate-table").querySelector("tbody");
  body.replaceChildren();
  const catalog = run.catalog;
  const entries = catalog ? catalog.candidates : [];
  const byId = Object.fromEntries(entries.map((e) => [e.plan.candidate_id, e.plan]));
  const selected = run.candidate && run.candidate.candidate_id;
  for (const id of catalog ? catalog.shortlist_ids : []) {
    const p = byId[id];
    if (!p) continue;
    const row = el("tr");
    if (id === selected) row.className = "selected";
    const cells = [
      id + (id === selected ? " (proposed)" : ""),
      clock(p.landing_time_s), clock(p.gate_in_time_s), `${p.gate_id} / ${p.taxi_route_id}`,
      num(p.impacts.additional_landing_delay_s / 60, "min"), num(p.impacts.reserve_margin_kg, "kg"),
      num(p.impacts.crew_overtime_min, "min"),
      `${Number(p.costs.ATC).toFixed(3)} / ${Number(p.costs.PILOT).toFixed(3)} / ${Number(p.costs.GROUND).toFixed(3)}`,
    ];
    for (const text of cells) row.append(el("td", text));
    body.append(row);
  }
  let note = "No catalog for this outcome.";
  if (catalog) {
    const summary = catalogSummary(catalog);
    note =`${summary.candidates} candidates checked, ${summary.feasible} pass every hard rule; the agents see this shortlist of ${catalog.shortlist_ids.length}. Costs are illustrative policy scores (lower is better).`;
  }
  $("cand-note").textContent = note;
}

// Live payloads carry the full catalog; samples carry only the shortlist plus these counts.
function catalogSummary(catalog) {
  if (!catalog) return null;
  if (catalog.sample_summary) return catalog.sample_summary;
  const blockers = {};
  for (const entry of catalog.candidates) {
    for (const check of entry.checks) {
      if (check.status !== "PASS") blockers[check.rule_id] = (blockers[check.rule_id] || 0) + 1;
    }
  }
  return {
    candidates: catalog.candidates.length,
    feasible: catalog.candidates.filter((e) => e.checks.every((c) => c.status === "PASS")).length,
    blockers,
  };
}

function renderChecks(run) {
  const box = $("check-list");
  box.replaceChildren();
  if (!run.checks.length) {
    box.append(el("p", "No plan was checked for this outcome.", "hint"));
    return;
  }
  for (const check of run.checks) {
    const item = el("details", null, "check");
    const summary = el("summary");
    summary.append(el("span", ICON[check.status], `status status-${check.status}`));
    summary.append(el("span", `${check.rule_id} · ${check.reason_code}`));
    summary.append(el("span", check.margin === null ? "" : `margin ${Number(check.margin)} ${check.units}`, "margin"));
    item.append(summary);
    item.append(el("pre", JSON.stringify({
      actual: check.actual, operator: check.operator, required: check.required, units: check.units,
      evidence: check.evidence_ids, rule_version: check.rule_version,
    }, null, 2)));
    box.append(item);
  }
}

function eventSummary(event) {
  const p = event.payload || {};
  const keys = ["state", "decision", "reason", "candidate_id", "outcome", "role", "round", "cause", "resource_revision", "reason_code"];
  return keys.filter((k) => p[k] !== undefined && p[k] !== null).map((k) => `${k}=${p[k]}`).join(", ");
}

function renderEvidence(run) {
  const list = $("event-list");
  list.replaceChildren();
  for (const event of view.events) {
    const item = el("li");
    item.append(el("strong", event.event_type), el("span", ` ${event.actor} `, "actor"), el("span", eventSummary(event)));
    list.append(item);
  }
  const link = $("export-link");
  link.href = LIVE ? `/api/runs/${encodeURIComponent(run.run_id)}/export` : `/static/fixtures/${encodeURIComponent(sampleName)}.json`;
  link.setAttribute("download", `run-${run.run_id}.json`);
  const evaluation = view.evaluation || {};
  $("evaluation").textContent = evaluation.status === "NOT_YET_EVALUATED" || !evaluation.status
    ? "Not yet evaluated" : (evaluation.summary || `Report status: ${evaluation.status}`);
}

function showResult(text, ok) {
  const result = $("review-result");
  result.hidden = false;
  result.className = `result ${ok ? "state-ok" : "state-bad"}`;
  result.textContent = text;
}

async function decide(decision) {
  const run = view.run;
  if (!token()) return showResult("Enter the operator token first.", false);
  if (!decisionKey || decisionKey.decision !== decision) decisionKey = { decision, key: crypto.randomUUID() };
  const body = {
    decision,
    plan_hash: run.plan_hash,
    scenario_revision: run.scenario_revision,
    resource_revision: run.resource_revision,
    ruleset_version: run.ruleset_version,
    policy_version: run.policy_version,
    expires_at: run.expires_at,
    reason: $("reason-input").value.trim(),
    acknowledge_fallback: $("ack-box").checked,
  };
  $("approve-btn").disabled = true;
  $("reject-btn").disabled = true;
  const result = await api(`/api/runs/${encodeURIComponent(run.run_id)}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token()}`, "Idempotency-Key": decisionKey.key },
    body: JSON.stringify(body),
  });
  if (result.ok) {
    decisionKey = null;
    showResult(result.data.approval_state === "APPLIED_SIMULATION" ? "Applied to the simulation." : "Decision recorded.", true);
  } else {
    if (result.status !== 0 && result.status < 500) decisionKey = null;
    showResult(errorText(result), false);
  }
  await load();
}

async function startRun(parentRunId) {
  if (!token()) {
    $("session-hint").textContent = "Enter the operator token first.";
    $("token-input").focus();
    return;
  }
  const choice = $("scenario-select").value;
  const target = parentRunId && view ? { scenario_id: view.run.snapshot.scenario_id, scenario_revision: view.run.snapshot.revision }
    : JSON.parse(choice || "null");
  if (!target) return;
  const result = await api("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token()}`, "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({ ...target, parent_run_id: parentRunId || null }),
  });
  if (result.ok && result.data.run_id) location.search = `?run=${encodeURIComponent(result.data.run_id)}`;
  else $("session-hint").textContent = errorText(result);
}

function showError(error) {
  $("session-hint").textContent = `Could not load: ${error.message}`;
}

$("token-input").addEventListener("change", rememberToken);
$("sample-select").addEventListener("change", (event) => {
  location.search = event.target.value ? `?sample=${encodeURIComponent(event.target.value)}` : "";
});
$("ack-box").addEventListener("change", () => view && renderReview(view.run));
$("reason-input").addEventListener("input", () => view && renderReview(view.run));
$("approve-btn").addEventListener("click", () => decide("APPROVE").catch(showError));
$("reject-btn").addEventListener("click", () => decide("REJECT").catch(showError));
$("start-btn").addEventListener("click", () => startRun(null).catch(showError));
$("rerun-btn").addEventListener("click", () => startRun(view && view.run.run_id).catch(showError));
$("cancel-btn").addEventListener("click", async () => {
  if (!token()) return showResult("Enter the operator token first.", false);
  const result = await api(`/api/runs/${encodeURIComponent(view.run.run_id)}/cancel`, {
    method: "POST", headers: { Authorization: `Bearer ${token()}` },
  });
  if (!result.ok) showResult(errorText(result), false);
  await load().catch(showError);
});

restoreToken();
loadScenarios().then(load).catch(showError);
