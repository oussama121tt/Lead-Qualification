"""LinkedIn founder-profile lane: full profile + attributed posts harvest.

Ported from the proven lead_tool enrichment project (pipeline/linkedin_profile,
linkedin_harvest, linkedin_attribution, linkedin_extract) and adapted to feed
this system's scorer as `person_linkedin` web evidence.

Why this replaces the old search-snippet approach for the founder:
- Full authored post text is where "built this with Cursor in a weekend"
  confessions actually live — search snippets truncate exactly that.
- Attribution is done in CODE, never by an LLM: a post is AUTHORED iff its
  permalink handle matches the profile owner (or "shared by <owner>").
  Everything else is LIKED with the original author recorded — so the scorer
  can treat authored posts as first-party STRONG evidence and liked posts as
  weak association, instead of guessing.
- Junk (auth-walls, bare URLs, bare timestamps) is regex-dropped before it
  can pollute the prompt.

Operational discipline (the part the old flow lacked entirely):
- SEQUENTIAL: one profile at a time process-wide (module lock), with
  human-mimicking randomized pacing between profiles (throttle.Pacer).
- CAPPED: global daily/weekly caps persisted in the DB (caps.py); a failed
  scrape never consumes the cap. When capped, the caller gets a clear
  "capped" result and a coverage note — never a silent skip.
- KEY-ROTATED: SGAI keys through keyring.MemoryKeyRing with distinct
  handling of 429 (brief cool, retry same key) vs 401/402/403 (long cool).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

import requests as _requests

import caps as capsmod
from keyring import AllKeysExhausted, MemoryKeyRing, QUOTA_COOL, RATE_COOL
from throttle import Pacer, sleep_interruptible

_SGAI_API = "https://v2-api.scrapegraphai.com/api"

FETCH_PROFILE = {"mode": "auto", "stealth": True, "scrolls": 5, "wait": 3000}
FORMATS_PROFILE = [{"type": "markdown"}, {"type": "links"}]
FETCH_POST = {"mode": "auto", "stealth": True, "wait": 3000}
FORMATS_POST = [{"type": "markdown"}]

_AUTHWALL = re.compile(r"join linkedin|sign in to view|authwall", re.I)
_HANDLE = re.compile(r"/posts/([^_/?]+)")

# Self-introduction / origin-story markers. If an authored post matches, it is
# force-kept past the recency cap (highest-value "who I am" content).
BIO_PATTERNS = re.compile(
    r"here'?s who i am|my story|a bit about me|about me\b|i'?m originally from|"
    r"originally from|my background|my journey|founder story|our story|"
    r"\bfollowers\b|i started this|why i (?:started|founded|built)|"
    r"a little about (?:me|myself)|introduce myself|📌|pinned|featured",
    re.I,
)

# Scraping artifacts that are NOT real post content — dropped from output.
_JUNK_TEXT = re.compile(
    r"signup blocking page|sign\s?up blocking page|there is a sign|"
    r"sign in to view|join linkedin|authwall|please enable javascript",
    re.I)
_BARE_TS = re.compile(r"^\s*\d{4}-\d{2}-\d{2}t\d{2}:\d{2}", re.I)   # bare timestamp
_BARE_URL = re.compile(r"^\s*https?://\S+\s*$", re.I)               # text is just a URL

AUTHOR_KEYS = ("user_id", "author_id", "author_handle", "authorUrn", "actor_id",
               "author", "actor", "profile_id", "owner_id")

# One profile at a time process-wide — LinkedIn pacing is only meaningful
# when the lane is sequential, even while the rest of the pipeline runs
# leads in parallel.
_lane_lock = threading.Lock()
_pacer: Pacer | None = None
_pacer_lock = threading.Lock()

_ring: MemoryKeyRing | None = None
_ring_lock = threading.Lock()


def _get_ring() -> MemoryKeyRing | None:
    global _ring
    with _ring_lock:
        if _ring is None:
            keys = []
            for k in ("SGAI_API_KEY", "SGAI_API_KEY_2", "SGAI_API_KEY_3",
                      "SGAI_API_KEY_4", "SGAI_API_KEY_5"):
                val = os.getenv(k)
                if val:
                    keys.append(val)
            multi = os.getenv("SCRAPE_API_KEYS", "").strip()
            if multi:
                keys = [k.strip() for k in multi.split(",") if k.strip()] or keys
            _ring = MemoryKeyRing(keys) if keys else None
        return _ring


def _get_pacer(cfg_linkedin) -> Pacer:
    global _pacer
    with _pacer_lock:
        if _pacer is None:
            _pacer = Pacer(cfg_linkedin)
        return _pacer


def _sgai_scrape(url: str, formats: list, fetch_config: dict,
                 timeout: float = 120.0, backoffs=(5, 20)) -> tuple[int | None, dict | None]:
    """One SGAI scrape with same-key 429 backoff + recovering rotation.
    Raises AllKeysExhausted when every key is long-cooled."""
    ring = _get_ring()
    if ring is None:
        raise RuntimeError("no SGAI API key configured")
    waited = 0.0
    while True:
        got = ring.active()
        if got is None:
            wait = ring.seconds_until_recovery()
            if wait <= 0 or waited >= 120.0:
                raise AllKeysExhausted(f"all SGAI keys cooling for {url[:80]}")
            nap = min(wait + 0.1, 15.0)
            time.sleep(nap)
            waited += nap
            continue
        idx, key = got

        def _post():
            try:
                r = _requests.post(
                    f"{_SGAI_API}/scrape",
                    headers={"Content-Type": "application/json", "SGAI-APIKEY": key},
                    json={"url": url, "formats": formats, "fetchConfig": fetch_config},
                    timeout=timeout,
                )
            except _requests.RequestException:
                return None, None
            try:
                return r.status_code, (r.json() if r.status_code == 200 else None)
            except ValueError:
                return r.status_code, None

        status, data = _post()
        if status == 200:
            ring.note_used(idx)
            return status, data
        if status == 429:
            for w in backoffs:                    # retry SAME key first
                time.sleep(w)
                status, data = _post()
                if status == 200:
                    ring.note_used(idx)
                    return status, data
                if status != 429:
                    break
            ring.cool(idx, RATE_COOL)
            continue
        if status in (401, 402, 403):             # out of credits / forbidden
            ring.cool(idx, QUOTA_COOL)
            continue
        if status is None or status >= 500:       # transient
            ring.cool(idx, RATE_COOL)
            continue
        return status, data                       # other 4xx -> surface to caller


# ---------------------------------------------------------------------------
# Profile + post parsing (deterministic, no LLM)
# ---------------------------------------------------------------------------

def _parse_profile(raw: dict) -> dict | None:
    """The profile object is a JSON string at results.markdown.data[0]."""
    try:
        data = raw["results"]["markdown"]["data"]
    except (KeyError, TypeError):
        return None
    try:
        parsed = json.loads(data[0]) if isinstance(data, list) else data
        return parsed[0] if isinstance(parsed, list) else parsed
    except (ValueError, TypeError, IndexError):
        return None


def owner_handle(url: str) -> str:
    m = _HANDLE.search(url or "")
    return m.group(1) if m else "?"


def _extract_post(raw: dict) -> dict:
    """Pull the biggest post object out of a single-post scrape response."""
    try:
        data = raw["results"]["markdown"]["data"]
        parsed = json.loads(data[0]) if isinstance(data, list) else data
        obj = parsed[0] if isinstance(parsed, list) else parsed
    except (ValueError, TypeError, IndexError, KeyError):
        obj = {}
    if not isinstance(obj, dict):
        obj = {}
    author_key = next((k for k in AUTHOR_KEYS if obj.get(k)), None)
    text = ""
    for v in obj.values():
        if isinstance(v, str) and len(v) > len(text):
            text = v
    return {
        "author_key": author_key,
        "author": obj.get(author_key) if author_key else None,
        "text": text,
        "post_id": obj.get("id"),
    }


def is_junk_text(text: str | None) -> bool:
    """True if the post text is a scraping artifact rather than real content."""
    if not text:
        return True
    t = text.strip()
    if len(t) < 12:
        return True
    if _JUNK_TEXT.search(t):
        return True
    if _BARE_TS.match(t):
        return True
    if _BARE_URL.match(t):
        return True
    if t.lower().startswith("http") and len(t.split()) <= 2:
        return True
    return False


def _is_authored(rec: dict, owner: str, owner_name: str | None) -> bool:
    rec_handle = (rec.get("owner_handle") or "").lower()
    if rec_handle and owner and rec_handle == owner.lower():
        return True
    interaction = (rec.get("interaction") or "").lower()
    if interaction.startswith("shared by"):
        if owner_name and owner_name.lower() in interaction:
            return True
        if owner and owner.lower() in interaction:
            return True
        if not owner_name and not owner:
            return True
    return False


def _is_bio_post(text: str | None) -> bool:
    return bool(text and BIO_PATTERNS.search(text))


def attribute_and_cap(records: list[dict], owner: str, owner_name: str | None,
                      *, authored_keep: int = 10, liked_keep: int = 5) -> dict:
    """Split records into authored/liked, dedup, clean, apply caps.
    `records` must be in most-recent-first order.

    Returns {authored, liked, dropped_authored, dropped_liked, dropped_junk,
    bio_kept}."""
    authored_by_id: dict[str, dict] = {}
    liked_by_id: dict[str, dict] = {}
    authored_order: list[str] = []
    liked_order: list[str] = []

    dropped_junk = 0
    for r in records:
        pid = str(r.get("id"))
        text = (r.get("text") or "").strip() or None
        if is_junk_text(text):
            dropped_junk += 1
            continue
        if _is_authored(r, owner, owner_name):
            rec = {"post_id": pid, "url": r.get("url"), "text": text,
                   "_len": len(text or ""), "bio": _is_bio_post(text)}
            if pid not in authored_by_id:
                authored_order.append(pid)
                authored_by_id[pid] = rec
            elif rec["_len"] > authored_by_id[pid]["_len"]:      # keep longest
                rec["bio"] = rec["bio"] or authored_by_id[pid]["bio"]
                authored_by_id[pid] = rec
        else:
            rec = {"post_id": pid, "url": r.get("url"),
                   "original_author": r.get("author") or r.get("owner_handle"),
                   "excerpt": (text or "")[:280] or None, "_len": len(text or "")}
            if pid not in liked_by_id:
                liked_order.append(pid)
                liked_by_id[pid] = rec
            elif rec["_len"] > liked_by_id[pid]["_len"]:
                liked_by_id[pid] = rec

    authored = [authored_by_id[p] for p in authored_order]   # recency order
    liked = [liked_by_id[p] for p in liked_order]

    kept, bio_kept = [], False
    for i, post in enumerate(authored):
        if i < authored_keep:
            kept.append(post)
        elif post["bio"]:
            kept.append(post)
            bio_kept = True
    dropped_authored = len(authored) - len(kept)

    liked_kept = liked[:liked_keep]
    dropped_liked = len(liked) - len(liked_kept)

    for p in kept:
        p.pop("_len", None)
    for p in liked_kept:
        p.pop("_len", None)

    return {
        "authored": kept,
        "liked": liked_kept,
        "dropped_authored": dropped_authored,
        "dropped_liked": dropped_liked,
        "dropped_junk": dropped_junk,
        "bio_kept": bio_kept,
    }


def _select_activity(activity: list[dict], owner: str, owner_name: str | None,
                     liked_keep: int) -> list[dict]:
    """Pick only the activity items the output caps will keep, in feed order:
    ALL authored posts plus the most-recent `liked_keep` liked posts. Liked
    posts beyond the cap would be dropped anyway, so we never fetch them —
    identical result to fetching all then capping, at lower credit cost."""
    kept, liked_count = [], 0
    for item in activity:
        h = owner_handle(item.get("link", ""))
        is_auth = _is_authored(
            {"owner_handle": h, "interaction": item.get("interaction", "")},
            owner, owner_name)
        if is_auth:
            kept.append(item)
        elif liked_count < liked_keep:
            kept.append(item)
            liked_count += 1
    return kept


# ---------------------------------------------------------------------------
# The lane entrypoint
# ---------------------------------------------------------------------------

def harvest_founder_profile(linkedin_url: str, cfg_linkedin, caps_conn,
                            stop: threading.Event | None = None) -> dict:
    """Harvest one founder LinkedIn profile + attributed posts, respecting
    global caps and human-mimicking pacing. Sequential process-wide.

    caps_conn: an open DB connection used ONLY for the caps counter (the
    caller owns it and its lifetime).

    Returns:
        {
            "status": "ok" | "capped" | "failed" | "no_key" | "keys_exhausted",
            "reason": str | None,       # cap reason / error detail
            "hits": [ {url, title, content}, ... ]   # scorer-ready evidence
            "notes": [str, ...],        # data-quality coverage notes
        }
    """
    out = {"status": "failed", "reason": None, "hits": [], "notes": []}
    if _get_ring() is None:
        out["status"] = "no_key"
        out["reason"] = "SGAI_API_KEY not configured"
        return out

    with _lane_lock:
        # --- cap gate (reserve, don't record yet) ---
        if not cfg_linkedin.bypass_caps:
            capsmod.ensure_table(caps_conn)
            allowed, reason, state = capsmod.reserve(caps_conn, cfg_linkedin)
            if not allowed:
                out["status"] = "capped"
                out["reason"] = reason
                out["notes"].append(
                    f"linkedin {reason} reached ({state['daily_done']}/{state['daily_cap']} today, "
                    f"{state['weekly_done']}/{state['weekly_cap']} this week); profile deferred to next reset")
                return out

        # --- human pacing before the profile ---
        pacer = _get_pacer(cfg_linkedin)
        if not sleep_interruptible(pacer.profile_delay(), stop):
            out["reason"] = "stopped"
            return out

        try:
            status, raw = _sgai_scrape(linkedin_url, FORMATS_PROFILE, FETCH_PROFILE)
        except AllKeysExhausted as e:
            out["status"] = "keys_exhausted"
            out["reason"] = str(e)
            out["notes"].append("all SGAI keys exhausted during linkedin profile scrape")
            return out
        if status != 200 or not raw:
            out["reason"] = f"profile scrape failed (HTTP {status})"
            out["notes"].append(out["reason"])
            return out

        profile = _parse_profile(raw)
        if profile is None:
            out["reason"] = "profile scrape returned no parseable profile object"
            out["notes"].append(out["reason"])
            return out
        if _AUTHWALL.search(json.dumps(raw)):
            out["notes"].append("auth wall detected on linkedin profile; data may be partial")

        owner = profile.get("linkedin_id") or profile.get("id") or ""
        owner_name = profile.get("name")
        activity = profile.get("activity") or []
        activity = _select_activity(activity, owner, owner_name, cfg_linkedin.liked_keep)
        if cfg_linkedin.max_posts:
            activity = activity[:cfg_linkedin.max_posts]

        records = []
        for i, item in enumerate(activity):
            url = item.get("link", "")
            rec = {"id": item.get("id"), "url": url,
                   "owner_handle": owner_handle(url),
                   "interaction": item.get("interaction") or "",
                   "author": None, "text": ""}
            try:
                pstatus, praw = _sgai_scrape(url, FORMATS_POST, FETCH_POST)
                if pstatus == 200 and praw:
                    info = _extract_post(praw)
                    rec["author"] = info["author"]
                    rec["text"] = info["text"]
            except AllKeysExhausted:
                out["notes"].append(
                    f"SGAI keys exhausted after {i}/{len(activity)} posts; keeping partial harvest")
                records.append(rec)
                break
            records.append(rec)
            if i < len(activity) - 1:
                if not sleep_interruptible(cfg_linkedin.post_interval, stop):
                    out["notes"].append("stopped during post harvest; keeping partial harvest")
                    break

        attribution = attribute_and_cap(
            records, owner, owner_name,
            authored_keep=cfg_linkedin.authored_keep,
            liked_keep=cfg_linkedin.liked_keep)

        # --- shape scorer-ready evidence hits ---
        hits = []
        headline = profile.get("position") or ""
        about = profile.get("about") or ""
        followers = profile.get("followers")
        summary_lines = [f"LinkedIn profile: {owner_name or owner} — {headline}".strip(" —")]
        if followers:
            summary_lines.append(f"Followers: {followers}")
        if about:
            summary_lines.append(f"About: {about}")
        hits.append({
            "url": linkedin_url,
            "title": "LinkedIn profile (full harvest)",
            "content": "\n".join(summary_lines),
        })
        for post in attribution["authored"]:
            hits.append({
                "url": post.get("url") or linkedin_url,
                "title": "AUTHORED LinkedIn post (written by the founder themselves — first-party evidence)",
                "content": post.get("text") or "",
            })
        for post in attribution["liked"]:
            hits.append({
                "url": post.get("url") or linkedin_url,
                "title": f"Liked/reposted LinkedIn post (original author: {post.get('original_author')} — weak association only)",
                "content": post.get("excerpt") or "",
            })

        if attribution["dropped_junk"]:
            out["notes"].append(f"dropped {attribution['dropped_junk']} junk post(s) (auth-wall/blank/bare-url)")
        if len(attribution["authored"]) < 3:
            out["notes"].append(
                f"thin authored signal ({len(attribution['authored'])} posts); the activity feed is a "
                "non-deterministic recent slice, authored capture may be partial")
        if attribution["bio_kept"]:
            out["notes"].append("bio/origin-story post force-kept past the recency cap")

        # --- record the cap AFTER success only ---
        if not cfg_linkedin.bypass_caps:
            capsmod.record_done(caps_conn)
        extra = pacer.note_profile_done()
        if extra:
            sleep_interruptible(extra, stop)

        out["status"] = "ok"
        out["hits"] = hits
        return out
