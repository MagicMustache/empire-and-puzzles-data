#!/usr/bin/env python3
"""
Empires & Puzzles hero scraper.

Reads every hero from the fandom wiki via the MediaWiki API and writes a
structured `heroes.json`. See README.md for the output schema and for notes on
extending this.

Why the API and not HTML scraping:

  * `/wiki/*` pages sit behind Cloudflare and start returning challenge pages
    and then 403s under any sustained crawl. `api.php` does not.
  * One request returns 50 complete pages, so the whole wiki is ~130 requests
    instead of ~1200 -- seconds rather than half an hour.
  * The wikitext is already structured. Stats arrive as separate `bpower` and
    `power` fields, so base and maxed values cannot be confused; costumes are
    separate `{{Hero}}` blocks rather than repeated HTML tables that have to be
    told apart by position.

Usage:
    python scrape_heroes.py                  # full run
    python scrape_heroes.py --limit 25       # quick smoke test
    python scrape_heroes.py --no-network     # re-parse the cache only
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
import time
from typing import Any, Iterable

import requests

from wikitext import (
    clean_effect,
    file_title,
    find_release,
    is_ambiguous_number,
    iter_template_blocks,
    normalize_bullets,
    parse_int,
    parse_switch,
    split_params,
    strip_markup,
    template_args,
)

WIKI = "https://empiresandpuzzles.fandom.com"
API = f"{WIKI}/api.php"

# MediaWiki asks crawlers to identify themselves and to back off when the
# database lags. Both are cheap to honour and keep us off the rate limiter.
# Put a real contact address here if you run this at any volume.
USER_AGENT = "EPHeroScraper/2.0 (python-requests; personal hero database)"
MAXLAG = 5

HERO_CATEGORIES = [
    "Category:1 Star Heroes",
    "Category:2 Star Heroes",
    "Category:3 Star Heroes",
    "Category:4 Star Heroes",
    "Category:5 Star Heroes",
    # Catches heroes missing a star category (currently Magistine). It also
    # pulls in a handful of meta pages such as "Leveling" and "Museum"; those
    # carry no {{Hero}} template and are dropped, and listed in the report.
    "Category:Heroes",
]

# Meta pages in Category:Heroes that are known not to be heroes. Listing them
# keeps the report's "no {{Hero}} template" warning meaningful -- that warning
# should mean something went wrong, not scroll past a known-good baseline.
KNOWN_NON_HERO_PAGES = {
    "Costumes",
    "Hero Academy/Training",
    "Hero Information",
    "Hero of the Month",
    "Leveling",
    "Museum",
    "Passives",
    "Special Skills",
    "Tanks",
    "Trainer Heroes",
}

# Pages that live in the hero categories but are not playable heroes. Nothing
# is dropped silently: every exclusion is listed in the run report, and
# `--include-all` keeps them. Each hero also carries its full `categories`
# list, so downstream filtering does not require changing this script.
EXCLUDE_CATEGORIES = {"Category:Mimic Family"}

STAR_BY_CATEGORY = {f"Category:{n} Star Heroes": n for n in range(1, 6)}
STAR_BY_RARITY = {
    "Common": 1,
    "Uncommon": 2,
    "Rare": 3,
    "Epic": 4,
    "Legendary": 5,
}

# Infobox fields whose value is a `{{code}}` lookup rather than literal text.
# The templates backing them are fetched and parsed at runtime, so new families
# or aether powers need no change here.
LOOKUP_FIELDS = ("element", "rarity", "class", "family", "realm", "aether")
DESCRIPTIVE_FIELDS = ("passive", "resist", "trait")

STAT_GROUPS = {
    "base": ("bpower", "battack", "bdefense", "bhealth"),
    "maxed": ("power", "attack", "defense", "health"),
    "costume_bonus": ("cbpower", "cbattack", "cbdefense", "cbhealth"),
}


# ==========================================================================
#  API client
# ==========================================================================


class WikiClient:
    """Thin MediaWiki API wrapper with retries and query continuation."""

    def __init__(self, verbose: bool = False):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.verbose = verbose
        self.request_count = 0

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"      {msg}", file=sys.stderr)

    def get(self, params: dict[str, Any], attempts: int = 5) -> dict:
        """Issue one API request, retrying on throttling and server errors."""
        params = {**params, "format": "json", "formatversion": "2", "maxlag": MAXLAG}
        delay = 1.0
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.get(API, params=params, timeout=30)
                self.request_count += 1

                if resp.status_code in (429, 502, 503, 504):
                    wait = float(resp.headers.get("Retry-After", delay))
                    self._log(f"HTTP {resp.status_code}, retrying in {wait:.0f}s")
                    time.sleep(wait)
                    delay = min(delay * 2, 60)
                    continue

                resp.raise_for_status()
                data = resp.json()

                # `maxlag` rejections come back as HTTP 200 with an error body.
                error = data.get("error", {})
                if error.get("code") == "maxlag":
                    wait = float(resp.headers.get("Retry-After", delay))
                    self._log(f"replica lag, retrying in {wait:.0f}s")
                    time.sleep(wait)
                    delay = min(delay * 2, 60)
                    continue
                if error:
                    raise RuntimeError(f"API error: {error}")
                return data

            except (requests.RequestException, ValueError) as exc:
                if attempt == attempts:
                    raise
                self._log(f"{type(exc).__name__}: {exc}; retrying in {delay:.0f}s")
                time.sleep(delay)
                delay = min(delay * 2, 60)

        raise RuntimeError(f"gave up after {attempts} attempts: {params}")

    def query_all(self, **params: Any) -> dict[int, dict]:
        """Run an `action=query`, following continuation to completion.

        Pages are merged by page id and list-valued properties are extended
        rather than overwritten, because a page's categories can be split
        across continuation batches.
        """
        pages: dict[int, dict] = {}
        cont: dict[str, Any] = {}
        while True:
            data = self.get({"action": "query", **params, **cont})
            for page in data.get("query", {}).get("pages", []):
                pid = page.get("pageid")
                if pid is None:
                    continue
                if pid not in pages:
                    pages[pid] = page
                    continue
                merged = pages[pid]
                for key, value in page.items():
                    if isinstance(value, list):
                        merged.setdefault(key, []).extend(value)
                    else:
                        merged.setdefault(key, value)
            if "continue" not in data:
                return pages
            cont = data["continue"]


def batched(items: Iterable[str], size: int) -> Iterable[list[str]]:
    batch: list[str] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


# ==========================================================================
#  Fetching
# ==========================================================================


def fetch_hero_pages(client: WikiClient, categories: list[str]) -> dict[str, dict]:
    """Fetch wikitext and categories for every page in the hero categories.

    `generator=categorymembers` collapses what used to be two passes -- listing
    the category, then fetching each hero -- into a single query returning 50
    full pages at a time.
    """
    heroes: dict[str, dict] = {}
    for category in categories:
        print(f"   -> {category}")
        pages = client.query_all(
            generator="categorymembers",
            gcmtitle=category,
            gcmnamespace=0,  # articles only; skips File:/Template:/Category: members
            gcmlimit="max",
            prop="revisions|categories",
            rvprop="content",
            rvslots="main",
            cllimit="max",
        )
        for page in pages.values():
            title = page["title"]
            revisions = page.get("revisions") or []
            if not revisions:
                continue
            record = heroes.setdefault(
                title,
                {
                    "pageid": page["pageid"],
                    "title": title,
                    "wikitext": revisions[0]["slots"]["main"]["content"],
                    "categories": [],
                },
            )
            known = set(record["categories"])
            for cat in page.get("categories", []):
                if cat["title"] not in known:
                    known.add(cat["title"])
                    record["categories"].append(cat["title"])
        print(f"      {len(pages)} pages ({len(heroes)} unique so far)")
    return heroes


def fetch_template_maps(client: WikiClient, names: Iterable[str]) -> dict[str, dict[str, str]]:
    """Fetch and parse the `{{#switch:}}` lookup templates used by the heroes.

    The set of templates is discovered from the hero pages themselves, so a new
    lookup template added to the wiki is picked up automatically. `redirects=1`
    resolves aliases such as `Template:El` -> `Template:Element`.
    """
    maps: dict[str, dict[str, str]] = {}
    titles = sorted({f"Template:{n}" for n in names})
    for batch in batched(titles, 50):
        data = client.get(
            {
                "action": "query",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "titles": "|".join(batch),
                "redirects": "1",
            }
        )
        query = data.get("query", {})
        # Map every requested title back to whatever it resolved to.
        resolved = {r["from"]: r["to"] for r in query.get("redirects", [])}
        resolved.update({n["from"]: n["to"] for n in query.get("normalized", [])})

        by_title: dict[str, dict[str, str]] = {}
        for page in query.get("pages", []):
            if page.get("missing") or not page.get("revisions"):
                continue
            source = page["revisions"][0]["slots"]["main"]["content"]
            by_title[page["title"]] = parse_switch(source)

        for title in batch:
            target = title
            for _ in range(4):  # follow redirect chains
                target = resolved.get(target, target)
            table = by_title.get(target)
            if table:
                maps[title.split(":", 1)[1].lower()] = table
    return maps


def fetch_image_urls(client: WikiClient, titles: Iterable[str]) -> dict[str, str]:
    """Resolve `File:` titles to direct URLs, 50 per request."""
    urls: dict[str, str] = {}
    wanted = sorted({t for t in titles if t})
    for i, batch in enumerate(batched(wanted, 50), start=1):
        data = client.get(
            {
                "action": "query",
                "prop": "imageinfo",
                "iiprop": "url",
                "titles": "|".join(batch),
                "redirects": "1",
            }
        )
        query = data.get("query", {})
        aliases: dict[str, str] = {}
        for key in ("normalized", "redirects"):
            for entry in query.get(key, []):
                aliases[entry["from"]] = entry["to"]

        by_title: dict[str, str] = {}
        for page in query.get("pages", []):
            info = page.get("imageinfo") or []
            if info:
                by_title[page["title"]] = info[0]["url"]

        for title in batch:
            target = title
            for _ in range(4):
                target = aliases.get(target, target)
            if target in by_title:
                urls[title] = by_title[target]
        print(f"      batch {i}: {len(urls)}/{len(wanted)} resolved")
    return urls


# ==========================================================================
#  Parsing
# ==========================================================================


class Resolver:
    """Turns `{{template|code}}` values into human-readable labels.

    Also accumulates parse diagnostics. Anything unresolvable is recorded
    rather than dropped, so a degraded run is visible in the report instead of
    quietly producing empty fields.
    """

    def __init__(self, maps: dict[str, dict[str, str]]):
        self.maps = maps
        self.unknown: collections.Counter = collections.Counter()
        self.ambiguous_stats: collections.Counter = collections.Counter()
        self.repaired: collections.Counter = collections.Counter()

        # The set of real class and element names, taken from the templates
        # themselves rather than hardcoded, so new ones need no code change.
        self.classes = sorted(
            {strip_markup(v) for v in maps.get("cl", {}).values()} - {""}
        )
        self.elements = sorted(
            {strip_markup(v) for v in maps.get("el", {}).values()} - {""}
        )

    def label(self, raw: str) -> str | None:
        """Resolve a single-value field such as `element` or `family`."""
        values = self.values(raw)
        return values[0]["name"] if values else None

    def values(self, raw: str) -> list[dict[str, str]]:
        """Resolve a field that may hold several `{{tpl|code}}` calls.

        Returns `[{"name": ..., "description": ...}]`. Passive and resist
        templates render as a title followed by a paragraph of rules text, so
        the first line becomes the name and the remainder the description.
        """
        raw = (raw or "").strip()
        if not raw:
            return []

        out: list[dict[str, str]] = []
        consumed_any = False
        for start, end, body in iter_template_blocks(raw, r"[A-Za-z][\w ]*?\s*(?=\|)"):
            name = re.match(r"\{\{\s*([^|{}]+)", raw[start:end])
            if not name:
                continue
            tpl = name.group(1).strip().lower()
            table = self.maps.get(tpl)
            if table is None:
                continue
            consumed_any = True
            args = split_params(body)
            code = strip_markup(args[1]).strip().lower() if len(args) > 1 else ""
            if code not in table:
                self.unknown[f"{tpl}|{code}"] += 1
                continue
            out.append(self._describe(table[code]))

        if not consumed_any:
            # Plain text, e.g. `resist = RESIST MANA AILMENTS`.
            text = strip_markup(raw)
            if text:
                out.append(self._describe(text))
        return out

    @staticmethod
    def _describe(rendered: str) -> dict[str, str]:
        text = normalize_bullets(strip_markup(rendered))
        name, _, description = text.partition("\n")
        return {"name": name.strip(), "description": description.strip()}

    def repair_from_categories(self, forms: list[dict], categories: list[str]) -> None:
        """Fill in `class`/`element` that a wiki typo left unresolved.

        A handful of pages misspell the template code -- `{{cl|src}}` instead
        of `{{cl|scr}}`, `{{el|nadarkture}}` instead of `{{el|dark}}`. The
        wiki's own `#switch` has no `#default`, so those render blank on the
        site too; the previous scraper picked the blank up as an empty string.

        Rather than guess, fall back to the page's category memberships, which
        are real wiki data: a Sorcerer is in `Category:Sorcerer`. Only applied
        when exactly one candidate remains, so an ambiguous case stays empty
        and gets reported instead of being invented.
        """
        cats = set(categories)

        missing = [f for f in forms if not f["element"]]
        if missing:
            candidates = [e for e in self.elements if f"Category:{e} Heroes" in cats]
            if len(candidates) == 1:
                for form in missing:
                    form["element"] = candidates[0]
                self.repaired[f"element -> {candidates[0]}"] += len(missing)

        # Costumes can change class, so a page may carry several class
        # categories. Subtract the ones already accounted for by other forms.
        missing = [f for f in forms if not f["class"]]
        if len(missing) == 1:
            candidates = {c for c in self.classes if f"Category:{c}" in cats}
            candidates -= {f["class"] for f in forms if f["class"]}
            if len(candidates) == 1:
                value = candidates.pop()
                missing[0]["class"] = value
                self.repaired[f"class -> {value}"] += 1


def discover_lookup_templates(pages: Iterable[dict]) -> set[str]:
    """Collect the template names used inside `{{Hero}}` parameter values.

    Discovering these from the hero pages rather than hardcoding a list means
    a lookup template newly added to the wiki is picked up automatically.
    Names are folded to a single case, since MediaWiki treats `{{el}}` and
    `{{El}}` as the same page and we do not want to fetch both.
    """
    names: dict[str, str] = {}
    for page in pages:
        for _s, _e, body in iter_template_blocks(page["wikitext"], r"Hero\s*(?=[|}\n])"):
            named, _ = template_args(body)
            for field in LOOKUP_FIELDS + DESCRIPTIVE_FIELDS:
                for match in re.finditer(
                    r"\{\{\s*([A-Za-z][\w ]*?)\s*\|", named.get(field, "")
                ):
                    name = match.group(1).strip()
                    names.setdefault(name.lower(), name)
    return set(names.values())


_HEADING = re.compile(r"^\s*(={2,})\s*(.+?)\s*\1\s*$", re.M)
_FILE_IN_TEXT = re.compile(r"\[\[\s*(?:File|Image)\s*:([^\[\]|]+)", re.IGNORECASE)
_CARD_NAME = re.compile(r"hero[\s_%-]*card", re.IGNORECASE)


def _find_card(text: str, start: int, end: int) -> str | None:
    """Pick a form's hero-card file from the wikitext between two templates.

    Prefer an explicit "Hero Card" file name and fall back to the first file
    link in the gap. Returning `None` when the gap holds no file at all is
    intentional -- an unrelated image is worse than a null, because callers
    cannot tell it apart from a real card.
    """
    names = [m.group(1) for m in _FILE_IN_TEXT.finditer(text, start, end)]
    if not names:
        return None
    return next((n for n in names if _CARD_NAME.search(n)), names[0])


_HERO_MARKERS = ("special_name", "element", "rarity", "power", "bpower", "cbpower")


def _is_hero_block(body: str) -> bool:
    """Reject `{{Hero...}}`-lookalike templates that carry no hero data."""
    named, _ = template_args(body)
    return any(named.get(k) for k in _HERO_MARKERS)


def _form_id(heading: str | None, index: int) -> str:
    if index == 0 and not heading:
        return "base"
    if not heading:
        return f"form_{index}"
    slug = re.sub(r"[^a-z0-9]+", "_", heading.lower()).strip("_")
    return slug or f"form_{index}"


def _stats(named: dict[str, str], title: str,
           resolver: Resolver) -> dict[str, dict[str, int] | None]:
    """Read the three stat blocks, keeping them strictly separate.

    `base`, `maxed` and `costume_bonus` come from distinct template
    parameters, so they cannot be confused with one another -- which is the
    failure mode of reading positional HTML tables, where a page that happens
    to omit its Base Stats section silently yields maxed values instead.

    Returning `None` for an absent block is likewise deliberate: the previous
    scraper wrote `0` for stats it failed to read, making them impossible to
    tell apart from real data.
    """
    out: dict[str, dict[str, int] | None] = {}
    for group, keys in STAT_GROUPS.items():
        values = {}
        for field, key in zip(("power", "attack", "defense", "hp"), keys):
            raw = named.get(key)
            values[field] = parse_int(raw)
            if is_ambiguous_number(raw):
                resolver.ambiguous_stats[f"{title}: {key} = {raw.strip()!r}"] += 1
        out[group] = None if all(v is None for v in values.values()) else values
    return out


def _special_skill(named: dict[str, str]) -> dict[str, Any]:
    effects = []
    for key in sorted(
        (k for k in named if re.fullmatch(r"effect\d+", k)),
        key=lambda k: int(k[6:]),
    ):
        text = clean_effect(named[key])
        if text:
            effects.append(text)
    return {
        "name": strip_markup(named.get("special_name", "")),
        "speed": strip_markup(named.get("mana_speed", "")),
        "effects": effects,
    }


def parse_hero(page: dict, resolver: Resolver) -> dict | None:
    """Turn one wiki page into a hero record with one entry per form."""
    text = page["wikitext"]
    blocks = [
        block for block in iter_template_blocks(text, r"Hero\s*(?=[|}\n])")
        if _is_hero_block(block[2])
    ]
    if not blocks:
        return None

    headings = [(m.start(), strip_markup(m.group(2))) for m in _HEADING.finditer(text)]

    forms = []
    for index, (start, end, body) in enumerate(blocks):
        named, _ = template_args(body)

        # The heading this form sits under, e.g. "Costume 2". The lead form has
        # none. Headings are used as-is rather than pattern-matched, because
        # the wiki writes "Costume", "Costume 1" and "Costume 2" inconsistently.
        heading = next((h for pos, h in reversed(headings) if pos < start), None)

        # A form's hero card, and the sentence dating its release, both sit in
        # the wikitext between this template and the next one. Reading the date
        # per form rather than per page is what keeps a costume released in
        # 2025 from being mistaken for the 2017 hero's own release date.
        next_start = blocks[index + 1][0] if index + 1 < len(blocks) else len(text)
        card = _find_card(text, end, next_start)
        release = find_release(text[end:next_start], page["title"]) or {}

        forms.append(
            {
                "form": _form_id(heading, index),
                "section": heading,
                "title": strip_markup(named.get("title1", "")) or page["title"],
                "epithet": strip_markup(named.get("caption1", "")),
                "release_date": release.get("date"),
                "release_date_precision": release.get("precision"),
                "release_note": release.get("source"),
                "element": resolver.label(named.get("element", "")),
                "rarity": resolver.label(named.get("rarity", "")),
                "class": resolver.label(named.get("class", "")),
                "family": resolver.label(named.get("family", "")),
                "realm": resolver.label(named.get("realm", "")),
                "aether_power": resolver.label(named.get("aether", "")),
                "passives": resolver.values(named.get("passive", "")),
                "resists": resolver.values(named.get("resist", "")),
                "traits": resolver.values(named.get("trait", "")),
                "stats": _stats(named, page["title"], resolver),
                "special_skill": _special_skill(named),
                "_portrait_file": file_title(named.get("image", "")),
                "_card_file": file_title(card) if card else None,
            }
        )

    categories = page.get("categories", [])
    resolver.repair_from_categories(forms, categories)

    stars = next(
        (STAR_BY_CATEGORY[c] for c in categories if c in STAR_BY_CATEGORY),
        STAR_BY_RARITY.get(forms[0]["rarity"]),
    )

    return {
        "name": page["title"],
        "page_id": page["pageid"],
        "url": f"{WIKI}/wiki/" + page["title"].replace(" ", "_"),
        # Convenience mirror of the base form, for consumers that ignore costumes.
        "element": forms[0]["element"],
        "rarity": forms[0]["rarity"],
        "stars": stars,
        "class": forms[0]["class"],
        "family": forms[0]["family"],
        "epithet": forms[0]["epithet"],
        # The hero's own release, i.e. the base form's. Costume dates stay on
        # the costume; a hero whose page only ever dated its costumes gets
        # `None` here rather than borrowing one of them.
        "release_date": forms[0]["release_date"],
        "release_date_precision": forms[0]["release_date_precision"],
        "forms": forms,
        "categories": [c.replace("Category:", "") for c in categories],
    }


def attach_images(heroes: list[dict], urls: dict[str, str]) -> int:
    """Replace the internal `_*_file` markers with resolved URLs.

    Unlike the previous scraper, a form with no card image gets `None` rather
    than an unrelated image that happened to appear first on the page.
    """
    unresolved = 0
    for hero in heroes:
        for form in hero["forms"]:
            for key, out in (("_portrait_file", "portrait"), ("_card_file", "card")):
                title = form.pop(key)
                form[out] = urls.get(title) if title else None
                if title and not form[out]:
                    unresolved += 1
    return unresolved


# ==========================================================================
#  Reporting
# ==========================================================================


def report(heroes: list[dict], resolver: Resolver, skipped: list[str],
           excluded: list[str], unresolved_images: int) -> None:
    """Print a data-quality summary.

    The point is that a degraded run should look different from a good one.
    The previous scraper printed the same cheerful summary whether it had
    parsed every hero or been served Cloudflare challenges throughout.
    """
    forms = [f for h in heroes for f in h["forms"]]
    missing_maxed = [
        h["name"] for h in heroes
        if h["forms"][0]["stats"]["maxed"] is None
        and h["forms"][0]["stats"]["costume_bonus"] is None
    ]
    no_card = sum(1 for f in forms if not f["card"])

    print("\n" + "=" * 62)
    print("RUN REPORT")
    print("=" * 62)
    print(f"  heroes            {len(heroes)}")
    print(f"  forms             {len(forms)} ({len(forms) - len(heroes)} costumes)")
    print(f"  with base stats   {sum(1 for f in forms if f['stats']['base'])}")
    print(f"  with maxed stats  {sum(1 for f in forms if f['stats']['maxed'])}")
    print(f"  with costume bonus{sum(1 for f in forms if f['stats']['costume_bonus']):>4}")
    print(f"  forms w/o card    {no_card}")
    print(f"  unresolved images {unresolved_images}")

    dated = [h for h in heroes if h["release_date"]]
    month_only = [h for h in dated if h["release_date_precision"] == "month"]
    print(f"  with release date {len(dated)} ({len(month_only)} to the month only)")
    print(f"  dated costumes    "
          f"{sum(1 for f in forms if f['form'] != 'base' and f['release_date'])}")
    if dated:
        span = sorted(h["release_date"] for h in dated)
        print(f"  release span      {span[0]} .. {span[-1]}")

    by_stars = collections.Counter(h["stars"] for h in heroes)
    print("  by rarity         " + ", ".join(
        f"{k}*={v}" for k, v in sorted(by_stars.items(), key=lambda kv: (kv[0] or 0))
    ))

    def listing(label: str, items, limit: int = 12) -> None:
        items = sorted(items)
        if not items:
            return
        print(f"\n  {label} ({len(items)}):")
        print("    " + ", ".join(items[:limit]) + (" ..." if len(items) > limit else ""))

    listing("excluded as non-heroes", excluded)
    if resolver.repaired:
        print(f"\n  repaired from categories ({sum(resolver.repaired.values())}):")
        for what, count in resolver.repaired.most_common(8):
            print(f"    {what} x{count}")

    listing("!! pages with no {{Hero}} template", skipped)
    listing("!! no maxed or costume-bonus stats", missing_maxed)
    # Mostly Season 1, whose pages never stated a date. Worth watching: a jump
    # here means the prose changed shape, not that the wiki lost its dates.
    listing("undated (no release sentence on the page)",
            [h["name"] for h in heroes if not h["release_date"]], limit=20)
    if resolver.ambiguous_stats:
        print(f"\n  !! ambiguous stat values ({len(resolver.ambiguous_stats)}), "
              "leading number used:")
        for what, _ in resolver.ambiguous_stats.most_common(8):
            print(f"    {what}")
    if resolver.unknown:
        print(f"\n  !! unknown template codes ({len(resolver.unknown)}), "
              "blank on the wiki too:")
        for code, count in resolver.unknown.most_common(12):
            print(f"    {{{{{code}}}}} x{count}")
    print("=" * 62)


# ==========================================================================
#  Entry point
# ==========================================================================


def load_cache(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_cache(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(here, "heroes.json"))
    ap.add_argument("--cache", default=os.path.join(here, ".cache", "wiki.json"),
                    help="raw API payloads; lets you re-parse without refetching")
    ap.add_argument("--no-network", action="store_true",
                    help="parse the existing cache and exit; no requests are made")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and refetch")
    ap.add_argument("--limit", type=int, help="only parse the first N heroes (smoke test)")
    ap.add_argument("--include-all", action="store_true",
                    help=f"keep pages normally excluded: {sorted(EXCLUDE_CATEGORIES)}")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    print("\nEmpires & Puzzles hero scraper\n")
    client = WikiClient(verbose=args.verbose)
    cache = None if args.refresh else load_cache(args.cache)

    if args.no_network and not cache:
        print(f"ERROR: --no-network needs a cache at {args.cache}", file=sys.stderr)
        return 1

    if cache and (args.no_network or not args.refresh):
        pages = cache["pages"]
        template_maps = cache["templates"]
        image_urls = cache["images"]
        print(f"Using cache: {len(pages)} pages "
              f"(--refresh to refetch, cached {cache.get('fetched_at', '?')})")
    else:
        print("Fetching hero pages...")
        pages = fetch_hero_pages(client, HERO_CATEGORIES)

        print("\nFetching lookup templates...")
        names = discover_lookup_templates(pages.values())
        print(f"   discovered: {', '.join(sorted(names))}")
        template_maps = fetch_template_maps(client, names)
        print(f"   parsed {sum(len(v) for v in template_maps.values())} codes "
              f"across {len(template_maps)} templates")

        image_urls = {}
        cache = None

    # ---- parse ----
    print("\nParsing...")
    resolver = Resolver(template_maps)
    heroes: list[dict] = []
    skipped: list[str] = []
    excluded: list[str] = []

    ordered = sorted(pages.values(), key=lambda p: p["title"])
    if args.limit:
        ordered = ordered[: args.limit]

    for page in ordered:
        cats = set(page.get("categories", []))
        if not args.include_all and cats & EXCLUDE_CATEGORIES:
            excluded.append(page["title"])
            continue
        hero = parse_hero(page, resolver)
        if hero is None:
            if page["title"] not in KNOWN_NON_HERO_PAGES:
                skipped.append(page["title"])
            continue
        heroes.append(hero)

    # ---- images ----
    wanted = {
        f[key] for h in heroes for f in h["forms"]
        for key in ("_portrait_file", "_card_file") if f.get(key)
    }
    missing = wanted - set(image_urls)
    if missing and not args.no_network:
        print(f"\nResolving {len(missing)} image titles...")
        image_urls = {**image_urls, **fetch_image_urls(client, missing)}
    elif missing:
        print(f"\nSkipping {len(missing)} unresolved images (--no-network)")

    unresolved = attach_images(heroes, image_urls)

    # ---- write ----
    if not args.limit:
        save_cache(args.cache, {
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "pages": pages,
            "templates": template_maps,
            "images": image_urls,
        })

    payload = {
        "scraped_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": WIKI,
        "hero_count": len(heroes),
        "heroes": heroes,  # sorted by name, so diffs between runs are meaningful
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)

    report(heroes, resolver, skipped, excluded, unresolved)
    print(f"\nWrote {len(heroes)} heroes to {args.out}")
    print(f"HTTP requests: {client.request_count}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
