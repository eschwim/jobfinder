import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from jobfinder.config import DigestConfig
from jobfinder.runner import RunResult
from jobfinder.web.scheduler import DigestScheduler, PollScheduler


def _cfg(minutes=999):
    return SimpleNamespace(poll_interval_minutes=minutes, repost_window_days=60)


def _result():
    return RunResult(started_at=datetime.now(timezone.utc))


async def _until(predicate, timeout=2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


def _scheduler(cfg=None, runs=None):
    sched = PollScheduler(Path("unused.yaml"), Path("unused.db"))
    sched._load_config = lambda: cfg
    runs = runs if runs is not None else []

    def fake_run(config):
        runs.append(config)
        return _result()

    sched._run_sync = fake_run
    return sched, runs


class TestPollScheduler:
    def test_runs_once_then_sleeps(self):
        async def scenario():
            sched, runs = _scheduler(cfg=_cfg())
            await sched.start()
            await _until(lambda: len(runs) == 1)
            await asyncio.sleep(0.05)
            assert len(runs) == 1  # 999-minute interval: no second run
            assert sched.next_run_at is not None
            await sched.stop()
        asyncio.run(scenario())

    def test_trigger_now_interrupts_sleep(self):
        async def scenario():
            sched, runs = _scheduler(cfg=_cfg())
            await sched.start()
            await _until(lambda: len(runs) == 1 and sched.next_run_at is not None)
            sched.trigger_now()
            await _until(lambda: len(runs) == 2)
            await sched.stop()
        asyncio.run(scenario())

    def test_reschedule_rereads_interval_without_running(self):
        async def scenario():
            cfg_holder = {"cfg": _cfg(minutes=999)}
            sched = PollScheduler(Path("unused.yaml"), Path("unused.db"))
            sched._load_config = lambda: cfg_holder["cfg"]
            runs = []
            sched._run_sync = lambda c: (runs.append(c), _result())[1]
            await sched.start()
            await _until(lambda: len(runs) == 1 and sched.next_run_at is not None)
            first_due = sched.next_run_at

            cfg_holder["cfg"] = _cfg(minutes=10)
            sched.reschedule()
            await _until(lambda: sched.next_run_at != first_due)
            assert len(runs) == 1  # rescheduled, not re-run
            assert sched.next_run_at < first_due
            await sched.stop()
        asyncio.run(scenario())

    def test_invalid_config_idles_until_woken(self):
        async def scenario():
            cfg_holder = {"cfg": None}
            sched = PollScheduler(Path("unused.yaml"), Path("unused.db"))

            def load():
                if cfg_holder["cfg"] is None:
                    sched.config_error = "config file not found"
                    return None
                sched.config_error = None
                return cfg_holder["cfg"]

            sched._load_config = load
            runs = []
            sched._run_sync = lambda c: (runs.append(c), _result())[1]
            await sched.start()
            await asyncio.sleep(0.05)
            assert runs == []
            assert sched.config_error

            cfg_holder["cfg"] = _cfg()  # "config saved" via the UI
            sched.reschedule()
            await _until(lambda: len(runs) == 1)
            assert sched.config_error is None
            await sched.stop()
        asyncio.run(scenario())

    def test_crashing_run_does_not_kill_loop(self):
        async def scenario():
            sched, _ = _scheduler(cfg=_cfg())
            calls = []

            def boom(cfg):
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("scrape exploded")
                return _result()

            sched._run_sync = boom
            await sched.start()
            await _until(lambda: len(calls) == 1)
            sched.trigger_now()
            await _until(lambda: len(calls) == 2)  # loop survived the crash
            await sched.stop()
        asyncio.run(scenario())


def _digest_cfg(time="18:00", enabled=True):
    return SimpleNamespace(digest=DigestConfig(enabled=enabled, time=time),
                           repost_window_days=60)


class TestDigestScheduler:
    def test_next_fire_is_future_occurrence(self):
        now = datetime.now().astimezone()
        soon = (now + timedelta(minutes=5)).strftime("%H:%M")
        earlier = (now - timedelta(minutes=5)).strftime("%H:%M")

        fire_soon = DigestScheduler._next_fire(_digest_cfg(time=soon))
        assert timedelta(0) < fire_soon - now <= timedelta(minutes=6)

        fire_earlier = DigestScheduler._next_fire(_digest_cfg(time=earlier))
        assert timedelta(hours=23) < fire_earlier - now < timedelta(hours=24)

    def test_disabled_idles_but_manual_trigger_runs(self):
        async def scenario():
            sched = DigestScheduler(Path("unused.yaml"), Path("unused.db"))
            sched._load_config = lambda: _digest_cfg(enabled=False)
            runs = []
            sched._run_sync = lambda cfg: runs.append(cfg)
            await sched.start()
            await asyncio.sleep(0.05)
            assert runs == []
            assert sched.next_run_at is None
            sched.trigger_now()  # test-drive the digest before enabling it
            await _until(lambda: len(runs) == 1)
            await sched.stop()
        asyncio.run(scenario())

    def test_enabled_schedules_next_fire(self):
        async def scenario():
            sched = DigestScheduler(Path("unused.yaml"), Path("unused.db"))
            sched._load_config = lambda: _digest_cfg(time="00:00")
            runs = []
            sched._run_sync = lambda cfg: runs.append(cfg)
            await sched.start()
            await _until(lambda: sched.next_run_at is not None)
            assert runs == []  # not due yet
            await sched.stop()
        asyncio.run(scenario())

    def test_invalid_config_idles(self):
        async def scenario():
            sched = DigestScheduler(Path("unused.yaml"), Path("unused.db"))
            sched._load_config = lambda: None
            runs = []
            sched._run_sync = lambda cfg: runs.append(cfg)
            await sched.start()
            await asyncio.sleep(0.05)
            sched.trigger_now()
            await asyncio.sleep(0.05)
            assert runs == []  # nothing to run without a config
            await sched.stop()
        asyncio.run(scenario())
