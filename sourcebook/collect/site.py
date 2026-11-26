"""Read a business's own website (home, contact and about pages) for what it publishes about itself:
business contact channels, address, products and a short self-description. Follows robots.txt, waits between
requests, reads at most a handful of pages per site, and never submits forms or logs in."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from . import USER_AGENT

CONTACT_PATH_RE = re.compile(r"contact|about|who-we-are|company|reach-us|get-in-touch|products|services", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+?233|0)[\s-]?(?:\d[\s-]?){8,9}\d")        # Ghana numbers; other countries come from tel: links
SKIP_EMAIL_RE = re.compile(r"\.(png|jpe?g|gif|webp|svg)$|example\.|sentry|wixpress|your(name|email)|@domain\.", re.I)
MAX_PAGES = 4

_LOOK = r"(?:looking|seeking|searching|we are looking|we seek|recherch\w*|buscamos|procuramos)\s+(?:for\s+)?(?:an?\s+|new\s+|des\s+|de\s+|nuevos\s+|novos\s+)?"
NEED_PATTERNS = {
    "seeking_investment": re.compile(_LOOK + r"(?:investors?|investment|funding|financing|capital|investisseurs|financement|inversores|inversi[oó]n|investidores)"
                                     r"|open to (?:new )?investors|investment opportunit\w+|raise (?:capital|funds)", re.I),
    "seeking_distributors": re.compile(_LOOK + r"(?:distributors?|agents?|dealers?|resellers?|stockists?|wholesalers?|distributeurs|revendeurs|distribuidores|representantes)"
                                       r"|become (?:a|our) (?:distributor|dealer|agent|reseller|stockist)|distributors? wanted|dealership opportunit\w+", re.I),
    "seeking_suppliers": re.compile(_LOOK + r"(?:suppliers?|vendors?|sources? of|fournisseurs|proveedores|fornecedores)"
                                    r"|request for (?:quotations?|proposals?)|\b(?:rfq|rfp)s?\b|invitation to tender|tender notice|call for (?:bids|tenders)|appel d.offres", re.I),
    "seeking_partners": re.compile(_LOOK + r"(?:partners?|partnerships?|collaborators?|partenaires|socios|parceiros)"
                                   r"|partnership opportunit\w+|become (?:a|our) partner", re.I),
    "hiring": re.compile(r"we(?:'|’)?re hiring|we are hiring|now hiring|now recruiting|vacanc(?:y|ies)|job openings?|current openings|"
                         r"nous recrutons|offres? d.emploi|estamos contratando|vagas de emprego", re.I),
}
SIGNAL_PATTERNS = {
    "delivery": re.compile(r"deliver|shipping|we ship|dispatch|courier|livraison|entrega|env[ií]o|nationwide", re.I),
    "export": re.compile(r"export|exportation|exportaci[oó]n|exporta[cç][aã]o|international (?:buyers|markets|customers)", re.I),
    "producer": re.compile(r"manufactur|we produce|producers? of|processing plant|processor|factory|our plant|our farms?|\bmill\b|"
                           r"fabricant|usine|f[aá]brica|production (?:line|capacity|facility)", re.I),
    "certified": re.compile(r"\biso ?\d{3,5}|haccp|fssc|\bgmp\b|halal|kosher|organic certif|certified organic|fair ?trade|rainforest alliance|"
                            r"\bbrc\b|ifs food|\bfda\b|nafdac|kebs|ghana standards|standards (?:authority|organi[sz]ation|bureau)|certified|certification|certifi[ée]", re.I),
    "card_payment": re.compile(r"\bvisa\b|mastercard|credit card|debit card|card payment|mobile money|\bmomo\b|m-?pesa|paypal|stripe|paystack|"
                               r"flutterwave|hubtel|orange money|mtn money|airtel money|\bpos\b", re.I),
    "hours": re.compile(r"(?:mon(?:day)?|lundi|lunes|segunda)\s*(?:-|–|to|à|a)\s*(?:fri(?:day)?|sat(?:urday)?|vendredi|samedi|viernes|s[aá]bado|sexta)|"
                        r"opening hours|business hours|working hours|office hours|open daily|open 24|horaires|horario|\d{1,2}(?::\d{2})?\s*(?:am|pm)\s*(?:-|–|to)\s*\d", re.I),
    "address": re.compile(r"\b(?:street|road|avenue|boulevard|highway|p\.?\s?o\.?\s?box|plot \d|rue|calle|rua|industrial area)\b", re.I),
}


def _quote(text: str, m: re.Match) -> str:
    start = max(text.rfind(".", 0, m.start()) + 1, text.rfind("©", 0, m.start()) + 7 if "©" in text[: m.start()] else 0,
                text.rfind("|", 0, m.start()) + 1, m.start() - 80)
    end = text.find(".", m.end())
    end = min(end if end != -1 else len(text), m.end() + 80)
    q = re.sub(r"\s+", " ", text[start:end]).strip(" .,;:-")
    words = q.split()
    return " ".join(words[:14]) + ("…" if len(words) > 14 else "")


class BlockedAddress(httpx.HTTPError):
    pass


def public_host(url: str) -> bool:
    """Only public internet addresses on the normal web ports — never the server's own network."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.port not in (None, 80, 443):
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except OSError:
        return True            # unresolvable: the request itself will fail harmlessly
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            return False
    return True


async def _guard(request: httpx.Request) -> None:
    if not await asyncio.to_thread(public_host, str(request.url)):
        raise BlockedAddress(f"Not a public website address: {request.url.host}")


@dataclass
class SiteFacts:
    url: str
    pages: list[str] = field(default_factory=list)
    contacts: dict[str, list[tuple[str, str]]] = field(default_factory=dict)   # field -> [(value, page_url)]
    products: list[tuple[str, str]] = field(default_factory=list)              # (product term, page_url)
    about: list[tuple[str, str]] = field(default_factory=list)                 # (sentence, page_url)
    address: list[tuple[str, str]] = field(default_factory=list)
    names: list[tuple[str, str]] = field(default_factory=list)                 # the name the site gives itself
    text: str = ""                                                             # lower-cased page text, for keyword evidence
    blocked: str = ""
    error: str = ""                                                            # why the site couldn't be read
    final_url: str = ""                                                        # where the home page ended up after redirects
    signals: set[str] = field(default_factory=set)                             # viewport, ordering, contact_form …
    years: list[int] = field(default_factory=list)                             # © years seen in footers
    statements: dict[str, tuple[str, str]] = field(default_factory=dict)       # need code -> (short quote, page)
    load_seconds: float | None = None                                          # how long the home page took

    def mentions(self, stems: list[str]) -> list[str]:
        return [s for s in stems if re.search(rf"\b{re.escape(s)}", self.text)]

    def add(self, kind: str, value: str, page: str) -> None:
        value = value.strip()
        bucket = self.contacts.setdefault(kind, [])
        if value and value not in [v for v, _ in bucket]:
            bucket.append((value, page))


def normalise_phone(raw: str) -> str:
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("0") and len(digits) == 10:          # Ghana local format
        digits = "+233" + digits[1:]
    return digits


def extract(html: str, page_url: str, facts: SiteFacts, product_terms: dict[str, list[str]]) -> list[str]:
    """Pull published contacts/products/about text from one page; return same-site links worth reading next."""
    soup = BeautifulSoup(html, "lxml")
    for bad in soup(["script", "style", "noscript", "template"]):
        if bad.name == "script" and bad.get("type") == "application/ld+json":
            continue
        bad.decompose()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        low = href.lower()
        if low.startswith("tel:"):
            facts.add("phone", normalise_phone(unquote(href[4:])), page_url)
        elif low.startswith("mailto:"):
            email = unquote(href[7:]).split("?")[0]
            if EMAIL_RE.fullmatch(email) and not SKIP_EMAIL_RE.search(email):
                facts.add("email", email.lower(), page_url)
        elif re.search(r"(wa\.me/|api\.whatsapp\.com/send|whatsapp\.com/send)", low):
            m = re.search(r"wa\.me/(\+?\d+)|phone=(\+?\d+)", href)
            if m:
                facts.add("whatsapp", "+" + (m.group(1) or m.group(2)).lstrip("+"), page_url)
        elif re.search(r"(^|//)(t\.me|telegram\.me)/", low):
            facts.add("telegram", "https://t.me/" + href.rstrip("/").split("/")[-1], page_url)
        elif "facebook.com/" in low and "sharer" not in low:
            facts.add("facebook", href.split("?")[0], page_url)
        elif "instagram.com/" in low:
            facts.add("instagram", href.split("?")[0], page_url)
        elif "linkedin.com/company/" in low:
            facts.add("linkedin", href.split("?")[0], page_url)
    # schema.org JSON-LD (Organization / LocalBusiness)
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        for node in (data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []):
            if not isinstance(node, dict):
                continue
            if node.get("telephone"):
                facts.add("phone", normalise_phone(str(node["telephone"])), page_url)
            if node.get("email"):
                facts.add("email", str(node["email"]).replace("mailto:", "").lower(), page_url)
            addr = node.get("address")
            if isinstance(addr, dict):
                text = ", ".join(str(addr.get(k)) for k in ("streetAddress", "addressLocality", "addressRegion") if addr.get(k))
                if text:
                    facts.address.append((text, page_url))
            if node.get("name") and node.get("@type") in ("Organization", "LocalBusiness", "Corporation", "Store", "WebSite") and not facts.names:
                facts.names.append((str(node["name"]).strip()[:120], page_url))
            if node.get("description") and len(facts.about) < 3:
                facts.about.append((str(node["description"])[:400], page_url))
    text = soup.get_text(" ", strip=True)
    for m in PHONE_RE.finditer(text):
        facts.add("phone", normalise_phone(m.group(0)), page_url)
    for m in EMAIL_RE.finditer(text):
        if not SKIP_EMAIL_RE.search(m.group(0)):
            facts.add("email", m.group(0).lower(), page_url)
    if soup.find("meta", attrs={"name": "viewport"}):
        facts.signals.add("viewport")
    if not facts.pages:                                     # home-page-only checks
        md = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        if md and len((md.get("content") or "").strip()) >= 30:
            facts.signals.add("meta_description")
        if soup.title and soup.title.get_text(strip=True):
            facts.signals.add("title")
    raw = html.lower()
    if "connect.facebook.net" in raw or "fbq(" in raw or "facebook pixel" in raw:
        facts.signals.add("meta_pixel")
    if re.search(r"googletagmanager\.com|google-analytics\.com|gtag\(|plausible\.io|matomo|clarity\.ms|hotjar|segment\.com/analytics|static\.cloudflareinsights", raw):
        facts.signals.add("tracking")
    if re.search(r"calendly\.com|book(?:ing)?[ -]?(?:now|online|an? appointment|a table)|make an? (?:appointment|reservation)|reserve (?:a table|now)|"
                 r"setmore|acuityscheduling|simplybook|fresha|booksy|opentable|resy\.com|tidycal|zcal|rendez-vous|reservar|agendar", raw):
        facts.signals.add("booking")
    if soup.find("form") and (soup.find("textarea") or soup.find("input", attrs={"type": re.compile("email", re.I)})):
        facts.signals.add("contact_form")
    low_html = html.lower()
    if re.search(r"add[ -]to[ -]cart|checkout|order online|order now|buy now|shop now|woocommerce|shopify|paystack|flutterwave|"
                 r"hubtel|expresspay|mobile money payment|pay online|/cart\b|/shop\b", low_html):
        facts.signals.add("ordering")
    for m in re.finditer(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(\d{4})", html, re.I):
        y = int(m.group(1))
        if 1995 <= y <= 2100:
            facts.years.append(y)
    site_name = soup.find("meta", attrs={"property": "og:site_name"})
    if site_name and site_name.get("content") and not facts.names:
        facts.names.append((site_name["content"].strip()[:120], page_url))
    for code, rx in NEED_PATTERNS.items():
        if code not in facts.statements:
            m = rx.search(text)
            if m:
                facts.statements[code] = (_quote(text, m), page_url)
    for code, rx in SIGNAL_PATTERNS.items():
        if code not in facts.signals and rx.search(text):
            facts.signals.add(code)
    if len(facts.text) < 60_000:
        facts.text += " " + text.lower()[:30_000]
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content") and len(facts.about) < 3:
        facts.about.append((meta["content"].strip()[:400], page_url))
    low = text.lower()
    for product, keywords in product_terms.items():
        if any(re.search(rf"\b{re.escape(k)}", low) for k in keywords) and product not in [p for p, _ in facts.products]:
            facts.products.append((product, page_url))
    base = urlparse(page_url)
    links = []
    for a in soup.find_all("a", href=True):
        u = urljoin(page_url, a["href"]).split("#")[0]
        p = urlparse(u)
        if p.netloc == base.netloc and CONTACT_PATH_RE.search(p.path + " " + a.get_text(" ")) and u not in links:
            links.append(u)
    return links


async def read_site(url: str, product_terms: dict[str, list[str]], delay: float = 2.0) -> SiteFacts:
    if not re.match(r"^https?://", url):
        url = "https://" + url
    facts = SiteFacts(url=url)
    parsed = urlparse(url)
    robots = RobotFileParser()
    if not await asyncio.to_thread(public_host, url):
        facts.blocked = "not a public website address"
        return facts
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True, max_redirects=5,
                                 event_hooks={"request": [_guard]}) as c:
        try:
            r = await c.get(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
            robots.parse(r.text.splitlines() if r.status_code == 200 else [])
        except httpx.HTTPError:
            robots.parse([])
        queue, seen = [url], set()
        while queue and len(facts.pages) < MAX_PAGES:
            page = queue.pop(0)
            if page in seen:
                continue
            seen.add(page)
            if not robots.can_fetch(USER_AGENT, page):
                facts.blocked = facts.blocked or "robots.txt asks automated tools not to read this site"
                continue
            if facts.pages:
                await asyncio.sleep(delay)
            try:
                r = await c.get(page)
            except httpx.HTTPError as exc:
                if not facts.pages and page == url:
                    facts.error = _describe_error(exc)
                    if url.startswith("https://"):            # some small-business sites only work over plain http
                        try:
                            r = await c.get("http://" + url[8:])
                            facts.error = ""
                        except httpx.HTTPError:
                            continue
                    else:
                        continue
                else:
                    continue
            if len(r.content) > 3_000_000:
                continue
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                if not facts.pages and page == url:
                    facts.error = f"the home page answered with status {r.status_code}"
                continue
            if not facts.pages:
                facts.final_url = str(r.url)
                facts.error = ""
                facts.load_seconds = r.elapsed.total_seconds() if r.elapsed else None
            facts.pages.append(str(r.url))
            queue.extend(extract(r.text, str(r.url), facts, product_terms))
    return facts


def _describe_error(exc: Exception) -> str:
    text = str(exc).lower()
    if "ssl" in text or "certificate" in text:
        return "its security certificate isn't valid"
    if isinstance(exc, httpx.TimeoutException):
        return "it took too long to answer"
    if isinstance(exc, httpx.ConnectError):
        return "it couldn't be reached"
    return "it couldn't be loaded"


def summarise(facts: SiteFacts) -> dict[str, Any]:
    return {"pages": facts.pages, "contacts": facts.contacts, "products": facts.products, "about": facts.about,
            "address": facts.address, "blocked": facts.blocked}
