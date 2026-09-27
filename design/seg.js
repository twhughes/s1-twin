// design/seg.js — a switch: a row of named options, the chosen one underlined.
//   const s = seg({label: "Wave", options: [{value: 0, label: "Saw", glyph: GLYPHS.saw}, …], value: 2, onInput});
// Arrow keys move between options (roving tabindex); glyph options keep their name as aria-label.

export const GLYPHS = {
  saw: "M1 12 L10 2 L10 12 L20 2 L20 12",
  isaw: "M1 2 L10 12 L10 2 L20 12 L20 2",
  tri: "M1 12 L6 2 L11 12 L16 2 L21 12",
  sq: "M1 12 L1 2 L7 2 L7 12 L13 12 L13 2 L19 2 L19 12",
  rnd: "M1 9 L4 9 L4 3 L8 3 L8 11 L12 11 L12 5 L16 5 L16 8 L21 8",
  noise: "M1 8 L3 3 L5 11 L6 5 L8 10 L10 2 L12 12 L13 6 L15 9 L17 3 L19 10 L21 6",
};

export function seg({ label, options, value, onInput = () => {} } = {}) {
  let v = value;
  const el = document.createElement("div");
  el.className = "seg";
  const box = document.createElement("div");
  box.className = "seg-opts";
  box.setAttribute("role", "radiogroup");
  box.setAttribute("aria-label", label);
  const lab = document.createElement("span");
  lab.className = "k-label";
  lab.textContent = label;
  el.append(box, lab);

  const pick = (x) => {
    if (x === v) return;
    v = x; render(); onInput(v);
  };
  const buttons = options.map((o, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "radio");
    if (o.glyph) {
      b.innerHTML = `<svg viewBox="0 0 22 14" aria-hidden="true"><path d="${o.glyph}"/></svg>`;
      b.setAttribute("aria-label", o.label);
      b.title = o.label;
    } else {
      b.textContent = o.label;
    }
    b.addEventListener("click", () => pick(o.value));
    b.addEventListener("keydown", (e) => {
      const d = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
      if (!d) return;
      e.preventDefault();
      const j = (i + d + options.length) % options.length;
      pick(options[j].value);
      buttons[j].focus();
    });
    box.appendChild(b);
    return b;
  });
  function render() {
    buttons.forEach((b, i) => {
      const on = options[i].value === v;
      b.setAttribute("aria-checked", String(on));
      b.tabIndex = on ? 0 : -1;
    });
    if (!options.some((o) => o.value === v) && buttons[0]) buttons[0].tabIndex = 0;
  }
  function set(x) { if (x === v) return; v = x; render(); }
  function modeled(ok, why = "Only on the S-1 for now: the twin does not model this yet.") {
    el.classList.toggle("unmodeled", !ok);
    el.title = ok ? "" : why;
  }
  render();
  return { el, set, get: () => v, modeled };
}
