"""
rapl_server.py

Lightweight HTTP server that exposes Windows RAPL measurements via JSON API.
This runs on the WINDOWS side and bridges the kernel driver to WSL2/Docker.

Requirements:
    - ScaphandreDrv kernel driver installed and running
    - rapl_reader.dll in the same folder
    - Python with conda env (lca) activated

Start (in Anaconda Prompt as Administrator):
    cd C:\\Users\\jahns\\Documents\\CASO\\vs_code\\windows-rapl-driver\\rapl_reader
    conda activate lca
    python rapl_server.py

The server listens on 0.0.0.0:8420 so WSL2 can reach it via the Windows host IP.
"""

import ctypes
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


# ── DLL struct (must match rapl_data_t in rapl_reader.c) ─────────────────────

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


# ── Load DLL ──────────────────────────────────────────────────────────────────

def load_dll():
    dll_path = Path(__file__).parent / "rapl_reader.dll"
    dll = ctypes.CDLL(str(dll_path))
    dll.read_rapl_all.restype  = RaplData
    dll.read_rapl_all.argtypes = [ctypes.c_uint32]
    return dll


dll = load_dll()
_lock = threading.Lock()


# ── RAPL reading ──────────────────────────────────────────────────────────────

_last_sample: dict | None = None

def read_rapl(cpu_index: int = 0) -> dict:
    global _last_sample

    data = dll.read_rapl_all(cpu_index)
    now  = datetime.now(timezone.utc).isoformat()

    if not data.valid:
        return {
            "valid": False,
            "error": data.error_msg.decode("utf-8", errors="replace"),
            "timestamp": now,
        }

    sample = {
        "valid":              True,
        "timestamp":          now,
        "cpu_index":          data.cpu_index,
        "pkg_energy_j":       round(data.pkg_energy_j,      6),
        "dram_energy_j":      round(data.dram_energy_j,     6),
        "pp0_energy_j":       round(data.pp0_energy_j,      6),
        "pp1_energy_j":       round(data.pp1_energy_j,      6),
        "platform_energy_j":  round(data.platform_energy_j, 6),
        "pkg_tdp_w":          round(data.pkg_tdp_w,         3),
        "energy_unit":        data.energy_unit,
        # Derived power (delta vs last sample)
        "pkg_power_w":        0.0,
        "dram_power_w":       0.0,
        "pp0_power_w":        0.0,
    }

    with _lock:
        prev = _last_sample
        if prev and prev["valid"]:
            prev_ts = datetime.fromisoformat(prev["timestamp"])
            curr_ts = datetime.fromisoformat(now)
            delta_t = (curr_ts - prev_ts).total_seconds()
            if delta_t > 0:
                def pw(key):
                    d = sample[key] - prev[key]
                    if d < 0:  # wrap-around
                        d += (2**32) * data.energy_unit
                    return round(d / delta_t, 3)
                sample["pkg_power_w"]  = pw("pkg_energy_j")
                sample["dram_power_w"] = pw("dram_energy_j")
                sample["pp0_power_w"]  = pw("pp0_energy_j")
        _last_sample = sample

    return sample


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class RaplHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # Suppress per-request logs for cleaner output
        pass

    def do_GET(self):
        if self.path == "/rapl" or self.path == "/rapl/":
            data = read_rapl(cpu_index=0)
            body = json.dumps(data, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/health":
            body = b'{"status": "ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error": "not found"}')


# ── Main ──────────────────────────────────────────────────────────────────────

HOST = "0.0.0.0"
PORT = 8420

if __name__ == "__main__":
    print("=" * 60)
    print(f"  Windows RAPL HTTP Bridge Server")
    print(f"  Listening on http://{HOST}:{PORT}")
    print(f"  Endpoints:")
    print(f"    GET /rapl    → current RAPL measurements (JSON)")
    print(f"    GET /health  → health check")
    print("=" * 60)

    # Warmup read
    warmup = read_rapl()
    if warmup["valid"]:
        print(f"\n✅ Driver OK – pkg_tdp_w={warmup['pkg_tdp_w']}W")
    else:
        print(f"\n❌ Driver error: {warmup.get('error')}")

    print(f"\nServer running... (Ctrl+C to stop)\n")

    server = HTTPServer((HOST, PORT), RaplHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()
