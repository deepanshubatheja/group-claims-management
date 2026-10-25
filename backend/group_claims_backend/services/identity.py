"""Canonical identity resolution for parties, subfunds, and accounts.

Every legal entity, investment vehicle, and account keeps one internal id.
Each name or reference a source document uses for it is stored as an alias,
so later documents that spell it differently resolve to the same record.

Matching is deliberately tiered and conservative:

* ``matched``: the normalized alias is already known for exactly one record
  (case, spacing, punctuation, and equivalent legal-form spellings such as
  "Limited"/"Ltd" are ignored).  Safe to link automatically.
* ``ask``: only a weaker similarity exists (same name once legal-form words
  are removed, or a close spelling).  This is never linked automatically; the
  caller raises a "same as?" question for the user.
* ``new``: nothing comparable exists.
* ``ambiguous``: the normalized alias already belongs to several records.

Series numbers and single-letter markers must agree before a weaker match is
even suggested, so "Fund A" is never proposed as the same as "Fund B".
Account references are identifiers, so they only ever match exactly after
normalizing case and separators; they are never fuzzy-matched.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal


TargetType = Literal["legal_entity", "investment_vehicle", "account"]
TARGET_TYPES: frozenset[str] = frozenset({"legal_entity", "investment_vehicle", "account"})

# Equivalent spellings of the same legal form.  Mapping keeps the legal form
# in the normalized key, so "Alpha Ltd" and "Alpha Limited" match while
# "Alpha" and "Alpha Ltd" only reach the weaker ``ask`` tier.
_LEGAL_FORM_SYNONYMS = {
    "limited": "ltd", "ltd": "ltd",
    "incorporated": "inc", "inc": "inc",
    "corporation": "corp", "corp": "corp",
    "company": "co", "co": "co",
    "plc": "plc", "llc": "llc", "llp": "llp", "lp": "lp",
    "sa": "sa", "ag": "ag", "nv": "nv", "bv": "bv", "gmbh": "gmbh", "sarl": "sarl",
    "sicav": "sicav", "sicaf": "sicaf", "scs": "scs", "scsp": "scsp",
}
_LEGAL_FORMS = frozenset(_LEGAL_FORM_SYNONYMS.values())
# Series numerals after the first word ("Fund II") compare equal to digits.
_ROMAN_NUMERALS = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}
_SIMILARITY_THRESHOLD = 0.88


def normalize_name(value: str) -> str:
    """Return the comparison key used for automatic name matching."""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(character for character in text if not unicodedata.combining(character)).casefold()
    text = text.replace("&", " and ")
    # Dotted abbreviations (S.A., L.P.) collapse before other punctuation
    # becomes a word break.
    text = re.sub(r"(?<=\b\w)\.(?=\w\b)", "", text)
    text = re.sub(r"[^\w]+", " ", text).replace("_", " ")
    tokens = [_LEGAL_FORM_SYNONYMS.get(token, token) for token in text.split()]
    tokens = [_ROMAN_NUMERALS.get(token, token) if index else token for index, token in enumerate(tokens)]
    return " ".join(tokens)


def _distinguishing_markers(normalized: str) -> tuple[str, ...]:
    """Series numbers and single letters ("Fund A" vs "Fund B") that must agree."""
    return tuple(sorted({token for token in normalized.split() if token.isdigit() or len(token) == 1}))


def core_name(value: str) -> str:
    """Return the normalized name without legal-form words (for ``ask`` only)."""
    tokens = [token for token in normalize_name(value).split() if token not in _LEGAL_FORMS and token not in {"the", "and"}]
    return " ".join(tokens)


def normalize_account_reference(value: str) -> str:
    """Return an account comparison key: case and separators are ignored."""
    return re.sub(r"[\s\-_./]+", "", unicodedata.normalize("NFKC", value)).upper()


def normalize_alias(target_type: str, value: str) -> str:
    return normalize_account_reference(value) if target_type == "account" else normalize_name(value)


@dataclass(frozen=True)
class Resolution:
    status: Literal["matched", "ask", "new", "ambiguous"]
    target_id: str | None = None
    basis: str | None = None


class IdentityResolver:
    """Reads and records aliases inside the caller's transaction."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def resolve(self, target_type: TargetType, value: str, *, within: Collection[str] | None = None) -> Resolution:
        """Resolve a name; ``within`` limits candidates (e.g. one investor's funds)."""
        normalized = normalize_alias(target_type, value)
        if not normalized:
            return Resolution("new")
        exact = sorted({
            str(row["target_id"]) for row in self._connection.execute(
                "SELECT target_id FROM identity_aliases WHERE target_type = ? AND normalized_alias = ?",
                (target_type, normalized),
            )
            if within is None or str(row["target_id"]) in within
        })
        if len(exact) == 1:
            return Resolution("matched", exact[0], "known_alias")
        if len(exact) > 1:
            return Resolution("ambiguous", None, "alias_shared_by_several_records")
        if target_type == "account":
            return Resolution("new")
        return self._similar(target_type, value, normalized, within)

    def is_alias_of(self, target_type: TargetType, target_id: str, value: str) -> bool:
        """True when ``value`` is already a recorded name of this exact record."""
        normalized = normalize_alias(target_type, value)
        return bool(normalized) and self._connection.execute(
            "SELECT 1 FROM identity_aliases WHERE target_type = ? AND target_id = ? AND normalized_alias = ? LIMIT 1",
            (target_type, target_id, normalized),
        ).fetchone() is not None

    def _similar(self, target_type: TargetType, value: str, normalized: str, within: Collection[str] | None = None) -> Resolution:
        declined = {
            str(row["suggested_target_id"]) for row in self._connection.execute(
                "SELECT suggested_target_id FROM identity_link_questions WHERE target_type = ? AND normalized_observed = ? AND status = 'distinct'",
                (target_type, normalized),
            )
        }
        observed_core = core_name(value)
        observed_markers = _distinguishing_markers(normalized)
        best: tuple[float, str, str] | None = None
        for row in self._connection.execute(
            "SELECT target_id, alias_text, normalized_alias FROM identity_aliases WHERE target_type = ? ORDER BY created_at, id",
            (target_type,),
        ):
            target_id = str(row["target_id"])
            if (within is not None and target_id not in within) or target_id in declined or _distinguishing_markers(str(row["normalized_alias"])) != observed_markers:
                continue
            if observed_core and core_name(str(row["alias_text"])) == observed_core:
                score, basis = 1.0, "same_name_without_legal_form"
            else:
                score = difflib.SequenceMatcher(None, normalized, str(row["normalized_alias"])).ratio()
                basis = "similar_spelling"
            if score >= _SIMILARITY_THRESHOLD and (best is None or score > best[0]):
                best = (score, target_id, basis)
        if best is None:
            return Resolution("new")
        return Resolution("ask", best[1], best[2])

    def record_alias(
        self,
        target_type: TargetType,
        target_id: str,
        value: str,
        *,
        origin: Literal["observed", "user_confirmed"],
        source_document_id: str | None,
        first_seen_on: str | None,
        now: str,
    ) -> None:
        """Record a spelling used for a record; repeated spellings are ignored."""
        alias = value.strip()
        normalized = normalize_alias(target_type, alias)
        if not alias or not normalized:
            return
        self._connection.execute(
            "INSERT OR IGNORE INTO identity_aliases (id, target_type, target_id, alias_text, normalized_alias, origin, source_document_id, first_seen_on, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), target_type, target_id, alias, normalized, origin, source_document_id, first_seen_on, now),
        )
