"""Tests for the ear test (``synth/web/eartest.py``) and its report (``tools/eartest_report.py``).

FABLE rule 1 — "play me pairs, ask which is closer, confirm the number agrees with my
ears" — only works if (a) the side the metric calls closer really IS closer by the
metric, on the exact audio that is played, (b) trials are reproducible, (c) every answer
lands on disk once, and (d) the agreement math is right. Each block below pins one of
those. Rendering runs at a low rate so the whole file takes seconds, not minutes.
"""

from __future__ import annotations

import base64
import io
import json
import math
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

import synth.engine as engine_module
import synth.web.eartest as et
from synth.engine import S1Engine
from synth.match.twin import spectral_loss
from synth.web.server import app
from tests.fakes import FakeMidiWorld
from tools import eartest_report as rep

BASE_URL = "http://127.0.0.1:8766"


@pytest.fixture
def fast(monkeypatch):
    """Low-rate, short renders and a fresh trial cache (trials are cached per id)."""
    monkeypatch.setattr(et, "SR", 8000)
    monkeypatch.setattr(et, "SECONDS", 1.0)
    et._CACHE.clear()
    yield
    et._CACHE.clear()


@pytest.fixture
def answers_dir(tmp_path, monkeypatch):
    d = tmp_path / "eartest"
    monkeypatch.setenv("SYNTH_EARTEST_DIR", str(d))
    return d


@pytest.fixture
def client(monkeypatch, fast, answers_dir):
    e = S1Engine(midi_module=FakeMidiWorld(), audio_auto=False, poll_interval=999)
    monkeypatch.setattr(engine_module, "ENGINE", e)
    yield TestClient(app, base_url=BASE_URL)
    e.stop()


def decode(b64: str) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(base64.b64decode(b64)), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        return pcm.astype(np.float64) / 32767.0, w.getframerate()


# ── the page ─────────────────────────────────────────────────────────────────
def test_page_is_served_with_the_kit_and_its_assets(client):
    r = client.get("/eartest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    assert "Which one is closer to the reference?" in html
    assert 'href="/design/tokens.css"' in html          # the kit, when present
    assert "Can't tell" in html and "Start" in html
    for asset in ("/eartest/eartest.js", "/eartest/eartest.css"):
        assert asset in html
        assert client.get(asset).status_code == 200


def test_page_speaks_sentence_case_and_never_shows_the_answer(client):
    js = client.get("/eartest/eartest.js").text
    html = client.get("/eartest").text
    # The page must not learn (or draw) which side the metric prefers.
    assert "closer\"" not in js and "loss" not in js.lower()
    assert "<canvas" not in html
    # No all-caps labels (design rule 4).
    import re

    labels = re.findall(r">([^<>]{3,})<", html)
    assert not [t for t in labels if t.strip().isupper()], labels


# ── trials ───────────────────────────────────────────────────────────────────
def test_trials_are_deterministic(client):
    one = client.get("/api/eartest/trial", params={"session": "same-seed", "i": 1}).json()
    et._CACHE.clear()                                   # force a fresh render
    two = client.get("/api/eartest/trial", params={"session": "same-seed", "i": 1}).json()
    assert one["id"] == two["id"] == "same-seed.1"
    for key in ("reference", "a", "b"):
        assert one[key] == two[key]
    other = client.get("/api/eartest/trial", params={"session": "another-seed", "i": 1}).json()
    assert other["reference"] != one["reference"]


def test_the_closer_side_is_truly_closer_by_the_metric(client, answers_dir):
    """Decode the WAVs the page would play, recompute spectral_loss, and check the
    side recorded as the metric's answer is the one with the lower loss."""
    sides = set()
    for i in range(5):
        t = client.get("/api/eartest/trial", params={"session": "truth", "i": i}).json()
        ref, sr = decode(t["reference"])
        a, _ = decode(t["a"])
        b, _ = decode(t["b"])
        assert sr == et.SR and ref.size == a.size == b.size == int(et.SECONDS * et.SR)
        la, lb = float(spectral_loss(a, ref, sr)), float(spectral_loss(b, ref, sr))
        r = client.post("/api/eartest/answer", json={"trial_id": t["id"], "choice": "A", "ms": 1200})
        assert r.status_code == 200 and r.json()["ok"]
        row = json.loads((answers_dir / "truth.jsonl").read_text().splitlines()[-1])
        assert row["closer"] == ("A" if la < lb else "B")
        near, far = sorted((la, lb))
        assert far / near > 1.04                          # a real gap survives 16-bit encoding
        assert row["loss_a"] == pytest.approx(la, rel=0.03)
        assert row["loss_b"] == pytest.approx(lb, rel=0.03)
        sides.add(row["closer"])
    assert sides == {"A", "B"}                            # which side is closer is randomized


def test_gaps_follow_the_tier_schedule(fast):
    order = et.schedule("tiers")
    assert len(order) == et.TRIALS == 40
    assert order[0] == "easy"
    assert {name: order.count(name) for name, _lo, _hi in et.TIERS} == {
        "easy": 10, "medium": 10, "hard": 10, "very hard": 10}
    seen = {}
    for i in range(12):
        t = et.get_trial("tiers", i)
        lo, hi = et.TIER_BOUNDS[t.tier]
        assert lo * 0.85 <= t.ratio <= hi * 1.15, (t.tier, t.ratio)
        seen.setdefault(t.tier, []).append(t.ratio)
    assert min(seen["easy"]) > max(seen["very hard"])     # easy really is the bigger gap


def test_loudness_is_matched_so_level_cannot_give_it_away(fast):
    t = et.get_trial("loud", 0)
    rms = [float(np.sqrt(np.mean(x ** 2))) for x in (t.reference, t.a, t.b)]
    assert max(rms) / min(rms) < 1.05
    assert max(float(np.abs(x).max()) for x in (t.reference, t.a, t.b)) <= et.PEAK_CEILING + 1e-9


# ── answers ──────────────────────────────────────────────────────────────────
def test_answers_append_once_and_the_session_advances(client, answers_dir, monkeypatch):
    monkeypatch.setattr(et, "TRIALS", 3)
    first = client.get("/api/eartest/trial", params={"session": "flow"}).json()
    assert first["index"] == 0 and first["total"] == 3 and first["done"] is False
    ok = client.post("/api/eartest/answer",
                     json={"trial_id": first["id"], "choice": "same", "ms": 900}).json()
    assert ok == {"ok": True, "duplicate": False, "answered": 1, "total": 3, "done": False,
                  "path": str(answers_dir / "flow.jsonl")}
    again = client.post("/api/eartest/answer", json={"trial_id": first["id"], "choice": "A", "ms": 5}).json()
    assert again["duplicate"] is True and again["answered"] == 1
    lines = (answers_dir / "flow.jsonl").read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    for key in ("trial_id", "choice", "ms", "closer", "loss_a", "loss_b", "ratio", "tier", "note", "s",
                "a_dk", "b_dk", "a_module", "b_module", "ts", "session", "index"):
        assert key in row, key
    assert row["choice"] == "same" and row["ms"] == 900
    nxt = client.get("/api/eartest/trial", params={"session": "flow"}).json()
    assert nxt["index"] == 1 and nxt["answered"] == 1
    for i in (1, 2):
        t = client.get("/api/eartest/trial", params={"session": "flow", "i": i}).json()
        client.post("/api/eartest/answer", json={"trial_id": t["id"], "choice": "B", "ms": 1000})
    done = client.get("/api/eartest/trial", params={"session": "flow"}).json()
    assert done["done"] is True and done["answered"] == 3
    assert done["path"].endswith("flow.jsonl")


def test_bad_requests_say_what_is_wrong(client, monkeypatch):
    monkeypatch.setattr(et, "TRIALS", 3)
    assert client.get("/api/eartest/trial", params={"session": "../etc"}).status_code == 400
    assert client.get("/api/eartest/trial", params={"session": "ok", "i": 3}).status_code == 404
    def post(body: dict) -> int:
        return client.post("/api/eartest/answer", json=body).status_code

    assert post({"trial_id": "ok.0", "choice": "C", "ms": 1}) == 422
    assert post({"trial_id": "nodot", "choice": "A"}) == 400
    assert post({"trial_id": "a/b.0", "choice": "A"}) == 400


def test_answers_dir_follows_the_env_override(answers_dir):
    assert et.eartest_dir() == answers_dir
    assert et.session_path("x") == answers_dir / "x.jsonl"


# ── the report math ──────────────────────────────────────────────────────────
def _rows(spec: list[tuple[str, str, float]], module: str = "env") -> list[dict]:
    """(choice, closer, ratio) triples -> answer rows."""
    return [{"session": "s", "trial_id": f"s.{i}", "choice": c, "closer": k, "ratio": r, "ms": 5000,
             "a_module": module, "b_module": module} for i, (c, k, r) in enumerate(spec)]


def test_report_agreement_on_a_known_answer_set():
    # 30 clear answers, 24 agree; 10 "can't tell" (excluded from the clear rate).
    spec = [("A", "A", 2.5)] * 12 + [("B", "B", 1.8)] * 12 + [("A", "B", 1.4)] * 6 + [("same", "A", 1.2)] * 10
    s = rep.summarize(_rows(spec))
    o = s["overall"]
    assert (o["trials"], o["clear"], o["agree"], o["cant_tell"]) == (40, 30, 24, 10)
    assert o["rate"] == pytest.approx(0.8)
    assert o["strict_rate"] == pytest.approx(24 / 40)
    lo, hi = o["ci"]
    assert 0.0 <= lo < 0.8 < hi <= 1.0
    assert 0.6 < lo and hi < 0.95                       # n=30 at 80%: roughly 63-93%
    assert o["p_chance"] == pytest.approx(sum(math.comb(30, j) for j in range(24, 31)) / 2 ** 30)
    assert s["verdict"] == (f"The metric agrees with your ears on 80% of clear trials "
                            f"(95% CI {round(100 * lo)}–{round(100 * hi)}%).")
    assert s["bins"]["very hard"]["cant_tell"] == 10 and s["bins"]["very hard"]["clear"] == 0


def test_report_names_the_bins_that_fail():
    spec = ([("A", "A", 2.6)] * 10 + [("A", "A", 1.9)] * 8 + [("B", "A", 1.9)] * 2
            + [("A", "A", 1.45)] * 4 + [("B", "A", 1.45)] * 6
            + [("A", "A", 1.15)] * 2 + [("B", "A", 1.15)] * 8)
    s = rep.summarize(_rows(spec, module="filter"))
    assert s["bins"]["easy"]["rate"] == 1.0 and s["bins"]["medium"]["rate"] == pytest.approx(0.8)
    assert s["failing_bins"] == ["hard", "very hard"]
    assert s["verdict"].startswith("The metric agrees with your ears on 60% of clear trials (95% CI ")
    assert s["verdict"].endswith("; it fails on hard (40%) and very hard (20%) pairs.")
    assert s["underrated_modules"] == {"filter": 16}


def test_report_with_no_clear_answers():
    s = rep.summarize(_rows([("same", "A", 2.5), ("same", "B", 1.2)]))
    assert s["overall"]["rate"] is None and s["overall"]["ci"] is None
    assert s["verdict"] == "No clear answers yet, so there is nothing to compare."


def test_bootstrap_ci_edges_and_determinism():
    assert rep.bootstrap_ci([1] * 20) == (1.0, 1.0)
    assert rep.bootstrap_ci([]) is None
    half = rep.bootstrap_ci([1, 0] * 20, seed=4)
    assert half == rep.bootstrap_ci([1, 0] * 20, seed=4)
    assert half[0] < 0.5 < half[1]


def test_report_bins_match_the_ear_test_tiers():
    """Drift test: the report's bin edges are the ear test's tier edges (one home: eartest.TIERS)."""
    assert [b[0] for b in rep.BINS] == [t[0] for t in et.TIERS]
    assert [b[1] for b in rep.BINS[:-1]] == [t[1] for t in et.TIERS[:-1]]
    for (_n, lo, _hi), (_m, tlo, thi) in zip(rep.BINS, et.TIERS):
        assert lo <= tlo <= thi


def test_report_cli_reads_a_session_by_name(tmp_path, capsys):
    rows = _rows([("A", "A", 2.5)] * 9 + [("B", "A", 1.2)] * 3 + [("same", "B", 1.5)])
    (tmp_path / "s.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert rep.main(["s", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1].startswith("The metric agrees with your ears on 75% of clear trials")
    assert rep.main(["s", "--dir", str(tmp_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["overall"]["clear"] == 12 and data["files"] == [str(tmp_path / "s.jsonl")]
    assert rep.main(["missing", "--dir", str(tmp_path)]) == 1


def test_end_to_end_answering_like_the_metric_scores_100(client, answers_dir):
    """Answer each trial with the side the audio says is closer: the report must say 100%."""
    for i in range(4):
        t = client.get("/api/eartest/trial", params={"session": "e2e", "i": i}).json()
        ref, sr = decode(t["reference"])
        la = float(spectral_loss(decode(t["a"])[0], ref, sr))
        lb = float(spectral_loss(decode(t["b"])[0], ref, sr))
        client.post("/api/eartest/answer", json={"trial_id": t["id"], "choice": "A" if la < lb else "B"})
    s = rep.summarize(rep.read_rows([answers_dir / "e2e.jsonl"]))
    assert s["overall"]["clear"] == 4 and s["overall"]["rate"] == 1.0
