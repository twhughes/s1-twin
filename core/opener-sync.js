// core/opener-sync.js — keep the music app's S-1 twin sounding like this page.
//
// Tyler, 2026-10-04: "is there any way to use my new S-1 twin page to sync the sound to the music
// page?" The music app (hq/music) opens this page in a window of its own with "#sync=music"; while
// that window is open, every knob value goes back to it: the whole set once, then each change as it
// happens, from any source (a knob, a key, the S-1 itself when one is linked).
//
//   → {type: "s1-twin:values", v: 1, values: {cc: value}}   once, at start
//   → {type: "s1-twin:param",  v: 1, cc, value}             on every change
//
// One way only: this page is where the sound is made, the music app plays it. Nothing is received,
// so nothing outside can change this page or a linked S-1. The values are knob positions, nothing
// private, so they go to the opener whatever its origin ("*"); the music app checks who sent them.

export const SYNC_FLAG = "sync";
export const SYNC_PEER = "music";

/**
 * Start the sync when this page was opened by the music app (a window.opener and #sync=music).
 * Returns {stop()} while it runs, else null.
 */
export function startOpenerSync(ctx, { flags, win = globalThis.window } = {}) {
  const opener = win && win.opener;
  if (!opener || !flags || flags.get(SYNC_FLAG) !== SYNC_PEER) return null;
  const send = (msg) => {
    try {
      if (opener.closed) return false;
      opener.postMessage({ v: 1, ...msg }, "*");
      return true;
    } catch { return false; }        // the music page went away or navigated off
  };
  send({ type: "s1-twin:values", values: Object.fromEntries(ctx.params) });
  const off = ctx.on("param", ({ cc, value }) => { send({ type: "s1-twin:param", cc, value }); });
  return { stop: off };
}
