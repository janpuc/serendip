-- Stage (a): what the mirror shows. All timestamps are UTC ISO 8601 ("2026-10-12T09:30:00Z").

-- One row per name in SERENDIP_USERS that has sent an event; id is internal.
create table users (
  id          integer primary key,
  name        text unique not null,
  first_seen  text not null
);

create table events (
  id        integer primary key,
  user_id   integer not null references users (id),
  ts_utc    text not null,
  device    text not null,           -- 'tv', 'phone' or 'unknown', from the User-Agent
  kind      text not null check (kind in ('open', 'subscriptions_seen', 'search')),
  video_id  text,
  query     text                     -- open: proxy flags only; search: q, 200 chars
);
create index events_user_ts on events (user_id, ts_utc);

-- A union of every channel seen in a mirrored post. Missing channels are not unsubscribes.
create table subscriptions (
  user_id            integer not null references users (id),
  channel_id         text not null,
  name               text,
  first_seen         text not null,
  last_seen_by_user  text not null,
  primary key (user_id, channel_id)
);

create table opens (
  user_id        integer not null references users (id),
  video_id       text not null,
  channel_id     text,               -- filled in by stage (b)
  last_open_utc  text not null,
  count          integer not null default 1,
  unique (user_id, video_id)
);
create index opens_user_last on opens (user_id, last_open_utc);
