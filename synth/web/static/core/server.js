// core/server.js — the link to the cockpit server (synth/web/server.py).
//   probe()            -> /api/status JSON, or null when no cockpit answers (the static page)
//   api(method, path, body, {form})  JSON in, JSON (or the Response) out; errors carry the server's words
//   connectState({onMessage, onOpen, onClose})  the /ws/state client: reconnects with backoff
//   createEchoFilter() drops the server's echo of our own param sends, so a fast knob drag
//                      never snaps back to a value it already passed
//   serverTransport()  what ctx sends upstream: params and notes, over the socket or REST
// Importable in node (no DOM at load time): core/ctx.check.mjs tests the echo filter.

export async function probe(timeoutMs = 2500) {
  const ac = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timer = ac ? setTimeout(() => ac.abort(), timeoutMs) : 0;
  try {
    const r = await fetch("/api/status", { cache: "no-store", signal: ac?.signal });
    if (!r.ok) return null;
    const j = await r.json();
    return j && typeof j.sync === "string" ? j : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export async function api(method, path, body = null, { form = false } = {}) {
  const opts = { method, cache: "no-store" };
  if (body !== null && body !== undefined) {
    if (form) opts.body = body;
    else { opts.headers = { "Content-Type": "application/json" }; opts.body = JSON.stringify(body); }
  }
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = `The cockpit answered ${r.status}.`;
    try {
      const j = await r.json();
      if (j && j.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch { /* not JSON: keep the status line */ }
    const err = new Error(detail);
    err.status = r.status;
    throw err;
  }
  return (r.headers.get("content-type") || "").includes("json") ? r.json() : r;
}

export function wsURL(path) {
  return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${path}`;
}

/** Remembers what we sent per CC for `windowMs`; isEcho() consumes the match (and anything older). */
export function createEchoFilter({ windowMs = 3000, now = () => Date.now() } = {}) {
  const pending = new Map();
  return {
    sent(cc, v) {
      const q = pending.get(cc) || [];
      q.push({ v, t: now() });
      pending.set(cc, q);
    },
    isEcho(cc, v) {
      const q = pending.get(cc);
      if (!q) return false;
      const t = now();
      while (q.length && t - q[0].t > windowMs) q.shift();
      const i = q.findIndex((e) => e.v === v);
      if (i >= 0) q.splice(0, i + 1);
      if (!q.length) pending.delete(cc);
      return i >= 0;
    },
  };
}

/** The /ws/state socket. Backoff 1 s, doubling to 10 s; reset once a connection opens. */
export function connectState({ onMessage, onOpen = () => {}, onClose = () => {}, url = wsURL("/ws/state"),
  WS = globalThis.WebSocket, minMs = 1000, maxMs = 10000 } = {}) {
  let ws = null;
  let retry = minMs;
  let timer = 0;
  let stopped = false;
  const open = () => {
    const sock = new WS(url);
    ws = sock;
    sock.onopen = () => { retry = minMs; onOpen(); };
    sock.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      onMessage(m);
    };
    sock.onerror = () => {};                    // onclose follows and schedules the retry
    sock.onclose = () => {
      if (ws === sock) ws = null;
      onClose();
      if (stopped) return;
      timer = setTimeout(open, retry);
      retry = Math.min(retry * 2, maxMs);
    };
  };
  open();
  return {
    send(obj) {
      if (!ws || ws.readyState !== 1) return false;
      ws.send(JSON.stringify(obj));
      return true;
    },
    get isOpen() { return Boolean(ws && ws.readyState === 1); },
    close() { stopped = true; clearTimeout(timer); ws?.close(); },
  };
}

/** Upstream for ctx: the socket when it is open, REST when it is not (the old cockpit's rule). */
export function serverTransport({ link, echo, onError = () => {} }) {
  return {
    sendParam(cc, value) {
      echo.sent(cc, value);
      if (!link.send({ type: "param", cc, value })) {
        api("PUT", `/api/params/${cc}`, { value }).catch(onError);
      }
    },
    sendNote(note, on, velocity) {
      if (!link.send({ type: "note", note, velocity, on })) {
        api("POST", "/api/notes", { note, velocity, on }).catch(() => {});
      }
    },
  };
}
