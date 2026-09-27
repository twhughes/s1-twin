// core/flags.js — the URL hash: "#<view>&flag&key=value" (review flags are listed at the top of app.js).
// A first part that is not a known view is a flag, so "#still" works on its own.

export const VIEW_IDS = ["synth", "sequencer", "match"];

export function readHash(hash = globalThis.location ? location.hash : "", views = VIEW_IDS) {
  const parts = String(hash).replace(/^#/, "").split("&").filter(Boolean);
  const route = parts.length && views.includes(parts[0]) ? parts.shift() : "";
  const flags = new Map(parts.map((p) => {
    const i = p.indexOf("=");
    return i < 0 ? [p, true] : [p.slice(0, i), decodeURIComponent(p.slice(i + 1))];
  }));
  return { route, flags };
}

/** True when the page should not animate: the `still` flag or the reader's motion setting. */
export function isStill(flags = readHash().flags) {
  const reduced = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
  return flags.has("still") || reduced;
}
