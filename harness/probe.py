"""Per-query resource telemetry: peak RSS, CPU time, block I/O, faults.

Stdlib only, no third-party imports, so this file can be vendored straight
into `opteryx-core/tests/performance/` and imported by the runners without
touching that repo's zero-dependency rule.

Two ways in:

  * ``Probe`` — wraps a single query inside the runner's own process. This is
    the accurate one and needs the runner change (work item 6).
  * ``probe_child`` — wraps a whole benchmark line from outside, measuring the
    runner as a child process. Needs no harness change at all, so the suite
    gets line-level memory and CPU numbers from day one.

Everything unavailable on the host reads back as ``None``. A field is never
guessed, defaulted to zero, or silently carried over from a previous query —
a missing measurement must be distinguishable from a measurement of zero.
"""

from __future__ import annotations

import os
import resource
import sys
import time
from dataclasses import asdict, dataclass

_LINUX = sys.platform.startswith("linux")

# Linux's clear_refs type 5 (CLEAR_REFS_MM_HIWATER_RSS) resets the mm's
# high-water RSS. Without it VmHWM and ru_maxrss are monotonic for the life of
# the process, so every query after the largest one reports the largest one's
# peak — which looks like a plausible number and is not one.
_CLEAR_REFS_RESET_PEAK = "5\n"


@dataclass
class Reading:
    """One resource sample. All byte counts are bytes; all times milliseconds."""

    peak_rss_bytes: int | None = None
    cpu_ms: float | None = None
    disk_read_bytes: int | None = None
    major_faults: int | None = None
    involuntary_ctx_switches: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _read_vm_hwm() -> int | None:
    """Peak RSS in bytes from /proc/self/status, or None off Linux."""
    if not _LINUX:
        return None
    with open("/proc/self/status", "r") as handle:
        for line in handle:
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) * 1024
    return None


def _read_io_bytes() -> int | None:
    """Bytes actually fetched from the block device by this process.

    ``read_bytes`` in /proc/self/io counts real device reads, so it separates a
    page-cache hit from a genuine scan — which is what makes a cold/warm claim
    measured rather than assumed.
    """
    if not _LINUX:
        return None
    with open("/proc/self/io", "r") as handle:
        for line in handle:
            if line.startswith("read_bytes:"):
                return int(line.split()[1])
    return None


def reset_peak_rss() -> bool:
    """Reset the high-water RSS mark. True if the kernel honoured it."""
    if not _LINUX:
        return False
    with open("/proc/self/clear_refs", "w") as handle:
        handle.write(_CLEAR_REFS_RESET_PEAK)
    return True


def verify_peak_rss_reset() -> bool:
    """Prove the reset works on THIS kernel before trusting a run's numbers.

    Allocate ~64MB, drop it, reset, and check the watermark actually fell. Run
    once at bootstrap: if it returns False the suite must record peak RSS as
    unavailable rather than publishing a column of the same number repeated.
    """
    if not _LINUX:
        return False
    ballast = bytearray(64 * 1024 * 1024)
    ballast[::4096] = b"\x01" * (len(ballast) // 4096)
    high = _read_vm_hwm()
    del ballast
    reset_peak_rss()
    low = _read_vm_hwm()
    if high is None or low is None:
        return False
    return low < high


class Probe:
    """Measure one query. Use as a context manager inside the runner.

        with Probe() as p:
            rows = run_query(sql)
        record.update(p.reading.as_dict())
    """

    def __init__(self, reset_peak: bool = True):
        self._reset_peak = reset_peak
        self._peak_reset_ok = False
        self._io_before: int | None = None
        self._rusage_before: resource.struct_rusage | None = None
        self.wall_ms: float | None = None
        self.reading = Reading()

    def __enter__(self) -> "Probe":
        if self._reset_peak:
            self._peak_reset_ok = reset_peak_rss()
        self._io_before = _read_io_bytes()
        self._rusage_before = resource.getrusage(resource.RUSAGE_SELF)
        self._t0 = time.monotonic_ns()
        return self

    def __exit__(self, *exc) -> bool:
        self.wall_ms = (time.monotonic_ns() - self._t0) / 1e6
        after = resource.getrusage(resource.RUSAGE_SELF)
        before = self._rusage_before
        assert before is not None

        cpu_s = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
        io_after = _read_io_bytes()

        self.reading = Reading(
            # Only report a peak we can attribute to THIS query. Without a
            # working reset the watermark belongs to whichever query was
            # biggest so far, and reporting that as this query's peak is worse
            # than reporting nothing.
            peak_rss_bytes=_read_vm_hwm() if self._peak_reset_ok else None,
            cpu_ms=cpu_s * 1000.0,
            disk_read_bytes=(
                io_after - self._io_before
                if io_after is not None and self._io_before is not None
                else None
            ),
            major_faults=after.ru_majflt - before.ru_majflt,
            involuntary_ctx_switches=after.ru_nivcsw - before.ru_nivcsw,
        )
        return False  # never swallow the query's own exception

    @property
    def cpu_efficiency(self) -> float | None:
        """Effective cores used: cpu_ms / wall_ms.

        The direct scoreboard for native threading. A query that gets 5% faster
        while efficiency falls from 11x to 7x is a regression wearing a win's
        clothes, and wall clock alone cannot see it.
        """
        if self.wall_ms is None or self.reading.cpu_ms is None or self.wall_ms <= 0:
            return None
        return self.reading.cpu_ms / self.wall_ms


def probe_child(argv: list[str], **popen_kwargs) -> tuple[int, Reading, float]:
    """Run a benchmark line as a child and measure the child.

    Returns (exit_code, reading, wall_ms). Line-level rather than per-query, but
    it needs no change to opteryx-core, so it is what the first weekly runs use.
    ``ru_maxrss`` for a reaped child is that child's own peak — no reset needed.
    """
    import subprocess

    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.monotonic_ns()
    completed = subprocess.run(argv, **popen_kwargs)
    wall_ms = (time.monotonic_ns() - t0) / 1e6
    after = resource.getrusage(resource.RUSAGE_CHILDREN)

    cpu_s = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    # ru_maxrss is kilobytes on Linux and bytes on macOS.
    peak = after.ru_maxrss * (1024 if _LINUX else 1)

    return (
        completed.returncode,
        Reading(
            peak_rss_bytes=peak,
            cpu_ms=cpu_s * 1000.0,
            disk_read_bytes=None,  # not attributable to a child from the parent
            major_faults=after.ru_majflt - before.ru_majflt,
            involuntary_ctx_switches=after.ru_nivcsw - before.ru_nivcsw,
        ),
        wall_ms,
    )


def host_facts() -> dict:
    """Environment facts recorded once per run, for benchmark_runs."""
    facts = {
        "python_version": sys.version.split()[0],
        "gil_enabled": sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else None,
        "cpu_count": os.cpu_count(),
        "platform": sys.platform,
        "allocator_preload": os.environ.get("LD_PRELOAD")
        or os.environ.get("DYLD_INSERT_LIBRARIES")
        or "",
        "peak_rss_reset_supported": verify_peak_rss_reset(),
    }
    if _LINUX:
        with open("/proc/cpuinfo", "r") as handle:
            for line in handle:
                if line.startswith(("model name", "CPU implementer", "Model")):
                    facts["cpu_model"] = line.split(":", 1)[1].strip()
                    break
        facts["kernel"] = os.uname().release
        with open("/proc/meminfo", "r") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    facts["mem_total_bytes"] = int(line.split()[1]) * 1024
                    break
    return facts
