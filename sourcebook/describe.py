"""Write a clear, short description of a business from its collected facts only — every sentence keeps its sources.
Optional: set SOURCEBOOK_LLM=anthropic and ANTHROPIC_API_KEY to have Claude rephrase the same facts more naturally
(it is told not to add anything that isn't in the facts)."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from .db import SOURCE_LABEL


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def template_description(name: str, kind_label: str, country: str, facts: list[dict[str, Any]], products: list[tuple[str, str]],
                         about: list[tuple[str, str]], city: str) -> tuple[str, list[str]]:
    """Returns (description, sources). Plain sentences, nothing claimed without a source."""
    sentences, sources = [], []
    place = f"{city}, {country}" if city else country
    osm = next((f for f in facts if f["source_type"] == "osm"), None)
    first_source = osm or (facts[0] if facts else None)
    sentences.append(f"{name} is a {kind_label} in {place}.")
    if first_source:
        sources.append(first_source["source_url"])
    if products:
        names = [p for p, _ in products[:6]]
        sentences.append(f"It lists {_join(names)} among what it supplies.")
        sources.extend(sorted({u for _, u in products[:6]}))
    if about:
        text, url = about[0]
        text = re.sub(r"\s+", " ", text).strip().rstrip(".")
        if 30 <= len(text) <= 300:
            sentences.append(f"In its own words: “{text}.”")
            sources.append(url)
    channels = sorted({f["field"] for f in facts if f["field"] in ("phone", "email", "whatsapp", "telegram")})
    if channels:
        label = {"phone": "phone", "email": "email", "whatsapp": "WhatsApp", "telegram": "Telegram"}
        where = sorted({SOURCE_LABEL.get(f["source_type"], f["source_type"]) for f in facts if f["field"] in channels})
        sentences.append(f"It publishes {_join([label[c] for c in channels])} contact details on {_join(where)}.")
    return " ".join(sentences), list(dict.fromkeys(sources))


async def llm_rephrase(text: str, facts: list[dict[str, Any]]) -> str | None:
    if os.environ.get("SOURCEBOOK_LLM") != "anthropic" or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    payload = {"description": text, "facts": [{k: f[k] for k in ("field", "value", "source_type")} for f in facts[:40]]}
    prompt = ("Rewrite this business description in 2–3 clear, neutral sentences for a public directory. Use ONLY the information "
              "in the description and facts; do not add claims, adjectives of quality, or anything not stated. Reply with the text only.\n\n"
              + json.dumps(payload, ensure_ascii=False))
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post("https://api.anthropic.com/v1/messages", headers={
                "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
                json={"model": os.environ.get("SOURCEBOOK_LLM_MODEL", "claude-sonnet-5-5"), "max_tokens": 300,
                      "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status()
            out = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
            return out or None
    except (httpx.HTTPError, ValueError, KeyError):
        return None
