#!/usr/bin/env python3
"""
Gate between a fresh scrape and publishing it.

The wiki is not a contract. Templates get renamed, a page gets vandalised, Fandom returns a
partial API response, and the scraper happily writes a valid JSON file that is missing half the
catalogue. Published, that file reaches every install and there is no way to take it back.

So nothing is published without passing here. Two kinds of check:

  * absolute floors  -- things that are true of any sane catalogue, applied even to a first run
  * regression checks -- the fresh scrape against the one currently published, which is what
    actually catches "the wiki changed and we now parse nothing"

Exit code 0 means publishable. Anything else means keep serving the old file.

    python validate.py heroes.json                       # floors only
    python validate.py heroes.json --against live.json   # floors + regressions
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from typing import Any

# A catalogue smaller than this is not a catalogue, it is a failed scrape. The real count has
# been over 1200 since 2024; 1000 leaves room for the wiki deleting a whole family without
# tripping the gate.
MIN_HEROES = 1000

# How far a count may fall from what is currently published before it reads as breakage rather
# than editing. Heroes essentially only get added, so any real drop is suspicious; the softer
# limit on field coverage absorbs a template rename that costs a few dozen heroes one field.
MAX_HERO_COUNT_DROP = 0.02
MAX_COVERAGE_DROP = 0.05

# Fields whose coverage is tracked run-to-run. These are the ones the app actually renders, and
# the ones a parser regression silently empties.
TRACKED_HERO_FIELDS = ("element", "rarity", "class", "family", "epithet", "release_date")
TRACKED_FORM_FIELDS = ("card", "portrait", "special_skill", "stats")


class Failures(list):
    """Collects every problem rather than stopping at the first, so one run fixes all of them."""

    def check(self, ok: bool, message: str) -> None:
        if not ok:
            self.append(message)


def load(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def coverage(catalogue: dict[str, Any]) -> dict[str, int]:
    """Non-null counts per tracked field, over heroes and over forms."""
    counts: dict[str, int] = collections.Counter()
    for hero in catalogue.get("heroes", []):
        for field in TRACKED_HERO_FIELDS:
            if hero.get(field) is not None:
                counts[f"hero.{field}"] += 1
        for form in hero.get("forms", []):
            for field in TRACKED_FORM_FIELDS:
                value = form.get(field)
                # `stats` is a dict of three nullable blocks; present-but-empty is not coverage.
                if field == "stats":
                    value = value and any(value.get(k) for k in ("base", "maxed", "costume_bonus"))
                if value:
                    counts[f"form.{field}"] += 1
    return counts


def check_floors(catalogue: dict[str, Any], failures: Failures) -> None:
    heroes = catalogue.get("heroes")

    failures.check(isinstance(heroes, list), "root has no `heroes` list")
    if not isinstance(heroes, list):
        return  # nothing below can run

    failures.check(
        len(heroes) >= MIN_HEROES,
        f"only {len(heroes)} heroes, floor is {MIN_HEROES}",
    )
    failures.check(
        catalogue.get("hero_count") == len(heroes),
        f"hero_count is {catalogue.get('hero_count')} but there are {len(heroes)} heroes",
    )
    failures.check(bool(catalogue.get("scraped_at")), "no `scraped_at`")

    nameless = [i for i, h in enumerate(heroes) if not h.get("name")]
    failures.check(not nameless, f"{len(nameless)} heroes have no name (first at index {nameless[:1]})")

    duplicates = [n for n, c in collections.Counter(h.get("name") for h in heroes).items() if c > 1]
    failures.check(not duplicates, f"duplicate hero names: {duplicates[:5]}")

    # The app's whole model is "one hero, one to seven forms, base first". A hero with no base
    # form renders as a blank page; two base forms means the parser lost a section heading.
    bad_forms = [
        h.get("name")
        for h in heroes
        if sum(1 for f in h.get("forms", []) if f.get("form") == "base") != 1
    ]
    failures.check(
        not bad_forms,
        f"{len(bad_forms)} heroes do not have exactly one base form: {bad_forms[:5]}",
    )


def check_regressions(fresh: dict[str, Any], live: dict[str, Any], failures: Failures) -> None:
    live_count, fresh_count = len(live.get("heroes", [])), len(fresh.get("heroes", []))
    if live_count == 0:
        return

    allowed = live_count * (1 - MAX_HERO_COUNT_DROP)
    failures.check(
        fresh_count >= allowed,
        f"hero count fell from {live_count} to {fresh_count} "
        f"(more than {MAX_HERO_COUNT_DROP:.0%})",
    )

    live_coverage, fresh_coverage = coverage(live), coverage(fresh)
    for field, live_n in sorted(live_coverage.items()):
        if live_n == 0:
            continue
        fresh_n = fresh_coverage.get(field, 0)
        failures.check(
            fresh_n >= live_n * (1 - MAX_COVERAGE_DROP),
            f"{field} coverage fell from {live_n} to {fresh_n} "
            f"(more than {MAX_COVERAGE_DROP:.0%})",
        )

    # Heroes disappearing is the signature of a partial API response: the scrape succeeds, the
    # file is well-formed, and a chunk of the wiki simply is not in it.
    missing = sorted({h["name"] for h in live.get("heroes", [])} - {h["name"] for h in fresh.get("heroes", [])})
    failures.check(
        len(missing) <= 5,
        f"{len(missing)} heroes present live are missing from the scrape: {missing[:10]}",
    )
    if 0 < len(missing) <= 5:
        print(f"note: {len(missing)} hero(es) dropped from the wiki: {missing}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("catalogue", help="the freshly scraped heroes.json")
    ap.add_argument("--against", help="the currently published heroes.json, for regression checks")
    args = ap.parse_args()

    fresh = load(args.catalogue)
    failures = Failures()

    check_floors(fresh, failures)

    if args.against:
        try:
            check_regressions(fresh, load(args.against), failures)
        except (OSError, json.JSONDecodeError) as exc:
            # No published catalogue yet, or an unreadable one. The floors still applied.
            print(f"note: skipping regression checks -- {exc}", file=sys.stderr)
    else:
        print("note: no --against baseline, floors only", file=sys.stderr)

    if failures:
        print(f"\nREJECTED -- {len(failures)} problem(s):\n", file=sys.stderr)
        for failure in failures:
            print(f"  * {failure}", file=sys.stderr)
        print("\nThe currently published catalogue stays up.\n", file=sys.stderr)
        return 1

    print(f"OK -- {len(fresh['heroes'])} heroes, publishable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
