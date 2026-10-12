# serendip: brief for the agent building it

You are the lead agent on serendip. Jan talks to you; you plan, delegate, check and report.
Read this whole file, then `AGENTS.md`, before doing anything.

## What Jan wants

Jan replaced YouTube with Yattee 2 (iPhone and Apple TV, TestFlight) backed by a self-hosted
Yattee Server in his Kubernetes cluster. He lost a front page. YouTube's own front page was
irritating: refreshing it showed the same videos over and over. He wants a **discover feed**
that:

- shows **something new on every refresh**;
- is **a mix**: new uploads from his subscriptions, related videos, channels he does not follow
  yet, older gems; whatever ratio makes sense;
- is **not limited to his subscriptions**: it fetches new material from YouTube, steered by his
  subscriptions, his watch history, **time of day**, and other factors you find useful
  (device used is a nice-to-have: Apple TV vs iPhone sessions likely want different lengths);
- needs **no Google account** and does not use YouTube's personalised algorithm;
- may use AI (cluster models or an API) **only if it measurably helps**. Jan is unsure it is
  needed. Decide with evidence, not by default.

Web UI is not needed. The surface is Yattee's Home screen.

## What is already verified (2026-10-12)

Check anything you rely on again; these are starting points, not guarantees.

**Yattee Server** (`yattee/yattee-server`, v1.0.9, Python/FastAPI, MIT)
- Deployed in Jan's cluster: namespace `media`, HelmRelease `yattee-server`, Service
  `yattee-server.media.svc.cluster.local:8085`, public route `https://yattee.janpuc.com`
  (Cloudflare -> `envoy-external`). Manifests: `~/Development/home-ops/kubernetes/apps/media/yattee-server/`.
- HTTP Basic Auth on everything except `/health` (and minimal `/info`). Admin credentials live
  in the pod env (`ADMIN_USERNAME`, `ADMIN_PASSWORD`) from 1Password item `yattee`. Stream,
  thumbnail and caption URLs use an HMAC `token` query parameter instead.
- No Invidious backend is configured, yet `/api/v1/trending`, `/popular`,
  `/search/suggestions` and `/comments/{id}` all returned data (InnerTube fallback).
- `GET /api/v1/videos/{id}` returns an Invidious-shaped object with `recommendedVideos`
  (19 items for `dQw4w9WgXcQ`), fetched anonymously via InnerTube `/next`. Parsing lives in
  `innertube/_converters.py` (`_recommended_videos_from_next`) with regression tests in
  `tests/test_innertube_recommended_videos.py`.
- It keeps proxied video files for `proxy_download_max_age` (default 24 h) and runs a feed
  fetcher for subscribed channels every 6 h.
- API reference: `docs/api.md` in the repo. Read the source, not only the docs; the docs said
  trending needs Invidious and it does not.

**Yattee 2 app** (`yattee/yattee`, beta.270)
- `Yattee/Models/Instance.swift`: `supportsPopular` is true for `.invidious` and
  `.yatteeServer`. Home can pin a per-source **Feed / Popular / Trending** section
  (`Yattee/Views/Home/HomeItem.swift`, `InstanceContentType`).
- `Yattee/Services/API/YatteeServerAPI.swift`: trending = `GET /api/v1/trending`, popular =
  `GET /api/v1/popular`, both decoded as `[YatteeVideo]`. Subscriptions feed =
  `POST /api/v1/feed` with the **subscribed channel list in the body**. Video open =
  `GET /api/v1/videos/{id}` (called from several places; see open question 1).
- `Yattee/Services/HomeInstanceCache.swift`: Home content is cached for 30 minutes and
  refreshed from the network; on error the stale cache is shown.
- Subscriptions are stored in the app (account "Local"), synced over iCloud, not on the server.
  Watch history and resume position are app-side too.

**Research already done** (no need to repeat; extend if you find more)
- No existing project gives an account-free, self-hosted discover feed usable on Apple TV.
  Closest: YourTube (`filyp/YourTube`, related-video graph clustering, needs a Google account,
  web UI), YT Zero (subscriptions only), NewPipe issue #2256 and the `aberja/newpipe` fork
  (random picks from subscriptions), Yattee issue #32 (open since 2021).
- Anonymous InnerTube `/next` related lists work in 2026 but are generic, not personalised.
  YouTube removed the public "Channels" tab, so there is no clean channel-similarity source;
  co-occurrence in related lists is the practical substitute.

**AI available to you, if it earns its place**
- LiteLLM (OpenAI-compatible) in cluster: `http://litellm.ai.svc.cluster.local:4000`, public
  `https://litellm.janpuc.com`. Model configs: `~/Development/home-ops/kubernetes/apps/ai/litellm/app/models/`.
- llmkube serves `bge-embedding-m3` and `bge-reranker-v2-m3` on the cluster GPU
  (`~/Development/home-ops/kubernetes/apps/ai/llmkube/models/`). Check how they are exposed
  (direct service or through LiteLLM) before designing around them.
- Plausible uses to evaluate: title/description embeddings for topic diversity and
  "more like what I finish" scoring; reranking a candidate pool; an LLM to tag topics. Plain
  heuristics (co-occurrence, freshness, channel caps, weighted random sampling) may be enough.

## Proposed shape (challenge it)

serendip is a small service that **sits in front of Yattee Server as a transparent reverse
proxy**. Yattee's source URL points at serendip; serendip forwards everything to Yattee Server
unchanged, except that it **answers `/api/v1/popular` (and/or `/api/v1/trending`) itself**.
Because every request passes through it, it can record, per Basic-Auth user:

- subscriptions (from the `POST /api/v1/feed` body),
- video opens and, if streams go through `/proxy/...`, how much of each video was actually
  fetched (a proxy for watch completion),
- time of day, and device (User-Agent, if Yattee sends a distinguishable one).

A background job builds a candidate pool from: new uploads of subscribed channels; related
videos of recently watched/finished videos (via Yattee Server, so you inherit its InnerTube
parsing and its upstream fixes); channels that co-occur with liked channels; search for topics
the user watches. Each request then **samples** from the pool (weighted random, not a fixed
rank) with novelty filters (never re-show watched; decay items already shown), per-channel
caps, a freshness bonus, a time-of-day/device bias, and a guaranteed mix of buckets
(e.g. subscriptions / adjacent / exploration). Storage: SQLite on a PVC.

Hard constraints:
- **Do not get the cluster IP blocked by YouTube.** Budget and rate-limit every upstream call,
  cache aggressively, precompute in the background; a Home request must be served from the
  local pool in well under a second. Prefer calling Yattee Server over hitting YouTube directly.
- Must degrade gracefully: if candidate building fails, fall back to the subscriptions feed;
  never break playback or the other proxied endpoints.
- No Google account, no cookies, no secrets in the repo. Data stays in the cluster.
- Responses must decode as Yattee's `YatteeVideo`; mirror Yattee Server's `/api/v1/trending`
  shape exactly.

## Open questions to answer first (spike before design)

1. Which requests mean "opened" vs "prefetched" vs "actually watched"? Find every call site of
   `/api/v1/videos/{id}` in the app, and whether playback goes through Yattee Server's
   `/proxy/fast` or `/proxy/relay` (byte ranges = watch progress) or directly to googlevideo.
2. Does Yattee send a User-Agent that distinguishes tvOS from iOS?
3. How does Yattee behave if a proxy sits in front: `/info` detection, HMAC-signed stream
   URLs (signed with which host?), redirects, the `SSRF_EXTRA_ALLOWED_CIDRS` guard.
4. Pull-to-refresh vs the 30-minute Home cache: what does Jan actually have to do to get a
   new list, on iPhone and on Apple TV?
5. How many related-video lookups per hour can the cluster IP do through Yattee Server
   without "Sign in to confirm you're not a bot"? Measure carefully and stop at the first sign
   of trouble.

## How to work

Follow `~/.pi/agent/AGENTS.md` (delegation, model levels, `usage_status` first). In short:

1. **Spike** (open questions above): delegate to `scout` agents at the Easy/Normal level, one
   or two at a time, each with a bounded question, source paths and what to return. Check their
   claims against the source before using them.
2. **Design**: write `docs/DESIGN.md` (architecture, signals, scoring, buckets, data model,
   upstream budget, AI decision with reasons, failure modes, test plan). Then **ask the
   advisor**: `oracle` on `openai/gpt-6-astra:high` with a written brief and the file paths.
   Relay its advice to Jan with your own view, and **wait for Jan's go-ahead** before building.
3. **Build in stages**, each one a `worker` task with tests to pass: (a) transparent proxy +
   signal logging, deployable on its own; (b) candidate pool + sampler; (c) tuning/AI if the
   design calls for it. After each stage a `reviewer` (Sol for anything touching auth or
   proxying of credentials) and a short report to Jan: what changed, what was verified.
4. **Package**: Python 3.12, uv, FastAPI/httpx, ruff, pytest (see `~/Development/tuiter` for
   the house style). Dockerfile, GitHub Actions CI, release-please and a ghcr.io image
   (`ghcr.io/janpuc/serendip`), Renovate (see `~/Development/browserr/.github` and
   `.renovaterc.json5`).
5. **Deploy**: prepare, do not apply. Write the home-ops changes (app-template HelmRelease in
   `media`, Kopiur-backed PVC like `yattee-server`, Service; route Yattee traffic to serendip,
   either a new hostname or by repointing `yattee.janpuc.com`) on a branch in
   `~/Development/home-ops` and hand Jan the diff. Committing/pushing home-ops, and any
   kubectl/flux action, are Jan's or the main session's call after Jan says yes.

## Done means

- On Jan's iPhone and Apple TV, a pinned Home section from serendip shows a list in which
  most items differ between two refreshes a few minutes apart, mixes subscriptions with
  channels he does not follow, never shows something he finished, and visibly shifts with
  time of day.
- Playback, search, channels and the subscriptions feed through serendip behave exactly as
  they did directly against Yattee Server.
- Upstream call volume is bounded and visible (metrics or logs), and no bot-check errors
  appear in a week of normal use.
- Tests cover the proxy passthrough, signal extraction and the sampler (determinism under a
  seed, novelty, caps, bucket mix).
