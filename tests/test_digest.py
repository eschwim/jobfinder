import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import jobfinder.digest as digest
from jobfinder.config import parse_config
from jobfinder.digest import (
    Assessment,
    DigestError,
    DigestResult,
    digest_bodies,
    etc_str,
    evaluate_matches,
    send_digest,
    _sorted_by_max_salary,
)
from jobfinder.filters import Job, Match
from jobfinder.store import Store


def _job(i, **kwargs):
    defaults = dict(id=f"li-{i}", title=f"SRE {i}", company=f"Co{i}",
                    site="linkedin", url=f"https://example.com/{i}",
                    location="Seattle, WA", description="Kubernetes, Python, SLOs")
    defaults.update(kwargs)
    return Job(**defaults)


def _cfg(monkeypatch, tmp_path, api_key="test-key", resume=True, **digest_overrides):
    if api_key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", api_key)
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SMTP_USER", "u@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    if resume:
        (tmp_path / "resume.md").write_text("25 years of SRE: Kubernetes, Python, SLOs")
    raw = {
        "searches": [{"name": "t", "sites": ["indeed"], "search_term": "sre"}],
        "notify": {"channels": ["email"]},
        "digest": {"enabled": True, **digest_overrides},
    }
    return parse_config(raw)


class FakeChannel:
    def __init__(self):
        self.sent = []

    def send(self, subject, text, html_body=None):
        self.sent.append((subject, text, html_body))


class TestSalarySort:
    def test_max_yearly_desc_missing_last(self):
        jobs = [
            _job(1, min_amount=100, max_amount=120, interval="hourly"),  # 249,600
            _job(2),                                                     # none
            _job(3, min_amount=300000, max_amount=350000, interval="yearly"),
            _job(4, min_amount=200000, interval="yearly"),               # max=min
        ]
        assert [j.id for j in _sorted_by_max_salary(jobs)] == \
            ["li-3", "li-1", "li-4", "li-2"]


class TestEvaluateMatches:
    def _fake_anthropic(self, monkeypatch, payload, stop_reason="end_turn"):
        captured = {}

        class FakeStream:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get_final_message(self):
                return SimpleNamespace(
                    stop_reason=stop_reason,
                    content=[SimpleNamespace(type="text", text=json.dumps(payload))])

        class FakeMessages:
            def stream(self, **kwargs):
                captured.update(kwargs)
                return FakeStream()

        class FakeClient:
            def __init__(self, api_key=None):
                captured["api_key"] = api_key
                self.messages = FakeMessages()

        monkeypatch.setattr(digest.anthropic, "Anthropic", FakeClient)
        return captured

    def test_grades_parsed_by_id(self, monkeypatch):
        captured = self._fake_anthropic(monkeypatch, {"assessments": [
            {"id": "li-1", "category": "strong", "reason": "K8s + SLOs",
             "etc_min_usd": 250000, "etc_max_usd": 380000,
             "etc_confidence": 70, "etc_note": "base + big-tech RSUs"},
            {"id": "li-2", "category": "none", "reason": "frontend role",
             "etc_min_usd": 150000, "etc_max_usd": 200000,
             "etc_confidence": 30, "etc_note": "market inference"},
        ]})
        graded = evaluate_matches("resume", [_job(1), _job(2)],
                                  "claude-opus-4-8", "key")
        a = graded["li-1"]
        assert (a.category, a.reason) == ("strong", "K8s + SLOs")
        assert (a.etc_min, a.etc_max, a.etc_confidence) == (250000, 380000, 70)
        assert a.etc_note == "base + big-tech RSUs"
        assert graded["li-2"].category == "none"
        assert captured["model"] == "claude-opus-4-8"
        assert captured["api_key"] == "key"
        assert "resume" in captured["messages"][0]["content"]

    def test_missing_etc_fields_tolerated(self, monkeypatch):
        # Defensive: schema requires them, but don't crash if absent/garbage.
        self._fake_anthropic(monkeypatch, {"assessments": [
            {"id": "li-1", "category": "weak", "reason": "partial"}]})
        a = evaluate_matches("r", [_job(1)], "m", "k")["li-1"]
        assert (a.etc_min, a.etc_max, a.etc_confidence) == (None, None, None)

    def test_confidence_clamped_to_0_100(self, monkeypatch):
        self._fake_anthropic(monkeypatch, {"assessments": [
            {"id": "li-1", "category": "weak", "reason": "r",
             "etc_min_usd": 1, "etc_max_usd": 2,
             "etc_confidence": 250, "etc_note": ""}]})
        assert evaluate_matches("r", [_job(1)], "m", "k")["li-1"].etc_confidence == 100

    def test_invalid_category_dropped(self, monkeypatch):
        self._fake_anthropic(monkeypatch, {"assessments": [
            {"id": "li-1", "category": "maybe", "reason": "?"}]})
        assert evaluate_matches("r", [_job(1)], "m", "k") == {}

    def test_refusal_raises(self, monkeypatch):
        self._fake_anthropic(monkeypatch, {"assessments": []},
                             stop_reason="refusal")
        with pytest.raises(DigestError, match="refusal"):
            evaluate_matches("r", [_job(1)], "m", "k")


class TestEtcStr:
    def test_range_with_confidence(self):
        a = Assessment(etc_min=250000, etc_max=380000, etc_confidence=70)
        assert etc_str(a) == "ETC 250,000–380,000 USD/yr (confidence 70%)"

    def test_point_estimate(self):
        a = Assessment(etc_min=300000, etc_max=300000, etc_confidence=55)
        assert etc_str(a) == "ETC 300,000 USD/yr (confidence 55%)"

    def test_missing_estimate(self):
        assert etc_str(Assessment()) == "ETC not estimated"

    def test_no_confidence_omits_suffix(self):
        a = Assessment(etc_min=100000, etc_max=120000)
        assert etc_str(a) == "ETC 100,000–120,000 USD/yr"


class TestDigestBodies:
    def test_sections_and_reasons(self):
        groups = {"strong": [_job(1)], "weak": [], "none": [_job(2)]}
        graded = {"li-1": Assessment("strong", "matches core skills"),
                  "li-2": Assessment("none", "unrelated domain")}
        text, html_body = digest_bodies(groups, graded)
        assert "Strong match (1)" in text
        assert "Weak match (0)" in text
        assert "matches core skills" in text
        assert "https://example.com/1" in text
        assert "unrelated domain" in html_body
        assert text.index("SRE 1") < text.index("SRE 2")

    def test_etc_line_with_note_in_both_bodies(self):
        groups = {"strong": [_job(1)], "weak": [], "none": []}
        graded = {"li-1": Assessment("strong", "fit", etc_min=280000,
                                     etc_max=400000, etc_confidence=65,
                                     etc_note="base + bonus + startup equity")}
        text, html_body = digest_bodies(groups, graded)
        expected = "ETC 280,000–400,000 USD/yr (confidence 65%) — base + bonus + startup equity"
        assert expected in text
        assert html.escape(expected) in html_body

    def test_ungraded_job_shows_not_estimated(self):
        groups = {"strong": [], "weak": [], "none": [_job(1)]}
        text, _ = digest_bodies(groups, {})
        assert "ETC not estimated" in text


class TestSendDigest:
    def _store_with_match(self, hours_ago=1):
        store = Store(":memory:")
        store.mark_matched(Match(_job(1)))
        stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
                 ).isoformat(timespec="seconds")
        store._conn.execute("UPDATE seen_jobs SET matched_at = ?", (stamp,))
        store._conn.commit()
        return store

    def test_missing_api_key_raises(self, monkeypatch, tmp_path):
        cfg = _cfg(monkeypatch, tmp_path, api_key=None)
        with pytest.raises(DigestError, match="ANTHROPIC_API_KEY"):
            send_digest(cfg, Store(":memory:"), tmp_path)

    def test_missing_resume_raises(self, monkeypatch, tmp_path):
        cfg = _cfg(monkeypatch, tmp_path, resume=False)
        with pytest.raises(DigestError, match="resume not found"):
            send_digest(cfg, Store(":memory:"), tmp_path)

    def test_no_matches_sends_short_email(self, monkeypatch, tmp_path):
        cfg = _cfg(monkeypatch, tmp_path)
        channel = FakeChannel()
        monkeypatch.setattr(digest, "_email_channel", lambda cfg: channel)
        store = Store(":memory:")
        result = send_digest(cfg, store, tmp_path)
        assert result == DigestResult()
        assert "no new matches" in channel.sent[0][0]
        assert store.last_digest()["matches"] == 0

    def test_full_flow_groups_and_records(self, monkeypatch, tmp_path):
        cfg = _cfg(monkeypatch, tmp_path)
        channel = FakeChannel()
        monkeypatch.setattr(digest, "_email_channel", lambda cfg: channel)
        monkeypatch.setattr(digest, "evaluate_matches",
                            lambda *a, **k: {"li-1": Assessment("strong", "good fit")})
        store = self._store_with_match()
        result = send_digest(cfg, store, tmp_path)
        assert (result.matches, result.strong, result.weak, result.none) == (1, 1, 0, 0)
        subject, text, html_body = channel.sent[0]
        assert "1 strong" in subject
        assert "good fit" in text
        row = store.last_digest()
        assert row["strong"] == 1 and row["error"] is None

    def test_ungraded_job_defaults_to_none(self, monkeypatch, tmp_path):
        cfg = _cfg(monkeypatch, tmp_path)
        channel = FakeChannel()
        monkeypatch.setattr(digest, "_email_channel", lambda cfg: channel)
        monkeypatch.setattr(digest, "evaluate_matches", lambda *a, **k: {})
        store = self._store_with_match()
        result = send_digest(cfg, store, tmp_path)
        assert result.none == 1

    def test_old_matches_excluded(self, monkeypatch, tmp_path):
        cfg = _cfg(monkeypatch, tmp_path)
        channel = FakeChannel()
        monkeypatch.setattr(digest, "_email_channel", lambda cfg: channel)
        store = self._store_with_match(hours_ago=30)
        result = send_digest(cfg, store, tmp_path)
        assert result.matches == 0  # outside the 24h window -> "no matches" mail


class TestDigestConfig:
    def test_defaults(self):
        cfg = parse_config({"searches": [{"name": "t", "sites": ["indeed"],
                                          "search_term": "x"}],
                            "notify": {"channels": ["email"]}})
        assert cfg.digest.enabled is False
        assert cfg.digest.time == "18:00"
        assert cfg.digest.model == "claude-opus-4-8"

    @pytest.mark.parametrize("bad", ["25:00", "9:00", "18h00", "18:60", 1800])
    def test_bad_time_rejected(self, bad):
        from jobfinder.config import ConfigError
        with pytest.raises(ConfigError, match="digest.time"):
            parse_config({"searches": [{"name": "t", "sites": ["indeed"],
                                        "search_term": "x"}],
                          "notify": {"channels": ["email"]},
                          "digest": {"time": bad}})
