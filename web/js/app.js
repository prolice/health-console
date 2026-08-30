// Start-up and control wiring. Everything else lives in its own module.

import { el } from "./dom.js";
import {
  adoptTokenFromUrl, authHeaders, formatBytes, loadFallback, pickLocale,
  setLocale, translate, whenLocaleChanges,
} from "./i18n.js";
import { connect, isMeasurementStale, lastKnownState, markFreshness,
         noteState, setInterfaceTextUnavailable } from "./stream.js";
import { forceGaugeRedraw, renderSimple } from "./simple.js";

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
  el("simple").hidden = !simple;
  el("expert").hidden = simple;
  el("mode-simple").setAttribute("aria-selected", String(simple));
  el("mode-expert").setAttribute("aria-selected", String(!simple));
  localStorage.setItem("mode", mode);
}

function render(state) {
  noteState(state);
  renderSimple(state);
  markFreshness(isMeasurementStale(state));
}

async function start() {
  adoptTokenFromUrl();
  el("mode-simple").addEventListener("click", () => switchMode("simple"));
  el("mode-expert").addEventListener("click", () => switchMode("expert"));
  el("locale").addEventListener("change", (event) =>
    setLocale(event.target.value));
  switchMode(localStorage.getItem("mode") || DEFAULT_MODE);
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
    const state = lastKnownState();
    if (state) render(state);
  });
  // The verdict gauge skips its own redraw when the score has not moved
  // (see simple.js's gaugeSignature()), so a theme flip alone would leave
  // it in the old theme's colours -- forceGaugeRedraw() clears that memo
  // before the re-render picks the new one up. Task 9 registers its own
  // whenThemeChanges handler for the Expert chart grid the same way.
  whenThemeChanges(() => {
    forceGaugeRedraw();
    const state = lastKnownState();
    if (state) render(state);
  });
  try {
    await loadFallback();
    await setLocale(pickLocale());
    try {
      render(await (await fetch("/api/now", { headers: authHeaders() })).json());
    } catch (error) {
      console.warn("initial state unavailable", error);
    }
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
  connect({ onState: render, onFreshness: markFreshness });
}

globalThis.healthConsole = { translate, render, setLocale, formatBytes };
start();
