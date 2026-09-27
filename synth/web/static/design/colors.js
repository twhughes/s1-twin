// Tyler's synesthesia palette: color means pitch, and nothing else in the UI gets a hue.
// Vendored from hq/music/music/web/static/colors.js (THE note-color authority, stated 2026-07-28).
// Drift test: hq tests/test_drift_music_synth.py (P2) parses TRUE_BASE, PC_LETTER and the
// sharp-brightening factor from this file, so keep their literal shapes exactly as below.
// A sharp brightens the letter's color (a flat would darken it; a 12-pc grid can only spell
// one, so black keys render as brightened sharps).

const TRUE_BASE = {
  A: [235, 92, 92], B: [181, 141, 102], C: [236, 236, 238], D: [178, 192, 216],
  E: [57, 255, 20], F: [168, 24, 30], G: [36, 66, 168],
};

// pitch class (0 = C) -> [letter, sharp?]
const PC_LETTER = [
  ["C", 0], ["C", 1], ["D", 0], ["D", 1], ["E", 0], ["F", 0],
  ["F", 1], ["G", 0], ["G", 1], ["A", 0], ["A", 1], ["B", 0],
];

export const PC_NAMES = ["C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B"];

/** [r, g, b] for a pitch class (any integer; wraps). */
export function rgbOf(pc) {
  const [letter, sharp] = PC_LETTER[((pc % 12) + 12) % 12];
  let [r, g, b] = TRUE_BASE[letter];
  if (sharp) {
    r = Math.round(r + (255 - r) * 0.35);
    g = Math.round(g + (255 - g) * 0.35);
    b = Math.round(b + (255 - b) * 0.35);
  }
  return [r, g, b];
}

/** Relative luminance 0..1, for picking ink or field text on a pitch color. */
export const lum = ([r, g, b]) => (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;

/** rgba() string for a pitch color at a given alpha. */
export const rgba = (rgb, a) => `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${a})`;

/** Note name with octave, MIDI 60 = C4. */
export const noteName = (n) => PC_NAMES[((n % 12) + 12) % 12] + (Math.floor(n / 12) - 1);
