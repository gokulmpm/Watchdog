"""
start.py — SandMan AI Watchdog launcher
Run:  python start.py
      python start.py stop

Active monitors (sigma-based, no legacy SI score):
  - Window SI       Prepared sand sigma zone breach vs baseline (z-score)
  - Prescription    Additive dose deviation vs AI prescription
  - Component Change Component ID transition detection
  - Bad Batch       SMC vs COSP absolute/% threshold breach
  - Sieve Change    Sieve band % shift vs previous reading
  - SMC Batch       Per-batch prepared_sand_extra LCL/UCL + sigma alerts (Mixer)
"""

import subprocess
import sys
import time
import os
import socket
from pathlib import Path

ROOT   = Path(__file__).parent
PYTHON = sys.executable
LOG    = ROOT / "logs"
LOG.mkdir(exist_ok=True)

SERVICES = [
    {
        "name"  : "Background Monitor",
        "args"  : [PYTHON, "-m", "watchdog.run_alert_monitor"],
        "stdout": str(LOG / "monitor.log"),
        "stderr": str(LOG / "monitor_err.log"),
        "pid"   : str(LOG / "monitor.pid"),
    },
    {
        "name"  : "Dashboard Server",
        "args"  : [PYTHON, "-m", "watchdog.alert_server"],
        "stdout": str(LOG / "server.log"),
        "stderr": str(LOG / "server_err.log"),
        "pid"   : str(LOG / "server.pid"),
    },
]


_IS_WINDOWS = sys.platform == "win32"
_DETACHED = (
    subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000
) if _IS_WINDOWS else 0


def stop():
    """Kill only SandMan watchdog Python processes, not unrelated processes (e.g. Teams)."""
    import psutil

    _OUR_MODULES = {"run_alert_monitor", "alert_server"}
    killed = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (p.info["name"] or "").lower()
            cmd  = " ".join(p.info["cmdline"] or [])

            is_python  = "python" in name
            is_ours    = any(m in cmd for m in _OUR_MODULES)
            if is_python and is_ours and p.pid != os.getpid():
                p.kill()
                killed.append(p.pid)
        except Exception:
            pass
    return killed


def port_listening(port: int) -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def start():
    print()
    print("=" * 52)
    print("  SandMan AI Watchdog")
    print("=" * 52)


    print("\n  Stopping existing watchdog processes...")
    try:
        killed = stop()
        if killed:
            print(f"  Stopped: {killed}")
            time.sleep(1)
        else:
            print("  None running.")
    except ImportError:
        print("  (psutil not found — skipping)")


    for svc in SERVICES:
        open(svc["stdout"], "w").close()
        open(svc["stderr"], "w").close()


    pids = []
    print()
    for svc in SERVICES:
        out = open(svc["stdout"], "w")
        err = open(svc["stderr"], "w")
        popen_kwargs = dict(
            cwd    = str(ROOT),
            stdout = out,
            stderr = err,
            stdin  = subprocess.DEVNULL,
        )
        if _IS_WINDOWS:
            popen_kwargs["creationflags"] = _DETACHED
        else:
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(svc["args"], **popen_kwargs)
        # Save PID
        Path(svc["pid"]).write_text(str(proc.pid))
        pids.append((svc["name"], proc.pid))
        print(f"  Starting  {svc['name']}")
        print(f"            PID {proc.pid}  |  log: logs/{Path(svc['stdout']).name}")

    # Wait for startup
    print()
    print("  Waiting for services to initialise...")
    time.sleep(6)


    import psutil

    def is_watchdog_running(keyword: str) -> tuple:
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = " ".join(p.info["cmdline"] or [])
                if keyword in cmd:
                    return True, p.pid
            except Exception:
                pass
        return False, None

    print()
    print("=" * 52)
    print("  Status")
    print("=" * 52)
    checks = [
        ("Background Monitor", "run_alert_monitor"),
        ("Dashboard Server",   "alert_server"),
    ]
    for name, keyword in checks:
        alive, found_pid = is_watchdog_running(keyword)
        mark  = "[OK]" if alive else "[!!]"
        state = f"RUNNING  (PID {found_pid})" if alive else "FAILED   - check logs/"
        print(f"  {mark}  {name:24s}  {state}")

    ok = port_listening(5055)
    print(f"  {'[OK]' if ok else '[  ]'}  Port 5055               {'LISTENING' if ok else 'not ready yet'}")

    print()
    print("  Alert monitors active:")
    print("    - Window SI         (sigma zone breach vs baseline — z-score)")
    print("    - Prescription      (dose vs AI prescription — poll every 30s)")
    print("    - Component Change  (component_id transition — poll every 30s)")
    print("    - Bad Batch         (SMC vs COSP threshold — poll every 30s)")
    print("    - Sieve Change      (band % shift vs previous — poll every 60s)")
    print("    - SMC Batch         (prepared_sand_extra LCL/UCL + sigma — Mixer webhook)")
    print()
    print("  Data Flow Monitor (autonomous):")
    print("    - Auto-discovers sources per foundry line")
    print("    - Learns normal data rhythm (p50 / p95 / p99 gaps)")
    print("    - Cross-table inference (SCADA + additive + lab + consumption)")
    print("    - Auto-suppresses analysis when data unavailable")
    print("    - Sends confirmation request when all sources go silent")
    print()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        host = s.getsockname()[0]
        s.close()
    except Exception:
        host = "localhost"
    print(f"  Dashboard  ->  http://{host}:5055/?user=<username>")
    print("  Logs dir   ->  logs/")
    print()
    print("  To stop:   python start.py stop")
    print("=" * 52)
    print()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        print("Stopping all watchdog processes...")
        killed = stop()
        print(f"Stopped: {killed}" if killed else "Nothing was running.")
    else:
        start()
