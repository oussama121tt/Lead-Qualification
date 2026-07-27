"""
Étape 3 — Fetch + parsing du site web (Firecrawl)
Étape 3bis — Extraction des signaux techniques déterministes (DOM/CSS/meta/git)
Étape 4 — Recherche web ciblée (Firecrawl search)

Principe (issu de la veille sur les détecteurs "AI slop"/vibe-coding) :
tout ce qui est vérifiable par une règle (police, couleur, meta tag, commit git...)
est calculé ICI, sans appel LLM. Le LLM (étape 5) ne reçoit que le texte +
ces signaux déjà calculés en JSON — jamais du HTML brut à interpréter lui-même.

streamlit run app.py
"""

import hashlib
import os
import re
import time
from urllib.parse import urlparse

from dotenv import load_dotenv
from firecrawl import Firecrawl

load_dotenv()
_app = None  # client Firecrawl, initialisé au premier appel

KEYWORDS = {
    "about": ["about", "a-propos", "team", "equipe"],
    "pricing": ["pricing", "tarif", "plans", "price"],
    "careers": ["careers", "jobs", "recrutement", "emploi"],
    "product": ["product", "services", "produit", "solutions"],
}

# Fallback quand la découverte par liens (homepage -> result.links) ne
# trouve rien pour une catégorie : chemins standards les plus courants,
# essayés dans l'ordre. Utile pour les sites où la nav n'est pas exposée
# dans le HTML statique (menu généré en JS, Firecrawl qui ne l'attrape pas),
# ou qui n'ont simplement pas de lien direct vers ces pages depuis la home.
COMMON_PATH_CANDIDATES = {
    "about": ["/about", "/about-us", "/team", "/company"],
    "pricing": ["/pricing", "/plans"],
    "careers": ["/careers", "/jobs"],
    "product": ["/product", "/products", "/services", "/solutions"],
}

MAX_CONTENT_CHARS_PER_PAGE = 32000  # ~8000 tokens, garde-fou tier gratuit

# Pages qui répondent HTTP 200 côté Firecrawl mais dont le rendu a planté
# côté client (SPA React/Next.js qui crash avant d'afficher le vrai contenu),
# ou vraies pages d'erreur/404. Sans ce filtre, ce texte est envoyé tel quel
# au scoring comme s'il s'agissait du contenu réel du site.
#
# Marqueurs littéraux : rapides, mais ratent les variantes de mise en forme
# (ex: "# 404\n\nPage Not Found" ne contient jamais la sous-chaîne exacte
# "404 not found"). D'où l'ajout de BROKEN_PAGE_PATTERNS en complément.
BROKEN_PAGE_MARKERS = [
    "client-side exception has occurred",
    "application error",
    "hydration failed",
    "unhandled runtime error",
    "this page could not be found",
    "404 not found",
    "404: this page could not be found",
    "500 internal server error",
]

# Patterns regex : couvrent les 404/erreurs où le numéro et le message sont
# séparés (titre Markdown "# 404" suivi d'un paragraphe "Page Not Found"),
# quel que soit l'ordre exact des mots ou la ponctuation.
BROKEN_PAGE_PATTERNS = [
    r"#\s*404\b",                          # heading Markdown "# 404"
    r"\b404\b.{0,40}page\s+not\s+found",   # "404 ... page not found" (40 chars d'écart max)
    r"page\s+not\s+found.{0,40}\b404\b",   # ordre inverse
    r"\boops!?\b.{0,60}vanished",          # tournures type "Oops! ... vanished into thin air"
    r"\bpage\s+(?:you('|')?re\s+)?looking\s+for\b.{0,60}(?:doesn'?t\s+exist|not\s+found|vanished)",
]

MIN_VALID_CONTENT_CHARS = 50  # en dessous, contenu trop trivial pour être une vraie page

# ---------------------------------------------------------------------------
# Signatures déterministes — checklist issue de la veille (Design Slop Cop,
# isthatvibecoded.com, detectvibecode.com...). Chaque entrée = un regex simple
# sur le HTML brut. Aucune de ces valeurs n'est jugée par un LLM.
# ---------------------------------------------------------------------------

GENERATOR_FINGERPRINTS = {
    # nom du builder → patterns cherchés dans le HTML brut / meta / liens
    "lovable": [r"lovable\.dev", r"lovable-tagger", r"gpteng\.co"],
    "v0": [r"v0\.dev", r"vusercontent\.net"],
    "bolt": [r"bolt\.new", r"stackblitz"],
    "replit": [r"replit\.com", r"replit\.dev"],
    "cursor": [r"built with cursor", r"cursor\.sh"],
}

TREND_FONTS = [
    "Space Grotesk", "Instrument Serif", "Geist", "Syne", "Fraunces",
]

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

VIBE_LANGUAGE_MARKERS = [
    "built with cursor", "built with v0", "made with lovable", "built with bolt",
    "vibe coded", "vibe-coded", "no-code", "sans code",
]

# Style d'écriture typique du contenu généré/assisté par IA — indépendant de
# VIBE_LANGUAGE_MARKERS (qui détecte des mentions explicites d'outils de build).
# Ici on détecte des tournures marketing clichées, sur-représentées dans le
# copywriting généré ou fortement assisté par LLM. Un seul match n'est pas
# significatif ; c'est la densité (plusieurs occurrences distinctes sur une
# même page) qui est le signal utile.
AI_STYLE_PHRASES = [
    "seamlessly integrate", "seamless integration", "unlock the power of",
    "unlock the full potential", "elevate your", "revolutionize the way",
    "revolutionize how", "game-changer", "game changing", "cutting-edge",
    "state-of-the-art", "harness the power of", "empower you to",
    "empower your team", "in today's fast-paced world", "in today's digital age",
    "in today's rapidly evolving", "at the intersection of", "whether you're a",
    "whether you are a", "dive into", "navigate the complexities of",
    "take your business to the next level", "robust and scalable",
    "effortlessly", "streamline your workflow", "supercharge your",
    "unleash the power", "transform the way you", "tailored to your needs",
    "one-stop solution", "end-to-end solution", "peace of mind",
    "step into the future", "reimagine how", "redefine how",
]

# Mentions explicites de contenu généré par IA (distinct des deux listes
# ci-dessus : ici l'entreprise le dit elle-même, pas une inférence stylistique).
AI_AUTHORSHIP_DISCLOSURES = [
    "written with ai", "generated with ai", "powered by gpt", "powered by chatgpt",
    "ai-generated content", "content generated by ai", "drafted by ai",
]


# ---------------------------------------------------------------------------
# Extraction de signaux ciblés — careers & pricing (point #3 du plan de
# correction). Au lieu d'envoyer le texte brut de ces deux pages au LLM
# (souvent 3-8K caractères de boilerplate RH ou de grille tarifaire), on
# calcule ici un signal compact et déterministe. Gain double : volume divisé
# par ~20-50x, et signal plus fiable qu'une interprétation de texte par LLM.
# ---------------------------------------------------------------------------

ENGINEERING_ROLE_KEYWORDS = [
    "engineer", "developer", "backend", "back-end", "frontend", "front-end",
    "full-stack", "fullstack", "devops", "sre", "site reliability",
    "data scientist", "machine learning", "ml engineer", "software architect",
    "qa engineer", "sdet", "platform engineer",
]

OTHER_ROLE_KEYWORDS = [
    "sales", "account executive", "marketing", "customer success", "support",
    "designer", "product manager", "operations", "recruiter", "finance",
    "content writer", "community manager", "growth",
]

SELF_SERVE_CTA_MARKERS = [
    "sign up", "start free trial", "start your free trial", "get started free",
    "buy now", "start for free", "try for free", "subscribe now", "upgrade now",
]

SALES_LED_CTA_MARKERS = [
    "contact us", "book a demo", "talk to sales", "request a quote",
    "schedule a call", "contact sales", "get in touch", "request a demo",
]

VISIBLE_PRICE_PATTERN = re.compile(r"[$€£]\s?\d|\b\d+\s?(?:/mo|/month|per month)\b", re.IGNORECASE)


def extract_careers_signal(content: str) -> dict:
    """
    Signal déterministe sur une page careers/jobs : ratio de postes techniques
    vs non-techniques mentionnés. Ne compte PAS le nombre exact de postes
    ouverts (Firecrawl renvoie du texte, pas une liste structurée fiable) —
    juste la présence de vocabulaire technique vs non-technique, ce qui suffit
    comme signal de pression de croissance technique (ou son absence).
    """
    text = (content or "").lower()
    if not text.strip():
        return {"has_careers_page_content": False}

    eng_matches = sorted({kw for kw in ENGINEERING_ROLE_KEYWORDS if kw in text})
    other_matches = sorted({kw for kw in OTHER_ROLE_KEYWORDS if kw in text})
    total = len(eng_matches) + len(other_matches)

    return {
        "has_careers_page_content": True,
        "engineering_keywords_found": eng_matches,
        "other_keywords_found": other_matches,
        "hiring_technical": len(eng_matches) > 0,
        "engineering_ratio": round(len(eng_matches) / total, 2) if total else None,
    }


def extract_pricing_signal(content: str) -> dict:
    """
    Signal déterministe sur une page pricing : motion self-serve (CTA "Sign
    up"/"Start free trial") vs sales-led (CTA "Contact us"/"Book a demo").
    Un site self-serve avec prix visible est un signal de stade "early/scaling"
    différent d'un site 100% sales-led sans prix affiché.
    """
    text = (content or "").lower()
    if not text.strip():
        return {"has_pricing_page_content": False}

    self_serve_hits = sorted({m for m in SELF_SERVE_CTA_MARKERS if m in text})
    sales_hits = sorted({m for m in SALES_LED_CTA_MARKERS if m in text})
    has_visible_price = bool(VISIBLE_PRICE_PATTERN.search(text))

    if self_serve_hits and not sales_hits:
        motion = "self_serve"
    elif sales_hits and not self_serve_hits:
        motion = "sales_led"
    elif self_serve_hits and sales_hits:
        motion = "mixed"
    else:
        motion = "unclear"

    return {
        "has_pricing_page_content": True,
        "self_serve_markers_found": self_serve_hits,
        "sales_led_markers_found": sales_hits,
        "has_visible_price": has_visible_price,
        "pricing_motion": motion,
    }


def _format_signal_as_text(label: str, signal: dict) -> str:
    """Représentation compacte et lisible d'un signal déterministe, pour
    stockage dans `lead_content.content` à la place du texte brut."""
    lines = [f"[{label} — extraction déterministe, pas de texte brut]"]
    for key, value in signal.items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _get_client() -> Firecrawl:
    global _app
    if _app is None:
        _app = Firecrawl(api_key=os.getenv("FIRECRAWL_API_KEY"))
    return _app


def _normalize_domain(url: str) -> str:
    """
    Retourne le netloc normalisé (sans www., en minuscule) d'une URL, pour
    comparer deux URLs "même domaine" sans se faire piéger par www vs non-www.
    """
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def _is_same_domain(link: str, homepage_url: str) -> bool:
    """
    Vrai seulement si `link` pointe vers le même domaine que la homepage.
    Empêche de matcher par erreur un lien externe (ex: g2.com, un article de
    blog tiers, une page LinkedIn) juste parce que l'URL contient un mot-clé
    comme "product" ou "about".
    """
    link_domain = _normalize_domain(link)
    home_domain = _normalize_domain(homepage_url)
    return bool(link_domain) and link_domain == home_domain


def _is_real_subpage(link: str, homepage_url: str) -> bool:
    """
    Rejette les ancres (#services) et tout lien qui pointe en réalité vers
    la même page que la homepage (fréquent sur les sites single-page, très
    courants chez les fondateurs solo/vibe-codés — justement notre cible).
    Sans ce filtre, on paie un crédit Firecrawl pour re-scraper deux fois
    un contenu identique.

    NB : ce filtre ne détecte que les doublons par URL/fragment. Les SPA qui
    servent un contenu identique sur des URLs *distinctes* (ex: /about et /
    renvoient le même shell côté client) ne sont PAS filtrées ici — elles le
    sont a posteriori dans scrape_website() par hash de contenu, une fois le
    texte réellement récupéré.
    """
    link_no_fragment = link.split("#", 1)[0].rstrip("/")
    homepage_no_fragment = homepage_url.split("#", 1)[0].rstrip("/")
    if "#" in link and link_no_fragment == homepage_no_fragment:
        return False
    if link_no_fragment == homepage_no_fragment:
        return False
    return True


def _url_exists(url: str, timeout: float = 5.0) -> bool:
    """
    Vérifie qu'une URL candidate répond réellement (status < 400) AVANT de
    dépenser un crédit Firecrawl dessus. Utilise `requests` (gratuit) plutôt
    que de tenter le scrape directement.

    HEAD d'abord (le plus léger) ; si l'hébergeur ne le supporte pas (405/501,
    fréquent sur Vercel/Netlify pour des routes générées dynamiquement), on
    retente en GET. Toute exception (timeout, DNS, SSL...) = URL considérée
    comme inexistante, jamais bloquant pour le reste du pipeline.
    """
    import requests

    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        if resp.status_code in (405, 501):
            resp = requests.get(url, timeout=timeout, allow_redirects=True, stream=True)
        return resp.status_code < 400
    except Exception:
        return False


def _looks_broken(markdown: str) -> bool:
    """
    Détecte une page qui a techniquement répondu mais n'est pas exploitable :
    crash de rendu côté client, page d'erreur/404, ou contenu quasi vide.
    Ne juge rien sur le fond du site — filtre uniquement le "bruit technique"
    avant que ça n'arrive au scoring (étape 5).

    Vérifie à la fois les marqueurs littéraux (rapide, cas standards) et les
    patterns regex (couvre les 404 "éclatées" sur plusieurs lignes/tournures,
    ex: "# 404\n\nPage Not Found\n\nOops! ... vanished into thin air").
    """
    text = (markdown or "").strip()
    if len(text) < MIN_VALID_CONTENT_CHARS:
        return True
    lowered = text.lower()
    if any(marker in lowered for marker in BROKEN_PAGE_MARKERS):
        return True
    if any(re.search(p, lowered, flags=re.IGNORECASE) for p in BROKEN_PAGE_PATTERNS):
        return True
    return False


def _content_fingerprint(markdown: str) -> str:
    """
    Hash normalisé du contenu (espaces réduits, casse ignorée) pour détecter
    des pages qui renvoient un texte identique malgré des URLs différentes —
    typique des SPA où toutes les routes servent le même shell côté client
    avant que le vrai routing (JS) ne prenne le relais, non capturé par
    Firecrawl. Normaliser les espaces évite de rater un doublon à cause d'une
    différence triviale d'espacement/retours à la ligne.
    """
    normalized = re.sub(r"\s+", " ", (markdown or "").strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _find_key_pages(homepage_url: str):
    app = _get_client()
    # rawHtml en plus de markdown/links : nécessaire pour l'extraction de
    # signaux déterministes (étape 3bis), le markdown seul ne suffit pas.
    result = app.scrape(homepage_url, formats=["markdown", "rawHtml", "links"])
    all_links = [
        link for link in (result.links or [])
        if _is_real_subpage(link, homepage_url)
    ]

    # Fix bug #2 : ne garder QUE les liens du même domaine pour le matching
    # par mot-clé. Un lien externe (g2.com, un review site, LinkedIn...) qui
    # contient "product" ou "about" dans son URL ne doit jamais être choisi
    # comme page "produit"/"about" du site du lead.
    same_domain_links = [
        link for link in all_links if _is_same_domain(link, homepage_url)
    ]

    found_pages = {"homepage": homepage_url}
    for category, keywords in KEYWORDS.items():
        for link in same_domain_links:
            link_lower = link.lower()
            if any(kw in link_lower for kw in keywords):
                found_pages[category] = link
                break

    # Fallback automatique : pour toute catégorie non trouvée via les liens
    # de la homepage (nav en JS non exposée à Firecrawl, site sans lien
    # direct, etc.), on teste les chemins standards les plus courants. On ne
    # les scrape que s'ils existent réellement (_url_exists, gratuit) —
    # jamais de crédit Firecrawl dépensé à l'aveugle sur un 404.
    # (Ces candidats sont construits sur le domaine de la homepage, donc
    # toujours "same domain" par construction — pas besoin de refiltrer ici.)
    base = homepage_url.rstrip("/")
    for category, candidate_paths in COMMON_PATH_CANDIDATES.items():
        if category in found_pages:
            continue
        for path in candidate_paths:
            candidate_url = base + path
            if _url_exists(candidate_url):
                found_pages[category] = candidate_url
                break

    # all_links (toutes origines confondues) reste retourné tel quel pour
    # extract_technical_signals, qui a besoin de pouvoir trouver un lien
    # GitHub externe — ce n'est QUE le matching de pages clés qui doit être
    # restreint au domaine du lead.
    return found_pages, result, all_links


def _match_any(patterns: list, text: str) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


def extract_technical_signals(raw_html: str, all_links: list, homepage_text: str = "") -> dict:
    """
    Étape 3bis. Calcule uniquement des signaux déterministes (aucun LLM).
    Retourne un dict prêt à être injecté tel quel dans le prompt de scoring
    (champ `technical_signals` du schéma de l'étape 5), avec l'evidence brute
    associée à chaque signal déclenché — jamais un verdict déjà interprété.

    NB : on ne passe pas par `result.metadata` de Firecrawl (c'est un objet
    Pydantic sans `.get()`, et rien ne garantit qu'il capture une balise
    <meta name="generator"> arbitraire). On extrait ce tag directement du
    raw_html par regex — plus fiable et indépendant du parsing de Firecrawl.
    """
    raw_html = raw_html or ""
    homepage_text = homepage_text or ""
    signals = {
        "generator_fingerprint": None,
        "vibe_language_matches": [],
        "trend_fonts_found": [],
        "visual_patterns_triggered": [],
        "generator_meta_tag": None,
        "github_repo_url": None,
        "ai_style_phrases_found": [],
        "ai_style_phrase_density": "none",
        "ai_authorship_disclosures_found": [],
    }

    generator_match = re.search(
        r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)["\']',
        raw_html,
        flags=re.IGNORECASE,
    )
    if generator_match:
        signals["generator_meta_tag"] = generator_match.group(1)

    # Fingerprint de builder (le signal le plus fort, cf. isthatvibecoded.com)
    for builder, patterns in GENERATOR_FINGERPRINTS.items():
        if _match_any(patterns, raw_html):
            signals["generator_fingerprint"] = builder
            break

    # Langage explicite ("built with X") — recherché tel quel, pas déduit
    lowered = raw_html.lower()
    signals["vibe_language_matches"] = [
        m for m in VIBE_LANGUAGE_MARKERS if m in lowered
    ]

    # Polices tendance
    signals["trend_fonts_found"] = [f for f in TREND_FONTS if f.lower() in lowered]

    # Patterns visuels (14 catégories façon Design Slop Cop)
    signals["visual_patterns_triggered"] = [
        name for name, patterns in VISUAL_PATTERNS.items() if _match_any(patterns, raw_html)
    ]

    # Style d'écriture : scanné sur le texte visible, pas le HTML brut.
    lowered_text = homepage_text.lower()
    found_phrases = [phrase for phrase in AI_STYLE_PHRASES if phrase in lowered_text]
    signals["ai_style_phrases_found"] = found_phrases
    if len(found_phrases) >= 4:
        signals["ai_style_phrase_density"] = "high"
    elif len(found_phrases) >= 2:
        signals["ai_style_phrase_density"] = "medium"
    elif len(found_phrases) == 1:
        signals["ai_style_phrase_density"] = "low"

    signals["ai_authorship_disclosures_found"] = [
        disclosure for disclosure in AI_AUTHORSHIP_DISCLOSURES if disclosure in lowered_text
    ]

    # Lien GitHub public, pour le check git (étape 3ter). Volontairement PAS
    # restreint au même domaine : un lead peut légitimement linker vers un
    # repo GitHub externe (org GitHub différente du domaine du site).
    for link in all_links or []:
        if "github.com" in link.lower() and "/issues" not in link and "/pull" not in link:
            signals["github_repo_url"] = link
            break

    return signals


def check_github_repo_pattern(repo_url: str) -> dict:
    """
    Étape 3ter (optionnelle, uniquement si un repo public a été trouvé).
    Vérifie le pattern "un seul commit massif / message générique" via
    l'API publique GitHub (non authentifiée, 60 req/h — throttler si utilisé
    sur beaucoup de leads). Ne juge rien : renvoie les faits bruts, le
    jugement ("vibe-codé ou non") reste au scoring (étape 5).
    """
    import requests

    result = {"repo_url": repo_url, "checked": False, "evidence": {}, "error": None}
    try:
        m = re.search(r"github\.com/([^/]+)/([^/?#]+)", repo_url)
        if not m:
            result["error"] = "URL GitHub non reconnue"
            return result
        owner, repo = m.group(1), m.group(2)

        commits_resp = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits",
            params={"per_page": 100},
            timeout=10,
        )
        if commits_resp.status_code != 200:
            result["error"] = f"GitHub API status {commits_resp.status_code}"
            return result

        commits = commits_resp.json()
        result["checked"] = True
        result["evidence"]["total_commits_seen"] = len(commits)
        if commits:
            first_commit = commits[-1]  # le plus ancien de la page récupérée
            result["evidence"]["first_commit_message"] = first_commit.get("commit", {}).get("message", "")
            result["evidence"]["single_commit_repo"] = len(commits) <= 1
    except Exception as e:
        result["error"] = str(e)

    return result


def scrape_website(homepage_url: str, throttle_seconds: float = 1.0) -> dict:
    """
    Scrape la homepage + jusqu'à 4 pages clés découvertes automatiquement,
    et calcule les signaux techniques déterministes sur la homepage.

    Retourne :
        {
            "status": "PARSED" | "FETCH_PARTIAL" | "FETCH_FAILED",
            "rows": [(source, url, content), ...],
            "technical_signals": {...} | None,
            "github_check": {...} | None,
            "error": str | None,
        }
    """
    try:
        pages, homepage_result, all_links = _find_key_pages(homepage_url)
    except Exception as e:
        return {
            "status": "FETCH_FAILED",
            "rows": [],
            "technical_signals": None,
            "github_check": None,
            "error": str(e),
        }

    homepage_markdown = homepage_result.markdown or ""

    if _looks_broken(homepage_markdown):
        # Homepage cassée (crash de rendu JS, page d'erreur, contenu vide) :
        # on s'arrête ici plutôt que d'envoyer ce texte au scoring comme si
        # c'était le vrai site. rows=[] déclenche automatiquement le verdict
        # "no_content_scraped" côté scorer.py (needs_human_review=True).
        return {
            "status": "FETCH_FAILED",
            "rows": [],
            "technical_signals": None,
            "github_check": None,
            "error": "homepage_render_error_or_empty_content",
        }

    rows = [("homepage", homepage_url, homepage_markdown[:MAX_CONTENT_CHARS_PER_PAGE])]
    # Fingerprints de contenu déjà retenus, pour détecter les doublons SPA
    # (une URL différente qui sert malgré tout le même shell que la homepage
    # ou qu'une autre page déjà acceptée).
    seen_fingerprints = {_content_fingerprint(homepage_markdown)}

    technical_signals = extract_technical_signals(
        raw_html=getattr(homepage_result, "raw_html", None),
        all_links=all_links,
        homepage_text=homepage_markdown,
    )

    github_check = None
    if technical_signals.get("github_repo_url"):
        github_check = check_github_repo_pattern(technical_signals["github_repo_url"])

    app = _get_client()
    failures = 0
    duplicates = 0
    other_pages = {k: v for k, v in pages.items() if k != "homepage"}

    for category, url in other_pages.items():
        time.sleep(throttle_seconds)  # max 1 req/sec/domaine, tier gratuit oblige
        try:
            r = app.scrape(url, formats=["markdown"], only_main_content=True)
            raw_content = (r.markdown or "")[:MAX_CONTENT_CHARS_PER_PAGE]
            if _looks_broken(raw_content):
                failures += 1
                print(f"Page ignorée (rendu cassé/vide) sur {category} ({url})")
                continue

            # Fix bug #3 : rejette le contenu identique à une page déjà
            # retenue (typiquement le shell d'une SPA servi sur toutes les
            # routes). On compare le hash normalisé, pas juste la longueur,
            # pour ne pas dépendre d'une coïncidence de taille. Toujours fait
            # sur le contenu BRUT (avant compaction careers/pricing ci-dessous),
            # sinon un shell SPA dupliqué sur /careers ne serait plus détecté.
            fingerprint = _content_fingerprint(raw_content)
            if fingerprint in seen_fingerprints:
                duplicates += 1
                print(
                    f"Page ignorée (contenu identique à une page déjà "
                    f"retenue, probable SPA shell) sur {category} ({url})"
                )
                continue
            seen_fingerprints.add(fingerprint)

            # Fix bug confirmé (section 3 du plan) : extract_careers_signal
            # et extract_pricing_signal existaient mais n'étaient jamais
            # appelées. Sans ça, careers/pricing partaient en texte brut
            # complet (3-8K caractères de boilerplate) vers le scoring LLM
            # — exactement ce que ce design voulait éviter. On remplace donc
            # ici le contenu par le signal compact déterministe.
            if category == "careers":
                content = _format_signal_as_text("Careers", extract_careers_signal(raw_content))
            elif category == "pricing":
                content = _format_signal_as_text("Pricing", extract_pricing_signal(raw_content))
            else:
                content = raw_content

            rows.append((category, url, content))
        except Exception as e:
            failures += 1
            print(f"Échec sur {category} ({url}): {e}")
            continue

    # Un doublon SPA n'est pas un "échec" au même titre qu'un timeout ou un
    # 404 (la page existe, elle est juste inutile) — mais elle compte quand
    # même comme "pas de contenu utile supplémentaire" pour déterminer le
    # statut final ci-dessous.
    unusable = failures + duplicates

    if len(rows) == 1 and other_pages:
        status = "FETCH_PARTIAL" if unusable < len(other_pages) else "FETCH_FAILED"
    elif unusable > 0:
        status = "FETCH_PARTIAL"
    else:
        status = "PARSED"

    return {
        "status": status,
        "rows": rows,
        "technical_signals": technical_signals,
        "github_check": github_check,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Étape 4 — Recherche web ciblée (escalade, uniquement si confiance basse
# après le scoring passage 1). Jamais de scraping via session connectée :
# uniquement des résultats publics indexés via l'endpoint `search` Firecrawl.
# ---------------------------------------------------------------------------

SEARCH_QUERY_TEMPLATES = {
    "linkedin": '"{company}" site:linkedin.com/in OR site:linkedin.com/company',
    "product_hunt": '"{company}" site:producthunt.com',
    "twitter": '"{company}" (site:twitter.com OR site:x.com) (vibe coded OR built with AI OR built in a weekend)',
    "github": '"{company}" site:github.com',
    "interviews": '"{founder}" OR "{company}" interview (vibe coding OR built with AI OR built with Cursor OR built with v0)',
}


def search_additional_evidence(
    company_name: str,
    founder_name: str | None = None,
    limit_per_query: int = 3,
    throttle_seconds: float = 1.0,
) -> dict:
    """
    Étape 4. Interroge Firecrawl `search` pour chaque source ciblée et
    renvoie les résultats bruts (url, titre, extrait) — pas de jugement ici,
    ce sont des preuves candidates que le scoring (étape 5) devra citer
    verbatim dans `evidence_quotes` s'il les retient.

    NB : vérifier la signature exacte de `Firecrawl.search()` dans la doc
    Firecrawl au moment de coder — le SDK peut différer selon la version
    installée (paramètres `limit`, `sources`, format de retour).
    """
    app = _get_client()
    founder_name = founder_name or ""
    results_by_source: dict = {}

    for source, template in SEARCH_QUERY_TEMPLATES.items():
        if "{founder}" in template and not founder_name:
            continue  # pas de nom de fondateur connu → on saute cette requête
        query = template.format(company=company_name, founder=founder_name)
        time.sleep(throttle_seconds)
        try:
            resp = app.search(query, limit=limit_per_query)
            hits = getattr(resp, "web", None) or getattr(resp, "data", None) or []
            results_by_source[source] = [
                {
                    "url": getattr(hit, "url", None) or hit.get("url"),
                    "title": getattr(hit, "title", None) or hit.get("title"),
                    "snippet": getattr(hit, "description", None) or hit.get("description"),
                }
                for hit in hits
            ]
        except Exception as e:
            results_by_source[source] = {"error": str(e)}

    return results_by_source