// The Actions tab: what this console can run, confirmed before it runs,
// watched while it runs, and read back afterwards from the audit log.
//
// SSE `action` events are demultiplexed by run_id (shouldTrackEvent()),
// never assumed to belong to "the current run": a slow listener can let one
// run's `finished` arrive after a later run has already completed, and the
// server never infers "the run is over" from its own lock being free either
// -- a new run can be accepted while a previous run's stream is still
// flushing. The event queue also drops under flood (`started`/`finished`
// never, `output` sometimes), so the live pane in #action-output is a view,
// not a transcript: reconcileRun() replaces it with the audit row's own
// output once the run is known to have finished, rather than trusting
// whatever happened to arrive over the wire.

import { el, clear } from "./dom.js";
import { authHeaders, translate, formatTime, formatDate } from "./i18n.js";

// The last /api/actions response, kept so a run starting or finishing can
// redraw the list (to disable/enable Run buttons) without asking again.
let actionsCache = [];
// The run this tab is actively tracking, or null when none is in flight.
// Sourced from the run_id a successful POST returns -- never inferred from
// the run lock, which can free up before this run's own stream has
// finished flushing.
let activeRunId = null;
// The action awaiting confirmation in the modal, set when Run is clicked
// and read back when "Run it" is clicked.
let pendingActionId = null;
// Guards the one-time listener attachment in paintActionsChrome(), which
// (like app.js's paintChrome()) runs again on every locale change.
let wired = false;

// A 409 means another run is in progress; a 403 means this request did not
// come from the machine itself; anything else (a network failure, a 401, a
// malformed response, a 5xx) is reported the same generic way -- the point
// is never an empty panel, not a precise diagnosis of every failure mode.
export function errorKeyForStatus(status) {
  if (status === 409) return "ui.error.action.busy";
  if (status === 403) return "ui.error.action.refused";
  return "ui.error.action.failed";
}

// exit_code is null exactly when a run was killed (a timeout, an operator
// cancel, an OOM kill) -- never 0 for that case, and this must not collapse
// the two: a killed run rendered as a success would undo a distinction the
// rest of the system goes out of its way to preserve.
export function outcomeKey(exitCode) {
  if (exitCode === null) return "ui.actions.result.killed";
  return exitCode === 0 ? "ui.actions.result.ok" : "ui.actions.result.failed";
}

// Only an event carrying the run_id this tab is actively tracking may touch
// the live pane or the button state -- see the module comment.
export function shouldTrackEvent(event, trackedRunId) {
  return Boolean(trackedRunId) && event.run_id === trackedRunId;
}

// The audit row for this run_id is the record of what actually happened;
// the `finished` event's own exit_code/duration_ms are used only as a
// fallback for the rare case (see runner.py) where the row itself could not
// be written. output is null in that fallback case -- the caller must then
// leave whatever the live pane already accumulated alone, rather than
// blanking it.
export function reconcileRun(event, runs) {
  const row = runs.find((run) => run.id === event.run_id);
  return row
    ? { exitCode: row.exit_code, durationMs: row.duration_ms, output: row.output }
    : { exitCode: event.exit_code, durationMs: event.duration_ms, output: null };
}

// A Run button is disabled when the action itself is unavailable, or while
// any run is in flight -- B1 runs one action at a time, and re-disabling
// every button (not just the one clicked) is what stops a second click from
// racing the first while the POST is still on the wire.
export function isRunDisabled(action, running) {
  return !action.available || running;
}

// A single fetch discipline for both /api/actions and /api/actions/runs: a
// non-2xx response is a failure this caller must hear about, exactly like
// history.js's fetchSeries() -- a caller must be able to tell "nothing was
// recorded" (or "nothing is available") from "we could not ask".
async function fetchJson(path) {
  const response = await fetch(path, { headers: authHeaders() });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

function riskBadge(risk) {
  const badge = document.createElement("span");
  badge.className = `risk-badge risk-${risk}`;
  // Colour never carries the risk level alone: icon and word travel
  // together, exactly like simple.js's severity tag.
  badge.textContent =
    `${translate(`risk.${risk}.icon`)} ${translate(`risk.${risk}.word`)}`;
  return badge;
}

function openConfirm(actionId) {
  pendingActionId = actionId;
  el("action-confirm-body").textContent = translate(`action.${actionId}.confirm`);
  // The modal itself opens via the Run button's data-bs-toggle/data-bs-target
  // attributes (set in actionRow()) -- Bootstrap's own data-api handles the
  // show, invoked after this listener since it runs on the same click but is
  // registered on the document, later in the bubble than the button itself.
}

function actionRow(action, running) {
  const row = document.createElement("div");
  row.className =
    "list-group-item d-flex justify-content-between align-items-center gap-3";
  const text = document.createElement("div");
  const title = document.createElement("div");
  title.className = "fw-semibold";
  title.textContent = translate(`action.${action.id}.label`);
  const description = document.createElement("p");
  description.className = "mb-1 text-body-secondary small";
  description.textContent = translate(`action.${action.id}.description`);
  text.append(title, description, riskBadge(action.risk));

  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-outline-secondary flex-shrink-0";
  button.textContent = translate("ui.actions.run");
  button.disabled = isRunDisabled(action, running);
  button.dataset.bsToggle = "modal";
  button.dataset.bsTarget = "#action-confirm-modal";
  button.addEventListener("click", () => openConfirm(action.id));

  row.append(text, button);
  return row;
}

// A single translated line in #action-list, replacing whatever was there --
// used both for "nothing to show" (available list is empty) and "could not
// find out" (the fetch itself failed). Never an empty panel either way.
function renderActionsNotice(key) {
  const list = el("action-list");
  clear(list);
  const message = document.createElement("p");
  message.className = "text-body-secondary";
  message.textContent = translate(key);
  list.append(message);
}

// Pure DOM builder over an already-fetched list, so a run starting or
// finishing can redraw the Run buttons' disabled state from actionsCache
// without a further round trip, and so it can be exercised directly in a
// test without stubbing fetch. An empty `actions` array reaching here is a
// genuinely empty catalogue, never the unknown state a failed request
// leaves behind -- see renderActions()'s own catch for that case.
export function renderActionRows(actions, running) {
  if (actions.length === 0) {
    renderActionsNotice("ui.actions.none");
    return;
  }
  const list = el("action-list");
  clear(list);
  for (const action of actions) list.append(actionRow(action, running));
}

export async function renderActions() {
  let actions;
  try {
    actions = await fetchJson("/api/actions");
  } catch (error) {
    // A blank panel here would read as "no actions exist"; it is instead
    // "we could not find out" -- an auth hiccup, a brief restart, a
    // network blip -- and must say so rather than leave the reader with no
    // button and no visible way to retry short of leaving the tab.
    console.warn("action catalogue unavailable", error);
    renderActionsNotice("ui.error.action.failed");
    return;
  }
  actionsCache = Array.isArray(actions) ? actions : [];
  renderActionRows(actionsCache, activeRunId !== null);
}

function auditRow(run) {
  const row = document.createElement("tr");
  const when = document.createElement("td");
  const at = new Date(run.ts * 1000);
  when.textContent = `${formatDate(at)} ${formatTime(at)}`;
  const what = document.createElement("td");
  what.textContent = translate(`action.${run.action_id}.label`);
  const source = document.createElement("td");
  // The raw client address, deliberately untranslated -- diagnostic
  // material, the same ruling Expert mode's probe table already applies to
  // a probe's raw failure reason.
  source.textContent = run.source;
  const outcome = document.createElement("td");
  outcome.textContent = translate(outcomeKey(run.exit_code),
    { code: run.exit_code, seconds: (run.duration_ms || 0) / 1000 });
  row.append(when, what, source, outcome);
  return row;
}

function auditHead() {
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const key of ["when", "what", "source", "outcome"]) {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = translate(`ui.actions.audit.${key}`);
    headRow.append(cell);
  }
  head.append(headRow);
  return head;
}

function auditNoticeRow(key) {
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = 4;
  cell.className = "text-body-secondary";
  cell.textContent = translate(key);
  row.append(cell);
  return row;
}

// Pure DOM builder over an already-fetched list of runs -- mirrors
// renderActionRows() above and expert.js's renderProbeTable(state). An
// empty `runs` array reaching here is a genuinely empty audit log; a fetch
// failure is rendered separately, through renderAuditNotice(), precisely
// so the two are never confused -- "nothing has been run yet" is a factual
// claim this console must not make at a moment it does not actually know.
export function renderAuditRows(runs) {
  const table = el("audit-table");
  clear(table);
  table.append(auditHead());
  const body = document.createElement("tbody");
  if (runs.length === 0) {
    body.append(auditNoticeRow("ui.actions.audit.empty"));
  } else {
    for (const run of runs) body.append(auditRow(run));
  }
  table.append(body);
}

function renderAuditNotice(key) {
  const table = el("audit-table");
  clear(table);
  table.append(auditHead());
  const body = document.createElement("tbody");
  body.append(auditNoticeRow(key));
  table.append(body);
}

async function fetchRuns() {
  const body = await fetchJson("/api/actions/runs");
  return Array.isArray(body.runs) ? body.runs : [];
}

export async function renderAuditTable() {
  try {
    renderAuditRows(await fetchRuns());
  } catch (error) {
    console.warn("action audit unavailable", error);
    renderAuditNotice("ui.error.action.failed");
  }
}

function appendOutputLine(text) {
  const pane = el("action-output");
  pane.textContent = pane.textContent ? `${pane.textContent}\n${text}` : text;
}

// Never an empty panel: a button that appears to do nothing is worse than
// one that reports a failure. Buttons are re-enabled immediately -- no run
// was ever accepted, so there is nothing left to wait for.
function reportActionError(key) {
  appendOutputLine(translate(key));
  renderActionRows(actionsCache, false);
}

async function runAction(actionId) {
  if (!actionId) return;
  clear(el("action-output"));
  // Disable every Run button for the round trip too, not only once a
  // run_id comes back -- otherwise a second click racing the POST could
  // start a second run before this one's 202 has even arrived.
  renderActionRows(actionsCache, true);
  appendOutputLine(translate("ui.actions.running"));
  let response;
  try {
    response = await fetch(`/api/actions/${encodeURIComponent(actionId)}`,
      { method: "POST", headers: authHeaders() });
  } catch (error) {
    console.warn("action request failed", error);
    reportActionError("ui.error.action.failed");
    return;
  }
  if (response.status !== 202) {
    reportActionError(errorKeyForStatus(response.status));
    return;
  }
  try {
    const body = await response.json();
    activeRunId = body.run_id;
  } catch (error) {
    reportActionError("ui.error.action.failed");
  }
}

async function finishRun(event) {
  activeRunId = null;
  let runs = [];
  let auditFailed = false;
  try {
    runs = await fetchRuns();
  } catch (error) {
    // The run just finished; a blank or "nothing has been run yet" audit
    // table right now would both be wrong in the same way renderActions()'s
    // silence was -- reconcileRun() below still has the finished event's
    // own fields to fall back on for the outcome line itself.
    console.warn("action audit unavailable", error);
    auditFailed = true;
  }
  const { exitCode, durationMs, output } = reconcileRun(event, runs);
  // Reconcile against the audit row: the live pane is a view, not a
  // transcript, and an `output` event can be dropped under flood.
  if (output !== null) el("action-output").textContent = output;
  appendOutputLine(translate(outcomeKey(exitCode),
    { code: exitCode, seconds: (durationMs || 0) / 1000 }));
  if (auditFailed) renderAuditNotice("ui.error.action.failed");
  else renderAuditRows(runs);
  renderActionRows(actionsCache, false);
}

export function onActionEvent(event) {
  if (!shouldTrackEvent(event, activeRunId)) return;
  if (event.phase === "output" && event.line) appendOutputLine(event.line);
  if (event.phase === "finished") finishRun(event);
}

export function paintActionsChrome() {
  el("mode-actions").textContent = translate("ui.mode.actions");
  el("actions-available-heading").textContent = translate("ui.actions.available");
  el("actions-output-heading").textContent = translate("ui.actions.output");
  el("actions-audit-heading").textContent = translate("ui.actions.audit");
  el("action-confirm-title").textContent = translate("ui.actions.confirm.title");
  el("action-confirm-cancel").textContent = translate("ui.actions.confirm.cancel");
  el("action-confirm-go").textContent = translate("ui.actions.confirm.go");
  if (wired) return;
  wired = true;
  // B1 confirms every action, including the one safe action it ships:
  // gating the confirmation dialogue on risk would be defensible UX in a
  // catalogue with several risk levels, but B1's only action is "safe", so
  // gating on risk would ship the dialogue having never once been
  // exercised. Revisit once the catalogue holds more than one risk level
  // (see healthconsole/actions.py's Risk enum).
  el("action-confirm-go").addEventListener("click", () => runAction(pendingActionId));
}
