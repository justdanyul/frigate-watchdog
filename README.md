# frigate-watchdog

Watches a [Frigate](https://frigate.video) NVR for cameras that have silently
stopped recording, and recovers them.

Frigate sometimes keeps running while one camera's recordings quietly stop —
typically when go2rtc loses its connection to the camera and doesn't
reconnect. Frigate's own watchdog restarts its ffmpeg process, but that just
reconnects to the same dead go2rtc stream. This service notices and fixes it.

## How it works

Every `CHECK_INTERVAL` seconds, for every camera with `record.enabled: true`:

1. Ask Frigate for the newest recording segment it has saved
   (`GET /api/<camera>/recordings`).
2. If that segment ended more than `STALE_SECONDS` ago, the camera counts as
   **not recording**. The log line includes `camera_fps` and the go2rtc
   stream state to help diagnose why.
3. Recover, escalating only if the previous step didn't help:

   | Step | Action | Scope |
   |------|--------|-------|
   | 1 | MQTT `frigate/<camera>/enabled/set` `OFF` → `ON` | that camera |
   | 2 | go2rtc `POST /api/restart` (or `s6-svc -r` via `docker exec`) | all streams, a few seconds |
   | 3 | Frigate `POST /api/restart` | everything |

After any action the camera is left alone for `GRACE_SECONDS`. go2rtc and
Frigate restarts are limited to one per `GLOBAL_COOLDOWN`. Nothing is
checked until Frigate has been up for `STARTUP_GRACE`.

## Requirements

- Frigate 0.16+ (needed for the per-camera MQTT bounce; the rest works on older versions).
- Frigate's **unauthenticated API port 5000** reachable from the host. Since
  0.14 it isn't published by default; add it to your compose file, bound to
  loopback only:

  ```yaml
  services:
    frigate:
      ports:
        - "8971:8971"
        - "127.0.0.1:5000:5000"
        - "127.0.0.1:1984:1984"   # go2rtc API, for step 2
  ```

- Python 3. `python-paho-mqtt` is optional; without it step 1 is skipped.

## Install on Arch Linux

```bash
git clone <this repo> frigate-watchdog && cd frigate-watchdog
sudo pacman -S --needed base-devel python-paho-mqtt
makepkg -si
```

This installs:

| Path | What |
|------|------|
| `/usr/bin/frigate-watchdog` | the script |
| `/usr/lib/systemd/system/frigate-watchdog.service` | systemd unit |
| `/etc/frigate-watchdog.env` | settings (mode 600; kept across upgrades) |

Then:

```bash
sudoedit /etc/frigate-watchdog.env

# One-off check with the real settings, changing nothing:
sudo systemd-run --pty -p EnvironmentFile=/etc/frigate-watchdog.env \
    /usr/bin/frigate-watchdog --once --dry-run -v

sudo systemctl enable --now frigate-watchdog
```

**Upgrade:** pull, then `makepkg -sif` (`-f` rebuilds over the previous
package; bump `pkgrel` if you want pacman to show a version change). The
service restarts automatically and your `/etc` settings are kept (pacman
writes any changed template as `.pacnew`).

**Remove:** `sudo pacman -R frigate-watchdog`

## Other distros

```bash
sudo install -Dm755 frigate_watchdog.py      /usr/bin/frigate-watchdog
sudo install -Dm644 frigate-watchdog.service /etc/systemd/system/frigate-watchdog.service
sudo install -Dm600 frigate-watchdog.env     /etc/frigate-watchdog.env
sudo systemctl daemon-reload && sudo systemctl enable --now frigate-watchdog
```

## Configuration

All settings are environment variables, read from `/etc/frigate-watchdog.env`.

| Variable | Default | Meaning |
|----------|---------|---------|
| `FRIGATE_URL` | `http://127.0.0.1:5000` | Frigate API (unauthenticated port) |
| `GO2RTC_URL` | *(empty)* | go2rtc API, e.g. `http://127.0.0.1:1984`. Empty = use `docker exec` |
| `FRIGATE_CONTAINER` | `frigate` | container name for the `docker exec` fallback |
| `MQTT_HOST` | *(empty)* | broker for the camera bounce. Empty = skip step 1 |
| `MQTT_PORT` / `MQTT_USER` / `MQTT_PASS` | `1883` / — / — | broker details |
| `MQTT_PREFIX` | `frigate` | Frigate's `mqtt.topic_prefix` |
| `STALE_SECONDS` | `300` | newest segment older than this = not recording |
| `CHECK_INTERVAL` | `60` | seconds between checks |
| `GRACE_SECONDS` | `240` | quiet period for a camera after an action |
| `STARTUP_GRACE` | `300` | don't check until Frigate has been up this long |
| `GLOBAL_COOLDOWN` | `900` | minimum gap between go2rtc/Frigate restarts |
| `ALLOW_FRIGATE_RESTART` | `1` | `0` = never restart Frigate itself |

Command-line flags: `--once`, `--dry-run`, `--cameras a,b`, `-v`.

### Running in report-only mode first

To see what it *would* do for a few days without touching anything:

```bash
sudo systemctl edit frigate-watchdog
```

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/frigate-watchdog --dry-run
```

## Logs

```bash
journalctl -u frigate-watchdog -f             # live
journalctl -u frigate-watchdog | grep ACTION  # every recovery it has done
```

A stalled camera looks like:

```
WARNING [drive] NOT RECORDING: last segment 1800s ago, camera_fps=0.0, go2rtc: producers=1 consumers=1 bytes_recv=[123]
WARNING [drive] ACTION bounce camera via frigate/drive/enabled/set OFF->ON
```

If step 2 (go2rtc restart) is what usually fixes a camera, consider pointing
that camera's `record` input straight at the camera's RTSP URL instead of the
go2rtc restream.

## Caveats

- **Motion-only recording.** If a camera only keeps segments with motion, a
  quiet scene looks like a stall. Raise `STALE_SECONDS`, or exclude that camera
  with `--cameras`.
- **Security.** Port 5000 and go2rtc's port 1984 have no authentication. Keep
  them bound to `127.0.0.1`.
- The `docker exec` fallback needs Docker access, which the default unit
  (`DynamicUser=yes`) doesn't have. Set `GO2RTC_URL` instead, or override the
  user as described in the unit file.
