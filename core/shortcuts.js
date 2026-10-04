// core/shortcuts.js — the page's keyboard shortcuts (docs/design/ROUND2.md §2): one global keydown
// handler, created by the shell. Keys never act while you type (input, textarea, select,
// contenteditable) or while Cmd, Ctrl or Alt is held; a key that a focused control already handled
// (the roll's arrows and Enter, a knob's arrows, the Match well's Space) is left to that control.
//
//   Space        play or pause the sequence (every view)    ⇧ Space   stop, back to step 1
//   1  2  3      go to Synth, Sequencer, Match              ?         this list of keys (Esc closes it)
//   −  =         tempo down and up by 1 BPM, with ⇧ by 10 (Sequencer)
//   Delete       delete the selected note (Sequencer; the view binds it with handle("delete", fn))
// Space never also clicks a focused button: its keydown and its keyup both prevent the default.
// The keys that play the synth (A to K, Z and X) live in core/keys.js; the roll's own keys in
// views/sequencer.js; Esc on a drawer in app.js.
//
//   const keys = createShortcuts(ctx, {go(id), view() -> id, onSheet(open)})   the shell's one
//   keys.handle(action, fn) -> off    a view's own action; fn() returns true when it acted
//   keys.sheet                        {open, show(), hide(), toggle()}: the list of keys
//   keys.destroy()
// The pure parts (actionFor, SHEET, the typing guard) run in node: core/shortcuts.check.mjs.

import { typing } from "./keys.js";

export { typing };
export const VIEW_KEYS = { 1: "synth", 2: "sequencer", 3: "match" };
export const isSpace = (e) => e.key === " " || e.key === "Spacebar" || e.code === "Space";

/** The action a key press asks for, or null: {action: "toggle" | "stop" | "sheet" | "view" | "tempo" |
 *  "delete", id?, delta?}. `view` is the view on screen; while the list of keys (`sheet`) is open only
 *  the transport and ? act. The handler checks typing and keys a control already took. */
export function actionFor(e, { view = "", sheet = false } = {}) {
  if (!e || e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return null;
  if (isSpace(e)) return { action: e.shiftKey ? "stop" : "toggle" };
  if (e.key === "?") return { action: "sheet" };
  if (sheet) return null;
  if (VIEW_KEYS[e.key]) return { action: "view", id: VIEW_KEYS[e.key] };
  if (view === "sequencer") {
    // With Shift a US keyboard sends _ and + for these keys; the keypad's − and + come without it.
    if (e.key === "-" || e.key === "_" || e.key === "−") return { action: "tempo", delta: e.shiftKey ? -10 : -1 };
    if (e.key === "=" || e.key === "+") return { action: "tempo", delta: e.shiftKey ? 10 : 1 };
    if (e.key === "Delete" || e.key === "Backspace") return { action: "delete" };
  }
  return null;
}

/** The list of keys, grouped by where they act. `keys` are key caps; `join` stands between them. */
export const SHEET = [
  { title: "Everywhere", rows: [
    { keys: ["Space"], does: "Play or pause the sequence" },
    { keys: ["⇧ Space"], does: "Stop, back to step 1" },
    { keys: ["1", "2", "3"], does: "Go to Synth, Sequencer or Match" },
    { keys: ["?"], does: "Show this list of keys" },
    { keys: ["Esc"], does: "Close a drawer or this list" },
  ] },
  { title: "Synth and Match", rows: [
    { keys: ["A", "K"], join: "to", does: "Play notes (the row above plays the black keys)" },
    { keys: ["Z", "X"], join: "and", does: "Octave down and up" },
  ] },
  { title: "Sequencer", rows: [
    { keys: ["−", "="], join: "and", does: "Tempo down and up by 1 BPM; with ⇧, by 10" },
    { keys: ["Delete"], does: "Delete the selected note" },
    { keys: ["←", "↑", "→", "↓"], does: "Move in the piano roll; with ⇧, up or down an octave" },
    { keys: ["Enter"], does: "Add a note in the roll, or select the one there" },
    { keys: ["[", "]"], join: "and", does: "Make the selected note shorter or longer" },
    { keys: ["Esc"], does: "Deselect the note" },
  ] },
];

// ── the list of keys: a small centered dialog (tokens only) ──────────────────────────────
const CSS = `
.keys-sheet { width: min(600px, calc(100vw - 32px)); max-height: calc(100vh - 48px); padding: 0; overflow: auto;
  background: var(--lift); color: var(--ink); border: 1px solid var(--ink-3); border-radius: 3px; }
.keys-sheet::backdrop { background: color-mix(in srgb, var(--deep) 62%, transparent); }
.keys-sheet .ks-body { position: relative; padding: 26px 30px 30px; }
.keys-sheet h2 { font: italic 400 26px/1.1 var(--serif); margin: 0 90px 4px 0; }
.keys-sheet .ks-close { position: absolute; top: 24px; right: 28px; }
.keys-sheet .note { margin: 0; max-width: none; }
.keys-sheet h3 { font: italic 400 19px/1.2 var(--serif); margin: 24px 0 10px; }
.keys-sheet dl { display: grid; grid-template-columns: minmax(8.5em, max-content) minmax(0, 1fr); gap: 9px 22px; margin: 0; align-items: baseline; }
.keys-sheet dt { display: flex; flex-wrap: wrap; align-items: baseline; gap: 5px; color: var(--ink-2); font-size: 12.5px; }
.keys-sheet dd { margin: 0; font-size: 13.5px; line-height: 1.4; }
.keys-sheet kbd { display: inline-block; min-width: 1.9em; padding: 3px 7px; text-align: center;
  font: 600 12px/1.2 var(--sans); color: var(--ink); background: var(--deep);
  border: 1px solid var(--ink-3); border-radius: 4px; }
@media (max-width: 640px) {
  .keys-sheet .ks-body { padding: 22px 18px 24px; }
  .keys-sheet dl { grid-template-columns: minmax(0, 1fr); gap: 4px; }
  .keys-sheet dd { margin-bottom: 8px; }
}
`;

const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};

/** The dialog, built on first use. Focus moves to its Close button and back to where it was; Esc, the
 *  Close button and a click outside close it. A modal <dialog>: the page behind cannot take focus. */
function createSheet({ onToggle = () => {} } = {}) {
  let dialog = null, closeBtn = null, opener = null;
  function build() {
    const style = el("style");
    style.dataset.part = "shortcuts";
    style.textContent = CSS;
    document.head.append(style);
    dialog = el("dialog", "keys-sheet");
    dialog.setAttribute("aria-labelledby", "keys-sheet-title");
    const body = el("div", "ks-body");
    closeBtn = el("button", "quiet ks-close", "Close");
    closeBtn.type = "button";
    closeBtn.addEventListener("click", () => hide());
    const title = el("h2", null, "Keys");
    title.id = "keys-sheet-title";
    body.append(closeBtn, title, el("p", "note", "None of them act while you type in a box."));
    for (const group of SHEET) {
      const dl = el("dl");
      for (const row of group.rows) {
        const dt = el("dt");
        row.keys.forEach((k, i) => {
          if (i && row.join) dt.append(el("span", null, row.join));
          dt.append(el("kbd", null, k));
        });
        dl.append(dt, el("dd", null, row.does));
      }
      const section = el("section");
      section.append(el("h3", null, group.title), dl);
      body.append(section);
    }
    dialog.append(body);
    dialog.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();                 // Esc closes this list, not a drawer under it
      hide();
    });
    dialog.addEventListener("cancel", (e) => { e.preventDefault(); hide(); });
    dialog.addEventListener("click", (e) => { if (e.target === dialog) hide(); });   // outside the panel
    document.body.append(dialog);
  }
  function show() {
    if (!dialog) build();
    if (dialog.open) return;
    opener = document.activeElement;
    if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
    closeBtn.focus();
    onToggle(true);
  }
  function hide() {
    if (!dialog || !dialog.open) return;
    if (typeof dialog.close === "function") dialog.close(); else dialog.removeAttribute("open");
    onToggle(false);
    if (opener && opener.isConnected && typeof opener.focus === "function") opener.focus({ preventScroll: true });
    opener = null;
  }
  return {
    get open() { return Boolean(dialog && dialog.open); },
    show,
    hide,
    toggle() { if (dialog && dialog.open) hide(); else show(); },
  };
}

// ── the handler ──────────────────────────────────────────────────────────────────────────
export function createShortcuts(ctx, { go = () => {}, view = () => "", onSheet = () => {}, target = globalThis.window, sheet = null } = {}) {
  const handlers = new Map();
  const panel = sheet || createSheet({ onToggle: onSheet });
  let claimed = false;                     // we acted on this Space: its keyup must not click a button either

  function onKeyDown(e) {
    if (isSpace(e)) claimed = false;
    if (e.defaultPrevented || typing(e.target)) return;
    const a = actionFor(e, { view: view(), sheet: panel.open });
    if (!a) return;
    if (a.action === "toggle" || a.action === "stop") {
      e.preventDefault();
      claimed = true;
      if (e.repeat) return;                // holding Space does not flip it again
      if (a.action === "stop") ctx.transport?.stop(); else ctx.transport?.toggle();
      return;
    }
    if (e.repeat && a.action !== "tempo") { e.preventDefault(); return; }   // holding − keeps lowering
    switch (a.action) {
      case "view": go(a.id); break;
      case "sheet": panel.toggle(); break;
      case "tempo":
        if (!ctx.transport) return;
        ctx.transport.nudgeTempo(a.delta);
        break;
      case "delete": {
        const fn = handlers.get("delete");
        if (!fn || !fn()) return;          // nothing selected: the key is left alone
        break;
      }
      default: return;
    }
    e.preventDefault();
  }
  function onKeyUp(e) {
    if (!claimed || !isSpace(e)) return;
    claimed = false;
    e.preventDefault();
  }
  target.addEventListener("keydown", onKeyDown);
  target.addEventListener("keyup", onKeyUp);

  return {
    sheet: panel,
    handle(action, fn) {
      handlers.set(action, fn);
      return () => { if (handlers.get(action) === fn) handlers.delete(action); };
    },
    destroy() {
      target.removeEventListener("keydown", onKeyDown);
      target.removeEventListener("keyup", onKeyUp);
      panel.hide();
      handlers.clear();
    },
  };
}
