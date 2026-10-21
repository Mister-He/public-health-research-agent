"""Identifier and text normalization. Conservative by design: when unsure, do not normalize
two different things into the same key."""

from __future__ import annotations

import html
import re
import unicodedata
from urllib.parse import unquote

_DOI_PREFIXES = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*|urn:doi:)", re.IGNORECASE)
_DOI_VALID = re.compile(r"^10\.\d{4,9}/\S+$")


def normalize_doi(value: str | None) -> str | None:
    """Lower-cased bare DOI ('10.x/y'), or None if the value is not a DOI.

    DOIs are case-insensitive by spec; URL encoding and resolver prefixes are removed;
    trailing punctuation picked up from text is stripped."""
    if not value:
        return None
    v = unquote(str(value)).strip()
    v = _DOI_PREFIXES.sub("", v).strip()
    v = v.rstrip(".,;)]}>\"' ")
    v = v.lower()
    return v if _DOI_VALID.match(v) else None


def normalize_pmid(value: str | int | None) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    v = re.sub(r"^(?:pmid:?\s*)", "", v, flags=re.IGNORECASE)
    return v if re.fullmatch(r"[1-9]\d{0,9}", v) else None


def normalize_pmcid(value: str | None) -> str | None:
    if not value:
        return None
    v = str(value).strip().upper()
    if re.fullmatch(r"\d+", v):
        v = "PMC" + v
    return v if re.fullmatch(r"PMC\d+", v) else None


def normalize_arxiv(value: str | None) -> tuple[str | None, int | None]:
    """'arXiv:2401.01234v3' -> ('2401.01234', 3). Old-style ids keep their archive."""
    if not value:
        return None, None
    v = str(value).strip()
    v = re.sub(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:)", "", v, flags=re.IGNORECASE)
    v = re.sub(r"\.pdf$", "", v)
    m = re.fullmatch(r"(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v(\d+))?", v, flags=re.IGNORECASE)
    if not m:
        return None, None
    return m[1].lower(), int(m[2]) if m[2] else None


def normalize_openalex(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"(W\d+)$", str(value).strip(), flags=re.IGNORECASE)
    return m[1].upper() if m else None


_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_markup(text: str | None) -> str | None:
    """Remove JATS/HTML tags and entities; collapse whitespace. Preserves wording exactly."""
    if text is None:
        return None
    t = html.unescape(_TAGS.sub(" ", text))
    t = _WS.sub(" ", t).strip()
    return t or None


def normalize_title(title: str | None) -> str:
    """Comparison key for titles: Unicode NFKC, markup removed, case-folded,
    punctuation and whitespace removed (letters/digits of every script kept)."""
    if not title:
        return ""
    t = strip_markup(title) or ""
    t = unicodedata.normalize("NFKC", t).casefold()
    t = "".join(ch for ch in unicodedata.normalize("NFKD", t) if not unicodedata.combining(ch))
    return re.sub(r"[\W_]+", "", t)


def normalize_text_for_match(text: str | None) -> str:
    """Lower-cased text with hyphens/punctuation as spaces; used for phrase matching."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", strip_markup(text) or "").casefold()
    t = re.sub(r"[\-‐-―/]", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return " " + _WS.sub(" ", t).strip() + " "


def phrase_in(phrase: str, normalized_text: str) -> bool:
    """Whole-word phrase match on text produced by normalize_text_for_match.
    A trailing '*' makes the last word a prefix ('epidemiolog*' matches 'epidemiology')."""
    prefix = phrase.strip().endswith("*")
    p = normalize_text_for_match(phrase.strip().rstrip("*")).strip()
    if not p:
        return False
    return f" {p}" in normalized_text if prefix else f" {p} " in normalized_text


def surname(name: str) -> str:
    """Best-effort family name for conservative title+author matching."""
    name = (name or "").strip()
    if "," in name:
        fam = name.split(",", 1)[0]
    else:
        parts = name.split()
        # PubMed-style "Smith JA": initials last
        if len(parts) >= 2 and re.fullmatch(r"[A-Z]{1,3}", parts[-1]):
            fam = " ".join(parts[:-1])
        else:
            fam = parts[-1] if parts else ""
    return normalize_title(fam)


def is_preprint_doi(doi: str | None) -> bool:
    """Known preprint-server DOI registrants. medRxiv/bioRxiv moved from 10.1101 to 10.64898
    in 2026 — both are recognized. Used only as a hint; source records set status explicitly."""
    if not doi:
        return False
    return doi.startswith(("10.1101/", "10.64898/", "10.21203/", "10.2139/ssrn", "10.20944/",
                           "10.31219/", "10.22541/", "10.36227/", "10.48550/arxiv"))
