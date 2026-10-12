"""Prometheus metrics."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge
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


def render() -> bytes:
    return _generate(REGISTRY)
