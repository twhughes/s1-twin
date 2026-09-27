// vendored from hq/panel/keyhint/keyhint.js (the HQ canonical copy, 2026-08-16); the code below is
// unchanged. Themed only through the --kh-* custom properties, set from the design tokens in
// core/app.css (which also drops the drop shadow: DIRECTION.md allows none).

/* keyhint.js — CANONICAL keyboard-hint bar for HQ apps.
 *
 * ── Why this file exists ─────────────────────────────────────────────────
 * An always-visible bottom-right keyboard-hint bar had been reinvented FIVE
 * times across HQ apps (see CLAUDE.md "Sharing rule" / PORTS.md). Built once in desk (2026-08-05); desk was archived 2026-08-16, so the canon
 * moved to panel/keyhint/ (the control plane never gets archived). This is
 * the version to copy.
 *
 * ── How to reuse (the sharing rule: vendor a copy, don't import) ──────────
 *   1. Copy keyhint.js + keyhint.css into your app's static dir.
 *   2. Keep a provenance comment:  // vendored from hq/panel/keyhint/keyhint.js
 *   3. <link rel="stylesheet" href="keyhint.css"> and
 *      <script src="keyhint.js"></script>  (no build step, no deps, no CDN).
 *
 * ── Contract ─────────────────────────────────────────────────────────────
 *   KeyHint.set(items)   items = [{key, label}, ...]  render/replace the bar.
 *   KeyHint.clear()      empty the bar.
 *   KeyHint.hide()       KeyHint.show()   toggle visibility.
 *   KeyHint.dim(on)      fade it (e.g. while a text field has focus).
 * The bar mounts itself into <body> on first use; no markup required.
 * Zero global side effects beyond the single window.KeyHint object and one
 * <div class="keyhint"> element. It does NOT bind any keys — your app owns the
 * keydown handler; this only renders the legend. That separation is the point:
 * one place draws the hints, each app decides what the keys do.
 */
(function (global) {
  "use strict";
  let el = null;

  function mount() {
    if (el) return el;
    el = document.createElement("div");
    el.className = "keyhint";
    el.setAttribute("role", "status");
    el.setAttribute("aria-label", "keyboard shortcuts");
    (document.body || document.documentElement).appendChild(el);
    return el;
  }

  function set(items) {
    const node = mount();
    node.hidden = false;
    node.replaceChildren();
    (items || []).forEach(function (it) {
      if (!it || !it.key) return;
      const wrap = document.createElement("span");
      wrap.className = "kh-item";
      const k = document.createElement("span");
      k.className = "kh-key";
      k.textContent = it.key;
      const l = document.createElement("span");
      l.className = "kh-label";
      l.textContent = it.label || "";
      wrap.append(k, l);
      node.appendChild(wrap);
    });
  }

  const KeyHint = {
    set: set,
    clear: function () { set([]); },
    hide: function () { if (el) el.hidden = true; },
    show: function () { if (el) el.hidden = false; },
    dim: function (on) { mount().classList.toggle("kh-dim", on !== false); },
  };

  global.KeyHint = KeyHint;
})(window);
