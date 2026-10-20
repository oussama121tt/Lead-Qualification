"""Research a hand-picked account list: website + person LinkedIn + Apollo facts,
rendered into one Markdown dossier per run. No scoring, no judgement — raw,
organised evidence for a human (or a later rating pass) to judge.

Input: an .xlsx/.csv with at least Contact, Role, Agency, Website, LinkedIn
columns (the Romandie outreach sheet layout). Hyperlinked cells are read for
their target URL, so "Open profile" links work.

Stages, each cached to <out>.json so a re-run only does missing work:
  1. website  — homepage + up to 10 key pages (services, about, team, work,
                clients, partners, careers, tech/AI, blog). Free fetch first,
                Firecrawl only when the free fetch fails or the page is a JS
                shell. Nav/footer boilerplate repeated across pages is removed.
  2. apollo   — people/bulk_match by name + domain: headline, career history,
                company headcount, industry, keywords, founded year
                (1 Apollo credit per person).
  3. linkedin — founder profile + their own recent posts via the paced
                ScrapeGraphAI lane (slow on purpose: anti-ban pacing).

    python tools/research_accounts.py --input "C:/.../Romandie_First20_Outreach.xlsx" \
        --out "C:/Users/w26be/Desktop/Toumi/Romandie_First20_Research"
    python tools/research_accounts.py ... --skip-linkedin      # website + apollo only
    python tools/research_accounts.py ... --render-only        # rebuild the .md from cache
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
_env = dict(re.findall(r'^\s*([A-Z0-9_]+)\s*=\s*"?([^"\n]*)"?\s*$',
                       (ROOT / ".env").read_text(encoding="utf-8"), re.M))
for _k, _v in _env.items():
    os.environ.setdefault(_k, _v)

import site_fetcher  # noqa: E402

PAGE_CAP = 4500          # chars kept per page in the dossier
MAX_PAGES = 12           # key pages per site, homepage excluded
# Ordered by what matters for judging a delivery partner: what they sell, whether
# they build software, who is on the team, whether they hire developers, who they
# already partner with, then proof (work/clients), story, and opinions (blog).
CATEGORIES = [
    ("services", 2, r"service|prestation|expertise|savoir-faire|solution|offre|competence|compétence|what-we-do|capabilit|metier|métier|leistung"),
    ("tech / dev / AI", 2, r"developpement|développement|development|dev|technolog|digital|numerique|numérique|ia|ai|intelligence|application|app|logiciel|software|web|e-commerce|ecommerce|integration|intégration|automatisation|automation"),
    ("team", 1, r"team|equipe|équipe|people|collaborat|talents|staff|nous-sommes|qui-sommes"),
    ("careers", 1, r"job|carriere|carrière|career|emploi|recrut|join|rejoindre|poste|stage"),
    ("partners", 1, r"partenaire|partner"),
    ("work / clients", 2, r"reference|référence|realisation|réalisation|projet|project|work|portfolio|cases?|case-stud|client|travaux"),
    ("about", 1, r"about|a-propos|à-propos|apropos|agence|agency|histoire|studio|story|philosoph|valeurs|values"),
    ("blog / news", 1, r"blog|news|actualit|insight|journal|article|magazine"),
]
# When a site has no linked page for these, try the usual URLs directly.
PROBE_PATHS = {
    "team": ["equipe", "team", "l-equipe", "notre-equipe", "fr/equipe", "fr/team", "a-propos/equipe", "agence/equipe"],
    "careers": ["jobs", "carrieres", "carriere", "emplois", "emploi", "recrutement", "careers", "fr/jobs", "fr/carrieres", "rejoindre", "nous-rejoindre"],
    "partners": ["partenaires", "partners", "fr/partenaires"],
}
SKIP_EXT = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?|pptx?|mp4|mov)(\?|$)", re.I)
LANG_SEG = re.compile(r"^/(fr|en|de|it)(/|$)", re.I)


# --------------------------------------------------------------------------- input

def read_accounts(path: Path) -> list[dict]:
    rows: list[dict] = []
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(path)
        ws = wb.worksheets[0]
        header_row, header = None, None
        for r in ws.iter_rows(min_row=1, max_row=10):
            vals = [str(c.value).strip() if c.value is not None else "" for c in r]
            if "Contact" in vals and "Agency" in vals:
                header_row, header = r[0].row, vals
                break
        if header is None:
            raise SystemExit("no header row with Contact/Agency found")
        for r in ws.iter_rows(min_row=header_row + 1):
            rec = {}
            for name, cell in zip(header, r):
                if not name:
                    continue
                val = cell.value
                if cell.hyperlink and cell.hyperlink.target:
                    val = cell.hyperlink.target
                rec[name] = "" if val is None else str(val).strip()
            if rec.get("Agency"):          # a row without a named contact is still an account
                rows.append(rec)
    else:
        import csv
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("#,") or ",Contact," in l)
        for rec in csv.DictReader(lines[start:]):
            if rec.get("Agency"):
                rows.append({k: (v or "").strip() for k, v in rec.items() if k})
    for r in rows:
        _normalize_account(r)
    return rows


_URL_RX = re.compile(r"(https?://)?([a-z0-9-]+\.)+[a-z]{2,}(/[^\s)]*)?", re.I)


def _normalize_account(r: dict) -> None:
    """Make Romandie-style and Luxembourg-style sheets look the same.

    - Role comes from "Role" or "Title"; phone from "Phone" or "Company phone".
    - Website: the first URL-looking token ("https://cuco.lu (customcoding.lu
      redirects)" -> https://cuco.lu).
    - Contact: the first person in the cell. "_name" is set only when it is a
      usable full name; abbreviated ("Jérôme B. (Owner)") or descriptive cells
      ("Same leader as Interact: ...") leave "_name" empty and keep a first-name
      hint, and the Apollo stage resolves the person from the company's staff.
    """
    r["Role"] = r.get("Role") or r.get("Title") or ""
    r["Company phone"] = r.get("Company phone") or r.get("Phone") or ""
    w = (r.get("Website") or "").strip()
    m = _URL_RX.search(w)
    if m:
        w = m.group(0).rstrip("/.,;")
        r["Website"] = w if w.lower().startswith("http") else "https://" + w
    raw = (r.get("Contact") or "").strip()
    first_person = re.split(r"\n|/|,| and | & ", raw)[0].strip() if raw else ""
    paren = re.search(r"\(([^)]*)\)", first_person)
    role_hint = paren.group(1).strip() if paren else ""
    person = re.sub(r"\([^)]*\)", "", first_person).strip()
    tokens = person.split()
    descriptive = ":" in person or len(tokens) > 4 or not tokens
    abbreviated = bool(tokens) and (len(tokens) < 2 or len(tokens[-1].rstrip(".")) <= 1)
    r["_name"] = "" if (descriptive or abbreviated) else person
    r["_first_hint"] = "" if descriptive or not tokens else tokens[0]
    if descriptive:
        role_hint = ""     # brackets inside a note ("(contact once)") are not a job title
    r["_role_hint"] = role_hint or r["Role"]
    if not r["Role"] and role_hint:
        r["Role"] = role_hint


def person_key(a: dict) -> str:
    return a.get("_name") or f"#{a.get('#')} {a['Agency']}"


def li_url_for(a: dict, cache: dict) -> str:
    return a.get("LinkedIn") or (cache.get("apollo", {}).get(person_key(a)) or {}).get("linkedin_url") or ""


def company_link_for(a: dict, cache: dict) -> str:
    li = cache.get("linkedin", {}).get(li_url_for(a, cache)) or {}
    ap = cache.get("apollo", {}).get(person_key(a)) or {}
    return li.get("company_link") or (ap.get("org") or {}).get("linkedin_url") or ""


def _fold(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)).lower()


# --------------------------------------------------------------------------- website

BOTWALL = re.compile(r"checking (the )?site connection|robot-suspicion|just a moment\.\.\.|attention required|"
                     r"verify you are (a )?human|cf-chl|enable javascript and cookies|ddos protection", re.I)


def _stealth_fetch(url: str) -> dict | None:
    """Third resort for bot-protected sites: ScrapeGraphAI stealth browser."""
    try:
        import linkedin_lane as L
        status, raw = L._sgai_scrape(url, [{"type": "markdown"}, {"type": "links"}],
                                     {"mode": "auto", "stealth": True, "wait": 6000})
    except Exception:
        return None
    res = (raw or {}).get("results") or {}
    md = (res.get("markdown") or {}).get("data")
    md = md[0] if isinstance(md, list) and md else md
    links = (res.get("links") or {}).get("data") or []
    if status == 200 and isinstance(md, str) and len(md.strip()) > 300 and not BOTWALL.search(md[:2000]):
        return {"ok": True, "text": md, "links": list(links), "via": "stealth", "status": 200}
    return None


def _fetch(url: str) -> dict:
    """Free fetch; Firecrawl for failures and JS shells; stealth browser for
    bot-check walls that both of those get stuck on."""
    page = site_fetcher.fetch_page(url, timeout=15.0, per_domain_delay=1.0)
    text = (page.get("text") or "").strip()
    walled = bool(BOTWALL.search(text[:2000]))
    if page.get("ok") and len(text) > 300 and not page.get("js_heavy") and not walled:
        page["via"] = "free"
        return page
    try:
        import scraper
        r = scraper._firecrawl_scrape(url, formats=["markdown", "links"], only_main_content=True, timeout=15000)
        md = getattr(r, "markdown", None) or (r.get("markdown") if isinstance(r, dict) else "") or ""
        links = getattr(r, "links", None) or (r.get("links") if isinstance(r, dict) else []) or []
        if md.strip() and not BOTWALL.search(md[:2000]) and len(md.strip()) > 300:
            return {"ok": True, "text": md, "links": list(links), "via": "firecrawl", "status": 200}
        walled = walled or bool(BOTWALL.search(md[:2000]))
    except Exception as exc:  # keep whatever the free fetch got
        page["firecrawl_error"] = f"{exc.__class__.__name__}: {str(exc)[:160]}"
    if walled or not page.get("ok") or len(text) <= 300:
        st = _stealth_fetch(url)
        if st:
            return st
    page["via"] = "free(partial)" if page.get("ok") else "failed"
    return page


def _norm(u: str) -> str:
    pr = urlparse(u)
    host = (pr.hostname or "").lower().removeprefix("www.")
    return f"{host}{pr.path.rstrip('/')}?{pr.query}" if pr.query else f"{host}{pr.path.rstrip('/')}"


def _same_site(link: str, base_host: str) -> bool:
    h = (urlparse(link).hostname or "").lower().removeprefix("www.")
    return h == base_host or h.endswith("." + base_host)


def _pick_pages(home_url: str, links: list[str]) -> list[tuple[str, str]]:
    base_host = (urlparse(home_url).hostname or "").lower().removeprefix("www.")
    home_path = urlparse(home_url).path or "/"
    home_lang = (LANG_SEG.match(home_path) or [None, None])[1]
    seen, picked = set(), []
    clean = []
    for l in links:
        if not l or l.startswith(("mailto:", "tel:", "javascript:")):
            continue
        u = urljoin(home_url, l).split("#")[0]
        if not _same_site(u, base_host) or SKIP_EXT.search(u):
            continue
        path = urlparse(u).path or "/"
        if path.rstrip("/") in ("", home_path.rstrip("/")):
            continue
        m = LANG_SEG.match(path)
        if m and home_lang and m.group(1).lower() != home_lang.lower():
            continue  # another language version of the same site
        if m and not home_lang and m.group(1).lower() not in ("fr", "en"):
            continue
        u = u.rstrip("/")
        if _norm(u) in seen:
            continue
        seen.add(_norm(u))
        clean.append(u)
    per_cat = Counter()
    taken: set[str] = set()
    for cat, quota, pattern in CATEGORIES:
        rx = re.compile(pattern, re.I)
        for u in clean:
            if len(picked) >= MAX_PAGES or per_cat[cat] >= quota:
                break
            if _norm(u) in taken:
                continue
            path = urlparse(u).path.lower()
            depth = len([s for s in path.split("/") if s])
            if depth > 3:      # deep article/project pages: one level is enough
                continue
            if rx.search(path):
                picked.append((cat, u))
                per_cat[cat] += 1
                taken.add(_norm(u))
    return picked


def _strip_boilerplate(pages: list[dict]) -> None:
    """Drop lines repeated on 3+ pages of the same site (nav, footer, cookie bar)."""
    counts = Counter()
    for p in pages:
        for line in {l.strip() for l in (p.get("text") or "").splitlines() if l.strip()}:
            counts[line] += 1
    if len(pages) < 3:
        return
    common = {l for l, n in counts.items() if n >= 3 and len(l) < 200}
    for i, p in enumerate(pages):
        kept = []
        for line in (p.get("text") or "").splitlines():
            s = line.strip()
            if not s:
                continue
            if s in common and i > 0:
                continue
            kept.append(s)
        p["text"] = "\n".join(kept)


def crawl_site(url: str) -> dict:
    t0 = time.monotonic()
    home = _fetch(url)
    pages = [{"category": "homepage", "url": url, "via": home.get("via"),
              "text": home.get("text") or "", "error": home.get("error") or home.get("firecrawl_error")}]
    picked = _pick_pages(url, home.get("links") or [])
    for cat, u in picked:
        p = _fetch(u)
        pages.append({"category": cat, "url": u, "via": p.get("via"), "text": p.get("text") or "",
                      "error": p.get("error") or p.get("firecrawl_error")})
    # Probe the usual URLs for team / careers / partners when nothing linked to them.
    # Free fetch only: a 404 probe must never cost a Firecrawl credit.
    have = {c for c, _ in picked}
    home_text = (home.get("text") or "")[:400]
    seen_urls = {_norm(u) for _, u in picked} | {_norm(url)}
    for cat, paths in PROBE_PATHS.items():
        if cat in have:
            continue
        for path in paths:
            u = urljoin(url if url.endswith("/") else url + "/", path)
            if _norm(u) in seen_urls:
                continue
            p = site_fetcher.fetch_page(u, timeout=10.0, per_domain_delay=1.0)
            txt = (p.get("text") or "").strip()
            final = p.get("final_url") or p.get("url") or u
            redirected_home = _norm(final) == _norm(url)
            if p.get("ok") and len(txt) > 300 and not redirected_home and txt[:400] != home_text:
                pages.append({"category": cat + " (probed)", "url": u, "via": "free", "text": txt, "error": None})
                seen_urls.add(_norm(u))
                break
    _strip_boilerplate(pages)
    return {"url": url, "pages": pages, "seconds": round(time.monotonic() - t0, 1),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


# --------------------------------------------------------------------------- apollo

def _resolve_from_staff(a: dict, staff: dict) -> tuple[dict | None, str]:
    """Pick the Apollo person for an account whose sheet contact is abbreviated
    or missing: first-name match among the company's staff (leaders first),
    else the most senior leader listed. Returns (staff entry, how)."""
    people = [p for p in (staff or {}).get("staff") or [] if p.get("id")]
    if not people:
        return None, "no staff listed in Apollo"
    lead_rx = re.compile(r"\bceo\b|founder|fondat|owner|managing|gérant|directeur général|general manager|"
                         r"président|president|partner|associé|director", re.I)
    hint = _fold(a.get("_first_hint") or "")
    if hint:
        same = [p for p in people if _fold(p.get("first_name") or "") == hint]
        same.sort(key=lambda p: 0 if lead_rx.search(p.get("title") or "") else 1)
        if same:
            return same[0], f"first name '{a.get('_first_hint')}' matched in Apollo staff"
    rank = [r"\bceo\b|chief executive|administrateur délégué", r"founder|fondat|owner",
            r"managing (partner|director)|general manager|gérant", r"partner|associé|director|président"]
    for rx in rank:
        hit = [p for p in people if re.search(rx, p.get("title") or "", re.I)]
        if hit:
            return hit[0], "most senior leader in Apollo staff (sheet contact was missing or descriptive)"
    return None, "no leader found in Apollo staff"


def apollo_match(accounts: list[dict], staff_cache: dict | None = None) -> dict[str, dict]:
    """people/bulk_match, 1 credit per matched person. Full names match on
    name + domain; abbreviated or missing contacts are resolved to an Apollo
    person id from the free staff search first, then matched by id."""
    import requests
    key = os.environ.get("APOLLO_API_KEY", "")
    out: dict[str, dict] = {}
    jobs = []   # (person_key, detail, how)
    for a in accounts:
        k = person_key(a)
        dom = (urlparse(a.get("Website", "")).hostname or "").lower().removeprefix("www.")
        if a.get("_name"):
            first, _, last = a["_name"].partition(" ")
            jobs.append((k, {"first_name": first, "last_name": last, "domain": dom,
                             "organization_name": a["Agency"]}, "sheet name"))
            continue
        hit, how = _resolve_from_staff(a, (staff_cache or {}).get(dom) or {})
        if hit is None:
            out[k] = {"error": f"contact not identified: {how}"}
            continue
        jobs.append((k, {"id": hit["id"]}, how))
    for i in range(0, len(jobs), 10):
        chunk = jobs[i:i + 10]
        r = requests.post("https://api.apollo.io/api/v1/people/bulk_match",
                          headers={"x-api-key": key, "Content-Type": "application/json"},
                          json={"details": [d for _, d, _ in chunk], "reveal_personal_emails": False}, timeout=60)
        if r.status_code != 200:
            for k, _, _ in chunk:
                out[k] = {"error": f"HTTP {r.status_code}: {r.text[:160]}"}
            continue
        for (name, _, how), m in zip(chunk, r.json().get("matches") or []):
            if not m:
                out[name] = {"error": "no Apollo match"}
                continue
            org = m.get("organization") or {}
            out[name] = {
                "resolved_name": " ".join(filter(None, [m.get("first_name"), m.get("last_name")])),
                "resolved_how": how,
                "headline": m.get("headline"),
                "title": m.get("title"),
                "location": ", ".join(filter(None, [m.get("city"), m.get("country")])),
                "seniority": m.get("seniority"),
                "departments": m.get("departments"),
                "linkedin_url": m.get("linkedin_url"),
                "employment_history": [
                    {"title": e.get("title"), "organization": e.get("organization_name"),
                     "start": (e.get("start_date") or "")[:7], "end": (e.get("end_date") or "")[:7],
                     "current": bool(e.get("current"))}
                    for e in (m.get("employment_history") or [])[:10]
                ],
                "org": {
                    "name": org.get("name"),
                    "employees": org.get("estimated_num_employees"),
                    "founded_year": org.get("founded_year"),
                    "industry": org.get("industry"),
                    "keywords": (org.get("keywords") or [])[:25],
                    "short_description": org.get("short_description"),
                    "linkedin_url": org.get("linkedin_url"),
                    "technologies": (org.get("technology_names") or org.get("current_technologies") or [])[:30],
                    "annual_revenue": org.get("annual_revenue_printed") or org.get("annual_revenue"),
                },
            }
    return out


# --------------------------------------------------------------------------- staff

# Ordered: the first matching role family wins, so a "Lead Developer" counts as
# development, not management.
ROLE_FAMILIES = [
    ("development / engineering", r"develop|développ|developer|engineer|ingénieur|ingenieur|programm|full.?stack|front.?end|back.?end|devops|software|\bcto\b|tech(nical)? lead|lead dev|intégrat|integrat|wordpress|drupal|magento|typo3|php|python|javascript|mobile|ios\b|android|architect"),
    ("data / AI", r"\bdata\b|\bai\b|\bia\b|machine learning|intelligence artificielle|analytics"),
    ("design / UX", r"design|graphi|\bux\b|\bui\b|artistique|art director|motion|illustr|créati|creative|photograph|vidéo|video"),
    ("marketing / content", r"market|communication|\bseo\b|\bsea\b|social|content|rédact|redact|copywr|community|brand|media|relations publiques|\bpr\b"),
    ("project / client", r"project|projet|account|client|consult|strat|product owner|producer|production|traffic"),
    ("leadership", r"\bceo\b|founder|fondat|directeur|director|partner|associ|owner|gérant|managing|président|president|head of|chief"),
]


def apollo_staff(domain: str) -> dict:
    """Who works there, by job title, from Apollo's free people search (no
    credits: names are obfuscated, titles are not). The development headcount
    is the single best public signal of whether an agency builds in-house."""
    import requests
    key = os.environ.get("APOLLO_API_KEY", "")
    people = []
    for page in (1, 2):
        r = requests.post("https://api.apollo.io/api/v1/mixed_people/api_search",
                          headers={"x-api-key": key, "Content-Type": "application/json"},
                          json={"q_organization_domains_list": [domain], "per_page": 100, "page": page},
                          timeout=60)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}: {r.text[:160]}"}
        batch = r.json().get("people") or []
        people += batch
        if len(batch) < 100:
            break
    staff, counts = [], Counter()
    for p in people:
        title = (p.get("title") or "").strip()
        fam = next((f for f, rx in ROLE_FAMILIES if title and re.search(rx, title, re.I)), "other")
        counts[fam] += 1
        staff.append({"id": p.get("id"), "first_name": p.get("first_name"), "title": title, "family": fam})
    staff.sort(key=lambda s: ([f for f, _ in ROLE_FAMILIES] + ["other"]).index(s["family"]))
    return {"total": len(staff), "by_family": dict(counts), "staff": staff,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


# --------------------------------------------------------------------------- linkedin

PERSON_FIELDS = ("name", "position", "city", "followers", "connections", "about", "current_company_name",
                 "experience", "education", "languages", "volunteer_experience", "honors_and_awards")
COMPANY_FIELDS = ("name", "slogan", "about", "description", "specialties", "industries", "company_size",
                  "employees_in_linkedin", "founded", "headquarters", "organization_type", "followers", "website")


def _li_scrape(url: str) -> dict | None:
    """One public-profile scrape through the ScrapeGraphAI key ring. Returns the
    structured object LinkedIn's logged-out page exposes, or None."""
    import linkedin_lane as L
    status, raw = L._sgai_scrape(url, L.FORMATS_PROFILE, L.FETCH_PROFILE)
    if status != 200 or not raw:
        return None
    return L._parse_profile(raw)


def _handle(url: str) -> str:
    m = re.search(r"/(?:in|company)/([^/?#]+)", url or "")
    return (m.group(1) if m else "").lower()


def linkedin_person(url: str) -> dict:
    """The person's public LinkedIn page, kept whole. Experience/education are
    usually hidden from logged-out views; the Apollo section covers career
    history. Activity is classified so a reader can tell the person's own
    posts from their company's posts and from things they merely reacted to."""
    p = _li_scrape(url)
    if not p:
        return {"status": "failed", "reason": "no data returned", "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    me = _handle(url)
    company = (p.get("current_company") or {})
    company_id = (company.get("company_id") or "").lower()
    activity = []
    for a in p.get("activity") or []:
        link = a.get("link") or ""
        inter = a.get("interaction") or ""
        poster = _handle(link.replace("/posts/", "/in/").split("_")[0]) if "/posts/" in link else ""
        if poster == me or "shared this" in inter.lower():
            kind = "own post"
        elif company_id and poster == company_id:
            kind = "company post"
        elif inter:
            kind = inter
        else:
            kind = f"reacted to / reshared ({poster or 'unknown'})"
        activity.append({"kind": kind, "link": link.split("?")[0], "text": (a.get("title") or "").strip()})
    return {
        "status": "ok",
        "profile": {k: p.get(k) for k in PERSON_FIELDS if p.get(k) not in (None, "", [])},
        "company_link": (company.get("link") or "").split("?")[0],
        "activity": activity,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def linkedin_company(url: str) -> dict:
    c = _li_scrape(url)
    if not c:
        return {"status": "failed", "reason": "no data returned", "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    return {
        "status": "ok",
        "company": {k: c.get(k) for k in COMPANY_FIELDS if c.get(k) not in (None, "", [])},
        "employees_shown": [{"name": e.get("title"), "role": e.get("subtitle"), "link": (e.get("link") or "").split("?")[0]}
                            for e in (c.get("employees") or [])],
        "updates": [{"date": u.get("date") or u.get("time"), "likes": u.get("likes_count"),
                     "text": (u.get("text") or "").strip()} for u in (c.get("updates") or [])],
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


# --------------------------------------------------------------------------- render

def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + f"\n\n…[truncated, {len(s) - n} more chars]"


CORE_COLUMNS = {"#", "Contact", "Role", "Title", "Agency", "City", "Website", "LinkedIn", "Phone", "Company phone",
                "Wave", "Tier"}
TRACKER_RX = re.compile(r"status|^notes$|sent|accepted|day 0|checked|\(auto\)|fallback|due", re.I)


def _sheet_notes(a: dict) -> list[tuple[str, str]]:
    """Every research column the user already filled in, in sheet order, except
    identity columns and outreach-tracking columns."""
    return [(k, v) for k, v in a.items()
            if not k.startswith("_") and k not in CORE_COLUMNS and not TRACKER_RX.search(k) and str(v).strip()]


def render(accounts: list[dict], cache: dict, out_md: Path, title: str = "Research dossier") -> None:
    L = []
    L.append(f"# {title}\n")
    L.append(f"Generated {time.strftime('%Y-%m-%d %H:%M')}. Raw evidence only, no judgement: "
             "for each contact, Apollo facts, the person's LinkedIn profile and recent posts, "
             "then the company website organised by page type. Rate it afterwards.\n")
    L.append("Sources and limits: website text is what the public pages show today; "
             "LinkedIn content is the profile plus the recent activity slice LinkedIn exposes "
             "(not the full history); Apollo data is Apollo's view and can be stale.\n")
    # Side-by-side facts, no judgement: what each agency SAYS it builds (terms
    # found on its own pages) next to who it EMPLOYS (Apollo titles).
    build_terms = re.compile(
        r"développement|developpement|development|sur mesure|sur-mesure|application|appli\b|logiciel|software|"
        r"\bapi\b|intégration|integration|e-commerce|ecommerce|magento|shopify|drupal|wordpress|typo3|"
        r"react|vue\.?js|symfony|laravel|\bpim\b|\berp\b|\bcrm\b|intranet|plateforme|platform|\bsaas\b|"
        r"intelligence artificielle|\bia\b|\bai\b|automatisation|automation|chatbot", re.I)
    L.append("## Overview\n")
    L.append("Facts only. **Build terms** = software-delivery words found on the agency's own pages "
             "(how many pages mention each). **Dev listed** = people Apollo lists with a development or "
             "engineering title; Apollo under-covers small agencies, so 0 means none visible, not none.\n")
    L.append("| # | Agency | Contact | LinkedIn staff | Apollo listed | Dev listed | Design listed | Careers page | Build terms on site |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for a in accounts:
        dom = (urlparse(a.get("Website", "")).hostname or "").lower().removeprefix("www.")
        st = cache.get("staff", {}).get(dom) or {}
        fams = st.get("by_family") or {}
        lc = cache.get("linkedin_company", {}).get(company_link_for(a, cache), {}) or {}
        li_staff = (lc.get("company") or {}).get("employees_in_linkedin", "—")
        pages = (cache["website"].get(a["Website"]) or {}).get("pages") or []
        hits = Counter()
        for p in pages:
            for t in {m.group(0).lower() for m in build_terms.finditer(p.get("text") or "")}:
                hits[t] += 1
        terms = ", ".join(f"{t} ({n})" for t, n in hits.most_common(7)) or "—"
        careers = "yes" if any(p["category"].startswith("careers") for p in pages) else "—"
        who = (cache["apollo"].get(person_key(a)) or {}).get("resolved_name") or a.get("_name") or a.get("Contact") or "—"
        tier = f" ({a['Tier']})" if a.get("Tier") else ""
        L.append(f"| {a['#']}{tier} | {a['Agency']} | {who} | {li_staff} | {st.get('total', '—')} | "
                 f"{fams.get('development / engineering', 0)} | {fams.get('design / UX', 0)} | {careers} | {terms} |")
    L.append("")
    L.append("## Contents\n")
    for a in accounts:
        who = (cache["apollo"].get(person_key(a)) or {}).get("resolved_name") or a.get("_name") or "contact not named"
        anchor = re.sub(r"[^a-z0-9]+", "-", f"{a['#']} {a['Agency']} {who}".lower()).strip("-")
        L.append(f"- [{a['#']}. {a['Agency']} — {who}](#{anchor})")
    L.append("")

    for a in accounts:
        site = cache["website"].get(a["Website"], {})
        ap = cache["apollo"].get(person_key(a), {})
        li_link = li_url_for(a, cache)
        li = cache["linkedin"].get(li_link, {}) if li_link else {}
        who = ap.get("resolved_name") or a.get("_name") or "contact not named"
        L.append("---\n")
        L.append(f"## {a['#']}. {a['Agency']} — {who}\n")
        if a.get("Contact") and a.get("Contact") != who:
            L.append(f"- **Contact cell in your sheet:** {a['Contact'].replace(chr(10), ' / ')}")
        if ap.get("resolved_how") and ap.get("resolved_how") != "sheet name":
            L.append(f"- **How the person was identified:** {ap['resolved_how']}")
        L.append(f"- **Role:** {a.get('Role') or ap.get('title') or ''}")
        L.append(f"- **City:** {a.get('City', '')}")
        L.append(f"- **Website:** {a.get('Website', '')}")
        L.append(f"- **LinkedIn:** {li_link or '—'}{' _(found via Apollo)_' if li_link and not a.get('LinkedIn') else ''}")
        if a.get("Company phone"):
            L.append(f"- **Phone:** {a['Company phone']}")
        for k in ("Wave", "Tier"):
            if a.get(k):
                L.append(f"- **{k}:** {a[k]}")
        notes = _sheet_notes(a)
        if notes:
            L.append("")
            L.append("**Your sheet notes (your earlier research, kept as written):**\n")
            for k, v in notes:
                L.append(f"- **{k}:** {str(v).replace(chr(10), ' ')}")
        L.append("")

        L.append("### Apollo facts\n")
        if not ap:
            L.append("_not fetched_\n")
        elif ap.get("error"):
            L.append(f"_{ap['error']}_\n")
        else:
            o = ap.get("org") or {}
            L.append(f"- **Headline:** {ap.get('headline') or '—'}")
            L.append(f"- **Location:** {ap.get('location') or '—'} · seniority {ap.get('seniority') or '—'}")
            L.append(f"- **Company headcount (Apollo):** {o.get('employees') or '—'} · founded {o.get('founded_year') or '—'} · industry {o.get('industry') or '—'}")
            if o.get("annual_revenue"):
                L.append(f"- **Revenue (Apollo estimate):** {o['annual_revenue']}")
            if o.get("short_description"):
                L.append(f"- **Apollo company description:** {o['short_description']}")
            if o.get("keywords"):
                L.append(f"- **Apollo keywords:** {', '.join(map(str, o['keywords']))}")
            if o.get("technologies"):
                L.append(f"- **Technologies detected on their stack:** {', '.join(map(str, o['technologies']))}")
            hist = ap.get("employment_history") or []
            if hist:
                L.append("- **Career history:**")
                for e in hist:
                    end = e.get("end") or ("now" if e.get("current") else "?")
                    L.append(f"  - {e.get('title') or '?'} @ {e.get('organization') or '?'} ({e.get('start') or '?'} → {end})")
            L.append("")

        dom = (urlparse(a.get("Website", "")).hostname or "").lower().removeprefix("www.")
        st = cache.get("staff", {}).get(dom)
        L.append("### Who works there (Apollo people search)\n")
        if not st:
            L.append("_not fetched_\n")
        elif st.get("error"):
            L.append(f"_{st['error']}_\n")
        else:
            fams = st.get("by_family") or {}
            dev = fams.get("development / engineering", 0)
            L.append(f"**{st['total']} people listed · {dev} in development / engineering**  ")
            L.append("_" + " · ".join(f"{f}: {n}" for f, n in sorted(fams.items(), key=lambda kv: -kv[1])) + "_\n")
            for s in st.get("staff") or []:
                L.append(f"- {s.get('title') or '(no title)'} — {s.get('first_name') or '?'} _[{s['family']}]_")
            L.append("\n_Apollo's coverage of small agencies is partial: treat these as a floor, not the full team._\n")

        L.append("### LinkedIn — person\n")
        if not li:
            L.append("_not fetched yet_\n")
        elif li.get("status") != "ok" or "profile" not in li:
            L.append(f"_{li.get('status')}: {li.get('reason') or 'no data'}_\n")
        else:
            prof = li["profile"]
            for k in PERSON_FIELDS:
                v = prof.get(k)
                if v in (None, "", []):
                    continue
                if isinstance(v, (list, dict)):
                    L.append(f"- **{k.replace('_', ' ')}:**")
                    for item in (v if isinstance(v, list) else [v]):
                        if isinstance(item, dict):
                            bits = [str(item.get(x)).strip() for x in ("title", "subtitle", "company", "duration", "description")
                                    if item.get(x)]
                            L.append("  - " + " · ".join(b.replace("\n", " ") for b in bits))
                        else:
                            L.append(f"  - {item}")
                else:
                    L.append(f"- **{k.replace('_', ' ')}:** {v}")
            acts = li.get("activity") or []
            L.append("")
            L.append(f"**Recent LinkedIn activity ({len(acts)} item(s), the slice LinkedIn shows publicly):**\n")
            for act in acts:   # not `a`: that is the account being rendered
                L.append(f"- _{act['kind']}_ — {(act['text'] or '').replace(chr(10), ' ')[:700]}  \n  <{act['link']}>")
            L.append("")

        lc = cache.get("linkedin_company", {}).get(company_link_for(a, cache), {})
        L.append("### LinkedIn — company page\n")
        if not lc:
            L.append("_not fetched_\n")
        elif lc.get("status") != "ok":
            L.append(f"_{lc.get('status')}: {lc.get('reason')}_\n")
        else:
            co = lc["company"]
            for k in COMPANY_FIELDS:
                if co.get(k) not in (None, "", []):
                    L.append(f"- **{k.replace('_', ' ')}:** {str(co[k]).strip()}")
            visible = [e for e in lc.get("employees_shown") or [] if e.get("name") and e["name"] != "LinkedIn Member"]
            if visible:
                L.append("- **Employees LinkedIn shows by name (public sample; see Apollo above for titles):** "
                         + ", ".join(e["name"] for e in visible))
            if lc.get("updates"):
                L.append("")
                L.append(f"**Recent company posts ({len(lc['updates'])}):**\n")
                for u in lc["updates"]:
                    meta = " · ".join(str(x) for x in (u.get("date"), f"{u['likes']} likes" if u.get("likes") is not None else None) if x)
                    L.append(f"- {('_' + meta + '_ — ') if meta else ''}{(u.get('text') or '').replace(chr(10), ' ')[:700]}")
            L.append("")

        L.append("### Website\n")
        if not site:
            L.append("_not fetched_\n")
        else:
            for p in site.get("pages") or []:
                L.append(f"#### {p['category']} — <{p['url']}>  \n_via {p.get('via')}{(' · ' + str(p['error'])) if p.get('error') and not p.get('text') else ''}_\n")
                txt = (p.get("text") or "").strip()
                L.append(_clip(txt, PAGE_CAP) if txt else "_no readable text_")
                L.append("")
    text = "\n".join(L)
    out_md.write_text(text, encoding="utf-8")
    # Same content, one file per account, for tools that cannot take the whole
    # dossier at once (Claude web uploads, quick reading).
    folder = out_md.with_suffix("")
    folder.mkdir(exist_ok=True)
    for old in folder.glob("*.md"):   # names change once contacts resolve; never leave stale copies
        old.unlink()
    head, *sections = re.split(r"\n---\n\n(?=## \d+\. )", text)
    (folder / "00_OVERVIEW.md").write_text(head.split("## Contents")[0], encoding="utf-8")
    for s in sections:
        m = re.match(r"## (\d+)\. (.+?) — (.+)", s)
        if not m:
            continue
        fn = (f"{int(m.group(1)):02d}_{re.sub(r'[^A-Za-z0-9]+', '_', m.group(2)).strip('_')[:40]}_"
              f"{re.sub(r'[^A-Za-z0-9]+', '_', m.group(3)).strip('_')[:30]}.md")
        (folder / fn).write_text(s, encoding="utf-8")


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True, help="output path without extension")
    ap.add_argument("--skip-linkedin", action="store_true")
    ap.add_argument("--skip-apollo", action="store_true")
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--title", default="", help="dossier title (default: from the input file name)")
    args = ap.parse_args()
    title = args.title or (Path(args.input).stem.replace("_", " ") + " — research dossier")

    accounts = read_accounts(Path(args.input))
    out = Path(args.out)
    cache_path = out.with_suffix(".json")
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    for k in ("website", "apollo", "linkedin"):
        cache.setdefault(k, {})
    # Website cache keys must match the normalised account URL exactly. Older
    # caches stored "https://x.ch/" while accounts now normalise to
    # "https://x.ch"; re-key so a trailing slash can never orphan a crawl (it
    # did: every Romandie website section rendered as "not fetched").
    cache["website"] = {k.rstrip("/"): v for k, v in cache["website"].items()}
    missing = [a["Agency"] for a in accounts if a.get("Website") and a["Website"] not in cache["website"]]
    if args.render_only and missing:
        print(f"[research] WARNING: {len(missing)} account(s) have no cached website: {missing[:5]}", flush=True)

    def save():
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        render(accounts, cache, out.with_suffix(".md"), title=title)

    print(f"[research] {len(accounts)} account(s), {len({a['Website'] for a in accounts})} unique site(s), "
          f"{sum(1 for a in accounts if a.get('_name'))} with a full contact name", flush=True)
    if args.render_only:
        save()
        print("[research] rendered", out.with_suffix(".md"))
        return 0

    todo = sorted({a["Website"] for a in accounts if a.get("Website") and a["Website"] not in cache["website"]})
    if todo:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(crawl_site, u): u for u in todo}
            for f in as_completed(futs):
                u = futs[f]
                try:
                    cache["website"][u] = f.result()
                    pg = cache["website"][u]["pages"]
                    print(f"[website] {u:<36} {len(pg):>2} page(s) "
                          f"{sum(len(p.get('text') or '') for p in pg):>7} chars "
                          f"via {Counter(p.get('via') for p in pg)}", flush=True)
                except Exception as exc:
                    cache["website"][u] = {"url": u, "pages": [], "error": str(exc)}
                    print(f"[website] {u} FAILED {exc}", flush=True)
        save()

    cache.setdefault("staff", {})
    domains = sorted({(urlparse(a.get("Website", "")).hostname or "").lower().removeprefix("www.")
                      for a in accounts if a.get("Website")})
    need_staff = [d for d in domains if d and d not in cache["staff"]]
    for d in need_staff:
        try:
            cache["staff"][d] = apollo_staff(d)
        except Exception as exc:
            cache["staff"][d] = {"error": f"{exc.__class__.__name__}: {str(exc)[:160]}"}
        s = cache["staff"][d]
        print(f"[staff   ] {d:<24} {s.get('total', '?'):>3} people, "
              f"{(s.get('by_family') or {}).get('development / engineering', 0)} dev", flush=True)
    if need_staff:
        save()

    # Apollo after staff: abbreviated or missing contacts are resolved from the
    # staff list (free) and then matched by id (1 credit each).
    if not args.skip_apollo:
        need = [a for a in accounts if person_key(a) not in cache["apollo"]]
        if need:
            cache["apollo"].update(apollo_match(need, cache["staff"]))
            ok = [a for a in need if not cache["apollo"].get(person_key(a), {}).get("error")]
            print(f"[apollo] matched {len(ok)}/{len(need)}", flush=True)
            for a in need:
                r = cache["apollo"].get(person_key(a), {})
                if r.get("error") or r.get("resolved_how", "sheet name") != "sheet name":
                    print(f"         {a['Agency'][:28]:<28} -> {r.get('resolved_name') or r.get('error')}"
                          f"{(' (' + r['resolved_how'] + ')') if r.get('resolved_how') and r['resolved_how'] != 'sheet name' else ''}", flush=True)
            save()

    if not args.skip_linkedin:
        import random
        cache.setdefault("linkedin_company", {})
        for a in accounts:
            url = li_url_for(a, cache)
            li = cache["linkedin"].get(url) if url else None
            if url and not (li and li.get("status") == "ok" and "profile" in li):   # missing, failed or old lane format
                t0 = time.monotonic()
                try:
                    li = linkedin_person(url)
                except Exception as exc:
                    li = {"status": "failed", "reason": f"{exc.__class__.__name__}: {str(exc)[:200]}"}
                cache["linkedin"][url] = li
                own = sum(1 for x in li.get("activity") or [] if x["kind"] == "own post")
                print(f"[linkedin] {(a.get('_name') or person_key(a))[:22]:<22} {li.get('status'):<7} "
                      f"{len(li.get('activity') or []):>2} activity, "
                      f"{own} own post(s) [{time.monotonic()-t0:.0f}s]", flush=True)
                save()
                time.sleep(random.uniform(10, 25))
            curl = company_link_for(a, cache)
            if curl and curl not in cache["linkedin_company"]:
                t0 = time.monotonic()
                try:
                    cache["linkedin_company"][curl] = linkedin_company(curl)
                except Exception as exc:
                    cache["linkedin_company"][curl] = {"status": "failed", "reason": f"{exc.__class__.__name__}: {str(exc)[:200]}"}
                lc = cache["linkedin_company"][curl]
                print(f"[company ] {a['Agency']:<22} {lc.get('status'):<7} "
                      f"{(lc.get('company') or {}).get('employees_in_linkedin', '?')} on LinkedIn, "
                      f"{len(lc.get('employees_shown') or [])} shown, {len(lc.get('updates') or [])} post(s) "
                      f"[{time.monotonic()-t0:.0f}s]", flush=True)
                save()
                time.sleep(random.uniform(10, 25))

    save()
    print("[research] done ->", out.with_suffix(".md"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
