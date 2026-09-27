// core/static.js — the static page's context (GitHub Pages, no cockpit server; BUILD.md §0):
// the schema comes from core/schema.json (exported by tools/export_schema.py, drift-tested against
// /api/schema), every value starts at its factory default, and there is never an S-1.

export async function loadStatic() {
  const r = await fetch(new URL("./schema.json", import.meta.url));
  if (!r.ok) throw new Error("The page could not load its parameter list. Reload to try again.");
  return {
    schema: await r.json(),
    status: { sync: "disconnected", port: null, monitor: null, keyboards: [], mode: "solo", studio: false },
  };
}

/** The twin's curves on the static page: twin/curves.json when it is published, else the twin's own. */
export async function loadStaticCurves() {
  try {
    const r = await fetch(new URL("../twin/curves.json", import.meta.url));
    return r.ok ? { curves: await r.json(), kind: "default" } : { curves: null, kind: "bundled" };
  } catch {
    return { curves: null, kind: "bundled" };
  }
}
