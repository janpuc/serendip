# serendip design

Status: stage (a) is live (2026-10-12); stage (b) revised after the second advisor review the
same day. Evidence for every claim about Yattee, Yattee Server and the cluster is in `docs/SPIKE.md`. App paths are relative
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
- **A real refresh** (Jan, 2026-10-12): every refresh shows mostly videos he has not been shown
  before, keeping at most 20-40% of the previous list, reshuffled (section 6.1). YouTube's own
  refresh keeps about 95% and reorders it. *When* a refresh happens is up to the app: the
  browse view fetches when it is opened and on pull-to-refresh (iPhone), and the pinned section
  at most every 30 minutes. Popular has no load-more at the bottom: it is a single request
  without parameters (`Services/API/YatteeServerAPI.swift:30-35`), and the browse view's
  load-more covers only Subscriptions and Search (`InstanceBrowseView.swift:838-873`).
  No changes to the Yattee app (Jan, 2026-10-12): serendip treats the requests the app already
  sends as the refresh events and reacts to them.
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

- The fragile code serendip will own once anonymous `/next` is added (stage c) is the
  InnerTube request and the parser for related items. Yattee Server has the parser in Python under MIT: 261 self-contained lines
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

serendip worker ──► yattee-server:8085 in-cluster, own user (stage b):
                    POST /api/v1/feed, GET /api/v1/videos/{id} follow-ups
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
| `related` | **follow-up**: `GET /api/v1/videos/{id}` on the in-cluster server about 60 s after the user opens a video, keeping its `recommendedVideos` | unknown per call, see 5.1; reserved as a full extraction |
| `adjacent` | items in related lists whose channel the user does not follow | none (from `related`) |

### 5.1 Follow-ups instead of YouTube calls

After the bot-check of 2026-10-12 (`docs/SPIKE.md`, Q5) serendip makes no YouTube calls of its
own. Related videos come from the server: when serendip observes an open, it asks the
in-cluster server for the same video about 60 s later. The server answered that id for the app
moments before. Both of its caches (InnerTube and yt-dlp) are keyed by video id only, hold 100
entries and expire an hour after they were filled; a read does not renew them
(`innertube/_video.py:24-52`, `ytdlp_wrapper/_cache.py:18-24`, `settings.py:21`). So a
follow-up usually causes no upstream request, but not always: after a restart or an eviction,
for an entry about to expire, while the original is still extracting, or when the original
failed, it becomes a full extraction inside the server (`/player`, `/next`, sometimes a second
`/player`, and a yt-dlp run, with the server's own retries), now with the account's cookies.
serendip cannot tell which happened. Every follow-up is therefore reserved in the budget as a
full extraction job (section 7).

- **Personalisation.** Since Jan added a throwaway account's cookies (2026-10-12), the server
  fetches `/next`, and so `recommendedVideos`, signed in to that account
  (`innertube/_client.py:257, 287-298`; `innertube/_video.py:60`). Follow-ups reuse related
  lists YouTube produced for that account; serendip itself holds no Google credentials. The
  server runs yt-dlp without `--mark-watched` and makes no playback-reporting calls
  (`ytdlp_wrapper/_youtube.py:112-120`), so it does not knowingly build a watch history on the
  account; what Google records from signed-in `/next` calls is unknown. Jan accepted this
  (2026-10-12) on condition that it is switchable.
- **`SERENDIP_RELATED_SOURCE`**: `followup` (default) or `off` (subscriptions only), applied at
  restart. A third value, `next` (serendip's own anonymous `/next`, unpersonalised), is
  reserved for a later stage and refused at startup until it exists.
- **Scheduling.** A follow-up is attempted about 60 s after the observed open and dropped if it
  cannot start within 120 s (budget used up, breaker open, another follow-up running). It is
  never queued for later, so a delayed job cannot turn into a deliberate cold extraction.
  Follow-ups are deduplicated by video id across users and skipped for ids fetched in the last
  6 hours or that failed before. Seeds known to be live are skipped; an open does not say
  whether a video is live, so most cannot be checked in advance. The reservation is written
  before the request is sent and counts even if the request fails or times out; pending work is
  not replayed after a restart. No retries.
- Opens observed before stage (b) are not followed up.
- serendip uses its own non-admin Yattee Server user for follow-ups and feed posts, never the
  credentials of incoming requests.
- **Deferred** to a later stage: anonymous `/next` (and the vendored parser), second-hop
  related lists, and ranking channels by how many seeds they appeared under.

`gems` is a tag, not a pool: items older than a year with high view counts in any pool get a
small bonus so older videos keep appearing.

- **`POST /api/v1/feed` is not free of side effects.** The server refreshes each channel's
  `last_requested`, schedules avatar fetches, and immediately queues a fetch for channels it has
  not cached (`routers/subscriptions.py:152-160, 182-188`). serendip posts only channels the
  user has posted in the last 30 days, which the server already watches because of the app, at
  most every 6 hours, with `limit` 1000 (the server's maximum; its default is 50,
  `routers/subscriptions.py:65-68`), and records when the answer was truncated. It never posts
  adjacent or exploration channels: the server would poll each one every 6 hours until 14 days
  pass without a post (`feed_fetcher.py:786-792`). Because a post can queue upstream fetches,
  feed posts pause while the breaker is open.
- Related items carry only relative `publishedText` and hard-code `isShort` false
  (`innertube/_converters.py:1093-1100`). serendip derives an approximate upload time from the
  text as the server does (`converters/_formatting.py:64-94`).
- **Filters**: no video opened by the user in the last 90 days; no item with `lengthSeconds` 0
  (live and upcoming); no Shorts, approximated as `lengthSeconds` ≤ 180 unless known otherwise
  (Shorts can run three minutes and related items do not say). Items already shown are
  handled by the refresh rule (section 6.1).
- **Expiry**: `subs` items 30 days after upload, others 30 days after they were found.

## 6. Selection

Per request, from SQLite, target under 100 ms, N = 30.

1. **Eligible set**: all pools merged and deduplicated by video id, filters applied, then
   split by the refresh rule (section 6.1) into carry-over candidates and new items.
2. **Weight** (all factors positive, floors keep cold start working):
   - `interest` = 1 + recency-weighted opens on the item's channel (half-life 30 days), with
     subscribed channels starting at 2 and adjacent channels at 1;
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
5. **Shortage**: if fewer than N items qualify after the fallback in section 6.1, return a
   shorter list. The 90-day exclusion of opened videos is never relaxed.
6. **Impressions**: the chosen list, with each item marked new, carried or re-shown, is written
   in the same transaction that read the eligible set, so the iPhone and the Apple TV cannot be
   served overlapping new items by a race.
7. **Order**: subscriptions and discoveries interleaved.
8. **Response**: a JSON array of `VideoListItem`, the shape of the server's `/api/v1/trending`
   (`models.py:127-150`). Yattee needs `videoId`, `title`, `author`, `authorId`,
   `lengthSeconds` on every item, and one bad item fails the whole list
   (`Services/API/YatteeServerAPI.swift:603-646`), so each item is validated before sending.

### 6.1 Refresh

Every `/popular` request that reaches serendip is a refresh. The app sends one when the browse
view is opened or pulled to refresh, and when the pinned Home section's 30-minute cache has
expired; the two keep separate copies. Home shows only the first items of the list, 5 by
default and adjustable from 1 to 20 in Yattee's Home settings (`Views/Home/HomeView.swift:1354`,
`Views/Home/HomeSettingsView.swift:23, 295`). The rules below are about consecutive lists
serendip serves, not about what a screen happens to show.

A list (N = 30, head H = 5) is built in this order of precedence:

1. **Hard constraints**: no video opened in the last 90 days; only valid items; channel caps;
   no video in more than three consecutive lists on one device.
2. **Head**: positions 1 to H are always videos never served to the user before on any device,
   so the pinned section and the top of the browse view are new on every refresh.
   `SERENDIP_HEAD_SIZE` should match the Home setting.
3. **Carry-over**: from the previous list served on the same device, up to 30% of the returned
   list (configurable within 20-40%; the ceiling is enforced against the actual list size),
   drawn by weight from items not opened since, placed after the head and never at their
   previous position. The lower bound holds only when enough previous items are still
   eligible.
4. **New items** fill the rest: never served to the user on any device. The 40% subscriptions
   to 60% discoveries split is a soft target; when one side runs out, the other fills.
5. **Re-shown items**, served before but not in the previous list, fill in only when new items
   run out, the longest-ago first. Items of the previous list come back only through the
   carry-over allowance. If that is still not enough, the list is shorter.
6. **Repeat**: a request from the same device within 15 s of a list gets that list again,
   minus anything opened since. It records nothing new and does not extend the 15 s.

- **Exposure ledger.** "Served" is tracked per user and video in a durable table (section 9),
  independent of the 30-day detailed impressions and of pool expiry, so a rediscovered video
  never counts as new again.
- A video found both among subscription uploads and in a related list counts as `subs`.
- Selection, the ledger update and the stored list happen in one SQLite transaction that takes
  the write lock before reading eligibility (`BEGIN IMMEDIATE`), so requests from the iPhone
  and the Apple TV at the same moment get disjoint new items. Items are validated before the
  transaction commits.
- **Devices**: "same device" uses the User-Agent mapping (section 4); without it both devices
  share one refresh state. New items never repeat across devices; in a shortage, re-shown items
  can.
- **Capacity**: each refresh uses about 21 never-served videos at N = 30. Supply is the
  subscriptions feed (up to 1000 recent uploads) plus about 19 related items per follow-up (up
  to 20 follow-ups a day), before deduplication and filters. `/status` reports how many
  never-served eligible videos remain, and `serendip_list_items_total{kind}` counts new,
  carried and re-shown items.

Two consecutive lists on one device share at most 40% of their items, and their heads none.

## 7. Upstream budget

Context from the spike: the server itself made 286 `/browse`, 12 `/next` and 8 `/player` calls
in its first hour, all HTTP 200, and its 6-hourly fetcher adds about two `/browse` calls per
subscribed channel per run (66 channels). YouTube is reached from a hosted IP
(a Warsaw hosting network), which is flagged sooner than a residential one. The Q5 measurement
(`docs/SPIKE.md`) is an initial observation of tolerance, not proof of a safe rate.

After this, the server was bot-checked (`docs/SPIKE.md`): no rate from this IP is known to be
safe.

serendip makes no YouTube requests of its own and none at request time. It can cause YouTube
work inside the server in two ways, both behind one breaker:

- **Follow-ups** (section 5.1): extraction jobs, at most 2 in any rolling hour and 20 in any
  rolling day, at least 10 minutes apart, one at a time; jitter never overrides a limit. This
  is a provisional ceiling, not a measured safe rate, and it is not raised to fix a thin pool.
  One job can mean several YouTube requests inside the server.
- **Feed posts**: at most one every 6 hours; a post can queue fetches of uncached channels.

**Breaker**, persistent across restarts, reset only by `serendip breaker reset`
(`serendip breaker trip` stops everything by hand):

- trips on sign-in or bot text in any response, on any 5xx, malformed response, transport
  error or timeout, and on 401, 403 or 429 from the server (logged as an operational stop, not
  as a bot-check);
- a plain 404 (video unavailable) marks that seed failed without tripping it;
- while open: no follow-ups and no feed posts; serving continues from the pool. It logs at
  ERROR, sets `serendip_breaker_open 1` and shows the reason in `/status`.

The server hides many upstream problems behind its fallbacks and still answers 200; those are
not observable from serendip, and the server's log is the place to see them.

**Metrics** (scraped through the ServiceMonitor): `serendip_jobs_total{job,outcome}`,
`serendip_server_requests_total{endpoint,status}`, follow-up latency and the response's
`extractionMethod` as recorded facts (whether a follow-up hit the cache is not measured),
`serendip_breaker_open`, and `serendip_list_items_total{kind}`.

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
| `pool` | user_id, video_id, source (`subs`/`related`/`adjacent`), seed_video_id (first seed only), found_at, expires_at (unique user_id + video_id) |
| `lists` | id, user_id, device, ts_utc, seed, algorithm_version, size |
| `impressions` | list_id, user_id, video_id, ts_utc, device, position, kind (`new`/`carried`/`reshown`) |
| `exposures` | user_id, video_id, first_served, last_served, times_served (unique user_id + video_id; kept as long as the user exists) |
| `followups` | video_id, reserved_at, sent_at, finished_at, status, seconds, extraction_method, outcome |
| `budget`, `breaker` | single-row state |

Indexes cover the exclusion queries (opens by user and time, impressions by user and time).
Retention: events 180 days, impressions and follow-ups 30 days, exposures kept. Migrations run in an
init container as in tuiter. Backups are Kopiur snapshots of the PVC; serendip checkpoints the
WAL every few minutes so a snapshot is consistent enough to restore.

## 10. Failure modes

| Failure | Effect | Handling |
|---|---|---|
| serendip down | Popular fails; the pinned section keeps its cached list, the browse view shows an error | nothing else is affected |
| Pool empty or worker failing | | serve from the stored pool with the same filters; if nothing qualifies, return an empty list. Never a request-time call to YouTube or to the server's `/popular` |
| Bot check or server error | follow-ups and feed posts stop | breaker, manual reset; the stored pool keeps serving |
| Yattee Server down | `subs` cannot refresh | the stored pool keeps serving; auth is local, so `/popular` still answers |
| Follow-ups return no related videos | discoveries dry up | `/status` and metrics show it; lists fall back to subscriptions |
| Mirror lost or delayed | an open is missed | accepted (best effort) |
| Bad item | the app rejects the whole list | validation drops the item before sending |

## 11. Test plan

- **Mirror spike (stage a, done)**: local Envoy with a fake primary and serendip as mirror;
  results in `spikes/mirror/RESULTS.md`.
- **Unit**: event extraction from recorded requests; subscription union and ageing; local
  allowlist with no outgoing request; redaction; response validation.
- **Selection**: deterministic under a seed and snapshot; never returns opened (90 days)
  items; caps hold during the draw; the subscriptions share is a soft target; daypart weighting
  shifts the length mix across many fixed seeds (statistically, not per pair).
- **Refresh matrix**: abundant, scarce and empty pools; short previous and current lists; all
  previous items opened; items at their carry limit; videos in both pools; channel caps that
  prevent the nominal mix. In every case: the head is never-served items; carried items stay
  within 40% of the returned list, are never opened, never keep their position, and no video
  appears in more than three consecutive lists; re-shown items never bypass these limits.
- **History**: prune impressions, expire and rediscover candidates, restart; a previously
  served video is never new again.
- **Concurrency**: simultaneous requests on one device and on two devices; disjoint new items;
  rollback and cancellation leave no phantom impressions; a validation failure records nothing.
- **Repeat**: boundaries around 15 s, an open in between, a fixed (not sliding) window, devices
  without a User-Agent mapping, no extra work.
- **Follow-ups** against a fake server: warm original, failed original, still extracting,
  restart, duplicate opens, the 120 s drop, rolling budgets, a crash after the reservation.
- **Breaker**: bot text in a 404 and in a 200, plain 404, empty or missing related list,
  malformed JSON, 401/403/429, 5xx, timeout; the breaker persists, needs a manual reset, and no
  request is made while it is open.
- **Integration**: FastAPI test client with a fake Yattee Server (respx): mirrored events →
  follow-ups and feed → pool → `/popular`, asserting no request-time HTTP calls and the number
  of server requests.
- **Live (Jan, after deploy)**: Home → browse → Home on both devices; two refreshes a few
  minutes apart; the server log shows no upstream pattern beyond the app's own and serendip's
  follow-ups.

## 12. Stages

- **(a) Observe** (live since 2026-10-12): mirror ingestion, local credential allowlist,
  SQLite, subscriptions union, opens, metrics, `/status`. No outgoing calls.
- **(b) Discover**: follow-ups with budget and breaker, feed ingestion, pool, selection with
  the refresh rule, `/popular`, the `serendip` service user. Home-ops: the `/popular` route to
  serendip and the service user's credentials in the ExternalSecret.
- **(c) Later**, only on evidence from use: anonymous `/next` (`SERENDIP_RELATED_SOURCE=next`),
  second-hop related lists, co-occurrence ranking, search-based exploration, device-specific
  priors, AI.

## 13. Open items

- Whether a current `/next` response offers a continuation for more related items (stage c).
- Jan: custom User-Agent strings per device (optional, needed for per-device refresh state);
  whether Shorts stay out with the 180-second cut-off; list size 30; head size matching the
  Home setting (5 by default).
