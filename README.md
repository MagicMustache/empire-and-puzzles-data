# Empires & Puzzles hero data

The data pipeline behind the *Empires & Puzzles Guide* Android app. A GitHub Actions cron scrapes
the [Empires & Puzzles Fandom wiki][wiki] once a week, checks the result against the one currently
published, and serves it as two static files on GitHub Pages.

There is no server. The cron is Actions, the host is Pages, and both are free for a public repo.

## Endpoints

| File | Size | Purpose |
|---|---|---|
| [`manifest.json`](https://magicmustache.github.io/empire-and-puzzles-data/manifest.json) | ~365 B | Version, hero count, checksum. **Poll this.** |
| [`heroes.json.gz`](https://magicmustache.github.io/empire-and-puzzles-data/heroes.json.gz) | ~630 KB | The catalogue, gzipped from 4.9 MB. |

```jsonc
{
  "version": "008edee4630b43ca…",   // sha256 of the *uncompressed* catalogue
  "hero_count": 1284,
  "scraped_at": "2026-08-16T10:12:25+00:00",
  "url": "https://…/heroes.json.gz",
  "bytes": 4915282,
  "bytes_gz": 628856,
  "sha256": "008edee4630b43ca…"
}
```

A client keeps the `version` it last installed, fetches the manifest, and downloads the catalogue
only when the two differ. `version` is the sha256 of the decompressed bytes, so a client can
verify what it actually decoded rather than trusting that the transfer and the gunzip both went
fine. `heroes.json.gz` is a literal gzip file, not `Content-Encoding: gzip` — clients decompress
it themselves and do not depend on how the host negotiates compression.

See [`SCRAPER.md`](SCRAPER.md) for the catalogue's own schema.

## The pipeline

```
cron ──▶ test_wikitext.py ──▶ scrape_heroes.py ──▶ validate.py ──▶ publish.py ──▶ Pages
         parser tests         ~130 API requests   the gate        gz + manifest
                              under a minute
```

`validate.py` is the part that matters. The wiki is not a contract: templates get renamed, pages
get vandalised, and Fandom occasionally returns a partial API response. Each of those produces a
*valid* JSON file that is missing half the catalogue, and once that reaches Pages every install
picks it up. So the fresh scrape is checked against the live one before anything is deployed:

- **absolute floors** — at least 1000 heroes, `hero_count` agreeing with the array, every hero
  named, no duplicates, exactly one `base` form each.
- **regression checks** — hero count may not fall more than 2%; per-field coverage (element,
  rarity, class, family, epithet, release date, cards, portraits, skills, stats) may not fall
  more than 5%; no more than five heroes may vanish outright.

If the gate fails, the workflow fails and **the previously published catalogue stays up**. The
rejected scrape is still uploaded as a build artifact so you can see what the wiki did.

Nothing is committed by the workflow — the catalogue is a build output, and committing 4.9 MB
weekly would add a quarter of a gigabyte a year to a repo whose source is three Python files.

## Running it locally

```bash
pip install -r requirements.txt
python test_wikitext.py                                    # parser tests, no network
python scrape_heroes.py                                    # ~130 requests, under a minute
python validate.py heroes.json --against live/heroes.json  # the gate
python publish.py heroes.json --base-url https://magicmustache.github.io/empire-and-puzzles-data
```

`scrape_heroes.py --no-network` re-parses `.cache/wiki.json`, which is how you iterate on
`wikitext.py` without hitting the wiki. See [`SCRAPER.md`](SCRAPER.md) for the rest of the flags.

## Setup

Pages must be set to deploy from Actions rather than a branch:
**Settings → Pages → Build and deployment → Source: GitHub Actions**. Nothing else is needed —
no secrets, no tokens, no accounts.

## Licence

Hero data is derived from the [Empires & Puzzles Fandom wiki][wiki] and is licensed
[CC BY-SA 3.0][cc], as is this derivative of it. Any app or site consuming these files needs to
carry that attribution too.

The scraper code itself is MIT. *Empires & Puzzles* is a trademark of Small Giant Games; this
project is unaffiliated with them and with Fandom.

[wiki]: https://empiresandpuzzles.fandom.com
[cc]: https://creativecommons.org/licenses/by-sa/3.0/
