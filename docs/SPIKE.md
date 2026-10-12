# Spike findings (2026-10-12)

Source read: `yattee/yattee` at tag 2.0.0-beta.270 and `yattee/yattee-server` at v1.0.9, the
versions Jan runs. App paths are relative to `Yattee/`, server paths to the repo root. Two scouts
did the first pass; the claims below that the design depends on were re-checked by hand.
The live deployment was then checked read-only (logs, admin API, DNS); see "Live cluster".
Q5 was measured live on 2026-10-12 (see Q5).

## Live cluster (checked 2026-10-12, server deployed 10:54 the same day)

- **Playback is direct today.** The admin API reports `proxy_streaming: true` for YouTube, yet
  a plain `GET /api/v1/videos/{id}` returns direct `googlevideo.com` URLs, and the server log
  has no `/proxy/relay` request since the deploy. Only `?proxy=true` (the app's per-source
  "proxies videos" toggle) turns the adaptive formats into `https://yattee.janpuc.com/proxy/relay?...`.
  Cause not traced; the design must not depend on the server default.
- **The cluster reaches YouTube from a hosted IP** (a Warsaw hosting network, AS208172), not
  Jan's home ISP address. Stream URLs are bound to the cluster's IP (`ip=` parameter), yet a
  range request to one of them from the home ISP address got 206, so direct playback works
  from Jan's devices. A bot-check would hit the cluster, not
  Jan's own browsing, but a hosted IP is more likely to be flagged than a home one.
- **Routing:** at home `yattee.janpuc.com` resolves to `192.168.69.54` (envoy-external on the
  LAN, via `unifi-dns`); outside it resolves to Cloudflare and arrives through
  `cloudflare-tunnel`. With relay streaming on, video bytes stay on the LAN at home and cross
  the Cloudflare tunnel and Jan's upload when he is away.
- **Upstream baseline since the deploy:** 286 InnerTube `/browse` (including the first feed
  build: 66 subscribed channels, fetched at once), 12 `/next`, 8 `/player`, all HTTP 200.
  Anonymous `/player` reports `UNPLAYABLE` (once `LOGIN_REQUIRED`) on every video; the server
  gets streams from yt-dlp instead, which succeeded every time.
- `/api/v1/trending` with the default region returns 15 items, localised by the egress IP
  (Polish channels appear) and sometimes live streams. Live videos come back through the
  yt-dlp-only path with no `recommendedVideos`.

## Q1. Opened, prefetched, watched

**`GET /api/v1/videos/{id}` fires from many places**, not only playback:

| Trigger | Where | Query |
|---|---|---|
| Play (tap on iOS; Play button on tvOS; autoplay; deep link; remote control) | `Services/Player/PlayerService.swift:2137` via `ContentService.swift:278-293` | none, or `?proxy=true&proxy_mode=relay` when the source has "proxies videos" on |
| Info view opens (the **default tvOS tap action**, `Views/Components/TappableVideoModifier.swift:61-67`) | `Views/Video/VideoInfoView.swift:2186` | none |
| Queue preload of the next item when a video starts | `Services/Player/QueueManager.swift:307-405` | same as Play |
| Download sheet opens, downloads, Open Link sheet | `ContentService.swift:259-267`, `DownloadQualitySheet.swift:904`, `OpenLinkSheet.swift:744` | `?proxy=true&proxy_mode=download` |
| Local (downloaded) file playback refreshes metadata | `PlayerService.swift:2791-2812` | none |

- With "proxies videos" off (the default, `Models/Instance.swift:100`), **a play request and an
  info-view request are identical** (`YatteeServerAPI.swift:118-122` vs `246-256`). The info view
  also fires `GET /api/v1/comments/{id}`; on tvOS a play usually follows an info-view GET for
  the same id.
- **Playback bytes bypass the server** unless the app's per-source "proxies videos" toggle
  sends `?proxy=true&proxy_mode=relay`. The source suggests the per-site `proxy_streaming`
  flag also decides this; on the live server it reads `true` but plain requests still get
  direct `googlevideo.com` URLs (see "Live cluster").
- With relay on, MPV (the app's only player, `Services/Player/PlayerBackend.swift:119-132`)
  fetches `https://<host>/proxy/relay?url=<googlevideo url>&sig=&exp=` with HTTP Range
  (`routers/proxy/_relay.py:190-208, 330-506`). A proxy in that path can count bytes delivered
  per stream: the only real **watch-progress** signal available.
- **Nothing about watching is reported to the server.** History and progress are SwiftData
  synced over CloudKit (`Data/DataManager+WatchHistory.swift`).
- Other proxy-visible signals: `POST /api/v1/feed` with the full subscription list
  (`YatteeServerAPI.swift:350-367`; on foreground when the feed cache is stale, on opening
  Subscriptions, on pull-to-refresh, and from background refresh), comments, channel pages,
  search and suggestions, `/api/v1/extract`.
- SponsorBlock, Return YouTube Dislike and DeArrow go to third parties, not to the server.

## Q2. Device signal

- API requests carry a **random desktop-browser User-Agent** (Chrome/Firefox/Safari/Edge on
  Windows/macOS), the same on iOS and tvOS (`Core/UserAgentGenerator.swift`,
  `Services/Networking/HTTPClient.swift:112-117`). No platform header exists.
- The UA is per-device settings state, never synced (`Core/Settings/SettingsManager+General.swift:251-288`).
  Advanced settings lets the user set a custom UA. MPV sends the same UA to stream hosts
  (`Services/Player/MPV/MPVClient.swift:554`).
- Feed and channel-metadata POSTs use a bare `HTTPClient()` and send the CFNetwork default UA
  (`SubscriptionFeedCache.swift:496,563`), which names the app but not the device type.
- **Workable signal:** set a distinct custom UA on the Apple TV and on the iPhone (a realistic
  browser UA with a marker, because MPV also sends it to googlevideo). serendip maps UA to device.

## Q3. A proxy in front

- Detection: the app calls `GET /info` and accepts a Yattee Server when `name` contains
  "yattee" (`Services/API/InstanceDetector.swift:186-233`). Pass-through keeps that.
- Absolute URLs (relay, `/proxy/fast`, captions, thumbnails) are built from the **Host header**
  plus `X-Forwarded-Proto` (`utils.py:6-23`); `X-Forwarded-Host` is ignored and uvicorn runs
  without proxy headers. serendip must forward the client's `Host` and `X-Forwarded-Proto`
  unchanged, so returned URLs point back at the public hostname (and so at serendip).
- Signatures do not bind the host: relay `sig = HMAC(secret, "{url}:{exp}")`, 6 h
  (`routers/proxy/_relay.py:102-132`); stream/caption/thumbnail tokens
  `HMAC(secret, "{user_id}:{video_id}:{expiry}")`, 24 h (`tokens.py:48-72`). The secret lives in
  the server's data dir. A proxy does not affect them.
- The app uses stream URLs verbatim (no host rewrite, relative URLs unsupported,
  `YatteeServerAPI.swift:966-991`), follows API redirects with default URLSession behaviour,
  and sends Basic Auth only on API calls, not on `/proxy/*`, captions or thumbnails (those are
  exempt on the server and use tokens, `basic_auth.py:25-35`).
- `SSRF_EXTRA_ALLOWED_CIDRS` guards only the server's outbound fetches
  (`security.py:117-209`); it cannot affect inbound requests from a proxy.
- Auth: no sessions or cookies; every API request carries `Authorization: Basic`. Successful
  responses carry `X-User-Id` (`basic_auth.py:243-244`). Failed-auth rate limit is per TCP peer
  (`basic_auth.py:170-182`), which is already the Envoy pod today.
- **serendip must not answer `/api/v1/popular` without checking auth**: it has no password
  hashes, so it has to validate the `Authorization` header against the server (and cache
  the verdict briefly).

## Q4. What Jan has to do to get a new list

- Home **section** (Popular/Trending pinned on Home): fetched only from `HomeView`'s `.task`
  when the cached list is older than **30 minutes** (hard-coded,
  `Services/HomeInstanceCache.swift:45-51`; `Views/Home/HomeView.swift:142-146, 1500-1517`).
  Cached on disk across launches; stale list shown on error. Home has **no pull-to-refresh**.
- Home **shortcut card** and the section header (iOS) open `InstanceBrowseView` on the
  Popular/Trending tab (`HomeView.swift:744, 1365`). That view keeps in-memory lists only and
  fetches when it is newly created; a view that still holds data skips the fetch
  (`Views/Instances/InstanceBrowseView.swift:1197-1209`). iOS also has pull-to-refresh there
  (`InstanceBrowseView.swift:240, 331`). Its tvOS branch uses the same `.refreshable`, which
  has no gesture on tvOS; whether going back and opening the card again re-creates the view
  is to be checked on both devices.
- So "new on every refresh" works through the **shortcut card** (both devices) and
  pull-to-refresh (iPhone). The pinned section can change at most every 30 minutes.
- Decoding contract (`YatteeServerAPI.swift:603-646`, decoder uses `convertFromSnakeCase`, so
  the server's camelCase keys decode as they are): required `videoId`, `title`, `author`,
  `authorId`, `lengthSeconds`; everything else optional. A single bad item fails the whole list.

## Q5. Upstream cost and the bot-check budget

**Measured 2026-10-12, 11:55-13:26** with `scripts/spike_related_budget.py` through the public
hostname: 42 cold `GET /api/v1/videos/{id}` lookups on distinct videos, seeded from the
subscriptions feed and walking related lists. Phase A: 12 lookups about 5 minutes apart;
phase B: 30 lookups about 1 minute apart. **All 42 returned 200 with a related list (19-20
items), extraction `hybrid`, 2.5-7.6 s each (mean 3.5 s); no bot-check text, no empty lists.**
Each lookup is the server's heaviest request (InnerTube `/player` + `/next` and a yt-dlp run),
so about 60 an hour of them was tolerated for half an hour from the cluster's hosted IP. This
shows tolerance at that rate on that day, not a safe ceiling. serendip's planned `/next`
budget (3 an hour, 30 a day, one request each) is below a twentieth of it.

Two earlier attempts stopped on their own safety rule (two empty related lists in a row):
`/trending` for the egress IP returned mostly live streams, which the server serves without
related videos. Not a bot-check.


- One cold `GET /api/v1/videos/{id}` = InnerTube `/player` + `/next` in parallel, sometimes a
  second `/player` (TVHTML5, for storyboards), **plus a full yt-dlp extraction** with a PO
  token (`routers/videos.py:238-260`, `innertube/_video.py:38-126`). Cached in-process for 1 h,
  100 entries, lost on restart.
- **No lighter endpoint returns only related videos.** Getting `recommendedVideos` through the
  server costs a full extraction, the request type most likely to trigger a bot-check. A direct
  anonymous InnerTube `/next` call is a single request.
- `/trending`, `/popular`: one InnerTube `/browse` each, not cached. `/search`: one `/search`
  per first page. `POST /feed` answers from local SQLite, but has side effects: it refreshes
  each channel's `last_requested` in `watched_channels`, schedules avatar fetches, and queues
  an immediate fetch for channels not cached yet (`routers/subscriptions.py:152-160,
  182-188`). The 6-hourly fetcher walks every watched channel at one channel per 2 s and drops
  channels not requested for 14 days (`feed_fetcher.py:786-792`).
- Bot-check visibility: `/videos/{id}` returns **404** with the yt-dlp error text in `detail`
  ("Sign in to confirm you're not a bot"); trending/popular/search return **200 `[]`** on
  failure. Upstream throttling is hidden behind fallbacks (the only 429 a client sees is the
  server's own failed-auth limiter). serendip has to read the 404 detail and watch for empty
  lists.
- Response shape for `/trending` and `/popular`: `VideoListItem` (`models.py:127-150`);
  `POST /feed` items are a smaller shape (`routers/subscriptions.py:33-78`).

## Server-side state

Only users; no watch history, subscriptions or progress per user (`migrations/`). No webhooks.
`watched_channels` and `cached_videos` are shared across users.
