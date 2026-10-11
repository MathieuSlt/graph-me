"""Tier 0 values from rules (no model): deadlines and amounts written near a keyword.

"The lease ends on June 30, 2026" -> ``ends: 2026-06-30``; "Devis : 480 €" -> ``quote_amount:
480 EUR``. Keywords come from the rule packs (``deadlines``, ``amounts``), matched on folded
words (case and accents ignored). A date must have a day, a month and a year, and is read with the
pack's ``date_order``. A date equal to the message's own date is skipped (it is when it was
written, not a deadline).

Pure and picklable: it runs in the worker processes, next to parsing (pipeline/run.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from functools import cache, lru_cache

from graph_me import rules
from graph_me.pipeline.sanitize import fold

DATE_WINDOW_WORDS = 12  # how far after a keyword a date may be
AMOUNT_WINDOW_WORDS = 30  # amounts come later ("the quote for ... comes to 480 dollars")
MIN_YEAR, MAX_YEAR = 1990, 2100
MAX_VALUES = 20  # per item: a long statement must not flood the graph

_WORD = re.compile(r"\w+")
_DATE = re.compile(
    r"\b\d{4}-\d{1,2}-\d{1,2}\b"  # 2026-06-30
    r"|\b\d{1,2}[/.]\d{1,2}[/.](?:\d{4}|\d{2})\b"  # 30/06/2026, 06.30.26
    r"|\b\d{1,2}(?:er|st|nd|rd|th)?\s+\w{3,}\.?\s+\d{4}\b"  # 30 juin 2026, 1st July 2025
    r"|\b\w{3,}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b"  # June 30, 2026
)
_CURRENCY = {
    "$": "USD", "usd": "USD", "dollar": "USD", "dollars": "USD",
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "£": "GBP", "gbp": "GBP", "pound": "GBP", "pounds": "GBP",
    "chf": "CHF", "cad": "CAD",
}  # fmt: skip
_NUM = r"\d[\d\s.,']*\d|\d"
_CUR = r"\$|€|£|usd|eur|euros?|dollars?|gbp|pounds?|chf|cad"
_AMOUNT = re.compile(
    rf"(?P<c1>\$|€|£)\s?(?P<n1>{_NUM})|(?P<n2>{_NUM})\s?(?P<c2>{_CUR})(?!\w)", re.IGNORECASE
)


@dataclass(frozen=True)
class Rule:
    words: tuple[str, ...]  # folded keyword words
    key: str  # fact key: expires | renews | due | ends | quote_amount | invoice_amount
    lang: str
    date_order: str
    is_amount: bool


# Bump when extraction code changes what it finds; rule pack edits are hashed in too.
CODE_VERSION = 1


@cache
def rules_version() -> str:
    """Changes when the extraction rules change, so stored items get the new rules too."""
    import hashlib
    import json

    packs = {lang: [p.date_order, p.deadlines, p.amounts] for lang, p in rules.load_all().items()}
    data = json.dumps([CODE_VERSION, packs], sort_keys=True, default=list)
    return hashlib.sha256(data.encode()).hexdigest()[:16]


@cache
def _rules() -> dict[str, tuple[Rule, ...]]:
    """Rules by their first folded word, longer phrases first ("renewal date" before "renewal")."""
    out: list[Rule] = []
    for lang, pack in rules.load_all().items():
        for key, phrases in pack.deadlines.items():
            out += [Rule(tuple(_WORD.findall(fold(p))), key, lang, pack.date_order, False)
                    for p in phrases]  # fmt: skip
        for key, phrases in pack.amounts.items():
            out += [Rule(tuple(_WORD.findall(fold(p))), f"{key}_amount", lang, pack.date_order,
                         True) for p in phrases]  # fmt: skip
    index: dict[str, list[Rule]] = {}
    for rule in sorted(out, key=lambda r: -len(r.words)):
        if rule.words:
            index.setdefault(rule.words[0], []).append(rule)
    return {word: tuple(found) for word, found in index.items()}


def extract(text: str, item_date: date | None = None) -> list[tuple[str, str, str]]:
    """``(key, value, method)`` found in ``text``; value is an ISO date or "<amount> <CUR>"."""
    if not text:
        return []
    index = _rules()
    tokens = [(fold(m.group()), m.start(), m.end()) for m in _WORD.finditer(text)]
    words = [t[0] for t in tokens]
    found: dict[tuple[str, str], str] = {}
    for i, word in enumerate(words):
        for rule in index.get(word, ()):
            n = len(rule.words)
            if tuple(words[i : i + n]) != rule.words:
                continue
            reach = AMOUNT_WINDOW_WORDS if rule.is_amount else DATE_WINDOW_WORDS
            start = tokens[i + n - 1][2]
            end = tokens[min(i + n - 1 + reach, len(tokens) - 1)][2]
            window = text[start:end]
            value = _amount(window) if rule.is_amount else _date(window, rule, item_date)
            if value:
                kind = "amount" if rule.is_amount else "deadline"
                found.setdefault((rule.key, value), f"rule:{rule.lang}.{kind}")
                if len(found) >= MAX_VALUES:
                    return [(k, v, m) for (k, v), m in found.items()]
            break  # the longest phrase starting here was tried
    return [(k, v, m) for (k, v), m in found.items()]


def _date(window: str, rule: Rule, item_date: date | None) -> str | None:
    for m in _DATE.finditer(window):
        day = _parse_date(m.group(), rule.lang, rule.date_order)
        if day is None:
            continue
        if item_date is not None and day == item_date:
            return None
        return day.isoformat()
    return None


@lru_cache(maxsize=4096)
def _parse_date(text: str, lang: str, date_order: str) -> date | None:
    """A full date (day, month and year) or None. Cached: documents repeat their dates."""
    import dateparser  # imported here: slow to load, and only needed near a keyword

    settings = {"DATE_ORDER": date_order, "REQUIRE_PARTS": ["day", "month", "year"],
                "STRICT_PARSING": True, "PREFER_LOCALE_DATE_ORDER": False}  # fmt: skip
    parsed = dateparser.parse(text, languages=[lang, "en"], settings=settings)
    if parsed is None or not (MIN_YEAR <= parsed.year <= MAX_YEAR):
        return None
    return parsed.date()


def _amount(window: str) -> str | None:
    m = _AMOUNT.search(window)
    if not m:
        return None
    number, currency = (m["n1"], m["c1"]) if m["n1"] else (m["n2"], m["c2"])
    value = _number(number)
    if value is None or value <= 0:
        return None
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{text} {_CURRENCY[currency.casefold()]}"


def _number(raw: str) -> float | None:
    """1,450.00 / 1 450,00 / 1.450,00 / 1'450 -> 1450.0"""
    raw = re.sub(r"[\s']", "", raw)
    comma, dot = raw.rfind(","), raw.rfind(".")
    if comma >= 0 and dot >= 0:  # the last one is the decimal separator
        decimal = "," if comma > dot else "."
        raw = raw.replace("." if decimal == "," else ",", "").replace(decimal, ".")
    elif comma >= 0 or dot >= 0:
        sep = "," if comma >= 0 else "."
        head, _, tail = raw.rpartition(sep)
        raw = f"{head.replace(sep, '')}.{tail}" if len(tail) in (1, 2) else raw.replace(sep, "")
    try:
        return float(raw)
    except ValueError:
        return None
