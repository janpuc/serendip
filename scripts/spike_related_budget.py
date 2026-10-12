"""Spike Q5: measure related-video lookups through Yattee Server at a fixed, small rate.

Each lookup is a cold GET /api/v1/videos/{id} (InnerTube /player + /next + yt-dlp on the
server). IDs come from a random walk over recommendedVideos, starting from the subscriptions
feed (POST /api/v1/feed reads the server's local database, no upstream) or /trending, and are
never repeated, so the server's one-hour cache cannot hide the upstream cost.

Phase A: 12 lookups, one every ~5 min. Phase B (only if A is clean): 30 lookups, one every
~60 s. Stops at the first non-200, any bot/sign-in text, or two empty related lists in a row.

Credentials come from YATTEE_USER / YATTEE_PASS; nothing secret is written to disk.
"""

import base64
import csv
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("YATTEE_URL", "https://yattee.janpuc.com")
OUT = sys.argv[1] if len(sys.argv) > 1 else "q5.csv"
PHASES = [("A", 12, 300), ("B", 30, 60)]
BOT_MARKERS = ("sign in", "not a bot", "confirm you", "login_required", "captcha")
UA = "serendip-spike/0.1 (Q5 rate probe)"

auth = base64.b64encode(f"{os.environ['YATTEE_USER']}:{os.environ['YATTEE_PASS']}".encode()).decode()


def get(path: str, payload: dict | None = None) -> tuple[int, float, bytes]:
    headers = {"Authorization": f"Basic {auth}", "User-Agent": UA}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
            return resp.status, time.monotonic() - t0, body
    except urllib.error.HTTPError as e:
        return e.code, time.monotonic() - t0, e.read()
    except Exception as e:  # network error: treat as a failure, record the text
        return 0, time.monotonic() - t0, str(e).encode()


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def seeds() -> list[str]:
    """Seed ids: the subscriptions feed if SEED_CHANNELS is set, else /trending."""
    channels = [c for c in os.environ.get("SEED_CHANNELS", "").split(",") if c]
    if channels:
        payload = {"channels": [{"channel_id": c, "site": "youtube"} for c in channels],
                   "limit": 200}
        status, dt, body = get("/api/v1/feed", payload)
        items = json.loads(body).get("videos", []) if status == 200 else []
        source = "feed"
    else:
        status, dt, body = get("/api/v1/trending")
        items = json.loads(body) if status == 200 else []
        source = "trending"
    log(f"{source} {status} in {dt:.2f}s, {len(items)} items")
    # Live streams come back through the server's yt-dlp-only path with no related list.
    return [
        v["videoId"] for v in items
        if v.get("videoId") and not v.get("liveNow") and (v.get("lengthSeconds") or 0) > 0
    ]


def main() -> int:
    frontier = seeds()
    random.shuffle(frontier)
    seen: set[str] = set()
    log(f"{len(frontier)} seed ids")

    with open(OUT, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["utc", "phase", "n", "video_id", "status", "seconds", "recommended",
             "adaptive_formats", "extraction", "detail"]
        )
        empty_streak = 0
        for phase, count, interval in PHASES:
            log(f"phase {phase}: {count} lookups, ~{interval}s apart")
            for n in range(1, count + 1):
                while frontier and frontier[0] in seen:
                    frontier.pop(0)
                if not frontier:
                    log("frontier exhausted")
                    return 1
                vid = frontier.pop(0)
                seen.add(vid)
                status, dt, body = get(f"/api/v1/videos/{vid}")
                rec, fmts, method, detail, live = 0, 0, "", "", False
                if status == 200:
                    d = json.loads(body)
                    live = bool(d.get("liveNow"))
                    recs = [
                        r["videoId"] for r in d.get("recommendedVideos") or []
                        if r.get("videoId") and (r.get("lengthSeconds") or 0) > 0
                    ]
                    rec, fmts = len(recs), len(d.get("adaptiveFormats") or [])
                    method = d.get("extractionMethod") or ""
                    new = [r for r in recs if r not in seen]
                    random.shuffle(new)
                    frontier = new[:5] + frontier  # walk: prefer fresh related ids
                else:
                    detail = body[:300].decode(errors="replace").replace("\n", " ")
                w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), phase, n,
                            vid, status, f"{dt:.2f}", rec, fmts, method,
                            detail or ("live" if live else "")])
                fh.flush()
                log(f"{phase}{n:02d} {vid} {status} {dt:.1f}s rec={rec} fmts={fmts} {method}"
                    f"{' live' if live else ''} {detail[:120]}")

                if status != 200:
                    hit = any(m in detail.lower() for m in BOT_MARKERS)
                    log("STOP: " + ("bot-check text in response" if hit else f"HTTP {status}"))
                    return 2
                if not live:
                    empty_streak = empty_streak + 1 if rec == 0 else 0
                if empty_streak >= 2:
                    log("STOP: two empty related lists in a row")
                    return 2
                if n < count or phase == "A":
                    time.sleep(interval * random.uniform(0.8, 1.2))
    log("done, all phases clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
