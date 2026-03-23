"""
windows_rapl_provider.py

GMT Metric Provider for Windows RAPL energy measurements.
Communicates with the ScaphandreDrv kernel driver via rapl_reader.dll.

Requirements:
    - ScaphandreDrv kernel driver installed and running
    - rapl_reader.dll compiled and in the same folder (or on PATH)
    - Python 3.8+

Usage (standalone test):
    python windows_rapl_provider.py

Usage (within GMT):
    Configure in usage_scenario.yml as metric provider.
"""

import ctypes
import ctypes.wintypes
import csv
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


# ── DLL struct (must match rapl_data_t in rapl_reader.c exactly) ──────────────

class RaplData(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("pkg_energy_j",      ctypes.c_double),
        ("dram_energy_j",     ctypes.c_double),
        ("pp0_energy_j",      ctypes.c_double),
        ("pp1_energy_j",      ctypes.c_double),
        ("platform_energy_j", ctypes.c_double),
        ("pkg_tdp_w",         ctypes.c_double),
        ("energy_unit",       ctypes.c_double),
        ("power_unit",        ctypes.c_double),
        ("time_unit",         ctypes.c_double),
        ("cpu_index",         ctypes.c_int),
        ("valid",             ctypes.c_int),
        ("error_msg",         ctypes.c_char * 128),
    ]


# ── DLL loader ────────────────────────────────────────────────────────────────

def _load_dll(dll_path: str | None = None) -> ctypes.CDLL:
    """Load rapl_reader.dll. Searches next to this script if no path given."""
    if dll_path is None:
        dll_path = Path(__file__).parent / "rapl_reader.dll"
    dll = ctypes.CDLL(str(dll_path))

    # read_rapl_all(uint32 cpu_index) -> RaplData  (returned by value)
    dll.read_rapl_all.restype  = RaplData
    dll.read_rapl_all.argtypes = [ctypes.c_uint32]

    # read_single_msr(uint32 msr, uint32 cpu) -> int64
    dll.read_single_msr.restype  = ctypes.c_int64
    dll.read_single_msr.argtypes = [ctypes.c_uint32, ctypes.c_uint32]

    return dll


# ── Sample dataclass (plain dict for easy JSON/CSV export) ───────────────────

def _make_sample(data: RaplData, prev: "dict | None") -> dict:
    """
    Build one measurement sample.

    For energy counters we compute delta_j (Joules since last sample)
    and instantaneous power_w = delta_j / delta_t.
    On the first call prev=None so power_w values are 0.
    """
    now = datetime.now(timezone.utc)
    ts  = now.isoformat()

    sample = {
        "timestamp":          ts,
        "cpu_index":          data.cpu_index,
        # Raw cumulative energy counters (Joules)
        "pkg_energy_j":       round(data.pkg_energy_j,      6),
        "dram_energy_j":      round(data.dram_energy_j,     6),
        "pp0_energy_j":       round(data.pp0_energy_j,      6),
        "pp1_energy_j":       round(data.pp1_energy_j,      6),
        "platform_energy_j":  round(data.platform_energy_j, 6),
        # Power limit / TDP
        "pkg_tdp_w":          round(data.pkg_tdp_w,         3),
        # Unit info
        "energy_unit":        data.energy_unit,
        "power_unit":         data.power_unit,
        "time_unit":          data.time_unit,
        # Derived power (filled in below)
        "pkg_power_w":        0.0,
        "dram_power_w":       0.0,
        "pp0_power_w":        0.0,
    }

    if prev is not None:
        # Parse previous timestamp
        prev_ts = datetime.fromisoformat(prev["timestamp"])
        delta_t = (now - prev_ts).total_seconds()
        if delta_t > 0:
            def pw(key):
                delta = sample[key] - prev[key]
                # Handle MSR wrap-around (32-bit counter * energy_unit)
                if delta < 0:
                    wrap = (2**32) * data.energy_unit
                    delta += wrap
                return round(delta / delta_t, 3)

            sample["pkg_power_w"]  = pw("pkg_energy_j")
            sample["dram_power_w"] = pw("dram_energy_j")
            sample["pp0_power_w"]  = pw("pp0_energy_j")

    return sample


# ── GMT Metric Provider ───────────────────────────────────────────────────────

class WindowsRaplProvider:
    """
    GMT-compatible Metric Provider for Windows RAPL measurements.

    Public API (called by GMT):
        provider = WindowsRaplProvider()
        provider.start()          # begin background sampling
        # ... GMT runs benchmark ...
        provider.stop()           # stop sampling
        provider.export("json")   # or "csv"
    """

    def __init__(
        self,
        sampling_interval_ms: int = 100,
        cpu_index: int = 0,
        dll_path: str | None = None,
        export_path: str | None = None,
        export_format: str = "csv",
    ):
        self.sampling_interval = sampling_interval_ms / 1000.0
        self.cpu_index         = cpu_index
        self.export_path       = export_path
        self.export_format     = export_format.lower()

        self._dll     = _load_dll(dll_path)
        self._samples: list[dict] = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        print(f"[WindowsRaplProvider] Initialized "
              f"(cpu={cpu_index}, interval={sampling_interval_ms}ms)")

    # ── GMT lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        """Start background sampling thread."""
        self._samples.clear()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()
        print("[WindowsRaplProvider] Sampling started.")

    def stop(self):
        """Stop background sampling thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        print(f"[WindowsRaplProvider] Sampling stopped. "
              f"{len(self._samples)} samples collected.")

    def export(self, fmt: str | None = None, file_path: str | None = None) -> str:
        """
        Export collected samples.

        Args:
            fmt:       "json" or "csv" (overrides constructor setting)
            file_path: output file path (overrides constructor setting)

        Returns:
            File path written, or JSON string if no path given.
        """
        fmt       = (fmt or self.export_format).lower()
        file_path = file_path or self.export_path

        if fmt == "json":
            content = json.dumps(self._samples, indent=2)
            if file_path:
                Path(file_path).write_text(content, encoding="utf-8")
                print(f"[WindowsRaplProvider] Exported JSON → {file_path}")
                return file_path
            return content

        elif fmt == "csv":
            if not self._samples:
                print("[WindowsRaplProvider] No samples to export.")
                return ""
            if file_path is None:
                file_path = "windows_rapl_samples.csv"
            keys = list(self._samples[0].keys())
            with open(file_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=keys)
                writer.writeheader()
                writer.writerows(self._samples)
            print(f"[WindowsRaplProvider] Exported CSV → {file_path}")
            return file_path

        else:
            raise ValueError(f"Unsupported export format: {fmt!r}")

    def get_summary(self) -> dict:
        """
        Return aggregated summary of the measurement run.
        Useful for quick reporting without loading the full CSV.
        """
        if not self._samples:
            return {}

        def _total_delta(key):
            """Sum energy delta across all samples (handles wrap-around)."""
            total = 0.0
            for i in range(1, len(self._samples)):
                d = self._samples[i][key] - self._samples[i-1][key]
                if d < 0:
                    # wrap-around: add 2^32 * energy_unit
                    eu = self._samples[i].get("energy_unit", 1e-5)
                    d += (2**32) * eu
                total += d
            return round(total, 6)

        first = self._samples[0]["timestamp"]
        last  = self._samples[-1]["timestamp"]
        t0    = datetime.fromisoformat(first)
        t1    = datetime.fromisoformat(last)
        dur   = (t1 - t0).total_seconds()

        total_pkg  = _total_delta("pkg_energy_j")
        total_dram = _total_delta("dram_energy_j")
        total_pp0  = _total_delta("pp0_energy_j")

        return {
            "start_time":           first,
            "end_time":             last,
            "duration_s":           round(dur, 3),
            "sample_count":         len(self._samples),
            "total_pkg_energy_j":   total_pkg,
            "total_dram_energy_j":  total_dram,
            "total_pp0_energy_j":   total_pp0,
            "avg_pkg_power_w":      round(total_pkg  / dur, 3) if dur > 0 else 0,
            "avg_dram_power_w":     round(total_dram / dur, 3) if dur > 0 else 0,
            "avg_pp0_power_w":      round(total_pp0  / dur, 3) if dur > 0 else 0,
            "pkg_tdp_w":            self._samples[0].get("pkg_tdp_w", 0),
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _sample_loop(self):
        prev = None
        while not self._stop_event.is_set():
            t_start = time.monotonic()

            data = self._dll.read_rapl_all(self.cpu_index)
            if data.valid:
                sample = _make_sample(data, prev)
                self._samples.append(sample)
                prev = sample
            else:
                msg = data.error_msg.decode("utf-8", errors="replace")
                print(f"[WindowsRaplProvider] Read error: {msg}")

            elapsed = time.monotonic() - t_start
            sleep   = self.sampling_interval - elapsed
            if sleep > 0:
                self._stop_event.wait(timeout=sleep)


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print(" Windows RAPL GMT Metric Provider – standalone test")
    print("=" * 60)

    provider = WindowsRaplProvider(
        sampling_interval_ms=500,   # 2 Hz for the test
        cpu_index=0,
        export_format="csv",
        export_path="rapl_test_output.csv",
    )

    print("\nStarting 5-second measurement...")
    provider.start()
    time.sleep(5)
    provider.stop()

    print("\n── Summary ──────────────────────────────────────────")
    summary = provider.get_summary()
    for k, v in summary.items():
        print(f"  {k:<28} {v}")

    print("\n── Last 3 samples ───────────────────────────────────")
    for s in provider._samples[-3:]:
        print(f"  {s['timestamp']}  "
              f"pkg={s['pkg_power_w']:6.2f}W  "
              f"dram={s['dram_power_w']:5.2f}W  "
              f"pp0={s['pp0_power_w']:5.2f}W")

    csv_path = provider.export("csv")
    json_path = provider.export("json", "rapl_test_output.json")

    print(f"\nFiles written:")
    print(f"  CSV  → {csv_path}")
    print(f"  JSON → {json_path}")
    print("\nDone.")
