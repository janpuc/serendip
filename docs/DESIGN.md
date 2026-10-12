# serendip design

Status: revised after the advisor review, 2026-10-12. Nothing is built yet. Evidence for every
claim about Yattee, Yattee Server and the cluster is in `docs/SPIKE.md`. App paths are relative
to the app's `Yattee/` directory, server paths to the yattee-server repository root.

## 1. Decisions

- **Envoy mirrors, serendip is not in the request path.** Envoy sends serendip fire-and-forget
  copies of the requests that carry signals; the real requests go to Yattee Server as today.
  In stage (b) `GET /api/v1/popular` is routed to serendip. If serendip is down, playback,
  search, channels and the subscriptions feed are unaffected.
- **Observation is best effort.** A mirror proves that the app sent a request, not that it
  succeeded, that a video played, or that it was finished. serendip records *observed opens*
  and does not try to infer playback or completion.
- **Refresh cadence.** The pinned Home section is cached by the app for 30 minutes and refreshes
  when Home's `.task` runs after that (`Services/HomeInstanceCache.swift:45-51`,
  `Views/Home/HomeView.swift:142-146, 1500-1517`). Jan accepted this (2026-10-12). The Home
  shortcut card opens a new browse view that fetches when it is created; iPhone also has
  pull-to-refresh there.
- **"Never shows something he finished"** is met by never showing a video he opened in the
  last 90 days. Opens serendip does not see (offline downloads, other sources) can slip
  through; Jan accepted this (2026-10-12). The browse view's "hide watched" option
  (`Views/Instances/InstanceBrowseView.swift:585-590`) filters the app's own finished history
  on top.
- **Upstream calls only from budgeted background jobs**, stopping at the first sign of a bot
  check until Jan re-enables them. Every request is served from local storage.
- **No AI in v1** (section 8).

## 2. Language: Python 3.12

uv, FastAPI, httpx, ruff, pytest, as the brief says, kept on the evidence:

- The fragile code serendip owns is the InnerTube `/next` request and the parser for related
  items. Yattee Server has the parser in Python under MIT: 261 self-contained lines
  (`innertube/_converters.py:16-115, 940-1139`) with synthetic tests
  (`tests/test_innertube_recommended_videos.py`). serendip vendors the parser and tests with
  attribution and re-syncs when upstream fixes a YouTube layout change. It writes its own small
  client (endpoint, key, WEB context, no cookies, no automatic retries) rather than copying
  upstream's cookie and retry handling (`innertube/_client.py:45-79, 225-350`).
- Go's advantage is a streaming reverse proxy; with mirroring serendip proxies nothing.
- Same toolchain as tuiter for CI, release-please and the image.

## 3. Architecture

```
Yattee app ──► Envoy (yattee.janpuc.com) ──► yattee-server:8085          (all requests, as today)
                    ├─ RequestMirror ───────► serendip:8081 (ingest)     (record, reply 204)
                    └─ GET /api/v1/popular ─► serendip:8080 (api)        (stage b)

serendip worker ──► yattee-server:8085 in-cluster, own user: POST /api/v1/feed (stage b)
                └─► www.youtube.com/youtubei/v1/next   (anonymous, budgeted)
```

serendip listens on two ports: **8080** for `/api/v1/popular`, `/health`, `/status` and
`/metrics`, and **8081** for mirrored traffic only, which receives requests on their original
paths. Port 8081 is reachable only through the mirror rules; `/status` and `/metrics` stay
internal (no route).

HTTPRoute rules for `yattee.janpuc.com`, prepared on a home-ops branch and applied by Jan:

| Match | Backend | Filter |
|---|---|---|
| `GET` `PathPrefix /api/v1/videos` | yattee-server | `RequestMirror` → serendip:8081 |
| `POST` `Exact /api/v1/feed` | yattee-server | `RequestMirror` → serendip:8081 |
| `GET` `Exact /api/v1/search` | yattee-server | `RequestMirror` → serendip:8081 |
| `GET` `Exact /api/v1/popular` (stage b) | serendip:8080 | none |
| everything else | yattee-server | none |

`Exact /api/v1/search` does not match `/api/v1/search/suggestions`, so typeahead is not
mirrored. Channel views are left out of v1: their path prefix also carries hundreds of avatar
requests.

serendip calls Yattee Server through the in-cluster Service, never the public hostname, so its
own requests are never mirrored.

**Mirror contract.** Envoy mirrors the method, path, query, headers and body, and discards
the mirror's response. There is no delivery guarantee, no ordering against the primary, and no
view of the primary's status or body. serendip therefore:

- bounds request bodies (64 KiB), connections and an in-memory ingest queue, and drops when
  full rather than slowing anything;
- accepts any `Host` and does not depend on the `-shadow` suffix behaviour;
- replies 204 immediately and processes asynchronously.

Body mirroring for `POST`, original path and query, `Host` handling, and behaviour with a slow
or dead mirror are tested in stage (a) on a local Envoy with fake primary and mirror backends,
then once on the live gateway. If `POST` bodies are not mirrored, the subscriptions signal is
missing and the design is reconsidered with Jan; there is no silent inline fallback.

### 3.1 Authentication

- **serendip checks credentials locally and never forwards them.** `SERENDIP_USERS` lists
  `name=<sha256>` entries, the SHA-256 of each user's full `Authorization` header. The
  ExternalSecret derives it from the same 1Password item that provisions the server's user, so
  a password change updates both. The server has one user today (`janpuc`).
- Why not ask the server: its limiter allows 5 failed logins per peer IP per minute and
  applies it before validating, even to valid credentials (`settings.py:131-132`,
  `basic_auth.py:168-182`). Any failing check serendip forwarded, from an attacker, a stale
  password, a retry after a timeout or a restart, would bring serendip's own checks closer to
  a 429. Two review rounds found new ways to reach it; a local check has none.
- A header that is not on the list, or is malformed, drops the event. A new Yattee user is not
  recognised until added to the list.
- `/api/v1/popular` without a valid header gets 401 with `WWW-Authenticate`, as the server
  sends. Mirrored requests whose header does not validate are dropped.
- serendip's own background calls use a separate Yattee Server user (`serendip`, created by
  Jan) whose credentials come from 1Password through an ExternalSecret.
- `Authorization` headers and signed query strings are redacted everywhere: logs, object
  representations, and every stored free-text value (search terms, channel names), which are
  sanitised at ingest. serendip never fetches a URL taken from a mirrored request.
- Popular responses carry `Cache-Control: private, no-store`.

## 4. Signals (v1)

| Mirrored request | Event | Use |
|---|---|---|
| `POST /api/v1/feed` | `subscriptions_seen` | add or refresh observed subscriptions |
| `GET /api/v1/videos/{id}` (any query) | `open` | exclude the id for 90 days; interest in its channel |
| `GET /api/v1/search?q=` | `search` | stored for later topic use; not used for candidates in v1 |

Every event stores the user id, UTC time and device. Daypart is derived from UTC in
Europe/Warsaw when needed: morning 06-12, afternoon 12-18, evening 18-23, night 23-06.

- **Opens are ambiguous by design.** The same request comes from playing, opening the info
  view, the queue preloading the next item, downloads and offline-file playback
  (`Services/Player/QueueManager.swift:353-399`, `Services/API/ContentService.swift:259-292`).
  All of them count as an open: for exclusion that errs on the safe side, and for interest it is
  a modest signal.
- **Subscriptions** are a union of every channel seen in a mirrored post, with
  `last_seen_by_user`. Partial posts are normal (the background refresher sends only channels
  with notifications on, `Services/BackgroundRefresh/BackgroundFeedRefresher.swift:72-105`;
  long lists are chunked at 500), so missing channels are never treated as unsubscribed. A
  channel unseen for 30 days stops being used. `serendip subs reset --user` clears the set.
  serendip's own `POST /api/v1/feed` calls never update `last_seen_by_user`.
- **Device**: the app sends a random desktop-browser User-Agent on every platform. Jan can set
  a custom one per device in Yattee's Advanced settings (local, not synced); serendip maps
  configured substrings to `tv` or `phone`, otherwise `unknown`. MPV also sends this UA to
  googlevideo, so it should be a real browser UA with a marker appended
  (`... Safari/605.1.15 serendip-tv`). Device is optional: daypart works without it.
- Not in v1: relay requests, playback or completion inference, channel views.

## 5. Candidate pool

Built by a background worker; requests only read it.

| Pool | Source | Upstream |
|---|---|---|
| `subs` | `POST /api/v1/feed` with the user's active subscriptions, uploads from the last 30 days | none from YouTube in the normal case, see below |
| `related` | anonymous InnerTube `/next` for selected seeds: recent opens, and recent `subs` uploads the user opened | 1 request per seed, budgeted |
| `adjacent` | items in related lists whose channel the user does not follow; channels appearing in related lists of two or more different seeds rank higher | none (from `related`) |

`gems` is a tag, not a pool: items older than a year with high view counts in any pool get a
small bonus so older videos keep appearing.

- **`POST /api/v1/feed` is not free of side effects.** The server refreshes each channel's
  `last_requested`, schedules avatar fetches, and immediately queues a fetch for channels it has
  not cached (`routers/subscriptions.py:152-160, 182-188`). serendip posts only channels the
  user has posted in the last 30 days, which the server already watches because of the app, at
  most every 6 hours. It never posts adjacent or exploration channels: the server would poll
  each one every 6 hours until 14 days pass without a post (`feed_fetcher.py:786-792`).
- Related items carry only relative `publishedText` and hard-code `isShort` false
  (`innertube/_converters.py:1093-1100`). serendip derives an approximate upload time from the
  text as the server does (`converters/_formatting.py:64-94`).
- **Filters**: no video opened by the user in the last 90 days; no item with `lengthSeconds` 0
  (live and upcoming); no Shorts, approximated as `lengthSeconds` ≤ 180 unless known otherwise
  (Shorts can run three minutes and related items do not say); no item shown to the user in
  the last 2 hours.
- **Expiry**: `subs` items 30 days after upload, others 30 days after they were found.

## 6. Selection

Per request, from SQLite, target under 100 ms, N = 30.

1. **Eligible set**: all pools merged and deduplicated by video id, filters applied.
2. **Weight** (all factors positive, floors keep cold start working):
   - `interest` = 1 + recency-weighted opens on the item's channel (half-life 30 days), with
     subscribed channels starting at 2 and adjacent channels at 1 + 0.5 × number of seeds they
     appeared under, capped;
   - `freshness` = 0.3 + 0.7 × exp(−age / 14 days) for `subs`; 1 for everything else
     (`related` and `adjacent` are not penalised for age; `gems` gets × 1.3);
   - `daypart` = an explicit prior on video length per daypart (for example morning favours
     under 15 minutes, evening favours over 15), modest (× 0.7 to × 1.3), the same for every
     device in v1;
   - `novelty` = 0.6 if the channel was shown in the last 24 hours, else 1.
3. **Draw** without replacement with the exponential race: key = −ln(U) / w, smallest keys win,
   U from a generator seeded by (user id, request time). The seed and an algorithm version are
   logged; a list is reproducible from the same candidate snapshot.
4. **Mix and caps while drawing**, not after: at most 2 items per channel, 1 for a channel the
   user does not follow; aim for roughly 40% subscriptions and 60% related or adjacent, by
   skipping a draw that would exceed the share while the other side still has candidates.
5. **Shortage**: if fewer than N items qualify, return a shorter list. Filters are never
   relaxed.
6. **Impressions**: the chosen list is written in the same transaction that read the
   eligible set, so the iPhone and the Apple TV cannot be served overlapping lists by a race.
7. **Order**: subscriptions and discoveries interleaved.
8. **Response**: a JSON array of `VideoListItem`, the shape of the server's `/api/v1/trending`
   (`models.py:127-150`). Yattee needs `videoId`, `title`, `author`, `authorId`,
   `lengthSeconds` on every item, and one bad item fails the whole list
   (`Services/API/YatteeServerAPI.swift:603-646`), so each item is validated before sending.

Two lists within 2 hours do not share items, as long as the pool holds more than 60 eligible
items under the caps. `/status` reports the eligible count so a thin pool is visible.

## 7. Upstream budget

Context from the spike: the server itself made 286 `/browse`, 12 `/next` and 8 `/player` calls
in its first hour, all HTTP 200, and its 6-hourly fetcher adds about two `/browse` calls per
subscribed channel per run (66 channels). YouTube is reached from a hosted IP
(a Warsaw hosting network), which is flagged sooner than a residential one. The Q5 measurement
(`docs/SPIKE.md`) is an initial observation of tolerance, not proof of a safe rate.

v1 makes exactly one kind of YouTube request: `/next`.

- **Budget**: 3 per hour, 30 per day, one at a time, ±30% jitter, persisted so restarts do not
  reset it. Fixed client version (no homepage fetches). No automatic retries.
- **Breaker**: HTTP 429 or 403, a response containing sign-in or bot text, or a response
  without `contents` stops all `/next` calls until Jan re-enables them (`serendip breaker
  reset`, or an env flag). It logs at ERROR, sets `serendip_breaker_open 1`, and shows the
  reason in `/status`. Serving continues from the pool. The `UNPLAYABLE`/`LOGIN_REQUIRED`
  states the server already logs for anonymous `/player` are normal and not a trigger.
- **Metrics** (Prometheus, scraped through a ServiceMonitor by the cluster's
  kube-prometheus-stack): `serendip_jobs_total{job,outcome}`,
  `serendip_server_requests_total{endpoint,status}` for calls to Yattee Server, and
  `serendip_upstream_attempts_total{kind,outcome}` for requests that reach YouTube.

## 8. AI decision

**Not in v1.** One user's opens are a thin signal; channel interest and co-occurrence in
related lists already express "more like this", and an embedding service would add a
dependency with its own GPU failure alert (memini's `prometheusrule.yaml`).

Revisit after a few weeks of use, if lists feel repetitive across channels or miss obvious
interests. The test would be: freeze candidate snapshots, build lists with and without title
embeddings (`bge-m3`, called directly at
`http://bge-embedding-m3.ai.svc.cluster.local:8080/v1`; LiteLLM does not route to it), and
compare them by interleaving in live lists plus Jan's own judgement. One user's data cannot
support a fixed numeric threshold. Any embedding work would run in the background build with
the heuristic as fallback.

## 9. Data model (SQLite, WAL, one replica, on a Kopiur-backed PVC)

| Table | Columns |
|---|---|
| `users` | id (internal), name (from `SERENDIP_USERS`, unique), first_seen |
| `events` | id, user_id, ts_utc, device, kind, video_id, query |
| `subscriptions` | user_id, channel_id, name, first_seen, last_seen_by_user |
| `opens` | user_id, video_id, channel_id, last_open_utc, count (unique user_id + video_id) |
| `videos` | video_id, title, author, author_id, length_s, published_est, view_count, thumbnails (JSON), found_at |
| `pool` | user_id, video_id, source (`subs`/`related`/`adjacent`), seed_video_id, found_at, expires_at (unique user_id + video_id + source) |
| `impressions` | user_id, video_id, ts_utc, device, position, request_id |
| `upstream_attempts` | ts_utc, kind, target, status, seconds, outcome |
| `budget`, `breaker` | single-row state |

Indexes cover the exclusion queries (opens by user and time, impressions by user and time).
Retention: events 180 days, impressions and upstream attempts 30 days. Migrations run in an
init container as in tuiter. Backups are Kopiur snapshots of the PVC; serendip checkpoints the
WAL every few minutes so a snapshot is consistent enough to restore.

## 10. Failure modes

| Failure | Effect | Handling |
|---|---|---|
| serendip down | Popular fails; the pinned section keeps its cached list, the browse view shows an error | nothing else is affected |
| Pool empty or worker failing | | serve from the stored pool with the same filters; if nothing qualifies, return an empty list. Never a request-time call to YouTube or to the server's `/popular` |
| Bot check | `/next` stops | breaker, manual re-enable; pool and `subs` keep serving |
| Yattee Server down | `subs` cannot refresh | the stored pool keeps serving; auth is local, so `/popular` still answers |
| `/next` layout change | parser yields nothing | breaker on missing `contents`; metric on empty lists; re-sync the vendored parser |
| Mirror lost or delayed | an open is missed | accepted (best effort) |
| Bad item | the app rejects the whole list | validation drops the item before sending |

## 11. Test plan

- **Mirror spike (stage a, before deploy)**: local Envoy (OrbStack) with a fake primary and
  serendip as mirror: `POST` body arrives intact, path and query unchanged, `Host` as sent,
  primary latency unaffected with serendip slow, stopped, or returning errors.
- **Unit**: event extraction from recorded requests (from real server log lines); subscription
  union and ageing with partial and chunked posts; local allowlist (known, unknown and
  malformed credentials, and no outgoing request in any case); redaction; budget and breaker
  transitions; vendored parser
  against upstream's tests plus recorded real `/next` responses; response validation.
- **Selection**: deterministic under a seed and snapshot; never returns opened (90 days) or
  recently shown items; caps and the subscription share hold during the draw; shortage returns
  a shorter list; two consecutive lists do not overlap when the pool is large enough; the same
  device at 08:00 and at 21:00 gets a different length mix.
- **Integration**: FastAPI test client with a fake Yattee Server and fake InnerTube (respx):
  mirrored events → worker → pool → `/popular`, asserting the number of upstream attempts.
- **Live (Jan, after each deploy)**: mirrors arrive (stage a); lists on iPhone and Apple TV
  (stage b); the server log shows no change in upstream patterns beyond serendip's own `/next`
  calls.

## 12. Stages

- **(a) Observe**: service skeleton, both ports, mirror ingestion, auth check, SQLite,
  subscriptions union, opens, metrics, `/status`, the local mirror spike. **No outgoing calls
  at all and no change to Popular.** Home-ops branch: serendip release, ExternalSecret, PVC,
  ServiceMonitor, and the mirror rules on the yattee-server route.
- **(b) Discover**: `/next` client with budget and breaker, pool builder, selection, `/popular`
  route to serendip.
- **(c) Tune**, only on evidence from use: weights, device-specific priors, search-based
  exploration, channel uploads for adjacent channels, AI.

## 13. Open items

- Q5 (`docs/SPIKE.md`): 42 of 42 heavy lookups clean at up to about 60 an hour, so the
  `/next` budget of 3 an hour stands.
- Whether a current `/next` response offers a continuation for more related items (upstream
  never asks for one).
- Jan: custom User-Agent strings per device (optional); whether Shorts stay out with the
  180-second cut-off; list size 30; create the `serendip` user in Yattee Server's admin UI and
  a 1Password field for it before stage (a) deploys.
