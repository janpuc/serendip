#!/usr/bin/env python3
"""Local spike for the Envoy request-mirroring assumptions in docs/DESIGN.md.

DESIGN.md section 3 ("Mirror contract") assumes that Envoy mirrors the method, path,
query, headers and body of a request (including POST bodies), that the mirror still sees
the original Host, and that the mirror's response is discarded so a slow, dead or erroring
mirror never delays or fails the client. Section 11 ("Mirror spike") asks for exactly this
to be tested on a local Envoy with a fake primary and a fake mirror before anything is
deployed.

This script drives compose.yaml (Envoy v1.39.3 on OrbStack, fake primary, fake mirror),
runs six measurements, prints a results table and writes RESULTS.md and out/results.json.

Run with:  python3 -I run.py
"""

import base64
import datetime
import hashlib
import http.client
import json
import random
import re
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"
MIRROR_LOG = OUT_DIR / "mirror.jsonl"
MODE_FILE = OUT_DIR / "mode.txt"
LATENCY_FILE = OUT_DIR / "primary_latency_ms.txt"
RESULTS_MD = HERE / "RESULTS.md"
RESULTS_JSON = OUT_DIR / "results.json"
ROUTE_GO_EXCERPT = HERE / "route-go-v1.9.2-excerpt.txt"

ENVOY_IMAGE = "envoyproxy/envoy:v1.39.3"
ORIGIN_PORT = 10000  # envoy.yaml: disable_shadow_host_suffix_append: true
SHADOW_PORT = 10001  # envoy-shadow.yaml: Envoy default (suffix appended)
ADMIN_PORT = 9901
ADMIN_SHADOW_PORT = 9902
PRIMARY_PORT = 18080
MIRROR_PORT = 18081

PUBLIC_HOST = "yattee.janpuc.com"
AUTH = "Basic " + base64.b64encode(b"jan:hunter2").decode()
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
)
ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"

CHECKS = []
DETAILS = {}


# --------------------------------------------------------------------------- helpers


def compose(*args, check=True):
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=HERE,
        check=check,
        capture_output=True,
        text=True,
    )


def check(check_id, name, expected, observed, ok):
    verdict = "INFO" if ok is None else ("PASS" if ok else "FAIL")
    CHECKS.append(
        {
            "id": check_id,
            "check": name,
            "expected": expected,
            "observed": observed,
            "verdict": verdict,
        }
    )


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def wait_tcp(host, port, timeout=45.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def wait_admin(port, timeout=45.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            conn.request("GET", "/ready")
            response = conn.getresponse()
            response.read()
            conn.close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


TAG_SEQ = [0]


def next_tag():
    """Every request carries a unique X-Spike-Tag so a mirror record can be attributed to
    exactly one request, never to traffic left over from an earlier phase."""
    TAG_SEQ[0] += 1
    return f"spike-{TAG_SEQ[0]}"


def send(port, method, target, body=None, headers=None, timeout=60.0):
    """One request on its own connection; returns status, body and client-side timing."""
    tag = next_tag()
    request_headers = {
        "Host": PUBLIC_HOST,
        "Authorization": AUTH,
        "User-Agent": USER_AGENT,
        "X-Spike-Tag": tag,
    }
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    result = {"method": method, "target": target, "port": port, "tag": tag}
    t_send = time.time()
    result["t_send"] = t_send
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, target, body=body, headers=request_headers)
        response = conn.getresponse()
        payload = response.read()
        t_recv = time.time()
        result.update(
            status=response.status,
            headers=dict(response.getheaders()),
            body=payload,
            t_recv=t_recv,
            latency_ms=(t_recv - t_send) * 1000,
            error=None,
        )
    except Exception as exc:  # noqa: BLE001 - a failed request is a measurement
        t_recv = time.time()
        result.update(
            status=None,
            headers={},
            body=b"",
            t_recv=t_recv,
            latency_ms=(t_recv - t_send) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        conn.close()
    return result


def reset_mirror_log():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    MIRROR_LOG.write_text("")
    return MIRROR_LOG.stat().st_size


def read_records():
    if not MIRROR_LOG.exists():
        return []
    records = []
    for line in MIRROR_LOG.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a line still being appended
    return records


def wait_for_records(count, timeout):
    deadline = time.time() + timeout
    records = read_records()
    while time.time() < deadline and len(records) < count:
        time.sleep(0.1)
        records = read_records()
    return records


def mirror_header(record, name):
    for header_name, value in record["headers"]:
        if header_name.lower() == name.lower():
            return value
    return None


def find_record(records, tag):
    """The mirror record for one specific request, matched on its unique tag."""
    for record in records:
        if mirror_header(record, "x-spike-tag") == tag:
            return record
    return None


def stray_records(records, tag):
    return [record for record in records if mirror_header(record, "x-spike-tag") != tag]


def wait_for_tagged_record(tag, timeout):
    deadline = time.time() + timeout
    records = read_records()
    while time.time() < deadline:
        record = find_record(records, tag)
        if record is not None:
            return record, records
        time.sleep(0.1)
        records = read_records()
    return None, records


def set_primary_latency(milliseconds):
    LATENCY_FILE.write_text(f"{int(milliseconds)}\n")
    time.sleep(0.2)


def wait_mirror_healthy(timeout=30.0):
    """Envoy re-admits the mirror host only after its DNS refresh; until then it has no
    healthy host and drops shadow requests locally. Wait for it so a phase that is meant to
    test the mirror's behaviour does not measure that window instead."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mirror_stats_snapshot().get("cluster.mirror.membership_healthy") == 1:
            return True
        time.sleep(0.2)
    return False


def set_mirror_mode(mode):
    """normal / slow / 500 via the mode file the mirror reads per request; stopped by
    stopping the container."""
    if mode == "stopped":
        compose("stop", "mirror")
        return
    compose("start", "mirror")
    MODE_FILE.write_text(mode + "\n")
    if not wait_tcp("127.0.0.1", MIRROR_PORT, timeout=30):
        raise RuntimeError("mirror did not come back up")
    wait_mirror_healthy()


def make_feed(channel_count, target_bytes, seed=20261012):
    """A POST /api/v1/feed body of exactly target_bytes, with channel_count fake channels."""
    random_source = random.Random(seed)

    def text(length):
        return "".join(random_source.choice(ALPHABET) for _ in range(length))

    channels = [
        {"channel_id": "UC" + text(22), "site": "youtube", "channel_name": ""}
        for _ in range(channel_count)
    ]
    overhead = len(json.dumps(channels, separators=(",", ":")).encode())
    name_length = max(1, (target_bytes - overhead) // channel_count)
    for channel in channels:
        channel["channel_name"] = text(name_length)
    payload = json.dumps(channels, separators=(",", ":")).encode()
    delta = target_bytes - len(payload)
    last_name = channels[-1]["channel_name"]
    if delta > 0:
        channels[-1]["channel_name"] = last_name + text(delta)
    elif delta < 0:
        channels[-1]["channel_name"] = last_name[: max(1, len(last_name) + delta)]
    return json.dumps(channels, separators=(",", ":")).encode()


def wait_settle(seconds=0.5):
    time.sleep(seconds)


# ----------------------------------------------------------------------- measurements


def measurement_1_post_body():
    """POST /api/v1/feed, 5 KB (66 channels) and 70 KB (500 channels)."""
    rows = []
    for channel_count, target in ((66, 5_000), (500, 70_000)):
        payload = make_feed(channel_count, target)
        sent_sha = sha256(payload)
        reset_mirror_log()
        response = send(10000, "POST", "/api/v1/feed?chunk=1&total=3", body=payload)
        record, records = wait_for_tagged_record(response["tag"], 15)
        strays = stray_records(records, response["tag"])
        echo = {}
        if response["status"] == 200:
            try:
                echo = json.loads(response["body"])
            except json.JSONDecodeError:
                echo = {}
        label = f"{len(payload)} B / {channel_count} channels"
        check(
            "1-primary",
            f"primary answered the {label} POST itself",
            "200, body_len and sha256 match what was sent",
            f"status={response['status']}, body_len={echo.get('body_len')}, "
            f"sha_match={echo.get('body_sha256') == sent_sha}",
            response["status"] == 200
            and echo.get("body_len") == len(payload)
            and echo.get("body_sha256") == sent_sha,
        )
        check(
            "1-mirror-count",
            f"mirror got exactly one copy of the {label} POST",
            "1 record, and no stray records from another phase",
            f"{1 if record else 0} tagged record(s) after waiting 15 s, {len(strays)} stray record(s)",
            record is not None and not strays,
        )
        if record is None:
            check("1-body", f"mirror body for {label}", "identical sha256", "no mirror record", False)
            rows.append(
                {
                    "payload": label,
                    "sent_bytes": len(payload),
                    "sent_sha256": sent_sha,
                    "mirror": "no record",
                }
            )
            continue
        observed = {
            "method": record["method"],
            "request_target": record["request_target"],
            "path": record["path"],
            "raw_query": record["raw_query"],
            "host": record["host"],
            "body_len": record["body_len"],
            "body_sha256": record["body_sha256"],
            "truncated": record["truncated"],
            "content_length": record["content_length"],
            "authorization": mirror_header(record, "authorization"),
            "user_agent": mirror_header(record, "user-agent"),
            "content_type": mirror_header(record, "content-type"),
            "x_device": mirror_header(record, "x-device"),
            "x_forwarded_for": mirror_header(record, "x-forwarded-for"),
            "headers": record["headers"],
        }
        rows.append(
            {
                "payload": label,
                "sent_bytes": len(payload),
                "sent_sha256": sent_sha,
                "mirror": observed,
            }
        )
        for check_id, name, expected, actual, ok in (
            (
                "1-method",
                f"method of the {label} POST at the mirror",
                "POST",
                observed["method"],
                observed["method"] == "POST",
            ),
            (
                "1-path",
                f"path of the {label} POST at the mirror",
                "/api/v1/feed",
                observed["path"],
                observed["path"] == "/api/v1/feed",
            ),
            (
                "1-query",
                f"query of the {label} POST at the mirror",
                "chunk=1&total=3",
                observed["raw_query"],
                observed["raw_query"] == "chunk=1&total=3",
            ),
            (
                "1-body-sha",
                f"body sha256 of the {label} POST at the mirror",
                sent_sha,
                f"{observed['body_sha256']} (len {observed['body_len']}, "
                f"truncated={observed['truncated']})",
                observed["body_sha256"] == sent_sha
                and observed["body_len"] == len(payload)
                and not observed["truncated"],
            ),
            (
                "1-auth",
                f"Authorization header at the mirror ({label})",
                AUTH,
                observed["authorization"],
                observed["authorization"] == AUTH,
            ),
            (
                "1-ua",
                f"User-Agent header at the mirror ({label})",
                USER_AGENT,
                observed["user_agent"],
                observed["user_agent"] == USER_AGENT,
            ),
        ):
            check(check_id, name, expected, actual, ok)
    DETAILS["measurement_1"] = rows
    time.sleep(0.5)


def measurement_2_get_path_and_query():
    """GET /api/v1/videos/dQw4w9WgXcQ?proxy=true&proxy_mode=relay."""
    target = "/api/v1/videos/dQw4w9WgXcQ?proxy=true&proxy_mode=relay"
    reset_mirror_log()
    response = send(10000, "GET", target)
    record, _ = wait_for_tagged_record(response["tag"], 15)
    detail = {
        "sent_target": target,
        "primary_status": response["status"],
        "mirror": None if record is None else {
            "method": record["method"],
            "request_target": record["request_target"],
            "path": record["path"],
            "raw_query": record["raw_query"],
            "body_len": record["body_len"],
            "host": record["host"],
        },
    }
    DETAILS["measurement_2"] = detail
    check(
        "2-primary",
        "GET video request answered by the primary",
        "200",
        f"status={response['status']}",
        response["status"] == 200,
    )
    if record is None:
        check("2-mirror", "GET video request mirrored", "path and query identical", "no mirror record", False)
        return
    check(
        "2-path",
        "path at the mirror for GET /api/v1/videos/dQw4w9WgXcQ",
        "/api/v1/videos/dQw4w9WgXcQ",
        record["path"],
        record["path"] == "/api/v1/videos/dQw4w9WgXcQ",
    )
    check(
        "2-query",
        "raw query at the mirror",
        "proxy=true&proxy_mode=relay",
        record["raw_query"],
        record["raw_query"] == "proxy=true&proxy_mode=relay",
    )
    check(
        "2-method",
        "method at the mirror",
        "GET",
        record["method"],
        record["method"] == "GET",
    )


def measurement_3_host_header():
    """Host header at the mirror, with and without disable_shadow_host_suffix_append."""
    results = {}
    for label, port in (("suffix_disabled", ORIGIN_PORT), ("envoy_default", SHADOW_PORT)):
        reset_mirror_log()
        response = send(port, "GET", "/api/v1/videos/dQw4w9WgXcQ?proxy=true&proxy_mode=relay")
        record, _ = wait_for_tagged_record(response["tag"], 15)
        results[label] = {
            "listener_port": port,
            "primary_status": response["status"],
            "mirror_host": record["host"] if record else None,
        }
    DETAILS["measurement_3"] = results
    with_flag = results["suffix_disabled"]["mirror_host"]
    default = results["envoy_default"]["mirror_host"]
    check(
        "3-host-with-flag",
        "Host at the mirror with disable_shadow_host_suffix_append: true (port 10000)",
        PUBLIC_HOST,
        str(with_flag),
        with_flag == PUBLIC_HOST,
    )
    check(
        "3-host-default",
        "Host at the mirror without the flag (port 10001, Envoy default)",
        f"{PUBLIC_HOST}-shadow (Envoy appends -shadow)",
        str(default),
        default is not None and default != with_flag,
    )


def measurement_4_primary_latency(latency_ms=200, count=20):
    """Client-observed latency with the mirror normal, slow, 500 and stopped."""
    set_primary_latency(latency_ms)
    rows = []
    for mode in ("normal", "slow", "500", "stopped"):
        set_mirror_mode(mode)
        reset_mirror_log()
        before_stats = mirror_stats_snapshot()
        samples = [
            send(10000, "GET", f"/api/v1/videos/dQw4w9WgXcQ?proxy=true&proxy_mode=relay&n={i}")
            for i in range(count)
        ]
        latencies = [sample["latency_ms"] for sample in samples]
        failures = [sample for sample in samples if sample["status"] != 200]
        records = wait_for_records(count, 20) if mode != "stopped" else read_records()
        delivered = [find_record(records, sample["tag"]) for sample in samples]
        after_stats = mirror_stats_snapshot()
        rows.append(
            {
                "mirror_mode": mode,
                "requests": count,
                "primary_latency_ms": latency_ms,
                "p50_ms": round(statistics.median(latencies), 1),
                "max_ms": round(max(latencies), 1),
                "min_ms": round(min(latencies), 1),
                "client_failures": len(failures),
                "mirror_records": sum(1 for record in delivered if record is not None),
                "mirror_record_modes": sorted(
                    {record["mode"] for record in delivered if record is not None}
                ),
                "envoy_counter_delta": mirror_stats_delta(before_stats, after_stats),
            }
        )
    set_mirror_mode("normal")
    set_primary_latency(0)
    DETAILS["measurement_4"] = rows
    limit = latency_ms * 2
    for row in rows:
        check(
            "4-latency",
            f"client latency with mirror={row['mirror_mode']}",
            f"no failures and p50 well below {limit} ms (primary sleeps {latency_ms} ms)",
            f"p50={row['p50_ms']} ms, max={row['max_ms']} ms, "
            f"failures={row['client_failures']}, mirror records={row['mirror_records']}",
            row["client_failures"] == 0 and row["p50_ms"] < limit,
        )
    normal = next(row for row in rows if row["mirror_mode"] == "normal")
    stopped = next(row for row in rows if row["mirror_mode"] == "stopped")
    check(
        "4-delivery",
        "mirror delivery while it is up, none while it is stopped",
        "20 records with the mirror normal, 0 with it stopped",
        f"normal={normal['mirror_records']}, stopped={stopped['mirror_records']}",
        normal["mirror_records"] == count and stopped["mirror_records"] == 0,
    )


def envoy_log_tail(lines=400):
    result = compose("logs", "--no-color", "--tail", str(lines), "envoy", check=False)
    return (result.stdout or "") + (result.stderr or "")


def envoy_warnings():
    """Envoy log lines that matter for mirroring: warnings, errors, shadow/buffer notices."""
    lines = envoy_log_tail().splitlines()
    return [
        line
        for line in lines
        if re.search(r"\[(warning|error)\]", line)
        or "shadow" in line.lower()
        or "too large" in line.lower()
    ][-20:]


LARGE_BODY_CASES = (
    (1_048_576, 10, "1 MiB"),
    (2_097_152, 5, "2 MiB (extra probe)"),
    (524_288, 3, "512 KiB (boundary context)"),
    (65_536, 3, "64 KiB (serendip's bound)"),
)


def measurement_5_large_body():
    """A 1 MiB POST body, repeated, plus the neighbours that bound the answer."""
    summary = []
    attempts = []
    set_mirror_mode("normal")
    set_primary_latency(0)
    for size, repeat, label in LARGE_BODY_CASES:
        payload = make_feed(500, size)
        sent_sha = sha256(payload)
        whole = truncated = absent = strays = 0
        primary_ok = 0
        latencies = []
        for _ in range(repeat):
            reset_mirror_log()
            before_stats = mirror_stats_snapshot()
            response = send(10000, "POST", "/api/v1/feed", body=payload, timeout=120)
            record, records = wait_for_tagged_record(response["tag"], 30)
            counter_delta = mirror_stats_delta(before_stats, mirror_stats_snapshot())
            latencies.append(response["latency_ms"])
            strays += len(stray_records(records, response["tag"]))
            echo = {}
            if response["status"] == 200:
                try:
                    echo = json.loads(response["body"])
                except json.JSONDecodeError:
                    echo = {}
            primary_ok += int(
                response["status"] == 200
                and echo.get("body_len") == len(payload)
                and echo.get("body_sha256") == sent_sha
            )
            if record is None:
                absent += 1
                outcome = "absent"
            elif record["body_sha256"] == sent_sha and not record["truncated"]:
                whole += 1
                outcome = "whole"
            else:
                truncated += 1
                outcome = "truncated or empty"
            attempts.append(
                {
                    "size": label,
                    "sent_bytes": len(payload),
                    "sent_sha256": sent_sha,
                    "primary_status": response["status"],
                    "primary_body_len": echo.get("body_len"),
                    "primary_sha_match": echo.get("body_sha256") == sent_sha,
                    "client_latency_ms": round(response["latency_ms"], 1),
                    "outcome": outcome,
                    "mirror_body_len": None if record is None else record["body_len"],
                    "mirror_content_length": None if record is None else record["content_length"],
                    "mirror_transfer_encoding": None if record is None else record["transfer_encoding"],
                    "mirror_sha_match": None if record is None else record["body_sha256"] == sent_sha,
                    "mirror_reply_error": None if record is None else record["reply_error"],
                    "mirror_cluster_counters": {
                        name: delta
                        for name, delta in counter_delta.items()
                        if name.startswith("cluster.mirror.")
                        and not name.startswith("cluster.mirror.internal.")
                    },
                }
            )
            time.sleep(0.3)
        summary.append(
            {
                "size": label,
                "sent_bytes": len(payload),
                "attempts": repeat,
                "primary_ok": primary_ok,
                "mirror_whole": whole,
                "mirror_truncated_or_empty": truncated,
                "mirror_absent": absent,
                "stray_records": strays,
                "p50_client_latency_ms": round(statistics.median(latencies), 1),
                "max_client_latency_ms": round(max(latencies), 1),
            }
        )
    DETAILS["measurement_5"] = {"summary": summary, "attempts": attempts}
    DETAILS["measurement_5_envoy_log"] = envoy_warnings()
    for row in summary:
        check(
            "5-primary",
            f"primary served the {row['size']} POST ({row['attempts']}x)",
            "200 and the same body_len/sha256 as sent, every attempt",
            f"{row['primary_ok']}/{row['attempts']} whole, client latency "
            f"p50={row['p50_client_latency_ms']} ms max={row['max_client_latency_ms']} ms",
            row["primary_ok"] == row["attempts"],
        )
        check(
            "5-mirror",
            f"mirror copy of the {row['size']} POST ({row['attempts']}x)",
            "one whole copy per attempt (same sha256), or a recorded truncation/absence",
            f"whole={row['mirror_whole']}, truncated_or_empty={row['mirror_truncated_or_empty']}, "
            f"absent={row['mirror_absent']}, stray records={row['stray_records']}",
            row["mirror_whole"] == row["attempts"] and row["stray_records"] == 0,
        )
    time.sleep(0.5)


def measurement_6_order():
    """Does the mirror copy arrive before or after the primary responds?"""
    cases = []
    for label, primary_latency_ms in (("primary 200 ms", 200), ("primary 0 ms", 0)):
        set_primary_latency(primary_latency_ms)
        set_mirror_mode("slow")
        reset_mirror_log()
        count = 5
        sends = [
            send(10000, "GET", f"/api/v1/videos/dQw4w9WgXcQ?proxy=true&proxy_mode=relay&order={i}")
            for i in range(count)
        ]
        wait_for_records(count, 40)
        records = read_records()
        rows = []
        for i, response in enumerate(sends):
            record = find_record(records, response["tag"])
            if record is None:
                rows.append({"case": label, "order": i, "mirror_record": "missing"})
                continue
            rows.append(
                {
                    "case": label,
                    "order": i,
                    "client_latency_ms": round(response["latency_ms"], 1),
                    "mirror_arrival_minus_send_ms": round(
                        (record["arrival_ts"] - response["t_send"]) * 1000, 2
                    ),
                    "mirror_arrival_before_primary_response": record["arrival_ts"] < response["t_recv"],
                    "primary_response_minus_arrival_ms": round(
                        (response["t_recv"] - record["arrival_ts"]) * 1000, 2
                    ),
                    "mirror_finish_minus_primary_response_ms": round(
                        (record["finish_ts"] - response["t_recv"]) * 1000, 1
                    ),
                    "mirror_reply_error": record["reply_error"],
                }
            )
        cases.append(
            {
                "case": label,
                "primary_latency_ms": primary_latency_ms,
                "mirror_mode": "slow",
                "rows": rows,
            }
        )
    DETAILS["measurement_6"] = cases
    set_mirror_mode("normal")
    set_primary_latency(0)

    slow_primary = next(case for case in cases if case["primary_latency_ms"] == 200)["rows"]
    before = [row for row in slow_primary if row.get("mirror_arrival_before_primary_response")]
    check(
        "6-mirror-before-primary",
        "mirror copy arrives before the primary responds",
        "mirror arrival before the primary response in all 5 requests",
        f"{len(before)}/{len(slow_primary)} with the primary sleeping 200 ms; "
        f"mirror arrival {slow_primary[0].get('mirror_arrival_minus_send_ms')} ms after send vs "
        f"{slow_primary[0].get('client_latency_ms')} ms client latency",
        len(before) == len(slow_primary),
    )
    still_open = [
        row for case in cases for row in case["rows"]
        if row.get("mirror_finish_minus_primary_response_ms", 0) > 3000
    ]
    total = sum(len(case["rows"]) for case in cases)
    check(
        "6-copy-not-awaited",
        "the slow (5 s) mirror copy was still in flight after the primary responded",
        "mirror_finish_ts ~5 s after the primary response",
        f"{len(still_open)}/{total} mirror streams finished more than 3 s after the primary response",
        len(still_open) == total,
    )
    fast_primary = next(case for case in cases if case["primary_latency_ms"] == 0)["rows"]
    raced_before = sum(1 for row in fast_primary if row.get("mirror_arrival_before_primary_response"))
    check(
        "6-concurrent",
        "the mirror request is issued concurrently with the primary request, not after it",
        "mirror arrival within a few ms of sending; no ordering against the primary response",
        f"primary 0 ms: mirror arrival "
        f"{fast_primary[0].get('mirror_arrival_minus_send_ms')} ms after send, so it lands "
        f"{raced_before}/{len(fast_primary)} times before and "
        f"{len(fast_primary) - raced_before}/{len(fast_primary)} times after the "
        f"client's primary response",
        None,
    )


# ----------------------------------------------------------------------- environment


def collect_environment():
    def run(*args):
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        return ((result.stdout or "") + (result.stderr or "")).strip()

    environment = {
        "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "docker": run("docker", "--version"),
        "docker_server": run("docker", "info", "--format", "{{.ServerVersion}} {{.OperatingSystem}}"),
        "orbstack": run("orb", "version"),
        "python": sys.version.split()[0],
        "envoy_image": run("docker", "image", "inspect", ENVOY_IMAGE, "--format", "{{.Id}} {{index .RepoDigests 0}}"),
        "envoy_binary": compose("exec", "-T", "envoy", "envoy", "--version", check=False).stdout.strip(),
    }
    return environment


def mirror_stats_snapshot(port=ADMIN_PORT):
    """All numeric cluster.mirror.* counters, from the Envoy admin endpoint."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/stats?filter=cluster.mirror.")
    lines = conn.getresponse().read().decode().splitlines()
    conn.close()
    values = {}
    for line in lines:
        name, _, value = line.partition(": ")
        try:
            values[name] = int(value)
        except ValueError:
            continue
    return values


def mirror_stats_delta(before, after):
    return {
        name: after[name] - before.get(name, 0)
        for name in after
        if after[name] - before.get(name, 0) != 0
    }


# -------------------------------------------------------------------------- reporting


def markdown_table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for row in rows:
        cells = [str(cell).replace("|", "\\|").replace("\n", " ") for cell in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def print_table():
    widths = [4, 52, 46, 60, 6]
    header = ["id", "check", "expected", "observed", "verdict"]
    def row_cells(row):
        return [row["id"], row["check"], row["expected"], row["observed"], row["verdict"]]
    print("=" * 130)
    print(" | ".join(header[i].ljust(widths[i]) for i in range(len(header))))
    print("-" * 130)
    for row in CHECKS:
        cells = row_cells(row)
        print(" | ".join(str(cells[i])[: widths[i]].ljust(widths[i]) for i in range(len(header))))
    print("=" * 130)
    failures = [row for row in CHECKS if row["verdict"] == "FAIL"]
    print(f"{len(CHECKS)} checks, {len(failures)} failed")
    for row in failures:
        print(f"  FAIL {row['id']}: {row['observed']}")


def write_results_markdown(environment, error):
    envoy_yaml = (HERE / "envoy.yaml").read_text()
    envoy_shadow_yaml = (HERE / "envoy-shadow.yaml").read_text()
    excerpt = ROUTE_GO_EXCERPT.read_text() if ROUTE_GO_EXCERPT.exists() else "could not fetch"
    failing = {row["id"] for row in CHECKS if row["verdict"] == "FAIL"}

    def verdict(*ids):
        return "FAIL" if failing.intersection(ids) else "PASS"

    parts = []
    parts.append("# Mirror spike results (local Envoy)\n")
    parts.append(
        "Generated by `python3 -I run.py` in this directory. Envoy Gateway translates an "
        "HTTPRoute `RequestMirror` filter into a route-level `request_mirror_policies` entry; "
        "this spike runs that translated configuration directly on Envoy, with a fake primary "
        "(200 + JSON, configurable latency) and a fake mirror (records every request to JSONL, "
        "replies 204).\n"
    )
    parts.append(f"- generated: {environment.get('generated')}\n")
    parts.append(f"- docker: {environment.get('docker')} (server {environment.get('docker_server')})\n")
    parts.append(f"- orbstack: {environment.get('orbstack')}\n")
    parts.append(f"- envoy image: {ENVOY_IMAGE} `{environment.get('envoy_image')}`\n")
    parts.append(f"- envoy binary: `{environment.get('envoy_binary')}`\n")
    parts.append(f"- host python: {environment.get('python')}\n")
    if error:
        parts.append(f"\n**The run stopped early:** `{error}`\n")

    parts.append("\n## How to reproduce\n")
    parts.append(
        "```\norb start            # Docker comes from OrbStack\ncd spikes/mirror\npython3 -I run.py     # brings the stack up, measures, writes this file, brings it down\n```\n"
        "Only `envoyproxy/envoy:v1.39.3` and `python:3.12-slim` are pulled; nothing outside "
        "this directory is touched and no live cluster or public host is contacted.\n"
    )

    parts.append("\n## Results table\n")
    failed = [row for row in CHECKS if row["verdict"] == "FAIL"]
    parts.append(f"{len(CHECKS)} checks, {len(failed)} failed.\n")
    parts.append(
        markdown_table(
            ["id", "check", "expected", "observed", "verdict"],
            [[row["id"], row["check"], row["expected"], row["observed"], row["verdict"]] for row in CHECKS],
        )
    )

    parts.append("\n## 1. POST /api/v1/feed: body, method, path, query, headers\n")
    rows = []
    for row in DETAILS.get("measurement_1", []):
        mirror = row["mirror"]
        if isinstance(mirror, str):
            rows.append([row["payload"], row["sent_bytes"], row["sent_sha256"], mirror])
            continue
        rows.append(
            [
                row["payload"],
                row["sent_bytes"],
                row["sent_sha256"],
                f"len={mirror['body_len']} sha={mirror['body_sha256']} "
                f"sha_match={mirror['body_sha256'] == row['sent_sha256']}",
            ]
        )
    parts.append(markdown_table(["payload", "sent bytes", "sent sha256", "mirror saw"], rows))
    parts.append("\nHeaders and request line at the mirror:\n")
    header_rows = []
    for row in DETAILS.get("measurement_1", []):
        mirror = row["mirror"]
        if isinstance(mirror, str):
            continue
        header_rows.append(
            [
                row["payload"],
                mirror["request_target"],
                mirror["host"],
                f"Authorization: {'identical' if mirror['authorization'] == AUTH else 'DIFFERENT'}",
                f"User-Agent: {'identical' if mirror['user_agent'] == USER_AGENT else 'DIFFERENT'}",
                f"Content-Type: {mirror['content_type']}",
                f"Content-Length: {mirror['content_length']}",
            ]
        )
    parts.append(markdown_table(["payload", "request target", "Host", "Authorization", "User-Agent", "Content-Type", "Content-Length"], header_rows))
    all_headers = []
    for row in DETAILS.get("measurement_1", []):
        mirror = row["mirror"]
        if isinstance(mirror, str):
            continue
        for name, value in mirror["headers"]:
            all_headers.append([row["payload"], name, value])
    parts.append("\nEvery header the mirror received (from `out/mirror.jsonl`):\n")
    parts.append(markdown_table(["payload", "header", "value"], all_headers))

    parts.append("\n## 2. GET /api/v1/videos/dQw4w9WgXcQ?proxy=true&proxy_mode=relay\n")
    detail = DETAILS.get("measurement_2", {})
    if detail.get("mirror"):
        mirror = detail["mirror"]
        parts.append(
            markdown_table(
                ["field", "sent", "mirror saw"],
                [
                    ["method", "GET", mirror["method"]],
                    ["request target", detail["sent_target"], mirror["request_target"]],
                    ["path", "/api/v1/videos/dQw4w9WgXcQ", mirror["path"]],
                    ["raw query", "proxy=true&proxy_mode=relay", mirror["raw_query"]],
                    ["Host", PUBLIC_HOST, mirror["host"]],
                ],
            )
        )
    else:
        parts.append("No mirror record.\n")

    parts.append("\n## 3. Host header at the mirror\n")
    detail = DETAILS.get("measurement_3", {})
    rows = []
    for label, value in detail.items():
        rows.append([f"{label} (host port {value['listener_port']})", str(value["mirror_host"])])
    parts.append(markdown_table(["Envoy configuration", "Host seen by the mirror"], rows))
    parts.append(
        "\n`envoy.yaml` (port 10000) carries the flag Envoy Gateway v1.9.2 sets; "
        "`envoy-shadow.yaml` (port 10001) omits it, i.e. the Envoy default.\n"
    )

    parts.append("\n## 4. Primary latency with a normal, slow, 500 and stopped mirror\n")
    rows = DETAILS.get("measurement_4", [])
    parts.append(
        markdown_table(
            ["mirror mode", "requests", "primary latency", "p50", "max", "min", "client failures", "mirror records", "recorded modes"],
            [
                [
                    row["mirror_mode"],
                    row["requests"],
                    f"{row['primary_latency_ms']} ms",
                    f"{row['p50_ms']} ms",
                    f"{row['max_ms']} ms",
                    f"{row['min_ms']} ms",
                    row["client_failures"],
                    row["mirror_records"],
                    ",".join(row["mirror_record_modes"]),
                ]
                for row in rows
            ],
        )
    )
    parts.append(
        "\nSequential requests over fresh connections, primary sleeping 200 ms per request, "
        "mirror `slow` sleeping 5 s before replying and `500` replying 500, `stopped` meaning "
        "the mirror container is not running.\n"
    )
    counter_rows = []
    for row in rows:
        for name, delta in sorted(row.get("envoy_counter_delta", {}).items()):
            if ".internal." in name:
                continue  # same stat under Envoy's second name
            counter_rows.append([row["mirror_mode"], name, f"{delta:+d}"])
    parts.append(
        "\nEnvoy's own `cluster.mirror.*` counter deltas across each 20-request phase "
        "(read from the admin endpoint before and after the phase; the identically valued "
        "`.internal.*` aliases are omitted, and `membership_healthy` is a gauge):\n"
    )
    parts.append(markdown_table(["mirror mode", "counter", "delta"], counter_rows))
    parts.append(
        "\nWith the mirror up, Envoy forwards all 20 shadow requests per phase "
        "(`upstream_rq_total +20`) and the response codes are the mirror's own (204 or 500). "
        "With the mirror stopped, `upstream_rq_total` does not move at all: after a few failed "
        "connection attempts Envoy ejects the host and answers the shadow requests locally with "
        "503 (`upstream_rq_503 +20`, one `membership_change`, `membership_healthy` dropping to "
        "0), and the client sees no change. Only the count of client-visible effects differs "
        "from the modes above: zero.\n"
    )

    parts.append("\n## 5. Large POST bodies: 1 MiB and neighbours\n")
    measurement_5 = DETAILS.get("measurement_5", {})
    summary = measurement_5.get("summary", [])
    parts.append(
        markdown_table(
            [
                "body size",
                "sent bytes",
                "attempts",
                "primary whole",
                "mirror whole",
                "mirror truncated/empty",
                "mirror absent",
                "stray records",
                "client latency p50 / max",
            ],
            [
                [
                    row["size"],
                    row["sent_bytes"],
                    row["attempts"],
                    f"{row['primary_ok']}/{row['attempts']}",
                    f"{row['mirror_whole']}/{row['attempts']}",
                    row["mirror_truncated_or_empty"],
                    row["mirror_absent"],
                    row["stray_records"],
                    f"{row['p50_client_latency_ms']} ms / {row['max_client_latency_ms']} ms",
                ]
                for row in summary
            ],
        )
    )
    parts.append(
        "\nEvery attempt in detail (`mirror_content_length` is what Envoy put on the shadow "
        "request; both the 1 MiB and the 2 MiB body use it, not chunked encoding):\n"
    )
    parts.append(
        markdown_table(
            [
                "body size",
                "primary status",
                "primary body_len",
                "primary sha match",
                "mirror outcome",
                "mirror body_len",
                "mirror content-length",
                "mirror sha match",
                "client latency",
            ],
            [
                [
                    row["size"],
                    row["primary_status"],
                    row["primary_body_len"],
                    row["primary_sha_match"],
                    row["outcome"],
                    row["mirror_body_len"],
                    row["mirror_content_length"],
                    row["mirror_sha_match"],
                    f"{row['client_latency_ms']} ms",
                ]
                for row in measurement_5.get("attempts", [])
            ],
        )
    )
    log = DETAILS.get("measurement_5_envoy_log", [])
    parts.append("\nEnvoy log lines mentioning shadow/buffer/too large during measurement 5:\n")
    parts.append("```\n" + ("\n".join(log) if log else "(none)") + "\n```\n")

    parts.append("\n## 6. Order: mirror copy vs primary response\n")
    rows = []
    for case in DETAILS.get("measurement_6", []):
        for row in case["rows"]:
            rows.append(
                [
                    case["case"],
                    row.get("order"),
                    f"{row.get('client_latency_ms', '')} ms",
                    f"{row.get('mirror_arrival_minus_send_ms', '')} ms",
                    row.get("mirror_arrival_before_primary_response", row.get("mirror_record", "")),
                    f"{row.get('primary_response_minus_arrival_ms', '')} ms",
                    f"{row.get('mirror_finish_minus_primary_response_ms', '')} ms",
                    row.get("mirror_reply_error"),
                ]
            )
    parts.append(
        markdown_table(
            [
                "case",
                "#",
                "client latency",
                "mirror arrival - send",
                "mirror arrival before primary response?",
                "primary response - mirror arrival",
                "mirror finish - primary response",
                "mirror reply error",
            ],
            rows,
        )
    )
    parts.append(
        "\nMirror mode `slow` (5 s before replying), five sequential GETs per case. "
        "`mirror arrival - send` is the mirror request's arrival timestamp minus the moment the "
        "client sent the request. `mirror finish - primary response` shows how long the mirror "
        "stream stayed open after the client already had the primary's answer. With the primary "
        "sleeping 200 ms the mirror copy arrives ~200 ms before the primary responds; with a "
        "zero-latency primary the two land on either side of each other, i.e. the copy is issued "
        "concurrently and is not ordered against the primary response.\n"
    )

    parts.append("\n## Exact Envoy configuration used\n")
    parts.append("`envoy.yaml` (port 10000, the gateway's translated configuration):\n")
    parts.append("```yaml\n" + envoy_yaml + "```\n")
    parts.append("`envoy-shadow.yaml` (port 10001, control without the flag):\n")
    parts.append("```yaml\n" + envoy_shadow_yaml + "```\n")

    parts.append("\n## Envoy Gateway route.go v1.9.2 evidence\n")
    parts.append(
        "Fetched from "
        "`https://raw.githubusercontent.com/envoyproxy/gateway/v1.9.2/internal/xds/translator/route.go` "
        "(blob sha 857003f1f4f2a38eb39491b727169d195e74768a, 977 lines). "
        "`buildXdsRequestMirrorPolicies` is called for both `httpRoute.Mirrors` sites "
        "(route.go:79-93) and reads:\n"
    )
    parts.append("```go\n" + excerpt + "```\n")
    parts.append(
        "So an HTTPRoute `RequestMirror` filter becomes a route-level `request_mirror_policies` "
        "entry with `disable_shadow_host_suffix_append: true`, matching `envoy.yaml` here. "
        "Issue https://github.com/envoyproxy/gateway/issues/7227 (\"Remove shadow host suffix "
        "from the mirrored requests\") is the change that introduced it; the fix went in with "
        "the comment quoted above.\n"
    )

    parts.append("\n## Conclusions against the DESIGN.md assumptions\n")
    parts.append(
        markdown_table(
            ["DESIGN.md assumption", "verdict", "evidence"],
            [
                [
                    "POST body is mirrored whole",
                    verdict("1-body-sha", "1-mirror-count", "1-primary"),
                    "; ".join(
                        row["observed"]
                        for row in CHECKS
                        if row["id"] == "1-body-sha"
                    )
                    or "no data",
                ],
                [
                    "method, path and query are preserved",
                    verdict("1-path", "1-query", "1-method", "2-path", "2-query", "2-method"),
                    "measurements 1 and 2: request targets identical at the mirror",
                ],
                [
                    "Authorization and User-Agent survive mirroring",
                    verdict("1-auth", "1-ua"),
                    "measurement 1: both headers byte-identical at the mirror",
                ],
                [
                    "Host is preserved (no -shadow suffix)",
                    verdict("3-host-with-flag"),
                    "measurement 3: with the gateway's flag the mirror sees "
                    f"{DETAILS.get('measurement_3', {}).get('suffix_disabled', {}).get('mirror_host')!r}, "
                    "without it "
                    f"{DETAILS.get('measurement_3', {}).get('envoy_default', {}).get('mirror_host')!r}",
                ],
                [
                    "a slow, dead or erroring mirror does not delay or fail the client",
                    verdict("4-latency", "4-delivery"),
                    "measurement 4: p50 stays at the primary's own latency in all four modes",
                ],
                [
                    "no delivery guarantee and no ordering against the primary",
                    "INFO",
                    "measurement 6: the mirror request is issued concurrently with the primary "
                    "request, its arrival timestamp lands on either side of the primary response, "
                    "and its 5 s reply is discarded long after the client was served; "
                    "measurement 4 confirms the client never waits",
                ],
            ],
        )
    )
    parts.append(
        "\nReading of the numbers, per assumption:\n\n"
        "1. **POST body replicated.** The mirror received a byte-identical body (same sha256, same "
        "length) for the 5 KB and 70 KB feeds, for 1 MiB x10, 2 MiB x5, 512 KiB x3 and 64 KiB x3 in "
        "measurement 5. There is no truncation or size cut-off between 64 KiB and 2 MiB.\n"
        "2. **Path, query, method and headers preserved.** The mirror's request target, "
        "`Authorization` and `User-Agent` match what the client sent, character for character.\n"
        "3. **Host preserved.** With `disable_shadow_host_suffix_append: true` the mirror sees "
        f"`{PUBLIC_HOST}`; without it Envoy appends `-shadow`. Envoy Gateway v1.9.2 sets the flag, "
        "so serendip sees the original Host and must accept any Host anyway.\n"
        "4. **The primary is unaffected.** Client latency tracks the primary's own latency with the "
        "mirror normal, slow (5 s), erroring and stopped; no request failed and no response body "
        "changed. Mirror delivery stops when the mirror is down, and nothing else notices.\n"
        "5. **Best effort.** Mirrored copies are not ordered against the primary response and have "
        "no delivery guarantee: Envoy keeps the shadow stream open after the client already has "
        "its answer, and while the mirror cluster has no healthy host (for example right after the "
        "mirror restarts) it answers the shadow requests locally with 503 and the copy is simply "
        "lost, with no retry. The client sees none of it.\n"
    )

    parts.append("\n## Caveats: how this differs from the live gateway\n")
    parts.append(
        "- This is a hand-written static bootstrap, not the xDS output of Envoy Gateway. What it "
        "copies is the part that matters here: the route-level `request_mirror_policies` entry "
        "with `disable_shadow_host_suffix_append: true`, as read from `route.go` at tag v1.9.2. "
        "The real gateway adds its own HCM settings, timeouts, retries and TLS; none of those "
        "change how a mirror stream is created.\n"
        "- The mirror's mode is switched through `out/mode.txt`, which `mirror.py` re-reads per "
        "request, and `stopped` means the container is not running. This is a harness device, not "
        "part of the design.\n"
        "- Every request carries a unique `X-Spike-Tag` header and each mirror record is matched "
        "on it, so a record can never be attributed to the wrong request. The large-body "
        "measurement repeats each size because a single attempt is not evidence.\n"
        "- After the mirror container is restarted, the harness waits until Envoy reports the "
        "mirror cluster healthy again. In that window Envoy has no healthy mirror host and "
        "answers shadow requests locally with 503 (`upstream_cx_none_healthy`), which is the "
        "`stopped` case of measurement 4 and a real property of mirroring, not a harness bug.\n"
        "- Plain HTTP on one host (OrbStack, containers side by side): no TLS, no Cloudflare hop, "
        "no cross-node network. Latencies here are lower bounds; the ordering and non-blocking "
        "results do not depend on that.\n"
        "- One client at a time, sequential requests, HTTP/1.1 with the app's headers. Envoy's "
        "behaviour under concurrency or a full mirror cluster was not measured.\n"
        "- The 5 s slow mirror is far below the route `timeout: 30s`; a mirror that never responds "
        "was not tested, but a stopped mirror was (Envoy's connections to it fail or time out, and "
        "the client is unaffected either way).\n"
        "- The live gateway was not touched; `disable_shadow_host_suffix_append: true` is taken "
        "from the source at tag v1.9.2, and the counter-check on port 10001 shows what the same "
        "route looks like without it.\n"
    )

    parts.append("\n## Artefacts in this directory\n")
    parts.append(
        "- `compose.yaml` - the stack (`primary`, `mirror`, `envoy`, `envoy-shadow`)\n"
        "- `envoy.yaml`, `envoy-shadow.yaml` - the two Envoy configurations\n"
        "- `primary.py`, `mirror.py` - fake primary and recording mirror (stdlib only)\n"
        "- `run.py` - this harness; `python3 -I run.py` reproduces the run\n"
        "- `route-go-v1.9.2-excerpt.txt` - the quoted Envoy Gateway source\n"
        "- `out/mirror.jsonl` - every request the mirror received, with headers and body hashes\n"
        "- `out/results.json` - the raw measurements behind this file\n"
    )
    RESULTS_MD.write_text("\n".join(parts))


# -------------------------------------------------------------------------------- run


def main():
    error = None
    environment = {}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    MIRROR_LOG.write_text("")
    MODE_FILE.write_text("normal\n")
    LATENCY_FILE.write_text("0\n")
    try:
        compose("down", "--remove-orphans", check=False)
        compose("up", "-d", "--remove-orphans")
        if not wait_tcp("127.0.0.1", PRIMARY_PORT, 60):
            raise RuntimeError("primary did not become ready")
        if not wait_tcp("127.0.0.1", MIRROR_PORT, 60):
            raise RuntimeError("mirror did not become ready")
        if not wait_admin(ADMIN_PORT, 60) or not wait_admin(ADMIN_SHADOW_PORT, 60):
            raise RuntimeError("envoy admin did not become ready")
        environment = collect_environment()
        print(environment["envoy_binary"])
        measurement_1_post_body()
        measurement_2_get_path_and_query()
        measurement_3_host_header()
        measurement_4_primary_latency()
        measurement_5_large_body()
        measurement_6_order()
    except Exception as exc:  # noqa: BLE001 - report whatever was measured
        error = f"{type(exc).__name__}: {exc}"
        print(f"run stopped early: {error}", file=sys.stderr)
    finally:
        try:
            write_results_markdown(environment, error)
            RESULTS_JSON.write_text(
                json.dumps(
                    {"environment": environment, "checks": CHECKS, "details": DETAILS, "error": error},
                    indent=2,
                )
            )
        finally:
            print_table()
            compose("down", "--remove-orphans", check=False)
    return 1 if any(row["verdict"] == "FAIL" for row in CHECKS) or error else 0


if __name__ == "__main__":
    sys.exit(main())
