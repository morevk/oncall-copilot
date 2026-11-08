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
function configName(value) { return ({ c0_workflow: "C0", c1_single: "C1", c2_team: "C2" })[value] || value; }

function renderSummary(summary) {
  $("total-runs").textContent = summary.total_runs;
  $("passed-runs").textContent = summary.passed;
  $("failed-runs").textContent = summary.failed;
  $("pass-rate").textContent = percent(summary.pass_rate);
  $("rubric-count").textContent = `${summary.rubric_scored}/${summary.total_runs}`;
}

function filteredRuns() {
  return state.config === "all" ? state.runs : state.runs.filter((run) => run.config_id === state.config);
}

function renderRunList() {
  const runs = filteredRuns();
  $("run-list").innerHTML = runs.length ? runs.map((run) => `
    <button class="run-card ${run.run_id === state.selected ? "active" : ""}" data-run-id="${esc(run.run_id)}" type="button">
      <span class="run-card-top"><strong>${esc(run.fault_id)}</strong><span class="mini-result ${run.passed ? "" : "fail"}">${run.passed ? "PASS" : "FAIL"}</span></span>
      <p>${esc(configName(run.config_id))} · ${esc(run.complexity)} · ${run.rubric_score == null ? "rubric pending" : `${run.rubric_score}/35`}</p>
    </button>`).join("") : `<p class="rubric-dot">No runs match this filter.</p>`;
  document.querySelectorAll(".run-card").forEach((button) => button.addEventListener("click", () => selectRun(button.dataset.runId)));
}

function metrics(items) {
  return `<div class="metric-list">${items.map(([name, value]) => `<div class="metric"><span>${esc(name)}</span>${value}</div>`).join("")}</div>`;
}

function renderOverview(run, rubric) {
  const deterministic = metrics([
    ["Correct root cause", verdict(run.correct_root_cause)],
    ["Exact action", verdict(run.correct_action_proposed)],
    ["Independent recovery", verdict(run.independently_verified_recovery)],
    ["Within tool budget", verdict(run.within_tool_budget)],
    ["Harmful writes", `<strong class="${run.harmful_actions === 0 ? "pass" : "fail"}">${run.harmful_actions}</strong>`],
  ]);
  let quality;
  if (!rubric) {
    quality = `<div class="panel-heading"><div><span class="eyebrow">LLM or human judge</span><h3>Investigation quality</h3></div><span class="awaiting">Awaiting evaluation</span></div>${metrics([["Rubric", '<strong class="neutral">No artifact</strong>']])}`;
  } else {
    const rows = Object.entries(rubric.dimensions).slice(0, 5).map(([name, score]) => [label(name), `<strong>${score}/5</strong>`]);
    quality = `<div class="panel-heading"><div><span class="eyebrow">${esc(rubric.scorer_type)} judge</span><h3>Investigation quality</h3></div><span class="big-score">${rubric.score}/${rubric.max_score}</span></div>${metrics(rows)}`;
  }
  const failures = run.failure_reasons.length ? `<div class="failure-box"><h3>Why this run failed</h3><ul>${run.failure_reasons.map((reason) => `<li>${esc(reason)}</li>`).join("")}</ul></div>` : "";
  $("tab-overview").innerHTML = `<div class="overview-grid">
    <section class="panel"><div class="panel-heading"><div><span class="eyebrow">Hard checks</span><h3>Deterministic outcome</h3></div><span class="big-score ${run.passed ? "pass" : "fail"}">${run.passed ? "PASS" : "FAIL"}</span></div>${deterministic}</section>
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

function renderRubric(rubric) {
  if (!rubric) {
    $("tab-rubric").innerHTML = `<div class="rubric-empty"><strong>Awaiting evaluation</strong><p>No rubric artifact exists for this run. The deterministic result remains available above.</p></div>`;
    return;
  }
  const bars = Object.entries(rubric.dimensions).map(([name, score]) => `<div class="rubric-row"><span>${esc(label(name))}</span><div class="bar"><i style="width:${score * 20}%"></i></div><strong>${score}/5</strong></div>`).join("");
  $("tab-rubric").innerHTML = `<section class="panel"><div class="panel-heading"><div><span class="eyebrow">${esc(rubric.rubric_version)} · ${esc(rubric.scorer_type)}</span><h3>Investigation quality</h3></div><span class="big-score">${rubric.score}/${rubric.max_score}</span></div><div class="rubric-bars">${bars}</div><div class="json-meta"><div><span>Scorer</span><code>${esc(rubric.scorer_id)}</code></div><div><span>Version</span><code>${esc(rubric.rubric_version)}</code></div></div></section>`;
}

function renderTimeline(timeline) {
  $("tab-timeline").innerHTML = `<div class="timeline-note">This is a witness sequence derived from the completed RunRecord. Event timestamps will appear when lifecycle event artifacts are added.</div><div class="timeline">${timeline.map((step) => `<div class="timeline-step ${step.ok ? "" : "fail"}"><strong>${esc(step.label)}</strong><span>${step.ok ? "observed / passed" : "missing / failed"}</span></div>`).join("")}</div>`;
}

function renderDetail() {
  const { run, rubric, timeline } = state.detail;
  $("empty-state").classList.add("hidden");
  $("run-view").classList.remove("hidden");
  $("result-pill").textContent = run.passed ? "PASS" : "FAIL";
  $("result-pill").classList.toggle("fail", !run.passed);
  $("run-config").textContent = configName(run.config_id);
  $("run-id").textContent = run.run_id;
  $("run-title").textContent = run.fault_id;
  $("run-subtitle").textContent = `${run.case_id} · ${run.complexity} complexity`;
  $("run-tools").textContent = run.tool_calls;
  $("run-latency").textContent = `${run.latency_ms} ms`;
  $("run-cost").textContent = `$${Number(run.usd).toFixed(4)}`;
  renderOverview(run, rubric);
  renderDeterministic(run);
  renderRubric(rubric);
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
