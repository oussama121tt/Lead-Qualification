# Lead Qualification & Personalization Engine — Complete System Description

**Purpose of this document:** an exhaustive, code-accurate description of how the system works today (branch `merge-lead-tool`, August 2026), written to be handed to an external reviewer who already knows the business context (RuyaTech's cold-outreach strategy and persona) and must judge: what is genuinely useful, what is missing, and what is wrong. Every prompt, list, threshold, command and configuration knob below is copied verbatim from the code, not paraphrased.

---

## 0. Executive summary

**Input:** an Apollo CSV export of founders (name, title, company, email, website; optionally the founder's LinkedIn URL).

**Output:** each lead classified into one of 6 segments with a confidence score, grounded evidence quotes, situational personalization hooks (each tied to a verbatim quote), a recommended offer — then, for qualified leads, an AI-drafted outreach email that a human reads/edits/approves before it is sent through Gmail.

**Pipeline (per lead):**

```
Apollo CSV ──► ingest + dedup (email / domain / fuzzy company name)
           ──► website fetch (free requests+BS4 first; Firecrawl only for JS-heavy pages)
           ──► deterministic signal extraction (builder fingerprints, fonts, CSS patterns,
               AI-marketing phrase density, careers/pricing signals, GitHub repo check,
               founder names + LinkedIn links found on the site, testimonial tagging)
           ──► LLM scoring pass 1 (site content only)
           ──► IF ambiguous (confidence < 0.7 / needs review) OR confident scaling-agency:
                   web escalation = LinkedIn founder deep-harvest (full profile + attributed
                   posts, paced + capped) + company LinkedIn + Product Hunt / GitHub /
                   Twitter / interview searches
               ──► LLM scoring pass 2 with the web evidence
           ──► code-level guards (schema validation, confidence cap, quote grounding,
               hook grounding, testimonial exclusion, domain-mismatch, site-missing)
           ──► status SCORED or LOW_CONFIDENCE (+ coverage notes, + cost log)
Human ──► results page (5 buckets) ──► per-lead review page (approve / re-score)
      ──► select qualified leads ──► background email drafting (LLM)
      ──► email review page: edit subject/body per lead ──► confirm
      ──► background Gmail sending (10s throttle) ──► email_status = sent
```

**Stack:** Python 3.12, Flask, PostgreSQL (Neon) via psycopg2, Groq (`llama-3.3-70b-versatile`) by default for both scoring and emails with a one-line switch to Claude (`claude-sonnet-4-6`), Firecrawl (fallback scraping), ScrapeGraphAI (LinkedIn/web search), Gmail API (sending), gunicorn (prod server).

---

## 1. Business context the system encodes

RuyaTech sells two offers by cold email:

1. **Technical audit / "Product Rescue & Scale-Up"** — for founders whose product is fragile behind a nice facade. Two angles:
   - `ai_audit`: non-technical founder who built with AI ("vibe coding": Cursor, Bolt, Lovable, Replit, v0, ChatGPT) and is starting to break under real users.
   - `general_audit`: technical team with tech debt/gaps (security + architecture audit).
   - Proof point baked into prompts: "we took over an AI-generated SaaS that was collapsing, relaunched it in 2 weeks, 600 paying members 6 months later (Bake Genie case study)".
2. **AI lead-gen pipeline / "AI Agents & Automation"** (`pipeline`) — custom agents/automations (lead triage, document processing, workflows) sold to agencies/studios scaling their client acquisition. Proof point: "lead triage pipeline delivered to an overwhelmed B2B firm — 5h/week of business dev instead of several hours a day, 30K$+ in new contracts in 30 days".

General proof points available to the email writer: fixed price announced before coding (no hourly billing), 10+ delivered projects, 100% of the code belongs to the client from day one, reply within 4 business hours.

**Primary target persona:** the non-technical "vibe coder" founder. Secondary: technical founders using AI as a tool. Tertiary: small scaling agencies (for the pipeline offer).

---

## 2. Taxonomy (single source of truth: `constants.py`)

```python
VALID_SEGMENTS = {"ai_solo_founder", "technical_founder", "small_agency_scaling",
                  "too_big", "wrong_field", "unclear"}
TARGET_SEGMENTS = {"ai_solo_founder", "technical_founder", "small_agency_scaling"}
OUT_OF_TARGET_SEGMENTS = {"too_big", "wrong_field"}
NOT_YET_SCORED_STATUSES = ("NEW", "PARSED", "FETCH_PARTIAL", "FETCH_FAILED",
                           "SCORE_FAILED", "RESCORE_PENDING", "RESCORE_FAILED")
CONFIDENCE_THRESHOLD = 0.7
```

| Segment | Meaning | Offer |
|---|---|---|
| `ai_solo_founder` | non-technical founder, product built with AI | `ai_audit` |
| `technical_founder` | technical founder/team, uses AI as a dev tool | `general_audit` |
| `small_agency_scaling` | agency/studio in a scaling phase | `pipeline` |
| `too_big` | established, far above the persona | `none` |
| `wrong_field` | unrelated sector, nothing to audit | `none` |
| `unclear` | insufficient evidence — forces human review | usually `none` |

Company stages: `pre-launch | early | scaling | established`.

### Lead status machine

```
NEW ──► (scrape) ──► PARSED | FETCH_PARTIAL | FETCH_FAILED
    ──► (score)  ──► SCORED | LOW_CONFIDENCE | SCORE_FAILED
    ──► (re-score button) ──► RESCORE_PENDING ──► SCORED | LOW_CONFIDENCE | RESCORE_FAILED
    ──► SKIPPED   (deselected at import)
```
- `LOW_CONFIDENCE` = the verdict has `needs_human_review = true` (whatever the reason).
- Separate columns track human review (`review_status`: APPROVED/REJECTED + `review_segment_override`) and email state (`email_status`: draft / sent / failed).
- Session status: `imported | running | completed | failed | cancelled`.

---

## 3. Stage 1 — Ingestion & deduplication

### 3.1 CSV ingestion (`db.insert_leads_from_csv`)
Flexible header mapping (case-insensitive, first match wins):

```python
COLUMN_ALIASES = {
    "first_name":   ["first_name", "first name", "firstname"],
    "last_name":    ["last_name", "last name", "lastname"],
    "title":        ["title", "job title", "person title"],
    "company_name": ["company_name", "company", "company name", "organization"],
    "email":        ["email", "email address", "work email"],
    "website_url":  ["website_url", "website", "company website", "website url"],
    "linkedin_url": ["linkedin_url", "linkedin url", "person linkedin url", "linkedin"],
}
```
- Rows **without a website URL are skipped** (counted as `skipped_no_website`, reported in the flash message — but not stored).
- Computed at ingest: `domain_normalized` (from website), `email_domain`, and **`domain_mismatch`** = 1 when the email domain is not a free provider (gmail, yahoo, outlook, hotmail, icloud, proton, aol, gmx, live, yandex, mail.com, zoho) AND doesn't match the website domain (exact or subdomain). A mismatched lead is later **forced into human review** because the verdict may describe the wrong company.
- Each upload creates an `analysis_sessions` row owned by the logged-in user.

### 3.2 Deduplication (`dedup.py`) — flags, never deletes
Three passes in insertion order; the first occurrence is always the "original":
1. exact email → `duplicate_reason = "exact_email"`
2. normalized domain → `"domain_match"`
3. RapidFuzz `token_sort_ratio` on company name ≥ threshold (default **90**, form-tunable) → `"fuzzy_company_name"`

Duplicates are shown on the import-review page and can be re-included by ticking them.

### 3.3 Cross-batch export dedup
`export_history(lead_id, domain_normalized, exported_at)`. On every scores-CSV download, any lead whose domain was exported in a previous session is flagged `already_exported_previous_batch` and excluded from the CSV (shown with a badge in results).

---

## 4. Stage 2 — Website fetching & deterministic signals (`scraper.py`, `site_fetcher.py`)

### 4.1 Free-first hybrid fetching
`scrape_website(url)`:
1. **Free path** (`site_fetcher.fetch_page`, requests + BeautifulSoup, UA = Chrome desktop, ~1 req/s per domain enforced process-wide, timeout `[website].page_timeout` = 15s):
   - Homepage fetched → raw HTML, visible text (scripts/styles/noscript/svg stripped), links.
   - `<blockquote>` elements are rewritten as markdown `> ` lines so downstream testimonial tagging works identically to Firecrawl markdown.
   - **JS-heavy detection** (`looks_js_heavy`): text < 400 chars AND (`id="root"|"__next"|"app"` present OR > 8 `<script>` tags with < 200 chars of text) → the page is an app shell.
   - If the homepage is JS-heavy, fails, or looks broken → the whole lead escalates to the Firecrawl path.
   - Sub-pages: free fetch each; a JS-heavy/failed **single page** escalates to Firecrawl **for that page only**.
2. **Firecrawl path** (original behavior): homepage with `formats=["markdown","rawHtml","links"]`, sub-pages in parallel (one thread per Firecrawl key), key pool with dead-key marking on quota errors and rate-limit rotation. Up to 5 keys: `FIRECRAWL_API_KEY`, `_2` … `_5`.
3. Every result carries `fetch_notes` (e.g. `"homepage fetched free (requests)"`, `"careers page escalated to Firecrawl (free fetch JS-heavy)"`) → stored as coverage notes.
4. Toggle: `[website] free_first = true|false` in `config.toml`.

### 4.2 Key-page discovery (`_choose_key_pages`)
Only **same-domain** links count. Priority:
```python
KEYWORDS = {"about": ["about","team"], "pricing": ["pricing","plans","price"],
            "careers": ["careers","jobs"], "product": ["product","services","solutions","features"]}
COMMON_PATH_CANDIDATES = {"about": ["/about","/about-us","/team","/company"],
                          "pricing": ["/pricing","/plans"], "careers": ["/careers","/jobs"],
                          "product": ["/product","/products","/services","/solutions","/features"]}
```
Order: keyword match on nav links → common paths (verified with a free HEAD/GET before any paid fetch) → for `product` only, the first unassigned same-domain link (catch-all).

Anchors (`#section`) and links resolving to the homepage are rejected. Per-page cap `MAX_CONTENT_CHARS_PER_PAGE = 32000` (~8k tokens).

### 4.3 Broken / duplicate page filtering
- Pages < 50 chars, or matching `BROKEN_PAGE_MARKERS` (`"client-side exception has occurred"`, `"application error"`, `"hydration failed"`, `"unhandled runtime error"`, `"this page could not be found"`, `"404 not found"`, `"404: this page could not be found"`, `"500 internal server error"`) or the 404 regex patterns are dropped.
- **SPA-shell dedup**: pages whose normalized content hash equals an already-kept page are dropped (many SPA routes return the same shell).
- Status: homepage unusable → `FETCH_FAILED`; any sub-page unusable → `FETCH_PARTIAL`; else `PARSED`.

### 4.4 Compact page signals (replace raw text for careers/pricing)
- **Careers** → `{has_careers_page_content, engineering_keywords_found, other_keywords_found, hiring_technical, engineering_ratio}` using
  `ENGINEERING_ROLE_KEYWORDS` = engineer, developer, backend, back-end, frontend, front-end, full-stack, fullstack, devops, sre, site reliability, data scientist, machine learning, ml engineer, software architect, qa engineer, sdet, platform engineer
  `OTHER_ROLE_KEYWORDS` = sales, account executive, marketing, customer success, support, designer, product manager, operations, recruiter, finance, content writer, community manager, growth
- **Pricing** → `{self_serve_markers_found, sales_led_markers_found, has_visible_price, pricing_motion ∈ self_serve|sales_led|mixed|unclear}` using
  `SELF_SERVE_CTA_MARKERS` = sign up, start free trial, start your free trial, get started free, buy now, start for free, try for free, subscribe now, upgrade now
  `SALES_LED_CTA_MARKERS` = contact us, book a demo, talk to sales, request a quote, schedule a call, contact sales, get in touch, request a demo
  price regex: `[$€£]\s?\d|\b\d+\s?(?:/mo|/month|per month)\b`

### 4.5 Testimonial / third-party tagging (`_tag_attributed_content`) — deterministic
Before content reaches the LLM, structurally detected client content is wrapped in markers:
- Contiguous `>` blockquote blocks + up to 3–4 short following lines (the attribution) →
  `[ATTRIBUTED QUOTE — check the name/company below against lead_metadata before treating as first-party] … [/ATTRIBUTED QUOTE]`
- Sections under a heading matching
  `^#{1,4}\s*.*\b(testimonials?|case\s+stud(?:y|ies)|portfolio|success\s+stor(?:y|ies)|what\s+(?:we'?ve\s+built|clients?\s+say|founders?\s+say|our\s+clients?\s+say)|our\s+work|client\s+(?:stories|reviews))\b.*$`
  up to the next heading →
  `[THIRD-PARTY CONTENT SECTION — likely describes clients/case studies, check attribution before treating as first-party] … [/THIRD-PARTY CONTENT SECTION]`
The scorer both instructs the LLM about these markers and **programmatically discards** any hook/quote citation that falls inside such a block unless attributed to the lead's own name/company.

### 4.6 Deterministic technical signals (`extract_technical_signals`, homepage raw HTML + links)

```python
GENERATOR_FINGERPRINTS = {
  "lovable": [r"lovable\.dev", r"lovable-tagger", r"gpteng\.co"],
  "v0":      [r"v0\.dev", r"vusercontent\.net"],
  "bolt":    [r"bolt\.new", r"stackblitz"],
  "replit":  [r"replit\.com", r"replit\.dev"],
  "cursor":  [r"built with cursor", r"cursor\.sh"],
}
TREND_FONTS = ["Space Grotesk", "Instrument Serif", "Geist", "Syne", "Fraunces"]
VISUAL_PATTERNS = {
  "purple_accent": [r"(?:bg|text|border)-(?:indigo|violet|purple)-[4-7]00"],
  "gradient": [r"bg-gradient-to-\w+", r"from-\w+-\d{3}\s+to-\w+-\d{3}"],
  "glassmorphism": [r"backdrop-blur", r"backdrop-filter"],
  "colored_glow": [r"shadow-(?:indigo|violet|purple|blue)-\d{3}"],
  "numbered_steps": [r"step[\s\-_]?[1-3]", r"how[\s\-]it[\s\-]works"],
  "stat_banner": [r"\d[\d,\.]*\s?(?:\+|k\+)\s*(?:users|customers|clients)"],
  "headline_badge": [r"rounded-full[^\"']*(?:badge|pill|eyebrow)"],
  "faq_accordion": [r"frequently asked questions", r"faq[\s\-_]accordion"],
  "shadcn_ui": [r"data-radix-", r"class=\"[^\"]*\bring-offset-background\b"],
}
VIBE_LANGUAGE_MARKERS = ["built with cursor", "built with v0", "made with lovable",
                         "built with bolt", "vibe coded", "vibe-coded", "no-code"]
AI_STYLE_PHRASES = ["seamlessly integrate", "seamless integration", "unlock the power of",
  "unlock the full potential", "elevate your", "revolutionize the way", "revolutionize how",
  "game-changer", "game changing", "cutting-edge", "state-of-the-art", "harness the power of",
  "empower you to", "empower your team", "in today's fast-paced world", "in today's digital age",
  "in today's rapidly evolving", "at the intersection of", "whether you're a", "whether you are a",
  "dive into", "navigate the complexities of", "take your business to the next level",
  "robust and scalable", "effortlessly", "streamline your workflow", "supercharge your",
  "unleash the power", "transform the way you", "tailored to your needs", "one-stop solution",
  "end-to-end solution", "peace of mind", "step into the future", "reimagine how", "redefine how"]
AI_AUTHORSHIP_DISCLOSURES = ["written with ai", "generated with ai", "powered by gpt",
  "powered by chatgpt", "ai-generated content", "content generated by ai", "drafted by ai"]
```

Output fields: `generator_fingerprint`, `generator_meta_tag` (`<meta name="generator">`), `vibe_language_matches`, `trend_fonts_found`, `visual_patterns_triggered`, `ai_style_phrases_found`, `ai_style_phrase_density` (none / low=1 / medium=2–3 / high=4+), `ai_authorship_disclosures_found`, `github_repo_url` (first github.com link not /issues or /pull, any domain), `linkedin_company_url` (first `/company/` link found on the site), `linkedin_person_urls` (all `/in/` links found), `founder_name_candidates` (regex over homepage/about/product text: "founded by X", "X, founder", "meet the founder X", "Founder & CEO: X"…), `hiring_technical` (from the careers signal).

**GitHub check** (public API, unauthenticated, 60 req/h): fetches up to 100 commits of the found repo → `{total_commits_seen, first_commit_message, single_commit_repo}` — the "one massive initial commit" vibe-coding pattern.

---

## 5. Stage 3 — LLM scoring (`scorer.py`)

### 5.1 Call parameters
- Provider via `llm_provider.get_llm_provider("scoring")` — env `SCORING_LLM_PROVIDER=groq|anthropic` (default groq).
  - Groq: `llama-3.3-70b-versatile`, `temperature=0.2`, `response_format={"type":"json_object"}`, `max_tokens=2048` (retry: 1024), timeout 90 s.
  - Anthropic: `claude-sonnet-4-6` (override `ANTHROPIC_MODEL`), same system prompt, JSON parsed with code-fence tolerance.
- Budgets: `MAX_SITE_CONTENT_CHARS = 12000`, `MAX_WEB_EVIDENCE_CHARS = 12000` (equal weight, separate budgets), retry with site content cut to 6000 chars.
- Image/media markdown is stripped from content before prompting.
- Retry policy: on JSON parse failure or HTTP 400 → one retry with reduced content; on 413/429 → one retry with reduced content; otherwise the exception propagates → `SCORE_FAILED`. All calls (retries included) are cost-logged.

### 5.2 SYSTEM PROMPT (verbatim)

```
You are a senior analyst who evaluates B2B leads for a technical
development agency (RuyaTech). You receive the contact's Apollo metadata, the scraped
content of their website, and deterministic signals that have already been computed (do not
re-derive them).

THE TWO OFFERS WE SELL:
- Technical audit — for founders who have a fragile product behind a nice facade
  (ai_audit if built with AI by a non-technical person, general_audit if technical team
  but with tech debt/gaps).
- AI lead-gen pipeline — sold to agencies/studios that are scaling their own client
  acquisition (offer "pipeline").

OUR PRIMARY TARGET: non-technical founders who use AI to build
their product (vibe coding, Cursor, Bolt, Lovable, Replit, etc.). They need a technical
audit because their code lacks robustness.

SEGMENTS (choose EXACTLY ONE, never an invented value outside this list):
- ai_solo_founder — non-technical founder, product built with AI (vibe coding).
  → recommended_offer: ai_audit
- technical_founder — technical founder/team, uses AI as a dev tool (not as a
  crutch). → recommended_offer: general_audit
- small_agency_scaling — agency or services studio in a scaling phase (hiring, several
  visible clients, looking to industrialize). → recommended_offer: pipeline
- too_big — established company, size/maturity far above the targeted persona (large
  team, mature product for years, no sign of technical fragility).
  → recommended_offer: none
- wrong_field — sector unrelated to our offers (no software product, no technical
  site to audit). → recommended_offer: none
- unclear — INSUFFICIENT EVIDENCE to decide between the categories above. This is a normal
  and honest state, not a failure: use it whenever the content is too thin, too
  ambiguous, or contradictory to choose a segment with confidence. → needs_human_review
  necessarily true, recommended_offer usually none unless there is a partial exploitable signal.

Never confuse "unclear" (not enough evidence) with "wrong_field" (clear evidence this is
not our target) or "too_big" (clear evidence this is too big) — these three segments
say different things and must remain distinct.

RELIABILITY HIERARCHY OF DETERMINISTIC SIGNALS (provided at the end of the message) — respect
it strictly, never treat two signals of different strength as equivalents:
- STRONG (near-proof): generator_fingerprint non-null (direct reference to a tool like
  lovable.dev, bolt.new, v0.dev...), ai_authorship_disclosures_found non-empty (the company
  itself says it uses AI for its content), github_check.single_commit_repo=true combined with a
  generator_fingerprint present.
- MEDIUM: vibe_language_matches non-empty (explicit "built with X" mention in the HTML),
  ai_style_phrase_density "high".
- WEAK (never sufficient by itself): visual_patterns_triggered (gradient, shadcn_ui,
  glassmorphism, numbered_steps...) — thousands of well-built professional products use
  these same modern visual conventions. A single visual pattern, without an accompanying
  STRONG or MEDIUM signal, must NEVER tip the scale toward ai_solo_founder. Treat it as a
  hint that merits at most needs_human_review, never a conclusion.
- A fingerprint/pattern may come from code the user cannot see (third-party script,
  tracker, widget) — if it is isolated and nothing else corroborates it (no explicit mention
  in the visible text, no vibe-coding language), lower your confidence accordingly rather
  than treating it as a given.
- Both evidence blocks carry EQUAL WEIGHT: "Information collected on this lead" (the
  official site) and "Web search results" (LinkedIn, Product Hunt, GitHub, interviews,
  directories) — the web can be the ONLY source that reveals vibe-coding, a technical
  team, or an agency, so NEVER discount it because it is not the official site.
  An explicit mention found in either block (e.g. a post where the founder
  himself admits to vibe-coding) is a STRONG signal regardless of which block it
  comes from. Conversely, a vague, out-of-context search snippet, or one that seems to
  be about another company with the same name, carries equal doubt whether it appears
  in the site content or in the web results.
- Evidence labeled "person_*" (person_linkedin, person_github) describes the founder
  HIMSELF (his own LinkedIn profile, his own GitHub) — treat it as a PRIORITY signal
  to distinguish technical_founder from ai_solo_founder, more reliable than a signal
  inferred from the company's site: a founder's own profile showing engineering work,
  commits, or a technical history points toward technical_founder; a profile with no
  technical trace while the product is AI-built points toward ai_solo_founder.

- CURSOR - SPECIAL RULE (weaker than the Lovable/Bolt/v0 fingerprints): Cursor is a
  general-purpose IDE that leaves no detectable HTML fingerprint on the site (unlike
  "lovable-tagger" or "v0.dev" client scripts), and it is used by BOTH non-technical
  founders and very experienced engineers. A bare mention of Cursor ("built with
  cursor" in the site text or in the web search results) is therefore NEVER
  sufficient BY ITSELF to classify the lead as ai_solo_founder. It MUST be
  corroborated by at least one of: github_check.single_commit_repo = true, OR a
  founder's own profile (person_linkedin / person_github) showing an absence of
  technical background. Without such corroboration, a Cursor mention alone orients
  toward "unclear" (insufficient evidence) or "technical_founder" depending on the
  other signals - never ai_solo_founder on its own.

FIRST-PARTY VS. CLIENT CONTENT — CRITICAL DISTINCTION:
Scraped site content often mixes two different voices: the company describing ITSELF,
and the company describing ITS OWN CLIENTS (testimonials, case studies, portfolio
items, "what we built for X"). This is especially common for agencies/studios
(small_agency_scaling), whose entire site is often built around client success
stories.
- built_with_ai_signals, technical_signals, and pain_signals must describe the
  ANALYZED COMPANY ITSELF — never a client mentioned in a testimonial, case study,
  or portfolio entry.
- A phrase like "we rescue broken products" or "our client's MVP was falling apart"
  describes a SERVICE OFFERED TO OTHERS, not a problem the analyzed company itself
  has. Do not extract this as a pain_signal for the analyzed company.
- A testimonial quote from a named client ("I built my MVP with vibe coding...") is
  evidence about THAT CLIENT, not about the site's owner — never attribute it to the
  company being scored.
- Before extracting any signal, ask: "is this text describing the company I am
  scoring, or a business it works with / has worked with?" If it's the latter,
  discard it for built_with_ai_signals/technical_signals/pain_signals — it can still
  inform company_stage or segment (e.g. many detailed case studies suggest an
  established agency), but must not be cited as if it were first-party evidence
  about the analyzed company.

STRUCTURAL DETECTION — do not rely on specific wording, rely on these PATTERNS
(they recur across virtually every agency/services site, regardless of the exact
vocabulary used):
1. Markdown blockquotes (lines starting with ">") are almost always testimonials —
   treat their content as evidence about the QUOTED PERSON/COMPANY, never about the
   site owner, regardless of what the quote says or who appears to be "speaking".
2. Any block immediately followed or preceded by a name + title + company line
   (e.g. "Jane D. — Founder, Acme") is an attributed quote. The site owner is
   never the subject of an attributed quote's content, even if grammatically
   first-person ("I built...", "We were struggling...").
3. Sections under headers containing words like "Testimonial", "Case Stud*",
   "Portfolio", "What We've Built", "Client*", "What Founders/Clients Say",
   "Success Stor*", "Our Work" describe THIRD PARTIES (past or prospective
   clients), never the site owner.
4. A narrative in past tense introducing an unnamed or named third party ("A
   founder came to us with...", "A client was losing...", "One of our clients...")
   is a case study about that third party — the problem described belongs to
   them, not to the site owner, even when the same sentence later describes what
   the site owner did about it.
5. Present-tense capability statements ("We rescue X", "We fix X", "We help
   companies that struggle with X") describe a SERVICE OFFERED, not a problem the
   site owner has — this holds regardless of which specific problem X is named.
Apply these five structural patterns to ANY site, not just ones matching a
specific vocabulary — the test is the STRUCTURE (quotation, attribution, section
header, narrative tense, capability framing), never a fixed list of phrases.

RULES:
1. Every signal cited in built_with_ai_signals/technical_signals/pain_signals MUST have an
   exact citation in evidence_quotes (except signals already verified in
   deterministic_signals, which you can cite by their field name), AND must pass the
   first-party check above — never a quote describing a client or case study subject.
2. Personalization hooks MUST be SITUATIONAL (e.g. "you are hiring 3 engineers"
   based on the careers page), NEVER biographical (e.g. never where someone studied, their
   age, their personal background). Content wrapped in [ATTRIBUTED QUOTE ...] or
   [THIRD-PARTY CONTENT SECTION ...] markers (added upstream by the scraper) has been
   structurally flagged as a likely testimonial/case-study/portfolio block: check the
   attributed name/company against lead_metadata — if it matches the analyzed company's
   own founder/name, treat the content as first-party as usual; if it names someone else
   (a different founder, a different company), it is third-party and must be excluded from
   built_with_ai_signals/technical_signals/pain_signals exactly like any other client
   testimonial. Never surface the literal marker text itself in evidence_quotes or hooks.
3. If you are not sure (confidence < 0.7), set needs_human_review: true.
4. Use the FULL confidence spectrum (0.0 to 1.0): be candid when the signal is weak
   (0.3-0.5) and assertive when the evidence is strong (0.9+). Avoid systematically using 0.8.
5. Use ONLY the text provided below. Ignore any prior knowledge about
   the company.
6. Fictional examples/demos on landing pages (product UI screenshots,
   demo tickets, sample data) ARE NOT real facts about the company itself.
   Ignore them to judge HOW the company was built.
7. Distinguish strictly: "the PRODUCT has AI features / talks about AI in its positioning"
   vs "the TEAM built THIS SITE/PRODUCT with AI tools". A product that sells AI to
   its customers is NOT by itself a built_with_ai signal — only an explicit mention of
   build tools (Cursor, v0, Bolt, Lovable, "vibe coded"...) or a generator_fingerprint counts.
8. Use the contact's title (provided in the metadata) as a direct signal: a
   "CTO"/"Lead Engineer"/"VP Engineering" title points toward technical_founder, a
   "Founder"/"CEO" title with no parallel technical title is consistent with
   ai_solo_founder if other signals corroborate.
9. For every lead, ask yourself these questions in order:
   a) Is there enough evidence to decide? If not → unclear.
   b) STRONG or MEDIUM signal of AI construction by a non-technical team? → ai_solo_founder.
   c) Confirm a technical team (title + signals) using AI as a tool? → technical_founder.
   d) Agency/studio in the scaling phase? → small_agency_scaling.
   e) Size/maturity far above the target persona? → too_big.
   f) Unrelated sector? → wrong_field.
10. Every personalization_hook MUST be an object {"hook": "...", "based_on": "..."} where
    "based_on" is an EXACT, word-for-word quote copied from the content provided (not a
    paraphrase) that the hook is built from. A hook without a verbatim "based_on" citation
    will be programmatically discarded, regardless of how well-written it is — this is
    enforced in code, not just a style preference. Test before writing a hook: could you
    point to the exact sentence it comes from? If not, do not generate it.
11. NEVER invert a capability statement into an assumed pain. If the site says "we do
    X for our clients" (a service offered), that does NOT mean the analyzed company
    itself needs X or has the problem X solves — that would be projecting the
    company's own marketing pitch back onto itself. A personalization_hook may only
    claim the analyzed company "has" a problem, a need, or an experience if "based_on"
    is a quote that directly states this about the company itself (e.g. its own careers
    page, its own tech stack, its own product state) — never derived by flipping a
    description of what it sells or does for others. Note: even a well-formed "based_on"
    citation gets discarded downstream if it falls inside a testimonial/case-study block
    describing someone other than the analyzed company (see the [ATTRIBUTED QUOTE]/
    [THIRD-PARTY CONTENT SECTION] markers in the content) — so citing such a block does
    not satisfy rule 10 either.

Respond ONLY in JSON following this schema:
{
  "segment": "ai_solo_founder | technical_founder | small_agency_scaling | too_big | wrong_field | unclear",
  "confidence": 0.0,
  "company_stage": "pre-launch | early | scaling | established",
  "built_with_ai_signals": [],
  "technical_signals": [],
  "pain_signals": [],
  "evidence_quotes": [],
  "recommended_offer": "ai_audit | general_audit | pipeline | none",
  "personalization_hooks": [{"hook": "...", "based_on": "exact verbatim quote from the content"}],
  "disqualify_reason": null,
  "needs_human_review": false
}
```

### 5.3 USER MESSAGE layout (blocks joined by `\n\n---\n\n`, in this order)

1. **Contact metadata**
   ```
   Contact metadata (Apollo source):
   - Name: <first last>
   - Title: <title>
   - Company: <company>
   - Email: <email>
   - Website: <url>
   ```
2. **Site content** (if any): `Information collected on this lead (official site):\n\n` + pages concatenated as `## Source: homepage|about|product|pricing|careers\n<content>` separated by `\n\n---\n\n`, capped at 12,000 chars. Careers/pricing appear as compact signal blocks, e.g. `[Careers — deterministic extraction, no raw text]\n- hiring_technical: True …`.
3. **Web evidence** (if any, `person_*` sources first):
   ```
   Web search results (LinkedIn — company page and the founder's own profile
   (person_*), Product Hunt, GitHub, interviews, directories — same weight as
   the site content above):

   [person_linkedin] AUTHORED LinkedIn post (written by the founder themselves — first-party evidence) (<url>)
   <full post text>

   [linkedin] LinkedIn company page (from site link) (<url>)
   <markdown + "--- Structured ---" JSON>
   …
   ```
   capped at 12,000 chars.
4. **Site-missing instruction** (only when the site produced no usable content):
   ```
   IMPORTANT — the official website could not be scraped properly (empty content or fetch failure), so NO reliable site content is available for this lead.
   - Base your verdict ONLY on the contact metadata and the web search results provided. Do not invent or assume site content.
   - Do NOT treat the absence of site content as a signal in either direction: a site can be temporarily down or blocked without saying anything about the product's quality.
   - If your verdict relies solely on web evidence with no official site content, set needs_human_review to true REGARDLESS of confidence, and mention this limitation explicitly in disqualify_reason.
   ```
5. **User-selected scoring criteria** (chosen on the import-review page; optional):
   `Scoring criteria selected by the user (give more weight to these criteria):` followed by the selected keys with these descriptions:
   - `ai_solo_founder`: "PRIMARY TARGET: identify non-technical founders who build with AI (vibe coding, Cursor, Bolt, Lovable, Replit) — corresponds to the ai_solo_founder segment."
   - `technical_founder`: "SECONDARY TARGET: identify technical teams that use AI as a development tool — corresponds to the technical_founder segment."
   - `solo_or_small`: "Identify solo founders or micro-teams (1-5 people)."
   - `agency_or_studio`: "Identify agencies / services studios that are scaling — corresponds to the small_agency_scaling segment."
   - `no_ai`: "Identify established companies with no AI-construction signal."
   - `wrong_field`: "Identify leads that are clearly not our target (too_big, wrong_field)."
   - plus `- Custom criterion: <free text>` if entered.
6. **Deterministic signals**: `Deterministic signals already verified (do not re-derive, do not invent beyond what follows — apply the reliability hierarchy STRONG/MEDIUM/WEAK described in your instructions):\n<JSON of technical_signals + github_check>`.

### 5.4 Code-level guards applied to every verdict (in order)
1. `_apply_confidence_guard`: `confidence < 0.7` → `needs_human_review = true`.
2. `_validate_verdict`: segment ∉ VALID_SEGMENTS → forced `unclear`, review, note `invalid_segment_fixed_to_unclear`; offer ∉ {ai_audit, general_audit, pipeline, none} → `none`; stage ∉ valid → null. **Any forced correction caps confidence at 0.3.**
3. `_verify_evidence_grounding`: every `evidence_quotes` entry must appear verbatim (whitespace/case-normalized) in the site+web text; ungrounded quotes are removed, the lead is flagged for review with `ungrounded_evidence_quotes_removed: N`.
4. `_verify_hooks_grounding`: each hook must be `{hook, based_on}` with `based_on` found verbatim in the source and **not inside a third-party tagged block** attributed to someone other than the lead; failing hooks are discarded (`ungrounded_or_third_party_hooks_removed: N`).
5. `_apply_site_missing_guard`: no site content → review forced + `site_content_missing: …` note.
6. In the pipeline: `domain_mismatch` → review forced + note "this verdict may describe the wrong company".

### 5.5 Two-pass scoring & escalation rule (`pipeline._process_lead`)
- **Pass 1**: site content + metadata + deterministic signals only (no web credits).
- **Escalate to web evidence when**: `needs_human_review` OR `confidence < 0.7` OR (`segment == small_agency_scaling` AND `hiring_technical` AND `confidence ≥ 0.7`).
- Clear-cut verdicts (`too_big`, `wrong_field`, confident non-agency) never pay for web search — a coverage note records "web escalation skipped: pass-1 verdict was clear-cut".
- **Pass 2**: same prompt + web evidence block. If pass 2 throws, pass-1 verdict is kept with note `web_escalation_second_pass_failed`.

---

## 6. Stage 3b — Web escalation & the LinkedIn founder lane

### 6.1 Search queries (ScrapeGraphAI `/api/search`, 2 results per query, parallel one thread per key)
```
linkedin        : "{company}" site:linkedin.com/in OR site:linkedin.com/company
product_hunt    : "{company}" site:producthunt.com
twitter         : "{company}" (site:twitter.com OR site:x.com) (vibe coded OR built with AI OR built in a weekend)
github          : "{company}" site:github.com
interviews      : "{founder}" OR "{company}" interview (vibe coding OR built with AI OR built with Cursor OR built with v0)
person_linkedin : "{founder}" site:linkedin.com/in
person_github   : "{founder}" site:github.com
```
`{founder}` = first founder-name candidate found **on the site itself**, else the CSV first+last name (site names outrank CRM fields, which can be placeholders — a real bug: "Wael Test" once pulled a stranger's profile).

Disambiguation: if the site links its own LinkedIn company page, that URL is scraped directly and the `linkedin` query is skipped (a name search once returned a same-named unrelated company). A person URL from the site is trusted only when there is **exactly one** `/in/` link.

### 6.2 LinkedIn page scrape (ScrapeGraphAI `/api/scrape`, markdown + structured JSON) — extraction prompts
- Company page: `"Extract company name, description, headquarters, industry, company size, number of employees, specialties, website, and founders"`
- Person page (legacy/fallback path): `"Extract the person's name, current roles and company, work experience, education, skills, and whether they are a founder, CTO, or engineer"`

### 6.3 Founder deep harvest (`linkedin_lane.py`) — the merged lane
Used when a founder profile URL is known (CSV `linkedin_url` column first, else the single site `/in/` link). Replaces the snippet-based `person_linkedin` evidence when it succeeds.

Behavior:
1. **Cap gate** — `caps.reserve()` against global daily/weekly counters (`li_daily_counter` table, persisted across all sessions/users): defaults **50/day, 250/week**. If capped → status `capped`, coverage note, snippet fallback.
2. **Pacing** — random delay `[45, 180]` s before the profile; every 15–20 profiles an extra `[15, 40]` min pause. Sequential process-wide (module lock) even though leads run in parallel.
3. **Profile scrape** — `fetchConfig {"mode":"auto","stealth":true,"scrolls":5,"wait":3000}`, formats markdown+links. Auth-wall detection (`join linkedin|sign in to view|authwall`) is noted.
4. **Activity selection** — from the profile's activity feed: keep ALL posts whose permalink handle matches the owner (or "shared by <owner>") + at most 5 liked posts; cap `max_posts = 12`.
5. **Per-post fetch** — each permalink fetched for **full text** (7 s between posts). Junk dropped: auth-wall text, bare URLs, bare timestamps, < 12 chars.
6. **Attribution in code** — `_is_authored`: handle match or "shared by" owner → AUTHORED; else LIKED with `original_author`. Dedup by post id keeping the longest text. Caps: 10 authored (+ any "bio" post force-kept past the cap — regex: `here's who i am|my story|a bit about me|about me|originally from|my background|my journey|founder story|our story|followers|i started this|why i started/founded/built|a little about me|introduce myself|📌|pinned|featured`), 5 liked.
7. **Cap recorded only after success** (`caps.record_done`).
8. **Output to the scorer** as `person_linkedin` hits: one profile summary (`LinkedIn profile: <name> — <headline>`, followers, about) + each authored post titled `AUTHORED LinkedIn post (written by the founder themselves — first-party evidence)` + each liked post titled `Liked/reposted LinkedIn post (original author: X — weak association only)` with a 280-char excerpt.
9. **Key management** — SGAI keys (`SGAI_API_KEY`, `_2`…`_5` or `SCRAPE_API_KEYS=k1,k2,…`) in a `MemoryKeyRing`: 429 → retry same key after 5 s then 20 s, then cool 30 s; 401/402/403 → cool 1 h (out of credits); 5xx/network → cool 30 s; all keys cooling → wait up to 120 s then `AllKeysExhausted` (noted, snippet fallback).

Coverage notes written per lead, e.g. `"linkedin founder profile harvested in full (7 authored post(s))"`, `"thin authored signal (2 posts); the activity feed is a non-deterministic recent slice…"`, `"linkedin daily_cap reached (50/50 today, 180/250 this week); profile deferred to next reset"`, `"no founder linkedin url known (csv or site); person evidence limited to name search"`.

All web evidence is persisted in `lead_search_evidence` and **reloaded on re-score** (no re-spend).

---

## 7. Stage 4 — Human review

### 7.1 Results page buckets (`_categorize_leads`)
| Bucket | Rule |
|---|---|
| Not selected | status `SKIPPED` (deselected at import) |
| Waiting / pending | status in `NOT_YET_SCORED_STATUSES` |
| To review | `needs_human_review = true` (covers unclear, confidence < 0.7, domain mismatch, ungrounded quotes, site missing, forced corrections) |
| **Ready to approve** | segment in TARGET_SEGMENTS and no review flag |
| Out of target | segment in {too_big, wrong_field} and no review flag |
| (badge) Already exported | flagged by cross-batch export dedup |

Note for the reviewer: "Ready to approve" is **machine-classified**, not human-approved. Explicit human decisions exist as separate actions below.

### 7.2 Per-lead review page (`/lead/<id>/review`)
Shows: verdict, confidence, stage, offer, all signal lists, evidence quotes, hooks with their `based_on` citations, disqualify reason, deterministic technical signals, **evidence coverage notes**, every scraped page's content, every web-search source's hits. Actions:
- **Approve** (`POST /lead/<id>/approve`): clears `needs_human_review` and sets status `SCORED` → lead moves to "Ready to approve".
- **Re-score** (`POST /lead/<id>/rescore`): deletes the verdict, re-runs the LLM on stored content + stored web evidence (no re-scraping, no new search).
- Legacy endpoint `POST /lead/<id>/review` records `review_status = APPROVED|REJECTED` + segment override (no UI button currently calls it).
- Batch re-score: `POST /rescore/<session_id>` (selected leads, or all "to review" by default).

### 7.3 Import-review step (before scoring)
After upload: shows keepers vs duplicates; the user ticks which leads to analyze (unticked → `SKIPPED`), can re-include duplicates, picks scoring criteria (§5.3 item 5), a custom free-text criterion, throttle seconds (default 12) and concurrency (default 3, env `PIPELINE_CONCURRENCY`).

---

## 8. Stage 5 — Email generation (`emailer.py`)

- Trigger: results page checkboxes → `POST /session/<id>/prepare_emails` → **background thread**. Only leads in "Ready to approve" are drafted unless `include_unapproved=1` is posted; skipped counts are reported. Leads already `draft`/`sent` are skipped.
- Provider: `EMAIL_LLM_PROVIDER=groq|anthropic`, `max_tokens=1024`, JSON response `{"subject","body"}`. Every call cost-logged with purpose `email`.
- Inputs: company name, contact first name, segment, recommended offer, hooks (JSON, ≤ 800 chars), evidence quotes (≤ 800 chars), first non-empty scraped page (≤ 1200 chars), sender signature from `.env` (`SENDER_NAME` — `SENDER_COMPANY` default "RuyaTech").

### EMAIL PROMPT (verbatim; `{…}` are filled fields)

```
You write a short, personalized outreach email for RuyaTech,
a technical agency that builds, rescues, and scales SaaS products for founders.

Company: {company_name}
Contact first name (leave "Greetings," without a name if empty): {contact_first_name}
Detected segment: {segment}
Recommended offer: {recommended_offer}
Personalization hooks already identified by the scoring: {personalization_hooks}
Evidence/quotes taken from the site: {evidence_quotes}
Excerpt from the homepage content: {homepage_content}

Context of the RuyaTech offers (pick the one matching recommended_offer, stay faithful to the
exact positioning below — do not generalize, do not reinvent what we offer):

- ai_audit → "Product Rescue & Scale-Up" service: for non-technical founders whose product was
  built with AI (vibe-coding — Cursor, Replit, ChatGPT, Lovable, Bolt) and starts breaking under
  real users. Full code audit, stabilization, refactoring, and getting it back on track —
  typically in 4 to 8 weeks. Concrete example to reuse if relevant: we took over an AI-generated
  SaaS that was collapsing, relaunched it in 2 weeks, 600 paying members 6 months later
  (Bake Genie case study).

- general_audit → same "Product Rescue & Scale-Up" service, for a technical team:
  security and architecture audit, concrete recommendations, fix prioritization.

- pipeline → "AI Agents & Automation" service: custom AI agents and automations plugged into
  existing systems (lead triage, document processing, workflows), not "AI gadgets".
  Concrete example to reuse if relevant: lead triage pipeline delivered to an overwhelmed B2B
  firm — 5h/week of business dev instead of several hours a day, 30K$+ in new contracts in
  30 days.

General RuyaTech proof points, to use sparingly (one if needed, never all at once) to add
credibility without making the email sound like a sales brochure: fixed price announced before
coding (no hourly billing), 10+ delivered projects, 100% of the code belongs to the client
from day one, reply within 4 business hours.

Strict instructions:
- Short subject line specific to this company (not generic, no visible template) — never empty,
  mandatory in every response.
- Body structure, in this order, with a line break between each block:
  1. Short, direct greeting (e.g. "Greetings," or "Hi [first name]," if a contact first name is
     available in the context, otherwise "Greetings,").
  2. Personalized opener (1-2 sentences): the concrete situational detail spotted on their site.
  3. Offer presentation (1-2 sentences): the link between that detail and the recommended
     RuyaTech service, with at most one concrete proof point (case study/figure) if it adds real
     credibility.
  4. Call-to-action (1 sentence): one single clear action (e.g. propose a quick call).
  5. Sign-off + signature (e.g. "Best regards," then a line break, then "{sender_signature}").
  6. After the signature, on its own final line, a short polite opt-out sentence (e.g.
     "If you'd rather not hear from me again, just reply 'no thanks'."). This line is
     MANDATORY in every email — compliance requirement, never skip it.
- 4 to 6 sentences total for blocks 1 to 4 (excluding the signature), in English, direct and
  professional tone, no empty superlatives.
- Never write the body as one continuous block of text — the 5 parts above must stay visually
  separated by line breaks in the "body" field.
- SITUATIONAL personalization only (what the company does/uses/publishes) — never biographical
  (nothing about the person themselves).
- Reuse the hooks already provided rather than inventing new unverified ones.
- ONE SINGLE LANGUAGE throughout the email (subject + body): English. The hooks, quotes, and
  content provided may be in French (scraped from the site): translate and adapt them into
  English in the email, never paste them verbatim in their original language. The final email
  must not contain any word, phrase, or quote in a language other than English.
- Use the RuyaTech proof points (case studies, figures) only if they add real credibility to the
  message — never as filler, never more than one per email.
- One single clear call-to-action, toward the recommended offer.
- Do not invent any fact that is not in the context provided above.
- Respond only with this JSON, nothing else: {"subject": "...", "body": "..."}
  The "subject" field must never be empty. The "body" field must contain the line breaks
  ("\n\n" between each block) that structure the email as described above.
```

**Not enforced in code today (prompt-only):** the ≤ 25-word first line, the 120-word cap, a banned-hype-words list, the "weak hooks → plain template, flagged" fallback. These were in the original spec and are candidates for the next prompt update / code guards.

---

## 9. Stage 6 — Email review & sending

- **Review page** (`/session/<id>/email_review`): every draft with editable subject + body, a checkbox per lead, a live banner polling `/session/<id>/email_job` (generation/sending progress; auto-reload on completion).
- **Send** (`POST /session/<id>/send_emails`): the edited subject/body are captured from the form at click time ("what is sent is exactly what the user saw"), then a background thread sends via the **Gmail API** (`gmail.send` scope, OAuth installed-app flow, `credentials.json` + `token.json`, refresh handled automatically) with **10 s between sends**. Already-sent leads are skipped (no double contact). Per-lead `email_status`, `email_sent_at`, `email_error` recorded.
- One-time Gmail setup: `python setup_gmail.py --url` (prints consent URL) then `python setup_gmail.py <redirect-url>`.
- **Limits of this channel** (for the reviewer): single Gmail identity for all app users, Gmail's ~500/day cap, no domain warm-up, no Instantly/Smartlead export yet.

---

## 10. Cost control & observability (FR-7)

- Table `llm_calls(session_id, lead_id, purpose ∈ score|rescore|email, provider, model, tokens_in, tokens_out, cost_usd, latency_ms, created_at)` — written after **every** LLM call including retries, using the provider's own usage report.
- Price table (USD per 1M tokens, in `costlog.py`): `llama-3.3-70b-versatile` 0.59 / 0.79; `claude-sonnet-4-6` 3 / 15; `claude-haiku-4-5` 1 / 5; `claude-opus-5` 5 / 25. Unknown model → cost NULL, still logged.
- **Hard cap:** `[budget] session_cap_usd` (default **5.0**, 0 = off) — checked **before** each lead; when reached the lead is marked `SCORE_FAILED: budget_exceeded…`, the session is cancelled cooperatively, coverage note written.
- Results page shows per-session spend (calls, tokens in/out, USD).
- Per-lead timings (`scrape_seconds`, `score_seconds`) and `last_error` stored.
- **Coverage notes** (`leads.coverage_notes`, JSON list) — the "nothing silent" record per lead: fetch method per page, escalation ran/skipped and why, LinkedIn harvest outcome, thin/truncated signals, budget skips.
- Live progress: SSE stream `/progress/<session>/stream`; cooperative **Stop** (`POST /session/<id>/cancel`) checked between leads; leads in flight finish.

---

## 11. Configuration, environment, commands

### 11.1 `config.toml` (verbatim)
```toml
[linkedin]
delay_min = 45
delay_max = 180
long_pause_every_min = 15
long_pause_every_max = 20
long_pause_min = 900
long_pause_max = 2400
daily_cap = 50
weekly_cap = 250
post_interval = 7.0
authored_keep = 10
liked_keep = 5
max_posts = 12

[website]
page_timeout = 15
per_domain_delay = 1.0
free_first = true

[budget]
session_cap_usd = 5.0

[fast]
delay_min = 2
delay_max = 4
long_pause_every_min = 1000
long_pause_every_max = 1000
long_pause_min = 3
long_pause_max = 5
bypass_caps = true
```

### 11.2 `.env` variables
| Variable | Role |
|---|---|
| `DATABASE_URL` | **required** — PostgreSQL/Neon connection string (no SQLite fallback) |
| `GROQ_API_KEY` | scoring + emails (default provider) |
| `FIRECRAWL_API_KEY`, `FIRECRAWL_API_KEY_2…_5` | paid fallback scraping (key pool) |
| `SGAI_API_KEY`, `_2…_5` or `SCRAPE_API_KEYS=k1,k2` | ScrapeGraphAI: web search + LinkedIn |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | when a provider is set to `anthropic` (default model `claude-sonnet-4-6`) |
| `SCORING_LLM_PROVIDER`, `EMAIL_LLM_PROVIDER` | `groq` (default) or `anthropic` |
| `RUN_MODE=fast` | test overlay: tiny delays, caps bypassed |
| `PIPELINE_CONCURRENCY` | parallel leads (default 3) |
| `SENDER_NAME`, `SENDER_COMPANY` | email signature |
| `FLASK_SECRET_KEY` | session signing (random per process if unset) |
| `DB_POOL_MINCONN`, `DB_POOL_MAXCONN` | Postgres pool (1 / 8) |
| `PORT` | dev server port (5000) |

### 11.3 Commands
```
pip install -r requirements.txt
python app.py                         # dev server, http://127.0.0.1:5000
gunicorn app:app                      # production (email/scrape jobs run in threads)
python -m pytest tests -q             # 30 offline tests, no DB/API needed
python setup_gmail.py --url           # Gmail OAuth, step 1
python setup_gmail.py <redirect-url>  # Gmail OAuth, step 2
python export.py --db … --scores-out scores.csv   # CLI CSV export
```

### 11.4 HTTP routes
| Route | Purpose |
|---|---|
| `GET /`, `/history`, `/dashboard` | home (upload), session history, tabular dashboard with filters |
| `POST /upload` → `GET /import/<id>` → `POST /import/<id>/start` | upload+dedup → review keepers/duplicates/criteria → launch pipeline |
| `POST /start-analysis` | one-click upload + dedup + pipeline |
| `POST /ingest`, `/dedup`, `/pipeline` | individual steps |
| `POST /analyze-pending/<id>` | run leads that were skipped/failed |
| `GET /progress/<id>`, `/progress/<id>/stream` | live progress page + SSE |
| `POST /session/<id>/cancel`, `/delete` | cooperative stop, delete session |
| `GET /results/<id>` | 5-bucket results + LLM spend |
| `POST /rescore/<id>` | batch re-score |
| `GET /lead/<id>/review`, `POST …/approve`, `POST …/rescore`, `POST …/review` | per-lead review page + actions |
| `POST /session/<id>/prepare_emails`, `GET …/email_review`, `POST …/send_emails`, `GET …/email_job` | email drafting / review / sending / job status |
| `GET /download/scores.csv`, `/download/scraping.csv`, `/download/search.csv`, `/export/<id>/csv|pdf` | exports |
| `GET /batch-results/<id>`, `/web-search/<id>` | last-batch view, web-evidence view |
| `GET|POST /signup`, `/login`, `POST /logout` | auth (first account = admin) |
| `/admin/users…` | list users, change role, block/unblock, delete, per-user history |

### 11.5 Exports
- `scores.csv` columns: `lead_id, first_name, last_name, title, company_name, email, website_url, status, error, is_duplicate, duplicate_reason, segment, confidence, needs_human_review, company_stage, recommended_offer, disqualify_reason, built_with_ai_signals, technical_signals, pain_signals, evidence_quotes, personalization_hooks, scored_at` — exports all non-duplicate leads of the session (not gated on approval), records them in `export_history`.
- `scraping.csv`: one row per scraped page + deterministic signals. `search.csv`: one row per web-search hit. `/export/<id>/pdf`: print-friendly HTML.

---

## 12. Data model (PostgreSQL)
`analysis_sessions` (label, owner_id, status, scoring_criteria, cancelled, last_batch_ids…) · `users` (email, password_hash, role admin|user, is_active) · `leads` (Apollo fields, `linkedin_url`, domains, `domain_mismatch`, status, dedup flags, review_status, email_*, `coverage_notes`, timings) · `lead_content` (one row per scraped page: source, url, content) · `lead_technical_signals` · `lead_scores` (one row per verdict — history kept, latest shown) · `lead_search_evidence` (per source, JSON hits) · `export_history` · `llm_calls` · `li_daily_counter`.

Identity sequences are re-aligned after deletes (freed ids reused). Leads are visible only to their session owner or admins.

---

## 13. Reliability & safety properties (as implemented)
- Per-lead failure isolation: an exception on one lead never stops the batch.
- Every lead ends in a terminal status; failed/pending leads are re-runnable in bulk.
- Re-score never re-spends on scraping or search (stored content + evidence reused).
- Verdict history preserved (`lead_scores` rows accumulate).
- Human gate before sending: draft → editable review → explicit confirm.
- No double contact: `sent` leads skipped; cross-batch domain dedup on export.
- LinkedIn: sequential, human-paced, globally capped, failures don't consume caps, per-lead coverage notes.
- Budget cap stops runaway spend; every LLM call logged.
- Auth: hashed passwords, role checks, blocked users cut off on next request, sessions scoped to owners.
- XSS: dashboard cells HTML-escaped.
- Tests: 30 offline tests (attribution regressions incl. the generalized-owner rule, caps, key rotation, cost/budget, JS-shell detection, config overlay, scorer guards).

---

## 14. Known gaps & honest limitations (for the reviewer)

**Against the original cahier des charges**
1. Instantly/Smartlead-compatible export with `{{first_line}}`-style variables — not built (Gmail is the only send channel).
2. Explicit per-lead Approve/Reject buttons wired to `review_status` — the endpoint exists; the UI uses the auto "Ready to approve" bucket + the per-lead "Approve" (clears the review flag). There is no explicit "Reject".
3. Email constraints from the spec (≤ 25-word first line, ≤ 120 words, banned hype words, weak-hooks → plain-template fallback with flag) are **not enforced in code**; the prompt covers tone but not the numeric limits.
4. Apollo MCP live pull, verified-email flag exclusion, robots.txt respect — not implemented.
5. Scoring default is Groq/Llama; Claude (`claude-sonnet-4-6`, as specified) is available via env switch but not the default.
6. LinkedIn scraping was "Phase 3 / never a dependency" in the spec; it is now an escalation lane (conditional, capped) — a deliberate product decision, documented here for the reviewer.

**Operational**
7. Single shared Gmail identity for all users; Gmail daily caps; no domain warm-up.
8. Website scraping ignores robots.txt.
9. Firecrawl/SGAI free-tier key pools — fragile for 1,000-lead nights without paid keys.
10. Progress/job state lives in process memory (lost on restart; DB statuses survive).
11. LinkedIn provider limitations: activity feed is a non-deterministic recent slice; work history often null; "about" often truncated — all surfaced in coverage notes, none fixable client-side.
12. Open signup: anyone reaching the app can create an account (first account becomes admin).
13. No golden-set regression test for prompt changes: prompt edits are still unmeasured. (Highest-leverage next step: ~30 hand-verified leads re-scored automatically after every prompt change.)

**Questions we'd like the reviewer to answer**
- Is the segment taxonomy and the STRONG/MEDIUM/WEAK signal hierarchy the right decision structure for the persona, or is it over-fitted to "vibe coder" detection?
- Is the two-pass escalation rule (only ambiguous + confident-agency leads get web/LinkedIn evidence) leaving value on the table for confident `ai_solo_founder` verdicts?
- Are the deterministic lists (fingerprints, fonts, CSS patterns, AI-marketing phrases) meaningful signals or noise at 2026 web conventions?
- Is the scorer system prompt too long to be reliable on a 70B open model — what should move from prompt to code?
- Does the email prompt produce emails a founder would actually answer; what's missing in structure, proof usage, CTA, and compliance?
- What would make the "Ready to approve" → draft → send path safe enough for real campaigns at 100–300 leads/batch?
