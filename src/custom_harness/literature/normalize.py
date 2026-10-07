"""Text normalisation for citation checks (GOAL §5 L4.3). Pure functions, no I/O, no model calls.

Rule: a normalisation may only erase differences a reader cannot see or that never carry meaning in a citation
(Unicode compatibility forms, zero-width characters, whitespace, letter case, quotation marks, dash variants).
It must never erase a difference that changes meaning: punctuation (sentence boundaries, minus signs, decimal
points, brackets) stays a token, words must match whole, and nothing is mapped across scripts (a Cyrillic 'а' is
not a Latin 'a'). Anything a reader could not see but that changes what is compared (bidirectional overrides,
Unicode tag characters, other control characters) makes a quote unsupported.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

MIN_QUOTE_WORDS = 5  # a shorter span ("improves accuracy") appears almost anywhere and is not evidence

# invisible characters that do not change the visible text: removed
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
# dash and minus variants -> ASCII hyphen-minus (a token, never dropped: it can be a sign)
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−﹣－"), "-")
# quotation marks and apostrophes: dropped (authors legitimately change inner quote styles)
_QUOTES = dict.fromkeys(map(ord, "'\"`‘’‚‛“”„‟′″«»"), None)
_TOKEN = re.compile(r"\w+|[^\w\s]")
_WORD = re.compile(r"\w+")
_TAG = re.compile(r"<[^>]{0,200}>")  # HTML/JATS markup in Crossref/OpenAlex titles and abstracts
_ELISION = re.compile(r"\.\s*\.\s*\.")
_EDGE_PUNCT = {".", ",", ";", ":", "!", "?"}
_SCRIPTS = ("LATIN", "CYRILLIC", "GREEK", "ARMENIAN", "CHEROKEE")


def _hidden_controls(text: str) -> list[str]:
    """Characters that are invisible yet change what is compared: bidi controls, tag characters, other format
    (Cf) or control (Cc) characters, except the harmless invisibles above and ordinary whitespace."""
    bad = []
    for ch in text:
        if ord(ch) in _INVISIBLE or ch in "\t\n\r":
            continue
        if unicodedata.category(ch) in ("Cf", "Cc", "Co", "Cs"):
            bad.append(f"U+{ord(ch):04X}")
    return bad


def clean(text: str) -> str:
    """NFKC, invisible characters removed, dashes unified, quote marks dropped, case folded, spaces collapsed."""
    t = unicodedata.normalize("NFKC", text).translate(_INVISIBLE).translate(_DASHES).translate(_QUOTES)
    return " ".join(t.casefold().split())


def tokens(text: str) -> list[str]:
    """Words and single punctuation characters, in order (punctuation is kept: it carries meaning)."""
    return _TOKEN.findall(clean(text))


def strip_markup(text: str) -> str:
    return _TAG.sub(" ", text)


def mixed_script_words(text: str) -> list[str]:
    """Words whose letters come from more than one confusable script (e.g. Latin with one Cyrillic letter)."""
    out = []
    for w in _WORD.findall(unicodedata.normalize("NFKC", text).translate(_INVISIBLE)):
        scripts = set()
        for ch in w:
            if ch.isalpha():
                name = unicodedata.name(ch, "")
                scripts.update(s for s in _SCRIPTS if name.startswith(s))
        if len(scripts) > 1:
            out.append(w)
    return out


# ------------------------------------------------------------------ titles
def title_key(title: str) -> tuple[str, ...]:
    """Words only (title punctuation and markup vary across indexes and carry no meaning)."""
    return tuple(_WORD.findall(clean(strip_markup(title or ""))))


def titles_match(claimed: str, found: str) -> bool:
    a, b = title_key(claimed), title_key(found)
    return bool(a) and a == b


# ------------------------------------------------------------------ authors
_ET_AL = re.compile(r"[\s,]*\bet\.?\s*al\b\.?\s*$", re.IGNORECASE)


def _ascii_fold(s: str) -> str:
    d = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", s).translate(_INVISIBLE))
    return "".join(ch for ch in d if unicodedata.category(ch) != "Mn").casefold()


def family_name(name: str) -> str:
    """'Okafor, Mira' / 'Mira Okafor' / 'M. Okafor' -> 'okafor' (last word of the family name, accents folded)."""
    n = (name or "").strip()
    fam = n.split(",", 1)[0] if "," in n else n
    words = [w for w in (re.sub(r"[^\w]", "", _ascii_fold(x)) for x in fam.split()) if w]
    return words[-1] if words else ""


def authors_match(claimed: Sequence[str], found: Sequence[str]) -> bool:
    """The claimed authors, in order, are a prefix of the paper's authors (an 'et al.' tail is allowed).
    Nothing claimed is not a match."""
    names = [_ET_AL.sub("", c).strip() for c in claimed]
    names = [c for c in names if c]
    if not names or len(names) > len(found):
        return False
    for c, f in zip(names, found, strict=False):
        fc, ff = family_name(c), family_name(f)
        if not fc or fc != ff:
            return False
    return True


# ------------------------------------------------------------------ quote-span support
def quote_support(quote: str, texts: Iterable[str | None]) -> tuple[str, str]:
    """('supported' | 'unsupported' | 'unchecked', reason).

    supported: the quote's tokens occur as one contiguous whole-token run in one of `texts`.
    unsupported: it does not, or the quote itself is not acceptable evidence (hidden control characters,
    elisions, mixed-script words, too short, empty).
    unchecked: the quote is acceptable but no text is available to check it against (never counts as supported).
    """
    hidden = _hidden_controls(quote or "")
    if hidden:
        return "unsupported", f"quote contains invisible control characters ({', '.join(hidden[:5])})"
    c = clean(quote or "")
    if _ELISION.search(c):
        return "unsupported", "elided quote ('...'): only verbatim contiguous spans are accepted"
    q = _TOKEN.findall(c)
    while q and q[0] in _EDGE_PUNCT:
        q.pop(0)
    while q and q[-1] in _EDGE_PUNCT:
        q.pop()
    n_words = sum(1 for t in q if _WORD.fullmatch(t))
    if n_words == 0:
        return "unsupported", "empty quote"
    if n_words < MIN_QUOTE_WORDS:
        return "unsupported", f"quote too short to be evidence ({n_words} < {MIN_QUOTE_WORDS} words)"
    mixed = mixed_script_words(quote)
    if mixed:
        return "unsupported", f"mixed-script words (possible homoglyphs): {mixed[:3]}"
    available = [t for t in texts if t and t.strip()]
    if not available:
        return "unchecked", "no stored abstract or full text to check the quote against"
    needle = "\x00" + "\x00".join(q) + "\x00"
    for t in available:
        hay = "\x00" + "\x00".join(tokens(strip_markup(t))) + "\x00"
        if needle in hay:
            return "supported", "verbatim span found in the stored text"
    return "unsupported", "quote not found as a contiguous span in the stored abstract/full text"
