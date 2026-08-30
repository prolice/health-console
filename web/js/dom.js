// Element access and the write discipline the live regions depend on.

export function el(id) { return document.getElementById(id); }

// #verdict-word, #verdict-sentence, #score and #freshness sit inside two
// aria-live="polite" regions and are repainted on every SSE event (every
// 2s). Assigning textContent unconditionally makes a screen reader re-read
// the whole verdict forever, even when nothing changed -- spec §10.2 asks
// for live regions "without screen-reader chatter". Only actually writing
// when the value changed keeps the live region silent the rest of the time.
export function setText(node, text) {
  if (node.textContent !== text) node.textContent = text;
}

export function clear(node) { node.textContent = ""; }
