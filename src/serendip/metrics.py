"""Prometheus metrics."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client import generate_latest as _generate

REGISTRY = CollectorRegistry()

CONTENT_TYPE = CONTENT_TYPE_LATEST

ingest_requests = Counter(
    "serendip_ingest_requests_total",
    "Mirrored requests received",
    ["kind", "outcome"],
    registry=REGISTRY,
)
ingest_dropped = Counter(
    "serendip_ingest_dropped_total",
    "Mirrored requests or events dropped",
    ["reason"],
    registry=REGISTRY,
)
queue_depth = Gauge(
    "serendip_ingest_queue_depth", "Requests waiting for the consumer", registry=REGISTRY
)
auth_checks = Counter(
    "serendip_auth_checks_total", "Authorization verdicts", ["result"], registry=REGISTRY
)
events = Counter("serendip_events_total", "Events stored", ["kind"], registry=REGISTRY)
jobs = Counter(
    "serendip_jobs_total", "Background job outcomes", ["job", "outcome"], registry=REGISTRY
)
server_requests = Counter(
    "serendip_server_requests_total",
    "Calls to Yattee Server",
    ["endpoint", "status"],
    registry=REGISTRY,
)
sends_skipped = Counter(
    "serendip_sends_skipped_total",
    "Calls to Yattee Server the gate refused at the moment of sending",
    ["reason"],
    registry=REGISTRY,
)
followup_seconds = Histogram(
    "serendip_followup_seconds",
    "Time Yattee Server took to answer a follow-up",
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 90),
    registry=REGISTRY,
)
list_items = Counter(
    "serendip_list_items_total", "Items in served lists", ["kind"], registry=REGISTRY
)
breaker_open = Gauge(
    "serendip_breaker_open", "1 while the breaker stops calls to Yattee Server", registry=REGISTRY
)


def render() -> bytes:
    return _generate(REGISTRY)
