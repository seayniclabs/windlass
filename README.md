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

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/status.json` | Full service status, memory, upcoming events |
| `POST` | `/commands.json` | `[{service, action}]` — start/stop/restart |
| `POST` | `/exec` | `{command}` — run an allowlisted command |
| `GET` | `/health` | `{"ok": true}` |

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
