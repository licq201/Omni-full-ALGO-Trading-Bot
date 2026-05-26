"""
watchdog.py — Supervisor process for OMNI-ICT autonomy.

Keeps the three long-lived services alive and healthy:

    · server       (FastAPI dashboard + API, uvicorn)
    · orchestrator (signal generator, loop mode)
    · auto_trader  (MT5 executor, if enabled)

Each service is described by a `ServiceSpec`. The watchdog starts them as
subprocesses, pipes their stdout/stderr to per-service log files under
`logs/`, and restarts any that exit with non-zero status — throttled by an
exponential back-off so a broken service doesn't thrash.

A simple "alive" check reads the child process status each cycle. For richer
health-checks you can extend `ServiceSpec.healthcheck` to hit an HTTP endpoint
or parse a heartbeat file.

CLI
---
    python watchdog.py                       # supervise default services
    python watchdog.py --only orchestrator   # run a subset
    python watchdog.py --status              # print running state then exit
    python watchdog.py --stop                # kill all tracked children

All stop signals are graceful (SIGTERM, then SIGKILL after --grace seconds).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from i18n import msg as _msg

log = logging.getLogger("watchdog")

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
LOG_DIR = PROJECT_ROOT / "logs"
STATE_FILE = LOG_DIR / "watchdog_state.json"
# Always use the venv Python so child services get all installed packages
# (uvicorn, fastapi, etc.). Fall back to sys.executable if venv not found.
_venv_py = PROJECT_ROOT / "venv" / "bin" / "python3"
PY = str(_venv_py) if _venv_py.exists() else sys.executable


# ──────────────────────────────────────────────────────────────────────────────
# Service specs
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ServiceSpec:
    name:         str
    argv:         list[str]
    cwd:          Path
    env:          dict = field(default_factory=dict)
    restart:      bool = True
    max_restarts: int = 20                          # per process lifetime
    backoff_s:    float = 2.0                        # initial back-off
    backoff_cap:  float = 120.0                      # max back-off
    healthcheck:  Optional[callable] = None          # optional fn → bool


def _default_specs(project_root: Path) -> list[ServiceSpec]:
    """The default process graph for OMNI-ICT autonomy."""
    return [
        ServiceSpec(
            name="server",
            argv=[PY, "-u", "-m", "uvicorn", "server:app",
                  "--host", "127.0.0.1", "--port", "8787"],
            cwd=project_root,
            env={"PYTHONUNBUFFERED": "1"},
        ),
        ServiceSpec(
            name="swarm",
            argv=[PY, "-u", str(HERE / "swarm.py")],
            cwd=HERE,
            env={"PYTHONUNBUFFERED": "1"},
            restart=True,
            max_restarts=50,
        ),
        ServiceSpec(
            name="orchestrator",
            argv=[PY, "-u", str(HERE / "orchestrator.py"), "--loop", "60"],
            cwd=HERE,
            env={"PYTHONUNBUFFERED": "1"},
        ),
        # auto_trader.py is used as a library by execution_agent in live mode.
        # Do NOT run it as a standalone supervised process — the swarm handles execution.
        # Uncomment below only for legacy single-process mode (without swarm).
        # ServiceSpec(
        #     name="auto_trader",
        #     argv=[PY, "-u", str(HERE / "auto_trader.py"), "--risk", "MODERATE", "--freq", "AGGRESSIVE"],
        #     cwd=HERE,
        #     env={"PYTHONUNBUFFERED": "1"},
        #     restart=True,
        # ),
        ServiceSpec(
            name="telegram_bot",
            argv=[PY, "-u", str(HERE / "telegram_bot.py")],
            cwd=HERE,
            env={"PYTHONUNBUFFERED": "1"},
            restart=True,
            max_restarts=50,
        ),
        # omni_bridge — optional SaaS client bridge.
        # Polls central license server for commands; posts alerts back.
        # Starts only when remote license/bridge mode is explicitly enabled.
        ServiceSpec(
            name="omni_bridge",
            argv=[PY, "-u", str(HERE / "omni_bridge.py")],
            cwd=HERE,
            env={"PYTHONUNBUFFERED": "1"},
            restart=True,
            max_restarts=100,
        ),
    ]


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _remote_license_mode_enabled() -> bool:
    mode = os.getenv("OMNI_LICENSE_MODE", os.getenv("OMNI_AUTH_MODE", "local"))
    return mode.strip().lower() in {"remote", "server", "saas", "license_server"}


def _filter_notification_specs(specs: list[ServiceSpec]) -> list[ServiceSpec]:
    """Select local Telegram or remote bridge process without enabling SaaS by accident."""
    has_tg_token = bool(os.getenv("OMNI_TELEGRAM_TOKEN", ""))
    bridge_enabled = _remote_license_mode_enabled() or _env_flag("OMNI_ENABLE_OMNI_BRIDGE", False)

    filtered: list[ServiceSpec] = []
    for spec in specs:
        if spec.name == "telegram_bot" and not has_tg_token:
            continue
        if spec.name == "omni_bridge" and (has_tg_token or not bridge_enabled):
            continue
        filtered.append(spec)
    return filtered


# ──────────────────────────────────────────────────────────────────────────────
# Running state
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RunningService:
    spec:           ServiceSpec
    proc:           Optional[subprocess.Popen] = None
    restarts:       int = 0
    next_delay:     float = 0.0
    started_at:     Optional[str] = None
    started_at_mono: float = 0.0     # time.monotonic() when last spawned, for stable-run detection
    log_path:       Optional[Path] = None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


def _write_state(running: dict[str, RunningService]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "services": {
            name: {
                "pid":        (r.proc.pid if r.proc else None),
                "alive":      r.is_alive(),
                "restarts":   r.restarts,
                "started_at": r.started_at,
                "log":        str(r.log_path) if r.log_path else None,
            }
            for name, r in running.items()
        },
    }
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def _read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Supervise loop
# ──────────────────────────────────────────────────────────────────────────────

def _write_alert(service_name: str, code: str) -> None:
    """Append an alert entry to logs/alerts.json (atomic write)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    alerts_path = LOG_DIR / "alerts.json"
    alerts = []
    if alerts_path.exists():
        try:
            alerts = json.loads(alerts_path.read_text(encoding="utf-8"))
        except Exception:
            alerts = []
    alerts.append({
        "ts":      datetime.now(timezone.utc).isoformat(),
        "service": service_name,
        "code":    code,
    })
    tmp = alerts_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(alerts, indent=2), encoding="utf-8")
    os.replace(tmp, alerts_path)


def _spawn(spec: ServiceSpec) -> tuple[subprocess.Popen, Path]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{spec.name}.log"
    env = os.environ.copy()
    env.update(spec.env)
    log.info(_msg("watchdog.spawn", name=spec.name, command=" ".join(spec.argv),
                  cwd=spec.cwd, log_path=log_path))
    # Open log file in a with-block so the Python-side handle closes after Popen;
    # the child process inherits the fd and keeps writing until it exits.
    with open(log_path, "ab") as fh:
        proc = subprocess.Popen(
            spec.argv,
            cwd=str(spec.cwd),
            env=env,
            # Explicitly redirect stdin to /dev/null. When watchdog is launched by
            # launchd as a daemon its stdin is a closed/invalid fd; children that
            # inherit it crash before Python's runtime is fully initialised with
            # `OSError: [Errno 9] Bad file descriptor` from init_sys_streams.
            # uvicorn (server.py) is especially sensitive to this. DEVNULL gives
            # every child a clean readable fd0 regardless of how watchdog was
            # started.
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,   # detach from TTY so SIGINT on watchdog won't cascade accidentally
        )
    return proc, log_path


def _terminate(rs: RunningService, grace: float = 5.0) -> None:
    if rs.proc is None or rs.proc.poll() is not None:
        return
    log.info(_msg("watchdog.stop", name=rs.spec.name, pid=rs.proc.pid))
    try:
        rs.proc.terminate()
        rs.proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        log.warning(_msg("watchdog.force_kill", name=rs.spec.name, pid=rs.proc.pid))
        try:
            rs.proc.kill()
        except Exception:
            pass


def _free_port(port: int) -> None:
    """Kill any process holding a TCP port (handles stale server survivors not in state file)."""
    try:
        import subprocess as _sp
        out = _sp.check_output(["lsof", "-ti", f":{port}"], text=True).strip()
        for pid_str in out.splitlines():
            pid = int(pid_str.strip())
            try:
                os.kill(pid, signal.SIGTERM)
                log.info("freed port %d from stale pid=%d", port, pid)
            except ProcessLookupError:
                pass
    except Exception:
        pass


def _kill_survivors() -> None:
    """Kill child PIDs still alive from the previous watchdog run (prevents orphan accumulation)."""
    state = _read_state()
    for name, info in state.get("services", {}).items():
        pid = info.get("pid")
        if not pid:
            continue
        try:
            os.kill(int(pid), 0)  # raises if not running
            log.info("killing survivor from previous run: %s (pid=%s)", name, pid)
            os.kill(int(pid), signal.SIGTERM)
            time.sleep(0.3)
            try:
                os.kill(int(pid), 0)
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass
        except Exception as e:
            log.debug("survivor check %s pid=%s: %s", name, pid, e)
    # Free bound ports so new server instances can start cleanly
    _free_port(8787)


def supervise(specs: list[ServiceSpec],
              *,
              poll_s: float = 2.0,
              grace_s: float = 5.0) -> int:
    """Run the supervise loop until SIGINT / SIGTERM."""
    _kill_survivors()

    running: dict[str, RunningService] = {
        s.name: RunningService(spec=s) for s in specs
    }

    def _shutdown(sig, frame):
        log.info("shutdown signal %s — stopping all services", sig)
        for rs in running.values():
            _terminate(rs, grace=grace_s)
        _write_state(running)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Initial start
    for rs in running.values():
        _start(rs)
    _write_state(running)

    while True:
        for rs in running.values():
            if rs.is_alive():
                # If the service has been alive for STABLE_RUN_S, treat it as
                # healthy and reset its crash budget so transient blow-ups
                # don't accumulate forever.
                if (rs.restarts > 1 and rs.started_at_mono > 0
                        and (time.monotonic() - rs.started_at_mono) >= STABLE_RUN_S):
                    log.info("%s has been stable for %.0fs — resetting restart counter",
                             rs.spec.name, STABLE_RUN_S)
                    rs.restarts = 1   # keep at 1 so the "first start" semantics still hold
                    rs.next_delay = 0.0
                continue
            if rs.proc is None:
                # never started, skip
                continue
            rc = rs.proc.returncode
            log.warning("%s exited rc=%s (restart=%s, restarts=%s)",
                        rs.spec.name, rc, rs.spec.restart, rs.restarts)
            if not rs.spec.restart:
                continue
            if rs.restarts >= rs.spec.max_restarts:
                log.critical("SERVICE %s HIT MAX RESTARTS (%d) — LEAVING STOPPED",
                             rs.spec.name, rs.spec.max_restarts)
                _write_alert(rs.spec.name, "max_restarts_exceeded")
                continue
            # Back-off before restart
            if rs.next_delay > 0:
                log.info("%s backing off %.1fs", rs.spec.name, rs.next_delay)
                time.sleep(rs.next_delay)
            _start(rs)
            rs.next_delay = min(max(rs.next_delay * 2, rs.spec.backoff_s),
                                rs.spec.backoff_cap)

        _write_state(running)
        # Jitter ±10% to spread I/O load and avoid deterministic timing
        time.sleep(poll_s * (0.9 + 0.2 * random.random()))


def _start(rs: RunningService) -> None:
    try:
        rs.proc, rs.log_path = _spawn(rs.spec)
        rs.started_at = datetime.now(timezone.utc).isoformat()
        rs.started_at_mono = time.monotonic()
        rs.restarts += 1
        # fresh back-off on a manual (re)start
        if rs.restarts == 1:
            rs.next_delay = 0.0
    except Exception as e:
        log.exception("failed to spawn %s: %s", rs.spec.name, e)
        rs.next_delay = min(max(rs.next_delay * 2, rs.spec.backoff_s),
                            rs.spec.backoff_cap)


# A service that ran cleanly for at least this long is "stable" — its restart
# counter and back-off get reset so a long-lived service that crashes once a
# day doesn't eventually exhaust max_restarts and get parked permanently.
STABLE_RUN_S: float = 300.0  # 5 minutes


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _print_status() -> int:
    state = _read_state()
    if not state:
        print("watchdog: no state file (not running or never started)")
        return 1
    print(json.dumps(state, indent=2))
    return 0


def _stop_all(grace: float) -> int:
    state = _read_state()
    services = state.get("services", {})
    stopped = 0
    for name, info in services.items():
        pid = info.get("pid")
        if not pid:
            continue
        try:
            os.kill(int(pid), signal.SIGTERM)
            stopped += 1
            print(f"sent SIGTERM to {name} (pid={pid})")
        except ProcessLookupError:
            print(f"{name} pid={pid} not running")
        except Exception as e:
            print(f"failed to stop {name} pid={pid}: {e}")
    if stopped:
        print(f"waiting {grace}s for graceful shutdown...")
        time.sleep(grace)
    return 0


def _validate_configs(project_root: Path) -> list[str]:
    """
    Validate rules.json and config.json before spawning services.
    Returns a list of error strings; empty list means all good.
    """
    errors = []

    rules_path = project_root / "python" / "rules.json"
    if not rules_path.exists():
        errors.append(f"rules.json not found: {rules_path}")
    else:
        try:
            rules = json.loads(rules_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"rules.json parse error: {e}")
            rules = {}

        for field in ("watchlist", "dual_tf", "scaling", "risk_rules"):
            if field not in rules:
                errors.append(f"rules.json missing required field: '{field}'")
        if not isinstance(rules.get("watchlist", None), list) or not rules.get("watchlist"):
            errors.append("rules.json: 'watchlist' must be a non-empty list")
        dual_tf = rules.get("dual_tf", {})
        for sub in ("enabled", "htf_timeframe", "ltf_timeframe"):
            if sub not in dual_tf:
                errors.append(f"rules.json: dual_tf missing field '{sub}'")

    cfg_path = project_root / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"config.json parse error: {e}")
            cfg = {}
        accounts = cfg.get("accounts", [])
        if not isinstance(accounts, list):
            errors.append("config.json: 'accounts' must be a list")
        for i, acc in enumerate(accounts):
            if not acc.get("id"):
                errors.append(f"config.json: accounts[{i}] missing 'id'")
            if not acc.get("data_path"):
                errors.append(f"config.json: accounts[{i}] missing 'data_path'")

    return errors


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="OMNI-ICT autonomy supervisor")
    p.add_argument("--only", nargs="+", help="run only these services by name")
    p.add_argument("--poll", type=float, default=2.0, help="supervise poll interval (s)")
    p.add_argument("--grace", type=float, default=5.0, help="graceful stop timeout (s)")
    p.add_argument("--status", action="store_true", help="print state and exit")
    p.add_argument("--stop", action="store_true", help="stop all tracked children and exit")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.status:
        return _print_status()
    if args.stop:
        return _stop_all(args.grace)

    # Load .env BEFORE importing license — under launchd, child processes
    # don't get .env automatically, only what's in the plist. The license
    # module reads OMNI_LICENSE_KEY at import time, so the load has to happen
    # before that import.
    _env_path = PROJECT_ROOT / ".env"
    if _env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(_env_path, override=False)
            log.info(_msg("watchdog.env_loaded", path=_env_path))
        except ImportError:
            # python-dotenv not installed — parse a minimal subset ourselves.
            for line in _env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
            log.info(_msg("watchdog.env_loaded_minimal", path=_env_path))

    # License check (owner bypass key skips this instantly)
    try:
        import license as _lic
        _lic.check(raise_on_fail=True)
    except SystemExit:
        return 1
    except Exception as e:
        log.error("授权模块错误：%s", e)
        return 1

    cfg_errors = _validate_configs(PROJECT_ROOT)
    if cfg_errors:
        for err in cfg_errors:
            log.error(_msg("watchdog.config_error", error=err))
        log.error(_msg("watchdog.config_abort"))
        return 2

    specs = _default_specs(PROJECT_ROOT)

    # Choose telegram_bot OR omni_bridge. Local mode starts neither unless a
    # personal Telegram token is configured; SaaS bridge is opt-in only.
    specs = _filter_notification_specs(specs)

    if args.only:
        specs = [s for s in specs if s.name in set(args.only)]
        if not specs:
            print(_msg("watchdog.no_matching_service", services=args.only), file=sys.stderr)
            return 2
    return supervise(specs, poll_s=args.poll, grace_s=args.grace)


if __name__ == "__main__":
    sys.exit(main())
