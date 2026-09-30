"""Normalization of the identifiers people are recognized by (no model)."""

from __future__ import annotations

import re

_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_BDAY = re.compile(
    r"^(?:(?P<y>\d{4})-?|--)(?P<m>\d{2})-?(?P<d>\d{2})(?:T.*)?$"
)  # 1990-03-12, 19900312, --0312, --03-12, 1990-03-12T00:00:00Z


def norm_email(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().strip("<>").casefold()
    if value.startswith("mailto:"):
        value = value[7:]
    return value if _EMAIL.match(value) else None


def norm_phone(value: str | None, country_code: str | None = None) -> str | None:
    """E.164-like form: '+33612345678'. National numbers need ``country_code`` (e.g. '33')."""
    if not value:
        return None
    raw = value.strip()
    if raw.startswith("tel:"):
        raw = raw[4:]
    raw = raw.split("@", 1)[0]  # WhatsApp JIDs: 33612345678@s.whatsapp.net
    if raw.startswith("+"):
        raw = raw.replace("(0)", "")  # "+33 (0)6 12..." keeps a national trunk prefix
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 6:
        return None
    if raw.startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0") and not digits.startswith("00"):
        return f"+{country_code}{digits[1:]}" if country_code else None
    return "+" + digits  # international number written without "+"


def norm_bday(value: str | None) -> str | None:
    """'MM-DD' or 'YYYY-MM-DD', or None when the value is not a usable date."""
    if not value:
        return None
    m = _BDAY.match(value.strip())
    if not m:
        return None
    month, day = int(m["m"]), int(m["d"])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    year = m["y"]
    if year and year not in ("1604", "0000"):  # Apple uses 1604 for "no year"
        return f"{year}-{month:02d}-{day:02d}"
    return f"{month:02d}-{day:02d}"


def norm_name(value: str | None) -> str | None:
    if not value:
        return None
    value = " ".join(value.split())
    if not value or norm_email(value) or re.fullmatch(r"[+\d\s().-]+", value):
        return None  # an address or a number is not a name
    return value
