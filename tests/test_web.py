import re
from datetime import datetime, timedelta, timezone

import yaml
from fastapi.testclient import TestClient

from jobfinder.filters import Job, Match
from jobfinder.store import Store
from jobfinder.web.app import create_app


class StubScheduler:
    def __init__(self):
        self.triggered = 0
        self.rescheduled = 0
        self.running = False
        self.last_result = None
        self.last_error = None
        self.next_run_at = None
        self.config_error = None
        self.notify_error = None

    async def start(self):
        pass

    async def stop(self):
        pass

    def trigger_now(self):
        self.triggered += 1

    def reschedule(self):
        self.rescheduled += 1


def _write_config(path):
    raw = {
        "searches": [{"name": "test", "sites": ["indeed"],
                      "search_term": "platform engineer"}],
        "filters": {"title_include": ["(?i)platform"]},
        "notify": {"channels": ["pushover"]},
    }
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return raw


def _job(i: int, **kwargs) -> Job:
    defaults = dict(id=f"li-{i}", title=f"Platform Engineer {i}", company=f"Co{i}",
                    site="indeed", url=f"https://example.com/{i}",
                    location="Oakland, CA")
    defaults.update(kwargs)
    return Job(**defaults)


def _client(tmp_path, scheduler=None):
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "test.db"
    _write_config(config_path)
    scheduler = scheduler or StubScheduler()
    app = create_app(config_path=config_path, db_path=db_path,
                     scheduler=scheduler, digest_scheduler=StubScheduler())
    return TestClient(app), config_path, db_path, scheduler


def _backdate_match(db_path, job_id: str, hours: int):
    store = Store(db_path)
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)
             ).isoformat(timespec="seconds")
    store._conn.execute("UPDATE seen_jobs SET matched_at = ? WHERE id = ?",
                        (stamp, job_id))
    store._conn.commit()
    store.close()


class TestJobsPage:
    def test_default_window_is_24h(self, tmp_path):
        client, _, db_path, _ = _client(tmp_path)
        store = Store(db_path)
        store.mark_matched(Match(_job(1)))
        store.mark_matched(Match(_job(2)))
        store.close()
        _backdate_match(db_path, "li-2", hours=30)

        body = client.get("/jobs").text
        assert "Platform Engineer 1" in body
        assert "Platform Engineer 2" not in body

    def test_wider_window_shows_older_matches(self, tmp_path):
        client, _, db_path, _ = _client(tmp_path)
        store = Store(db_path)
        store.mark_matched(Match(_job(1)))
        store.close()
        _backdate_match(db_path, "li-1", hours=30)

        assert "Platform Engineer 1" not in client.get("/jobs?window=24h").text
        assert "Platform Engineer 1" in client.get("/jobs?window=3d").text
        assert "Platform Engineer 1" in client.get("/jobs?window=all").text

    def test_table_partial_renders_salary_and_location(self, tmp_path):
        client, _, db_path, _ = _client(tmp_path)
        store = Store(db_path)
        store.mark_matched(Match(_job(1, min_amount=180000, max_amount=220000,
                                      interval="yearly", currency="USD",
                                      is_remote=True)))
        store.close()
        body = client.get("/jobs/table?window=24h").text
        assert "180,000" in body
        assert "220,000" in body
        assert "Remote" in body

    def test_hourly_salary_normalized_to_yearly(self, tmp_path):
        client, _, db_path, _ = _client(tmp_path)
        store = Store(db_path)
        store.mark_matched(Match(_job(1, min_amount=100, max_amount=120,
                                      interval="hourly", currency="USD")))
        store.close()
        body = client.get("/jobs/table?window=24h").text
        assert "208,000" in body  # 100/hr * 2080
        assert "249,600" in body  # 120/hr * 2080

    def test_unlisted_salary_shows_dashes(self, tmp_path):
        client, _, db_path, _ = _client(tmp_path)
        store = Store(db_path)
        store.mark_matched(Match(_job(1), salary_unlisted=True))
        store.close()
        assert "—" in client.get("/jobs/table?window=24h").text


class TestJobsSorting:
    def _seed(self, db_path):
        store = Store(db_path)
        store.mark_matched(Match(_job(1, title="Alpha Role", min_amount=200000,
                                      max_amount=250000, interval="yearly")))
        store.mark_matched(Match(_job(2, title="Beta Role", min_amount=75,
                                      interval="hourly")))       # 156,000/yr
        store.mark_matched(Match(_job(3, title="Gamma Role")))   # no salary
        store.close()
        # distinct matched_at times so "matched desc" ordering is deterministic
        _backdate_match(db_path, "li-1", hours=3)
        _backdate_match(db_path, "li-2", hours=2)
        _backdate_match(db_path, "li-3", hours=1)

    def _titles(self, client, qs):
        body = client.get(f"/jobs/table?{qs}").text
        return re.findall(r"(Alpha|Beta|Gamma) Role", body)

    def test_sort_by_min_salary_asc_puts_missing_last(self, tmp_path):
        client, _, db_path, _ = _client(tmp_path)
        self._seed(db_path)
        assert self._titles(client, "window=all&sort=min&dir=asc") == \
            ["Beta", "Alpha", "Gamma"]

    def test_sort_by_min_salary_desc_keeps_missing_last(self, tmp_path):
        client, _, db_path, _ = _client(tmp_path)
        self._seed(db_path)
        assert self._titles(client, "window=all&sort=min&dir=desc") == \
            ["Alpha", "Beta", "Gamma"]

    def test_sort_by_title(self, tmp_path):
        client, _, db_path, _ = _client(tmp_path)
        self._seed(db_path)
        assert self._titles(client, "window=all&sort=title&dir=desc") == \
            ["Gamma", "Beta", "Alpha"]

    def test_invalid_sort_falls_back_to_matched_desc(self, tmp_path):
        client, _, db_path, _ = _client(tmp_path)
        self._seed(db_path)
        resp = client.get("/jobs/table?window=all&sort=bogus&dir=sideways")
        assert resp.status_code == 200
        # matched_at desc = insertion order reversed
        assert self._titles(client, "window=all&sort=bogus&dir=sideways") == \
            ["Gamma", "Beta", "Alpha"]

    def test_root_redirects_to_jobs(self, tmp_path):
        client, _, _, _ = _client(tmp_path)
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/jobs"

    def test_healthz(self, tmp_path):
        client, _, _, _ = _client(tmp_path)
        assert client.get("/healthz").json() == {"status": "ok"}


class TestConfigPage:
    def test_renders_existing_config(self, tmp_path):
        client, _, _, _ = _client(tmp_path)
        body = client.get("/config").text
        assert "platform engineer" in body
        assert "(?i)platform" in body

    def test_add_search_round_trips(self, tmp_path):
        client, config_path, _, scheduler = _client(tmp_path)
        resp = client.post("/config/searches", data={
            "name": "sre-remote", "search_term": "site reliability engineer",
            "sites": ["linkedin"], "location": "United States",
            "hours_old": "4", "results_wanted": "30", "country_indeed": "USA",
            "is_remote": "on",
        }, follow_redirects=False)
        assert resp.status_code == 303
        raw = yaml.safe_load(config_path.read_text())
        assert len(raw["searches"]) == 2
        assert raw["searches"][1]["name"] == "sre-remote"
        assert raw["searches"][1]["is_remote"] is True
        assert scheduler.rescheduled == 1

    def test_delete_search_rejected_when_last_one(self, tmp_path):
        # config validation requires at least one search, so deleting the only
        # search re-renders with the error and leaves the file untouched
        client, config_path, _, _ = _client(tmp_path)
        before = config_path.read_text()
        resp = client.post("/config/searches/0/delete")
        assert resp.status_code == 422
        assert config_path.read_text() == before

    def test_delete_search_removes_entry(self, tmp_path):
        client, config_path, _, _ = _client(tmp_path)
        client.post("/config/searches", data={
            "name": "extra", "search_term": "sre", "sites": ["indeed"]})
        resp = client.post("/config/searches/1/delete", follow_redirects=False)
        assert resp.status_code == 303
        raw = yaml.safe_load(config_path.read_text())
        assert [s["name"] for s in raw["searches"]] == ["test"]

    def test_bad_regex_shows_error_and_keeps_file(self, tmp_path):
        client, config_path, _, _ = _client(tmp_path)
        before = config_path.read_text()
        resp = client.post("/config/filters", data={
            "title_include": "(unclosed", "keep_unlisted": "on"})
        assert resp.status_code == 422
        assert "(unclosed" in resp.text
        assert config_path.read_text() == before

    def test_save_filters_writes_lines_as_lists(self, tmp_path):
        client, config_path, _, _ = _client(tmp_path)
        resp = client.post("/config/filters", data={
            "title_include": "(?i)platform\n(?i)sre",
            "employer_exclude": "Initech",
            "locations_deny": "bellevue",
            "salary_min": "150000", "salary_currency": "USD",
            "keep_unlisted": "on",
        }, follow_redirects=False)
        assert resp.status_code == 303
        raw = yaml.safe_load(config_path.read_text())
        assert raw["filters"]["title_include"] == ["(?i)platform", "(?i)sre"]
        assert raw["filters"]["locations_deny"] == ["bellevue"]
        assert raw["filters"]["salary"] == {
            "min": 150000, "currency": "USD", "keep_unlisted": True}

    def test_save_general_updates_poll_interval(self, tmp_path):
        client, config_path, _, _ = _client(tmp_path)
        resp = client.post("/config/general", data={
            "poll_interval_minutes": "45", "repost_window_days": "30",
        }, follow_redirects=False)
        assert resp.status_code == 303
        raw = yaml.safe_load(config_path.read_text())
        assert raw["poll_interval_minutes"] == 45
        assert raw["repost_window_days"] == 30

    def test_notify_saves_separate_health_channels(self, tmp_path):
        # digest-only per-run alerts, but health alerts still go to email
        client, config_path, _, _ = _client(tmp_path)
        resp = client.post("/config/notify", data={
            "health_channels": "email", "health_alerts": "on",
            "digest_threshold": "5", "empty_runs_before_alert": "3",
        }, follow_redirects=False)  # note: no "channels" field -> []
        assert resp.status_code == 303
        raw = yaml.safe_load(config_path.read_text())
        assert raw["notify"]["channels"] == []
        assert raw["notify"]["health_channels"] == ["email"]

    def test_config_page_renders_resolved_health_channels(self, tmp_path):
        # With health_channels absent, the checkboxes should reflect channels.
        client, config_path, _, _ = _client(tmp_path)
        raw = yaml.safe_load(config_path.read_text())
        raw["notify"] = {"channels": ["email"]}
        config_path.write_text(yaml.safe_dump(raw))
        body = client.get("/config").text
        # both a channels checkbox and a health_channels checkbox for email
        assert 'name="channels" value="email"' in body
        assert 'name="health_channels" value="email"' in body
    def test_secret_values_never_rendered(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUSHOVER_TOKEN", "sekrit-token-value")
        monkeypatch.setenv("SMTP_PASSWORD", "sekrit-password")
        client, _, _, _ = _client(tmp_path)
        body = client.get("/status").text
        assert "sekrit" not in body
        assert "PUSHOVER_TOKEN" in body
        assert "set" in body

    def test_unset_secret_flagged(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
        client, _, _, _ = _client(tmp_path)
        assert "not set" in client.get("/status").text

    def test_run_now_triggers_scheduler(self, tmp_path):
        client, _, _, scheduler = _client(tmp_path)
        resp = client.post("/run")
        assert resp.status_code == 200
        assert scheduler.triggered == 1

    def test_digest_run_now_triggers_digest_scheduler(self, tmp_path):
        client, _, _, _ = _client(tmp_path)
        digest_scheduler = client.app.state.digest_scheduler
        resp = client.post("/digest/run")
        assert resp.status_code == 200
        assert digest_scheduler.triggered == 1


class TestDigestConfigPage:
    def test_save_digest_settings_round_trips(self, tmp_path):
        client, config_path, _, _ = _client(tmp_path)
        resp = client.post("/config/digest", data={
            "enabled": "on", "time": "07:30",
            "model": "claude-opus-4-8", "resume_path": "resume.md",
        }, follow_redirects=False)
        assert resp.status_code == 303
        raw = yaml.safe_load(config_path.read_text())
        assert raw["digest"] == {"enabled": True, "time": "07:30",
                                 "model": "claude-opus-4-8",
                                 "resume_path": "resume.md"}
        digest_scheduler = client.app.state.digest_scheduler
        assert digest_scheduler.rescheduled == 1

    def test_bad_digest_time_rejected(self, tmp_path):
        client, config_path, _, _ = _client(tmp_path)
        before = config_path.read_text()
        resp = client.post("/config/digest", data={"time": "25:99"})
        assert resp.status_code == 422
        assert "digest.time" in resp.text
        assert config_path.read_text() == before
