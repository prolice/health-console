// Simple mode rendering. No dependency, no outbound network, no wording in
// the code -- every string comes from the active catalogue via translate().

import { el, setText } from "./dom.js";
import { translate, formatFindingParams } from "./i18n.js";
import { hasMeasurement } from "./stream.js";

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

function renderNoMeasurement(state) {
  el("verdict-icon").textContent = "";
  setText(el("verdict-word"), translate("ui.state.no_measurement"));
  setText(el("verdict-sentence"), translate("ui.state.no_measurement.detail"));
  setText(el("score"), "—");
  el("findings").textContent = "";
  el("raw").textContent = JSON.stringify(state, null, 2);
}

export function renderSimple(state) {
  if (!hasMeasurement(state)) {
    renderNoMeasurement(state);
    return;
  }

  const severity = state.severity || "OK";
  el("verdict-icon").textContent = translate(`severity.${severity}.icon`);
  setText(el("verdict-word"), translate(`severity.${severity}.word`));
  setText(el("verdict-sentence"), translate(`verdict.${severity}`));
  setText(el("score"), String(state.score));

  const host = el("findings");
  host.textContent = "";
  for (const finding of state.findings || []) {
    const params = formatFindingParams(finding.params);
    host.append(card(
      finding.severity,
      translate(`finding.${finding.id}.title`, params),
      translate(`finding.${finding.id}.why`, params)));
  }

  // An unavailable probe is shown as unavailable, never as a reassuring zero.
  // A probe whose evaluate() raised keeps status "ok" (the reading itself
  // succeeded) but carries eval_error, and that must not stay invisible.
  for (const [name, probe] of Object.entries(state.probes || {})) {
    if (probe.status === "ok" && !probe.eval_error) continue;
    const article = card("INFO",
      translate("ui.probe.unavailable", { probe: name }),
      translate("ui.probe.unavailable.why"));
    // probe.reason/eval_error is diagnostic material in English -- see
    // healthconsole/probes/__init__.py's unavailable() -- never translated
    // prose. It stays visible (hiding it would be its own dishonesty) but
    // is rendered as a visually secondary line behind a translated lead-in,
    // rather than as the card's whole explanation.
    const raw = probe.reason || probe.eval_error || "";
    if (raw) {
      const detail = document.createElement("p");
      detail.className = "raw-detail";
      detail.textContent =
        `${translate("ui.probe.unavailable.raw_prefix")} ${raw}`;
      article.append(detail);
    }
    host.append(article);
  }

  el("raw").textContent = JSON.stringify(state, null, 2);
}
