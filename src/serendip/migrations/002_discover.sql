-- Stage (b): the candidate pool, served lists, the exposure ledger, follow-ups and the breaker.
-- Timestamps are UTC ISO 8601. Lists, impressions and every reservation that a budget or a
-- spacing is computed from carry microseconds ("2026-10-12T09:30:00.123456Z"), so a boundary is
-- exact; everything else has whole seconds.

-- Metadata for every video serendip has seen in the feed or a related list. Thumbnails are not
-- stored: the response builds the standard set from the video id.
create table videos (
  video_id         text primary key,
  title            text not null,
  author           text not null,
  author_id        text not null,
  length_s         integer not null,
  published_est    text,              -- upload time; for related items estimated from the text
  published_text   text,
  view_count       integer,
  view_count_text  text,
  is_live          integer not null default 0,
  is_upcoming      integer not null default 0,
  is_short         integer,           -- null: unknown
  found_at         text not null
);
create index videos_author on videos (author_id);

create table pool (
  user_id        integer not null references users (id),
  video_id       text not null references videos (video_id),
  source         text not null check (source in ('subs', 'related', 'adjacent')),
  seed_video_id  text,                -- first seed only
  found_at       text not null,
  expires_at     text not null,
  unique (user_id, video_id)
);
create index pool_user_expires on pool (user_id, expires_at);

create table lists (
  id                 integer primary key,
  user_id            integer not null references users (id),
  device             text not null,
  ts_utc             text not null,
  seed               text not null,   -- hex of the 64-bit seed
  algorithm_version  integer not null,
  size               integer not null
);
create index lists_user_device_ts on lists (user_id, device, ts_utc);

create table impressions (
  list_id   integer not null references lists (id) on delete cascade,
  user_id   integer not null references users (id),
  video_id  text not null,
  ts_utc    text not null,
  device    text not null,
  position  integer not null,
  kind      text not null check (kind in ('new', 'carried', 'reshown')),
  primary key (list_id, position)
);
create index impressions_user_ts on impressions (user_id, ts_utc);
create index impressions_user_video on impressions (user_id, video_id);

-- Which videos a user has been served, on any device. Outlives impressions and the pool, so a
-- rediscovered video is never new again.
create table exposures (
  user_id       integer not null references users (id),
  video_id      text not null,
  first_served  text not null,
  last_served   text not null,
  times_served  integer not null,
  times_carried integer not null default 0,
  primary key (user_id, video_id)
);

-- One row per reservation. The rolling budgets, the spacing and the "fetched before" checks
-- all read reserved_at.
create table followups (
  id                 integer primary key,
  video_id           text not null,
  reserved_at        text not null,
  sent_at            text,
  finished_at        text,
  status             text not null,   -- reserved, sent, ok, not_found, tripped, skipped, interrupted
  seconds            real,
  extraction_method  text,
  outcome            text,            -- related items kept, or the trip reason code
  related_count      integer
);
create index followups_video on followups (video_id);
create index followups_reserved on followups (reserved_at);

-- One row per feed call (one user, one chunk of up to 500 channels), written before the POST is
-- sent. The next call, for any user, is due feed_interval_s after the newest started_at.
create table feed_runs (
  id            integer primary key,
  user_id       integer not null references users (id),
  started_at    text not null,
  finished_at   text,
  status        text not null,        -- fetching, ready, failed, tripped, skipped
  chunk_offset  integer not null default 0,
  channels      integer not null default 0,
  items         integer not null default 0,
  truncated     integer not null default 0
);
create index feed_runs_user on feed_runs (user_id, started_at);
create index feed_runs_started on feed_runs (started_at);

-- Single row: where the next feed call starts. Users and their chunks are served round-robin;
-- it moves in the same transaction as the reservation.
create table feed_cursor (
  id            integer primary key check (id = 1),
  user_name     text not null,
  chunk_offset  integer not null
);

-- Single row. open = 1 stops follow-ups and feed posts until `serendip breaker reset`.
create table breaker (
  id         integer primary key check (id = 1),
  open       integer not null default 0,
  kind       text,                    -- bot_check, operational_stop, error or manual
  reason     text,
  tripped_at text,
  initialised integer not null default 0  -- set by the first start (SERENDIP_BREAKER_START_OPEN)
);
insert into breaker (id, open) values (1, 0);

create index opens_user_channel on opens (user_id, channel_id);
