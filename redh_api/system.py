"""The machine, read from /proc and /sys: the boot for the status, and the report of GET /v1/system."""

from __future__ import annotations

import glob
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# hwmon drivers of processors, in the order they're preferred, and the label of their main sensor
CPU_HWMONS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal")
MAIN_LABELS = ("Package id 0", "Tctl", "Tdie")
# thermal zones that measure the processor, or at least the board, where no hwmon does
THERMAL_TYPES = ("x86_pkg_temp", "cpu-thermal", "cpu_thermal", "soc-thermal", "acpitz")


def uptime_seconds(proc: str = "/proc") -> Optional[int]:
    """Whole seconds since boot, or None where /proc has no uptime."""
    try:
        with open(os.path.join(proc, "uptime"), encoding="ascii") as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def boot_id(proc: str = "/proc") -> Optional[str]:
    """The kernel's ID for this boot: a new one after every start."""
    try:
        with open(os.path.join(proc, "sys", "kernel", "random", "boot_id"), encoding="ascii") as f:
            return f.read().strip() or None
    except (OSError, ValueError):
        return None


def read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def read_int(path: str) -> Optional[int]:
    text = read(path)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def cpu_times(proc: str = "/proc") -> Optional[Tuple[int, int]]:
    """Busy and total processor time since boot, in ticks, from the first line of /proc/stat. Waiting
    for I/O counts as idle."""
    fields = (read(os.path.join(proc, "stat")) or "").split("\n", 1)[0].split()
    if len(fields) < 5 or fields[0] != "cpu":
        return None
    try:
        ticks = [int(x) for x in fields[1:9]]  # user nice system idle iowait irq softirq steal
    except ValueError:
        return None
    idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
    return sum(ticks) - idle, sum(ticks)


def cpu_count(proc: str = "/proc") -> Optional[int]:
    """The processors that /proc/stat counts: one line each, cpu0, cpu1 and so on."""
    lines = (read(os.path.join(proc, "stat")) or "").splitlines()
    count = sum(1 for line in lines if line.startswith("cpu") and line[3:4].isdigit())
    return count or None


def load(proc: str = "/proc") -> Optional[List[float]]:
    """The load averages of the last 1, 5 and 15 minutes."""
    try:
        return [float(x) for x in (read(os.path.join(proc, "loadavg")) or "").split()[:3]] or None
    except ValueError:
        return None


def pressure(resource: str, proc: str = "/proc") -> Optional[float]:
    """The share of the last 10 seconds in which tasks waited for the resource (cpu, memory or io),
    in percent: "some avg10" of /proc/pressure. None where the kernel has no pressure stall
    information."""
    for line in (read(os.path.join(proc, "pressure", resource)) or "").splitlines():
        if line.startswith("some "):
            for field in line.split()[1:]:
                key, _, value = field.partition("=")
                if key == "avg10":
                    try:
                        return float(value)
                    except ValueError:
                        return None
    return None


def meminfo(proc: str = "/proc") -> Dict[str, int]:
    """/proc/meminfo, in bytes."""
    values: Dict[str, int] = {}
    for line in (read(os.path.join(proc, "meminfo")) or "").splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[name] = int(parts[0]) * (1024 if parts[1:2] == ["kB"] else 1)
    return values


def temperature(sys: str = "/sys") -> Tuple[Optional[float], Optional[str]]:
    """The processor's temperature in °C, and the sensor it comes from: a hwmon driver of the
    processor first, its main sensor or else its hottest, then a thermal zone."""
    hwmons: Dict[str, str] = {}
    for path in sorted(glob.glob(os.path.join(sys, "class", "hwmon", "hwmon*"))):
        hwmons.setdefault(read(os.path.join(path, "name")) or "", path)
    for name in CPU_HWMONS:
        if name not in hwmons:
            continue
        sensors = []
        for input_path in sorted(glob.glob(os.path.join(hwmons[name], "temp*_input"))):
            value = read_int(input_path)
            if value is not None:
                sensors.append((read(input_path[: -len("input")] + "label") or "", value / 1000))
        if sensors:
            label, celsius = next((s for s in sensors if s[0] in MAIN_LABELS), max(sensors, key=lambda s: s[1]))
            return round(celsius, 1), f"{name}: {label}" if label else name
    zones: Dict[str, str] = {}
    for path in sorted(glob.glob(os.path.join(sys, "class", "thermal", "thermal_zone*"))):
        zones.setdefault(read(os.path.join(path, "type")) or "", path)
    for kind in THERMAL_TYPES:
        value = read_int(os.path.join(zones[kind], "temp")) if kind in zones else None
        if value is not None:
            return round(value / 1000, 1), kind
    return None, None


def rapl_zones(sys: str = "/sys") -> List[str]:
    """The processor packages' energy counters (RAPL), one zone per package: intel-rapl:0 and so on,
    also on AMD's processors; not their subzones, such as intel-rapl:0:0."""
    paths = glob.glob(os.path.join(sys, "class", "powercap", "intel-rapl:*"))
    return sorted(p for p in paths if os.path.basename(p).count(":") == 1)


@dataclass
class Reading:
    """What usage and power are worked out from: the time, the processor's busy and total ticks, and
    each package's energy counter in µJ with the value at which it wraps around."""

    time: float
    cpu: Optional[Tuple[int, int]]
    energy: Optional[List[Tuple[int, int]]]


class Sampler:
    """Processor usage and power, which need two readings: the change from one to the next. A
    reading stays for 5 seconds, so that the numbers change no faster than that; the kernel keeps
    fast readings of the energy counters from everyone but root, because they leak what the
    processor does. A reading older than a minute is too old to compare with: then, and for the
    first request of all, two readings one second apart."""

    FRESH = 5.0
    STALE = 60.0
    FIRST = 1.0

    def __init__(self, proc: str = "/proc", sys: str = "/sys", clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.proc = proc
        self.sys = sys
        self.clock = clock
        self.sleep = sleep
        self.lock = threading.Lock()
        self.last: Optional[Reading] = None
        self.usage: Optional[float] = None
        self.watts: Optional[float] = None
        self.power_problem: Optional[str] = None

    def measure(self) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        """The processor's usage in percent, the power in watts, and why there is no power."""
        with self.lock:
            if self.last is not None and self.clock() - self.last.time < self.FRESH:
                return self.usage, self.watts, self.power_problem
            if self.last is None or self.clock() - self.last.time > self.STALE:
                self.last = self.read()
                self.sleep(self.FIRST)
            current = self.read()
            seconds = current.time - self.last.time
            self.usage = usage(self.last.cpu, current.cpu)
            self.watts = watts(self.last.energy, current.energy, seconds)
            self.last = current
            return self.usage, self.watts, self.power_problem

    def read(self) -> Reading:
        energy: Optional[List[Tuple[int, int]]] = None
        zones = rapl_zones(self.sys)
        if not zones:
            self.power_problem = "There is no energy counter (RAPL) for the processor."
        else:
            counters = [(read_int(os.path.join(z, "energy_uj")), read_int(os.path.join(z, "max_energy_range_uj")))
                        for z in zones]
            if all(value is not None and wrap is not None for value, wrap in counters):
                energy = [(value, wrap) for value, wrap in counters]  # type: ignore[misc]
                self.power_problem = None
            elif not os.access(os.path.join(zones[0], "energy_uj"), os.R_OK):
                self.power_problem = (f"Only root may read {os.path.join(zones[0], 'energy_uj')}; "
                                      "RedhAPI's README says how to let RedhAPI read it.")
            else:
                self.power_problem = "The energy counter (RAPL) can't be read."
        return Reading(self.clock(), cpu_times(self.proc), energy)


def usage(before: Optional[Tuple[int, int]], after: Optional[Tuple[int, int]]) -> Optional[float]:
    if before is None or after is None or after[1] <= before[1]:
        return None
    return round(100 * (after[0] - before[0]) / (after[1] - before[1]), 1)


def watts(before: Optional[List[Tuple[int, int]]], after: Optional[List[Tuple[int, int]]],
          seconds: float) -> Optional[float]:
    if before is None or after is None or len(before) != len(after) or seconds <= 0:
        return None
    joules = 0.0
    for (start, _), (end, wrap) in zip(before, after):
        joules += ((end - start) % wrap if wrap > 0 else end - start) / 1e6
    return round(joules / seconds, 1)


def disk(path: str) -> Dict[str, Any]:
    """The size of the file system the path is on, and what's free on it for anyone but root."""
    try:
        stats = os.statvfs(path)
    except OSError:
        return {"path": path, "total_bytes": None, "free_bytes": None}
    return {"path": path, "total_bytes": stats.f_blocks * stats.f_frsize, "free_bytes": stats.f_bavail * stats.f_frsize}


def report(sampler: Sampler, disks: Sequence[str]) -> Dict[str, Any]:
    """GET /v1/system: what the machine is doing. Whatever it can't say is null."""
    proc, sys = sampler.proc, sampler.sys
    cpu_usage, power, problem = sampler.measure()
    celsius, sensor = temperature(sys)
    memory = meminfo(proc)
    zones = rapl_zones(sys)
    return {
        "cpu": {
            "count": cpu_count(proc),
            "usage_percent": cpu_usage,
            "load": load(proc),
            "pressure_percent": pressure("cpu", proc),
        },
        "temperature": {"celsius": celsius, "sensor": sensor},
        "power": {
            "watts": power,
            "sensor": ", ".join(read(os.path.join(z, "name")) or os.path.basename(z) for z in zones) or None,
            "problem": problem if power is None else None,
        },
        "memory": {
            "total_bytes": memory.get("MemTotal"),
            "available_bytes": memory.get("MemAvailable"),
            "pressure_percent": pressure("memory", proc),
        },
        "swap": {"total_bytes": memory.get("SwapTotal"), "free_bytes": memory.get("SwapFree")},
        "disks": [disk(path) for path in disks],
    }
