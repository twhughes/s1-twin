// app.js — the shell (docs/design/BUILD.md §1 W-plate, §2.2): boot, context, routes, drawers, toasts.
// The views (views/*.js) and the drawers (drawers/*.js) do the rest; the look is design/ (the kit).
//
// Review flags, in the URL hash, joined with "&" (parsed by core/flags.js):
//   #synth&demo=play       hold A2 without sound, so the pitch halos, the lit key and the caption show
//   #synth&demo=connected  pretend the S-1 is synced: a synthetic "real" signal draws the bronze plume
//                          with the twin dotted over it, and a hardware twist on Cutoff leaves the
//                          bronze knob trail. Nothing is sent to a server or a device.
//   &still                 no page-load animation (reduced motion does the same)
//   &library, &settings    open that drawer
//   #match&replay=<slug>  on the static page, start that recorded match (see matches/index.json)
//   &sync=music            opened by the music app (hq/music): send it every knob value, so its twin
//                          sounds like this page (core/opener-sync.js; one way, nothing is received)
//
// Contexts (BUILD.md §0): when GET /api/status answers, the cockpit server is here (server mode:
// the schema, the live state and the S-1 arrive over /api and /ws/state). Otherwise the page is
// static: the schema comes from core/schema.json and the browser twin is the only sound.
// The twin is twin/audio.js (W-twin) whenever it loads, else the stand-in core/twin-stub.js.
// The sequence's transport (core/transport.js) and the keyboard shortcuts (core/shortcuts.js) live for
// the page's life as ctx.transport and ctx.shortcuts, so a pattern keeps playing across the views.

import { NAME, TAGLINE, DISCLAIMER, REPO_URL, CHANGES_URL } from "./design/brand.js";
import { createCtx, VOICES_KEY } from "./core/ctx.js";
import { probe, api, wsURL, connectState, createEchoFilter, serverTransport } from "./core/server.js";
import { loadStatic, loadStaticCurves } from "./core/static.js";
import { createKeys } from "./core/keys.js";
import { createTransport } from "./core/transport.js";
import { createShortcuts } from "./core/shortcuts.js";
import { readHash } from "./core/flags.js";
import { sendPatch } from "./core/actions.js";
import { startOpenerSync } from "./core/opener-sync.js";

const VIEWS = [
  { id: "synth", title: "Synth", load: () => import("./views/synth.js") },
  { id: "sequencer", title: "Sequencer", load: () => import("./views/sequencer.js") },
  { id: "match", title: "Match", load: () => import("./views/match.js") },
];
const DRAWERS = {
  library: () => import("./drawers/library.js"),
  settings: () => import("./drawers/settings.js"),
};
const $ = (id) => document.getElementById(id);

// ── toasts ────────────────────────────────────────────────────────────────
let toastTimer = 0;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("on"), Math.max(2600, 55 * String(msg).length));
}

// ── the twin: twin/audio.js when it exists, else the stand-in ─────────────────
async function loadTwin(server) {
  let curves = null, kind = "bundled";
  if (server) {
    try {
      const r = await fetch("/api/twin/curves", { cache: "no-store" });
      if (r.ok) { curves = await r.json(); kind = r.headers.get("X-Twin-Curves") || "default"; }
    } catch { /* the twin keeps its bundled curves */ }
  } else {
    ({ curves, kind } = await loadStaticCurves());
  }
  let twin;
  try {
    twin = await (await import("./twin/audio.js")).createTwin({ curves });
  } catch {
    twin = await (await import("./core/twin-stub.js")).createTwin({ curves });
  }
  return { twin, info: { curves: kind, stub: Boolean(twin.stub) } };
}

// ── the server's live events ─────────────────────────────────────────────────
function onServer(ctx, m, echo, setStatus) {
  switch (m.type) {
    case "hello":
      ctx._receiveAll(m.params, "patch");
      setStatus({ ...m.status, server: "up" });
      break;
    case "param":
      if (!(m.source === "ui" && echo.isEcho(m.cc, m.value))) ctx._receive(m.cc, m.value, m.source || "ui");
      break;
    case "sync": setStatus({ sync: m.state, port: m.port }); break;
    case "monitor": { const { type: _t, ...monitor } = m; setStatus({ monitor }); break; }
    case "keyboards": setStatus({ keyboards: m.names || [] }); break;
    case "mode": setStatus({ mode: m.mode }); break;
    case "transport": { const { type: _t, ...transport } = m; setStatus({ transport }); break; }
    default: break;
  }
  ctx._emit("server", m);
}

// ── the header ────────────────────────────────────────────────────────────────
function renderSync(ctx) {
  const st = ctx.status, box = $("sync");
  let words;
  if (!ctx.server) words = "Playing the twin in this browser";
  else if (st.demo) words = "In sync with S-1";
  else if (st.server === "down") words = "Reconnecting to the cockpit";
  else words = { synced: "In sync with S-1", listening: "S-1 connected", connecting: "Connecting to S-1" }[st.link] || "S-1 not connected";
  $("sync-text").textContent = words;
  box.classList.toggle("on", st.sync === "synced");
  box.classList.toggle("near", st.link === "listening" && !st.demo);
  box.title = st.link === "listening" && !st.demo
    ? "Knob moves flow both ways. Send patch to S-1 to make every other setting match."
    : st.port ? `MIDI port: ${st.port}` : "";
  $("send").hidden = !ctx.server;
}

// ── drawers ─────────────────────────────────────────────────────────────────
function drawers(ctx, hints) {
  const loaded = {};
  let opener = null;
  const openOne = () => Object.keys(loaded).find((n) => loaded[n].el.classList.contains("open"));
  async function open(name, from, { focus = true, instant = false } = {}) {
    const el = $(`drawer-${name}`);
    if (!el || !DRAWERS[name]) return;
    if (!loaded[name]) {
      const mod = await DRAWERS[name]();
      mod.mount(el, ctx);
      el.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", close));
      loaded[name] = { mod, el };
    }
    const other = openOne();
    if (other && other !== name) hide(other);
    opener = from || document.activeElement;
    if (instant) {                        // opened by a review flag: no slide, so a capture is exact
      el.style.transition = "none";
      requestAnimationFrame(() => requestAnimationFrame(() => { el.style.transition = ""; }));
    }
    el.classList.add("open");
    el.removeAttribute("inert");
    el.setAttribute("aria-hidden", "false");
    $("scrim").classList.add("on");
    document.body.classList.add("drawer-open");
    loaded[name].mod.open?.();
    if (focus) el.querySelector("[data-close]")?.focus({ preventScroll: true });
    hints(true);
  }
  function hide(name) {
    const { el, mod } = loaded[name];
    el.classList.remove("open");
    el.setAttribute("inert", "");
    el.setAttribute("aria-hidden", "true");
    mod.close?.();
  }
  function close() {
    const name = openOne();
    if (!name) return;
    hide(name);
    $("scrim").classList.remove("on");
    document.body.classList.remove("drawer-open");
    hints(false);
    if (opener && opener.focus) opener.focus({ preventScroll: true });
    opener = null;
  }
  document.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => {
    const name = b.dataset.open;
    if (openOne() === name) close(); else open(name, b);
  }));
  $("scrim").addEventListener("click", close);
  window.addEventListener("keydown", (e) => { if (e.key === "Escape" && openOne()) { e.preventDefault(); close(); } });
  return { open, close };
}

// ── views ───────────────────────────────────────────────────────────────────
function placeholder(view, broken) {
  const box = document.createElement("section");
  box.className = "placeholder";
  const h = document.createElement("h2");
  h.className = "heading";
  h.textContent = view.title;
  const p = document.createElement("p");
  p.className = "note";
  p.textContent = broken
    ? "This view did not load. Reload the page; if it fails again, the browser console says why."
    : "This view is not in this build yet. The Synth view has everything you can play today.";
  box.append(h, p);
  return box;
}

function router(ctx, hints) {
  let current = null, mountedId = null, token = 0;
  async function go() {
    const id = readHash().route || "synth";
    const view = VIEWS.find((v) => v.id === id) || VIEWS[0];
    if (view.id === mountedId) return;
    const mine = ++token;
    try { current?.unmount?.(); } catch (e) { console.error(e); }
    current = null;
    mountedId = view.id;
    for (const a of document.querySelectorAll("#nav a")) {
      if (a.dataset.view === view.id) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    }
    const host = $("view");
    host.replaceChildren();
    let mod = null;
    try { mod = await view.load(); } catch { mod = null; }
    if (mine !== token) return;
    const v = mod && (typeof mod.mount === "function" ? mod : mod.default);
    if (!v || typeof v.mount !== "function") {
      host.append(placeholder(view, false));
    } else {
      try { v.mount(host, ctx); current = v; } catch (e) { console.error(e); host.replaceChildren(placeholder(view, true)); }
    }
    if (view.id !== "synth") document.documentElement.classList.remove("developing");
    hints.view = view.id;
    hints.items = v && Array.isArray(v.hints) ? v.hints : null;
    hints();
  }
  window.addEventListener("hashchange", go);
  return go();
}

// ── boot ────────────────────────────────────────────────────────────────────
async function boot() {
  const { flags } = readHash();
  document.title = NAME;
  $("wordmark").textContent = NAME;
  $("brand").setAttribute("aria-label", `${NAME}: the synth`);

  const status = await probe();
  const server = status ? { api, ws: (path) => new WebSocket(wsURL(path)) } : null;
  const { schema, status: initial } = server ? { schema: await api("GET", "/api/schema"), status } : await loadStatic();
  const { twin, info } = await loadTwin(Boolean(server));
  if (!server) {  // the public page: say what this is, and where the full app lives
    const intro = $("intro");
    intro.append(
      "A software twin of the Roland S-1, running in your browser. Play it with A to K or a MIDI keyboard. "
      + "To sync a real S-1 and match your own sounds, ",
      Object.assign(document.createElement("a"), { href: REPO_URL, textContent: "install it from GitHub", rel: "noopener" }),
      ". To practice jazz chords and tunes with this sound, try ",
      Object.assign(document.createElement("a"), { href: CHANGES_URL, textContent: "Changes", rel: "noopener" }),
      ".",
    );
    intro.hidden = false;
  }
  $("foot").textContent = (info.stub ? "The waveforms here come from a stand-in model until the twin module is installed. " : "")
    + `${TAGLINE} ${DISCLAIMER}`;

  const demo = flags.get("demo") === "connected";
  const echo = createEchoFilter();
  let link = null;
  const upstream = server ? serverTransport({
    link: { send: (m) => (link ? link.send(m) : false) },
    echo,
    onError: (e) => toast(`A change did not reach the cockpit: ${e.message}`),
  }) : null;
  // The voice count chosen in the Settings drawer last time (ctx.js: the S-1's 4 while one is linked).
  let voices = null;
  try { voices = Number(localStorage.getItem(VOICES_KEY)) || null; } catch { /* private mode */ }
  const ctx = createCtx({ schema, twin, transport: upstream, server, toast, voices });
  ctx.twinInfo = info;
  ctx.keys = createKeys(ctx);
  ctx.transport = createTransport(ctx);   // before the socket opens: it follows the app from the hello on
  startOpenerSync(ctx, { flags });        // the music app opened this page: keep its twin sounding like it
  // In the connected demo the S-1's link is pretend: the server's own word for it is ignored.
  const setStatus = (patch) => {
    if (demo) { const { sync: _s, port: _p, server: _v, ...rest } = patch; patch = rest; }
    ctx._setStatus(patch);
  };
  ctx._setStatus({ ...initial, server: server ? "up" : "none" });
  if (demo) ctx._setStatus({ sync: "synced", port: "S-1 (demo)", demo: true });
  ctx.on("status", () => renderSync(ctx));
  renderSync(ctx);
  if (server) {
    link = connectState({
      onMessage: (m) => onServer(ctx, m, echo, setStatus),
      onOpen: () => setStatus({ server: "up" }),
      onClose: () => setStatus({ server: "down", sync: "disconnected" }),
    });
  }

  $("send").addEventListener("click", async (e) => {
    e.currentTarget.disabled = true;
    await sendPatch(ctx);
    $("send").disabled = false;
  });

  // The KeyHint bar (in the bottom strip on wide windows, core/app.css): Esc while a drawer or the list
  // of keys is open; else the view's own `hints` export, or the one key every view has.
  const overlayOpen = () => document.body.classList.contains("drawer-open") || Boolean(ctx.shortcuts?.sheet.open);
  const hints = (overlay = overlayOpen()) => {
    if (!window.KeyHint) return;
    if (overlay) window.KeyHint.set([{ key: "Esc", label: "Close" }]);
    else window.KeyHint.set(hints.items && hints.items.length ? hints.items : [{ key: "?", label: "Keys" }]);
  };
  const drawer = drawers(ctx, hints);
  ctx.shortcuts = createShortcuts(ctx, {
    go: (id) => { location.hash = `#${id}`; },
    view: () => hints.view,
    onSheet: () => hints(),
  });
  await router(ctx, hints);
  for (const name of Object.keys(DRAWERS)) if (flags.has(name)) { await drawer.open(name, null, { focus: false, instant: true }); break; }
}

boot().catch((e) => {
  document.documentElement.classList.remove("developing");
  const p = document.createElement("p");
  p.className = "note placeholder";
  p.textContent = `The app did not start: ${e && e.message ? e.message : e}. Reload the page to try again.`;
  $("view").replaceChildren(p);
});
// Never leave the page pale: the synth view clears this on its develop moment; this is the backstop.
setTimeout(() => document.documentElement.classList.remove("developing"), 4000);
