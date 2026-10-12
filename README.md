# serendip

A self-hosted discover feed for [Yattee](https://github.com/yattee/yattee).

serendip sits in front of [Yattee Server](https://github.com/yattee/yattee-server) as a
transparent proxy. It learns from your subscriptions and what you watch, then answers Yattee's
**Popular** / **Trending** home sections with a fresh mix on every refresh: new uploads from
your channels, related videos, channels you don't follow yet. No Google account, no YouTube
algorithm.

Status: stage (a), observe-only ingest and SQLite storage, is live. Stage (b), the discover feed
(`GET /api/v1/popular`, follow-ups, the feed job and the breaker), is built and not yet deployed.
The brief is in [`docs/BRIEF.md`](docs/BRIEF.md), the design in [`docs/DESIGN.md`](docs/DESIGN.md).

## Running locally

```sh
uv sync --extra dev
export SERENDIP_DB_PATH=./serendip.db
uv run python -m serendip migrate
uv run python -m serendip serve    # API on :8080, mirrored-traffic ingest on :8081
uv run pytest -q
```

## Settings

Environment variables, all prefixed `SERENDIP_` (see `src/serendip/config.py` for the full list):

| Variable | Default | Meaning |
|---|---|---|
| `SERENDIP_USERS` | empty | Comma-separated `name=<sha256>` entries, one per allowed user |
| `SERENDIP_DB_PATH` | `/data/serendip.db` | SQLite database file |
| `SERENDIP_YATTEE_URL` | in-cluster Yattee Server | Where serendip calls Yattee |
| `SERENDIP_DEVICE_UA_MAP` | empty | `serendip-tv=tv,serendip-phone=phone`: User-Agent marker to device |
| `SERENDIP_YATTEE_USERNAME`, `SERENDIP_YATTEE_PASSWORD` | unset | serendip's own non-admin Yattee Server user. Without both, the feed job and follow-ups are off and `/api/v1/popular` serves from the database |
| `SERENDIP_RELATED_SOURCE` | `followup` | `followup` or `off`; `next` is reserved and refused at startup |
| `SERENDIP_LIST_SIZE`, `SERENDIP_HEAD_SIZE`, `SERENDIP_CARRY_MAX` | `30`, `5`, `0.3` | List length, never-served head, carry-over ceiling (0 to 0.5) |
| `SERENDIP_REPEAT_WINDOW_S` | `15` | A request this soon after a list on the same device gets that list again |
| `SERENDIP_FOLLOWUP_DELAY_S`, `_DEADLINE_S`, `_PER_HOUR`, `_PER_DAY`, `_MIN_SPACING_S` | `60`, `120`, `2`, `20`, `600` | Follow-up timing and rolling budgets |
| `SERENDIP_FEED_INTERVAL_S`, `SERENDIP_FEED_LIMIT` | `21600`, `1000` | At most one feed post per interval across all users (one chunk of up to 500 channels, users and chunks in turn), and the per-chunk limit |
| `SERENDIP_BREAKER_START_OPEN` | `false` | `true` opens the breaker on the very first start with a fresh database (reason `initial: confirm before enabling jobs`), so no job runs until `python -m serendip breaker reset`. Later starts ignore it |
| `SERENDIP_SHORTS_MAX_SECONDS`, `SERENDIP_TIMEZONE` | `180`, `Europe/Warsaw` | Shorts cut-off; zone for the daypart |
| `SERENDIP_VERSION` | `dev` | Shown by `/status`; set by the image build |
| `SERENDIP_LOG_LEVEL` | `INFO` | Log level |

The hash in `SERENDIP_USERS` is the lowercase hex sha256 of the full `Authorization` header
value the client sends. For a Yattee instance logging in as `janpuc` with password `secret`:

```sh
printf 'Basic %s' "$(printf 'janpuc:secret' | base64)" | shasum -a 256 | cut -d' ' -f1
# SERENDIP_USERS=janpuc=<that hash>
```

## Breaker

Follow-ups and feed posts stop at the first sign of trouble (bot-check text, 5xx, malformed
reply, timeout, 401/403/429) and stay stopped until reset. `/status` shows the reason.

```sh
python -m serendip breaker reset
python -m serendip breaker trip --reason "maintenance"
```

Every call to Yattee Server goes through one gate that checks the breaker, the follow-up
deadline and the clocks at the moment of sending. If the wall clock and the monotonic clock
disagree by more than 60 s between two sends, nothing is sent (a warning is logged and
`serendip_sends_skipped_total{reason="clock"}` counts it) until the process is restarted.

## Image

`ghcr.io/janpuc/serendip` (linux/amd64). Every push to `main` that passes CI publishes `:edge`
and `:sha-<short>`; merging the release-please PR also publishes `:X.Y.Z`, `:X.Y`, `:X` and
`:latest`. The container runs as uid 1000, keeps its data in `/data`, serves on 8080 (API) and
8081 (ingest), and works with a read-only root filesystem. Apply migrations first, for example
in an init container:

```sh
docker run --rm -v serendip-data:/data ghcr.io/janpuc/serendip python -m serendip migrate
docker run -d -v serendip-data:/data -p 8080:8080 -p 8081:8081 \
  -e SERENDIP_USERS=janpuc=<sha256> ghcr.io/janpuc/serendip
```

To build it yourself: `docker build --build-arg SERENDIP_VERSION=dev -t serendip .`
