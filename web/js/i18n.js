// Translation catalogue, locale-aware formatting, and the auth token
// handoff that every fetch() (including the catalogue's own) needs.

import { el } from "./dom.js";

export const FALLBACK_LOCALE = "en";
export const AVAILABLE_LOCALES = ["en", "fr"];
const TOKEN_STORAGE_KEY = "health_token";

let catalogue = {};
let fallback = {};
let locale = FALLBACK_LOCALE;
let numberFormat = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
let timeFormat = new Intl.DateTimeFormat(locale,
  { hour: "2-digit", minute: "2-digit" });

export function currentLocale() { return locale; }
export function formatNumber(value) { return numberFormat.format(value); }
export function formatTime(date) { return timeFormat.format(date); }

// setLocale() previously called paintChrome() and render() directly. It now
// notifies a listener instead, so i18n.js does not depend on the modules
// that render -- which would be a cycle.
let onLocaleChange = () => {};
export function whenLocaleChanges(handler) { onLocaleChange = handler; }

// A ?k=<token> link (shared or bookmarked) is the only way a plain
// navigation can authenticate. The server hands that token off to a
// session cookie on its response (see healthconsole/server.py), which
// covers every subsequent same-origin request automatically -- including
// EventSource, which cannot set a custom header at all. Reading and
// storing it here too, and sending it as X-Health-Token on our own
// fetch() calls, is belt-and-braces for the window before that cookie
// lands. The token is stripped from the visible URL immediately so it
// does not linger in the address bar, browser history or a bookmark.
export function adoptTokenFromUrl() {
  const url = new URL(location.href);
  const token = url.searchParams.get("k");
  if (!token) return;
  localStorage.setItem(TOKEN_STORAGE_KEY, token);
  url.searchParams.delete("k");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
}

export function authHeaders() {
  const token = localStorage.getItem(TOKEN_STORAGE_KEY);
  return token ? { "X-Health-Token": token } : {};
}

export function pickLocale() {
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

// Declared byte-valued finding parameters (healthconsole/findings.py's
// FINDING_PARAMS) are formatted through formatBytes before interpolation,
// never as a bare number of bytes -- spec §10.1 wants "441 GB left", not a
// raw byte count or a percentage alone.
const BYTE_FINDING_PARAMS = new Set(["available_bytes", "swap_used_bytes"]);

export function formatFindingParams(params) {
  const formatted = {};
  for (const [key, value] of Object.entries(params || {})) {
    formatted[key] = (BYTE_FINDING_PARAMS.has(key) && typeof value === "number")
      ? formatBytes(value)
      : value;
  }
  return formatted;
}

async function loadCatalogue(target) {
  const response = await fetch(`/static/i18n/${target}.json`,
    { headers: authHeaders() });
  if (!response.ok) {
    throw new Error(`catalogue unavailable: ${target} (${response.status})`);
  }
  return response.json();
}

export async function loadFallback() {
  fallback = await loadCatalogue(FALLBACK_LOCALE);
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
  onLocaleChange();
}
