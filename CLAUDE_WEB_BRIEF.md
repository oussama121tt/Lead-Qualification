# Lead Qualification & Personalization Engine — Full Brief for External Review

Date: 2026-09-09. Repo: `oussama121tt/Lead-Qualification`, branch `main` at commit `c1fbc48` (merge-lead-tool fast-forwarded into main today). Owner: Wael (RuyaTech). Developer: Oussama. Everything below was verified today by reading the code, running the 210-test suite, and executing the modules against the live Supabase Postgres database and the live Apollo API. Nothing is from memory. Where something is unproven, it says so.

---

## 0. What this document is for

You are being asked to review a working system, not a plan. Sections 1 to 3 give the business context and the loop. Sections 4 to 9 are the deep detail: every prompt verbatim, every guard, every governor. Sections 10 to 14 are configuration, schema, commands, tests and the live data state. Section 15 is the blunt list of gaps and the questions we would like your opinion on.

---

## 1. Business context the system encodes

RuyaTech is a technical agency that builds, rescues and scales SaaS products. It sells three things:

| Offer key | Service | Who it is for |
|---|---|---|
| `ai_audit` | Product Rescue & Scale-Up | Non-technical founder whose product was built with AI tools (Cursor, Replit, Lovable, Bolt, v0) and is breaking under real users. Audit, stabilise, refactor, 4 to 8 weeks. Proof point: Bake Genie, relaunched in 2 weeks, 600 paying members 6 months later. |
| `general_audit` | Same service, technical team | Security and architecture audit with prioritised fixes. |
| `pipeline` | AI Agents & Automation | Small agency that is scaling and drowning in ops. Proof point: lead-triage pipeline, 5h/week instead of hours a day, 30K$+ new contracts in 30 days. |

The engine's job: find founders of small, recently founded software companies, prove from evidence which offer fits, write a personalised opener grounded in that evidence, and learn from replies which signals actually convert.

Segment taxonomy (single source of truth is `constants.py`):

- Target: `ai_solo_founder` → ai_audit, `technical_founder` → general_audit, `small_agency_scaling` → pipeline
- Out of target: `too_big`, `wrong_field`
- `unclear` → insufficient evidence, always forces human review. Never used as a polite "no".

Confidence threshold 0.7. Below that, `needs_human_review` is forced in code, not left to the model.

---

## 2. Infrastructure, live

| Component | What is running |
|---|---|
| App | Python 3.12, Flask, gunicorn. Deployed on Render (branch not declared in the repo; verify in the Render dashboard). |
| Database | Supabase Postgres via psycopg2. A thin wrapper (`db._PgConnection`) translates sqlite-style `?` placeholders to `%s`, reconnects dead pooled connections, and rolls back aborted transactions. The tests run the same code on in-memory sqlite. Pooler host `aws-1-eu-west-1.pooler.supabase.com:5432`. The port is blocked on some networks; Cloudflare WARP fixes that. |
| Scoring LLM | Groq, model `openai/gpt-oss-120b` (OpenAI-compatible SDK). Switchable to Claude `claude-sonnet-4-6` with `SCORING_LLM_PROVIDER=anthropic`, no code change. Same switch for email drafting via `EMAIL_LLM_PROVIDER`. |
| Groq keys | Rotation across `GROQ_API_KEY`, `GROQ_API_KEY2`, … with 429 backoff that honours Groq's "try again in Ns". Only ONE key is in `.env` today. The free tier is 8k tokens/min per key, which is the binding constraint on scoring throughput. |
| Lead source | Apollo REST. `/mixed_people/api_search` (free, obfuscated) for search, `/people/bulk_match` (1 credit per person, max 10 per call) for enrichment. `/emailer_messages/search` for outreach outcomes. |
| Web evidence | ScrapeGraphAI (SGAI) `/api/search` and `/api/scrape`. 14 keys in `.env` (`SGAI_API_KEY1` … `SGAI_API_KEY14`) in a ring with per-key cool-downs. |
| Site fetching | Free-first: requests + BeautifulSoup, Firecrawl as paid fallback (one key). |
| Email sending | Single Gmail account through the Gmail API (`gmail_sender.py`). DNC is enforced at send time. Multi-touch sequences are NOT sent by this system; Instantly/Smartlead CSV export is the path for sequences. |
| Auth | Per-user login, admin role, per-session ownership. |

---

## 3. The loop, end to end

```
 Apollo search (free)
   └─ Stage-0 prefilter (deterministic regex, optional Groq pass)
        └─ cross-run dedup (apollo_enriched registry) + DNC check
             └─ credit governor (monthly cap 3600, per-run cap 1000)
                  └─ bulk_match enrichment (1 credit each, verified emails only)
                       └─ insert as a session of leads
                            └─ dedup inside the batch (flags, never deletes)
                                 └─ website fetch + deterministic signals (+ public surface scan)
                                      └─ Pass-1 LLM score (site only)
                                           └─ escalate? → SGAI web search + founder LinkedIn lane → Pass-2 score
                                                └─ code guards (grounding, attribution, confidence)
                                                     └─ human review (results page / keyboard queue)
                                                          └─ email draft (hooks or reviewer override)
                                                               └─ Ship: capacity-scheduled Instantly CSVs + DNC + export_history
                                                                    (or send now via Gmail, DNC-checked)
                                                                         └─ nightly Apollo outcomes sync → lead_outcomes
                                                                              └─ analytics: signal→outcome lift, $/reply, channel comparison
 Weekly trigger monitor re-checks every scored lead for readiness changes and re-queues it with a fresh hook.
```

Each arrow is a real function. The rest of this document walks them in order.

---

## 4. Module map (13,933 lines of application code)

| Module | Lines | Role |
|---|---|---|
| `app.py` | 2466 | Flask routes, background jobs, campaign/analytics/capacity views |
| `db.py` | 2077 | Schema, migrations, all queries, PG wrapper |
| `scraper.py` | 1546 | Site fetching, key-page discovery, deterministic signals, third-party tagging, SGAI search |
| `triggers.py` | 1070 | Trigger monitoring (10 checks, two credit governors, two-phase locked runner) |
| `scorer.py` | 837 | Prompt assembly, LLM call, verdict normalisation and guards |
| `export.py` | 684 | Scores / scraping / search CSVs, Instantly export with `{{first_line}}` tag |
| `pipeline.py` | 654 | Per-lead orchestration, two-pass scoring, escalation, cost logging |
| `analytics.py` | 519 | Signal families, attribution with lift, cost per outcome, channel comparison |
| `linkedin_lane.py` | 477 | Founder profile harvest: pacing, caps, post attribution |
| `apollo_analytics.py` | 427 | Outcomes sync from Apollo, engagement enrichment, fold into lead_outcomes |
| `surface_scan.py` | 385 | 8 GET/HEAD-only public-surface checks written to lead_public_findings |
| `apollo_client.py` | 313 | Search, bulk_match, credit governor, enriched registry, person→lead mapping |
| `runconfig.py` | 245 | Typed config loader for config.toml |
| `prefilter.py` | 225 | Stage-0 rules and headcount parsing |
| `capacity.py` | 223 | Pure send-capacity model (ramp, forecast, max new contacts, batch scheduling) |
| `sourcing.py` | 191 | The enforced sourcing order |
| `recipes.py` | 188 | Saved Apollo filter recipes, versioned, with yield counters |
| `llm_provider.py` | 187 | Groq / Anthropic providers, key rotation, RateLimited |
| `emailer.py` | 149 | Email prompt and draft |
| `dnc.py` | 120 | Do-not-contact registry |
| `dedup.py`, `costlog.py`, `campaigns.py`, `keyring.py`, `caps.py`, `sgai_client.py`, `gmail_sender.py`, `throttle.py`, `site_fetcher.py`, `constants.py` | < 130 each | Supporting pieces |

Tools in `tools/`: `run_bulk_sourcing.py`, `export_evaluation.py`, `run_recipes.py`, `run_triggers.py`, `run_outcomes_sync.py`, `run_recipe_outcomes.py`, `run_golden.py`, `run_golden_live.py`.

---

## 5. Sourcing layer (Apollo)

### 5.1 Enforced order in `sourcing.run_recipe`

1. Search, free, paginated, up to `max_people_per_run` (500). If the recipe does not set `contact_email_status`, `["verified"]` is added at search time. This came from the first run where 52 percent of enriched people had no usable email.
2. Stage-0 prefilter (section 5.3). Dry-run stops here and reports what would be enriched.
3. Cross-recipe dedup by Apollo person id, both in-run (`seen_ids`) and persisted (`apollo_enriched` table). The same person is never enriched twice, ever.
4. DNC check against `do_not_contact` by email and domain.
5. Credit governor: `check_credit_budget(needed, monthly_cap)` raises `ApolloCreditCapReached` before any spend. Per-run cap 1000, monthly cap 3600, counted in `apollo_usage(month, credits_used)`.
6. Enrichment via `bulk_match`, 10 people per call, only survivors.
7. Insert as a new analysis session; `apollo_id`, `apollo_person`, `apollo_org`, `apollo_email_status` are stored on the lead row for provenance.
8. Recipe yield counters updated (`runs`, `leads_pulled`, `qualified`, `enriched`).

### 5.2 Apollo facts learned live

- `/mixed_people/search` returns 403 API_INACCESSIBLE on this plan. `/mixed_people/api_search` is the working endpoint. This was an endpoint rename, not a key-scope problem.
- Search results are obfuscated (no email) but carry organisation headcount, which the team-growth trigger reads for free.
- `/emailer_messages/search` returns messages with `to_email`, `status`, `created_at`, `completed_at`, `emailer_campaign_id`, `replied` (only on replied messages), and NO opened/clicked fields. Filtering with `emailer_message_stats[]=opened|clicked|replied|bounced` returns exactly the messages in that state. Verified today: 148 opened, 18 clicked, 6 replied, 8 bounced across the account's last 500 messages.

### 5.3 Stage-0 prefilter (deterministic, free)

Decision per person is `keep`, `reject` or `unclear`. Unclear is kept unless the optional Groq pass is enabled (`[prefilter].use_llm`, off today).

Reject rules, in order:
- Company name matches `AGENCY_COMPANY_MARKERS`:
  `\b(agency|agencies|consult(?:ing|ancy|ants?)?|labs?|solutions|software\s+house|digital\s+agency|dev\s?shop|web\s+design|it\s+services|systems?\b|systems\s+integrat|outsourc|technolog(?:y|ies)\s+partner|interactive|creative\s+agency|staffing|recruit(?:ing|ment)\s+agency|we\s+build|development\s+(?:company|partner|services)|mvp\s+(?:development|studio|agency)|app\s+development|software\s+development)\b`
  Note: `studio` was deliberately removed. A product studio is a target (golden case george).
- Company name matches `DEV_SHOP_NAME_PATTERN`: `^[\w&.'\- ]{1,40}\s+(software|technologies|technology|tech|systems|digital|it)\s*(inc|llc|ltd|limited|pvt|pty|gmbh|co)?\.?$`
- Title matches `AGENCY_TITLE_MARKERS`:
  `\b(agency\s+owner|freelance|freelancer|consultant|contractor|fractional\s+(?:cto|cpo|coo|cmo|cfo)|advisor|mentor|coach|managing\s+director\s+at\s+.*\bagency\b|we\s+build\s+(?:your|apps|mvps|software|products))\b`
  Plain CTO, engineer and technical co-founder titles are NOT rejected: a technical founder of their own product company is the `technical_founder` segment (golden cases marius, eric).
- Title matches `NON_DECISION_TITLE_MARKERS`: `\b(intern|student|assistant|recruiter|talent|hr\b|human\s+resources|sales\s+(?:rep|representative|development)|sdr\b|bdr\b|account\s+executive|support|customer\s+success|bookkeeper|accountant|receptionist)\b`, unless a `FOUNDER_TITLE_MARKERS` word is also present (`founder|co-?founder|ceo|owner|managing partner|president|chief executive`).
- Headcount above `max_headcount` (20). Apollo returns headcount as int, `"11-50"`, `"11,50"`, `"5000+"` or `"1,001-5,000"`; ranges parse to their LOWER bound. The old parser read `"11-50"` as 1150 and rejected target companies.

Optional LLM pass prompt (`LLM_PREFILTER_SYSTEM`):
> You are a fast pre-filter for a B2B lead list. Given only a person's title, company name, headcount and founded year (no website), decide if they are plausibly a NON-TECHNICAL or technical FOUNDER of a small software product company (our target), or clearly NOT (agency, consultancy, dev shop, fractional CTO, enterprise employee, recruiter, unrelated sector). Respond ONLY as JSON: {"decision": "keep"|"reject", "reason": "..."}. When genuinely unsure, keep — a wrong reject loses a good lead.

Golden gate: `golden/stage0_apollo.json` holds 12 good targets and 9 known-bad; `tests/test_stage0_golden.py` asserts Stage-0 never kills a good target and rejects every known-bad.

### 5.4 Recipes and the bulk set

`apollo_recipes` stores named filter JSON with yield counters. Every filter edit creates a new row in `apollo_recipe_versions` and freezes the old version's cumulative yield, so a filter edit that tanks yield is visible.

`golden/bulk_recipes.json` is the work-order set. Base filters merged into every recipe:

```json
{"person_titles": ["founder","co-founder","ceo","owner"],
 "organization_num_employees_ranges": ["1,10","11,20"],
 "organization_founded_year_range": {"min": 2020, "max": 2026},
 "person_locations": ["United States","United Kingdom","Canada","Australia","Ireland","New Zealand"],
 "contact_email_status": ["verified"]}
```

40 narrow `q_keywords` (health app, patient app, care platform, therapy platform, clinic app, telehealth app, hr platform, employee app, payroll app, recruiting platform, workforce app, property app, tenant app, landlord platform, rental app, booking app, scheduling app, marketplace app, payments app, invoicing app, childcare platform, kids app, parenting app, student app, school platform, tutoring app, fitness app, nutrition app, wellness platform, veterinary app, pet care app, events app, ticketing app, membership app, legal platform, document platform, compliance app, salon app, restaurant app, delivery app) run first. 6 broad sweeps (app, mobile app, consumer app, platform, saas app, web app) run after with `broad_max_people` 1500. The founded-year band 2020 to 2026 was chosen after a free plan pass showed older companies were mostly agencies.

47 recipes exist in the live database.

---

## 6. Evidence gathering per lead

### 6.1 Website (`scraper.scrape_website`, free-first)

Homepage fetched with requests; Firecrawl only if the free fetch fails or the page is JS-heavy. Key pages discovered by link keywords then standard paths: about, product, pricing, careers. Broken and duplicate pages dropped. Careers and pricing pages are reduced to compact deterministic signal blocks (hiring_technical, engineering keywords, has_visible_price, pricing_motion) rather than raw text.

Third-party content tagging (`_tag_attributed_content`) marks testimonials and quoted sections as `[ATTRIBUTED QUOTE …]` or `[THIRD-PARTY CONTENT SECTION …]` so the model cannot mistake a client's words for the founder's. The hook guard later rejects any hook grounded inside such a block unless the attribution names the founder.

Deterministic technical signals (`lead_technical_signals` table): `app_builder_fingerprint` (Bubble, Lovable, Bolt, Replit, v0, Glide…), `site_builder_fingerprint` (Framer, Webflow, Wix, Squarespace, Carrd), `on_builder_subdomain` + which builder, generator meta tag, vibe-language matches, trend fonts, visual patterns, AI-style phrase density, explicit AI-authorship disclosures, GitHub repo URL + single-commit check, traction signals.

Public surface scan (`surface_scan.py`, enabled): 8 GET/HEAD-only checks against the site's public surface, per-check isolation, body cap, findings capped at 8 rows per lead in `lead_public_findings` with severity and evidence excerpt.

### 6.2 Web escalation (SGAI search, paid)

Queries, 2 results each, one thread per key:

```
linkedin        : "{company}" site:linkedin.com/in OR site:linkedin.com/company
product_hunt    : "{company}" site:producthunt.com
twitter         : "{company}" (site:twitter.com OR site:x.com) (vibe coded OR built with AI OR built in a weekend)
github          : "{company}" site:github.com
interviews      : "{founder}" OR "{company}" interview (vibe coding OR built with AI OR built with Cursor OR built with v0)
person_linkedin : "{founder}" site:linkedin.com/in
person_github   : "{founder}" site:github.com
```

`{founder}` is the first founder-name candidate found on the site, else the CSV name. Site names outrank CRM fields because a placeholder name once pulled a stranger's profile. If the site links its own LinkedIn company page, that URL is scraped directly and the name search is skipped. A person URL from the site is trusted only when there is exactly one `/in/` link.

LinkedIn scrape extraction prompts:
- Company page: "Extract company name, description, headquarters, industry, company size, number of employees, specialties, website, and founders"
- Person page (fallback): "Extract the person's name, current roles and company, work experience, education, skills, and whether they are a founder, CTO, or engineer"

### 6.3 Founder LinkedIn lane (`linkedin_lane.harvest_founder_profile`)

Used when a founder profile URL is known. Global caps 50/day, 250/week persisted in `li_daily_counter`. Random delay 45 to 180 s between profiles, a 15 to 40 minute pause every 15 to 20 profiles, sequential process-wide. Profile scraped with stealth fetch config, auth-wall detected. From the activity feed: all posts whose permalink handle matches the owner, plus at most 5 liked posts, cap 12 total, each fetched for full text with 7 s spacing. Attribution is done in code: handle match or "shared by owner" → AUTHORED, else LIKED with original author. Bio posts are force-kept past the cap. Output to the scorer as `person_linkedin` hits titled "AUTHORED LinkedIn post (written by the founder themselves — first-party evidence)" or "Liked/reposted LinkedIn post (original author: X — weak association only)". All web evidence is persisted in `lead_search_evidence` and reloaded on re-score, so a re-score never re-spends.

SGAI key ring: 429 → retry after 5 s then 20 s then cool 30 s; 401/402/403 → cool 1 hour; 5xx → cool 30 s; all keys cooling → wait up to 120 s then `AllKeysExhausted`, noted on the lead, snippet fallback.

---

## 7. Scoring (`scorer.py`, `pipeline._process_lead`)

### 7.1 System prompt, verbatim

```
You are a senior B2B lead analyst for RuyaTech. Use only supplied Apollo metadata,
official site content, web evidence, and verified deterministic signals.
OFFERS: ai_audit is for an AI-built product owned by a non-technical founder; general_audit is
for a technical team needing architecture or security review; pipeline is for a scaling agency.
Choose exactly one segment: ai_solo_founder, technical_founder, small_agency_scaling, too_big,
wrong_field, or unclear. Map those segments to ai_audit, general_audit, pipeline, none, none,
and normally none. Unclear means insufficient evidence, not wrong_field or too_big.
STRONG signals are app_builder_fingerprint, explicit AI authorship, or a verified single-commit
GitHub repository combined with an app builder. site_builder_fingerprint (Framer/Webflow/Wix/
Squarespace/Carrd) is metadata only and never changes the segment. on_builder_subdomain=true is
near-proof of AI-build and early stage. MEDIUM signals are explicit vibe-language or high AI-style
density. Treat isolated evidence cautiously. Site and web evidence have equal weight; person_*
evidence describes the founder. Cursor alone never proves ai_solo_founder.
Describe the analyzed company, never clients or testimonials. Content marked [ATTRIBUTED QUOTE ...]
or [THIRD-PARTY CONTENT SECTION ...] is third-party unless its attribution names the analyzed
founder. Cite non-deterministic signals with exact evidence_quotes. Hooks are situational, never
biographical, and each must be {"hook":"...","based_on":"exact quote"}. Use only supplied content;
demos, product AI features, and client capabilities do not prove AI construction. Never invert a
capability into pain. Confidence below 0.7 requires needs_human_review=true. Decide in order:
enough evidence, AI-built non-technical team, technical team, scaling agency, too_big, wrong_field,
otherwise unclear.
Identify sensitive categories only when stated or clearly implied: minors, health_phi, biometric,
payments, identity_documents, financial, legal, location, employee_data, none. Set
sensitive_data_categories to a list of those exact keys and data_sensitivity_score from 0 to 100
for breach impact; use [] and 0 when none.
Set budget_signal to strong, moderate, weak, or none. Record paid pricing, hiring, funding, exits,
or enterprise logos in budget_evidence. Record nonprofit funding, student founder, side project,
default builder subdomain, or shrinking headcount in budget_blockers. A strong blocker caps the
budget signal at weak.

Respond ONLY with JSON using EXACTLY these keys (no others, no renaming):
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
  "sensitive_data_categories": [],
  "data_sensitivity_score": 0,
  "budget_signal": "strong | moderate | weak | none",
  "budget_evidence": [],
  "budget_blockers": [],
  "disqualify_reason": null,
  "needs_human_review": false
}
```

History that matters: a "prompt diet" to 330 words dropped the JSON schema block. gpt-oss-120b then returned its own key names, and 353 leads were stored with confidence 0.0. The schema was restored and a key normaliser added (`_normalize_verdict_keys` maps aliases such as `hooks` → `personalization_hooks`). The offline golden harness did not catch this because it mocks the LLM.

### 7.2 User message layout, blocks joined by `\n\n---\n\n`

1. Contact metadata (name, title, company, email, website).
2. Site content: `## Source: homepage|about|product|pricing|careers` blocks, 12,000 char cap. Careers and pricing appear as compact deterministic blocks.
3. Web evidence, `person_*` sources first, 12,000 char cap.
4. Site-missing instruction when no usable site content: base the verdict on metadata and web evidence only, do not treat absence as a signal, and set needs_human_review true regardless of confidence.
5. User-selected scoring criteria from the import-review page (optional), with fixed descriptions per criterion plus any custom text.
6. Deterministic signals JSON with the instruction "do not re-derive, do not invent beyond what follows".

### 7.3 Code-level guards on every verdict, in order

1. `confidence < 0.7` → `needs_human_review = true`.
2. Invalid segment → forced `unclear`; invalid offer → `none`; any forced correction caps confidence at 0.3.
3. `segment == unclear` → `needs_human_review = true` (enforced in code since Sept 6).
4. Evidence grounding: every `evidence_quotes` entry must appear verbatim in the site + web text after whitespace, case and wrapping-quote normalisation (`_normalize_for_grounding` strips straight and curly quotes). Ungrounded quotes are removed and counted in a coverage note.
5. Hook grounding: each hook must be `{hook, based_on}` with `based_on` found verbatim and NOT inside a third-party block attributed to someone other than the founder. Failing hooks are discarded and counted.
6. Site-missing guard: no site content → review forced.
7. `domain_mismatch` (email domain vs website) → review forced with "this verdict may describe the wrong company".

### 7.4 Two-pass scoring and escalation

- Pass 1: site content + metadata + deterministic signals. No web credits.
- Escalation mode is `high_only` with `min_confidence 0.8` (config `[escalation]`): web evidence is bought only for leads that pass 1 already rates in a target segment at or above 0.8, so SGAI spend goes to leads worth proving, not to rescuing weak ones. The original rule (escalate when review needed or confidence under 0.7 or agency+hiring) is still in code as the alternative mode.
- Pass 2: same prompt plus the web evidence block. If pass 2 throws, pass 1 is kept with a note.
- Rate limits are retryable failures, not verdicts: `RateLimited` propagates and the lead goes to `SCORE_FAILED`, which the re-score path picks up. Previously the rate-limit fallback was stored as a real verdict with confidence 0.

Every LLM call is logged to `llm_calls` with purpose (`prefilter`, `score`, `rescore`, `email`), provider, model, tokens and cost from `costlog.MODEL_PRICES` (gpt-oss-120b at $0.15/$0.60 per million, claude-sonnet-4-6 at $3/$15).

Coverage notes: every skipped or degraded step writes a human-readable note on the lead. The rule is "nothing silent".

---

## 8. Email drafting (`emailer.py`)

Hooks source: `hook_override` (set by a reviewer in the keyboard queue) if present, else the scorer's `personalization_hooks`. Sender signature is `"{SENDER_NAME} — {SENDER_COMPANY}"` from env. The prompt template has not been changed since the owner asked for it to be left for their own update. Verbatim:

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

Instantly export: the drafted opener is extracted into a `first_line` column and replaced in the body by the literal merge tag `{{first_line}}`, so the sending tool substitutes it once instead of the opener appearing twice. Columns: email, first_name, last_name, company_name, website_url, linkedin_url, segment, recommended_offer, first_line, email_subject, email_body, hooks_used. Only APPROVED, non-duplicate leads export. Every exported lead is written to `do_not_contact` and `export_history`.

---

## 9. Trigger monitoring, campaigns, capacity, analytics (built Sept 6 to 8, audited and fixed Sept 9)

### 9.1 Triggers (`triggers.py`), currently `enabled = false`

Ten checks, each producing a snapshot state and possibly one event with a ready-made hook:

| Check | Priority | Hook | Cost |
|---|---|---|---|
| hiring_engineer | 4 | "Saw you're hiring an engineer" | free fetch |
| funding_announced | 4 | "Saw your funding news" | SGAI search, governed |
| founder_posts_pain | 4 | "Saw the founder posting about a pain point" | LinkedIn lane, only for leads with confidence ≥ 0.8 in a target segment and not flagged for review |
| pricing_introduced | 3 | "Saw you added a pricing page" | free fetch |
| custom_domain_move | 3 | (builder subdomain → own domain) | free fetch |
| app_store_launch | 3 | "Saw your app live on the App Store" | free fetch |
| product_hunt | 3 | "Saw you launched on Product Hunt" | SGAI search, governed |
| team_growth | 3 | "Saw your team grew" (headcount up by ≥ 3) | Apollo free search, paused when Apollo is over its monthly cap |
| compliance_page | 2 | "Saw you added a compliance page" | free fetch |
| enterprise_logo | 2 | "Saw a new named customer" | free fetch |

Design points that were verified by execution today:
- Baseline-first: the first observation of a lead stores state and never fires. A change fires exactly once; the same state again does not; a page that goes 404 then returns re-fires (True→False→True).
- Two phases per lead. Phase A runs all network checks with no lock and memoises every observation. Phase B re-reads the row under `SELECT … FOR UPDATE`, diffs the cached observations against the fresh snapshot, writes events, trigger fields, hook injection, snapshot and `next_check_at` (+7 days) in one transaction, and rolls back on any error. Two overlapping scheduler runs cannot double-fire.
- Exactly one trigger-sourced hook ever lives in the lead's `personalization_hooks` (`db.inject_trigger_hook` removes the previous one).
- Per-check isolation: a throwing check never kills the run. Per-lead isolation in the scheduler now counts and names failures (`summary.failed`, `summary.errors`) instead of skipping silently.
- Two independent credit governors: Apollo (`apollo_usage`) for team_growth, SGAI (`sgai_usage`, 1 nominal credit per governed search per lead per run) for funding and Product Hunt. The SGAI governor now reserves the credit before searching so it pauses at exactly the cap. The cap value 1000 is a placeholder pending the real SGAI quota.
- Trigger events show on the lead review page and as a pill on results; the review queue is ordered by trigger priority when `reorder_queue` is true.

### 9.2 Campaigns and the keyboard review queue

A campaign wraps one sourcing session: `campaigns(id, recipe_id, session_id, name, status reviewing|shipped, ship_plan)`. Start from a recipe → background sourcing run → its session becomes the queue. The queue page (`/campaign/<id>/review`, template committed today) shows one lead at a time: A approve, X reject, E edit hook then Enter to save, arrows to skip and go back. Each decision is one POST returning the true remaining count; no reloads. A saved hook is not a decision, so the reviewer can write the opener and then press A.

Ship (`/campaign/<id>/ship`) is NOT a send. It takes the APPROVED leads, asks the capacity model to split them into per-day batches the mailbox fleet can absorb, stores the plan on the campaign, records every lead in DNC and export_history, and regenerates one Instantly CSV per batch on demand. A campaign ships once.

### 9.3 Send-capacity planner (`capacity.py`, `/capacity`)

Pure model. Mailboxes have a daily cap and an optional linear warm-up ramp. A sequence is a tuple of day offsets, default (0, 3, 7). Every new contact books one slot per touch on its future dates, so the sustainable new-contact rate is roughly total daily capacity divided by touch count: with one mailbox at 30/day that is 10 new contacts per day. `max_new_contacts` checks the bound on every future date the follow-ups land on, not just today. Already-sent leads (`email_sent_at`) and already-shipped campaign plans are both counted as committed slots, so a second campaign sees the first one's reservations. Verified today: a 100-lead ship produced ten batches of ten with zero collisions on replay.

Caveat for the reviewer: nothing in this system sends touches 2 and 3 yet. The model is ahead of the sender.

### 9.4 Outcomes sync and analytics

Nightly `tools/run_outcomes_sync.py --days 1` (or the Sync button on `/analytics`) pulls Apollo outreach messages, drops unsent statuses, keeps the window, saves raw rows to `apollo_analytics_sync_report` (unique on month + message id, so re-runs are idempotent), then sweeps the four engagement filters and flags rows by message id (added today; without it opens and clicks were permanently zero). Rows are folded into `lead_outcomes` by lower-cased email with a COALESCE upsert that never overwrites a manually entered meeting, close or revenue.

`analytics.py`:
- `signal_families(row)` turns a lead's scorer JSON and wide fingerprint columns into (family, value) pairs: segment, budget, technical (including `app_builder:bubble`, `site_builder:webflow`, `on_builder_subdomain:yes`), pain, sensitive_data, plus `trigger` families from events.
- `attribution()` per signal: leads, sent, replies, reply rate, lift versus the overall baseline. Lift is reported only with at least 10 sends; below that the UI says "not enough data".
- `cost_per_outcome()` per recipe, segment and channel: LLM cost from `llm_calls`, scrape estimate, operator review time, Apollo credits (exact per recipe, apportioned by lead share per segment and channel), then $/reply, $/close, net revenue.
- `channel_comparison()` one row per channel with cycle length (days from session creation to first send). Channels are set per session.

Run today against the live database: attribution 0.7 s, cost 1.0 s, channels 0.7 s, all correct.

---

## 10. Configuration (`config.toml`, effective values)

```toml
[linkedin]   delay_min=45 delay_max=180 long_pause_every_min=15 long_pause_every_max=20
             long_pause_min=900 long_pause_max=2400 daily_cap=50 weekly_cap=250
             post_interval=7.0 authored_keep=10 liked_keep=5 max_posts=12
[website]    page_timeout=15 per_domain_delay=1.0 free_first=true
[budget]     session_cap_usd=5.0
[surface_scan] enabled=true timeout=10 per_domain_delay=1.0 max_findings=8
[triggers]   enabled=false interval_days=7 max_leads_per_run=50 high_value_confidence=0.8
             team_growth_delta=3 reorder_queue=true sgai_monthly_credit_cap=1000  # placeholder
[fast]       (test overlay: tiny delays, bypass_caps=true)
[apollo]     monthly_credit_cap=3600 search_page_size=100 max_people_per_run=500
             require_verified_email=true run_credit_cap=1000
[prefilter]  enabled=true use_llm=false max_headcount=20 min_headcount=0
[escalation] mode="high_only" min_confidence=0.8
[sending]    mailboxes=[{name="primary", daily_cap=30}] sequence_offsets=[0,3,7]
[costs]      apollo_credit_price_usd=0.05 scrape_price_usd=0.01
             operator_rate_usd_hour=30.0 review_minutes_per_lead=5
```

Environment variables read by the code: `DATABASE_URL`, `DB_POOL_MINCONN/MAXCONN`, `APOLLO_API_KEY`, `GROQ_API_KEY` (+ numbered), `GROQ_SCORING_MODEL`, `GROQ_EMAIL_MODEL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `SCORING_LLM_PROVIDER`, `EMAIL_LLM_PROVIDER`, `SGAI_API_KEY` (+ numbered) or `SCRAPE_API_KEYS`, `FIRECRAWL_API_KEY`, `SENDER_NAME`, `SENDER_COMPANY`, `FLASK_SECRET_KEY`, `FLASK_ENV`, `RUN_MODE`, `PIPELINE_CONCURRENCY`, `PORT`, `SUPABASE_ANON_KEY` (used only as a REST fallback when the Postgres port is blocked).

Keys present today: 14 SGAI, 1 Groq, 1 Firecrawl, 1 Apollo, no Anthropic.

---

## 11. Database (Supabase Postgres, 21 tables, migrated today)

Core:
- `analysis_sessions` (label, status, created_at, channel, owner_id)
- `leads` (41 columns): identity and contact fields, `domain_normalized`, `domain_mismatch`, `status` state machine, dedup flags, `review_status` APPROVED|REJECTED, `review_segment_override`, email draft fields (`email_subject`, `email_body`, `email_status`, `email_sent_at`, `email_error`), `linkedin_url`, `coverage_notes`, Apollo provenance (`apollo_id`, `apollo_person`, `apollo_org`, `apollo_email_status`), trigger fields (`next_check_at`, `trigger_state`, `trigger_priority`, `trigger_hook`), `hook_override`
- `lead_scores` (one row per scoring pass, latest wins by max id): the full verdict schema
- `lead_technical_signals` (19 columns of deterministic signals)
- `lead_content` (fetched pages), `lead_search_evidence` (web hits, reloaded on re-score), `lead_public_findings` (surface scan)
- `llm_calls` (cost ledger), `export_history`, `users`, `li_daily_counter`

Sourcing and governance:
- `apollo_recipes`, `apollo_recipe_versions`, `apollo_usage(month, credits_used)`, `apollo_enriched(apollo_id, email, domain, lead_id, session_id, enriched_at)`, `do_not_contact(email, domain, reason, added_at)`, `sgai_usage`

Outreach and learning:
- `campaigns`, `lead_outcomes` (unique per lead: sent_at, opened, clicked, replied, reply_sentiment, meeting_booked, closed_won, revenue, channel, recipe_id), `lead_trigger_events`, `apollo_analytics_sync_report` (raw messages, unique month + message id)

Lead status machine: NEW → PARSED → FETCH_PARTIAL | FETCH_FAILED → SCORED | LOW_CONFIDENCE | SCORE_FAILED; RESCORE_PENDING → RESCORE_FAILED. `NOT_YET_SCORED_STATUSES` gates what the trigger scheduler and analytics consider.

---

## 12. Commands

```
# Bulk sourcing work order
python tools/run_bulk_sourcing.py --plan                 # free search + prefilter, spends 0, writes plan
python tools/run_bulk_sourcing.py --enrich narrow|broad|all   # credit-gated enrichment
python tools/run_bulk_sourcing.py --score --concurrency 3
python tools/run_bulk_sourcing.py --status
python tools/export_evaluation.py                        # leads_core.csv, leads_content.csv, prefilter_rejects.csv, run_summary.md

# Scheduled jobs (cron / Render cron)
python tools/run_recipes.py --all [--dry-run]            # every saved recipe through run_recipe
python tools/run_outcomes_sync.py --days 1               # nightly Apollo outcomes → lead_outcomes
python tools/run_triggers.py [--dry-run] [--limit N]     # weekly trigger monitor (needs [triggers].enabled=true)
python tools/run_recipe_outcomes.py --recipe 5 --sent 12 --replies 2   # manual per-recipe outcome entry

# Quality gates
python -m pytest -q                                      # 210 tests, ~3 s, no network
python tools/run_golden.py                               # 15 golden cases, mocked LLM
python tools/run_golden_live.py                          # same cases against the real LLM
```

Web routes worth knowing: `/` upload, `/import/<id>` review criteria then start, `/results/<id>`, `/lead/<id>/review`, `/session/<id>/prepare_emails`, `/session/<id>/email_review`, `/session/<id>/send_emails`, `/download/instantly.csv`, `/sourcing`, `/campaigns/new`, `/campaign/<id>`, `/campaign/<id>/review`, `/campaign/<id>/ship`, `/capacity`, `/analytics`, `/analytics/costs`, `/analytics/channels`, `/admin/users`.

---

## 13. Tests and what is actually proven

210 tests in 31 files, 209 pass, 1 skipped (a Playwright browser test of the keyboard queue that needs Chromium). Runs in 3 seconds with no network. Coverage by area: triggers 14, capacity 12, campaign flow 12, analytics 14 + 10 route tests, outcomes sync 10, prefilter 18 + 4 golden, sourcing order 4, recipes 4, export 11, DNC send 3, SGAI governor 5, audit regressions 6, plus the original scoring, grounding, dedup and config tests.

How the tests are built: most run against real in-memory sqlite with hand-built schemas; route tests stub at the module boundary; the LLM is always mocked. That last point is the known blind spot: it let the prompt-diet regression and the rate-limit-as-verdict bug through, and it cannot catch payload-shape drift from Apollo. Today's audit therefore exercised the code against live services, and that is where the remaining two production bugs were found (Postgres cursor not iterable; opens/clicks absent from the Apollo payload). Both are fixed and covered.

Proven live today: schema migration on Supabase, Apollo search and stats filters, the outcomes sync backfill, all three analytics computations, the trigger runner on adversarial sequences, the capacity planner on edge cases.

Not proven live: the trigger monitor against real websites at scale (it is switched off), a real multi-touch send, Apollo sequence enrolment (not built), the queue page in a real browser.

---

## 14. Live data state, 2026-09-09

| Item | Value |
|---|---|
| Leads in database | 506 across 13 sessions, all sourced from Apollo with verified emails, US/UK/CA/AU/IE/NZ, founded 2020 to 2026, headcount 1 to 20, founder/CEO/owner titles |
| Apollo credits used this month | 542 of 3600 |
| People in the never-re-enrich registry | 508 |
| Saved recipes | 47 |
| Leads with a usable verdict | 18 (12 technical_founder avg confidence 0.72, 4 wrong_field 0.89, 1 small_agency_scaling 0.75, 1 ai_solo_founder 0.90) |
| Leads scored `unclear` with confidence ≈ 0 | 381, of which 355 at exactly 0.0. These are the rate-limit casualties from the first run and need re-scoring. Status LOW_CONFIDENCE 395, NEW 107, SCORED 4. |
| Email drafts | 0 |
| Review decisions | 0 |
| Do-not-contact entries | 0. The owner still owes the list of 55 people contacted before this system. Nothing should be exported or sent until it is loaded. |
| Outcomes | 19 leads matched to Apollo outreach history, 6 opened, 0 replied. 381 of the 400 synced messages belong to older campaigns not in this database. |
| Trigger events | 0 (monitor off) |
| LLM spend logged | 72 calls, about $0.07 |

A colour-coded evaluation sheet of all 506 people with their current scores is at `exports/leads_with_scores.xlsx` and the Toumi folder root.

---

## 15. Gaps, decisions pending, and questions for the reviewer

Blunt list, most important first.

1. **462 leads need re-scoring, and the model is the decision.** Groq's free tier at 8k tokens/min per key makes a 500-lead pass take hours with one key and still risks garbage. Options: add 4 or 5 Groq keys (rotation already works), or set `SCORING_LLM_PROVIDER=anthropic` with an Anthropic key (about $0.02 to $0.04 per lead on Sonnet, no rate-limit problem, likely better grounding). Our recommendation is Anthropic for scoring and Groq for the cheap prefilter pass. Question for you: do you agree, and would you run the 15 golden cases on both before choosing?
2. **DNC list not loaded.** 55 previously contacted people. Blocks any export or send.
3. **Email prompt untouched pending the owner's rewrite.** The template above is the original. The scorer's hook grounding is strict; the email prompt is not yet told to prefer the `first_line` structure the Instantly export expects. Worth a look.
4. **Ship is not a send.** Touches 2 and 3 exist only in the capacity model and in the exported CSV. Either wire Instantly/Smartlead as the real sequence sender (CSV upload or API) or build Apollo sequence enrolment. This is the biggest functional gap between the model and reality.
5. **Single mailbox.** The fleet model is ready; only `primary` at 30/day is configured. Warm-up ramp fields exist.
6. **Triggers are off.** Turning them on costs nothing on Apollo, up to 50 SGAI searches per run for funding/Product Hunt, and one LinkedIn harvest per high-value lead. The first pass only stores baselines. The SGAI cap value is a placeholder.
7. **Analytics has never seen a real reply for a lead in this database.** The math is tested on synthetic data and runs on live data, but calibration is unknown until a campaign goes out.
8. **Render deploy branch unknown.** If Render tracks main, today's fast-forward was a deploy; the schema migration has already been run, so it should come up clean.
9. **Code health.** `triggers.py` is 1,070 lines with the lowest maintainability band in the codebase; `run_checks_for_lead` has cyclomatic complexity 21. Readable thanks to docstrings, but the next change there will be slow. The Postgres-only DDL in campaigns, outcomes and the sync report is stubbed in tests; it has now been run once for real.
10. **Escalation mode `high_only` is a bet.** It saves SGAI credits by proving only already-strong leads. The cost is that a lead sitting at 0.6 with a thin site never gets web evidence that might have lifted it. Worth revisiting once the analytics show which segment converts.

Questions we would value your view on:
- Is the segment taxonomy right for the three offers, or is `small_agency_scaling` too narrow for the `pipeline` service?
- The system prompt is about 450 words plus the schema. Would you restructure it, and if so what would you drop?
- Given 40 narrow verticals and 6 broad sweeps, which verticals would you prioritise for the first 200 sends, and why?
- Is "reply implies send" the right rule for attribution, or should a reply without a send marker be excluded entirely?

---

## Appendix: other documents in the repo

`SYSTEM_OVERVIEW.md` (original deep description of stages 1 to 4 with all extraction rules), `STATUS_NOW.md` (status as of Sept 6), `VOLUME_READINESS.md` (the sourcing and governor design), `MERGE_NOTES.md` (how the two products were merged), `DEVELOPER_TASKS.md` (the task list the developer executed), `PRELAUNCH.md`, `DOCUMENTATION.md`, `README.md`. This brief supersedes STATUS_NOW.md.
