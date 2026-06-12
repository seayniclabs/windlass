#!/usr/bin/env python3
"""
Windlass — Schedule-Aware Docker Service Manager

Reads schedule.yaml and manages Docker Compose stacks according to
defined schedules. Serves an HTTP API that StdOut polls for status.

Usage:
  windlass --run              # Evaluate schedule once (use with cron)
  windlass --serve            # HTTP server + internal scheduler (use with Docker)
  windlass --serve --port N   # Custom port (default: 8116)

Endpoints:
  GET  /status.json    → current service states, memory, upcoming events
  POST /commands.json  → [{service, action}] start/stop/restart commands
  POST /exec           → {command} run a command, return {exitCode, stdout, stderr}
  GET  /health         → {"ok": true}
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml

try:
    from croniter import croniter
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_DIR = Path(os.environ.get("WINDLASS_CONFIG", "/opt/windlass"))
SCHEDULE_FILE = CONFIG_DIR / "schedule.yaml"
STATE_FILE = CONFIG_DIR / "state.json"
LOG_FILE = CONFIG_DIR / "windlass.log"
SCHEDULE_INTERVAL_SEC = int(os.environ.get("WINDLASS_INTERVAL", "300"))  # 5 min
MAX_EVENTS = 100

# --- StdOut watchdog (Windlass watches StdOut because StdOut can't watch itself) ---
# StdOut is the eyes/brain; Windlass is the external watchdog. If StdOut's health endpoint
# goes dark for N consecutive checks, Windlass restarts its container. A circuit breaker caps
# restarts in a window so a crash-loop doesn't get hammered forever (escalates via log instead).
STDOUT_URL = os.environ.get("STDOUT_URL", "http://localhost:8112").strip().rstrip("/")
STDOUT_CONTAINER = os.environ.get("STDOUT_CONTAINER", "stdout").strip()
WATCHDOG_ENABLED = os.environ.get("WINDLASS_WATCHDOG", "true").strip().lower() in ("1", "true", "yes")
WATCHDOG_INTERVAL_SEC = int(os.environ.get("WINDLASS_WATCHDOG_INTERVAL", "30"))
WATCHDOG_FAIL_THRESHOLD = int(os.environ.get("WINDLASS_WATCHDOG_FAILS", "3"))  # consecutive fails before restart
WATCHDOG_MAX_RESTARTS = int(os.environ.get("WINDLASS_WATCHDOG_MAX_RESTARTS", "5"))  # per window
WATCHDOG_RESTART_WINDOW_SEC = int(os.environ.get("WINDLASS_WATCHDOG_WINDOW", "3600"))  # 1h
MEMORY_SHED_FREE_MB_THRESHOLD = int(os.environ.get("WINDLASS_MEMORY_SHED_FREE_MB", "1024"))
N8N_WORKFLOWS_FILE = Path(os.environ.get("WINDLASS_N8N_WORKFLOWS", str(CONFIG_DIR / "n8n-workflows.json")))
# Optional live n8n REST (e.g. WINDLASS_N8N_BASE_URL=http://localhost:5678/api/v1)
WINDLASS_N8N_BASE_URL = os.environ.get("WINDLASS_N8N_BASE_URL", "").strip().rstrip("/")
WINDLASS_N8N_API_KEY = (
    os.environ.get("WINDLASS_N8N_API_KEY", "").strip()
    or os.environ.get("N8N_API_KEY", "").strip()
)

EXEC_ALLOWED_PREFIXES = [
    "docker ", "docker-compose ", "curl ", "dig ", "nslookup ",
    "ping ", "netstat ", "ss ", "openssl ", "cat /",
    "git -C ", "git log", "git status", "git rev-parse", "git describe",
]
EXEC_BLOCKED_PATTERNS = [
    "rm -rf /", "mkfs", "dd if=", ":(){:|:&};:", "chmod -R 777 /",
    "> /dev/sda", "| bash", "| sh",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("windlass")

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"services": {}, "events": []}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def log_event(state: dict, service: str | None, action: str, reason: str) -> None:
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": service or "system",
        "action": action,
        "reason": reason,
    }
    state.setdefault("events", []).append(event)
    if len(state["events"]) > MAX_EVENTS:
        state["events"] = state["events"][-MAX_EVENTS:]


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------

def load_schedule() -> dict:
    if not SCHEDULE_FILE.exists():
        log.warning("schedule.yaml not found at %s", SCHEDULE_FILE)
        return {}
    try:
        data = yaml.safe_load(SCHEDULE_FILE.read_text())
        return data.get("services", {}) if data else {}
    except Exception as e:
        log.error("Failed to parse schedule.yaml: %s", e)
        return {}


def is_in_schedule_window(cron_start: str, cron_stop: str, now: datetime | None = None) -> bool:
    """Return True if `now` falls within a schedule window defined by cron_start/cron_stop."""
    if not HAS_CRONITER:
        log.warning("croniter not installed — treating all scheduled services as always-on")
        return True
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        # Find the most recent start time before now
        start_iter = croniter(cron_start, now)
        last_start = start_iter.get_prev(datetime)

        # Find the most recent stop time before now
        stop_iter = croniter(cron_stop, now)
        last_stop = stop_iter.get_prev(datetime)

        return last_start > last_stop
    except Exception as e:
        log.error("Cron evaluation error: %s", e)
        return False


def get_upcoming_events(services: dict, limit: int = 10) -> list[dict]:
    if not HAS_CRONITER:
        return []
    events = []
    now = datetime.now(timezone.utc)
    for name, cfg in services.items():
        if cfg.get("type") != "schedule":
            continue
        cron_start = cfg.get("cron_start")
        cron_stop = cfg.get("cron_stop")
        if cron_start:
            try:
                it = croniter(cron_start, now)
                next_dt = it.get_next(datetime)
                events.append({"time": next_dt.isoformat(), "service": name, "action": "start"})
            except Exception:
                pass
        if cron_stop:
            try:
                it = croniter(cron_stop, now)
                next_dt = it.get_next(datetime)
                events.append({"time": next_dt.isoformat(), "service": name, "action": "stop"})
            except Exception:
                pass
    events.sort(key=lambda e: e["time"])
    return events[:limit]


def get_schedule_windows(services: dict) -> list[dict]:
    if not HAS_CRONITER:
        return []
    windows = []
    now = datetime.now(timezone.utc)
    for name, cfg in services.items():
        if cfg.get("type") != "schedule":
            continue
        cron_start = cfg.get("cron_start")
        cron_stop = cfg.get("cron_stop")
        if not cron_start or not cron_stop:
            continue
        try:
            svc_windows = []
            start_iter = croniter(cron_start, now)
            stop_iter = croniter(cron_stop, now)
            for _ in range(7):  # next 7 windows
                s = start_iter.get_next(datetime)
                e = stop_iter.get_next(datetime)
                if e < s:
                    e = stop_iter.get_next(datetime)
                svc_windows.append({"start": s.isoformat(), "end": e.isoformat()})
            windows.append({"service": name, "windows": svc_windows})
        except Exception:
            pass
    return windows


def _n8n_windows_from_file(limit: int = 7) -> list[dict]:
    if not HAS_CRONITER or not N8N_WORKFLOWS_FILE.exists():
        return []
    try:
        raw = json.loads(N8N_WORKFLOWS_FILE.read_text())
    except Exception:
        return []

    workflows = raw.get("workflows", []) if isinstance(raw, dict) else []
    now = datetime.now(timezone.utc)
    windows = []
    for wf in workflows:
        cron = wf.get("cron")
        name = wf.get("name")
        if not cron or not name:
            continue
        try:
            it = croniter(cron, now)
            runs = []
            for _ in range(limit):
                run_at = it.get_next(datetime)
                runs.append({
                    "start": run_at.isoformat(),
                    "end": (run_at + timedelta(minutes=5)).isoformat(),
                })
            windows.append({"name": name, "cron": cron, "windows": runs})
        except Exception:
            continue
    return windows


def _cron_expression_from_node(node: dict) -> str | None:
    params = node.get("parameters") or {}
    if params.get("cronExpression"):
        return str(params["cronExpression"]).strip()
    rule = params.get("rule") or {}
    if isinstance(rule, dict) and rule.get("expression"):
        return str(rule["expression"]).strip()
    trigger_times = params.get("triggerTimes") or {}
    items = trigger_times.get("item") if isinstance(trigger_times, dict) else None
    if isinstance(items, list) and items:
        ce = items[0].get("cronExpression") if isinstance(items[0], dict) else None
        if ce:
            return str(ce).strip()
    return None


def _n8n_windows_from_api(limit: int = 7) -> list[dict]:
    """Fetch active workflows from n8n REST and derive next run windows from Cron nodes."""
    if not HAS_CRONITER or not WINDLASS_N8N_BASE_URL or not WINDLASS_N8N_API_KEY:
        return []
    url = f"{WINDLASS_N8N_BASE_URL}/workflows"
    req = urllib.request.Request(
        url,
        headers={"X-N8N-API-KEY": WINDLASS_N8N_API_KEY},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        log.warning("n8n workflows HTTP %s: %s", e.code, e.reason)
        return []
    except Exception as e:
        log.warning("n8n workflows fetch failed: %s", e)
        return []

    workflows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(workflows, list):
        return []

    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for wf in workflows:
        if not wf or not wf.get("active"):
            continue
        name = wf.get("name") or wf.get("id") or "workflow"
        nodes = wf.get("nodes") or []
        cron_expr = None
        for node in nodes:
            if not isinstance(node, dict):
                continue
            ntype = str(node.get("type") or "")
            if ntype == "n8n-nodes-base.cron" or ".cron" in ntype:
                cron_expr = _cron_expression_from_node(node)
                if cron_expr:
                    break
        if not cron_expr:
            continue
        try:
            it = croniter(cron_expr, now)
            runs = []
            for _ in range(limit):
                run_at = it.get_next(datetime)
                runs.append({
                    "start": run_at.isoformat(),
                    "end": (run_at + timedelta(minutes=5)).isoformat(),
                })
            out.append({"name": name, "cron": cron_expr, "windows": runs})
        except Exception:
            continue
    return out


def get_n8n_workflow_windows(limit: int = 7) -> list[dict]:
    """Merge file-based snapshot with live n8n API (when configured)."""
    merged: dict = {}
    for wf in _n8n_windows_from_file(limit):
        key = (wf.get("name") or "", wf.get("cron") or "")
        merged[key] = wf
    for wf in _n8n_windows_from_api(limit):
        key = (wf.get("name") or "", wf.get("cron") or "")
        merged[key] = wf
    return list(merged.values())


# ---------------------------------------------------------------------------
# Docker operations
# ---------------------------------------------------------------------------

def docker_compose_up(compose_path: str, service_name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=compose_path, capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            log.error("compose up failed for %s: %s", service_name, result.stderr)
            return False
        log.info("Started %s", service_name)
        return True
    except Exception as e:
        log.error("Failed to start %s: %s", service_name, e)
        return False


def docker_compose_down(compose_path: str, service_name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "compose", "stop"],
            cwd=compose_path, capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            log.error("compose stop failed for %s: %s", service_name, result.stderr)
            return False
        log.info("Stopped %s", service_name)
        return True
    except Exception as e:
        log.error("Failed to stop %s: %s", service_name, e)
        return False


def get_container_states(containers: list[str]) -> tuple[str, float]:
    """Return (state, memory_mb) for a list of container names."""
    if not containers:
        return "unknown", 0.0
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format",
             "{{.Name}} {{.State.Status}} {{.State.Running}}"] + containers,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return "stopped", 0.0

        lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
        running_count = sum(1 for l in lines if "true" in l.lower())

        if running_count == 0:
            state = "stopped"
        elif running_count == len(containers):
            state = "running"
        else:
            state = "partial"

        # Get memory
        mem_result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}"] + containers,
            capture_output=True, text=True, timeout=10,
        )
        total_mb = 0.0
        for line in mem_result.stdout.strip().splitlines():
            try:
                used = line.split("/")[0].strip()
                if "GiB" in used:
                    total_mb += float(used.replace("GiB", "").strip()) * 1024
                elif "MiB" in used:
                    total_mb += float(used.replace("MiB", "").strip())
                elif "kB" in used:
                    total_mb += float(used.replace("kB", "").strip()) / 1024
            except Exception:
                pass

        return state, round(total_mb, 1)
    except Exception:
        return "unknown", 0.0


def get_system_memory() -> tuple[int, int]:
    """Return (total_mb, free_mb)."""
    if not HAS_PSUTIL:
        try:
            with open("/proc/meminfo") as f:
                info = {}
                for line in f:
                    k, v = line.split(":")
                    info[k.strip()] = int(v.strip().split()[0])
            total = info.get("MemTotal", 0) // 1024
            free = (info.get("MemAvailable", 0)) // 1024
            return total, free
        except Exception:
            return 0, 0
    mem = psutil.virtual_memory()
    return mem.total // (1024 * 1024), mem.available // (1024 * 1024)


# ---------------------------------------------------------------------------
# Core scheduler
# ---------------------------------------------------------------------------

def build_status(schedule: dict, state: dict) -> dict:
    now = datetime.now(timezone.utc)
    service_states = []
    total_docker_mem = 0.0

    for name, cfg in schedule.items():
        svc_state = state.get("services", {}).get(name, {})
        containers = cfg.get("containers", [])
        current_state, mem_mb = get_container_states(containers)
        total_docker_mem += mem_mb

        # Detect idle for on-demand services
        idle_since = svc_state.get("idle_since")
        if cfg.get("type") == "on-demand":
            if current_state == "running":
                idle_since = svc_state.get("idle_since")  # preserve existing
            else:
                idle_since = None

        service_states.append({
            "name": name,
            "type": cfg.get("type", "manual"),
            "state": current_state,
            "memory_mb": mem_mb,
            "last_started": svc_state.get("last_started"),
            "last_stopped": svc_state.get("last_stopped"),
            "idle_since": idle_since,
            "next_start": None,
            "next_stop": None,
            "priority": cfg.get("priority", 5),
            "description": cfg.get("description", ""),
            "containers": containers,
            "last_memory_shed_reason": svc_state.get("last_memory_shed_reason"),
        })

    # Docker-level memory
    try:
        docker_limit_result = subprocess.run(
            ["docker", "info", "--format", "{{.MemTotal}}"],
            capture_output=True, text=True, timeout=5,
        )
        docker_limit_mb = int(docker_limit_result.stdout.strip()) // (1024 * 1024) if docker_limit_result.returncode == 0 else 0
    except Exception:
        docker_limit_mb = 0

    sys_total, sys_free = get_system_memory()

    return {
        "last_updated": now.isoformat(),
        "summary": {
            "running": sum(1 for s in service_states if s["state"] == "running"),
            "stopped": sum(1 for s in service_states if s["state"] == "stopped"),
            "total": len(service_states),
            "docker_memory_used_mb": round(total_docker_mem, 1),
            "docker_memory_limit_mb": docker_limit_mb,
            "system_memory_total_mb": sys_total,
            "system_memory_free_mb": sys_free,
            "scheduler_interval_sec": SCHEDULE_INTERVAL_SEC,
        },
        "services": service_states,
        "upcoming_events": get_upcoming_events(schedule),
        "recent_events": list(reversed(state.get("events", [])[-20:])),
        "schedule_windows": get_schedule_windows(schedule),
        "n8n_workflow_windows": get_n8n_workflow_windows(),
        "service_analytics": state.get("analytics", {}),
    }


def run_scheduler(schedule: dict | None = None) -> None:
    """Evaluate schedule once and update state."""
    if schedule is None:
        schedule = load_schedule()
    if not schedule:
        log.info("No services defined in schedule.yaml")
        return

    with _state_lock:
        state = load_state()
        now = datetime.now(timezone.utc)
        changed = False
        analytics = state.setdefault("analytics", {})

        for name, cfg in schedule.items():
            svc_type = cfg.get("type", "manual")
            compose_path = cfg.get("compose_path")
            containers = cfg.get("containers", [])

            if svc_type == "manual" or not compose_path:
                continue

            current_state, _ = get_container_states(containers)
            svc_state = state.setdefault("services", {}).setdefault(name, {})
            svc_analytics = analytics.setdefault(name, {"hourly": {}, "idle_minutes_total": 0, "samples": 0})
            hour_key = now.strftime("%H")
            bucket = svc_analytics["hourly"].setdefault(hour_key, {"running": 0, "idle": 0, "total": 0})
            bucket["total"] += 1
            svc_analytics["samples"] += 1

            if svc_type == "always":
                if current_state != "running":
                    log.info("always-on service %s is down — starting", name)
                    if docker_compose_up(compose_path, name):
                        svc_state["last_started"] = now.isoformat()
                        log_event(state, name, "service_started", "always-on restart")
                        changed = True

            elif svc_type == "schedule":
                cron_start = cfg.get("cron_start")
                cron_stop = cfg.get("cron_stop")
                if not cron_start or not cron_stop:
                    continue

                should_run = is_in_schedule_window(cron_start, cron_stop, now)

                if should_run and current_state == "stopped":
                    log.info("scheduled window: starting %s", name)
                    if docker_compose_up(compose_path, name):
                        svc_state["last_started"] = now.isoformat()
                        log_event(state, name, "service_started", "schedule window")
                        changed = True
                elif not should_run and current_state == "running":
                    log.info("outside schedule window: stopping %s", name)
                    if docker_compose_down(compose_path, name):
                        svc_state["last_stopped"] = now.isoformat()
                        log_event(state, name, "service_stopped", "outside schedule window")
                        changed = True

            elif svc_type == "on-demand":
                idle_minutes = cfg.get("idle_shutdown_minutes")
                if not idle_minutes or current_state != "running":
                    svc_state["idle_since"] = None
                    if current_state == "running":
                        bucket["running"] += 1
                    continue

                if not svc_state.get("idle_since"):
                    svc_state["idle_since"] = now.isoformat()
                    bucket["running"] += 1
                else:
                    idle_since = datetime.fromisoformat(svc_state["idle_since"])
                    if idle_since.tzinfo is None:
                        idle_since = idle_since.replace(tzinfo=timezone.utc)
                    elapsed = (now - idle_since).total_seconds() / 60
                    bucket["running"] += 1
                    bucket["idle"] += 1
                    svc_analytics["idle_minutes_total"] += SCHEDULE_INTERVAL_SEC / 60
                    if elapsed >= idle_minutes:
                        log.info("on-demand idle shutdown: stopping %s (idle %.1f min)", name, elapsed)
                        if docker_compose_down(compose_path, name):
                            svc_state["last_stopped"] = now.isoformat()
                            svc_state["idle_since"] = None
                            log_event(state, name, "service_stopped", f"idle timeout ({idle_minutes}m)")
                            changed = True
            elif current_state == "running":
                bucket["running"] += 1

        # Memory pressure auto-shedding for on-demand services.
        _, free_mb = get_system_memory()
        if free_mb and free_mb < MEMORY_SHED_FREE_MB_THRESHOLD:
            candidates = []
            for name, cfg in schedule.items():
                if cfg.get("type") != "on-demand":
                    continue
                current_state, mem_mb = get_container_states(cfg.get("containers", []))
                if current_state == "running" and cfg.get("compose_path"):
                    candidates.append({
                        "name": name,
                        "compose_path": cfg.get("compose_path"),
                        "priority": int(cfg.get("priority", 5)),
                        "memory_mb": mem_mb,
                    })
            candidates.sort(key=lambda c: (c["priority"], c["memory_mb"]), reverse=True)
            recovered_mb = 0.0
            for cand in candidates:
                if free_mb + recovered_mb >= MEMORY_SHED_FREE_MB_THRESHOLD:
                    break
                if docker_compose_down(cand["compose_path"], cand["name"]):
                    svc_state = state.setdefault("services", {}).setdefault(cand["name"], {})
                    svc_state["last_stopped"] = now.isoformat()
                    recovered_mb += cand["memory_mb"]
                    reason = (
                        f"memory pressure (free={free_mb}MB < {MEMORY_SHED_FREE_MB_THRESHOLD}MB); "
                        f"auto-shed priority={cand['priority']} recovered~{round(cand['memory_mb'], 1)}MB"
                    )
                    svc_state["last_memory_shed_reason"] = reason
                    log_event(state, cand["name"], "memory_shed", reason)
                    changed = True

        if changed:
            log_event(state, None, "sync_completed", f"Evaluated {len(schedule)} services")
        save_state(state)

    log.info("Scheduler run complete (%d services)", len(schedule))


def control_service(service_name: str, action: str, schedule: dict) -> dict:
    """Execute start/stop/restart for a named service. Returns {ok, message}."""
    cfg = schedule.get(service_name)
    if not cfg:
        return {"ok": False, "error": f"Service '{service_name}' not found in schedule"}

    compose_path = cfg.get("compose_path")
    if not compose_path:
        return {"ok": False, "error": f"No compose_path for '{service_name}'"}

    with _state_lock:
        state = load_state()
        svc_state = state.setdefault("services", {}).setdefault(service_name, {})
        now = datetime.now(timezone.utc).isoformat()

        success = False
        if action in ("start", "restart"):
            success = docker_compose_up(compose_path, service_name)
            if success:
                svc_state["last_started"] = now
                log_event(state, service_name, "manual_start", f"manual {action}")
        elif action == "stop":
            success = docker_compose_down(compose_path, service_name)
            if success:
                svc_state["last_stopped"] = now
                log_event(state, service_name, "manual_stop", "manual stop")

        save_state(state)

    return {"ok": success, "service": service_name, "action": action}


def execute_command(command: str) -> dict:
    """Run a shell command; enforce allowlist. Returns {exitCode, stdout, stderr}."""
    if "\n" in command or "\r" in command or "\x00" in command:
        return {"exitCode": 1, "stdout": "", "stderr": "Command blocked: NUL/newlines are not allowed"}
    if "&&" in command or "||" in command:
        return {"exitCode": 1, "stdout": "", "stderr": "Command blocked: shell chaining (&& or ||) is not allowed"}
    for ch in (";", "|", "$", "`"):
        if ch in command:
            return {"exitCode": 1, "stdout": "", "stderr": "Command blocked: shell metacharacters are not allowed"}
    cmd_lower = command.lower().strip()

    for blocked in EXEC_BLOCKED_PATTERNS:
        if blocked in cmd_lower:
            return {"exitCode": 1, "stdout": "", "stderr": f"Command blocked by safety policy: {blocked}"}

    allowed = any(cmd_lower.startswith(p) for p in EXEC_ALLOWED_PREFIXES)
    if not allowed:
        return {
            "exitCode": 1, "stdout": "",
            "stderr": f"Command not in allowlist. Allowed prefixes: {', '.join(EXEC_ALLOWED_PREFIXES)}",
        }

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30,
        )
        return {
            "exitCode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"exitCode": 1, "stdout": "", "stderr": "Command timed out (30s limit)"}
    except Exception as e:
        return {"exitCode": 1, "stdout": "", "stderr": str(e)}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

_schedule_cache: dict = {}
_status_cache: dict = {}
_cache_lock = threading.Lock()


def refresh_status_cache() -> None:
    global _status_cache
    schedule = load_schedule()
    with _state_lock:
        state = load_state()
    status = build_status(schedule, state)
    with _cache_lock:
        _schedule_cache.clear()
        _schedule_cache.update(schedule)
        _status_cache = status


class WindlassHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        log.debug("HTTP %s", format % args)

    def send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/status.json", "/status"):
            with _cache_lock:
                data = dict(_status_cache)
            if not data:
                refresh_status_cache()
                with _cache_lock:
                    data = dict(_status_cache)
            self.send_json(data)

        elif self.path in ("/health", "/health.json"):
            self.send_json({"ok": True, "version": "1.0.0"})

        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            self.send_json({"error": "Invalid JSON"}, 400)
            return

        if self.path in ("/commands.json", "/commands"):
            commands = payload if isinstance(payload, list) else [payload]
            results = []
            with _cache_lock:
                schedule = dict(_schedule_cache)
            for cmd in commands:
                svc = cmd.get("service")
                action = cmd.get("action")
                if not svc or not action:
                    results.append({"ok": False, "error": "service and action required"})
                    continue
                result = control_service(svc, action, schedule)
                results.append(result)
            # Refresh cache after control actions
            threading.Thread(target=refresh_status_cache, daemon=True).start()
            self.send_json({"results": results})

        elif self.path == "/exec":
            command = payload.get("command", "").strip()
            if not command:
                self.send_json({"error": "command required"}, 400)
                return
            result = execute_command(command)
            self.send_json(result)

        else:
            self.send_json({"error": "Not found"}, 404)

    def do_PUT(self):
        if self.path != "/config/schedule":
            self.send_json({"error": "Not found"}, 404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        if not body.strip():
            self.send_json({"error": "YAML body required"}, 400)
            return

        try:
            parsed = yaml.safe_load(body)
            if not isinstance(parsed, dict) or "services" not in parsed:
                self.send_json({"error": "YAML must contain a services: block"}, 400)
                return
        except Exception as e:
            self.send_json({"error": f"Invalid YAML: {e}"}, 400)
            return

        try:
            SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
            SCHEDULE_FILE.write_text(body, encoding="utf-8")
            refresh_status_cache()
            self.send_json({"ok": True, "services": len(load_schedule())})
        except Exception as e:
            log.error("Failed to write schedule: %s", e)
            self.send_json({"error": str(e)}, 500)


# ---------------------------------------------------------------------------
# StdOut watchdog — Windlass watches StdOut (StdOut can't watch itself)
# ---------------------------------------------------------------------------

# Restart timestamps within the circuit-breaker window.
_watchdog_restarts: list[float] = []
_watchdog_lock = threading.Lock()


def _stdout_is_healthy() -> bool:
    """Return True if StdOut's health endpoint responds 2xx."""
    try:
        req = urllib.request.Request(f"{STDOUT_URL}/healthz", method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _restart_stdout() -> bool:
    """Restart the StdOut container. Returns True on success."""
    try:
        result = subprocess.run(
            ["docker", "restart", STDOUT_CONTAINER],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            log.error("[watchdog] docker restart %s failed: %s", STDOUT_CONTAINER, result.stderr.strip())
            return False
        log.warning("[watchdog] restarted StdOut container '%s'", STDOUT_CONTAINER)
        return True
    except Exception as e:
        log.error("[watchdog] restart error: %s", e)
        return False


def _within_restart_budget() -> bool:
    """Circuit breaker: True if we have not exceeded MAX_RESTARTS in the rolling window."""
    now = time.time()
    with _watchdog_lock:
        # Drop timestamps outside the window
        cutoff = now - WATCHDOG_RESTART_WINDOW_SEC
        _watchdog_restarts[:] = [t for t in _watchdog_restarts if t >= cutoff]
        return len(_watchdog_restarts) < WATCHDOG_MAX_RESTARTS


def _record_restart() -> None:
    with _watchdog_lock:
        _watchdog_restarts.append(time.time())


def stdout_watchdog_loop() -> None:
    """
    Poll StdOut health; after WATCHDOG_FAIL_THRESHOLD consecutive failures, restart its container.
    A circuit breaker caps restarts per window; once exhausted, Windlass stops restarting and logs
    an escalation (the crash-loop is a deeper problem a restart won't fix).
    """
    consecutive_fails = 0
    breaker_tripped = False
    log.info(
        "[watchdog] StdOut watchdog active — url=%s container=%s interval=%ds threshold=%d",
        STDOUT_URL, STDOUT_CONTAINER, WATCHDOG_INTERVAL_SEC, WATCHDOG_FAIL_THRESHOLD,
    )
    while True:
        time.sleep(WATCHDOG_INTERVAL_SEC)
        try:
            if _stdout_is_healthy():
                if consecutive_fails > 0:
                    log.info("[watchdog] StdOut recovered (was failing %dx)", consecutive_fails)
                consecutive_fails = 0
                breaker_tripped = False
                continue

            consecutive_fails += 1
            log.warning("[watchdog] StdOut health check failed (%d/%d)", consecutive_fails, WATCHDOG_FAIL_THRESHOLD)

            if consecutive_fails < WATCHDOG_FAIL_THRESHOLD:
                continue

            if not _within_restart_budget():
                if not breaker_tripped:
                    breaker_tripped = True
                    log.error(
                        "[watchdog] CIRCUIT BREAKER: StdOut restarted %d+ times in %ds and is still "
                        "unhealthy — NOT restarting again. This needs human/brain investigation.",
                        WATCHDOG_MAX_RESTARTS, WATCHDOG_RESTART_WINDOW_SEC,
                    )
                continue

            if _restart_stdout():
                _record_restart()
                consecutive_fails = 0  # give it time to come back before re-counting
        except Exception as e:
            log.error("[watchdog] loop error: %s", e)


def run_server(port: int) -> None:
    schedule = load_schedule()
    log.info("Loaded %d services from schedule.yaml", len(schedule))

    # Initial cache build
    refresh_status_cache()

    # Background scheduler thread
    def scheduler_loop():
        while True:
            time.sleep(SCHEDULE_INTERVAL_SEC)
            try:
                run_scheduler(load_schedule())
                refresh_status_cache()
            except Exception as e:
                log.error("Scheduler error: %s", e)

    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    log.info("Scheduler thread started (interval: %ds)", SCHEDULE_INTERVAL_SEC)

    # StdOut watchdog thread — Windlass watches StdOut because StdOut can't watch itself.
    if WATCHDOG_ENABLED:
        wd = threading.Thread(target=stdout_watchdog_loop, daemon=True)
        wd.start()
    else:
        log.info("[watchdog] disabled (WINDLASS_WATCHDOG=false)")

    server = HTTPServer(("0.0.0.0", port), WindlassHandler)
    log.info("Windlass serving on port %d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Windlass — Docker service scheduler")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run", action="store_true", help="Evaluate schedule once")
    group.add_argument("--serve", action="store_true", help="Start HTTP server with embedded scheduler")
    parser.add_argument("--port", type=int, default=8116, help="HTTP port (default: 8116)")
    args = parser.parse_args()

    if args.run:
        run_scheduler()
    elif args.serve:
        run_server(args.port)


if __name__ == "__main__":
    main()
