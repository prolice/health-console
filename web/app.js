// Simple mode. No dependency, no outbound network, no wording in the code.

const FALLBACK_LOCALE = "en";
const AVAILABLE_LOCALES = ["en", "fr"];
const DEFAULT_MODE = "simple";
const STALE_AFTER_MS = 15000;

let catalogue = {};
let fallback = {};
let locale = FALLBACK_LOCALE;
let numberFormat = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
let timeFormat = new Intl.DateTimeFormat(locale,
  { hour: "2-digit", minute: "2-digit" });
let lastState = null;
let lastUpdate = Date.now();
let isStale = false;

function el(id) { return document.getElementById(id); }

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
  const response = await fetch(`/static/i18n/${target}.json`);
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

export function render(state) {
  lastState = state;
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
  markFreshness(isStale);
}

function markFreshness(stale) {
  // While stale, the banner must keep showing the last real update time,
  // never the timestamp of whatever just got (re)painted — a locale
  // switch during an outage must repaint the stale message, not erase it.
  const zone = el("freshness");
  const milliseconds = stale ? lastUpdate : lastState.ts * 1000;
  const time = timeFormat.format(new Date(milliseconds));
  zone.classList.toggle("stale", stale);
  document.body.classList.toggle("stale", stale);
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
      markFreshness(true);
    }
  }, 5000);
}

async function start() {
  el("mode-simple").addEventListener("click", () => switchMode("simple"));
  el("mode-expert").addEventListener("click", () => switchMode("expert"));
  el("locale").addEventListener("change", (event) =>
    setLocale(event.target.value));
  switchMode(localStorage.getItem("mode") || DEFAULT_MODE);
  try {
    fallback = await loadCatalogue(FALLBACK_LOCALE);
    await setLocale(pickLocale());
    try {
      render(await (await fetch("/api/now")).json());
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
    el("freshness").textContent =
      "Interface text failed to load. Please reload the page.";
  }
  connect();
}

globalThis.healthConsole = { translate, render, setLocale, formatBytes };
start();
