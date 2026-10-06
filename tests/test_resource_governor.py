"""Unit tests for the low-spec resource governor (plan C1).

All samplers are injected synthetic sequences — no real machine probing and
no real sleeping except the macOS smoke test (skipUnless darwin). gate() runs
with poll_interval=0.01 so red-zone waits finish in milliseconds.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WS, "webapp"))

import resource_governor as rg  # noqa: E402
from resource_governor import ResourceGovernor  # noqa: E402

# A healthy 8-core, 8GB machine as the synthetic baseline. With defaults:
# red free < 512, yellow free < 1228.8, red load > 32, yellow load > 20.
BASE = {"free_mb": 8000.0, "total_mb": 8192.0, "load1": 0.5,
        "cores": 8, "swap_used_mb": 0.0, "ts": 0.0}


def S(**kw) -> dict:
    s = dict(BASE)
    s.update(kw)
    return s


def fixed(**kw):
    """Sampler returning the same fresh dict forever (red/green holds)."""
    return lambda: S(**kw)


def seq(*samples):
    """Sampler walking a synthetic sequence; StopIteration past the end is
    treated by the governor as sampler errors (tests never rely on it)."""
    it = iter(samples)
    return lambda: next(it)


def boomer(exc: Exception = RuntimeError("sensor gone")):
    return lambda: (_ for _ in ()).throw(exc)


class ZoneHysteresisTest(unittest.TestCase):
    """Spec 1: green->yellow->red plus the debounce band — a single
    crossing never switches the zone; two consecutive same-side do."""

    def test_single_crossing_holds_two_consecutive_switch(self):
        g = ResourceGovernor(sampler=boomer())  # sampler unused: explicit feeds
        self.assertEqual(g.zone(S(free_mb=8000)), "green")       # bootstrap
        self.assertEqual(g.zone(S(free_mb=1100)), "green")       # 1st yellow: hold
        self.assertEqual(g.zone(S(free_mb=1100)), "yellow")      # 2nd yellow: switch
        self.assertEqual(g.zone(S(free_mb=100)), "yellow")       # 1st red: hold
        self.assertEqual(g.zone(S(free_mb=100)), "red")          # 2nd red: switch
        # flapping never lands: pending resets on every contrary reading
        self.assertEqual(g.zone(S(free_mb=8000)), "red")         # 1st green: hold
        self.assertEqual(g.zone(S(free_mb=500)), "red")          # back to red side
        self.assertEqual(g.zone(S(free_mb=8000)), "red")         # still holding

    def test_load_thresholds_trip_zones(self):
        g = ResourceGovernor(sampler=boomer())
        self.assertEqual(g.zone(S(free_mb=8000, load1=10)), "green")
        self.assertEqual(g.zone(S(free_mb=8000, load1=25)), "green")  # bootstrap green
        self.assertEqual(g.zone(S(free_mb=8000, load1=25)), "yellow")  # >2.5*8
        self.assertEqual(g.zone(S(free_mb=8000, load1=40)), "yellow")
        self.assertEqual(g.zone(S(free_mb=8000, load1=40)), "red")     # >4*8

    def test_first_reading_bootstraps_without_debounce(self):
        g = ResourceGovernor(sampler=boomer())
        self.assertEqual(g.zone(S(free_mb=100)), "red")


class GateTest(unittest.TestCase):
    """Spec 2: gate blocks in red, releases on recovery, and times out
    (宁轻微过载不可死等) instead of dead-waiting."""

    def test_gate_passes_immediately_in_green(self):
        g = ResourceGovernor(sampler=fixed(free_mb=8000), poll_interval=0.01)
        r = g.gate("asr")
        self.assertEqual(r["action"], "pass")
        self.assertEqual(r["waited_s"], 0.0)
        self.assertFalse(r["timed_out"])
        self.assertEqual(r["zone"], "green")

    def test_gate_waits_through_red_then_resumes(self):
        # red (bootstrap) -> yellow x2 needed by the debounce band -> release
        g = ResourceGovernor(sampler=seq(S(free_mb=100), S(free_mb=1100),
                                         S(free_mb=1100), S(free_mb=1100)),
                             poll_interval=0.01)
        r = g.gate("qwen-asr")
        self.assertEqual(r["action"], "resume")
        self.assertGreater(r["waited_s"], 0.0)
        self.assertEqual(r["zone"], "yellow")
        self.assertFalse(r["timed_out"])

    def test_gate_times_out_when_red_never_clears(self):
        g = ResourceGovernor(sampler=fixed(free_mb=100, load1=0.1),
                             config={"max_wait_s": 0.05}, poll_interval=0.01)
        r = g.gate("ollama")
        self.assertTrue(r["timed_out"])
        self.assertEqual(r["action"], "timeout_pass")
        self.assertGreaterEqual(r["waited_s"], 0.05)
        self.assertEqual(r["zone"], "red")

    def test_one_raw_red_after_green_history_never_gates(self):
        # debounce: a single red reading on top of green history only arms the
        # pending state — gate must pass with zero wait, not stall on a blip.
        g = ResourceGovernor(sampler=seq(S(free_mb=100, ts=1.0),
                                         S(free_mb=8000, ts=2.0)),
                             poll_interval=0.01)
        self.assertEqual(g.zone(S(free_mb=8000, ts=0.0)), "green")  # history
        r = g.gate("asr")
        self.assertEqual(r["action"], "pass")
        self.assertEqual(r["waited_s"], 0.0)

    def test_gate_blocks_against_already_red_history(self):
        g = ResourceGovernor(sampler=fixed(free_mb=100),
                             config={"max_wait_s": 0.04}, poll_interval=0.01)
        self.assertEqual(g.zone(), "red")         # history: zone already red
        r = g.gate("ollama")
        self.assertTrue(r["timed_out"])
        self.assertEqual(r["action"], "timeout_pass")


class SwapSpikeTest(unittest.TestCase):
    """Spec 4: swap churned out at >256MB/10s while free <1024MB is red."""

    def test_swap_growth_rate_forces_red(self):
        g = ResourceGovernor(sampler=boomer())
        self.assertEqual(g.zone(S(free_mb=5000, swap_used_mb=100.0, ts=0.0)),
                         "green")
        # +300MB over 10s with only 900MB free: red signal, held by debounce
        self.assertEqual(g.zone(S(free_mb=900, swap_used_mb=400.0, ts=10.0)),
                         "green")
        self.assertEqual(g.zone(S(free_mb=900, swap_used_mb=700.0, ts=20.0)),
                         "red")

    def test_swap_spike_ignored_when_free_is_plentiful(self):
        g = ResourceGovernor(sampler=boomer())
        g.zone(S(free_mb=5000, swap_used_mb=100.0, ts=0.0))
        g.zone(S(free_mb=5000, swap_used_mb=400.0, ts=10.0))
        g.zone(S(free_mb=5000, swap_used_mb=700.0, ts=20.0))
        self.assertEqual(g.zone(S(free_mb=5000, swap_used_mb=1000.0, ts=30.0)),
                         "green")

    def test_swap_shrink_and_flat_ts_never_trip(self):
        g = ResourceGovernor(sampler=boomer())
        g.zone(S(free_mb=900, swap_used_mb=500.0, ts=0.0))
        g.zone(S(free_mb=900, swap_used_mb=100.0, ts=10.0))   # swapping back in
        self.assertEqual(g.zone(S(free_mb=900, swap_used_mb=100.0, ts=10.0)),
                         "yellow")  # dt=0 -> no rate; yellow via low free


class SamplerFailureTest(unittest.TestCase):
    """Spec 1b/4: one failure reuses the last sample; three in a row flip to
    a sticky bypass with an honest sampler_broken flag — never fake green."""

    def test_single_failure_falls_back_to_last_good_sample(self):
        calls = {"n": 0}
        good = S(free_mb=8000)

        def flaky():
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("vm_stat hiccup")
            return dict(good)

        g = ResourceGovernor(sampler=flaky)
        first = g.sample()
        self.assertEqual(first["free_mb"], 8000.0)
        second = g.sample()                        # failure -> last good sample
        self.assertEqual(second, first)
        self.assertEqual(g.consecutive_errors, 1)
        self.assertFalse(g.sampler_broken)
        self.assertEqual(g.zone(S(free_mb=8000)), "green")  # still governs

    def test_three_consecutive_failures_enter_bypass(self):
        g = ResourceGovernor(sampler=boomer())
        self.assertEqual(g.sample(), {})
        self.assertFalse(g.sampler_broken)
        g.sample()
        g.sample()
        self.assertTrue(g.sampler_broken)
        self.assertTrue(g.bypass)
        self.assertEqual(g.zone(), "unknown")      # honest, not green
        r = g.gate("asr")
        self.assertEqual(r["action"], "bypass")
        self.assertEqual(r["waited_s"], 0.0)
        snap = g.snapshot()
        self.assertTrue(snap["sampler_broken"])
        self.assertTrue(snap["bypass"])
        self.assertEqual(snap["zone"], "unknown")
        # snapshot() samples once more itself, so the counter keeps climbing
        self.assertGreaterEqual(snap["consecutive_errors"],
                                rg.SAMPLER_ERROR_LIMIT)

    def test_gate_releases_mid_wait_when_sampler_breaks(self):
        # red once, then the sensor dies forever: the first two failures ride
        # the last good sample (still red -> keep waiting, spec 1), but the
        # third flips to broken and the gate must release instead of sitting
        # in the red loop with no data.
        calls = {"n": 0}

        def die_after_first():
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("gone")
            return S(free_mb=100)

        g = ResourceGovernor(sampler=die_after_first, poll_interval=0.01)
        r = g.gate("asr")
        self.assertTrue(g.sampler_broken)
        self.assertEqual(r["action"], "resume")   # released, not bypass at entry
        self.assertEqual(r["zone"], "unknown")    # honest: no valid reading
        self.assertFalse(r["timed_out"])
        self.assertGreater(r["waited_s"], 0.0)


class OffSwitchTest(unittest.TestCase):
    """Spec 5: MST_GOVERNOR=off is a total bypass, but sampling stays alive
    so /api/status can keep reporting honest numbers."""

    def test_env_off_disables_gate_and_degrade(self):
        with mock.patch.dict(os.environ, {"MST_GOVERNOR": "off"}):
            g = ResourceGovernor(sampler=fixed(free_mb=100))  # would be red
            r = g.gate("asr")
            self.assertEqual(r["action"], "bypass")
            self.assertEqual(r["waited_s"], 0.0)
            self.assertFalse(g.degrade_env())
            snap = g.snapshot()
            self.assertFalse(snap["enabled"])
            self.assertTrue(snap["bypass"])
            self.assertFalse(snap["sampler_broken"])

    def test_get_governor_singleton_respects_off(self):
        with mock.patch.dict(os.environ, {"MST_GOVERNOR": "off"}), \
                mock.patch.object(rg, "_GOVERNOR", None):
            g1 = rg.get_governor()
            g2 = rg.get_governor()
            self.assertIs(g1, g2)
            self.assertTrue(g1.bypass)
            self.assertEqual(g1.gate("x")["action"], "bypass")

    def test_get_governor_default_enabled(self):
        env = {k: v for k, v in os.environ.items() if k != "MST_GOVERNOR"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(rg, "_GOVERNOR", None):
            g = rg.get_governor()
            self.assertFalse(g.bypass)


class DegradeEnvTest(unittest.TestCase):
    """Spec 6: yellow advises fewer threads / batch 1; explicit os.environ
    keys are respected and never overwritten; the governor itself never
    mutates os.environ."""

    def setUp(self):
        # 防御性清理：run_meeting.governor_gate 用 os.environ.setdefault 把降级建议
        # 写进环境变量（生产里进程结束即消失），前序测试若触发真实 governor 的
        # yellow 降级会残留 MST_QWEN_BATCH/MST_WHISPER_THREADS——此处自愈，保证
        # 本组测试的预条件成立，而非断言前序测试的卫生。
        for _k in ("MST_QWEN_BATCH", "MST_WHISPER_THREADS"):
            os.environ.pop(_k, None)

    def tearDown(self):
        for _k in ("MST_QWEN_BATCH", "MST_WHISPER_THREADS"):
            os.environ.pop(_k, None)

    def test_yellow_suggests_degrade_without_touching_existing_keys(self):
        leaked = {k: os.environ[k] for k in
                  ("MST_QWEN_BATCH", "MST_WHISPER_THREADS") if k in os.environ}
        self.assertFalse(leaked, f"前序测试泄漏了治理环境变量: {leaked}")
        g = ResourceGovernor(sampler=fixed(free_mb=1100))  # yellow for 8GB
        self.assertEqual(g.zone(), "yellow")               # bootstrap
        with mock.patch.dict(os.environ,
                             {"MST_WHISPER_THREADS": "4"}, clear=False):
            advice = g.degrade_env()
            self.assertEqual(advice, {"MST_QWEN_BATCH": "1"})
            self.assertEqual(os.environ["MST_WHISPER_THREADS"], "4")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MST_WHISPER_THREADS", None)
            os.environ.pop("MST_QWEN_BATCH", None)
            self.assertEqual(g.degrade_env(),
                             {"MST_WHISPER_THREADS": "2",
                              "MST_QWEN_BATCH": "1"})
            self.assertNotIn("MST_WHISPER_THREADS", os.environ)  # advise only

    def test_green_and_red_yield_empty_advice(self):
        green = ResourceGovernor(sampler=fixed(free_mb=8000), poll_interval=0.01)
        red = ResourceGovernor(sampler=fixed(free_mb=100), poll_interval=0.01)
        self.assertEqual(green.zone(), "green")
        self.assertEqual(red.zone(), "red")
        self.assertEqual(green.degrade_env(), {})
        self.assertEqual(red.degrade_env(), {})


class ConfigResolutionTest(unittest.TestCase):
    """Spec 5: env > explicit config dict > built-in default; malformed
    values fall through instead of crashing."""

    def test_env_beats_config_beats_default(self):
        with mock.patch.dict(os.environ, {"MST_GOVERNOR_MAX_WAIT_S": "5"}):
            g = ResourceGovernor(config={"max_wait_s": 99})
            self.assertEqual(g.max_wait_s, 5.0)
        g = ResourceGovernor(config={"max_wait_s": 99})
        self.assertEqual(g.max_wait_s, 99.0)
        g = ResourceGovernor()
        self.assertEqual(g.max_wait_s, 300.0)

    def test_min_free_mb_override_lifts_yellow_threshold(self):
        g = ResourceGovernor(sampler=boomer(), config={"min_free_mb": 3000.0})
        self.assertEqual(g.zone(S(free_mb=4000)), "green")
        self.assertEqual(g.zone(S(free_mb=2500)), "green")   # bootstrap green
        self.assertEqual(g.zone(S(free_mb=2500)), "yellow")  # < custom 3000

    def test_malformed_env_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"MST_GOVERNOR_MAX_WAIT_S": "abc"}):
            g = ResourceGovernor()
            self.assertEqual(g.max_wait_s, 300.0)


class GovernorEventTest(unittest.TestCase):
    """Spec 6b: event dicts shaped for progress.jsonl; IO stays with caller."""

    def test_event_shape(self):
        ev = rg.governor_event("governor_wait", "asr", "red", 1.2345)
        self.assertEqual(ev["kind"], "governor_wait")
        self.assertEqual(ev["label"], "asr")
        self.assertEqual(ev["zone"], "red")
        self.assertEqual(ev["waited_s"], 1.234)
        self.assertIsInstance(ev["ts"], float)
        ev2 = rg.governor_event("governor_degrade", "asr", "yellow")
        self.assertEqual(ev2["kind"], "governor_degrade")
        self.assertEqual(ev2["waited_s"], 0.0)


@unittest.skipUnless(sys.platform == "darwin", "macOS vm_stat sampler smoke")
class MacSmokeTest(unittest.TestCase):
    """Spec 7: the real macOS sampler answers with a sane, complete sample."""

    def test_real_sample_fields_and_sanity(self):
        g = ResourceGovernor()
        s = g.sample()
        for key in ("free_mb", "total_mb", "load1", "cores",
                    "swap_used_mb", "ts"):
            self.assertIn(key, s)
        self.assertGreater(s["total_mb"], 0)
        self.assertGreater(s["free_mb"], 0)
        self.assertLess(s["free_mb"], s["total_mb"] * 1.1)
        self.assertGreaterEqual(s["cores"], 1)
        self.assertGreaterEqual(s["swap_used_mb"], 0)
        z = g.zone()
        self.assertIn(z, ("green", "yellow", "red"))


if __name__ == "__main__":
    unittest.main()
