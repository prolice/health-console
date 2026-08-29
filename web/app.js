// Simple mode. No dependency, no outbound network, no wording in the code.

const FALLBACK_LOCALE = "en";
const AVAILABLE_LOCALES = ["en", "fr"];
const DEFAULT_MODE = "simple";
// No SSE event at all for this long: the stream itself is presumed down.
const STALE_AFTER_MS = 15000;
// A measurement older than this is stale even while the stream stays open --
// set well above the 2 s live cadence so ordinary scheduling jitter or a
// single delayed tick never flaps the banner, but a wedged collector that
// keeps re-emitting the same state (see EMPTY_STATE / scheduler.state())
// is still caught rather than shown as "up to date" forever.
const MEASUREMENT_STALE_AFTER_SECONDS = 30;
const TOKEN_STORAGE_KEY = "health_token";

let catalogue = {};
let fallback = {};
let locale = FALLBACK_LOCALE;
let numberFormat = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
let timeFormat = new Intl.DateTimeFormat(locale,
  { hour: "2-digit", minute: "2-digit" });
let lastState = null;
let lastUpdate = Date.now();
let isStale = false;
let interfaceTextUnavailable = false;

function el(id) { return document.getElementById(id); }

// A ?k=<token> link (shared or bookmarked) is the only way a plain
// navigation can authenticate. The server hands that token off to a
// session cookie on its response (see healthconsole/server.py), which
// covers every subsequent same-origin request automatically -- including
// EventSource, which cannot set a custom header at all. Reading and
// storing it here too, and sending it as X-Health-Token on our own
// fetch() calls, is belt-and-braces for the window before that cookie
// lands. The token is stripped from the visible URL immediately so it
// does not linger in the address bar, browser history or a bookmark.
function adoptTokenFromUrl() {
  const url = new URL(location.href);
  const token = url.searchParams.get("k");
  if (!token) return;
  localStorage.setItem(TOKEN_STORAGE_KEY, token);
  url.searchParams.delete("k");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
}

function authHeaders() {
  const token = localStorage.getItem(TOKEN_STORAGE_KEY);
  return token ? { "X-Health-Token": token } : {};
}

function pickLocale() {
  const stored = localStorage.getItem("locale");
  if (stored && AVAILABLE_LOCALES.includes(stored)) return stored;
  for (const candidate of navigator.languages || []) {
    const short = candidate.split("-")[0];
    if (AVAILABLE_LOCALES.includes(short)) return short;
  }
  return FALLBACK_LOCALE;
}

export function translate(key, params = {}) {
  // A key missing from the active catalogue falls back to English rather than
  // showing the raw key to the user.
  let text = catalogue[key];
  if (text === undefined) {
    text = fallback[key];
    if (text === undefined) {
      console.warn(`missing catalogue key: ${key}`);
      return "";
    }
    console.warn(`key missing from ${locale}, using ${FALLBACK_LOCALE}: ${key}`);
  }
  return text.replace(/\{(\w+)\}/g, (whole, name) =>
    name in params ? formatValue(params[name]) : whole);
}

function formatValue(value) {
  return typeof value === "number" ? numberFormat.format(value) : String(value);
}

export function formatBytes(bytes) {
  const units = ["B", "kB", "MB", "GB", "TB"];
  let index = 0;
  let value = bytes;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${numberFormat.format(value)} ${units[index]}`;
}

async function loadCatalogue(target) {
  const response = await fetch(`/static/i18n/${target}.json`,
    { headers: authHeaders() });
  if (!response.ok) {
    throw new Error(`catalogue unavailable: ${target} (${response.status})`);
  }
  return response.json();
}

export async function setLocale(target) {
  const requested = AVAILABLE_LOCALES.includes(target) ? target : FALLBACK_LOCALE;
  if (requested === FALLBACK_LOCALE) {
    catalogue = fallback;
    locale = FALLBACK_LOCALE;
  } else {
    try {
      catalogue = await loadCatalogue(requested);
      locale = requested;
    } catch (error) {
      // A broken non-default catalogue must not take the page down: fall
      // back to the already-loaded English catalogue instead of aborting.
      console.warn(`falling back to ${FALLBACK_LOCALE} after ${requested} failed to load`, error);
      catalogue = fallback;
      locale = FALLBACK_LOCALE;
    }
  }
  numberFormat = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
  timeFormat = new Intl.DateTimeFormat(locale,
    { hour: "2-digit", minute: "2-digit" });
  localStorage.setItem("locale", locale);
  document.documentElement.lang = locale;
  el("locale").value = locale;
  paintChrome();
  if (lastState) render(lastState);
}

function paintChrome() {
  document.title = translate("ui.title");
  el("app-title").textContent = translate("ui.title");
  el("mode-simple").textContent = translate("ui.mode.simple");
  el("mode-expert").textContent = translate("ui.mode.expert");
  el("mode-group").setAttribute("aria-label", translate("ui.mode.group"));
  el("locale-label").textContent = translate("ui.language");
  el("score-label").textContent = translate("ui.score.label");
  el("expert-placeholder").textContent = translate("ui.expert.placeholder");
}

function card(severity, titleText, whyText) {
  const article = document.createElement("article");
  article.className = `finding severity-${severity}`;
  const tag = document.createElement("p");
  tag.className = "tag";
  // Colour never carries the state alone: icon and word travel together.
  tag.textContent = `${translate(`severity.${severity}.icon`)} `
    + translate(`severity.${severity}.word`);
  const heading = document.createElement("h2");
  heading.textContent = titleText;
  const why = document.createElement("p");
  why.textContent = whyText;
  article.append(tag, heading, why);
  return article;
}

// A state with no ts/score is not a measurement of anything -- it is the
// scheduler's EMPTY_STATE, seen before the first tick completes (or, in
// principle, if the collector never runs at all). Showing a score or a
// severity for it would be exactly the reassuring lie this console refuses
// to tell.
function hasMeasurement(state) {
  return Boolean(state) && state.ts != null && state.score != null;
}

// Stale means either signal says so: the stream itself reporting trouble
// (isStale), or -- the defect this guards against -- a stream that keeps
// emitting events on schedule while the measurement inside them stops
// advancing, which a connectivity-only check would never notice.
function isMeasurementStale(state) {
  if (isStale) return true;
  if (!hasMeasurement(state)) return true;
  return Date.now() / 1000 - state.ts > MEASUREMENT_STALE_AFTER_SECONDS;
}

function renderNoMeasurement(state) {
  el("verdict-icon").textContent = "";
  el("verdict-word").textContent = translate("ui.state.no_measurement");
  el("verdict-sentence").textContent = translate("ui.state.no_measurement.detail");
  el("score").textContent = "—";
  el("findings").textContent = "";
  el("raw").textContent = JSON.stringify(state, null, 2);
}

export function render(state) {
  lastState = state;
  if (!hasMeasurement(state)) {
    renderNoMeasurement(state);
    markFreshness(true);
    return;
  }

  const severity = state.severity || "OK";
  el("verdict-icon").textContent = translate(`severity.${severity}.icon`);
  el("verdict-word").textContent = translate(`severity.${severity}.word`);
  el("verdict-sentence").textContent = translate(`verdict.${severity}`);
  el("score").textContent = state.score;

  const host = el("findings");
  host.textContent = "";
  for (const finding of state.findings || []) {
    host.append(card(
      finding.severity,
      translate(`finding.${finding.id}.title`, finding.params),
      translate(`finding.${finding.id}.why`, finding.params)));
  }

  // An unavailable probe is shown as unavailable, never as a reassuring zero.
  // A probe whose evaluate() raised keeps status "ok" (the reading itself
  // succeeded) but carries eval_error, and that must not stay invisible.
  for (const [name, probe] of Object.entries(state.probes || {})) {
    if (probe.status === "ok" && !probe.eval_error) continue;
    host.append(card("INFO",
      translate("ui.probe.unavailable", { probe: name }),
      probe.reason || probe.eval_error || ""));
  }

  el("raw").textContent = JSON.stringify(state, null, 2);
  markFreshness(isMeasurementStale(state));
}

function markFreshness(stale) {
  if (interfaceTextUnavailable) {
    // The catalogue never loaded, so translate() has nothing to return
    // but "". If the SSE stream still comes up despite that (a plausible
    // split: static assets down, the API up), a state event must not
    // silently blank out the one visible sign that something is wrong.
    return;
  }
  const zone = el("freshness");
  zone.classList.toggle("stale", stale);
  document.body.classList.toggle("stale", stale);
  if (!hasMeasurement(lastState)) {
    zone.textContent = translate("ui.state.no_measurement.detail");
    return;
  }
  // Always the timestamp of the actual last measurement, never the moment
  // this banner happened to (re)paint -- a locale switch, or a stream that
  // keeps ticking over a wedged collector, must not manufacture freshness
  // that was never there.
  const time = timeFormat.format(new Date(lastState.ts * 1000));
  zone.textContent = translate(
    stale ? "ui.freshness.stale" : "ui.freshness.live", { time });
}

function switchMode(mode) {
  const simple = mode === "simple";
  el("simple").hidden = !simple;
  el("expert").hidden = simple;
  el("mode-simple").setAttribute("aria-selected", String(simple));
  el("mode-expert").setAttribute("aria-selected", String(!simple));
  localStorage.setItem("mode", mode);
}

function connect() {
  const source = new EventSource("/api/stream");
  source.addEventListener("state", (event) => {
    lastUpdate = Date.now();
    isStale = false;
    render(JSON.parse(event.data));
  });
  source.addEventListener("error", () => {
    isStale = true;
    markFreshness(true);
  });
  setInterval(() => {
    if (Date.now() - lastUpdate > STALE_AFTER_MS) {
      isStale = true;
    }
    // Re-evaluated every tick, not only when the stream just went down:
    // the payload can go stale on its own between SSE events.
    markFreshness(isMeasurementStale(lastState));
  }, 5000);
}

async function start() {
  adoptTokenFromUrl();
  el("mode-simple").addEventListener("click", () => switchMode("simple"));
  el("mode-expert").addEventListener("click", () => switchMode("expert"));
  el("locale").addEventListener("change", (event) =>
    setLocale(event.target.value));
  switchMode(localStorage.getItem("mode") || DEFAULT_MODE);
  try {
    fallback = await loadCatalogue(FALLBACK_LOCALE);
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
    interfaceTextUnavailable = true;
    el("freshness").textContent =
      "Interface text failed to load. Please reload the page.";
  }
  connect();
}

globalThis.healthConsole = { translate, render, setLocale, formatBytes };
start();
