#!/usr/bin/env python3
"""
frigate_watchdog.py - detect Frigate cameras that have silently stopped
recording and recover them with an escalating ladder:

    1. bounce the single camera  (MQTT frigate/<cam>/enabled/set OFF -> ON)
    2. restart go2rtc            (HTTP :1984/api/restart, or s6 inside the container)
    3. restart Frigate           (POST /api/restart)

"Recording" is judged by the newest segment Frigate has actually written to
its database for that camera (GET /api/<cam>/recordings), which is the thing
you care about - not whether ffmpeg is merely running.

Stdlib only. MQTT bounce needs paho-mqtt (optional; `pacman -S python-paho-mqtt`).

Usage:
    frigate-watchdog --once --dry-run      # just report
    frigate-watchdog                       # loop forever
    frigate-watchdog --cameras front,drive # limit to some cameras

All settings can also come from env vars (see CONFIG below).
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# CONFIG (env vars override defaults)
# --------------------------------------------------------------------------
FRIGATE_URL = os.getenv("FRIGATE_URL", "http://127.0.0.1:5000")  # unauthenticated internal port
FRIGATE_TOKEN = os.getenv("FRIGATE_TOKEN", "")  # optional bearer token if you go via :8971

# How stale the newest segment may be before we call it "not recording".
# Segments land in the DB after leaving /tmp/cache, so allow a few minutes.
STALE_SECONDS = int(os.getenv("STALE_SECONDS", "300"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
# After any recovery action, leave a camera alone this long before judging again.
GRACE_SECONDS = int(os.getenv("GRACE_SECONDS", "240"))
# Don't judge anything until Frigate has been up this long.
STARTUP_GRACE = int(os.getenv("STARTUP_GRACE", "300"))
# Minimum gap between global (go2rtc / Frigate) restarts.
GLOBAL_COOLDOWN = int(os.getenv("GLOBAL_COOLDOWN", "900"))

MQTT_HOST = os.getenv("MQTT_HOST", "")  # empty = skip the per-camera bounce
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
MQTT_PREFIX = os.getenv("MQTT_PREFIX", "frigate")

# go2rtc restart: use the API if :1984 is reachable, else docker exec.
GO2RTC_URL = os.getenv("GO2RTC_URL", "")  # e.g. http://127.0.0.1:1984
FRIGATE_CONTAINER = os.getenv("FRIGATE_CONTAINER", "frigate")

ALLOW_FRIGATE_RESTART = os.getenv("ALLOW_FRIGATE_RESTART", "1") == "1"

log = logging.getLogger("frigate-watchdog")


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------
def _req(method, url, timeout=15):
    req = urllib.request.Request(url, method=method)
    if FRIGATE_TOKEN and url.startswith(FRIGATE_URL):
        req.add_header("Authorization", f"Bearer {FRIGATE_TOKEN}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body) if body else None


def frigate_get(path):
    return _req("GET", f"{FRIGATE_URL}{path}")


def frigate_post(path):
    return _req("POST", f"{FRIGATE_URL}{path}")


# --------------------------------------------------------------------------
# Health checks
# --------------------------------------------------------------------------
def recording_cameras(config, only=None):
    """Cameras that are enabled and have recording enabled in config."""
    cams = []
    for name, c in (config.get("cameras") or {}).items():
        if only and name not in only:
            continue
        if c.get("enabled", True) is False:
            continue
        if not (c.get("record") or {}).get("enabled", False):
            continue
        cams.append(name)
    return cams


def camera_stats(stats, cam):
    # 0.12+ nests under "cameras"; very old versions put cameras at top level.
    return (stats.get("cameras") or stats).get(cam, {}) or {}


def last_segment_end(cam, now, lookback=3600):
    """Unix time of the newest recording segment's end, or None."""
    segs = frigate_get(f"/api/{cam}/recordings?after={int(now - lookback)}&before={int(now)}")
    if not segs:
        return None
    return max(float(s.get("end_time") or s.get("start_time") or 0) for s in segs)


def go2rtc_stream_info(cam):
    """Producer summary from go2rtc via Frigate's proxy - for the log only."""
    try:
        info = frigate_get(f"/api/go2rtc/streams/{cam}")
    except Exception:
        return "n/a"
    if not isinstance(info, dict):
        return "n/a"
    prods = info.get("producers") or []
    cons = info.get("consumers") or []
    rx = [p.get("bytes_recv", 0) for p in prods]
    return f"producers={len(prods)} consumers={len(cons)} bytes_recv={rx}"


# --------------------------------------------------------------------------
# Recovery actions
# --------------------------------------------------------------------------
def bounce_camera(cam, dry):
    if not MQTT_HOST:
        log.info("[%s] no MQTT_HOST set, skipping camera bounce", cam)
        return False
    try:
        from paho.mqtt import publish
    except ImportError:
        log.warning("paho-mqtt not installed, skipping camera bounce")
        return False
    topic = f"{MQTT_PREFIX}/{cam}/enabled/set"
    auth = {"username": MQTT_USER, "password": MQTT_PASS} if MQTT_USER else None
    log.warning("[%s] ACTION bounce camera via %s OFF->ON", cam, topic)
    if dry:
        return True
    publish.single(topic, "OFF", hostname=MQTT_HOST, port=MQTT_PORT, auth=auth)
    time.sleep(10)
    publish.single(topic, "ON", hostname=MQTT_HOST, port=MQTT_PORT, auth=auth)
    return True


def restart_go2rtc(dry):
    log.warning("ACTION restart go2rtc")
    if dry:
        return True
    if GO2RTC_URL:
        try:
            _req("POST", f"{GO2RTC_URL}/api/restart")
            return True
        except Exception as e:
            log.error("go2rtc API restart failed (%s), trying docker exec", e)
    # Inside the Frigate image go2rtc is an s6 service; -r restarts it.
    cmd = ["docker", "exec", FRIGATE_CONTAINER, "s6-svc", "-r", "/run/service/go2rtc"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log.error("docker exec failed: %s", r.stderr.strip())
        return False
    return True


def restart_frigate(dry):
    if not ALLOW_FRIGATE_RESTART:
        log.error("Frigate restart needed but ALLOW_FRIGATE_RESTART=0")
        return False
    log.warning("ACTION restart Frigate")
    if dry:
        return True
    try:
        frigate_post("/api/restart")
    except Exception:
        pass  # connection drops as it exits; that's expected
    return True


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
class State:
    def __init__(self):
        self.level = {}         # cam -> next escalation level (0,1,2)
        self.quiet_until = {}   # cam -> don't judge before this time
        self.last_global = 0.0


def check_once(st, only, dry):
    now = time.time()
    try:
        stats = frigate_get("/api/stats")
        config = frigate_get("/api/config")
    except Exception as e:
        log.error("Frigate API unreachable: %s", e)
        return

    uptime = (stats.get("service") or {}).get("uptime", STARTUP_GRACE + 1)
    if uptime < STARTUP_GRACE:
        log.info("Frigate up %ss, inside startup grace", uptime)
        return

    stale = []
    for cam in recording_cameras(config, only):
        if now < st.quiet_until.get(cam, 0):
            continue
        cs = camera_stats(stats, cam)
        try:
            last = last_segment_end(cam, now)
        except Exception as e:
            log.error("[%s] recordings query failed: %s", cam, e)
            continue
        age = None if last is None else now - last
        fps = cs.get("camera_fps")
        if age is not None and age <= STALE_SECONDS:
            if st.level.get(cam):
                log.info("[%s] recovered (last segment %.0fs ago)", cam, age)
            st.level[cam] = 0
            log.debug("[%s] ok: last segment %.0fs ago, camera_fps=%s", cam, age, fps)
            continue
        log.warning(
            "[%s] NOT RECORDING: last segment %s, camera_fps=%s, go2rtc: %s",
            cam,
            "none in last hour" if age is None else f"{age:.0f}s ago",
            fps,
            go2rtc_stream_info(cam),
        )
        stale.append(cam)

    if not stale:
        return

    # Level 0: bounce each stale camera individually.
    needs_global = []
    for cam in stale:
        lvl = st.level.get(cam, 0)
        if lvl == 0 and bounce_camera(cam, dry):
            st.level[cam] = 1
            st.quiet_until[cam] = now + GRACE_SECONDS
        else:
            needs_global.append(cam)

    if not needs_global:
        return
    if now - st.last_global < GLOBAL_COOLDOWN:
        log.info("global restart wanted for %s but in cooldown", needs_global)
        return

    # Level 1 -> go2rtc, level 2+ -> Frigate. Use the highest level among them.
    top = max(st.level.get(c, 0) for c in needs_global)
    if top <= 1:
        ok = restart_go2rtc(dry)
        nxt = 2
    else:
        ok = restart_frigate(dry)
        nxt = 2  # stay at Frigate-restart level until it recovers
    if ok:
        st.last_global = now
        for cam in needs_global:
            st.level[cam] = nxt
            st.quiet_until[cam] = now + GRACE_SECONDS
        # Give every camera grace after a global restart, not just stale ones.
        for cam in recording_cameras(config, only):
            st.quiet_until[cam] = max(st.quiet_until.get(cam, 0), now + GRACE_SECONDS)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run one check and exit")
    ap.add_argument("--dry-run", action="store_true", help="log actions without doing them")
    ap.add_argument("--cameras", help="comma-separated camera names to watch (default: all recording cameras)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    only = set(a.cameras.split(",")) if a.cameras else None
    st = State()

    if a.once:
        # One-shot: a single pass with no escalation memory. Startup grace still
        # applies - set STARTUP_GRACE=0 to check straight after a Frigate restart.
        check_once(st, only, a.dry_run)
        return
    log.info("watching %s every %ss (stale after %ss)%s",
             FRIGATE_URL, CHECK_INTERVAL, STALE_SECONDS, " [DRY RUN]" if a.dry_run else "")
    while True:
        try:
            check_once(st, only, a.dry_run)
        except Exception:
            log.exception("check failed")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
