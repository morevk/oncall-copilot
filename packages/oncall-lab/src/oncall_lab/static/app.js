const statusNode = document.querySelector("#status");
const healthSummary = document.querySelector("#health-summary");
const servicesNode = document.querySelector("#services");
const faultsNode = document.querySelector("#faults");
const budgetInput = document.querySelector("#tool-budget");
const consoleLink = document.querySelector("#console-link");
const evaluationLink = document.querySelector("#evaluation-link");
const workflowConsoleLink = document.querySelector("#workflow-console-link");
const workflowEvaluationLink = document.querySelector("#workflow-evaluation-link");
const runnerStatus = document.querySelector("#runner-state");
const runnerMessage = document.querySelector("#runner-message");
const runnerEvents = document.querySelector("#runner-events");
const runnerStream = document.querySelector("#runner-stream");
const runnerWarning = document.querySelector("#runner-warning");
const runnerReport = document.querySelector("#runner-report");
const preflightButton = document.querySelector("#run-preflight");
const deterministicButton = document.querySelector("#run-deterministic");
const acknowledgementCheck = document.querySelector("#ack-check");
const acknowledgementInput = document.querySelector("#ack-phrase");
const requiredAcknowledgement = document.querySelector("#required-ack");
const retrospectiveForm = document.querySelector("#retrospective-form");
const retrospectiveIncident = document.querySelector("#saved-incident");
const retrospectiveCase = document.querySelector("#saved-case");
const retrospectiveButton = document.querySelector("#run-retrospective");
const retrospectiveStatus = document.querySelector("#retrospective-status");
const retrospectiveReport = document.querySelector("#retrospective-report");
const batchState = document.querySelector("#batch-state");
const batchMessage = document.querySelector("#batch-message");
const batchEvents = document.querySelector("#batch-events");
const batchStream = document.querySelector("#batch-stream");
const batchButton = document.querySelector("#run-batch-evaluation");
const batchReEvaluate = document.querySelector("#batch-re-evaluate");
const batchEligible = document.querySelector("#batch-eligible");
const batchPending = document.querySelector("#batch-pending");
const batchComplete = document.querySelector("#batch-complete");
const batchReport = document.querySelector("#batch-report");

const runner = {
  capabilities: null,
  job: null,
  stream: null,
  seen: new Set(),
  faultsArmed: false,
  reportUrl: "http://127.0.0.1:8030/",
};

const batch = {
  capabilities: null,
  incidents: [],
  job: null,
  stream: null,
  seen: new Set(),
};

const terminalStates = new Set(["succeeded", "failed", "interrupted"]);
const faultActionHelp = {
  arm: "Turns this fault on in the live demo by changing its feature flag. The application begins exhibiting the failure, but no investigation starts until you click Page the agent now or telemetry raises an alert.",
  disarm: "Restores this fault's healthy baseline. Use it after manual testing. The evaluation runner performs its own cleanup automatically.",
  page: "Sends only the visible symptom to the incident API and starts the agent immediately. It does not inject the fault or reveal the root cause.",
};

function setStatus(message, isError) {
  statusNode.textContent = message;
  statusNode.classList.toggle("error", Boolean(isError));
}

async function jsonRequest(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { Accept: "application/json", ...(options.headers || {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed with HTTP ${response.status}.`);
  return payload;
}

function setRunnerMessage(message, isError = false) {
  runnerMessage.textContent = message;
  runnerMessage.classList.toggle("error", isError);
}

function runnerActive() {
  return runner.job && !terminalStates.has(runner.job.state);
}

function syncRunnerControls() {
  const phrase = runner.capabilities && runner.capabilities.acknowledgement;
  const acknowledged = Boolean(
    phrase
    && acknowledgementCheck.checked
    && acknowledgementInput.value === phrase,
  );
  const available = Boolean(runner.capabilities);
  preflightButton.disabled = !available || runnerActive();
  deterministicButton.disabled = !available || runnerActive() || runner.faultsArmed || !acknowledged;
  acknowledgementCheck.disabled = runnerActive();
  acknowledgementInput.disabled = runnerActive();
  runnerWarning.classList.toggle("hidden", !runner.faultsArmed);
  runnerWarning.textContent = runner.faultsArmed
    ? "A fault is already armed. Disarm it before starting a deterministic evaluation; the harness requires a clean baseline."
    : "";
}

function setRunnerState(state) {
  const value = state || "idle";
  runnerStatus.textContent = value;
  runnerStatus.className = `runner-state ${value}`;
}

function batchActive() {
  return batch.job && !terminalStates.has(batch.job.state);
}

function batchCounts() {
  const eligible = batch.incidents.length;
  const complete = batch.incidents.filter(
    (incident) => incident.deterministic_evaluated && incident.llm_evaluated,
  ).length;
  return { eligible, complete, pending: eligible - complete };
}

function syncBatchControls() {
  const counts = batchCounts();
  batchEligible.textContent = counts.eligible;
  batchPending.textContent = counts.pending;
  batchComplete.textContent = counts.complete;
  const configured = Boolean(batch.capabilities && batch.capabilities.judge_available);
  const hasWork = batchReEvaluate.checked ? counts.eligible > 0 : counts.pending > 0;
  batchButton.disabled = !configured || batchActive() || !hasWork;
  batchReEvaluate.disabled = batchActive();
  batchButton.textContent = batchReEvaluate.checked
    ? `Re-evaluate ${counts.eligible} incident${counts.eligible === 1 ? "" : "s"}`
    : `Evaluate ${counts.pending} pending incident${counts.pending === 1 ? "" : "s"}`;
}

function setBatchState(state) {
  const value = state || "idle";
  batchState.textContent = value;
  batchState.className = `runner-state ${value}`;
}

function setBatchMessage(message, isError = false) {
  batchMessage.textContent = message;
  batchMessage.classList.toggle("error", isError);
}

function appendBatchEvent(event) {
  const sequence = Number(event.sequence);
  if (Number.isFinite(sequence) && batch.seen.has(sequence)) return;
  if (Number.isFinite(sequence)) batch.seen.add(sequence);
  const item = document.createElement("li");
  const phase = document.createElement("span");
  phase.textContent = event.phase || "update";
  const message = document.createElement("p");
  message.textContent = event.message || "Batch evaluation updated.";
  item.append(phase, message);
  batchEvents.append(item);
  batchEvents.scrollTop = batchEvents.scrollHeight;
}

function renderBatchJob(job, { resetEvents = false } = {}) {
  batch.job = job;
  setBatchState(job.state);
  setBatchMessage(job.error || job.message || "Batch evaluation started.", job.state === "failed");
  if (resetEvents) {
    batch.seen.clear();
    batchEvents.replaceChildren();
    (job.events || []).forEach(appendBatchEvent);
  }
  const created = Number((job.summary || {}).llm_created || 0);
  batchReport.classList.toggle("hidden", created === 0);
  batchReport.href = runner.reportUrl;
  syncBatchControls();
}

function closeBatchStream() {
  if (batch.stream) batch.stream.close();
  batch.stream = null;
  batchStream.classList.remove("live");
  batchStream.lastChild.textContent = " SSE idle";
}

function connectBatch(jobId) {
  closeBatchStream();
  batch.job = { job_id: jobId, state: "running", message: "Connecting to batch…", summary: {} };
  batch.seen.clear();
  batchEvents.replaceChildren();
  batchReport.classList.add("hidden");
  setBatchState("running");
  setBatchMessage("Connecting to batch evaluation progress…");
  syncBatchControls();
  const stream = new EventSource(`/api/batch-evaluations/${encodeURIComponent(jobId)}/stream`);
  batch.stream = stream;
  stream.onopen = () => {
    batchStream.classList.add("live");
    batchStream.lastChild.textContent = " SSE live";
  };
  stream.addEventListener("snapshot", (event) => {
    renderBatchJob(JSON.parse(event.data), { resetEvents: true });
  });
  stream.addEventListener("progress", (event) => {
    const update = JSON.parse(event.data);
    appendBatchEvent(update);
    setBatchMessage(update.message || "Batch evaluation updated.");
  });
  stream.addEventListener("completed", (event) => {
    renderBatchJob(JSON.parse(event.data));
    closeBatchStream();
    loadBatchCapabilities().catch((error) => setBatchMessage(error.message, true));
    loadRetrospectiveInputs().catch((error) => setBatchMessage(error.message, true));
  });
  stream.onerror = () => {
    if (batchActive()) setBatchMessage("Batch progress stream interrupted; reconnecting…", true);
  };
}

async function loadBatchCapabilities() {
  batch.capabilities = await jsonRequest("/api/batch-evaluations/capabilities");
  if (
    batch.capabilities.active_job_id
    && (!batch.job || batch.job.job_id !== batch.capabilities.active_job_id)
  ) {
    connectBatch(batch.capabilities.active_job_id);
    return;
  }
  if (!batch.job) {
    setBatchState(batch.capabilities.judge_available ? "idle" : "unavailable");
    setBatchMessage(
      batch.capabilities.judge_available
        ? "Ready to evaluate all pending saved incidents."
        : batch.capabilities.unavailable_reason || "The LLM judge is not configured.",
      !batch.capabilities.judge_available,
    );
  }
  syncBatchControls();
}

async function startBatchEvaluation() {
  batchButton.disabled = true;
  batchReport.classList.add("hidden");
  setBatchMessage("Starting batch evaluation…");
  try {
    const job = await jsonRequest("/api/batch-evaluations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ re_evaluate: batchReEvaluate.checked }),
    });
    connectBatch(job.job_id);
  } catch (error) {
    setBatchState("failed");
    setBatchMessage(error.message, true);
    await loadBatchCapabilities().catch(() => {});
  }
}

function appendRunnerEvent(event) {
  const sequence = Number(event.sequence);
  if (Number.isFinite(sequence) && runner.seen.has(sequence)) return;
  if (Number.isFinite(sequence)) runner.seen.add(sequence);
  const item = document.createElement("li");
  const phase = document.createElement("span");
  phase.textContent = event.phase || "update";
  const message = document.createElement("p");
  message.textContent = event.message || "Evaluation updated.";
  item.append(phase, message);
  runnerEvents.append(item);
  runnerEvents.scrollTop = runnerEvents.scrollHeight;
}

function reportFor(job) {
  const runId = job && job.run_ids && job.run_ids[0];
  runnerReport.classList.toggle("hidden", !runId);
  if (runId) runnerReport.href = `${runner.reportUrl}?run=${encodeURIComponent(runId)}`;
}

function renderRunnerJob(job, { resetEvents = false } = {}) {
  runner.job = job;
  setRunnerState(job.state);
  setRunnerMessage(job.error || job.message || "Evaluation job started.", job.state === "failed");
  if (resetEvents) {
    runner.seen.clear();
    runnerEvents.replaceChildren();
    (job.events || []).forEach(appendRunnerEvent);
  }
  reportFor(job);
  syncRunnerControls();
}

function closeRunnerStream() {
  if (runner.stream) runner.stream.close();
  runner.stream = null;
  runnerStream.classList.remove("live");
  runnerStream.lastChild.textContent = " SSE idle";
}

function connectRunner(jobId) {
  closeRunnerStream();
  runner.job = { job_id: jobId, state: "running", message: "Connecting to evaluation job…", run_ids: [] };
  runner.seen.clear();
  runnerEvents.replaceChildren();
  reportFor(runner.job);
  setRunnerState("running");
  setRunnerMessage("Connecting to live evaluation progress…");
  syncRunnerControls();
  const stream = new EventSource(`/api/evaluation-jobs/${encodeURIComponent(jobId)}/stream`);
  runner.stream = stream;
  stream.onopen = () => {
    runnerStream.classList.add("live");
    runnerStream.lastChild.textContent = " SSE live";
  };
  stream.addEventListener("snapshot", (event) => {
    renderRunnerJob(JSON.parse(event.data), { resetEvents: true });
  });
  stream.addEventListener("progress", (event) => {
    const update = JSON.parse(event.data);
    appendRunnerEvent(update);
    setRunnerMessage(update.message || "Evaluation updated.");
  });
  stream.addEventListener("completed", (event) => {
    const job = JSON.parse(event.data);
    renderRunnerJob(job);
    closeRunnerStream();
    loadEvaluationCapabilities().catch((error) => setRunnerMessage(error.message, true));
    load().catch((error) => setStatus(error.message, true));
    loadServices().catch((error) => {
      healthSummary.textContent = error.message;
      healthSummary.classList.add("error");
    });
  });
  stream.onerror = () => {
    if (runnerActive()) setRunnerMessage("Progress stream interrupted; reconnecting…", true);
  };
}

async function loadEvaluationCapabilities() {
  const capabilities = await jsonRequest("/api/evaluation-jobs/capabilities");
  runner.capabilities = capabilities;
  requiredAcknowledgement.textContent = capabilities.acknowledgement;
  acknowledgementInput.placeholder = capabilities.acknowledgement;
  if (capabilities.active_job_id && (!runner.job || runner.job.job_id !== capabilities.active_job_id)) {
    connectRunner(capabilities.active_job_id);
    return;
  }
  if (!runner.job) {
    setRunnerState("idle");
    setRunnerMessage("Ready. Run the non-mutating readiness check before injecting a fault.");
  }
  syncRunnerControls();
}

async function startEvaluation(kind) {
  setRunnerMessage(kind === "preflight" ? "Starting readiness check…" : "Starting deterministic evaluation…");
  preflightButton.disabled = true;
  deterministicButton.disabled = true;
  try {
    const payload = {
      kind,
      config_id: "c1_single",
      case_ids: [],
    };
    if (kind === "deterministic") payload.acknowledgement = acknowledgementInput.value;
    const job = await jsonRequest("/api/evaluation-jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    connectRunner(job.job_id);
  } catch (error) {
    setRunnerState("failed");
    setRunnerMessage(error.message, true);
    await loadEvaluationCapabilities().catch(() => {});
  }
}

function selectOptions(node, rows, valueKey, label) {
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = rows.length ? "Select…" : "None available";
  node.replaceChildren(placeholder);
  rows.forEach((row) => {
    const option = document.createElement("option");
    option.value = row[valueKey];
    option.textContent = label(row);
    node.append(option);
  });
  node.disabled = rows.length === 0;
}

async function loadRetrospectiveInputs() {
  const [incidents, cases] = await Promise.all([
    jsonRequest("/api/saved-incidents"),
    jsonRequest("/api/evaluation-cases"),
  ]);
  const eligible = (incidents.incidents || []).filter((incident) => incident.eligible);
  batch.incidents = eligible;
  syncBatchControls();
  selectOptions(
    retrospectiveIncident,
    eligible,
    "incident_id",
    (incident) => `${incident.incident_id} · ${incident.status} · ${incident.alertname || "unknown alert"}`,
  );
  selectOptions(
    retrospectiveCase,
    cases.cases || [],
    "case_id",
    (item) => `${item.case_id} · ${item.fault_id}`,
  );
  retrospectiveButton.disabled = eligible.length === 0 || !(cases.cases || []).length;
  retrospectiveStatus.textContent = eligible.length
    ? `${eligible.length} completed incident${eligible.length === 1 ? "" : "s"} available.`
    : "No completed incidents with evaluation evidence are available yet.";
}

async function runRetrospective(event) {
  event.preventDefault();
  retrospectiveButton.disabled = true;
  retrospectiveReport.classList.add("hidden");
  retrospectiveStatus.classList.remove("error");
  retrospectiveStatus.textContent = "Scoring saved incident evidence…";
  try {
    const result = await jsonRequest("/api/retrospective-evaluations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        incident_id: retrospectiveIncident.value,
        case_id: retrospectiveCase.value,
      }),
    });
    retrospectiveStatus.textContent = result.passed
      ? "Retrospective evaluation passed."
      : `Recorded as non-passing: ${(result.failure_reasons || []).join("; ")}`;
    retrospectiveReport.href = result.report_url;
    retrospectiveReport.classList.remove("hidden");
  } catch (error) {
    retrospectiveStatus.textContent = error.message;
    retrospectiveStatus.classList.add("error");
  } finally {
    retrospectiveButton.disabled = false;
  }
}

function actionText(action) {
  if (!action) return "none";
  const args = Object.entries(action.args || [])
    .map(([key, value]) => `${key}=${value}`)
    .join(" ");
  return `${action.action} ${args}`.trim();
}

function card(fault) {
  const article = document.createElement("article");
  article.className = "card";

  const header = document.createElement("header");
  const title = document.createElement("h2");
  title.textContent = fault.fault_id;
  const badge = document.createElement("span");
  badge.className = fault.armed ? "badge on" : "badge";
  badge.textContent = fault.armed ? "Armed" : fault.current_variant || "unknown";
  header.append(title, badge);

  const meta = document.createElement("p");
  meta.className = "meta";
  meta.textContent = `Flag ${fault.flag}. Arm sets ${fault.arm_variant}. Disarm sets ${fault.disarm_variant}. Current variant is ${fault.current_variant}.`;

  const truth = document.createElement("div");
  truth.className = "truth";
  const truthTitle = document.createElement("h3");
  truthTitle.textContent = "Ground truth. This stays on this page.";
  const cause = document.createElement("p");
  cause.textContent = `Root cause: ${fault.root_cause_services.join(", ")}`;
  const correct = document.createElement("p");
  correct.textContent = `Correct action: ${actionText(fault.correct_action)}`;
  const detector = document.createElement("p");
  detector.textContent = `Manifest detector: ${fault.detect.operator} ${fault.detect.threshold}`;
  const query = document.createElement("pre");
  query.textContent = fault.detect.query;
  truth.append(truthTitle, cause, correct, detector, query);

  const actions = document.createElement("div");
  actions.className = "actions";
  actions.append(
    actionControl("Arm fault", "primary", () => run(fault, "arm"), faultActionHelp.arm),
    actionControl("Disarm", "", () => run(fault, "disarm"), faultActionHelp.disarm),
    actionControl(
      "Page the agent now",
      "",
      () => run(fault, "page"),
      faultActionHelp.page,
      !fault.can_page,
    ),
  );

  article.append(header, meta, truth, actions);
  if (fault.grafana_note) {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = fault.grafana_note;
    article.append(note);
  } else if (!fault.can_page) {
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = "No symptom is published for this fault, so this page cannot start the agent.";
    article.append(note);
  }
  return article;
}

function button(label, className, onClick, disabled) {
  const node = document.createElement("button");
  node.type = "button";
  node.textContent = label;
  if (className) node.className = className;
  node.disabled = Boolean(disabled);
  node.addEventListener("click", onClick);
  return node;
}

function actionControl(label, className, onClick, help, disabled = false) {
  const wrapper = document.createElement("span");
  wrapper.className = "action-control";
  const action = button(label, className, onClick, disabled);
  action.title = help;
  action.setAttribute("aria-description", help);
  const tip = document.createElement("span");
  tip.className = "help-tip";
  tip.tabIndex = 0;
  tip.setAttribute("role", "note");
  tip.setAttribute("aria-label", `${label}: ${help}`);
  tip.dataset.tooltip = help;
  tip.textContent = "?";
  wrapper.append(action, tip);
  return wrapper;
}

function budget() {
  const value = Number(budgetInput.value);
  if (!Number.isInteger(value) || value < 1 || value > 100) {
    throw new Error("Tool budget must be a whole number from 1 to 100.");
  }
  return value;
}

async function run(fault, action) {
  setStatus("");
  let response;
  try {
    const options = { method: "POST" };
    if (action === "page") {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify({ tool_budget: budget() });
    }
    response = await fetch(`/api/faults/${encodeURIComponent(fault.fault_id)}/${action}`, options);
  } catch (error) {
    setStatus(error.message || "The lab request failed.", true);
    return;
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    setStatus(payload.detail || "The lab rejected the action.", true);
    return;
  }
  if (action === "page") {
    const alert = payload.alert && payload.alert.alerts && payload.alert.alerts[0];
    const labels = (alert && alert.labels) || {};
    const incident = payload.incident_id ? ` Incident ${payload.incident_id}.` : "";
    setStatus(
      `Paged ${labels.service || "the service"} as ${labels.alertname || "the symptom"}.${incident} Open the incident console to follow it.`,
    );
  } else if (payload.changed) {
    const grafana = action === "arm" && fault.grafana_note ? " Grafana can page the agent after the rule holds." : "";
    setStatus(
      action === "arm"
        ? `${fault.flag} is now ${fault.arm_variant}.${grafana}`
        : `${fault.flag} is back to ${fault.disarm_variant}.`,
    );
  } else {
    setStatus(`${fault.flag} was already ${action === "arm" ? fault.arm_variant : fault.disarm_variant}.`);
  }
  await load();
}

function serviceRow(service) {
  const item = document.createElement("li");
  item.className = service.status === "up" ? "service up" : "service down";
  const state = document.createElement("span");
  state.className = "service-state";
  state.textContent = service.status === "up" ? "Up" : "Down";
  const name = document.createElement("strong");
  name.textContent = service.name;
  const detail = document.createElement("span");
  detail.className = "service-detail";
  detail.textContent = service.detail;
  const link = document.createElement("a");
  link.href = service.href;
  link.textContent = service.href;
  item.append(state, name, detail, link);
  return item;
}

function renderServices(groups) {
  servicesNode.replaceChildren(
    ...groups.map((group) => {
      const block = document.createElement("section");
      block.className = "service-group";
      const heading = document.createElement("h3");
      heading.textContent = group.name;
      const list = document.createElement("ul");
      list.className = "service-list";
      list.append(...(group.services || []).map(serviceRow));
      block.append(heading, list);
      return block;
    }),
  );
}

async function loadServices() {
  const response = await fetch("/api/services");
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    healthSummary.textContent = payload.detail || "The lab could not check the services.";
    healthSummary.classList.add("error");
    return;
  }
  const groups = payload.groups || [];
  const rows = groups.flatMap((group) => group.services || []);
  const down = rows.filter((service) => service.status !== "up").length;
  healthSummary.classList.toggle("error", down > 0);
  healthSummary.textContent = down === 0
    ? `${rows.length} services up.`
    : `${rows.length - down} up, ${down} down.`;
  renderServices(groups);
}

function render(faults) {
  runner.faultsArmed = faults.some((fault) => fault.armed);
  syncRunnerControls();
  faultsNode.replaceChildren(...faults.map(card));
}

async function load() {
  const response = await fetch("/api/faults");
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    setStatus(payload.detail || "The lab could not read the faults.", true);
    return;
  }
  if (payload.console_url) {
    consoleLink.href = payload.console_url;
    workflowConsoleLink.href = payload.console_url;
  }
  if (payload.evaluation_url) {
    runner.reportUrl = payload.evaluation_url;
    evaluationLink.href = payload.evaluation_url;
    workflowEvaluationLink.href = payload.evaluation_url;
  }
  render(payload.faults || []);
}

document.querySelector("#budget-form").addEventListener("submit", (event) => event.preventDefault());
preflightButton.addEventListener("click", () => startEvaluation("preflight"));
deterministicButton.addEventListener("click", () => startEvaluation("deterministic"));
acknowledgementCheck.addEventListener("change", syncRunnerControls);
acknowledgementInput.addEventListener("input", syncRunnerControls);
retrospectiveForm.addEventListener("submit", runRetrospective);
batchButton.addEventListener("click", startBatchEvaluation);
batchReEvaluate.addEventListener("change", syncBatchControls);
document.querySelector("#refresh-health").addEventListener("click", () => {
  loadServices().catch((error) => {
    healthSummary.textContent = error.message;
    healthSummary.classList.add("error");
  });
});
load().catch((error) => setStatus(error.message, true));
loadServices().catch((error) => {
  healthSummary.textContent = error.message;
  healthSummary.classList.add("error");
});
loadEvaluationCapabilities().catch((error) => {
  setRunnerState("failed");
  setRunnerMessage(error.message, true);
  syncRunnerControls();
});
loadBatchCapabilities().catch((error) => {
  setBatchState("failed");
  setBatchMessage(error.message, true);
  syncBatchControls();
});
loadRetrospectiveInputs().catch((error) => {
  retrospectiveStatus.textContent = error.message;
  retrospectiveStatus.classList.add("error");
  retrospectiveButton.disabled = true;
});
