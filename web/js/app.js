// Start-up and control wiring. Everything else lives in its own module.

import { el } from "./dom.js";
import {
  adoptTokenFromUrl, authHeaders, formatBytes, loadFallback, pickLocale,
  setLocale, translate, whenLocaleChanges,
} from "./i18n.js";
import { connect, isMeasurementStale, lastKnownState, markFreshness,
         noteState, setInterfaceTextUnavailable } from "./stream.js";
import { forceGaugeRedraw, renderSimple } from "./simple.js";
import { paintRangeControl, redrawCharts, renderExpert } from "./expert.js";
import { clearCache } from "./history.js";
import { onActionEvent, paintActionsChrome, renderActions,
         renderAuditTable } from "./actions.js";

const DEFAULT_MODE = "simple";

const THEME_KEY = "theme";
const THEMES = ["auto", "light", "dark"];

export function resolvedTheme() {
  const choice = localStorage.getItem(THEME_KEY) || "auto";
  if (choice !== "auto") return choice;
  return globalThis.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark" : "light";
}

const themeListeners = [];
// Task 9 redraws charts on this: chart colours are read from CSS custom
// properties at draw time, so a chart drawn in light theme keeps light
// colours on a dark page unless it is told the theme changed.
export function whenThemeChanges(handler) { themeListeners.push(handler); }

export function applyTheme(choice) {
  const wanted = THEMES.includes(choice) ? choice : "auto";
  localStorage.setItem(THEME_KEY, wanted);
  document.documentElement.setAttribute("data-bs-theme", resolvedTheme());
  for (const id of THEMES) {
    el(`theme-${id}`).classList.toggle("active", id === wanted);
    el(`theme-${id}`).setAttribute("aria-pressed", String(id === wanted));
  }
  for (const handler of themeListeners) handler();
}

function paintChrome() {
  document.title = translate("ui.title");
  el("app-title").textContent = translate("ui.title");
  el("mode-simple").textContent = translate("ui.mode.simple");
  el("mode-expert").textContent = translate("ui.mode.expert");
  el("mode-group").setAttribute("aria-label", translate("ui.mode.group"));
  paintActionsChrome();
  el("locale-label").textContent = translate("ui.language");
  el("score-label").textContent = translate("ui.score.label");
  el("theme-group").setAttribute("aria-label", translate("ui.theme.label"));
  el("theme-auto").textContent = translate("ui.theme.auto");
  el("theme-light").textContent = translate("ui.theme.light");
  el("theme-dark").textContent = translate("ui.theme.dark");
  el("refresh").textContent = translate("ui.refresh");
  el("range-group").setAttribute("aria-label", translate("ui.range.group"));
  el("probes-heading").textContent = translate("ui.expert.probes");
  el("raw-heading").textContent = translate("ui.expert.raw");
}

function switchMode(mode) {
  const simple = mode === "simple";
  const expert = mode === "expert";
  const actions = mode === "actions";
  el("simple").hidden = !simple;
  el("expert").hidden = !expert;
  el("actions").hidden = !actions;
  el("mode-simple").setAttribute("aria-selected", String(simple));
  el("mode-expert").setAttribute("aria-selected", String(expert));
  el("mode-actions").setAttribute("aria-selected", String(actions));
  // Bootstrap styles .btn.active, never [aria-selected="true"]: without
  // this the tab was correctly announced but visually identical either
  // way. Matches applyTheme()/paintRangeButtons(), which already do this.
  el("mode-simple").classList.toggle("active", simple);
  el("mode-expert").classList.toggle("active", expert);
  el("mode-actions").classList.toggle("active", actions);
  localStorage.setItem("mode", mode);
  if (expert) {
    // render() only calls renderExpert() while #expert is already visible,
    // so the state from the very first /api/now response -- which arrived
    // while Expert was still hidden -- was never painted into the tiles,
    // the probe table or #raw. Paint it now from whatever render() has
    // already noted, rather than leaving those panels empty until the next
    // SSE tick (which, if the stream never connects at all, is never).
    const state = lastKnownState();
    if (state) renderExpert(state);
    // Charts redraw only on a range change, refresh, a theme change, and
    // here -- never on the 2s tick that renderExpert() rides along on
    // (with one exception it owns itself: see thermalZoneSignature() in
    // expert.js).
    redrawCharts();
  }
  if (actions) {
    // Neither call depends on the SSE state stream, so (unlike Expert)
    // there is nothing already noted to paint from -- both go straight to
    // the server, refreshing the catalogue's availability and the audit
    // log every time this tab is entered.
    renderActions();
    renderAuditTable();
  }
}

function render(state) {
  noteState(state);
  renderSimple(state);
  if (!el("expert").hidden) renderExpert(state);
  markFreshness(isMeasurementStale(state));
}

async function start() {
  adoptTokenFromUrl();
  el("mode-simple").addEventListener("click", () => switchMode("simple"));
  el("mode-expert").addEventListener("click", () => switchMode("expert"));
  el("mode-actions").addEventListener("click", () => switchMode("actions"));
  el("locale").addEventListener("change", (event) =>
    setLocale(event.target.value));
  el("refresh").addEventListener("click", () => {
    clearCache();
    redrawCharts();
  });
  for (const id of THEMES) {
    el(`theme-${id}`).addEventListener("click", () => applyTheme(id));
  }
  applyTheme(localStorage.getItem(THEME_KEY) || "auto");
  // "auto" must follow the system while the page is open, not only at
  // load -- but only when the reader has not pinned an explicit choice.
  // Task 9 redraws every chart on whenThemeChanges, so an OS flip must not
  // touch a theme the reader deliberately set to light or dark.
  globalThis.matchMedia("(prefers-color-scheme: dark)")
    .addEventListener("change", () => {
      if ((localStorage.getItem(THEME_KEY) || "auto") === "auto") {
        applyTheme("auto");
      }
    });
  whenLocaleChanges(() => {
    paintChrome();
    // The range buttons are translated text too, and (unlike paintChrome's
    // targets) built from scratch each time -- this also restores which
    // one is pressed from expert.js's own range state.
    paintRangeControl();
    const state = lastKnownState();
    if (state) render(state);
    // Card titles, legend labels, depth sentences and aria-labels are all
    // produced at chart-draw time, not on every render() like the rest of
    // the page -- so switching locale while Expert mode is open would
    // otherwise leave every chart in the old language until the reader
    // happens to click a range button.
    if (!el("expert").hidden) redrawCharts();
  });
  // The verdict gauge skips its own redraw when the score has not moved
  // (see simple.js's gaugeSignature()), so a theme flip alone would leave
  // it in the old theme's colours -- forceGaugeRedraw() clears that memo
  // before the re-render picks the new one up.
  whenThemeChanges(() => {
    forceGaugeRedraw();
    const state = lastKnownState();
    if (state) render(state);
  });
  // Chart colours are read from CSS custom properties at draw time (see
  // charts.js's themeColour()), so a chart already on screen keeps the old
  // theme's colours until told otherwise. Registered independently of the
  // gauge's handler above, per app.js's existing whenThemeChanges contract,
  // and only while Expert mode is actually visible.
  whenThemeChanges(() => {
    if (!el("expert").hidden) redrawCharts();
  });
  try {
    await loadFallback();
    await setLocale(pickLocale());
    try {
      render(await (await fetch("/api/now", { headers: authHeaders() })).json());
    } catch (error) {
      console.warn("initial state unavailable", error);
    }
    // Only now -- catalogue loaded, range buttons painted, and the first
    // state (if any) already noted by the render() call above -- is it safe
    // to switch into a stored "expert" mode: switchMode() redraws every
    // chart, and the thermal card's zone list is read from
    // lastKnownState() at that moment. Switching earlier would still show
    // translated text (the previous ordering bug), but the thermal card
    // would silently fall back to cpu.temp.pkg alone on every single
    // reload, indistinguishable from a machine that genuinely has no
    // sensor zones.
    switchMode(localStorage.getItem("mode") || DEFAULT_MODE);
  } catch (error) {
    // The catalogue itself is what failed here, so there is nothing left
    // to translate this sentence with: every other text node in
    // index.html starts empty, and without this literal sentence the
    // viewer would see a blank page with no sign anything is wrong. Do
    // not "fix" this back to a translate() call.
    console.warn("catalogue unavailable, interface text cannot be shown", error);
    setInterfaceTextUnavailable();
    el("freshness").textContent =
      "Interface text failed to load. Please reload the page.";
  }
  // onAction rides the same connection as onState/onFreshness -- one
  // EventSource for the whole page, never two (see stream.js's connect()).
  connect({ onState: render, onFreshness: markFreshness, onAction: onActionEvent });
}

globalThis.healthConsole = { translate, render, setLocale, formatBytes };
start();
