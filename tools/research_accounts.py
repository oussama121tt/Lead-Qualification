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
            if rec.get("Contact") and rec.get("Agency"):
                rows.append(rec)
    else:
        import csv
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("#,") or ",Contact," in l)
        for rec in csv.DictReader(lines[start:]):
            if rec.get("Contact") and rec.get("Agency"):
                rows.append({k: (v or "").strip() for k, v in rec.items() if k})
    for r in rows:
        w = r.get("Website", "")
        if w and not w.startswith("http"):
            r["Website"] = "https://" + w.strip("/")
    return rows


# --------------------------------------------------------------------------- website

def _fetch(url: str) -> dict:
    """Free fetch; Firecrawl fallback for failures and JS shells."""
    page = site_fetcher.fetch_page(url, timeout=15.0, per_domain_delay=1.0)
    if page.get("ok") and len((page.get("text") or "").strip()) > 300 and not page.get("js_heavy"):
        page["via"] = "free"
        return page
    try:
        import scraper
        r = scraper._firecrawl_scrape(url, formats=["markdown", "links"], only_main_content=True, timeout=15000)
        md = getattr(r, "markdown", None) or (r.get("markdown") if isinstance(r, dict) else "") or ""
        links = getattr(r, "links", None) or (r.get("links") if isinstance(r, dict) else []) or []
        if md.strip():
            return {"ok": True, "text": md, "links": list(links), "via": "firecrawl", "status": 200}
    except Exception as exc:  # keep whatever the free fetch got
        page["firecrawl_error"] = f"{exc.__class__.__name__}: {str(exc)[:160]}"
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

def apollo_match(accounts: list[dict]) -> dict[str, dict]:
    import requests
    key = os.environ.get("APOLLO_API_KEY", "")
    out: dict[str, dict] = {}
    details = []
    for a in accounts:
        first, _, last = a["Contact"].partition(" ")
        dom = (urlparse(a.get("Website", "")).hostname or "").removeprefix("www.")
        details.append({"first_name": first, "last_name": last, "domain": dom,
                        "organization_name": a["Agency"]})
    for i in range(0, len(details), 10):
        chunk = details[i:i + 10]
        r = requests.post("https://api.apollo.io/api/v1/people/bulk_match",
                          headers={"x-api-key": key, "Content-Type": "application/json"},
                          json={"details": chunk, "reveal_personal_emails": False}, timeout=60)
        if r.status_code != 200:
            for d in chunk:
                out[f"{d['first_name']} {d['last_name']}"] = {"error": f"HTTP {r.status_code}: {r.text[:160]}"}
            continue
        for d, m in zip(chunk, r.json().get("matches") or []):
            name = f"{d['first_name']} {d['last_name']}"
            if not m:
                out[name] = {"error": "no Apollo match"}
                continue
            org = m.get("organization") or {}
            out[name] = {
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


# --------------------------------------------------------------------------- linkedin

def linkedin_harvest(url: str) -> dict:
    import db as dbmod
    import linkedin_lane
    from runconfig import load_config
    cfg = load_config()
    try:
        conn = dbmod.get_connection()
    except Exception:
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
    try:
        res = linkedin_lane.harvest_founder_profile(url, cfg.linkedin, conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {"status": res.get("status"), "reason": res.get("reason"),
            "notes": res.get("notes") or [], "hits": res.get("hits") or [],
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


# --------------------------------------------------------------------------- render

def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n].rstrip() + f"\n\n…[truncated, {len(s) - n} more chars]"


def render(accounts: list[dict], cache: dict, out_md: Path) -> None:
    L = []
    L.append("# Romandie agencies — research dossier\n")
    L.append(f"Generated {time.strftime('%Y-%m-%d %H:%M')}. Raw evidence only, no judgement: "
             "for each contact, Apollo facts, the person's LinkedIn profile and recent posts, "
             "then the company website organised by page type. Rate it afterwards.\n")
    L.append("Sources and limits: website text is what the public pages show today; "
             "LinkedIn content is the profile plus the recent activity slice LinkedIn exposes "
             "(not the full history); Apollo data is Apollo's view and can be stale.\n")
    L.append("## Contents\n")
    for a in accounts:
        anchor = re.sub(r"[^a-z0-9]+", "-", f"{a['#']} {a['Agency']} {a['Contact']}".lower()).strip("-")
        L.append(f"- [{a['#']}. {a['Agency']} — {a['Contact']}](#{anchor})")
    L.append("")

    for a in accounts:
        name = a["Contact"]
        site = cache["website"].get(a["Website"], {})
        ap = cache["apollo"].get(name, {})
        li = cache["linkedin"].get(a.get("LinkedIn", ""), {})
        L.append("---\n")
        L.append(f"## {a['#']}. {a['Agency']} — {name}\n")
        L.append(f"- **Role:** {a.get('Role', '')}")
        L.append(f"- **City:** {a.get('City', '')}")
        L.append(f"- **Website:** {a.get('Website', '')}")
        L.append(f"- **LinkedIn:** {a.get('LinkedIn', '')}")
        L.append(f"- **Phone:** {a.get('Company phone', '')}")
        L.append(f"- **Wave:** {a.get('Wave', '')}")
        if a.get("Hook (first line)"):
            L.append(f"- **Your existing hook note:** {a['Hook (first line)']}")
        if a.get("Flag"):
            L.append(f"- **Your flag:** {a['Flag']}")
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

        L.append("### LinkedIn — person\n")
        if not li:
            L.append("_not fetched yet_\n")
        else:
            L.append(f"_status: {li.get('status')}{(' — ' + str(li.get('reason'))) if li.get('reason') else ''}_\n")
            for n in li.get("notes") or []:
                L.append(f"> {n}")
            if li.get("notes"):
                L.append("")
            for h in li.get("hits") or []:
                L.append(f"**{h.get('title', '').strip()}**  ")
                if h.get("url"):
                    L.append(f"<{h['url']}>\n")
                L.append(_clip(h.get("content", ""), 3000))
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
    out_md.write_text("\n".join(L), encoding="utf-8")


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True, help="output path without extension")
    ap.add_argument("--skip-linkedin", action="store_true")
    ap.add_argument("--skip-apollo", action="store_true")
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    accounts = read_accounts(Path(args.input))
    out = Path(args.out)
    cache_path = out.with_suffix(".json")
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    for k in ("website", "apollo", "linkedin"):
        cache.setdefault(k, {})

    def save():
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        render(accounts, cache, out.with_suffix(".md"))

    print(f"[research] {len(accounts)} contact(s), {len({a['Website'] for a in accounts})} unique site(s)", flush=True)
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

    if not args.skip_apollo:
        need = [a for a in accounts if a["Contact"] not in cache["apollo"]]
        if need:
            cache["apollo"].update(apollo_match(need))
            print(f"[apollo] matched {sum(1 for a in need if not cache['apollo'].get(a['Contact'], {}).get('error'))}/{len(need)}", flush=True)
            save()

    if not args.skip_linkedin:
        for a in accounts:
            url = a.get("LinkedIn", "")
            if not url or url in cache["linkedin"]:
                continue
            t0 = time.monotonic()
            try:
                cache["linkedin"][url] = linkedin_harvest(url)
            except Exception as exc:
                cache["linkedin"][url] = {"status": "failed", "reason": str(exc)[:200], "hits": [], "notes": []}
            li = cache["linkedin"][url]
            authored = sum(1 for h in li.get("hits") or [] if str(h.get("title", "")).startswith("AUTHORED"))
            print(f"[linkedin] {a['Contact']:<22} {li.get('status'):<15} {len(li.get('hits') or []):>2} hit(s), "
                  f"{authored} authored post(s) [{time.monotonic()-t0:.0f}s]", flush=True)
            save()

    save()
    print("[research] done ->", out.with_suffix(".md"), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
