"""Send-capacity planner — pure model, offline.

Core arithmetic under test: sustainable new-contact rate ~= total_daily_capacity
/ touch_count, and max_new_contacts reserves T2/T3 slots on future dates (not
just today's cap).
"""
from datetime import date, timedelta

import capacity
from capacity import Mailbox, SequenceSchedule, forecast, max_new_contacts


def _day(start, i):
    return start + timedelta(days=i)


def _mb(name="box1", cap=30):
    return Mailbox(name=name, daily_cap=cap)


# --- Single steady-state mailbox: forecast flat, max new ~ cap / touch_count ---

def test_steady_state_flat_forecast_and_new_contacts():
    seq = SequenceSchedule()  # 3 touches
    mb = _mb(cap=30)
    start = date(2026, 1, 10)
    d = _day(start, 0)
    fx = forecast([mb], seq, [], start, horizon_days=10)
    assert fx[d]["capacity"] == 30
    assert fx[d]["forecast"] == 30  # filled to capacity at sustainable rate
    assert not fx[d]["collision"]
    assert max_new_contacts([mb], seq, [], start, horizon_days=30) == 10


def test_touch_count_drives_steady_state_rate():
    seq2 = SequenceSchedule(offsets=(0, 1))  # 2 touches
    seq3 = SequenceSchedule()  # 3 touches
    mb = _mb(cap=30)
    start = date(2026, 1, 10)
    assert max_new_contacts([mb], seq2, [], start, horizon_days=30) == 15  # 30//2
    assert max_new_contacts([mb], seq3, [], start, horizon_days=30) == 10  # 30//3


# --- Single mailbox mid-warmup-ramp: forecast rises; early day lower ---

def test_mid_warmup_ramp_grows_over_time():
    seq = SequenceSchedule()
    start = date(2026, 1, 1)
    mb = Mailbox(name="ramper", daily_cap=30, ramp_start=start,
                 ramp_days=10, ramp_start_cap=0)
    assert capacity.daily_capacity(mb, _day(start, 0)) == 0
    assert capacity.daily_capacity(mb, _day(start, 5)) == 15  # halfway up
    assert capacity.daily_capacity(mb, _day(start, 10)) == 30  # full cap
    assert max_new_contacts([mb], seq, [], start, horizon_days=12) == 0  # cap 0
    later = _day(start, 5)
    assert max_new_contacts([mb], seq, [], later, horizon_days=12) == 5  # 15//3


# --- Multi-mailbox, mixed states: totals sum; rate uses combined capacity ---

def test_multi_mailbox_mixed_sums():
    seq = SequenceSchedule()
    start = date(2026, 1, 1)
    steady = _mb("steady", cap=30)
    ramper = Mailbox(name="ramper", daily_cap=12, ramp_start=start,
                     ramp_days=4, ramp_start_cap=0)
    mailboxes = [steady, ramper]
    d = _day(start, 2)  # ramper at 50% → 6
    fx = forecast(mailboxes, seq, [], d, horizon_days=5)
    assert fx[d]["capacity"] == 36  # 30 + 6
    assert fx[d]["forecast"] == 36
    assert max_new_contacts(mailboxes, seq, [], d, horizon_days=30) == 12  # 36//3


# --- Collision: burst causes T2s/T3s to collide on future dates ---

def test_collision_warning_fires_on_correct_date():
    seq = SequenceSchedule(offsets=(0, 3, 7))
    start = date(2026, 1, 1)
    mb = _mb(cap=10)
    # 11 contacts added on day 0 with T1 sent → T2 (day 3) and T3 (day 7)
    # each land 11 sends on a 10-cap day.
    burst = [{"added_on": start, "touches_sent": 1}] * 11
    cols = capacity.find_collisions([mb], seq, burst, start, horizon_days=10)
    assert (date(2026, 1, 4), 11, 10) in cols  # T2
    assert (date(2026, 1, 8), 11, 10) in cols  # T3
    fx = forecast([mb], seq, burst, start, horizon_days=10)
    assert fx[date(2026, 1, 4)]["collision"] is True   # T2 day over cap
    assert fx[date(2026, 1, 8)]["collision"] is True   # T3 day over cap
    # Jan 1 (T1) already went out for these contacts (touches_sent=1), so it
    # is NOT a scheduled over-cap day.
    assert fx[date(2026, 1, 1)]["collision"] is False


def test_max_new_contacts_reserves_future_slots():
    """The sustainable rate (cap ÷ touch_count) is further bounded by tight
    future dates: 6 already-committed T2s on day+3 leave only 4 room a date the
    new contacts also hit, so the safe answer is below today's raw cap."""
    seq = SequenceSchedule(offsets=(0, 3, 7))
    start = date(2026, 1, 1)
    mb = _mb(cap=10)
    # 6 contacts already added on day 0 with T1 sent → their T2 lands 6 sends
    # on start+3 (room 4).
    existing = [{"added_on": start, "touches_sent": 1}] * 6
    # Sustainable base = 10 // 3 = 3; the future-date bound is 4, so 3 stands.
    assert max_new_contacts([mb], seq, existing, start, horizon_days=30) == 3
    # A fully-packed future date can force an even lower (0) cap.
    packed = [{"added_on": start, "touches_sent": 1}] * 10  # T2 fills cap on day+3
    assert max_new_contacts([mb], seq, packed, start, horizon_days=30) == 0


# --- Touch-count: variable per sequence is honored ---

def test_variable_touch_count_uses_sequence():
    seq3 = SequenceSchedule(offsets=(0, 2, 5))
    seq5 = SequenceSchedule(offsets=(0, 1, 2, 3, 4))
    start = date(2026, 1, 1)
    mb = _mb(cap=20)
    assert max_new_contacts([mb], seq3, [], start, horizon_days=30) == 6  # 20//3
    assert max_new_contacts([mb], seq5, [], start, horizon_days=30) == 4  # 20//5


# ---------------------------------------------------------------------------
# schedule_by_capacity — the Phase 2 Ship batcher
# ---------------------------------------------------------------------------

def test_schedule_empty_leads_returns_empty():
    seq = SequenceSchedule()
    start = date(2026, 1, 1)
    mb = _mb(cap=30)
    assert capacity.schedule_by_capacity([mb], seq, [], start, []) == []


def test_schedule_basic_respects_sustainable_rate():
    """30 cap / 3 touches → 10/day. 25 leads → [10,10,5] on consecutive days."""
    seq = SequenceSchedule(offsets=(0, 3, 7))
    start = date(2026, 5, 1)
    mb = _mb(cap=30)
    ids = list(range(1, 26))
    batches = capacity.schedule_by_capacity([mb], seq, [], start, ids)
    sizes = [len(b) for _, b in batches]
    assert sizes == [10, 10, 5]
    assert batches[0][0] == start
    assert batches[1][0] == start + timedelta(days=1)
    assert batches[2][0] == start + timedelta(days=2)
    flat = [lid for _, ls in batches for lid in ls]
    assert flat == ids  # input order preserved


def test_schedule_future_room_binding_reduces_today():
    """Existing contacts booked T2s on today's date: 20 contacts added on
    start-3 with touches_sent=1 land T2s today, leaving room=10. Sustainable
    = min(30//3=10, room=10) = 10. But 25 existing contacts land T2s today
    → room=5 → sustainable 5 on the first day."""
    seq = SequenceSchedule(offsets=(0, 3, 7))
    start = date(2026, 5, 1)
    mb = _mb(cap=30)
    # 25 contacts added on start-3 → T2 lands start (start-3+3)
    existing = [{"added_on": start - timedelta(days=3), "touches_sent": 1}] * 25
    ids = list(range(1, 21))
    batches = capacity.schedule_by_capacity([mb], seq, existing, start, ids)
    # Sustainable today = min(10, room_start=5) = 5; after 5 booked, future room
    # on day+3 shrinks: 25+5=30 T2s on day+3, room=0 → sustainable remains 5
    # then advance to day+1 where no conflict → 10
    first_day = [lid for d, ls in batches if d == start for lid in ls]
    assert len(first_day) == 5


def test_schedule_raises_when_volume_impossible():
    """Cap 5 / 5 touches → sustainable 1/day. 400 leads > 365 → ValueError."""
    seq = SequenceSchedule(offsets=(0, 1, 2, 3, 4))
    start = date(2026, 1, 1)
    mb = _mb(cap=5)
    ids = list(range(1, 401))
    try:
        capacity.schedule_by_capacity([mb], seq, [], start, ids)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_schedule_preserves_input_order_in_batch():
    """Output order is deterministic and matches input: ids flow through."""
    seq = SequenceSchedule()
    start = date(2026, 5, 1)
    mb = _mb(cap=10)  # 3/day
    ids = [101, 202, 303, 404, 505]
    batches = capacity.schedule_by_capacity([mb], seq, [], start, ids)
    flat = [lid for _, ls in batches for lid in ls]
    assert flat == ids
