# Empires & Puzzles hero scraper

Pulls every hero from [empiresandpuzzles.fandom.com](https://empiresandpuzzles.fandom.com)
into a structured `heroes.json`, including costumes.

```
py/
  scrape_heroes.py    entry point: fetch, parse, report, write
  wikitext.py         pure wikitext parsing helpers (no network, no I/O)
  test_wikitext.py    parser tests -- run these after touching wikitext.py
  heroes.json         output
  .cache/wiki.json    raw API payloads, so you can re-parse without refetching
```

## Quick start

```bash
pip install requests
python scrape_heroes.py
```

A full run is ~130 HTTP requests and finishes in well under a minute.

| Flag | Effect |
|---|---|
| `--limit N` | parse only the first N heroes (smoke test; skips the cache write) |
| `--no-network` | parse the existing cache and exit — **use this while iterating on the parser** |
| `--refresh` | ignore the cache and refetch everything |
| `--include-all` | keep pages normally excluded (the Mimics) |
| `--out PATH` | write somewhere other than `heroes.json` |
| `-v` | log retries and backoff to stderr |

## Output schema

```jsonc
{
  "scraped_at": "2026-08-15T...",
  "source": "https://empiresandpuzzles.fandom.com",
  "hero_count": 1284,
  "heroes": [                       // sorted by name, so run-to-run diffs are meaningful
    {
      "name": "Azlar",
      "page_id": 3061,
      "url": "https://empiresandpuzzles.fandom.com/wiki/Azlar",
      "stars": 5,
      "categories": ["5 Star Heroes", "Barbarian", "Classic Family", ...],

      // convenience mirror of forms[0], for consumers that ignore costumes
      "element": "Fire", "rarity": "Legendary", "class": "Barbarian",
      "family": "Classic", "epithet": "Last of the Leors",
      "release_date": "2017-03-02",       // null when the wiki never says
      "release_date_precision": "day",    // "day" | "month" | null

      "forms": [
        {
          "form": "base",           // "base", then a slug of the section heading
          "section": null,          // the wiki heading, e.g. "Costume 2"
          "title": "Azlar",
          "epithet": "Last of the Leors",
          "release_date": "2017-03-02",
          "release_date_precision": "day",
          "release_note": "Part of the initial release of the game on March 2, 2017.",
          "element": "Fire",
          "rarity": "Legendary",
          "class": "Barbarian",     // costumes often change class
          "family": "Classic",
          "realm": null,
          "aether_power": "Rage",
          "passives": [{"name": "Toon", "description": "..."}],
          "resists": [],
          "traits":  [],
          "stats": {
            "base":          {"power": 397, "attack": 385, "defense": 295, "hp": 642},
            "maxed":         {"power": 757, "attack": 793, "defense": 607, "hp": 1322},
            "costume_bonus": null
          },
          "special_skill": {
            "name": "Volcanic Eruption",
            "speed": "Slow",
            "effects": ["Deals 205% damage to all enemies.", "..."]
          },
          "portrait": "https://static.wikia.nocookie.net/...",
          "card":     "https://static.wikia.nocookie.net/..."
        }
        // ... one entry per costume
      ]
    }
  ]
}
```

### Reading the schema

- **`null` means "the wiki does not say"**, never zero. Any numeric field can be
  `null`, including individual stats within a block — Sartana's `base.power` is
  genuinely absent from the wiki, so it is `null` while her other base stats
  have values.
- **The three stat blocks are not interchangeable.** `base` is level 1, `maxed`
  is fully ascended and levelled, `costume_bonus` is the maxed stat line for a
  costumed version. Most costume forms carry only `costume_bonus`. Never compare
  a `base` value against a `maxed` one.
- **`forms[0]` is always the base hero**; the rest are costumes in page order.
- `special_skill.effects` entries may contain newlines, where the wiki formats
  an effect as a bulleted list (charge-based ninja skills, for example).
- **`release_date` is per form.** A costume released in 2025 for a hero released
  in 2017 carries its own date; the hero-level field mirrors `forms[0]` only, so
  a hero whose page dated nothing but its costumes stays `null` rather than
  borrowing one. See [Release dates](#release-dates).

## How it works

Three phases, in `main()`:

1. **Fetch** — `generator=categorymembers` walks the hero categories and returns
   the full wikitext of 50 pages per request. Then the lookup templates, then
   image URLs (50 per request).
2. **Parse** — pure functions over the cached wikitext. No network.
3. **Report** — a data-quality summary printed to stdout.

### Why the API rather than scraping HTML

- **`/wiki/*` is behind Cloudflare.** A sustained crawl gets `Just a moment...`
  interstitials and then `403`s. `api.php` is not protected — during development,
  the same client got `403` from `/wiki/Category:5_Star_Heroes` and `200` from
  `api.php` in the same second.
- **~130 requests instead of ~1200**, because pages come 50 at a time.
- **The wikitext is already structured.** The rendered page shows up to eight
  visually identical stat tables (base, maxed, and one per costume) that can only
  be told apart by position or caption. The source has them as distinct named
  parameters:

  ```wikitext
  {{Hero
  |bpower = 397   |battack = 385   |bdefense = 295   |bhealth = 642     <- base
  |power  = 757   |attack  = 793   |defense  = 607   |health  = 1,322   <- maxed
  |cbpower = 861  |cbattack = 923  ...                                  <- costume bonus
  ```

  Confusing base with maxed stats is therefore structurally impossible here.

### Template code resolution

Infobox values are template calls: `element = {{el|fire}}`, `class = {{cl|bar}}`.
Those templates are `{{#switch:}}` dispatch tables, so the scraper fetches them
and parses the switch into a `code -> label` map at runtime
(`wikitext.parse_switch`).

Nothing is hardcoded. The set of templates to fetch is *discovered* from the hero
pages themselves (`discover_lookup_templates`), so a new family, class or aether
power added to the wiki is picked up with no code change.

### Release dates

There is no release-date parameter anywhere in `{{Hero}}`. The date exists only
as a sentence of prose sitting under the hero card, and no two editors write it
the same way:

```
Kaski became available when [[Legends of Kalevala]] continued July 13, 2026.
Guardian Hippo premiered when [[Challenge Festival I]] continued on January 26, 2023.
Aegir's Costume was released January 22, 2023 as part of the Tavern of Legends portal.
One of 16 initial heroes released when [[Season 3]] launched on February 27th, 2020.
Alvar was introduced when the Clash of Knights event continued on 8/14/2024.
Released in February, 2019
```

So `wikitext.find_release` does not match sentence templates. It looks for the
*conjunction* of three things within one sentence:

1. **a date** — `Month D, YYYY`, `D Month YYYY`, `M/D/YYYY` or a bare
   `Month YYYY`, with or without commas and ordinal suffixes;
2. **a release cue** — `became available`, `introduced`, `released`, `premiered`,
   `debuted`, `launched`, `added`, `arrived`, `Hero of the Month`;
3. **no rebalance cue** — `balance update`, `rebalanced`, `buffed`, `nerfed`,
   `adjusted`, `artwork`, `Version N`.

The third condition is the one that earns its keep. Balance-update notes sit in
the same paragraph as the release note and are *also* dated
(`Hero was rebalanced February 8, 2022`), so without it a 2017 hero would be
silently dated to 2022. Wikitables are dropped before parsing for the same
reason: the balance-history tables carry a date in every row.

Two more details:

- **Dates are read per form**, from the wikitext between one `{{Hero}}` template
  and the next — the same slice the hero card is read from. This is what keeps a
  costume's date off the base hero.
- **When a form has several qualifying sentences, the one naming the hero wins.**
  A few pages open with an aside about *other* heroes ("Skills were adjusted to
  conform to the new Gargoyle heroes introduced on May 16, 2024") before stating
  their own date. Ties then go to the more precise date, and then to page order.

`release_date_precision` distinguishes `"day"` (`"2026-07-13"`) from `"month"`
(`"2018-10"`, all the wiki gives for Heroes of the Month and for a handful of
2017–2019 pages). The month-precision value is deliberately a short string
rather than a made-up first-of-the-month.

**Coverage: 1110 of 1284 heroes and 573 of 628 costumes.** The 174 undated
heroes are not a parser failure — the wiki does not date them. Most are Season 1,
whose pages never stated a date; the rest are pages written ahead of release,
where the sentence literally stops mid-way (`Clim was introduced when`). Since
those are the *newest* heroes, do not assume "no date" means "old".

## Iterating on the parser

The cache makes this fast. Fetch once, then loop on parsing alone:

```bash
python scrape_heroes.py                 # populates .cache/wiki.json
python scrape_heroes.py --no-network    # re-parse, ~2 seconds, zero requests
python test_wikitext.py                 # parser unit tests
```

`.cache/wiki.json` holds the raw API payloads (`pages`, `templates`, `images`),
so you can also poke at them directly:

```python
import json
cache = json.load(open('.cache/wiki.json', encoding='utf-8'))
print(cache['pages']['Azlar']['wikitext'])
```

### Adding a field

1. Find the parameter name in the wikitext (`cache['pages']['Azlar']['wikitext']`).
2. If it is a plain value, add it in `parse_hero`'s form dict via
   `strip_markup(named.get("yourfield", ""))`.
3. If it is a `{{template|code}}` lookup, add the parameter name to
   `LOOKUP_FIELDS` (single value) or `DESCRIPTIVE_FIELDS` (name + description
   text, possibly several per hero) and resolve it with `resolver.label(...)` or
   `resolver.values(...)`. The backing template is then fetched automatically.
4. `python scrape_heroes.py --no-network` to check the result.

The full parameter list currently in use, for reference: `title1`, `caption1`,
`image`, `element`, `rarity`, `class`, `family`, `realm`, `aether`, `passive`,
`resist`, `trait`, `bpower`/`battack`/`bdefense`/`bhealth`,
`power`/`attack`/`defense`/`health`, `cbpower`/`cbattack`/`cbdefense`/`cbhealth`,
`special_name`, `mana_speed`, `effect1`..`effect5`.

### Adding a source category

Append to `HERO_CATEGORIES`. Pages without a `{{Hero}}` template are dropped
automatically and reported; if the new category brings in known meta pages, add
them to `KNOWN_NON_HERO_PAGES` so the warning stays meaningful.

## Reading the run report

The report exists so that **a degraded run looks different from a good one**.
Lines prefixed `!!` want attention. A healthy run currently looks like:

```
  heroes            1284
  forms             1912 (628 costumes)
  with maxed stats  1470
  forms w/o card    35
  with release date 1110 (161 to the month only)
  dated costumes    573
  release span      2017-03 .. 2026-10
  repaired from categories (4)
  undated (no release sentence on the page) (174)
  !! ambiguous stat values (27)
  !! unknown template codes (13)
```

The known-benign warnings, all of which are wiki-side data problems rather than
scraper bugs:

| Warning | Cause |
|---|---|
| `forms w/o card` (35) | The `[[File:...]]` link on those pages points at a file that does not exist. Verified against the API; some pages are in `Category:Pages with broken file links`. The field is `null` rather than a guess. |
| `unknown template codes` (13) | Typos in the wiki source — `{{cl|src}}` for `scr`, `{{el|nadarkture}}` for `dark`. The wiki's own `#switch` has no `#default`, so **these render blank on the site too**; there is no data being lost. |
| `ambiguous stat values` (27) | Four pages write stats as `"440 (469)"` or `"???"`. The parenthetical is inconsistent — sometimes larger than the main value, sometimes smaller — so only the leading number is used. |
| `repaired from categories` (4) | Class/element that a typo left blank, recovered from the page's own category membership (a Sorcerer is in `Category:Sorcerer`). Applied only when exactly one candidate remains, so ambiguous cases stay `null` and get reported instead. |
| `undated` (174) | The page states no release date. Mostly Season 1; also every page written ahead of release, where the sentence stops at `"X was introduced when"`. A sharp rise here means the prose changed shape — check `find_release` against a few of the named heroes. |

### Known limitations

- Discovery is category-driven, so a hero page with no categories at all is
  invisible. Three pages are currently in that state on the wiki (`Hero`,
  `Sizzle 3000`, `Zavok`) — they were categorised as recently as late 2025.
- `Mimic Family` pages are excluded by default (`EXCLUDE_CATEGORIES`); pass
  `--include-all` to keep them. Nothing is dropped silently — exclusions are
  listed in the report, and every hero carries its full `categories` list so you
  can filter downstream instead.

## What changed from the previous scraper

The old `test.py` parsed rendered HTML with BeautifulSoup. Measured against its
output (`heroes - Copie.json`, 1156 heroes):

| | before | after |
|---|---|---|
| heroes | 1156 (incl. 20 non-hero Mimics) | 1284 |
| forms | 1156 — costumes discarded | 1912 |
| stat semantics | **mixed**: 871 rows held level-1 stats, 159 held maxed, 29 were `0` | base / maxed / costume_bonus kept separate, `null` when absent |
| card images | 85 wrong-but-plausible URLs (element placeholders, `R4_Missing_Yellow.jpg`, portraits) | `null` unless a real card resolves |
| fields per form | 8 | 20 |
| requests / runtime | ~1200 / ~20 min | ~130 / <1 min |
| failure mode | Cloudflare challenge → hero silently dropped, success message unchanged | retries with backoff; anything unresolved is counted in the report |

The single most consequential fix is the stat semantics. The old scraper took the
first `table.pi-horizontal-group` on the page, which is *Base Stats* for heroes
that have such a section and *Maxed Stats* for those that do not — so Azlar was
recorded at 397 power (level 1) and John Cena at 2240 (fully maxed), in the same
column. Any sort or comparison across that dataset was meaningless.
