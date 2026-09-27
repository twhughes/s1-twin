// drawers/library.js — the Library drawer (DIRECTION rule 3: the library and Save to S-1 live in a
// drawer, off the plate). Patches (list, load, play, save, delete), Save to the S-1 (the disk-mode
// ritual, Write to S-1, Download the .prm file), Load from the S-1 (the device's backups, this Mac's
// backups, or any .prm file), switching the S-1's stored pattern, and the pattern-file-only settings.
// The shell calls mount(el, ctx) once, then open() / close() as the drawer slides in and out.

import { plateLayout } from "../core/layout.js";

export const id = "library";
export const title = "Library";

const POLL_MS = 5000;
let ctx = null;
let ui = null;
let pollTimer = 0;
let current = null;                    // the patch last loaded or saved here, until a hand changes a knob
let names = new Set();                 // the saved patches' names, to ask before replacing one

const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) e.setAttribute(k, v === true ? "" : v);
  }
  e.append(...kids.filter((k) => k != null));
  return e;
};
const options = (n, from = 1) => Array.from({ length: n }, (_, i) => h("option", { value: i + from, text: String(i + from) }));
const say = (msg) => ctx.toast(msg);
const quote = (name) => `“${name}”`;

/** A button that asks once more before it acts: the first press arms it for a few seconds. */
function twoStep(button, armedText, act) {
  const idle = button.textContent;
  let armed = 0;
  button.addEventListener("click", () => {
    if (armed) { clearTimeout(armed); armed = 0; button.textContent = idle; act(); return; }
    button.textContent = armedText;
    armed = setTimeout(() => { armed = 0; button.textContent = idle; }, 3500);
  });
}

export function mount(el, context) {
  ctx = context;
  const server = Boolean(ctx.server);
  ui = {};
  el.replaceChildren(
    h("button", { class: "quiet close", type: "button", "data-close": true, text: "Close" }),
    h("h2", { text: "Library" }),
  );
  if (!server) {
    el.append(h("p", { text: "The library needs the cockpit running on your Mac: saved patches, Save to S-1 and Load from S-1 all live there." }));
  } else {
    el.append(...patchesSection(), ...saveSection(), ...loadSection(), ...patternSection());
  }
  el.append(prmSection());
  ctx.on("param", ({ source }) => {
    if (current && (source === "ui" || source === "midi")) { current = null; markCurrent(); }
  });
}

export function open() {
  if (!ctx || !ctx.server) return;
  refreshPatches();
  refreshDevice();
  clearInterval(pollTimer);
  pollTimer = setInterval(() => { if (!document.hidden) refreshDevice(); }, POLL_MS);
}

export function close() { clearInterval(pollTimer); pollTimer = 0; }

// ── patches ────────────────────────────────────────────────────────────────
function patchesSection() {
  ui.patches = h("ul", { class: "patches", "aria-label": "Saved patches" });
  ui.name = h("input", { type: "text", "aria-label": "Patch name", placeholder: "Name this patch", maxlength: "64" });
  ui.save = h("button", { class: "pill", type: "button", text: "Save patch", onclick: savePatch });
  ui.name.addEventListener("keydown", (e) => { if (e.key === "Enter") savePatch(); });
  ui.name.addEventListener("input", () => { ui.save.textContent = "Save patch"; ui.replace = false; });
  return [h("h3", { class: "first", text: "Patches" }), ui.patches, h("div", { class: "saverow" }, ui.name, ui.save)];
}

async function refreshPatches() {
  let list;
  try { list = await ctx.server.api("GET", "/api/patches"); } catch { return; }
  names = new Set(list.map((p) => p.name));
  ui.patches.replaceChildren();
  if (!list.length) {
    ui.patches.append(h("li", { class: "empty", text: "No patches yet. Shape a sound on the plate, name it, and save it." }));
    return;
  }
  for (const p of list) {
    const meta = p.metadata && p.metadata.closeness !== undefined ? `${p.metadata.closeness}% match` : "";
    const del = h("button", { class: "linkish", type: "button", text: "Delete", "aria-label": `Delete ${p.name}` });
    twoStep(del, "Really delete", () => deletePatch(p.name));
    ui.patches.append(h("li", { "data-name": p.name },
      h("button", { class: "patch-name", type: "button", text: p.name, title: "Load this patch", onclick: () => loadPatch(p.name) }),
      meta ? h("span", { class: "patch-meta", text: meta }) : null,
      h("span", { class: "patch-acts" },
        h("button", { class: "linkish", type: "button", text: "Play", "aria-label": `Play ${p.name}`, onclick: () => playPatch(p.name) }),
        del),
    ));
  }
  markCurrent();
}

function markCurrent() {
  if (!ui || !ui.patches) return;
  for (const li of ui.patches.querySelectorAll("li[data-name]")) {
    li.querySelector(".patch-name").setAttribute("aria-current", String(li.dataset.name === current));
  }
}

const enc = encodeURIComponent;

async function loadPatch(name) {
  try {
    await ctx.server.api("POST", `/api/patches/${enc(name)}/load`);
    current = name;
    markCurrent();
    say(`Loaded ${quote(name)}.`);
  } catch (e) { say(`${quote(name)} did not load: ${e.message}`); }
}

async function playPatch(name) {
  try {
    if (ctx.soundSource === "s1") {
      await ctx.server.api("POST", `/api/patches/${enc(name)}/play`);
      say(`Playing ${quote(name)} on the S-1.`);
    } else {
      await ctx.server.api("POST", `/api/patches/${enc(name)}/load`);
      ctx.note(48, true, 100);
      setTimeout(() => ctx.note(48, false), 1000);
      say(`Playing ${quote(name)} on the twin.`);
    }
    current = name;
    markCurrent();
  } catch (e) { say(`${quote(name)} did not play: ${e.message}`); }
}

async function deletePatch(name) {
  try {
    await ctx.server.api("DELETE", `/api/patches/${enc(name)}`);
    if (current === name) current = null;
    say(`Deleted ${quote(name)}.`);
    refreshPatches();
  } catch (e) { say(`${quote(name)} was not deleted: ${e.message}`); }
}

async function savePatch() {
  const name = ui.name.value.trim();
  if (!name) { say("Name the patch first."); ui.name.focus(); return; }
  if (!ui.replace && names.has(name)) { askReplace(name); return; }
  try {
    await ctx.server.api("POST", "/api/patches", { name, overwrite: Boolean(ui.replace) });
    say(ui.replace ? `Replaced ${quote(name)}.` : `Saved ${quote(name)}.`);
    current = name;
    ui.name.value = "";
    ui.replace = false;
    ui.save.textContent = "Save patch";
    refreshPatches();
  } catch (e) {
    if (e.status === 409) {
      askReplace(name);
    } else {
      say(`The patch was not saved: ${e.message}`);
    }
  }
}

function askReplace(name) {
  ui.replace = true;
  ui.save.textContent = "Replace it";
  say(`A patch named ${quote(name)} exists. Press Replace it to overwrite it.`);
}

// ── Save to the S-1 ────────────────────────────────────────────────────────
function saveSection() {
  ui.bank = h("select", { "aria-label": "Bank" }, ...options(4));
  ui.slot = h("select", { "aria-label": "Slot" }, ...options(16));
  ui.download = h("a", { class: "linkish", download: true, text: "Download the .prm file" });
  const sync = () => { ui.download.href = `/api/export/prm?bank=${ui.bank.value}&slot=${ui.slot.value}`; };
  ui.bank.addEventListener("change", sync);
  ui.slot.addEventListener("change", sync);
  sync();
  ui.write = h("button", { class: "pill", type: "button", text: "Write to S-1", onclick: writePattern });
  ui.driveNote = h("p", { class: "status", role: "status", text: "Looking for a drive named S-1." });
  return [
    h("h3", { text: "Save to the S-1" }),
    h("p", { text: "The S-1 takes patterns over USB disk mode. This app writes the file; the S-1 needs your hands for the rest." }),
    h("ol", { class: "steps" },
      h("li", { text: "Power the S-1 off. Hold Play while you power it on." }),
      h("li", { text: "Wait one to two minutes for a drive named S-1 to appear." }),
      h("li", { text: "Pick a bank and slot below, then write the pattern (or copy the downloaded file into the drive's RESTORE folder)." }),
      h("li", { text: "Press Hold on the S-1, wait for dOnE, then power-cycle it." }),
    ),
    h("div", { class: "slotrow" }, h("label", {}, "Bank ", ui.bank), h("label", {}, "Slot ", ui.slot)),
    h("div", { class: "saverow center" }, ui.write, ui.download),
    ui.driveNote,
  ];
}

let mounted = null;
let writtenAt = 0;                     // a fresh "Written" note outlives the next device poll
async function writePattern() {
  if (mounted === false) { say("No drive named S-1 is mounted. Do steps 1 and 2 first."); return; }
  ui.write.disabled = true;
  try {
    const r = await ctx.server.api("POST", "/api/export/device", { bank: Number(ui.bank.value), slot: Number(ui.slot.value) });
    const file = String(r.written).split("/").pop();
    ui.driveNote.textContent = `Written: ${file}. Now press Hold on the S-1 and wait for dOnE.`;
    writtenAt = Date.now();
    say("Written. Press Hold on the S-1 and wait for dOnE.");
  } catch (e) {
    say(e.status === 409 ? "No drive named S-1 is mounted. Do steps 1 and 2 first." : `The pattern was not written: ${e.message}`);
  } finally {
    ui.write.disabled = false;
  }
}

// ── Load from the S-1 ───────────────────────────────────────────────────────
function loadSection() {
  ui.impNote = h("p", { role: "status", text: "Looking for pattern files." });
  ui.impPatch = h("input", { type: "checkbox", checked: true });
  ui.impSeq = h("input", { type: "checkbox", checked: true });
  ui.imports = h("ul", { class: "patches", "aria-label": "Pattern files" });
  ui.file = h("input", { type: "file", accept: ".prm,.PRM", hidden: true, "aria-label": "A .prm pattern file" });
  ui.file.addEventListener("change", uploadPattern);
  return [
    h("h3", { text: "Load from the S-1" }),
    ui.impNote,
    h("div", { class: "checks" },
      h("label", {}, ui.impPatch, " Load the sound"),
      h("label", {}, ui.impSeq, " Load the sequence")),
    ui.imports,
    h("p", {}, h("button", { class: "linkish", type: "button", text: "Open a .prm file", onclick: () => ui.file.click() })),
    ui.file,
  ];
}

async function refreshDevice() {
  const [dev, imp] = await Promise.allSettled([
    ctx.server.api("GET", "/api/export/device"),
    ctx.server.api("GET", "/api/import/prm"),
  ]);
  if (dev.status === "fulfilled") {
    mounted = Boolean(dev.value.mounted);
    if (Date.now() - writtenAt > 20000) ui.driveNote.textContent = mounted
      ? `The S-1 drive is mounted at ${dev.value.volume}. Ready to write.`
      : "No drive named S-1 is mounted. Do steps 1 and 2 to write straight to it, or download the file.";
  }
  if (imp.status !== "fulfilled") return;
  const r = imp.value;
  const rows = [];
  for (const [source, files, where] of [["device", r.device.files, "on the S-1"], ["backups", r.backups, "on this Mac"]]) {
    for (const f of files) {
      const label = f.bank ? `${f.bank}-${String(f.slot).padStart(2, "0")}  ${f.name}` : f.name;
      rows.push(h("li", {},
        h("button", { class: "patch-name", type: "button", text: label, title: "Load this pattern", onclick: () => importPattern(source, f.name) }),
        h("span", { class: "patch-meta", text: where })));
    }
  }
  ui.impNote.textContent = r.device.mounted
    ? "The S-1 is in disk mode. Its saved patterns are listed first."
    : "The S-1 is not in disk mode, so the list shows the backups on this Mac (~/.synth/backups).";
  ui.imports.replaceChildren(...(rows.length ? rows
    : [h("li", { class: "empty", text: "No pattern files yet. Put the S-1 in disk mode (steps 1 and 2 above), or open a .prm file." })]));
}

function loaded(r) {
  const bits = [];
  if (r.params !== undefined) bits.push(`${r.params} settings`);
  if (r.sequence) bits.push(`${r.sequence.notes} notes at ${r.sequence.bpm} BPM`);
  current = null;
  markCurrent();
  say(`Loaded ${r.loaded}${bits.length ? `: ${bits.join(", ")}` : ""}.`);
}

async function importPattern(source, name) {
  try {
    loaded(await ctx.server.api("POST", "/api/import/prm", {
      source, name, load_patch: ui.impPatch.checked, load_sequence: ui.impSeq.checked,
    }));
  } catch (e) { say(`${name} did not load: ${e.message}`); }
}

async function uploadPattern() {
  const f = ui.file.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  try {
    loaded(await ctx.server.api("POST",
      `/api/import/upload?load_patch=${ui.impPatch.checked}&load_sequence=${ui.impSeq.checked}`, fd, { form: true }));
  } catch (e) { say(`${f.name} did not load: ${e.message}`); }
  ui.file.value = "";
}

// ── the S-1's stored patterns (Program Change) ──────────────────────────────
function patternSection() {
  ui.pcBank = h("select", { "aria-label": "Pattern bank" }, ...options(4));
  ui.pcSlot = h("select", { "aria-label": "Pattern slot" }, ...options(16));
  ui.switchBtn = h("button", { class: "pill", type: "button", text: "Switch pattern", onclick: switchPattern });
  return [
    h("h3", { text: "Patterns on the S-1" }),
    h("p", { text: "Switch the S-1 to one of its 64 stored patterns. Nothing is written." }),
    h("div", { class: "slotrow" }, h("label", {}, "Bank ", ui.pcBank), h("label", {}, "Slot ", ui.pcSlot), ui.switchBtn),
  ];
}

async function switchPattern() {
  if (ctx.soundSource !== "s1") { say("The S-1 is not connected. Plug it in to switch its pattern."); return; }
  try {
    const r = await ctx.server.api("POST", "/api/device/pattern", { bank: Number(ui.pcBank.value), slot: Number(ui.pcSlot.value) });
    say(`The S-1 now plays pattern ${r.bank}-${r.slot}.`);
  } catch (e) { say(`The pattern did not switch: ${e.message}`); }
}

// ── the pattern-file-only tier ─────────────────────────────────────────────
function prmSection() {
  const prm = plateLayout(ctx.schema).prm;
  const list = h("dl", { class: "prm-list" });
  for (const p of prm) {
    const label = p.labels && p.labels[String(p.default)];
    list.append(h("dt", { text: p.name, title: p.description || p.key }), h("dd", { text: label !== undefined ? label : String(p.default) }));
  }
  return h("details", { class: "more prm" },
    h("summary", { text: "Settings only in pattern files" }),
    h("p", { text: "No MIDI message reaches these; only a .prm pattern file carries them. Written patterns keep the values below, taken from a real S-1 backup." }),
    list);
}
