#!/usr/bin/env python3
"""
Turns a validated `heroes.json` into the two files the app actually fetches.

    dist/manifest.json   a few hundred bytes -- what the app polls
    dist/heroes.json.gz  ~630 KB -- what it downloads when the manifest says the version moved
    dist/index.html      so the Pages root is not a 404

The split is the whole point of the design. A phone checking for updates should not pull 4.8 MB
to discover there is nothing new, and it should not have to trust an HTTP cache to tell it so.
The manifest is small enough to fetch on any connection, and the version in it is the sha256 of
the *uncompressed* catalogue, so the app can verify what it decompressed rather than trusting
that the transfer and the gunzip both went fine.

    python publish.py heroes.json --base-url https://user.github.io/repo
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys

CATALOGUE_NAME = "heroes.json.gz"
MANIFEST_NAME = "manifest.json"

INDEX_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Empires &amp; Puzzles hero data</title>
<style>
  body {{ font: 16px/1.6 system-ui, sans-serif; max-width: 42rem; margin: 4rem auto; padding: 0 1rem; }}
  code {{ background: #f4f4f5; padding: .15em .4em; border-radius: .25em; }}
  dt {{ font-weight: 600; margin-top: 1rem; }}
</style>
<h1>Empires &amp; Puzzles hero data</h1>
<p>
  {hero_count} heroes, scraped from the
  <a href="https://empiresandpuzzles.fandom.com">Empires &amp; Puzzles Fandom wiki</a>
  and refreshed weekly. Last scrape: <code>{scraped_at}</code>.
</p>
<dl>
  <dt><a href="{manifest}">manifest.json</a></dt>
  <dd>Version, hero count and checksum. Poll this.</dd>
  <dt><a href="{catalogue}">heroes.json.gz</a></dt>
  <dd>The catalogue, gzipped ({bytes_gz_kb} KB, {bytes_kb} KB uncompressed).</dd>
</dl>
<p>
  Wiki content is licensed
  <a href="https://creativecommons.org/licenses/by-sa/3.0/">CC BY-SA 3.0</a>
  and this derivative is redistributed under the same terms.
</p>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("catalogue", help="the validated heroes.json")
    ap.add_argument("--base-url", required=True, help="where the files will be served from")
    ap.add_argument("--out", default="dist", help="output directory")
    ap.add_argument("--against", help="the currently published manifest.json, to detect no-ops")
    args = ap.parse_args()

    raw = open(args.catalogue, "rb").read()
    catalogue = json.loads(raw)
    version = hashlib.sha256(raw).hexdigest()
    base = args.base_url.rstrip("/")

    os.makedirs(args.out, exist_ok=True)

    # mtime=0 so an unchanged catalogue produces a byte-identical archive; otherwise every run
    # looks like a change to anything comparing the served bytes.
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    with open(os.path.join(args.out, CATALOGUE_NAME), "wb") as fh:
        fh.write(compressed)

    manifest = {
        "version": version,
        "hero_count": catalogue.get("hero_count", len(catalogue.get("heroes", []))),
        "scraped_at": catalogue.get("scraped_at"),
        "url": f"{base}/{CATALOGUE_NAME}",
        "bytes": len(raw),
        "bytes_gz": len(compressed),
        "sha256": version,
    }
    with open(os.path.join(args.out, MANIFEST_NAME), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    with open(os.path.join(args.out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(
            INDEX_HTML.format(
                hero_count=manifest["hero_count"],
                scraped_at=manifest["scraped_at"],
                manifest=MANIFEST_NAME,
                catalogue=CATALOGUE_NAME,
                bytes_kb=len(raw) // 1024,
                bytes_gz_kb=len(compressed) // 1024,
            )
        )

    changed = True
    if args.against:
        try:
            with open(args.against, encoding="utf-8") as fh:
                changed = json.load(fh).get("version") != version
        except (OSError, json.JSONDecodeError):
            changed = True  # nothing published yet

    ratio = len(compressed) / len(raw)
    print(f"version    {version[:16]}...")
    print(f"heroes     {manifest['hero_count']}")
    print(f"size       {len(raw):,} -> {len(compressed):,} bytes ({ratio:.1%})")
    print(f"changed    {changed}")

    # Consumed by the workflow to skip a no-op deploy.
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"changed={str(changed).lower()}\n")
            fh.write(f"version={version}\n")
            fh.write(f"hero_count={manifest['hero_count']}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
