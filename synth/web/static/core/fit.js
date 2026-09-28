// core/fit.js — one screen for every view (docs/design/ROUND2.md §4, round 3). Above the stacking width a
// view has one design size, DESIGN_W px wide, laid out to fit a 13-inch laptop at 100% zoom. A window
// narrower or shorter than that scales the whole view down evenly, like a plugin window, never below
// FIT_MIN and never up; nothing reflows. The room is the window's height from the view's top down to the
// bottom strip (core/app.css: --strip-h, the fixed bar with the legal line and the key hints). A narrow
// window stacks the view instead, and that scrolls.
//
//   const fit = fitView(page, {onFit})   wraps `page` in a .fit-box that holds the scaled size (the box
//                                        takes the page's place, or append fit.box yourself); refits on
//                                        window resize and whenever the page's own size changes
//   fit.refit()                          at once, after a change the observers cannot see
//   fit.scale                            the scale in use (1 when unscaled or stacked)
//   fit.destroy()                        stop observing and take the box out of the page
// app.css gives `.fit-box > .fit-page` the design width above STACKED px and clips the box.

export const DESIGN_W = 1470;
export const STACKED = 1180;
export const FIT_MIN = 0.7;

/** The scale that fits a page `width` × `height` into `roomW` × `roomH`: never up, never below `min`. */
export function fitScale(width, height, roomW, roomH, min = FIT_MIN) {
  if (!(width > 0) || !(height > 0)) return 1;
  return Math.max(min, Math.min(1, roomW / width, roomH / height));
}

export function fitView(page, { onFit = null } = {}) {
  const box = document.createElement("div");
  box.className = "fit-box";
  if (page.parentNode) page.parentNode.insertBefore(box, page);
  box.append(page);
  page.classList.add("fit-page");
  let scale = 1, raf = 0, alive = true;

  function refit() {
    if (!alive) return;
    page.style.transform = "";
    page.style.marginLeft = "";
    box.style.height = "";
    scale = 1;
    if (window.innerWidth > STACKED && box.isConnected) {
      const root = getComputedStyle(document.documentElement);
      const strip = parseFloat(root.getPropertyValue("--strip-h")) || 0;
      const room = box.clientWidth || document.documentElement.clientWidth;
      const width = page.offsetWidth, natural = page.offsetHeight;
      const top = box.getBoundingClientRect().top + window.scrollY;
      const s = fitScale(width, natural, room, window.innerHeight - strip - top);
      page.style.marginLeft = `${Math.max(0, (room - width * s) / 2).toFixed(1)}px`;
      if (s < 0.9995) {
        scale = s;
        page.style.transform = `scale(${s.toFixed(4)})`;
        box.style.height = `${(natural * s).toFixed(1)}px`;   // the page flows (and scrolls) by the scaled size
      }
    }
    if (onFit) onFit(scale);
  }
  const soon = () => {
    if (raf || !alive) return;
    raf = requestAnimationFrame(() => { raf = 0; refit(); });
  };
  // The page's own size (a fold-out, a report, a font) and the window's; the transform and the margin
  // change neither, so a refit never feeds itself.
  const ro = new ResizeObserver(soon);
  ro.observe(page);
  window.addEventListener("resize", soon);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(soon);
  soon();

  return {
    box,
    refit,
    get scale() { return scale; },
    destroy() {
      alive = false;
      cancelAnimationFrame(raf);
      ro.disconnect();
      window.removeEventListener("resize", soon);
      box.remove();
    },
  };
}
