"""Low-spec dynamic resource governor (plan C1): gate pipeline stages on real
memory/load pressure instead of hoping a 16GB Mac survives (HANDOVER §3, iron
law 1: 绝不卡死; iron law 5: 用户随时可能喊停).

stdlib-only. Three zones, decided per sample with a hysteresis band:
  green  — plenty of headroom: full speed.
  yellow — tight: caller should degrade (fewer whisper threads, batch 1).
  red    — dangerous: gate() blocks between stages until pressure clears,
           with a hard wait cap so the pipeline can never deadlock
           (宁轻微过载，不可死等).

Design invariants:
  * the sampler is injectable — tests drive it with synthetic sequences,
    no real machine probing, no real sleeping;
  * a broken sampler degrades to bypass plus an honest `sampler_broken`
    flag in snapshot(); it never fakes green;
  * this module only waits (gate) and only advises (degrade_env /
    governor_event): it never mutates os.environ and never writes files —
    env changes and progress.jsonl writes belong to the caller (run_meeting);
  * state is guarded by one RLock, so the same instance is safe to poke
    from a status thread while the pipeline thread gates.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time

__all__ = [
    "ResourceGovernor", "get_governor", "governor_event",
]

# --- tuned constants (all overridable via MST_GOVERNOR_* / config dict) ------

RED_FLOOR_MB = 512.0            # red free threshold floor: max(512, 8% total)
YELLOW_FLOOR_MB = 1024.0        # yellow free floor: max(1024, 15% total)
RED_LOAD_MULT = 4.0             # red: load1 > cores * 4
YELLOW_LOAD_MULT = 2.5          # yellow: load1 > cores * 2.5
SWAP_SPIKE_MB_PER_10S = 256.0   # swap growing faster than this = thrashing
SWAP_SPIKE_FREE_GUARD_MB = 1024.0  # ...and only fatal while free is scarce
SAMPLER_ERROR_LIMIT = 3         # consecutive failures -> broken/bypass
HYSTERESIS_CONFIRMATIONS = 2    # consecutive same-side samples to switch zone
GATE_POLL_CAP_S = 8.0           # exponential backoff ceiling inside gate()
GATE_POLL_BASE_S = 2.0          # default first sleep between red-zone probes
GATE_MAX_WAIT_S = 300.0         # hard cap: release even if still red
SUBPROC_TIMEOUT_S = 5.0
_OFF_VALUES = {"off", "0", "false", "no"}
_SWAP_UNITS = {"": 1.0, "K": 1.0 / 1024, "M": 1.0, "G": 1024.0,
               "T": 1024.0 * 1024, "P": 1024.0 * 1024 * 1024}
# Yellow-zone slow-down advice. Keys already present in os.environ are
# respected and left untouched — the pipeline owner wins.
DEGRADE_SUGGESTIONS = {"MST_WHISPER_THREADS": "2", "MST_QWEN_BATCH": "1"}


def _cfg_get(env_name: str, cfg_key: str, config: dict | None, cast):
    """Resolve one setting: env var > explicit config dict > None.

    Malformed values fall through to the next layer instead of raising —
    a governor must never crash the pipeline over a typo'd env var
    (same precedence spirit as config.py: env > file > default).
    """
    for raw in (os.environ.get(env_name), (config or {}).get(cfg_key)):
        if raw is None or raw == "":
            continue
        try:
            return cast(raw)
        except (TypeError, ValueError):
            continue
    return None


def _to_bool(v) -> bool:
    return str(v).strip().lower() not in _OFF_VALUES


def _normalize(raw) -> dict:
    """Coerce one sampler reading into the canonical sample dict; raise on
    garbage so the caller can count the error instead of acting on it."""
    if not isinstance(raw, dict):
        raise ValueError(f"sampler returned {type(raw).__name__}, not dict")
    if raw.get("free_mb") is None or raw.get("total_mb") is None:
        raise ValueError("sampler sample missing free_mb/total_mb")
    return {
        "free_mb": float(raw["free_mb"]),
        "total_mb": float(raw["total_mb"]),
        "load1": float(raw.get("load1") or 0.0),
        "cores": int(raw.get("cores") or os.cpu_count() or 1),
        "swap_used_mb": float(raw.get("swap_used_mb") or 0.0),
        "ts": float(raw.get("ts") or time.time()),
    }


# --- default samplers (macOS / Linux) ----------------------------------------

def _run(cmd: list[str]) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True,
                         timeout=SUBPROC_TIMEOUT_S)
    if out.returncode != 0:
        raise OSError(f"{cmd[0]} exited {out.returncode}: "
                      f"{out.stderr.strip()[:120]}")
    return out.stdout


def _sample_darwin() -> dict:
    """macOS: sysctl hw.memsize + vm_stat pages + vm.swapusage + loadavg.

    "Available" follows Activity Monitor's own recipe: free + inactive +
    purgeable pages (inactive is cached-but-reclaimable, purgeable is
    evictable) — raw `Pages free` alone would look like famine on a healthy
    Mac and trip the governor constantly.
    """
    total_mb = int(_run(["sysctl", "-n", "hw.memsize"]).strip()) / (1024 ** 2)
    stat = _run(["vm_stat"])
    m = re.search(r"page size of (\d+)", stat)
    page = int(m.group(1)) if m else 16384

    def pages(name: str) -> int:
        mm = re.search(re.escape(name) + r":\s+(\d+)", stat)
        return int(mm.group(1)) if mm else 0

    avail_mb = (pages("Pages free") + pages("Pages inactive")
                + pages("Pages purgeable")) * page / (1024 ** 2)
    swap_mb = 0.0
    try:  # swap accounting is advisory; failing to read it must not kill sampling
        mm = re.search(r"used\s*=\s*([\d.]+)\s*([KMGT]?)",
                       _run(["sysctl", "-n", "vm.swapusage"]))
        if mm:
            swap_mb = float(mm.group(1)) * _SWAP_UNITS.get(mm.group(2), 1.0)
    except (OSError, ValueError):
        pass
    return {"free_mb": round(avail_mb, 1), "total_mb": round(total_mb, 1),
            "load1": os.getloadavg()[0], "cores": os.cpu_count() or 1,
            "swap_used_mb": round(swap_mb, 1), "ts": time.time()}


def _sample_linux() -> dict:
    """Linux: /proc/meminfo MemAvailable/MemTotal + swap delta + loadavg."""
    info: dict[str, float] = {}
    for line in _run(["cat", "/proc/meminfo"]).splitlines():
        key, _, val = line.partition(":")
        parts = val.split()
        if parts:
            try:
                info[key.strip()] = float(parts[0])  # kB units
            except ValueError:
                continue
    total_mb = info.get("MemTotal", 0.0) / 1024
    if "MemAvailable" in info:
        free_mb = info["MemAvailable"] / 1024
    else:  # pre-3.14 kernels: approximate the same reclaimable pool
        free_mb = (info.get("MemFree", 0.0) + info.get("Buffers", 0.0)
                   + info.get("Cached", 0.0)) / 1024
    swap_mb = (info.get("SwapTotal", 0.0) - info.get("SwapFree", 0.0)) / 1024
    return {"free_mb": round(free_mb, 1), "total_mb": round(total_mb, 1),
            "load1": os.getloadavg()[0], "cores": os.cpu_count() or 1,
            "swap_used_mb": round(swap_mb, 1), "ts": time.time()}


def _default_sampler() -> dict:
    if sys.platform == "darwin":
        return _sample_darwin()
    if sys.platform.startswith("linux"):
        return _sample_linux()
    raise OSError(f"resource_governor: unsupported platform {sys.platform!r}")


# --- the governor -------------------------------------------------------------

class ResourceGovernor:
    """Zone gate between pipeline stages. See module docstring for contract."""

    def __init__(self, sampler=None, config: dict | None = None,
                 poll_interval: float | None = None):
        self.sampler = sampler or _default_sampler
        # Precedence: env var > config dict > constant default.
        enabled = _cfg_get("MST_GOVERNOR", "enabled", config, _to_bool)
        self.enabled = True if enabled is None else enabled
        self.min_free_mb = (_cfg_get("MST_GOVERNOR_MIN_FREE_MB", "min_free_mb",
                                     config, float) or YELLOW_FLOOR_MB)
        self.red_load = _cfg_get("MST_GOVERNOR_RED_LOAD", "red_load",
                                 config, float)  # None => cores * RED_LOAD_MULT
        self.max_wait_s = (_cfg_get("MST_GOVERNOR_MAX_WAIT_S", "max_wait_s",
                                    config, float) or GATE_MAX_WAIT_S)
        self.poll_interval = (poll_interval
                              or _cfg_get("MST_GOVERNOR_SAMPLE_INTERVAL_S",
                                          "sample_interval", config, float)
                              or GATE_POLL_BASE_S)
        self.bypass = not self.enabled      # MST_GOVERNOR=off -> pure pass-through
        self.sampler_broken = False         # sticky: 3 consecutive failures
        self.consecutive_errors = 0
        self._lock = threading.RLock()
        self._last_good: dict | None = None  # last valid sample (error fallback)
        self._last: dict | None = None       # previous observation, for swap rate
        self._zone: str | None = None        # debounced zone; None until 1st sample
        self._pending: str | None = None
        self._pending_count = 0
        self._last_thresholds: dict = {}

    # -- sampling ----------------------------------------------------------

    def sample(self) -> dict:
        """One fresh reading, or the last good one on a transient failure.
        Never raises: a single bad read must not kill the pipeline. After
        SAMPLER_ERROR_LIMIT consecutive failures the governor goes bypass
        (sticky) and snapshot() carries sampler_broken: true — honest, never
        a fake green."""
        with self._lock:
            try:
                s = _normalize(self.sampler())
            except Exception:
                self.consecutive_errors += 1
                if (self.consecutive_errors >= SAMPLER_ERROR_LIMIT
                        and not self.sampler_broken):
                    self.sampler_broken = True
                    self.bypass = True
            else:
                self.consecutive_errors = 0
                self._last_good = s
            return dict(self._last_good) if self._last_good else {}

    # -- zoning --------------------------------------------------------------

    def _thresholds(self, total_mb: float, cores: int) -> dict:
        red_free = max(RED_FLOOR_MB, total_mb * 0.08) if total_mb > 0 else RED_FLOOR_MB
        yellow_free = max(self.min_free_mb, total_mb * 0.15)
        yellow_free = max(yellow_free, red_free)  # invariant: yellow band >= red
        return {
            "red_free_mb": red_free,
            "yellow_free_mb": yellow_free,
            "red_load": self.red_load if self.red_load is not None
                        else cores * RED_LOAD_MULT,
            "yellow_load": cores * YELLOW_LOAD_MULT,
        }

    def _swap_spike(self, s: dict, prev: dict | None, free_mb: float) -> bool:
        """Swap is being pounded out right now: >256MB per 10s while free is
        already scarce — treat as red even if absolute numbers look okay."""
        if not prev:
            return False
        dt = s["ts"] - prev["ts"]
        if dt <= 0:
            return False
        rate = (s["swap_used_mb"] - prev["swap_used_mb"]) / dt * 10.0
        return (rate > SWAP_SPIKE_MB_PER_10S
                and free_mb < SWAP_SPIKE_FREE_GUARD_MB)

    def _raw_zone(self, s: dict, prev: dict | None) -> str:
        thr = self._thresholds(s["total_mb"], s["cores"])
        self._last_thresholds = thr
        if (s["free_mb"] < thr["red_free_mb"]
                or s["load1"] > thr["red_load"]
                or self._swap_spike(s, prev, s["free_mb"])):
            return "red"
        if s["free_mb"] < thr["yellow_free_mb"] or s["load1"] > thr["yellow_load"]:
            return "yellow"
        return "green"

    def zone(self, s: dict | None = None) -> str:
        """Debounced zone. Pass s to feed an explicit observation (tests);
        otherwise one fresh sample is taken. A zone switch needs
        HYSTERESIS_CONFIRMATIONS consecutive same-side raw readings, so one
        load spike no longer flips the machine into a wait. First valid
        reading bootstraps directly. Returns "unknown" when no valid sample
        exists (broken sampler) — never a fabricated green."""
        with self._lock:
            if s is None:
                s = self.sample()
            if self.sampler_broken or not s:
                return "unknown"
            raw = self._raw_zone(s, self._last)
            self._last = s
            if self._zone is None:                      # bootstrap
                self._zone, self._pending, self._pending_count = raw, None, 0
            elif raw == self._zone:
                self._pending, self._pending_count = None, 0
            elif raw == self._pending:
                self._pending_count += 1
                if self._pending_count >= HYSTERESIS_CONFIRMATIONS:
                    self._zone, self._pending, self._pending_count = raw, None, 0
            else:
                self._pending, self._pending_count = raw, 1
            return self._zone

    # -- gating --------------------------------------------------------------

    def gate(self, label: str = "") -> dict:
        """Block while red (poll with exponential backoff), release on
        yellow/green. Bypass (MST_GOVERNOR=off or broken sampler) returns
        immediately. Total red wait is capped at max_wait_s: on timeout we
        release with timed_out: true — a slightly overloaded run beats a
        dead one. Returns {"waited_s", "zone", "action", "timed_out", "label"};
        action ∈ pass | resume | timeout_pass | bypass."""
        if self.bypass or self.sampler_broken:
            return {"waited_s": 0.0, "zone": self._zone or "unknown",
                    "action": "bypass", "timed_out": False, "label": label}
        red_since: float | None = None   # wait clock starts at first red sighting
        interval = self.poll_interval
        cap = max(GATE_POLL_CAP_S, interval)
        step = 0
        while True:
            z = self.zone()
            now = time.monotonic()
            if z != "red":
                waited = (now - red_since) if red_since is not None else 0.0
                return {"waited_s": round(waited, 3), "zone": z,
                        "action": "pass" if red_since is None else "resume",
                        "timed_out": False, "label": label}
            if red_since is None:
                red_since = now
            waited = now - red_since
            if waited >= self.max_wait_s:
                return {"waited_s": round(waited, 3), "zone": z,
                        "action": "timeout_pass", "timed_out": True,
                        "label": label}
            time.sleep(min(interval * (2 ** step), cap))
            step += 1

    # -- advice ---------------------------------------------------------------

    def degrade_env(self) -> dict:
        """Yellow-zone slow-down advice. Keys already present in os.environ
        are omitted (caller's explicit settings win); green/red/unknown/bypass
        yield {} — in red the caller should have gated first, and env changes
        are always the caller's decision, never ours."""
        if self.bypass:
            return {}
        if self.zone() != "yellow":
            return {}
        return {k: v for k, v in DEGRADE_SUGGESTIONS.items()
                if k not in os.environ}

    def snapshot(self) -> dict:
        """Summary for /api/status and progress events. Honest under failure:
        sampler_broken/consecutive_errors travel with the numbers."""
        s = self.sample()
        z = self.zone(s)
        return {
            "enabled": self.enabled,
            "bypass": bool(self.bypass),
            "sampler_broken": bool(self.sampler_broken),
            "zone": z,
            "consecutive_errors": self.consecutive_errors,
            "free_mb": s.get("free_mb"),
            "total_mb": s.get("total_mb"),
            "load1": s.get("load1"),
            "cores": s.get("cores"),
            "swap_used_mb": s.get("swap_used_mb"),
            "ts": s.get("ts"),
            "thresholds": dict(self._last_thresholds),
            "max_wait_s": self.max_wait_s,
            "sample_interval_s": self.poll_interval,
        }


# --- helpers ------------------------------------------------------------------

def governor_event(kind: str, label: str = "", zone: str = "",
                   waited_s: float | None = None) -> dict:
    """Shape one progress event for the caller to append to progress.jsonl
    (expected kinds: "governor_wait", "governor_degrade"). File IO stays with
    the caller — the governor never touches disk."""
    return {"kind": kind, "label": label, "zone": zone,
            "waited_s": round(waited_s, 3) if waited_s is not None else 0.0,
            "ts": round(time.time(), 3)}


_GOVERNOR: ResourceGovernor | None = None


def get_governor() -> ResourceGovernor:
    """Module-level singleton for pipeline/webapp callers. With
    MST_GOVERNOR=off the instance is constructed with bypass=True and
    gate() returns immediately; sampling still works so /api/status can
    keep showing honest numbers."""
    global _GOVERNOR
    if _GOVERNOR is None:
        _GOVERNOR = ResourceGovernor()
    return _GOVERNOR
