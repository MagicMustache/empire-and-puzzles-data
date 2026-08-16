"""
Minimal MediaWiki wikitext parsing helpers.

The Empires & Puzzles wiki stores every hero as a `{{Hero|...}}` template
invocation, so the wikitext is already structured data. This module provides
just enough of a parser to read it reliably:

  * `iter_template_blocks` -- brace-matched extraction of `{{Name|...}}` blocks
  * `split_params`         -- split a template body on *top level* pipes
  * `parse_switch`         -- turn a `{{#switch:}}` template into a code -> value map
  * `strip_markup`         -- render wikitext down to plain text
  * `find_release`         -- read a release date out of the prose around a form

Everything here is pure (no network, no I/O), so it can be tested and iterated
on against a local cache. See README.md.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import unicodedata

# --------------------------------------------------------------------------
#  Brace-matched extraction
# --------------------------------------------------------------------------
#
# Regexes cannot match nested braces, and hero templates nest constantly
# (`{{Hero|element={{el|fire}}}}`). Every extractor below therefore walks the
# string and tracks nesting depth by hand.


def _scan_to_close(text: str, start: int) -> int:
    """Return the index just past the `}}` matching the `{{` at *start*.

    Note that `{{{param}}}` is consumed as `{{` + `{param` + `}}` + `}`, which
    nets out to a depth change of zero -- exactly what we want, since template
    parameters are not template calls.
    """
    depth = 0
    i = start
    while i < len(text):
        pair = text[i : i + 2]
        if pair == "{{":
            depth += 1
            i += 2
        elif pair == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return len(text)  # unbalanced; treat the remainder as the body


def iter_template_blocks(text: str, name_pattern: str):
    """Yield `(start, end, body)` for each template matching *name_pattern*.

    *start* is the index of the opening `{{`, *end* the index just past the
    closing `}}`, and *body* everything in between excluding the name itself
    (so it begins with the leading `|` of the first parameter, if any).

    *name_pattern* is a raw regex fragment, e.g. ``r"Hero"`` or ``r"#switch:"``.
    Matching is case-insensitive, as MediaWiki template names are.
    """
    opener = re.compile(r"\{\{\s*(?:" + name_pattern + r")", re.IGNORECASE)
    pos = 0
    while pos < len(text):
        m = opener.search(text, pos)
        if not m:
            return
        end = _scan_to_close(text, m.start())
        yield m.start(), end, text[m.end() : max(m.end(), end - 2)]
        pos = end if end > m.start() else m.start() + 2


def split_params(body: str) -> list[str]:
    """Split a template body on pipes that are not inside `{{}}` or `[[]]`.

    Parameter values routinely span multiple lines (E&P effect text does this
    constantly), so newlines are treated as ordinary content.
    """
    parts: list[str] = []
    buf: list[str] = []
    curly = square = 0
    i = 0
    while i < len(body):
        pair = body[i : i + 2]
        if pair == "{{":
            curly += 1
        elif pair == "}}":
            curly = max(0, curly - 1)
        elif pair == "[[":
            square += 1
        elif pair == "]]":
            square = max(0, square - 1)
        else:
            ch = body[i]
            if ch == "|" and curly == 0 and square == 0:
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(ch)
            i += 1
            continue
        buf.append(pair)
        i += 2
    parts.append("".join(buf))
    return parts


def template_args(body: str) -> tuple[dict[str, str], list[str]]:
    """Split a template body into `(named, positional)` arguments."""
    named: dict[str, str] = {}
    positional: list[str] = []
    for part in split_params(body)[1:]:  # [0] is the text before the first pipe
        key, sep, value = part.partition("=")
        if sep and re.fullmatch(r"[\w\s\-]+", key):
            named[key.strip().lower()] = value.strip()
        else:
            positional.append(part.strip())
    return named, positional


# --------------------------------------------------------------------------
#  #switch parsing
# --------------------------------------------------------------------------


def parse_switch(source: str) -> dict[str, str]:
    """Extract the code -> raw-value map from a template built on `{{#switch:}}`.

    Handles alias chains, where several codes share one value::

        | barbarian
        | bar = [[File:Barbarian icon.png|20px]] [[Barbarian]]

    `<noinclude>` blocks are dropped first -- they hold the documentation
    tables, whose example invocations would otherwise be mistaken for cases.
    """
    source = re.sub(r"<noinclude>.*?</noinclude>", "", source, flags=re.S | re.I)
    source = re.sub(r"</?includeonly>", "", source, flags=re.I)

    mapping: dict[str, str] = {}
    for _start, _end, body in iter_template_blocks(source, r"#switch:"):
        pending: list[str] = []
        for part in split_params(body)[1:]:
            key, sep, value = part.partition("=")
            code = key.strip().lower()
            if not sep:
                if code:
                    pending.append(code)
                continue
            for alias in pending + [code]:
                if alias and alias != "#default":
                    mapping.setdefault(alias, value.strip())
            pending = []
        break  # only the first #switch in a template is the dispatch table
    return mapping


# --------------------------------------------------------------------------
#  Markup -> plain text
# --------------------------------------------------------------------------

_FILE_LINK = re.compile(r"\[\[\s*(?:File|Image)\s*:[^\[\]]*\]\]", re.IGNORECASE)
_INNER_LINK = re.compile(r"\[\[([^\[\]]*)\]\]")
_INNER_TEMPLATE = re.compile(r"\{\{([^{}]*)\}\}")
_TEMPLATE_PARAM = re.compile(r"\{\{\{[^{}]*\}\}\}")
_BREAK = re.compile(r"<\s*br[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def _reduce_template(inner: str) -> str:
    """Collapse one innermost `{{...}}` to the text it contributes.

    Parser functions (`{{#if:}}`, `{{#switch:}}`) contribute nothing. Ordinary
    templates are assumed to render their last positional argument, which is
    the convention this wiki follows for text wrappers such as
    `{{Color|#7f7|Nature}}`.
    """
    inner = inner.strip()
    if inner.startswith("#"):
        return ""
    parts = split_params(inner)
    if len(parts) <= 1:
        return ""
    for candidate in reversed(parts[1:]):
        candidate = candidate.strip()
        if "=" in candidate.split("|")[0]:
            continue  # named argument, not display text
        if candidate:
            return candidate
    return ""


def _reduce_link(inner: str) -> str:
    """Collapse one innermost `[[...]]` to its display text."""
    target, _, label = inner.partition("|")
    text = label if label else target
    return text.lstrip(":").strip()


def strip_markup(text: str) -> str:
    """Render a wikitext fragment down to readable plain text."""
    if not text:
        return ""

    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = _TEMPLATE_PARAM.sub("", text)

    # Innermost-first, so nesting unwinds correctly. The bound is a guard
    # against pathological input rather than an expected limit.
    for _ in range(24):
        new = _INNER_TEMPLATE.sub(lambda m: _reduce_template(m.group(1)), text)
        if new == text:
            break
        text = new

    text = _FILE_LINK.sub("", text)
    for _ in range(8):
        new = _INNER_LINK.sub(lambda m: _reduce_link(m.group(1)), text)
        if new == text:
            break
        text = new

    text = _BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    text = re.sub(r"'{2,}", "", text)  # '' italic / ''' bold
    text = html.unescape(text)
    text = unicodedata.normalize("NFC", text)
    text = text.replace(" ", " ").replace("​", "")

    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


# --------------------------------------------------------------------------
#  Small shared helpers
# --------------------------------------------------------------------------

# The wiki uses U+2022, U+25CF, U+25CB, U+25E6, '-' and '*' interchangeably as
# bullet markers, sometimes within a single hero.
_BULLET = re.compile("^[•●○◦*-]\\s*")
BULLET = "•"


def normalize_bullets(text: str) -> str:
    """Rewrite assorted bullet glyphs to one consistent marker per line."""
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        body = _BULLET.sub("", line).strip()
        out.append(BULLET + " " + body if body != line else body)
    return "\n".join(out)


def clean_effect(text: str) -> str:
    """Normalise one `effectN` value.

    Sub-bullets arrive as indented, bullet-prefixed runs separated by `<br>`;
    keep them as separate lines so the structure survives into JSON.
    """
    return normalize_bullets(strip_markup(text))


# A leading integer, allowing thousands separators written as commas or spaces.
_LEADING_NUMBER = re.compile("-?\\d[\\d,\\s ]*")


def parse_int(value: str | None) -> int | None:
    """Parse a stat value, tolerating thousands separators.

    A few pages annotate stats as `"440 (469)"`. The parenthetical means
    different things on different pages -- sometimes a limit-broken value,
    sometimes a smaller number entirely -- so only the leading number is
    trusted. `is_ambiguous_number` flags those so they can be reported.

    Returns `None` rather than `0` when the value is absent or unparseable:
    a missing stat and a stat of zero are different facts, and conflating them
    is what made the previous dataset's zeros indistinguishable from real data.
    """
    if value is None:
        return None
    match = _LEADING_NUMBER.search(strip_markup(value))
    if not match:
        return None
    try:
        return int(re.sub("[,\\s ]", "", match.group(0)))
    except ValueError:
        return None


def is_ambiguous_number(value: str | None) -> bool:
    """True if a stat value carries content beyond the number we parsed."""
    if not value:
        return False
    text = strip_markup(value)
    match = _LEADING_NUMBER.search(text)
    if not match:
        return bool(text)
    return bool((text[: match.start()] + text[match.end() :]).strip())


def file_title(name: str) -> str | None:
    """Normalise a bare file name into a canonical `File:` title."""
    name = strip_markup(name).strip().strip("|").strip()
    if not name:
        return None
    name = re.sub(r"^(?:File|Image)\s*:\s*", "", name, flags=re.IGNORECASE)
    name = name.replace("_", " ").strip()
    name = re.sub(r"\s+", " ", name)
    if not name:
        return None
    return "File:" + name[0].upper() + name[1:]


# --------------------------------------------------------------------------
#  Release dates
# --------------------------------------------------------------------------
#
# There is no release-date field anywhere in the `{{Hero}}` template. The date
# only ever exists as a sentence of prose sitting under the hero card:
#
#     Kaski became available when [[Legends of Kalevala]] continued July 13, 2026.
#     Aegir's Costume was released January 22, 2023 as part of the Tavern of Legends.
#     Sapphire was introduced with the [[Ninja Tower]] event on October 2020.
#
# No two editors write it the same way, so nothing here pattern-matches a whole
# sentence. Instead `find_release` looks for the *conjunction* of three things
# in one sentence -- a date, a word meaning "became purchasable", and no word
# meaning "was rebalanced" -- which is what actually distinguishes a release
# note from the balance-update notes that sit in the same paragraph.

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
_MONTH_NUMBERS = {**_MONTH_NAMES, **{n[:3]: v for n, v in _MONTH_NAMES.items()}, "sept": 9}

_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?"
          r"|Jul(?:y)?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?"
          r"|Dec(?:ember)?)")

# Ordered: the day-precision forms have to be tried before the bare month-year
# one, or "July 13, 2026" would be read as "July 2026" with a stray 13.
_DATE = re.compile(
    r"(?P<mdy>(?P<m1>" + _MONTH + r")\s*\.?\s+(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(?P<y1>\d{4}))"
    r"|(?P<dmy>(?P<d2>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m2>" + _MONTH + r")\s*,?\s*(?P<y2>\d{4}))"
    r"|(?P<num>(?P<m4>\d{1,2})/(?P<d4>\d{1,2})/(?P<y4>\d{4}))"
    r"|(?P<my>(?P<m3>" + _MONTH + r")\s*,?\s*(?P<y3>\d{4}))",
    re.IGNORECASE,
)

# The game launched in March 2017; anything outside this window is a
# misparse rather than a release.
_EARLIEST_YEAR = 2017
_LATEST_YEAR = 2100

# Words that mean "this is when you could first get the hero". Deliberately
# broad -- precision comes from `_NOT_A_RELEASE` below, not from this list.
_RELEASE_CUE = re.compile(
    r"became\s+available|made\s+available|(?:was|were|is|are|first)\s+available"
    r"|introduc\w*|releas\w*|premier\w*|debut\w*|launch\w*"
    r"|(?:was|were)\s+added|arriv\w*"
    # Heroes of the Month are only ever dated by their featured month:
    # "Aegir was a featured Hero of the Month (October 2018)".
    r"|hero\s+of\s+the\s+month",
    re.IGNORECASE,
)

# Dated sentences that are *about* a hero without being its release. Balance
# updates are the common case and they sit in the same paragraph as the release
# note, so leaving them in would silently date a 2017 hero to 2024.
_NOT_A_RELEASE = re.compile(
    r"balance\s+update|rebalanc\w*|buff\w*|nerf\w*|re-?work\w*|adjust\w*|conform\w*"
    r"|stats?\s+(?:now|were|was|are|has|have)|artwork|new\s+card|version\s*\d",
    re.IGNORECASE,
)

_WIKITABLE = re.compile(r"\{\|.*?\|\}", re.S)
_EXTERNAL_LINK = re.compile(r"\[(?:https?:)?//[^\s\]]+(?:\s+([^\]]*))?\]")
# "the 8th. Birthday Summon" -- an ordinal followed by a full stop, which would
# otherwise look like the end of a sentence.
_ORDINAL_STOP = re.compile(r"\b(\d{1,2}(?:st|nd|rd|th))\.")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def parse_wiki_date(text: str) -> tuple[str, str] | None:
    """Read the first date out of *text*.

    Returns `(iso, precision)` where *precision* is `"day"` for a full date and
    `"month"` when the wiki only gave a month, in which case *iso* is a
    `"YYYY-MM"` string. Keeping the shorter string rather than inventing a day
    is the same choice made for missing stats: an approximate date and a
    precise one must stay distinguishable.
    """
    for match in _DATE.finditer(text or ""):
        parsed = _from_match(match)
        if parsed:
            return parsed
    return None


def _from_match(match: re.Match) -> tuple[str, str] | None:
    if match.group("mdy"):
        month, day, year = match.group("m1"), int(match.group("d1")), int(match.group("y1"))
    elif match.group("dmy"):
        month, day, year = match.group("m2"), int(match.group("d2")), int(match.group("y2"))
    elif match.group("num"):
        # The wiki writes numeric dates US-style ("8/14/2024"). Swap only when
        # the leading number cannot be a month.
        month, day, year = match.group("m4"), int(match.group("d4")), int(match.group("y4"))
        month = int(month)
        if month > 12 and day <= 12:
            month, day = day, month
    else:
        month, day, year = match.group("m3"), None, int(match.group("y3"))

    if isinstance(month, str):
        month = _MONTH_NUMBERS.get(month.lower().rstrip("."))
    if not month or not _EARLIEST_YEAR <= year <= _LATEST_YEAR:
        return None
    if day is None:
        return f"{year:04d}-{month:02d}", "month"
    try:
        return dt.date(year, month, day).isoformat(), "day"
    except ValueError:
        return None


def prose_text(wikitext: str) -> str:
    """Render the free text of a wikitext fragment, dropping its furniture.

    Wikitables are removed wholesale: the balance-update history tables carry a
    date in every row, and `strip_markup` would flatten them into bare
    `(June 2023)` lines indistinguishable from prose. External links are
    reduced to their label for the same reason -- the URLs themselves contain
    words like `release-notes` that would otherwise read as release cues.
    """
    text = _WIKITABLE.sub(" ", wikitext or "")
    text = _EXTERNAL_LINK.sub(lambda m: m.group(1) or " ", text)
    return strip_markup(text)


def iter_sentences(text: str) -> list[str]:
    """Split rendered prose into sentences, one line at a time."""
    out = []
    for line in _ORDINAL_STOP.sub(r"\1", text).split("\n"):
        for sentence in _SENTENCE_SPLIT.split(line):
            sentence = sentence.strip()
            if sentence:
                out.append(sentence)
    return out


def find_release(wikitext: str, subject: str | None = None) -> dict[str, str] | None:
    """Find the release date stated in the prose of *wikitext*.

    *subject* is the hero's name. When a fragment holds several dated release
    sentences, the one that is *about this hero* wins -- a few pages open with a
    note about other heroes ("Skills were adjusted to conform to the new
    Gargoyle heroes introduced on May 16, 2024") before stating their own date.

    Returns `{"date", "precision", "source"}`, or `None` when the wiki simply
    does not say -- which is the case for most of Season 1 and for every hero
    whose page only ever dated its costumes.
    """
    name = _fold(subject) if subject else None
    best: tuple[tuple[int, int, int], dict[str, str]] | None = None

    for order, sentence in enumerate(iter_sentences(prose_text(wikitext))):
        if _NOT_A_RELEASE.search(sentence) or not _RELEASE_CUE.search(sentence):
            continue
        parsed = parse_wiki_date(sentence)
        if not parsed:
            continue
        date, precision = parsed
        # Rank: a sentence about this hero beats one that is not, a full date
        # beats a month, and an earlier sentence beats a later one.
        rank = (
            0 if name and name in _fold(sentence) else 1,
            0 if precision == "day" else 1,
            order,
        )
        candidate = {"date": date, "precision": precision, "source": sentence}
        if best is None or rank < best[0]:
            best = (rank, candidate)
    return best[1] if best else None


def _fold(text: str) -> str:
    """Lower-case and flatten the apostrophes the wiki mixes freely."""
    return re.sub(r"[‘’ʼ`]", "'", text).lower()
