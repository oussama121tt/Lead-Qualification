"""Send-capacity planner — pure, offline-testable model (Task 14 / 3.4).

Owns the calendar arithmetic independently of the route/view layer and of the
(real, still single-account) Gmail sender. It answers:

  * sends/day forecast 30 days out, across mailboxes, ramp-aware
  * "you can add N contacts on date D" — max NEW contacts whose future
    follow-ups still fit capacity on every date they'll land on
  * collision warnings — dates where already-scheduled follow-ups exceed cap
  * ramp schedule for a new mailbox (day-by-day capacity rising to full cap)

Core arithmetic (the easy-to-get-wrong bit):
    sustainable new-contact rate on date D ~= total_daily_capacity(D) / touch_count

because every new contact books `touch_count` send slots (T1, T2, T3 by
default) spread across future dates. Crucially, a contact added on D reserves
its T2/T3 slots on *future* dates too — so max_new_contacts must satisfy the
capacity bound on EVERY future date the follow-ups land on, not just today.

The model is deliberately pure: it takes plain `Mailbox`/`SequenceSchedule`
objects and a `scheduled` snapshot (already-added contacts + how many touches
each has completed), and returns plain numbers/dicts. It has no DB or web
dependencies, so it is unit-tested without spinning up the app. Callers
(route/view) convert DB rows into this shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Mailbox:
    name: str
    daily_cap: int
    # Warmup ramp: a new mailbox starts at `ramp_start_cap` on `ramp_start` and
    # rises linearly to `daily_cap` over `ramp_days`. After ramp_days it is at
    # full cap. A mailbox with ramp_start_cap == daily_cap (or ramp_days == 0)
    # is steady-state (no warmup).
    ramp_start: date | None = None
    ramp_days: int = 0
    ramp_start_cap: int = 0


@dataclass(frozen=True)
class SequenceSchedule:
    """Offsets in days from a contact's add date at which each touch goes out.
    len(offsets) is the touch count. Default: 3 touches (T1 @0, T2 @3, T3 @7)."""
    offsets: tuple[int, ...] = (0, 3, 7)

    @property
    def touch_count(self) -> int:
        return len(self.offsets)


def daily_capacity(mb: Mailbox, d: date) -> int:
    """Ramp-aware daily cap for a mailbox on date `d`."""
    if mb.ramp_days <= 0 or mb.ramp_start is None:
        return mb.daily_cap
    if d < mb.ramp_start:
        # Not yet started ramping → still at its initial cap.
        return mb.ramp_start_cap
    elapsed = (d - mb.ramp_start).days
    if elapsed >= mb.ramp_days:
        return mb.daily_cap
    # Linear ramp from ramp_start_cap up to daily_cap over ramp_days.
    span = mb.daily_cap - mb.ramp_start_cap
    return mb.ramp_start_cap + int(span * elapsed / mb.ramp_days)


def total_capacity(mailboxes: list[Mailbox], d: date) -> int:
    return sum(daily_capacity(mb, d) for mb in mailboxes)


def ramp_schedule(mb: Mailbox, start: date, horizon_days: int) -> list[tuple[date, int]]:
    """Day-by-day capacity ramp for a (new) mailbox, `horizon_days` out."""
    return [(start + timedelta(days=i), daily_capacity(mb, start + timedelta(days=i)))
            for i in range(horizon_days)]


def scheduled_sends(contacts, seq: SequenceSchedule, d: date) -> int:
    """Number of already-committed sends landing on date `d`.

    `contacts` is a snapshot of already-added contacts, each with
    `added_on` (a date) and `touches_sent` (how many of the sequence's touches
    have already completed). The still-pending touches that fall on `d` are
    the scheduled sends for that day.
    """
    n = 0
    for c in contacts:
        added = c["added_on"]
        done = c.get("touches_sent", 0)
        for idx in range(done, seq.touch_count):
            landing = added + timedelta(days=seq.offsets[idx])
            if landing == d:
                n += 1
    return n


def forecast(mailboxes: list[Mailbox], seq: SequenceSchedule, contacts,
             start: date, horizon_days: int) -> dict[date, dict]:
    """Sends/day forecast 30 days out.

    For each date in [start, start + horizon): total capacity (ramp-aware)
    vs. already-scheduled follow-ups. When there is headroom we assume new
    contacts are added at the sustainable rate (filling to capacity); when
    already-scheduled commits exceed capacity the date is flagged as a
    collision and the forecast shows the over-committed send count.

    Returns {date: {"capacity", "scheduled", "forecast", "collision"}}.
    """
    out: dict[date, dict] = {}
    for i in range(horizon_days):
        d = start + timedelta(days=i)
        cap = total_capacity(mailboxes, d)
        sched = scheduled_sends(contacts, seq, d)
        # Add new contacts up to the sustainable rate that still fits today.
        sustainable_new = max(0, (cap - sched) // seq.touch_count)
        forecast_sends = sched + sustainable_new * seq.touch_count
        out[d] = {
            "capacity": cap,
            "scheduled": sched,
            "forecast": forecast_sends,
            "collision": sched > cap,
        }
    return out


def max_new_contacts(mailboxes: list[Mailbox], seq: SequenceSchedule, contacts,
                     target: date, horizon_days: int) -> int:
    """Max NEW contacts that can be added on `target` — the sustainable rate.

    Core arithmetic: sustainable new-contact rate ~= total_daily_capacity /
    touch_count, because every new contact books `touch_count` send slots
    (T1, T2, T3 by default) over the sequence's future dates.

    We additionally reserve future slots: the answer is capped by how much
    room each future date actually has once already-scheduled follow-ups are
    counted — so a burst of contacts added earlier (whose T2/T3 collide on a
    future date) reduces how many more can be safely added today.
    """
    if not mailboxes:
        return 0
    if seq.touch_count == 0:
        return 0
    base = total_capacity(mailboxes, target) // seq.touch_count

    # On each date the new contacts' touches land, bound the additions so the
    # combined (already-scheduled + new) send count stays within capacity.
    touches_on_day: dict[date, int] = {}
    for off in seq.offsets:
        d = target + timedelta(days=off)
        touches_on_day[d] = touches_on_day.get(d, 0) + 1

    for d, foot in touches_on_day.items():
        if foot == 0:
            continue
        room = total_capacity(mailboxes, d) - scheduled_sends(contacts, seq, d)
        base = min(base, room // foot)
    return max(0, base)


def schedule_by_capacity(mailboxes: list[Mailbox], seq: SequenceSchedule, contacts,
                         start: date, lead_ids: list) -> list[list]:
    """Greedily spread `lead_ids` across the earliest dates that fit capacity.

    Phase 2 Ship: the review queue's approved leads are batched onto concrete
    send dates so each produced batch's size on date D never exceeds
    `max_new_contacts` for D given the contacts already committed — including
    earlier batches from THIS ship (their T2/T3 follow-ups are reserved the
    instant a lead is assigned). Leads that spawn follow-ups on dates past
    `start + 365` will never fit and raise ValueError.

    `contacts` is the existing global snapshot (`{"added_on": date,
    "touches_sent": int}`); assigned leads are appended the same way but with
    touches_sent=1 (touch 1 scheduled for its add date). Every added contact is
    counted as a commit so a later batch's sustainable rate sees it.

    Returns a list of batches: `[[date, [lead_id, ...]], ...]` ordered by date,
    preserving input order within each day. Pure (no DB), same shape contract as
    the rest of this module.
    """
    if not mailboxes or not lead_ids:
        return []
    batches: list[list] = []          # [[date, [ids]], ...]
    booked = [dict(c) for c in contacts]
    day = start
    guard = 0
    for lid in lead_ids:
        while True:
            sustainable = max_new_contacts(mailboxes, seq, booked, day, horizon_days=365)
            today_count = sum(1 for c in booked if c["added_on"] == day)
            if today_count < sustainable:
                booked.append({"added_on": day, "touches_sent": 1})
                if batches and batches[-1][0] == day:
                    batches[-1][1].append(lid)
                else:
                    batches.append([day, [lid]])
                break
            day += timedelta(days=1)
            guard += 1
            if guard > 365:
                raise ValueError(
                    "campaign lead volume does not fit sending capacity within a year"
                )
    return batches


def find_collisions(mailboxes: list[Mailbox], seq: SequenceSchedule, contacts,
                    start: date, horizon_days: int) -> list[tuple[date, int, int]]:
    """Dates within the horizon where already-scheduled follow-ups exceed cap.

    Returns [(date, scheduled_sends, capacity)] for each over-committed day —
    e.g. a burst of contacts added earlier whose T2/T3 land on the same day.
    """
    cols = []
    for i in range(horizon_days):
        d = start + timedelta(days=i)
        sched = scheduled_sends(contacts, seq, d)
        cap = total_capacity(mailboxes, d)
        if sched > cap:
            cols.append((d, sched, cap))
    return cols
