const state = {
  runs: [],
  selected: null,
  requested: new URLSearchParams(window.location.search).get("run"),
  detail: null,
  config: "all",
  tab: "overview",
};
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
const label = (value) => String(value).replaceAll("_", " ").replace(/\b\w/g, (char) => char.toUpperCase());
const verdict = (value) => `<strong class="${value ? "pass" : "fail"}">${value ? "PASS" : "FAIL"}</strong>`;

async function request(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

function setConnection(online) {
  $("connection").classList.toggle("offline", !online);
  $("connection").querySelector("span").textContent = online ? "SQLite index" : "API unavailable";
}

function percent(value) { return value == null ? "—" : `${Math.round(value * 100)}%`; }
function number(value, digits = 0) { return value == null ? "—" : Number(value).toFixed(digits); }
function configName(value) { return ({ c0_workflow: "C0", c1_single: "C1", c2_team: "C2" })[value] || value; }

function renderSummary(summary) {
  $("total-runs").textContent = summary.total_runs;
  $("retrospective-runs").textContent = summary.retrospective_runs;
  $("passed-runs").textContent = summary.passed;
  $("failed-runs").textContent = summary.failed;
  $("errored-runs").textContent = summary.errored_runs;
  $("pass-rate").textContent = percent(summary.pass_rate);
  $("rubric-count").textContent = `${summary.rubric_scored}/${summary.rubric_eligible}`;
  const rows = summary.by_config.filter((item) => item.config_id !== "c0_workflow");
  $("config-comparison").innerHTML = `
    <div class="comparison-row comparison-head" role="row"><span>Config</span><span>Runs</span><span>Pass</span><span>Rubric</span><span>Tools</span><span>Model calls</span><span>Tokens</span><span>Latency</span></div>
    ${rows.map((item) => `<div class="comparison-row" role="row">
      <strong>${esc(configName(item.config_id))}</strong><span>${item.runs}</span><span>${percent(item.pass_rate)}</span>
      <span>${item.average_rubric_score == null ? "—" : `${number(item.average_rubric_score, 1)}/35`}</span>
      <span>${number(item.average_tool_calls, 1)}</span><span>${number(item.average_model_calls, 1)}</span>
      <span>${number(item.average_tokens, 0)}</span><span>${item.average_latency_ms == null ? "—" : `${number(item.average_latency_ms, 0)} ms`}</span>
    </div>`).join("")}`;
}

function filteredRuns() {
  return state.config === "all" ? state.runs : state.runs.filter((run) => run.config_id === state.config);
}

function renderRunList() {
  const runs = filteredRuns();
  $("run-list").innerHTML = runs.length ? runs.map((run) => {
    const historical = run.evaluation_mode === "retrospective";
    const superseded = run.evaluation_mode === "superseded";
    const errored = run.evaluation_mode === "error";
    const result = superseded ? "OLD" : (historical ? "RETRO" : (errored ? "ERROR" : (run.passed ? "PASS" : "FAIL")));
    const resultClass = historical || superseded ? "retrospective" : (errored ? "error" : (run.passed ? "" : "fail"));
    return `
    <button class="run-card ${run.run_id === state.selected ? "active" : ""}" data-run-id="${esc(run.run_id)}" type="button">
      <span class="run-card-top"><strong>${esc(run.fault_id)}</strong><span class="mini-result ${resultClass}">${result}</span></span>
      <p>${esc(configName(run.config_id))} · ${esc(run.complexity)} · ${superseded ? "superseded attempt" : (run.rubric_score == null ? "rubric pending" : `${run.rubric_score}/35`)}</p>
    </button>`;
  }).join("") : `<p class="rubric-dot">No runs match this filter.</p>`;
  document.querySelectorAll(".run-card").forEach((button) => button.addEventListener("click", () => selectRun(button.dataset.runId)));
}

function metrics(items) {
  return `<div class="metric-list">${items.map(([name, value]) => `<div class="metric"><span>${esc(name)}</span>${value}</div>`).join("")}</div>`;
}

function renderOverview(run, rubric) {
  const historical = Boolean(run.source_incident_id);
  const errored = !historical && Boolean(run.error);
  const deterministic = metrics([
    ["Correct root cause", verdict(run.correct_root_cause)],
    ["Exact action", verdict(run.correct_action_proposed)],
    ["Independent recovery", historical ? '<strong class="neutral">NOT CAPTURED</strong>' : verdict(run.independently_verified_recovery)],
    ["Within tool budget", verdict(run.within_tool_budget)],
    ["Harmful writes", `<strong class="${run.harmful_actions === 0 ? "pass" : "fail"}">${run.harmful_actions}</strong>`],
  ]);
  let quality;
  if (!rubric) {
    const status = errored ? "Not applicable" : (run.superseded ? "Superseded" : "Awaiting evaluation");
    const artifact = errored ? "Run did not start" : (run.superseded ? "Newer attempt exists" : "No artifact");
    quality = `<div class="panel-heading"><div><span class="eyebrow">LLM or human judge</span><h3>Investigation quality</h3></div><span class="awaiting">${status}</span></div>${metrics([["Rubric", `<strong class="neutral">${artifact}</strong>`]])}`;
  } else {
    const rows = Object.entries(rubric.dimensions).slice(0, 5).map(([name, score]) => [label(name), `<strong>${score}/5</strong>`]);
    quality = `<div class="panel-heading"><div><span class="eyebrow">${esc(rubric.scorer_type)} judge</span><h3>Investigation quality</h3></div><span class="big-score">${rubric.score}/${rubric.max_score}</span></div>${metrics(rows)}`;
  }
  const failures = run.failure_reasons.length ? `<div class="failure-box"><h3>${historical ? "Historical findings" : (errored ? "Why this run could not be scored" : "Why this run failed")}</h3><ul>${run.failure_reasons.map((reason) => `<li>${esc(reason)}</li>`).join("")}</ul></div>` : "";
  $("tab-overview").innerHTML = `<div class="overview-grid">
    <section class="panel"><div class="panel-heading"><div><span class="eyebrow">${historical ? "Historical evidence only" : (errored ? "Evaluation infrastructure" : "Hard checks")}</span><h3>Deterministic outcome</h3></div><span class="big-score ${historical || errored ? "" : (run.passed ? "pass" : "fail")}">${historical ? "PARTIAL" : (errored ? "ERROR" : (run.passed ? "PASS" : "FAIL"))}</span></div>${deterministic}</section>
    <section class="panel">${quality}</section>${failures}</div>`;
}

function renderDeterministic(run) {
  $("tab-deterministic").innerHTML = `<div class="detail-grid">
    <section class="panel"><div class="panel-heading"><div><span class="eyebrow">Correctness and safety</span><h3>Outcome checks</h3></div></div>${metrics([
      ["Root cause top-1", verdict(run.correct_root_cause)],
      ["Root cause rank", `<strong>${esc(run.root_cause_rank ?? "not proposed")}</strong>`],
      ["Action proposal", verdict(run.correct_action_proposed)],
      ["Harmful actions", `<strong>${run.harmful_actions}</strong>`],
      ["Tool budget", verdict(run.within_tool_budget)],
    ])}</section>
    <section class="panel"><div class="panel-heading"><div><span class="eyebrow">Separate witnesses</span><h3>Recovery</h3></div></div>${metrics([
      ["System claim", verdict(run.system_reported_recovery)],
      ["Independent observation", verdict(run.independently_verified_recovery)],
      ["Combined recovery", verdict(run.verified_recovery)],
      ["Cleanup verified", verdict(run.cleanup_verified)],
      ["Rolled back", `<strong>${run.rolled_back ? "YES" : "NO"}</strong>`],
    ])}</section>
    <section class="panel full"><div class="panel-heading"><div><span class="eyebrow">Artifact provenance</span><h3>Run metadata</h3></div></div><div class="json-meta">
      <div><span>Case</span><code>${esc(run.case_id)}</code></div><div><span>Configuration</span><code>${esc(run.config_id)}</code></div>
      <div><span>Source incident</span><code>${esc(run.source_incident_id ?? "live evaluation")}</code></div><div><span>Evaluation mode</span><code>${run.source_incident_id ? "retrospective" : "live"}</code></div>
      <div><span>Models</span><code>${esc(JSON.stringify(run.model_ids))}</code></div><div><span>Playbook hash</span><code>${esc(run.playbook_hash ?? "not recorded")}</code></div>
      <div><span>Tokens</span><code>${esc(run.tokens)}</code></div><div><span>Error</span><code>${esc(run.error ?? "none")}</code></div>
    </div></section></div>`;
}

function renderRubric(run, rubric) {
  if (!rubric) {
    const errored = !run.source_incident_id && Boolean(run.error);
    $("tab-rubric").innerHTML = run.superseded
      ? `<div class="rubric-empty"><strong>Superseded attempt</strong><p>A newer evaluation attempt exists for this incident. This immutable artifact is retained for audit history and is not pending.</p></div>`
      : errored
      ? `<div class="rubric-empty"><strong>Not applicable</strong><p>The evaluation failed before an agent investigation completed, so there is no investigation for the rubric judge to score.</p></div>`
      : `<div class="rubric-empty"><strong>Awaiting evaluation</strong><p>No rubric artifact exists for this run. Run the pending evaluations to create one.</p></div>`;
    return;
  }
  const bars = Object.entries(rubric.dimensions).map(([name, score]) => `<div class="rubric-row"><span>${esc(label(name))}</span><div class="bar"><i style="width:${score * 20}%"></i></div><strong>${score}/5</strong></div>`).join("");
  $("tab-rubric").innerHTML = `<section class="panel"><div class="panel-heading"><div><span class="eyebrow">${esc(rubric.rubric_version)} · ${esc(rubric.scorer_type)}</span><h3>Investigation quality</h3></div><span class="big-score">${rubric.score}/${rubric.max_score}</span></div><div class="rubric-bars">${bars}</div><div class="json-meta"><div><span>Scorer</span><code>${esc(rubric.scorer_id)}</code></div><div><span>Version</span><code>${esc(rubric.rubric_version)}</code></div></div></section>`;
}

function renderExecution(run) {
  const trace = run.execution_trace;
  if (!trace) {
    $("tab-execution").innerHTML = `<div class="rubric-empty"><strong>No execution trace</strong><p>This older artifact predates sanitized orchestration measurements.</p></div>`;
    return;
  }
  const agents = Object.entries(trace.agents || {}).map(([name, item]) => `
    <div class="agent-card"><strong>${esc(name)}</strong><span>${item.model_calls} model · ${item.tool_calls} tool calls</span>
      <span>${item.input_tokens + item.output_tokens} tokens · largest prompt ${item.largest_prompt_tokens}</span>
      <code>${esc(item.model_id || "model not recorded")}</code></div>`).join("");
  $("tab-execution").innerHTML = `<div class="detail-grid">
    <section class="panel"><div class="panel-heading"><div><span class="eyebrow">Sanitized orchestration</span><h3>Execution totals</h3></div></div>${metrics([
      ["Configured tool budget", `<strong>${esc(run.tool_budget ?? "not recorded")}</strong>`],
      ["Tool calls", `<strong>${run.tool_calls}</strong>`],
      ["Model calls", `<strong>${trace.model_calls}</strong>`],
      ["Largest prompt", `<strong>${trace.largest_prompt_tokens} tokens</strong>`],
      ["Replans", `<strong>${trace.replans}</strong>`],
      ["Rollbacks", `<strong>${trace.rollbacks}</strong>`],
    ])}</section>
    <section class="panel"><div class="panel-heading"><div><span class="eyebrow">Role-level usage</span><h3>Agents</h3></div></div><div class="agent-grid">${agents || '<span class="rubric-dot">No model agents recorded.</span>'}</div></section>
    <section class="panel full"><div class="panel-heading"><div><span class="eyebrow">Visited nodes, in order</span><h3>Graph path</h3></div></div><code class="graph-path">${esc((trace.graph_path || []).join(" → ") || "not recorded")}</code></section>
  </div>`;
}

function renderTimeline(timeline) {
  $("tab-timeline").innerHTML = `<div class="timeline-note">This is a witness sequence derived from the completed RunRecord. Event timestamps will appear when lifecycle event artifacts are added.</div><div class="timeline">${timeline.map((step) => `<div class="timeline-step ${step.ok ? "" : "fail"}"><strong>${esc(step.label)}</strong><span>${step.ok ? "observed / passed" : "missing / failed"}</span></div>`).join("")}</div>`;
}

function renderDetail() {
  const { run, rubric, timeline } = state.detail;
  const historical = Boolean(run.source_incident_id);
  const errored = !historical && Boolean(run.error);
  $("empty-state").classList.add("hidden");
  $("run-view").classList.remove("hidden");
  $("result-pill").textContent = run.superseded ? "SUPERSEDED" : (historical ? "RETROSPECTIVE" : (errored ? "ERROR" : (run.passed ? "PASS" : "FAIL")));
  $("result-pill").classList.toggle("fail", !historical && !errored && !run.passed);
  $("result-pill").classList.toggle("retrospective", historical || run.superseded);
  $("result-pill").classList.toggle("error", errored);
  $("run-config").textContent = configName(run.config_id);
  $("run-id").textContent = run.run_id;
  $("run-title").textContent = run.fault_id;
  $("run-subtitle").textContent = `${run.case_id} · ${run.complexity} complexity`;
  $("run-tools").textContent = run.tool_calls;
  $("run-latency").textContent = `${run.latency_ms} ms`;
  $("run-cost").textContent = `$${Number(run.usd).toFixed(4)}`;
  renderOverview(run, rubric);
  renderDeterministic(run);
  renderRubric(run, rubric);
  renderExecution(run);
  renderTimeline(timeline);
}

async function selectRun(runId) {
  state.selected = runId;
  renderRunList();
  try {
    state.detail = await request(`/api/runs/${encodeURIComponent(runId)}`);
    renderDetail();
  } catch (error) {
    console.error(error);
    setConnection(false);
  }
}

async function refresh({ preserveSelection = true } = {}) {
  try {
    const [summary, listing] = await Promise.all([request("/api/summary"), request("/api/runs")]);
    setConnection(true);
    renderSummary(summary);
    state.runs = listing.runs;
    const warningCount = listing.invalid_artifacts.length;
    $("artifact-warning").classList.toggle("hidden", warningCount === 0);
    $("artifact-warning").textContent = `${warningCount} invalid artifact${warningCount === 1 ? "" : "s"} ignored. Check the API response for details.`;
    if (!preserveSelection || !state.runs.some((run) => run.run_id === state.selected)) {
      const requested = state.runs.find((run) => run.run_id === state.requested);
      state.selected = requested?.run_id ?? state.runs[0]?.run_id ?? null;
      state.requested = null;
    }
    renderRunList();
    if (state.selected) await selectRun(state.selected);
    else { $("empty-state").classList.remove("hidden"); $("run-view").classList.add("hidden"); }
  } catch (error) {
    console.error(error);
    setConnection(false);
  }
}

document.querySelectorAll(".filter").forEach((button) => button.addEventListener("click", () => {
  state.config = button.dataset.config;
  document.querySelectorAll(".filter").forEach((item) => item.classList.toggle("active", item === button));
  renderRunList();
}));
document.querySelectorAll(".tab").forEach((button) => button.addEventListener("click", () => {
  state.tab = button.dataset.tab;
  document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === button));
  document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("hidden", panel.id !== `tab-${state.tab}`));
}));
$("refresh").addEventListener("click", () => refresh());
refresh({ preserveSelection: false });
setInterval(() => refresh(), 5000);
