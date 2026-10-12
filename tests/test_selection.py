"""selection.build on synthetic candidates (DESIGN 6, 6.1, 11 'Selection' and 'Refresh matrix')."""

import itertools
import math
import random
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from conftest import channel_id, video_id

from serendip import selection
from serendip.catalog import StoredVideo
from serendip.db import stamp
from serendip.selection import CARRIED, NEW, RESHOWN, Candidate, Params

NOW = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
_counter = itertools.count(1)


def cand(
    n: int | None = None,
    *,
    source: str = "related",
    channel: int | None = None,
    followed: bool = False,
    length: int = 600,
    served: bool = False,
    last_served: str | None = None,
    prev_position: int | None = None,
    carried_before: bool = False,
    in_prev_prev: bool = False,
    boost: float = 0.0,
    shown: bool = False,
    published_days: float = 3,
    views: int | None = None,
) -> Candidate:
    n = n if n is not None else next(_counter) + 100_000
    video = StoredVideo(
        video_id=video_id(n),
        title=f"Video {n}",
        author="A",
        author_id=channel_id(channel if channel is not None else n),
        length_s=length,
        published_est=stamp(NOW - timedelta(days=published_days)),
        published_text=None,
        view_count=views,
        view_count_text=None,
        is_live=False,
        is_upcoming=False,
        is_short=None,
        found_at=stamp(NOW - timedelta(days=1)),
    )
    if served and last_served is None:
        last_served = "2026-10-01T00:00:00.000000Z"
    return Candidate(
        video, source, followed, boost, shown, served, last_served, carried_before,
        prev_position, in_prev_prev,
    )  # fmt: skip


def params(**overrides) -> Params:
    values = {"list_size": 30, "head_size": 5, "carry_max": 0.3, "now": NOW}
    return Params(**{**values, **overrides})


def build(cands, seed=1, **overrides):
    return selection.build(cands, params(**overrides), random.Random(seed))


def kinds(picks):
    return Counter(p.kind for p in picks)


def check_invariants(picks, p: Params | None = None, *, all_candidates=()):
    """The refresh rule (6.1) on any returned list."""
    p = p or params()
    assert [x.position for x in picks] == list(range(1, len(picks) + 1))
    ids = [x.candidate.video_id for x in picks]
    assert len(ids) == len(set(ids))
    assert len(picks) <= p.list_size
    n_new = sum(1 for x in picks if x.kind == NEW)
    head = picks[: min(p.head_size, n_new)]
    assert all(x.kind == NEW for x in head), "head must be never-served items"
    # Nothing but new items before the head ends.
    carried = [x for x in picks if x.kind == CARRIED]
    assert len(carried) <= math.floor(p.carry_max * len(picks))
    for x in carried:
        assert x.position > min(p.head_size, n_new)
        assert x.candidate.prev_position is not None
        assert x.position != x.candidate.prev_position
        assert not x.candidate.carried_before
        assert not x.candidate.in_prev_prev
    # Last resort: nothing new, carried or re-shown was possible, so the previous list comes
    # back as re-shown items in a new order instead of an empty page.
    last_resort = (
        bool(picks)
        and all(x.kind == RESHOWN for x in picks)
        and any(x.candidate.prev_position is not None for x in picks)
    )
    for x in picks:
        if x.kind == NEW:
            assert not x.candidate.served
        if x.kind == RESHOWN:
            assert x.candidate.served
            assert last_resort or x.candidate.prev_position is None
    if last_resort:
        assert not [c for c in all_candidates if not c.served]
        if len(picks) > 1:
            assert all(x.position != x.candidate.prev_position for x in picks)
    if all_candidates:
        assert picks, "never an empty list while servable candidates exist"
    per_channel = Counter(x.candidate.channel for x in picks)
    followed_channels = {x.candidate.channel for x in picks if x.candidate.followed}
    for channel, count in per_channel.items():
        assert count <= (2 if channel in followed_channels else 1)
    picked = {x.candidate.video_id for x in picks}
    if any(x.kind == RESHOWN for x in picks) or len(picks) < p.list_size:
        # Re-shown items, or a short list, only once every unpicked new item is blocked by a cap.
        for c in all_candidates:
            if not c.served and c.video_id not in picked:
                assert per_channel[c.channel] >= (2 if c.followed else 1)


def pool(n_subs: int, n_other: int, *, start: int = 0, **kw) -> list[Candidate]:
    subs = [
        cand(start + i, source="subs", followed=True, channel=start + i, **kw)
        for i in range(n_subs)
    ]
    other = [cand(start + 10_000 + i, channel=start + 10_000 + i, **kw) for i in range(n_other)]
    return subs + other


def test_same_seed_and_snapshot_give_the_same_list():
    cands = pool(40, 60)
    a = build(cands, seed=7)
    assert [x.candidate.video_id for x in a] == [x.candidate.video_id for x in build(cands, seed=7)]
    assert [x.candidate.video_id for x in a] != [x.candidate.video_id for x in build(cands, seed=8)]
    # The candidates' input order does not matter, only the snapshot.
    shuffled = random.Random(0).sample(cands, len(cands))
    assert [x.candidate.video_id for x in build(shuffled, seed=7)] == [
        x.candidate.video_id for x in a
    ]


def test_abundant_pool_fills_the_list_with_new_items_and_a_soft_mix():
    cands = pool(60, 60)
    picks = build(cands)
    check_invariants(picks, all_candidates=cands)
    assert len(picks) == 30
    assert kinds(picks) == {NEW: 30}
    subs = sum(1 for x in picks if x.candidate.source == "subs")
    assert subs == round(0.4 * 30)
    # Interleaved: neither side clumps at the top.
    first = [x.candidate.source == "subs" for x in picks[:10]]
    assert 2 <= sum(first) <= 6


def test_mix_is_soft_when_one_side_runs_out():
    cands = pool(3, 80)
    picks = build(cands)
    check_invariants(picks, all_candidates=cands)
    assert len(picks) == 30
    assert sum(1 for x in picks if x.candidate.source == "subs") == 3
    only_subs = pool(80, 0)
    assert len(build(only_subs)) == 30


def test_channel_caps_hold_while_drawing():
    # Eight videos on a followed channel, eight on an unfollowed one, plus filler.
    cands = [cand(i, source="subs", followed=True, channel=1) for i in range(8)]
    cands += [cand(100 + i, channel=2) for i in range(8)]
    cands += pool(0, 40, start=500)
    picks = build(cands)
    check_invariants(picks, all_candidates=cands)
    by_channel = Counter(x.candidate.channel for x in picks)
    assert by_channel[channel_id(1)] == 2
    assert by_channel[channel_id(2)] == 1


def test_caps_that_prevent_the_nominal_mix_shorten_the_list():
    cands = [cand(i, channel=1) for i in range(20)]  # one unfollowed channel: 1 slot
    picks = build(cands)
    assert len(picks) == 1


def _previous_list(n: int = 30, start: int = 0) -> list[Candidate]:
    return [cand(start + i, channel=start + i, served=True, prev_position=i + 1) for i in range(n)]


def test_carry_over_is_capped_and_never_at_the_previous_position():
    prev = _previous_list()
    fresh = pool(0, 60, start=1000)
    for seed in range(40):
        picks = build(prev + fresh, seed=seed)
        check_invariants(picks, all_candidates=prev + fresh)
        carried = [x for x in picks if x.kind == CARRIED]
        assert len(carried) == 9  # 30% of 30, plenty available
        assert kinds(picks)[NEW] == 21
        assert all(x.position > 5 for x in carried)


def test_items_at_their_carry_limit_or_in_three_lists_are_not_carried():
    prev = [
        cand(i, channel=i, served=True, prev_position=i + 1, carried_before=(i % 2 == 0), in_prev_prev=(i % 3 == 0))
        for i in range(30)
    ]  # fmt: skip
    fresh = pool(0, 60, start=1000)
    picks = build(prev + fresh)
    check_invariants(picks, all_candidates=prev + fresh)
    for x in picks:
        if x.kind == CARRIED:
            assert x.candidate.video_id not in {video_id(i) for i in range(0, 30, 2)}
            assert x.candidate.video_id not in {video_id(i) for i in range(0, 30, 3)}


def test_only_the_previous_list_left_is_shown_again_in_a_new_order():
    # Only the previous list and nothing new: the carry ceiling of an empty remainder is zero, so
    # the last resort re-shows the previous list in a new order rather than an empty page.
    prev = _previous_list()
    picks = build(prev)
    check_invariants(picks, all_candidates=prev)
    assert picks and set(kinds(picks)) == {RESHOWN}
    assert all(x.position != x.candidate.prev_position for x in picks)


def test_scarce_pool_carry_stays_within_the_ceiling_of_the_returned_list():
    prev = _previous_list()
    fresh = pool(0, 5, start=1000)
    for seed in range(20):
        picks = build(prev + fresh, seed=seed)
        check_invariants(picks, all_candidates=prev + fresh)
        carried = kinds(picks)[CARRIED]
        assert carried == math.floor(0.3 * len(picks)) == 2
        assert len(picks) == 7


def test_reshown_items_fill_only_when_new_run_out_oldest_first():
    served = [
        cand(i, channel=i, served=True, last_served=f"2026-09-{i + 1:02d}T00:00:00.000000Z")
        for i in range(20)
    ]
    fresh = pool(0, 10, start=1000)
    picks = build(served + fresh)
    check_invariants(picks, all_candidates=served + fresh)
    assert kinds(picks)[NEW] == 10
    reshown = [x.candidate.video_id for x in picks if x.kind == RESHOWN]
    assert len(reshown) == 20
    assert set(reshown) == {video_id(i) for i in range(20)}
    # Oldest first: with room for fewer, the oldest are taken.
    picks = build(served + pool(0, 24, start=1000))
    reshown = {x.candidate.video_id for x in picks if x.kind == RESHOWN}
    assert reshown == {video_id(i) for i in range(6)}


def test_reshown_items_respect_caps_and_exclude_the_previous_list():
    served = [cand(i, channel=1, served=True) for i in range(10)]  # one channel
    prev = _previous_list(10, start=100)
    picks = build(served + prev)
    check_invariants(picks, all_candidates=served + prev)
    assert [x.kind for x in picks].count(RESHOWN) == 1


def test_head_is_never_served_even_when_few_new_items_exist():
    served = [cand(i, channel=i, served=True) for i in range(20)]
    fresh = pool(0, 3, start=1000)
    picks = build(served + fresh)
    assert [x.kind for x in picks[:3]] == [NEW, NEW, NEW]
    check_invariants(picks, all_candidates=served + fresh)


@pytest.mark.parametrize("n_prev", [0, 1, 4, 12, 30])
@pytest.mark.parametrize("n_new", [0, 2, 5, 9, 25, 80])
@pytest.mark.parametrize("n_served", [0, 15])
def test_refresh_matrix(n_prev, n_new, n_served):
    prev = _previous_list(n_prev, start=0)
    served = [cand(500 + i, channel=500 + i, served=True) for i in range(n_served)]
    fresh = pool(n_new // 3, n_new - n_new // 3, start=2000)
    for seed in range(10):
        picks = build(prev + served + fresh, seed=seed)
        check_invariants(picks, all_candidates=prev + served + fresh)
        if n_new >= 30:
            assert len(picks) == 30


def test_all_previous_items_opened_means_no_carry():
    # Opened items are filtered out before selection; what is left has no previous list.
    picks = build(pool(0, 60))
    assert kinds(picks)[CARRIED] == 0


def test_no_carry_when_carry_max_is_zero():
    prev = _previous_list()
    picks = build(prev + pool(0, 60, start=1000), carry_max=0.0)
    assert kinds(picks) == {NEW: 30}


def test_carry_never_lands_on_its_previous_position_in_a_tiny_list():
    # Head 1, one carried item whose previous position is the only slot left: it is dropped.
    prev = [cand(1, channel=1, served=True, prev_position=2)]
    fresh = [cand(2, channel=2)]
    for seed in range(20):
        picks = build(prev + fresh, seed=seed, carry_max=0.5, head_size=1)
        check_invariants(picks, params(carry_max=0.5, head_size=1))


def test_weights_are_positive_and_follow_the_factors():
    w = lambda c: selection.weight(c, NOW)  # noqa: E731
    assert w(cand(2, followed=True)) == pytest.approx(2 * w(cand(3)))
    assert w(cand(4, boost=1.0)) == pytest.approx(2 * w(cand(5)))
    assert w(cand(6, shown=True)) == pytest.approx(0.6 * w(cand(7)))
    fresh = cand(8, source="subs", published_days=0)
    old = cand(9, source="subs", published_days=60)
    assert w(fresh) > w(old)
    assert w(old) / w(fresh) >= 0.3
    assert w(cand(11, published_days=400, views=2_000_000)) == pytest.approx(
        1.3 * w(cand(12, published_days=400))
    )
    assert all(w(cand(i, length=l)) > 0 for i, l in enumerate((200, 600, 3000)))  # noqa: E741


def test_freshness_applies_to_subs_only():
    w = lambda c: selection.weight(c, NOW)  # noqa: E731
    assert w(cand(1, published_days=200)) == pytest.approx(w(cand(2, published_days=1)))
    assert w(cand(3, source="subs", published_days=200)) < w(
        cand(4, source="subs", published_days=1)
    )


@pytest.mark.parametrize(
    "hour, name",
    [(5, "night"), (6, "morning"), (11, "morning"), (12, "afternoon"), (17, "afternoon"),
     (18, "evening"), (22, "evening"), (23, "night"), (0, "night")],
)  # fmt: skip
def test_dayparts(hour, name):
    assert selection.daypart(hour) == name


def _long_share(hour: int, seeds=range(300)) -> float:
    now = NOW.replace(hour=hour)
    cands = [cand(i, channel=i, length=300) for i in range(30)]  # five minutes
    cands += [cand(100 + i, channel=100 + i, length=3000) for i in range(30)]  # fifty minutes
    long_picks = total = 0
    for seed in seeds:
        picks = selection.build(
            cands,
            Params(10, 0, 0.0, now),
            random.Random(seed),
        )
        long_picks += sum(1 for x in picks if x.candidate.video.length_s >= 900)
        total += len(picks)
    return long_picks / total


def test_daypart_weighting_shifts_the_length_mix_statistically():
    morning, afternoon, evening = _long_share(8), _long_share(14), _long_share(20)
    assert evening > afternoon > morning
    assert evening - morning > 0.15
    assert 0.35 < morning < 0.5 < evening < 0.65  # modest, not a hard filter


def test_novelty_penalty_lowers_a_recently_shown_channel():
    def share(shown: bool) -> float:
        hits = 0
        for seed in range(300):
            cands = [cand(i, channel=i, shown=(shown and i < 20)) for i in range(40)]
            picks = selection.build(cands, Params(10, 0, 0.0, NOW), random.Random(seed))
            hits += sum(1 for x in picks if int(x.candidate.video_id[1:]) < 20)
        return hits / (300 * 10)

    assert share(True) < share(False) - 0.05


def test_followed_channels_and_opens_raise_the_chance():
    def share(**kw) -> float:
        hits = 0
        for seed in range(300):
            cands = [cand(i, channel=i, **(kw if i < 20 else {})) for i in range(40)]
            picks = selection.build(cands, Params(10, 0, 0.0, NOW), random.Random(seed))
            hits += sum(1 for x in picks if int(x.candidate.video_id[1:]) < 20)
        return hits / (300 * 10)

    assert share(followed=True, source="related") > share() + 0.1
    assert share(boost=3.0) > share() + 0.1
