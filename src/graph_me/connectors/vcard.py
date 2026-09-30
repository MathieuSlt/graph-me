"""vCard connector: address books exported as .vcf files (vCard 2.1, 3.0 and 4.0).

Config keys (under ``sources.<name>``): ``paths``: .vcf files or folders containing them.

Each card becomes a ``contact`` item. Contacts are the user's own address book: trust ``self``.
"""

from __future__ import annotations

import hashlib
import quopri
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from graph_me.config import BlacklistConfig, SourceConfig
from graph_me.connectors.base import Contact, Item, Party
from graph_me.pipeline.parse import decode_bytes
from graph_me.pipeline.tier0.identity import norm_bday

_PROP = re.compile(r"^(?:[\w-]+\.)?(?P<name>[\w-]+)(?P<params>(?:;[^:]*)?):(?P<value>.*)$")


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif lines and lines[-1].endswith("=") and "QUOTED-PRINTABLE" in lines[-1].upper():
            lines[-1] = lines[-1][:-1] + raw  # vCard 2.1 soft line break
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1] in "nN" else m[1], value)


def parse_vcards(text: str) -> list[dict[str, list[str]]]:
    """Parse .vcf text into cards: property name -> list of decoded values."""
    cards: list[dict[str, list[str]]] = []
    card: dict[str, list[str]] | None = None
    for line in _unfold(text):
        upper = line.strip().upper()
        if upper == "BEGIN:VCARD":
            card = {}
            continue
        if upper == "END:VCARD":
            if card is not None:
                cards.append(card)
            card = None
            continue
        if card is None:
            continue
        m = _PROP.match(line.strip())
        if not m:
            continue
        name, params, value = m["name"].upper(), m["params"].upper(), m["value"]
        if "QUOTED-PRINTABLE" in params:
            charset = re.search(r"CHARSET=([\w-]+)", params)
            value = quopri.decodestring(value.encode("latin-1", "replace")).decode(
                charset[1].lower() if charset else "utf-8", errors="replace"
            )
        card.setdefault(name, []).append(value)
    return cards


def _names(card: dict[str, list[str]]) -> str | None:
    if card.get("FN") and _unescape(card["FN"][0]).strip():
        return _unescape(card["FN"][0]).strip()
    if card.get("N"):
        parts = [_unescape(p).strip() for p in re.split(r"(?<!\\);", card["N"][0])]
        family, given = (parts + ["", ""])[:2]
        return " ".join(p for p in (given, family) if p) or None
    return None


def card_to_contact(card: dict[str, list[str]]) -> Contact:
    def values(prop: str) -> list[str]:
        out = []
        for v in card.get(prop, []):
            out.extend(x.strip() for x in re.split(r"(?<!\\),", _unescape(v)) if x.strip())
        return out

    return Contact(
        name=_names(card),
        nicknames=values("NICKNAME"),
        emails=[_unescape(v).strip() for v in card.get("EMAIL", []) if v.strip()],
        phones=[_unescape(v).strip() for v in card.get("TEL", []) if v.strip()],
        birthday=norm_bday(card["BDAY"][0]) if card.get("BDAY") else None,
        org=_unescape(card["ORG"][0]).replace(";", " ").strip() if card.get("ORG") else None,
        note=_unescape(card["NOTE"][0]).strip() if card.get("NOTE") else None,
    )


def contact_text(c: Contact) -> str:
    """A small readable text for search: name, nicknames, org, emails, phones, birthday, note."""
    lines = [c.name or ""]
    if c.nicknames:
        lines.append("Surnom / nickname: " + ", ".join(c.nicknames))
    if c.org:
        lines.append(c.org)
    lines += c.emails + c.phones
    if c.birthday:
        lines.append(f"Anniversaire / birthday: {c.birthday}")
    if c.note:
        lines.append(c.note)
    return "\n".join(line for line in lines if line)


class VcardConnector:
    type = "vcard"

    def __init__(self, name: str, source: SourceConfig, blacklist: BlacklistConfig) -> None:
        extra = source.model_extra or {}
        self.name = name
        self.roots = [Path(p).expanduser().resolve() for p in extra.get("paths", [])]
        self.blocked = [Path(p).expanduser().resolve() for p in blacklist.paths]
        self._cards: dict[str, tuple[Path, dict[str, list[str]], str]] = {}

    def _files(self) -> Iterator[Path]:
        for root in self.roots:
            files = [root] if root.is_file() else sorted(root.rglob("*.vcf"))
            for f in files:
                if not any(f == b or f.is_relative_to(b) for b in self.blocked):
                    yield f

    def list_ids(self) -> Iterator[tuple[str, str]]:
        self._cards.clear()
        for path in self._files():
            try:
                text = decode_bytes(path.read_bytes())
            except OSError:
                continue
            for n, card in enumerate(parse_vcards(text)):
                digest = hashlib.sha256(repr(sorted(card.items())).encode()).hexdigest()
                key = card["UID"][0].strip() if card.get("UID") else f"card-{n}-{digest[:12]}"
                external_id = f"{path}#{key}"
                self._cards[external_id] = (path, card, digest)
                yield external_id, digest[:32]

    def fetch(self, external_ids: Iterable[str]) -> Iterator[Item]:
        for external_id in external_ids:
            if external_id not in self._cards:
                continue
            path, card, digest = self._cards[external_id]
            contact = card_to_contact(card)
            yield Item(
                external_id=external_id,
                version=digest[:32],
                kind="contact",
                title=contact.name or (contact.emails + contact.phones + ["(contact)"])[0],
                uri=f"{path}#{external_id.rsplit('#', 1)[1]}",
                text=contact_text(contact),
                content_hash=digest,
                trust="self",
                author=Party(
                    name=contact.name,
                    email=contact.emails[0] if contact.emails else None,
                    phone=contact.phones[0] if contact.phones else None,
                ),
                contact=contact,
            )
