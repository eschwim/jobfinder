import pytest
import yaml

from jobfinder.config import (
    ConfigError,
    load_config,
    load_raw_config,
    parse_config,
    save_config,
)


def _raw(**overrides) -> dict:
    raw = {
        "searches": [{"name": "test", "sites": ["indeed"],
                      "search_term": "platform engineer",
                      "location": "San Francisco, CA"}],
        "filters": {"title_include": ["(?i)platform"],
                    "salary": {"min": 150000}},
        "notify": {"channels": ["pushover"]},
        "repost_window_days": 30,
        "poll_interval_minutes": 60,
    }
    raw.update(overrides)
    return raw


class TestParseConfig:
    def test_parses_valid_config(self):
        cfg = parse_config(_raw())
        assert cfg.searches[0].name == "test"
        assert cfg.poll_interval_minutes == 60
        assert cfg.repost_window_days == 30

    def test_poll_interval_defaults(self):
        raw = _raw()
        del raw["poll_interval_minutes"]
        assert parse_config(raw).poll_interval_minutes == 120

    @pytest.mark.parametrize("bad", [0, -5, "60", 1.5, True])
    def test_poll_interval_rejects_non_positive_ints(self, bad):
        with pytest.raises(ConfigError, match="poll_interval_minutes"):
            parse_config(_raw(poll_interval_minutes=bad))

    def test_bad_regex_names_pattern(self):
        raw = _raw(filters={"title_include": ["(unclosed"]})
        with pytest.raises(ConfigError, match=r"\(unclosed"):
            parse_config(raw)

    def test_empty_channels_allowed_for_digest_only(self):
        # No per-run alert channels is valid — e.g. relying on the daily digest.
        cfg = parse_config(_raw(notify={"channels": []}))
        assert cfg.notify.channels == []

    def test_unknown_channel_still_rejected(self):
        with pytest.raises(ConfigError, match="unknown notify.channels"):
            parse_config(_raw(notify={"channels": ["carrier-pigeon"]}))

    def test_health_channels_default_inherit_channels(self):
        cfg = parse_config(_raw(notify={"channels": ["email"]}))
        assert cfg.notify.health_channels is None
        assert cfg.notify.resolved_health_channels() == ["email"]

    def test_health_channels_decoupled_from_channels(self):
        # The digest-only-with-health case: no match alerts, health to email.
        cfg = parse_config(_raw(notify={"channels": [],
                                        "health_channels": ["email"]}))
        assert cfg.notify.channels == []
        assert cfg.notify.resolved_health_channels() == ["email"]

    def test_health_channels_can_be_explicitly_empty(self):
        cfg = parse_config(_raw(notify={"channels": ["email"],
                                        "health_channels": []}))
        assert cfg.notify.resolved_health_channels() == []

    def test_unknown_health_channel_rejected(self):
        with pytest.raises(ConfigError, match="unknown notify.health_channels"):
            parse_config(_raw(notify={"channels": ["email"],
                                      "health_channels": ["smoke-signal"]}))


class TestSaveConfig:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "config.yaml"
        raw = _raw()
        save_config(path, raw)
        assert load_raw_config(path) == raw
        cfg = load_config(path)
        assert cfg.searches[0].search_term == "platform engineer"

    def test_invalid_config_not_written(self, tmp_path):
        path = tmp_path / "config.yaml"
        save_config(path, _raw())
        before = path.read_text()
        with pytest.raises(ConfigError):
            save_config(path, _raw(searches=[]))
        assert path.read_text() == before
        assert list(tmp_path.glob("*.tmp")) == []

    def test_overwrites_atomically_preserving_unknown_of_dict(self, tmp_path):
        # save writes exactly the dict given, nothing merged from the old file
        path = tmp_path / "config.yaml"
        save_config(path, _raw())
        save_config(path, _raw(repost_window_days=7))
        assert load_raw_config(path)["repost_window_days"] == 7

    def test_comments_are_lost_on_save(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("# a comment\n" + yaml.safe_dump(_raw()))
        save_config(path, load_raw_config(path))
        assert "# a comment" not in path.read_text()


class TestTailorConfig:
    def test_defaults(self):
        cfg = parse_config(_raw())
        assert cfg.tailor.template_path == "resume-template.html"
        assert cfg.tailor.model == "claude-opus-4-8"

    def test_round_trip(self):
        cfg = parse_config(_raw(tailor={"template_path": "my.html",
                                        "model": "claude-opus-4-8"}))
        assert cfg.tailor.template_path == "my.html"

    def test_unknown_key_rejected(self):
        with pytest.raises(ConfigError, match="tailor section is invalid"):
            parse_config(_raw(tailor={"nope": 1}))


class TestRemoteVerificationPolicy:
    def test_default_fail_closed(self):
        assert parse_config(_raw()).remote_verification_policy == "fail_closed"

    def test_fail_open_accepted(self):
        raw = _raw(remote_verification_policy="fail_open")
        assert parse_config(raw).remote_verification_policy == "fail_open"

    def test_unknown_value_rejected(self):
        with pytest.raises(ConfigError, match="remote_verification_policy"):
            parse_config(_raw(remote_verification_policy="maybe"))
