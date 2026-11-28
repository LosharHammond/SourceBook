"""Turn the workspace "New data page" form into a build definition, and back again for editing."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

from .db import slugify

SIMPLE_SELECTOR = re.compile(r'\["([^"]+)"="([^"]+)"\]')
OSM_TAG_RE = re.compile(r"^[a-z][a-z0-9_:]{0,40}=[A-Za-z0-9_ ;:.-]{1,60}$")
GENERIC = {"companies", "company", "firms", "firm", "businesses", "business", "suppliers", "supplier", "providers", "provider",
           "services", "service", "dealers", "distributors", "wholesalers", "retailers", "shops", "stores", "manufacturers",
           "producers", "makers", "sellers", "vendors", "agents", "agencies", "exporters", "importers", "traders", "processors",
           "installers", "contractors", "the", "and", "for", "with", "local", "small", "large", "best", "top"}
# common business words → OpenStreetMap tags, used when the person didn't give tags themselves
TAG_HINTS = {
    "web develop": ["office=it", "craft=web_design"], "web design": ["office=it", "craft=web_design"],
    "software": ["office=it"], "digital market": ["office=advertising", "office=it"],
    "pharmac": ["amenity=pharmacy"], "hotel": ["tourism=hotel"], "guest house": ["tourism=guest_house"], "restaurant": ["amenity=restaurant"],
    "supermarket": ["shop=supermarket"], "hardware": ["shop=hardware"], "hospital": ["amenity=hospital"], "clinic": ["amenity=clinic"],
    "bank": ["amenity=bank"], "fuel": ["amenity=fuel"], "filling station": ["amenity=fuel"], "bakery": ["shop=bakery"], "bakeries": ["shop=bakery"],
    "printing": ["shop=copyshop", "craft=printer"], "printer": ["craft=printer"], "tailor": ["craft=tailor"], "carpent": ["craft=carpenter"],
    "electronics": ["shop=electronics"], "mobile phone": ["shop=mobile_phone"], "car repair": ["shop=car_repair"], "mechanic": ["shop=car_repair"],
    "packaging": ["industrial=packaging", "shop=packaging"], "school": ["amenity=school"], "furniture": ["shop=furniture"],
    "cosmetic": ["shop=cosmetics"], "beauty": ["shop=beauty"], "pharmaceutical": ["industrial=pharmaceutical"], "brewer": ["craft=brewery"],
    "logistics": ["office=logistics"], "courier": ["office=courier", "amenity=post_office"], "insurance": ["office=insurance"],
    "estate agent": ["office=estate_agent"], "real estate": ["office=estate_agent"], "lawyer": ["office=lawyer"], "law firm": ["office=lawyer"],
    "accountant": ["office=accountant"], "travel agen": ["shop=travel_agency"], "solar": ["shop=solar", "craft=photovoltaic"],
    "veterinar": ["amenity=veterinary"], "factor": ["man_made=works"], "manufactur": ["man_made=works", "industrial=factory"],
    "cafe": ["amenity=cafe"], "dentist": ["amenity=dentist"], "dental": ["amenity=dentist"], "doctor": ["amenity=doctors"], "pharmacy": ["amenity=pharmacy"],
    "vet": ["amenity=veterinary"], "salon": ["shop=hairdresser", "shop=beauty"], "hairdress": ["shop=hairdresser"], "barber": ["shop=hairdresser"],
    "gym": ["leisure=fitness_centre"], "fitness": ["leisure=fitness_centre"], "cafe": ["amenity=cafe"], "coffee shop": ["amenity=cafe"],
    "bar": ["amenity=bar"], "pub": ["amenity=pub"], "plumber": ["craft=plumber"], "electrician": ["craft=electrician"], "florist": ["shop=florist"],
    "optician": ["shop=optician"], "butcher": ["shop=butcher"], "bookshop": ["shop=books"], "book store": ["shop=books"], "clothing": ["shop=clothes"],
    "clothes": ["shop=clothes"], "shoe": ["shop=shoes"], "jewel": ["shop=jewelry"], "car dealer": ["shop=car"], "tyre": ["shop=tyres"], "tire": ["shop=tyres"],
    "laundry": ["shop=laundry", "shop=dry_cleaning"], "dry clean": ["shop=dry_cleaning"], "driving school": ["amenity=driving_school"],
    "kindergarten": ["amenity=kindergarten"], "car wash": ["amenity=car_wash"], "photograph": ["craft=photographer", "shop=photo"],
    "pet shop": ["shop=pet"], "wholesale": ["shop=wholesale"], "warehouse": ["building=warehouse"], "factory": ["man_made=works"],
    "agro": ["shop=agrarian"], "farm input": ["shop=agrarian"], "fertili": ["shop=agrarian"], "seed": ["shop=agrarian"],
}
LIMITS = {"keywords": 12, "products": 20, "websites": 150, "tags": 6}


@lru_cache(maxsize=1)
def countries() -> list[tuple[str, str]]:
    """(ISO code, English name) for every country, sorted by name."""
    try:
        from babel import Locale
        names = Locale("en").territories
        out = [(code, name) for code, name in names.items() if re.fullmatch(r"[A-Z]{2}", code) and code not in ("ZZ", "EU", "EZ", "UN", "QO", "XA", "XB", "XK")]
    except Exception:  # noqa: BLE001 - babel missing: a short list still works
        out = [("GH", "Ghana"), ("NG", "Nigeria"), ("KE", "Kenya"), ("ZA", "South Africa"), ("CI", "Côte d’Ivoire"), ("SN", "Senegal"),
               ("TZ", "Tanzania"), ("UG", "Uganda"), ("RW", "Rwanda"), ("EG", "Egypt"), ("MA", "Morocco"), ("GB", "United Kingdom"), ("US", "United States")]
    return sorted(out, key=lambda x: x[1])


def country_name(code: str) -> str:
    return dict(countries()).get((code or "").upper(), code)


def _lines(text: str, sep: str = r"[\n,;]") -> list[str]:
    return [x.strip() for x in re.split(sep, text or "") if x.strip()]


def _singular(phrase: str) -> str:
    words = phrase.strip().split()
    if not words:
        return "business"
    last = words[-1]
    if last.lower().endswith("ies"):
        last = last[:-3] + "y"
    elif last.lower().endswith(("sses", "shes", "ches", "xes")):
        last = last[:-2]
    elif last.lower().endswith("s") and not last.lower().endswith("ss"):
        last = last[:-1]
    return " ".join(words[:-1] + [last]).lower()


def _stem(word: str) -> str:
    w = word.lower().strip()
    for suf in ("ers", "ing", "ies", "es", "er", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def parse_websites(text: str) -> tuple[list[dict[str, str]], list[str]]:
    rows, errors = [], []
    for line in _lines(text, r"\n"):
        name, _, url = line.rpartition(",") if "," in line else ("", "", line)
        url = url.strip()
        if not re.match(r"^https?://", url):
            url = "https://" + url
        host = urlparse(url).hostname or ""
        if "." not in host or " " in host:
            errors.append(f"“{line}” doesn't look like a website address.")
            continue
        label = name.strip() or host.removeprefix("www.").split(".")[0].replace("-", " ").title()
        rows.append({"name": label, "website": url, "city": ""})
    return rows, errors


def definition_from_form(form: dict[str, str]) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    what = (form.get("what") or "").strip()
    country = (form.get("country") or "").strip().upper()
    if not 3 <= len(what) <= 80:
        errors.append("Describe the businesses in 3 to 80 characters, e.g. “Cocoa exporters”.")
    if country not in dict(countries()):
        errors.append("Choose a country.")
    keywords = _lines(form.get("keywords", ""))
    if not keywords:
        words = re.findall(r"[A-Za-z]{3,}", what)
        keywords = [w for w in words if w.lower() not in GENERIC] or words
    if not keywords:
        errors.append("Add at least one keyword that appears in these businesses’ names or websites.")
    if len(keywords) > LIMITS["keywords"]:
        errors.append(f"Use at most {LIMITS['keywords']} keywords.")
    products = _lines(form.get("products", ""), r"[\n;]")[: LIMITS["products"] + 1]
    if len(products) > LIMITS["products"]:
        errors.append(f"List at most {LIMITS['products']} products or services.")
    tags = _lines(form.get("osm_tags", ""), r"[\n]")
    bad = [t for t in tags if not OSM_TAG_RE.match(t)]
    if bad:
        errors.append(f"These map tags aren't in key=value form: {', '.join(bad)}")
    if len(tags) > LIMITS["tags"]:
        errors.append(f"Use at most {LIMITS['tags']} map tags.")
    if not tags and not form.get("no_tag_hints"):
        low = unicodedata.normalize("NFKD", what).encode("ascii", "ignore").decode().lower()
        matched = [h for h in TAG_HINTS if h in low]
        matched = [h for h in matched if not any(h != o and h in o for o in matched)]     # "veterinary clinic" beats "clinic"
        if any(h in low for h in ("veterinar", "driving school", "vet ")):
            matched = [h for h in matched if h not in ("clinic", "school")]
        tags = list(dict.fromkeys(t for h in matched for t in TAG_HINTS[h]))[: LIMITS["tags"]]
    seeds, site_errors = parse_websites(form.get("websites", ""))
    errors += site_errors
    if len(seeds) > LIMITS["websites"]:
        errors.append(f"Add at most {LIMITS['websites']} websites per page.")
    if errors:
        return None, errors
    stems = sorted({_stem(k) for k in keywords if len(_stem(k)) >= 3})
    cname = country_name(country)
    place = form.get("place") if isinstance(form.get("place"), dict) else None
    where = f"{place['name']}, {cname}" if place else cname
    title = f"{what[0].upper()}{what[1:]} in {where}"
    summary = (form.get("summary") or "").strip()[:400] or (
        f"Businesses in {where} matching “{what}”, with the products and business contacts they publish themselves.")
    product_terms = {p: [p.lower()] + ([p.lower().rstrip("s")] if p.lower().endswith("s") and len(p) > 4 else []) for p in products}
    definition = {
        "slug": slugify(title), "title": title, "summary": summary, "country": country, "country_name": cname,
        "what": what, "kind_label": _singular(what),
        "keywords": keywords, "relevance_keywords": stems,
        "osm_selectors": [f'["{k}"="{v}"]' for k, v in (t.split("=", 1) for t in tags)],
        "osm_tags": tags if form.get("osm_tags") else [],
        "osm_name_regex": "|".join(re.escape(s) for s in stems),
        "products": product_terms, "seed_rows": seeds,
    }
    if place:
        definition["place"] = place
    # tag values count as evidence too: "amenity=pharmacy" is a pharmacy whatever language its name is in
    definition["relevance_keywords"] = list(dict.fromkeys(definition["relevance_keywords"] + [t.split("=", 1)[1].lower() for t in tags]))
    return definition, []


def form_from_definition(d: dict[str, Any]) -> dict[str, str]:
    return {
        "what": d.get("what") or re.sub(rf"\s+in\s+{re.escape(d.get('country_name') or country_name(d.get('country', '')))}$", "", d.get("title", "")),
        "country": d.get("country", ""),
        "keywords": ", ".join(d.get("keywords") or d.get("relevance_keywords", [])),
        "products": "\n".join(d.get("products", {}).keys()),
        "osm_tags": "\n".join(d.get("osm_tags") or [f"{m.group(1)}={m.group(2)}" for m in map(SIMPLE_SELECTOR.fullmatch, d.get("osm_selectors", [])) if m]),
        "websites": "\n".join(f"{r['name']}, {r['website']}" for r in d.get("seed_rows", [])),
        "summary": d.get("summary", ""),
    }


def merge_definition(new: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    """Keep hand-written settings the form can't show: a seeds CSV, advanced map queries, extra product keywords."""
    if old.get("seeds"):
        new["seeds"] = old["seeds"]
    new["osm_selectors"] += [s for s in old.get("osm_selectors", []) if not SIMPLE_SELECTOR.fullmatch(s)]
    for product, terms in old.get("products", {}).items():
        if product in new["products"]:
            new["products"][product] = list(dict.fromkeys(terms + new["products"][product]))
    if old.get("osm_name_regex") and set(new["keywords"]) == set(old.get("keywords") or old.get("relevance_keywords") or []):
        new["osm_name_regex"] = old["osm_name_regex"]
        new["relevance_keywords"] = old.get("relevance_keywords", new["relevance_keywords"])
    return new
