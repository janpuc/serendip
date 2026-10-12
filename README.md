# serendip

A self-hosted discover feed for [Yattee](https://github.com/yattee/yattee).

serendip sits in front of [Yattee Server](https://github.com/yattee/yattee-server) as a
transparent proxy. It learns from your subscriptions and what you watch, then answers Yattee's
**Popular** / **Trending** home sections with a fresh mix on every refresh: new uploads from
your channels, related videos, channels you don't follow yet. No Google account, no YouTube
algorithm.

Status: stage (a), observe-only ingest and SQLite storage, is built; no discover feed yet.
The brief is in [`docs/BRIEF.md`](docs/BRIEF.md), the design in [`docs/DESIGN.md`](docs/DESIGN.md).
