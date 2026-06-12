# Windlass

Schedule-aware Docker service manager. Reads a `schedule.yaml` and automatically starts, stops, and monitors your Docker Compose stacks according to defined windows. Pairs with [StdOut](https://github.com/seayniclabs/stdout) for a full service monitoring dashboard.

## Quick Start (Docker)

```bash
# 1. Create config directory
mkdir -p /opt/windlass

# 2. Create your schedule (see schedule.yaml.example)
cp schedule.yaml.example /opt/windlass/schedule.yaml
# Edit /opt/windlass/schedule.yaml to match your services

# 3. Run
docker compose up -d
```

Then connect StdOut → Windlass page → enter `http://your-host:8116` → Connect → Sync.

## Schedule Format

```yaml
services:
  my-service:
    compose_path: /opt/containers/my-service  # where docker-compose.yml lives
    containers: [container-name]              # container names for state detection
    type: always | schedule | on-demand | manual
    memory_mb: 256                            # expected memory (for display)
    priority: 1                               # 1 = highest, 5 = lowest
    description: "What this service does"
```

**Service types:**
| Type | Behavior |
|------|----------|
| `always` | Restarted automatically if found stopped |
| `schedule` | Started/stopped on cron windows (`cron_start`, `cron_stop`) |
| `on-demand` | Tracked; auto-stopped after `idle_shutdown_minutes` |
| `manual` | Tracked but never auto-managed |

**Scheduled service extra fields:**
```yaml
    cron_start: "0 23 * * *"    # 11 PM daily
    cron_stop:  "0 4 * * *"     # 4 AM daily
```

**On-demand extra fields:**
```yaml
    idle_shutdown_minutes: 30
```

**Optional n8n workflow awareness file (`n8n-workflows.json`):**
```json
{
  "workflows": [
    { "name": "Nightly backup", "cron": "0 3 * * *" }
  ]
}
```
When present, Windlass exposes these windows in `/status.json` as `n8n_workflow_windows` so StdOut can render them in timeline view.

**Optional live n8n REST (merged with the file above):**
| Env | Description |
|-----|-------------|
| `WINDLASS_N8N_BASE_URL` | API root including `/api/v1`, e.g. `http://n8n:5678/api/v1` |
| `WINDLASS_N8N_API_KEY` or `N8N_API_KEY` | n8n API key (`X-N8N-API-KEY` header) |

**Phase 4 — memory pressure auto-shed (on-demand only):**
| Env | Default | Description |
|-----|---------|-------------|
| `WINDLASS_MEMORY_SHED_FREE_MB` | `1024` | When host free RAM drops below this, lowest-priority running `on-demand` stacks are stopped until free memory recovers. Each shed is logged with a reason in `recent_events` and on the service row as `last_memory_shed_reason` in `/status.json`. |

`status.json` also includes `summary.scheduler_interval_sec` (from `WINDLASS_INTERVAL`) so StdOut can extrapolate idle hours per day from collected samples.

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/status.json` | Full service status, memory, upcoming events |
| `POST` | `/commands.json` | `[{service, action}]` — start/stop/restart |
| `POST` | `/exec` | `{command}` — run an allowlisted command |
| `GET` | `/health` | `{"ok": true}` |

## Deployment Notes

Three things that reliably cause problems on first deploy:

### 1. schedule.yaml — compose_path and container names

`compose_path` must point to the **directory containing `docker-compose.yml`**, not the file itself:

```yaml
compose_path: /opt/containers/postiz   # correct
compose_path: /opt/containers/postiz/docker-compose.yml  # wrong
```

The `containers` list takes **actual Docker container names** (as shown in `docker ps`), not Compose service names. Auto-generated names follow the pattern `{project}-{service}-1`. Pin them in your compose file with `container_name:` to make them predictable, then reference those names in `schedule.yaml`.

```bash
# Check exact container names on your host before editing schedule.yaml
docker ps --format '{{.Names}}'
```

### 2. Docker socket must be :rw

Windlass needs read-write access to the Docker socket. A read-only mount (`:ro`) allows state inspection but silently prevents start/stop operations — `always` services won't restart, scheduled windows will fail, and manual controls in StdOut will return errors.

```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock      # correct (rw is default)
  # - /var/run/docker.sock:/var/run/docker.sock:ro  # breaks container management
```

### 3. Cron times are UTC — TZ env var does not affect scheduling

All `cron_start` and `cron_stop` expressions are evaluated in UTC. Setting `TZ=America/Chicago` changes log timestamps only — it does not shift when cron windows fire.

If you want a service to start at 11 PM Central (CDT, UTC−5), write:

```yaml
cron_start: "0 4 * * *"   # 4 AM UTC = 11 PM CDT
cron_stop:  "0 9 * * *"   # 9 AM UTC = 4 AM CDT
```

CST (winter) is UTC−6; CDT (summer) is UTC−5. Convert accordingly.

---

## Without Docker (bare metal)

```bash
pip install pyyaml croniter psutil
curl -o /usr/local/bin/windlass https://raw.githubusercontent.com/seayniclabs/windlass/main/windlass.py
chmod +x /usr/local/bin/windlass

mkdir -p /opt/windlass
# Copy your schedule.yaml to /opt/windlass/schedule.yaml

# Start the server
windlass --serve --port 8116

# Or run on a cron (evaluate every 5 minutes)
*/5 * * * * /usr/local/bin/windlass --run >> /var/log/windlass.log 2>&1
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WINDLASS_CONFIG` | `/opt/windlass` | Directory for schedule.yaml and state.json |
| `WINDLASS_INTERVAL` | `300` | Scheduler interval in seconds (serve mode) |
| `WINDLASS_MEMORY_SHED_FREE_MB` | `1024` | Free RAM threshold for on-demand auto-shedding |
| `WINDLASS_N8N_WORKFLOWS` | `/opt/windlass/n8n-workflows.json` | Optional workflow cron file for n8n windows |
| `STDOUT_URL` | `http://localhost:8112` | StdOut base URL the watchdog health-checks |
| `STDOUT_CONTAINER` | `stdout` | StdOut container name the watchdog restarts |
| `WINDLASS_WATCHDOG` | `true` | Enable the StdOut watchdog (Windlass watches StdOut) |
| `WINDLASS_WATCHDOG_INTERVAL` | `30` | Watchdog health-poll interval (seconds) |
| `WINDLASS_WATCHDOG_FAILS` | `3` | Consecutive failures before restarting StdOut |
| `WINDLASS_WATCHDOG_MAX_RESTARTS` | `5` | Circuit breaker: max restarts per window before escalating |
| `WINDLASS_WATCHDOG_WINDOW` | `3600` | Circuit-breaker window (seconds) |

## StdOut watchdog

StdOut is the eyes+brain of the system; it cannot watch itself. Windlass — running in a separate
container — polls `STDOUT_URL/healthz` every `WINDLASS_WATCHDOG_INTERVAL` seconds and, after
`WINDLASS_WATCHDOG_FAILS` consecutive failures, restarts the StdOut container via `docker restart`.
A circuit breaker caps restarts at `WINDLASS_WATCHDOG_MAX_RESTARTS` per `WINDLASS_WATCHDOG_WINDOW`;
once exhausted it stops restarting and logs an escalation (a crash-loop needs investigation, not
more restarts). This is the reliability backbone of the autonomic stack.

## Architecture

```
StdOut Dashboard  ←──── HTTP poll ────→  Windlass Engine (port 8116)
                                              │
                                    reads schedule.yaml
                                    manages Docker socket
                                    tracks state.json
```

Windlass runs alongside your Docker host and manages containers directly via the Docker socket. StdOut is a read-only observer that polls `/status.json` and sends commands via `/commands.json`.

## License

MIT — [seayniclabs/windlass](https://github.com/seayniclabs/windlass)
