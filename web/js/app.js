// Start-up and control wiring. Everything else lives in its own module.

import { el } from "./dom.js";
import {
  adoptTokenFromUrl, authHeaders, loadFallback, pickLocale, setLocale,
  translate, whenLocaleChanges,
} from "./i18n.js";
import { connect, lastKnownState, markFreshness,
         setInterfaceTextUnavailable } from "./stream.js";
import { renderSimple } from "./simple.js";

const DEFAULT_MODE = "simple";

function paintChrome() {
  document.title = translate("ui.title");
  el("app-title").textContent = translate("ui.title");
  el("mode-simple").textContent = translate("ui.mode.simple");
  el("mode-expert").textContent = translate("ui.mode.expert");
  el("mode-group").setAttribute("aria-label", translate("ui.mode.group"));
  el("locale-label").textContent = translate("ui.language");
  el("score-label").textContent = translate("ui.score.label");
}

function switchMode(mode) {
  const simple = mode === "simple";
  el("simple").hidden = !simple;
  el("expert").hidden = simple;
  el("mode-simple").setAttribute("aria-selected", String(simple));
  el("mode-expert").setAttribute("aria-selected", String(!simple));
  localStorage.setItem("mode", mode);
}

function render(state) { renderSimple(state); }

async function start() {
  adoptTokenFromUrl();
  el("mode-simple").addEventListener("click", () => switchMode("simple"));
  el("mode-expert").addEventListener("click", () => switchMode("expert"));
  el("locale").addEventListener("change", (event) =>
    setLocale(event.target.value));
  switchMode(localStorage.getItem("mode") || DEFAULT_MODE);
  whenLocaleChanges(() => {
    paintChrome();
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

globalThis.healthConsole = { render, setLocale };
start();
