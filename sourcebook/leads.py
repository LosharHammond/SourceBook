"""Leads: score each business for what *you* sell (a "lens"), give the reasons, and draft an opening line.

A reason only counts if Sourcebook actually observed it (see gaps.py), so a business with no website can't score
for "no tracking tag" — that would be a guess."""

from __future__ import annotations

from typing import Any

# lens: (label, who it's for, {gap code: weight})
LENSES: dict[str, tuple[str, str, dict[str, int]]] = {
    "mixed": ("Mixed", "Anything: every need and gap counts", {}),
    "web": ("Web design", "Web designers and developers", {"no_website": 40, "site_down": 35, "not_mobile": 25, "no_https": 20,
                                                           "outdated_site": 15, "slow_site": 15, "no_contact_form": 10, "no_products_listed": 10}),
    "seo": ("Local SEO", "SEO and local-search specialists", {"no_website": 30, "no_meta_description": 25, "not_on_map": 20,
                                                              "no_opening_hours": 15, "no_address": 15, "slow_site": 10, "not_mobile": 10}),
    "ads": ("Paid ads", "Ad agencies and media buyers", {"no_meta_pixel": 30, "no_tracking": 30, "no_booking": 20, "no_online_ordering": 15,
                                                         "no_contact_form": 10}),
    "analytics": ("Analytics", "Analytics and measurement specialists", {"no_tracking": 40, "no_meta_pixel": 25, "no_contact_form": 20,
                                                                         "no_booking": 10}),
    "social": ("Social media", "Social media managers", {"no_social": 40, "no_meta_pixel": 20, "no_whatsapp": 20, "outdated_site": 10}),
    "reputation": ("Reputation and listings", "Listing, review and signage services", {"not_on_map": 30, "no_opening_hours": 25,
                                                                                         "no_address": 20, "no_booking": 15, "no_social": 10}),
    "payments": ("Payments and ordering", "Banks, mobile money, POS and e-commerce", {"no_card_payment": 40, "no_online_ordering": 35,
                                                                                         "no_booking": 15}),
    "logistics": ("Delivery and export", "Couriers, logistics firms and export agents", {"no_delivery": 45, "no_export": 40,
                                                                                            "seeking_distributors": 20}),
    "finance": ("Investment and finance", "Investors, banks and lenders", {"seeking_investment": 70, "no_lei": 25, "seeking_partners": 20}),
    "supply": ("Supplies and distribution", "Suppliers, wholesalers and distributors", {"seeking_suppliers": 60, "seeking_distributors": 50,
                                                                                         "seeking_partners": 25}),
    "hiring": ("Staff and training", "Recruiters, job seekers and trainers", {"hiring": 80}),
    "quality": ("Certification and quality", "Certification bodies and consultants", {"no_certification": 60, "no_lei": 15}),
    "access": ("Building and access", "Builders and accessibility contractors", {"not_accessible": 70, "no_address": 10}),
}
REACH = {"whatsapp": 8, "phone": 6, "email": 8}


def score(e: dict[str, Any], lens: str) -> tuple[int, list[dict[str, Any]]]:
    """(priority 0–100, reasons) for one business under a lens."""
    weights = LENSES.get(lens, LENSES["mixed"])[2]
    gaps = e.get("gaps", [])
    if not weights:                         # mixed: every observed item, stated needs first
        reasons = gaps
        raw = sum(30 if g["category"] == "Stated needs" else 8 for g in gaps)
    else:
        reasons = sorted([g for g in gaps if g["code"] in weights], key=lambda g: -weights[g["code"]])
        raw = sum(weights[g["code"]] for g in reasons)
    if not reasons:
        return 0, []
    reach = sum(v for k, v in REACH.items() if e.get("contacts", {}).get(k))
    return min(100, raw + reach), reasons


def opening_line(e: dict[str, Any], lens: str, reasons: list[dict[str, Any]]) -> str:
    if not reasons:
        return ""
    name, city = e["name"], e.get("city") or ""
    where = f" in {city}" if city else ""
    top = reasons[0]["code"]
    lines = {
        "no_website": f"I came across {name}{where} and couldn't find a website for you. Customers searching online may be missing you.",
        "site_down": f"I tried to visit {name}'s website and it didn't load. I wanted to let you know in case customers are seeing the same.",
        "not_mobile": f"I looked at {name}'s website on a phone and it isn't set up for small screens, which is where most visitors arrive.",
        "no_https": f"Browsers show {name}'s website as “Not secure” because it doesn't use HTTPS, which can put visitors off.",
        "outdated_site": f"I noticed {name}'s website still shows an older copyright year, so visitors may think it's out of date.",
        "slow_site": f"{name}'s home page took a few seconds to load when I checked, and slow pages lose visitors.",
        "no_meta_description": f"When {name} appears in search results, the snippet is chosen by the search engine because the site has no description of its own.",
        "no_tracking": f"I couldn't see any analytics on {name}'s website, so it may be hard to know where your visitors come from.",
        "no_meta_pixel": f"{name}'s website doesn't have a Meta pixel, so Facebook and Instagram ads can't be measured or retargeted.",
        "no_booking": f"I couldn't find a way to book with {name} online. Customers who want to book after hours may give up.",
        "no_social": f"I couldn't find social media links for {name}, which is often where customers check a business first.",
        "not_on_map": f"{name}{where} doesn't seem to be on OpenStreetMap, which feeds many map and navigation apps.",
        "no_opening_hours": f"I couldn't find {name}'s opening hours published anywhere, so customers can't tell when to visit.",
        "no_address": f"I couldn't find a street address for {name}, which makes it harder for customers and deliveries to find you.",
        "no_card_payment": f"I couldn't see card or mobile-money payment mentioned for {name}. Many customers now prefer paying that way.",
        "no_online_ordering": f"I couldn't find a way to order from {name} online.",
        "no_delivery": f"I couldn't see delivery mentioned for {name}{where}.",
        "no_export": f"{name} makes products but I couldn't see export mentioned. There may be buyers abroad for what you make.",
        "no_certification": f"I couldn't see a quality certification mentioned on {name}'s website. Certification can open doors to larger buyers.",
        "no_lei": f"I couldn't find {name} in the global LEI register, which many banks and foreign buyers now ask for.",
        "seeking_investment": f"I saw on {name}'s website that you're looking for investment.",
        "seeking_distributors": f"I saw on {name}'s website that you're looking for distributors.",
        "seeking_suppliers": f"I saw on {name}'s website that you're looking for suppliers.",
        "seeking_partners": f"I saw on {name}'s website that you're open to partnerships.",
        "hiring": f"I saw on {name}'s website that you're hiring.",
        "no_email": f"I couldn't find an email address for {name}, so I'm reaching out here instead.",
        "no_whatsapp": f"I couldn't find a WhatsApp number for {name}; many customers{where} prefer to message that way.",
        "not_accessible": f"The map lists {name} as not wheelchair accessible.",
    }
    return lines.get(top, f"I came across {name}{where} while looking at businesses like yours.")


def rank(entities: list[dict[str, Any]], lens: str) -> list[dict[str, Any]]:
    out = []
    for e in entities:
        pr, reasons = score(e, lens)
        out.append({**e, "priority": pr, "reasons": reasons, "opening": opening_line(e, lens, reasons)})
    return sorted(out, key=lambda x: (-x["priority"], x["name"].lower()))


# filter tabs shown above results: (key, label, test)
TABS = [
    ("all", "All", lambda e: True),
    ("match", "Matches you", lambda e: e["priority"] > 0),
    ("no_website", "No website", lambda e: "no_website" in e["gap_codes"]),
    ("no_meta_description", "No meta description", lambda e: "no_meta_description" in e["gap_codes"]),
    ("slow_site", "Slow site", lambda e: "slow_site" in e["gap_codes"]),
    ("not_mobile", "Not mobile-ready", lambda e: "not_mobile" in e["gap_codes"]),
    ("needs", "Stated needs", lambda e: any(g["category"] == "Stated needs" for g in e["gaps"])),
    ("whatsapp", "Has WhatsApp", lambda e: bool(e["contacts"].get("whatsapp"))),
    ("email", "Has email", lambda e: bool(e["contacts"].get("email"))),
]
