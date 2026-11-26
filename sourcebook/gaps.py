"""What a business needs or doesn't have yet — opportunities for anyone who can supply it, not only software makers.

Every item comes from a check Sourcebook actually made: the map tags, the business's own website, the contacts published
across the sources it read, the company register. Missing things are worded as "not found"; needs the business states
itself keep a short quote and the page it's on."""

from __future__ import annotations

import time
from typing import Any

CATEGORIES = ["Stated needs", "Money and registration", "Sales and delivery", "Quality and compliance", "Visibility and information",
              "Online presence", "Access"]

# code: (what's missing or needed, who could help, category)
GAPS: dict[str, tuple[str, str, str]] = {
    # the business says so itself
    "seeking_investment": ("Says it’s looking for investment or funding", "Investors, banks and lenders", "Stated needs"),
    "seeking_distributors": ("Says it’s looking for distributors or agents", "Distributors, wholesalers and sales agents", "Stated needs"),
    "seeking_suppliers": ("Says it’s looking for suppliers or quotes", "Suppliers of materials, goods and services", "Stated needs"),
    "seeking_partners": ("Says it’s looking for partners", "Partners, joint-venture and trade contacts", "Stated needs"),
    "hiring": ("Says it’s hiring", "Job seekers, recruiters and training providers", "Stated needs"),
    # money and registration
    "no_lei": ("No Legal Entity Identifier (LEI)", "LEI registration agents, banks and trade-finance providers", "Money and registration"),
    "no_card_payment": ("No card or mobile-money payment shown", "Banks, mobile-money and card-terminal providers", "Money and registration"),
    # sales and delivery
    "no_delivery": ("No delivery mentioned", "Delivery, courier and logistics companies", "Sales and delivery"),
    "no_export": ("Makes products but mentions no export", "Export agents, freight forwarders and trade promoters", "Sales and delivery"),
    "no_online_ordering": ("No online ordering or payment", "E-commerce, marketplaces and payment providers", "Sales and delivery"),
    # quality and compliance
    "no_certification": ("Makes products but mentions no quality certification", "Certification bodies, quality and food-safety consultants",
                         "Quality and compliance"),
    # visibility and information
    "no_opening_hours": ("Opening hours not published", "Listing, signage and marketing services", "Visibility and information"),
    "no_address": ("No street address published", "Local-listing, signage and mapping services", "Visibility and information"),
    "not_on_map": ("Not on OpenStreetMap", "Mapping and local-listing services", "Visibility and information"),
    "no_email": ("No email address published", "Business email and IT setup", "Visibility and information"),
    "no_whatsapp": ("No WhatsApp number published", "WhatsApp Business setup and marketing", "Visibility and information"),
    "no_phone": ("No phone number published", "Telephony and customer-service providers", "Visibility and information"),
    "no_social": ("No social media links found", "Social media managers and marketers", "Visibility and information"),
    # online presence
    "no_website": ("No website found", "Web designers and developers", "Online presence"),
    "site_down": ("Website doesn’t load", "Web hosting and IT support", "Online presence"),
    "no_https": ("Website isn’t secure (no HTTPS)", "Web hosting and IT support", "Online presence"),
    "not_mobile": ("Website isn’t set up for phones", "Web designers and developers", "Online presence"),
    "outdated_site": ("Website looks out of date", "Web designers and developers", "Online presence"),
    "no_products_listed": ("Website doesn’t list its products", "Content writers, photographers and catalogue builders", "Online presence"),
    "no_contact_form": ("No contact form on the website", "Web designers and developers", "Online presence"),
    "slow_site": ("Website is slow", "Web developers and hosting providers", "Online presence"),
    "no_meta_description": ("No meta description", "SEO and local-search specialists", "Online presence"),
    "no_tracking": ("No analytics or tracking tag", "Analytics and marketing specialists", "Online presence"),
    "no_meta_pixel": ("No Meta (Facebook) pixel", "Paid-ads specialists", "Online presence"),
    "no_booking": ("No online booking", "Booking and scheduling tools", "Sales and delivery"),
    # access
    "not_accessible": ("Not wheelchair accessible (according to the map)", "Accessibility contractors and builders", "Access"),
}
ORDER = list(GAPS)
SLOW_SECONDS = 3.0
# kinds of business where customers book time: appointments, tables, rooms
BOOKING_KINDS = ("dent", "clinic", "doctor", "salon", "beauty", "barber", "spa", "hotel", "guest", "restaurant", "lawyer", "accountant",
                 "physio", "vet", "tutor", "school", "gym", "fitness", "studio", "repair", "mechanic", "consult", "therap", "photograph",
                 "hairdress", "nail", "massage", "tour", "travel", "event", "driving")
NEEDS = {k for k, v in GAPS.items() if v[2] == "Stated needs"}


def label(code: str) -> str:
    return GAPS.get(code, (code, "", ""))[0]


def helper(code: str) -> str:
    return GAPS.get(code, ("", "", ""))[1]


def category(code: str) -> str:
    return GAPS.get(code, ("", "", "Other"))[2]


def grouped() -> list[tuple[str, list[tuple[str, tuple[str, str, str]]]]]:
    return [(cat, [(k, v) for k, v in GAPS.items() if v[2] == cat]) for cat in CATEGORIES]


def find(*, website: str, site: Any, facts: list[dict[str, Any]], on_map: bool, product_terms: bool = True,
         osm_tags: dict[str, str] | None = None, has_lei: bool | None = None, booking_matters: bool = True) -> list[tuple[str, str, str]]:
    """Returns (code, detail, source_url) for each need or gap the checks support."""
    out: list[tuple[str, str, str]] = []
    tags = osm_tags or {}
    fields = {f["field"] for f in facts}
    any_source = next((f["source_url"] for f in facts if f["source_url"].startswith("http")), "")
    osm_url = next((f["source_url"] for f in facts if f["source_type"] == "osm"), "")
    read = site is not None and bool(site.pages)
    signals = site.signals if read else set()
    home = (site.final_url or site.url) if site is not None else ""

    # needs the business states itself
    if read:
        for code in ("seeking_investment", "seeking_distributors", "seeking_suppliers", "seeking_partners", "hiring"):
            if code in site.statements:
                quote, page = site.statements[code]
                out.append((code, f"Its website says: “{quote}”", page))

    # online presence
    if not website and "website" not in fields:
        out.append(("no_website", "None of the sources checked lists a website for this business.", any_source))
    elif site is not None:
        if not site.pages and not site.blocked:
            out.append(("site_down", f"When checked, {site.error or 'the website could not be loaded'}.", site.url))
        elif read:
            if not home.startswith("https://"):
                out.append(("no_https", "The website opens over plain http, so browsers mark it “Not secure”.", home))
            if "viewport" not in signals:
                out.append(("not_mobile", "The pages don’t tell phones how to display them (no viewport setting).", home))
            if site.years and max(site.years) <= time.gmtime().tm_year - 3:
                out.append(("outdated_site", f"The newest copyright year on the site is {max(site.years)}.", home))
            if product_terms and not site.products:
                out.append(("no_products_listed", "None of this page’s product terms appear on the pages read.", home))
            if "contact_form" not in signals:
                out.append(("no_contact_form", "No enquiry form was found on the pages read.", home))
            if site.load_seconds is not None and site.load_seconds >= SLOW_SECONDS:
                out.append(("slow_site", f"The home page took {site.load_seconds:.1f} seconds to answer.", home))
            if "meta_description" not in signals:
                out.append(("no_meta_description", "The home page has no meta description, so search engines write their own snippet.", home))
            if "tracking" not in signals:
                out.append(("no_tracking", "No Google Analytics, Tag Manager or similar tag was found on the pages read.", home))
            if "meta_pixel" not in signals:
                out.append(("no_meta_pixel", "No Meta (Facebook) pixel was found, so ads can't be measured or retargeted.", home))
            if "booking" not in signals and booking_matters:
                out.append(("no_booking", "No booking, appointment or reservation option was found on the pages read.", home))

    # sales and delivery
    delivery_tag = tags.get("delivery")
    if delivery_tag == "no":
        out.append(("no_delivery", "The map lists this place as not offering delivery.", osm_url))
    elif read and "delivery" not in signals and not delivery_tag:
        out.append(("no_delivery", "No delivery, shipping or dispatch is mentioned on the pages read.", home))
    if read and "ordering" not in signals:
        out.append(("no_online_ordering", "No cart, checkout, “order online” or payment option was found on the pages read.", home))
    producer = read and "producer" in signals
    if producer and "export" not in signals:
        out.append(("no_export", "The website describes making or processing products but doesn’t mention export.", home))

    # quality and compliance
    if producer and "certified" not in signals:
        out.append(("no_certification", "The website describes making or processing products but mentions no certification "
                                        "(such as ISO, HACCP, GMP, organic, halal or a national standards mark).", home))

    # money and registration
    card_tags = {k: v for k, v in tags.items() if k.startswith("payment:")}
    if card_tags and not any(v == "yes" for k, v in card_tags.items() if any(w in k for w in ("card", "visa", "mastercard", "mobile", "momo", "mpesa", "contactless"))):
        out.append(("no_card_payment", "The map lists payment methods for this place, but no card or mobile money.", osm_url))
    elif read and not card_tags and "card_payment" not in signals and "ordering" not in signals:
        out.append(("no_card_payment", "No card, mobile-money or online payment is mentioned on the pages read.", home))
    if has_lei is False:
        out.append(("no_lei", "No active record with this name was found in the GLEIF register for this country.",
                    "https://search.gleif.org/"))

    # visibility and information
    if not tags.get("opening_hours") and not (read and "hours" in signals):
        where = "on the map or the website" if read and osm_url else "on the website" if read else "on the map" if osm_url else ""
        if where:
            out.append(("no_opening_hours", f"No opening hours are published {where}.", osm_url or home))
    has_street = bool(tags.get("addr:street")) or any(f["field"] == "address" for f in facts) or (read and "address" in signals)
    if not has_street and (osm_url or read):
        out.append(("no_address", "No street address is published in the sources checked.", osm_url or home))
    if not on_map:
        out.append(("not_on_map", "This business wasn’t found among the OpenStreetMap places searched for this page.",
                    "https://www.openstreetmap.org/"))
    checked = "on the website or in the other sources checked" if read else "in the sources checked"
    if "email" not in fields:
        out.append(("no_email", f"No email address is published {checked}.", website or any_source))
    if "whatsapp" not in fields:
        out.append(("no_whatsapp", f"No WhatsApp link or number is published {checked}.", website or any_source))
    if "phone" not in fields and "whatsapp" not in fields:
        out.append(("no_phone", f"No phone number is published {checked}.", website or any_source))
    social = {"facebook", "instagram", "linkedin", "x", "tiktok"} & fields
    if not social and read:
        out.append(("no_social", "The website doesn’t link to Facebook, Instagram, LinkedIn or X.", home))

    # access
    if tags.get("wheelchair") == "no":
        out.append(("not_accessible", "The map marks this place as not wheelchair accessible.", osm_url))
    return out
