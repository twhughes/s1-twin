// design/knob.js — the line-art dial of the cyanotype kit (docs/design/DIRECTION.md).
// Pure DOM, no app state: the caller owns the value's meaning and where it goes.
//   const k = knob({label: "Cutoff", min: 0, max: 127, value: 50, def: 127, onInput: v => …});
//   k.set(v, {source: "midi"})   // programmatic; "midi" draws the bronze trail (the hardware's hand)
// Drag vertically, scroll, double-click for the default, arrows/PageUp/PageDown/Home/End on focus.

const A0 = -135, A1 = 135, NT = 21;
const SVGNS = "http://www.w3.org/2000/svg";
const polar = (r, deg) => {
  const a = (deg - 90) * Math.PI / 180;
  return [50 + r * Math.cos(a), 50 + r * Math.sin(a)];
};

/** SVG arc path on radius r between two angles (degrees, 0 = up), or "" if too short. */
export function arcPath(r, d0, d1) {
  if (Math.abs(d1 - d0) < .5) return "";
  const lo = Math.min(d0, d1), hi = Math.max(d0, d1);
  const [x0, y0] = polar(r, lo), [x1, y1] = polar(r, hi);
  return `M${x0.toFixed(2)} ${y0.toFixed(2)} A${r} ${r} 0 ${hi - lo > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

export function knob({
  label, min = 0, max = 127, value = min, def = value, bipolar = false, size = 50,
  format = null, onInput = () => {},
} = {}) {
  const clamp = (x) => Math.max(min, Math.min(max, Math.round(x)));
  const center = Math.round((min + max) / 2);
  const norm = (x) => (max === min ? 0 : (x - min) / (max - min));
  const deg = (x) => A0 + (A1 - A0) * norm(x);
  let v = clamp(value);

  const el = document.createElement("div");
  el.className = "knob" + (size > 60 ? " hero" : "");
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", "0 0 100 100");
  svg.setAttribute("width", size); svg.setAttribute("height", size);
  svg.setAttribute("role", "slider"); svg.setAttribute("tabindex", "0");
  svg.setAttribute("aria-label", label);
  svg.setAttribute("aria-valuemin", min); svg.setAttribute("aria-valuemax", max);
  const ticks = [];
  for (let i = 0; i < NT; i++) {
    const d = A0 + (A1 - A0) * i / (NT - 1);
    const [x0, y0] = polar(40, d), [x1, y1] = polar(i % 5 === 0 ? 47 : 45, d);
    const t = document.createElementNS(SVGNS, "line");
    t.setAttribute("class", "tick");
    t.setAttribute("x1", x0.toFixed(2)); t.setAttribute("y1", y0.toFixed(2));
    t.setAttribute("x2", x1.toFixed(2)); t.setAttribute("y2", y1.toFixed(2));
    svg.appendChild(t); ticks.push(t);
  }
  const trailEl = document.createElementNS(SVGNS, "path"); trailEl.setAttribute("class", "trail");
  const body = document.createElementNS(SVGNS, "circle");
  body.setAttribute("class", "body"); body.setAttribute("cx", 50); body.setAttribute("cy", 50); body.setAttribute("r", 31);
  const ptr = document.createElementNS(SVGNS, "line");
  ptr.setAttribute("class", "ptr");
  ptr.setAttribute("x1", 50); ptr.setAttribute("y1", 50); ptr.setAttribute("x2", 50); ptr.setAttribute("y2", 24);
  svg.append(trailEl, body, ptr);
  const lab = document.createElement("span"); lab.className = "k-label"; lab.textContent = label;
  const val = document.createElement("span"); val.className = "k-val";
  el.append(svg, lab, val);

  const text = (x) => format ? format(x)
    : bipolar ? (x > center ? "+" : x < center ? "−" : "") + Math.abs(x - center) : String(x);
  function render() {
    const n = norm(v), c = norm(center);
    ptr.setAttribute("transform", `rotate(${deg(v)} 50 50)`);
    ticks.forEach((t, i) => {
      const u = i / (NT - 1);
      const on = bipolar
        ? (n >= c ? u >= c - 1e-6 && u <= n + 1e-6 : u <= c + 1e-6 && u >= n - 1e-6)
        : u <= n + 1e-6;
      t.classList.toggle("on", on);
    });
    val.textContent = text(v);
    svg.setAttribute("aria-valuenow", v);
    svg.setAttribute("aria-valuetext", val.textContent);
  }
  function fromHand(x) {
    x = clamp(x);
    if (x === v) return;
    v = x; render(); onInput(v);
  }

  // the hardware's hand: a bronze trail from where a burst of hardware moves started
  const hw = { active: false, from: v, timer: 0 };
  function trail(from, to) { trailEl.setAttribute("d", arcPath(36, deg(from), deg(to))); }
  function set(x, { source } = {}) {
    x = clamp(x);
    if (source === "midi") {
      if (!hw.active) { hw.active = true; hw.from = v; el.classList.add("hw"); }
      clearTimeout(hw.timer);
      hw.timer = setTimeout(() => { el.classList.remove("hw"); hw.active = false; }, 900);
      v = x; render(); trail(hw.from, v);
      return;
    }
    if (x === v) return;
    v = x; render();
  }

  let drag = null;
  svg.addEventListener("pointerdown", (e) => {
    svg.setPointerCapture(e.pointerId); drag = { y: e.clientY, v }; e.preventDefault(); svg.focus();
  });
  svg.addEventListener("pointermove", (e) => {
    if (!drag) return;
    fromHand(drag.v + (drag.y - e.clientY) * (e.shiftKey ? .12 : .55) * ((max - min) / 127));
  });
  const end = () => { drag = null; };
  svg.addEventListener("pointerup", end);
  svg.addEventListener("pointercancel", end);
  svg.addEventListener("dblclick", () => fromHand(def));
  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    fromHand(v + (e.deltaY < 0 ? 1 : -1) * (e.shiftKey ? 1 : 3));
  }, { passive: false });
  svg.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 10 : 1;
    const map = { ArrowUp: step, ArrowRight: step, ArrowDown: -step, ArrowLeft: -step, PageUp: 10, PageDown: -10 };
    if (e.key in map) { fromHand(v + map[e.key]); e.preventDefault(); }
    else if (e.key === "Home") { fromHand(min); e.preventDefault(); }
    else if (e.key === "End") { fromHand(max); e.preventDefault(); }
  });

  function modeled(ok, why = "Only on the S-1 for now: the twin does not model this yet.") {
    el.classList.toggle("unmodeled", !ok);
    el.title = ok ? "" : why;
  }

  render();
  return { el, set, get: () => v, trail, modeled };
}
