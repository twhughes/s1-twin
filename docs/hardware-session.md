# The hardware session: one sitting with the real S-1

**About 90 minutes, once.** This session does two things no code can do alone:

- It makes the word "twin" true. The calibration run measures the real S-1 and fits
  the twin's curves to it (FABLE: "calibrated against a few hundred real hardware probes").
- It checks the matcher's distance against your ears (FABLE rule 1: "play me pairs, ask
  which is closer, and confirm the number agrees with my ears").

Every command below is one line. Paste it into a terminal as it is.

| Part | What | Time |
|---|---|---|
| 0 | Get ready | 5 min |
| A | Plug-in check | 20 min |
| B | Calibration run | 20 min |
| C | Ear test | 15 min |
| D | Hand the results to the lead | 5 min |
| | Spare time for surprises | 25 min |

---

## 0. Get ready (5 min)

1. Install the two new commands into the synth venv:

   ```bash
   cd ~/Documents/hq/synth && .venv/bin/pip install -e ".[studio,twin,dev]"
   ```

   **Pass:** this command lists two files:

   ```bash
   ls ~/Documents/hq/synth/.venv/bin/synth-calibrate ~/Documents/hq/synth/.venv/bin/synth-eartest-report
   ```

   If the install fails (for example, no network), skip it. Use
   `.venv/bin/python -m synth.match.calibrate_cli` wherever this page says
   `.venv/bin/synth-calibrate`, and `.venv/bin/python -m synth.match.eartest_report` wherever
   it says `.venv/bin/synth-eartest-report`.

2. Have these ready: the S-1, its USB-C **data** cable, the Keystation and its cable, and
   headphones for part C.

---

## A. Plug-in check (20 min)

These are the "last mile" checks from `STATUS.md` (knob twist to screen, the hardware
keyboard, a disk-mode write, unplug and replug), plus the connection itself. Each step has
a pass line. If a step fails, write down what you saw and go on; the lead fixes it later.

1. **Start the cockpit.**

   ```bash
   synth
   ```

   **Pass:** the cockpit opens at http://localhost:8766/.

2. **Plug in the S-1** with the USB-C data cable.

   **Pass:** within about 2 seconds the sync chip says *listening*, and you hear the S-1
   through the Mac when you press one of its keys. To check it in text:

   ```bash
   curl -s localhost:8766/api/status
   ```

   Look for `"sync": "listening"`, a `"port"` with `S-1` in it, and `"running": true`
   under `"monitor"`.

3. **Knob twist to the screen.** Turn the S-1's filter cutoff knob slowly, end to end.

   **Pass:** the cockpit's cutoff control follows the knob with no visible lag. This shows
   the value the cockpit holds:

   ```bash
   curl -s localhost:8766/api/params/74
   ```

4. **Screen to the S-1.** Press *Push to S-1* in the cockpit. Then drag the cockpit's
   resonance control.

   **Pass:** the chip says *synced*, and the S-1's sound changes while you drag.

5. **The hardware keyboard.** Plug in the Keystation and play: long notes, fast taps,
   pitch bend.

   **Pass:** every key sounds on the S-1 at once, fast taps are not silent, and the
   status lists the keyboard under `"keyboards"`:

   ```bash
   curl -s localhost:8766/api/status
   ```

6. **Write a pattern in disk mode.** This overwrites pattern **4-16** on the S-1 (pick
   another slot below if 4-16 holds something you want).

   1. In the cockpit's sequencer, put in a short line of notes (any four), so the
      written pattern has something to play.
   2. Power the S-1 off. Hold **[PLAY]** while you power it on.
   3. Wait 1 to 2 minutes until a drive named `S-1` appears in Finder.
   4. Write the cockpit's patch and sequence into it:

      ```bash
      curl -s -X POST localhost:8766/api/export/device -H 'content-type: application/json' -d '{"bank": 4, "slot": 16}'
      ```

   5. Press **[HOLD]** on the S-1 and wait for `dOnE`. Power-cycle the S-1.

   **Pass:** the curl answer names a file in `RESTORE/`, and when you select pattern 4-16
   on the S-1 and press play, it plays your four notes.

7. **Unplug and plug back.** Pull the USB cable for 5 seconds, then plug it back in.

   **Pass:** the chip goes to *disconnected*, then back to *listening* within about
   2 seconds, the sound comes back, and nothing needs a restart.

8. **Stop the cockpit** before part B. Two programs driving the S-1 at once would spoil
   the measurements.

   ```bash
   synth off
   ```

---

## B. Calibration run (20 min)

The run plays about 300 short test notes (probes) on the S-1, records each one over USB,
and fits the twin to them.

1. **Check the setup.** This plays a few short notes.

   ```bash
   cd ~/Documents/hq/synth && .venv/bin/synth-calibrate --check
   ```

   **Pass:** the last line is `Ready for calibration.` Above it you see the latency
   (usually 10 to 40 ms), `C3 plays at 130.8 Hz` (or close), the level, and the hiss
   floor. If a line starts with `Problem:`, it says what to do; do that and run the
   check again.

2. **Start the run.**

   ```bash
   cd ~/Documents/hq/synth && .venv/bin/synth-calibrate
   ```

   It prints six setup steps first. The short version: data cable in, cockpit off, no
   keyboard, **do not touch the S-1's knobs**, volume knob at about 3 o'clock. Room noise
   does not matter (the sound goes over USB). Press Enter when ready.

   It then prints the estimate from the real cost of one probe, for example
   `Estimate: 300 probes x 2.41 s = 12 min of probing, then about 2 min of fitting`.

3. **Watch it run (about 12 minutes).** One line counts the probes and the time left.
   A line such as `probe 57: clipped` means that probe is left out. A few are fine.
   More than about ten means the level is wrong: press Ctrl-C, fix the volume, and
   resume.

   To stop at any time, press **Ctrl-C**. Nothing recorded is lost, and the S-1 goes back
   to a clean patch. The run prints the command to continue, which looks like:

   ```bash
   cd ~/Documents/hq/synth && .venv/bin/synth-calibrate --resume ~/.synth/calibration/<timestamp>
   ```

4. **The fit (1 to 3 minutes, the S-1 is free).** The run ends with a result line and
   one line per module, for example:

   ```text
   On 126 held-out joint probes the twin's feature gap went from 0.41 to 0.19 (closeness 54 to 75).
   Report: ~/.synth/calibration/<timestamp>/report.md
   Curves installed: ~/.synth/twin/curves.calibrated.json
   ```

   **Pass:** the line `Curves installed:` appears. If it says the curves were *not*
   installed, the fit made the twin worse; that is a finding, not a failure. Go on.

   At the end the S-1 is on the init patch. Your own patch is still in the cockpit: press
   *Push to S-1* later to send it back.

---

## C. Ear test (15 min)

You hear a reference sound, then two others, A and B, and choose the one closer to the
reference. The computer has its own opinion (the matcher's distance); this test checks
whether its opinion matches yours. The page never shows which one it thinks is closer.

1. **Start the cockpit** (the S-1 does not need to be plugged in):

   ```bash
   synth
   ```

2. **Open the test** in the same browser: http://localhost:8766/eartest. Put headphones
   on and press *Start*.

3. **Answer 40 pairs (about 10 minutes).** Each pair plays reference, A, B once by itself.

   | Key | Does |
   |---|---|
   | space | play the reference again |
   | a / b | play A / B again |
   | r | play all three again |
   | 1 / 2 | A is closer / B is closer |
   | 0 | can't tell |

   Choose "can't tell" when you honestly cannot hear which is closer; it is scored
   separately, not as a wrong answer. A reload continues where you stopped (the session
   name is in the address, so keep that address).

4. **See the verdict:**

   ```bash
   cd ~/Documents/hq/synth && .venv/bin/synth-eartest-report
   ```

   **Pass:** the last line reads like
   `The metric agrees with your ears on 82% of clear trials (95% CI 70–91%).`

---

## D. Hand the results to the lead (5 min)

The lead reads these files straight from this Mac. Paste this message to the lead:

```text
Hardware session done.
Plug-in check: <which of A1-A7 passed; what failed and what you saw>
Calibration report: ~/.synth/calibration/<newest folder>/report.md (and report.json)
Installed curves: ~/.synth/twin/curves.calibrated.json
Ear-test answers: ~/.synth/eartest/<session>.jsonl
Ear-test verdict: <the last line of synth-eartest-report>
```

To find the two names in angle brackets:

```bash
ls -t ~/.synth/calibration | head -1; ls -t ~/.synth/eartest | head -1
```

---

## If something goes wrong

- **`Problem: No S-1 MIDI port found`**: the cable is charge-only, or the S-1 is off. Use
  the data cable.
- **`Problem: The S-1 made no sound`**: check the volume knob, and that the S-1 receives
  on MIDI channel 3. Another channel works too: add `--channel N` to the command.
- **`Problem: ... not taking CCs`**: the S-1 plays notes but ignores knob messages. Check
  its MIDI channel.
- **The run stopped halfway and there is no time left**: fit what is there. The report
  says how many probes it used.

  ```bash
  cd ~/Documents/hq/synth && .venv/bin/synth-calibrate --resume ~/.synth/calibration/<timestamp> --fit-only
  ```

- **The ear-test page says the server did not answer**: the cockpit stopped. Run `synth`
  again and reload the page; your answers are kept.

A dry run of the whole calibration needs no S-1 and writes nothing outside its own folder
(the lead uses it to test the pipeline):

```bash
cd ~/Documents/hq/synth && .venv/bin/synth-calibrate --dry-run
```
