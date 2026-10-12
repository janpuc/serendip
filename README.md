# serendip

A self-hosted discover feed for [Yattee](https://github.com/yattee/yattee).

serendip sits in front of [Yattee Server](https://github.com/yattee/yattee-server) as a
transparent proxy. It learns from your subscriptions and what you watch, then answers Yattee's
**Popular** / **Trending** home sections with a fresh mix on every refresh: new uploads from
your channels, related videos, channels you don't follow yet. No Google account, no YouTube
algorithm.

Status: stage (a), observe-only ingest and SQLite storage, is built; no discover feed yet.
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
| `SERENDIP_VERSION` | `dev` | Shown by `/status`; set by the image build |
| `SERENDIP_LOG_LEVEL` | `INFO` | Log level |

The hash in `SERENDIP_USERS` is the lowercase hex sha256 of the full `Authorization` header
value the client sends. For a Yattee instance logging in as `janpuc` with password `secret`:

```sh
printf 'Basic %s' "$(printf 'janpuc:secret' | base64)" | shasum -a 256 | cut -d' ' -f1
# SERENDIP_USERS=janpuc=<that hash>
```

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
