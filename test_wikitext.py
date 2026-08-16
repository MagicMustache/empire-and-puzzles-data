#!/usr/bin/env python3
"""
Tests for the wikitext parser.

Run after touching `wikitext.py`:

    python test_wikitext.py

Every fixture below is real markup taken from the wiki, including the malformed
cases -- those are the ones that break naive parsing, so they are the ones worth
pinning down.
"""

import sys

from wikitext import (
    clean_effect,
    file_title,
    find_release,
    is_ambiguous_number,
    iter_template_blocks,
    normalize_bullets,
    parse_int,
    parse_switch,
    parse_wiki_date,
    prose_text,
    split_params,
    strip_markup,
    template_args,
)

FAILURES = []


def check(label, actual, expected):
    if actual != expected:
        FAILURES.append(f"{label}\n     expected: {expected!r}\n     actual:   {actual!r}")


# --------------------------------------------------------------------------
#  Brace matching
# --------------------------------------------------------------------------

AZLAR = """{{Hero
|image = Azlar.jpg
|element = {{el|fire}}
|bpower = 397
|health = 1,322
|effect1 = Deals 205% damage to all enemies.
}}

[[File:Azlar_-_Hero_Card.gif|thumb|none|300px]]

== Costume 1 ==
{{Hero
|element = {{el|fire}}
|class = {{cl|pal}}
|cbpower = 861
}}[[File: Azlar Costume 2_-_Hero_Card.gif|thumb]]
"""

blocks = list(iter_template_blocks(AZLAR, r"Hero\s*(?=[|}\n])"))
check("finds every {{Hero}} block", len(blocks), 2)

named, _ = template_args(blocks[0][2])
check("nested {{el|fire}} does not truncate the block", named["element"], "{{el|fire}}")
check("reads a plain parameter", named["bpower"], "397")
check("keeps thousands separator raw", named["health"], "1,322")
check("reads text with = and % in it",
      named["effect1"], "Deals 205% damage to all enemies.")

named2, _ = template_args(blocks[1][2])
check("second block is the costume", named2["cbpower"], "861")

# Multi-line parameter values: E&P effect text spans lines constantly.
MULTILINE = """{{Hero
|effect2 = '''x1 Mana Charge:'''
&nbsp;&nbsp;• Deals 350% damage to the target.<br>
&nbsp;&nbsp;• The target gets -35% defense for 2 turns.
|effect3 = next
}}"""
named3, _ = template_args(list(iter_template_blocks(MULTILINE, r"Hero\b"))[0][2])
check("multi-line value stops at the next top-level pipe",
      named3["effect3"], "next")
check("multi-line value keeps its newlines",
      named3["effect2"].count("\n"), 2)

check("split_params ignores pipes inside [[...]]",
      split_params("|a=[[X|Y]]|b=2"), ["", "a=[[X|Y]]", "b=2"])
check("split_params ignores pipes inside {{...}}",
      split_params("|a={{t|c}}|b=2"), ["", "a={{t|c}}", "b=2"])


# --------------------------------------------------------------------------
#  #switch parsing
# --------------------------------------------------------------------------

CL_TEMPLATE = """{{#switch:{{{1}}}
| barbarian
| bar = [[File:Barbarian icon.png|20px|link=Barbarian]] [[Barbarian]]
| sorcerer
| scr = [[File:Sorcerer icon.png|20px|link=Sorcerer]] [[Sorcerer]]
}}<noinclude>
{| class="fancy"
| bogus || {{Cl|bogus}}
|}
</noinclude>"""

cl = parse_switch(CL_TEMPLATE)
check("switch: short code", strip_markup(cl["bar"]), "Barbarian")
check("switch: alias resolves to the same value", cl["barbarian"], cl["bar"])
check("switch: <noinclude> docs are not treated as cases", "bogus" in cl, False)

# Rarity nests a parser function inside each case.
RARITY = ("<span>{{#ifeq: {{{2}}}|center|margin:auto|left:0}}{{#switch:{{{1}}}\n"
          "| R5 | 5 = [[File:Star_icon.png|15px]]{{#if:{{{br|}}}|<br>|&nbsp;}}"
          "[[:Category:5 Star Heroes|Legendary]]}}</span>")
check("switch: skips a leading #ifeq and reads the real table",
      strip_markup(parse_switch(RARITY)["r5"]), "Legendary")


# --------------------------------------------------------------------------
#  Markup stripping
# --------------------------------------------------------------------------

check("file links are dropped",
      strip_markup("[[File:Ninja_family_icon.png|20px]] Osamu"), "Osamu")
check("category links render their label",
      strip_markup("[[:Category:Bard Family|Bard]]"), "Bard")
check("plain links render their target",
      strip_markup("[[Barbarian]]"), "Barbarian")
check("text-wrapper templates render their last argument",
      strip_markup("[[File:Fire icon.png|15px]]&nbsp;{{Color|#f77|Fire}}"), "Fire")
# Parser functions are dropped wholesale rather than evaluated. In the lookup
# templates their branches only ever hold spacing markup (`<br>` vs `&nbsp;`),
# so nothing meaningful is lost, and whitespace is collapsed downstream anyway.
check("parser functions contribute nothing",
      strip_markup("A{{#if:{{{br|}}}|<br>|&nbsp;}}B"), "AB")
check("bold markers are removed",
      strip_markup("'''x1 Mana Charge:'''"), "x1 Mana Charge:")
check("<br> becomes a line break",
      strip_markup("one<br clear=\"left\"/>two"), "one\ntwo")
check("entities are decoded and nbsp normalised",
      strip_markup("Aether&nbsp;Power"), "Aether Power")
check("empty input is safe", strip_markup(""), "")


# --------------------------------------------------------------------------
#  Numbers -- the old scraper crashed on these
# --------------------------------------------------------------------------

check("thousands separator", parse_int("1,322"), 1322)
check("plain integer", parse_int("397"), 397)
check("'X (Y)' keeps the leading number", parse_int("440 (469)"), 440)
check("tolerates a typo'd bracket", parse_int("931 {977)"), 931)
check("absent value is None, not 0", parse_int(""), None)
check("None input is None", parse_int(None), None)
check("unparseable value is None, not 0", parse_int("???"), None)

check("'X (Y)' is flagged ambiguous", is_ambiguous_number("440 (469)"), True)
check("'???' is flagged ambiguous", is_ambiguous_number("???"), True)
check("a clean number is not flagged", is_ambiguous_number("1,322"), False)
check("empty is not flagged", is_ambiguous_number(""), False)


# --------------------------------------------------------------------------
#  Effects and file names
# --------------------------------------------------------------------------

check("effect bullets are normalised and split",
      clean_effect("'''x1 Mana Charge:'''\n&nbsp;&nbsp;• Deals 350% damage.<br>"
                   "&nbsp;&nbsp;• Target gets -35% defense."),
      "x1 Mana Charge:\n• Deals 350% damage.\n• Target gets -35% defense.")
check("mixed bullet glyphs collapse to one marker",
      normalize_bullets("* one\n● two\nthree"), "• one\n• two\nthree")

check("underscores and stray spaces normalise",
      file_title(" Osamu _-_Hero_Card.gif"), "File:Osamu - Hero Card.gif")
check("an existing File: prefix is not doubled",
      file_title("File:Azlar.jpg"), "File:Azlar.jpg")
check("first letter is capitalised like MediaWiki does",
      file_title("azlar.jpg"), "File:Azlar.jpg")
check("empty file name is None", file_title(""), None)


# --------------------------------------------------------------------------
#  Release dates -- every fixture is a real sentence from the wiki
# --------------------------------------------------------------------------

check("month day, year", parse_wiki_date("July 13, 2026"), ("2026-07-13", "day"))
check("no comma", parse_wiki_date("continued July 13 2026"), ("2026-07-13", "day"))
check("ordinal suffix", parse_wiki_date("February 27th, 2020"), ("2020-02-27", "day"))
check("day first", parse_wiki_date("13 July 2026"), ("2026-07-13", "day"))
check("abbreviated month", parse_wiki_date("Sept 8, 2021"), ("2021-09-08", "day"))
check("US numeric date", parse_wiki_date("on 8/14/2024."), ("2024-08-14", "day"))
check("numeric date with an impossible month is swapped",
      parse_wiki_date("on 14/8/2024."), ("2024-08-14", "day"))
check("month only keeps month precision",
      parse_wiki_date("in February, 2019"), ("2019-02", "month"))
check("an impossible day is rejected", parse_wiki_date("February 30, 2019"), None)
check("a year before the game existed is rejected",
      parse_wiki_date("March 2, 1998"), None)
check("a bare year is not a date", parse_wiki_date("2026"), None)
check("no date at all", parse_wiki_date("was introduced recently"), None)

KASKI = """[[File:Kaski_-_Hero_Card.gif|thumb|none|300px]]

Kaski became available when [[Legends of Kalevala]] continued July 13, 2026.

[[Category:Heroes]]"""
check("the example from the wiki",
      find_release(KASKI, "Kaski"),
      {"date": "2026-07-13", "precision": "day",
       "source": "Kaski became available when Legends of Kalevala continued July 13, 2026."})

check("'was introduced when ... continued on'",
      find_release("Vard was introduced when [[Astral Plane]] contined on January 29, 2025.",
                   "Vard")["date"],
      "2025-01-29")
check("'premiered when ... continued on'",
      find_release("Guardian Hippo premiered when [[Challenge Festival I]] continued "
                   "on January 26, 2023.", "Guardian Hippo")["date"],
      "2023-01-26")
check("'was released' with no event named",
      find_release("Buster was released December 1, 2022.", "Buster")["date"],
      "2022-12-01")
check("a bare 'Released on ...' with no subject",
      find_release("Released on May 28, 2020.", "Sudri")["date"], "2020-05-28")
check("'launched on', dating the season not the hero",
      find_release("One of 16 initial heroes released when [[Season 3]] launched on "
                   "February 27th, 2020.", "Bjorn")["date"],
      "2020-02-27")
check("Hero of the Month gives a month, not a day",
      find_release("Aegir was a featured [[Hero of the Month]] (October 2018) and, as "
                   "such, cannot be produced in a [[Training_Camp]].", "Aegir"),
      {"date": "2018-10", "precision": "month",
       "source": "Aegir was a featured Hero of the Month (October 2018) and, as such, "
                 "cannot be produced in a Training_Camp."})
check("an ordinal full stop does not end the sentence",
      find_release("Leonie was introduced during the 8th. Birthday Summon on "
                   "March 17, 2025.", "Leonie")["date"],
      "2025-03-17")

# The rejections matter more than the matches: every one of these sits in the
# same paragraph as a real release note on some hero's page.
check("a rebalance is not a release",
      find_release("Hero was rebalanced February 8, 2022. All stats now current.", "Grimm"),
      None)
check("a balance update link is not a release",
      find_release("Stats were buffed on [https://smallgiantgames.helpshift.com/faq/"
                   "1083-july-2023-balance-update/ July 2023 Balance Update].", "Yao"),
      None)
check("an artwork refresh is not a release",
      find_release("In Version 70 (Released 9/9/2024) Elena received an artwork update.",
                   "Elena"),
      None)
check("a balance-update table is not prose",
      find_release("""{| class="wikitable"
| rowspan="2" | [[Updates#Update 52|Version 52]]<br/><small>(October 2022)</small>
|}""", "Zocc"),
      None)
check("undated prose yields nothing",
      find_release("Elena is one of the original 20 R5 Heroes in the Season 1.", "Elena"),
      None)

# When a page states two dated releases, the hero's own wins over the aside
# about other heroes -- even though the aside comes first.
GARTEN = ("Skills were adjusted to conform to the new Gargoyle heroes introduced on "
          "May 16, 2024 as [[Sanctuary of Gargoyles]] returned.\n"
          "Garten was introduced with 7th Anniversary of the game available in the "
          "Birthday Summon on February 27, 2024.")
check("the sentence about this hero wins", find_release(GARTEN, "Garten")["date"],
      "2024-02-27")

check("external links are reduced to their label",
      prose_text("Stats were buffed on [https://example.com/july-2023-balance-update/ "
                 "July 2023 Balance Update]."),
      "Stats were buffed on July 2023 Balance Update.")


# --------------------------------------------------------------------------

if FAILURES:
    print(f"FAILED ({len(FAILURES)}):\n")
    for f in FAILURES:
        print(f"  - {f}\n")
    sys.exit(1)
print("all wikitext parser tests passed")
