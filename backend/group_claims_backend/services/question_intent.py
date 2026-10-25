"""Small deterministic routing of Matter Review questions.

The routing chooses a retrieval strategy only.  It never decides a legal,
ownership, or billing outcome.  When no rule applies confidently the
question takes the conservative general, source-grounded path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Entity roles the inventory can report.  Only explicit evidence assigns one.
ROLE_INVESTOR = "investor"
ROLE_VEHICLE = "investment_vehicle"
ROLE_SUBFUND = "subfund"
ROLE_CUSTODIAN = "custodian"
ROLE_BROKER = "broker_dealer"
ROLE_CLAIMANT = "legal_claimant"
ROLE_OTHER = "other_organization"
ROLE_UNKNOWN = "unknown"
# Earlier AI review proposal types are broader than one role; keep that
# ambiguity visible instead of narrowing it.
ROLE_INVESTOR_OR_ENTITY = "investor_or_legal_entity"
ROLE_VEHICLE_OR_SUBFUND = "investment_vehicle_or_subfund"
ALL_ROLES = frozenset({
    ROLE_INVESTOR, ROLE_VEHICLE, ROLE_SUBFUND, ROLE_CUSTODIAN, ROLE_BROKER, ROLE_CLAIMANT,
    ROLE_OTHER, ROLE_UNKNOWN, ROLE_INVESTOR_OR_ENTITY, ROLE_VEHICLE_OR_SUBFUND,
})

INTENT_INVENTORY = "inventory"
INTENT_RELATIONSHIP = "relationship"
INTENT_LOOKUP = "specific_lookup"
INTENT_HOLDINGS_COMPARISON = "holdings_comparison"
INTENT_CHANGE = "change"
INTENT_LEGAL_STATUS = "legal_status"
INTENT_COMPARISON = "comparison"
INTENT_CALCULATION = "calculation"
INTENT_GENERAL = "general"

_ISIN = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b")
_NUMERIC_IDENTIFIER = re.compile(r"\b[A-Za-z]{0,4}[-/]?[0-9][0-9\-/.]{3,}[0-9A-Za-z]*\b")
_QUOTED = re.compile(r"[\"“”'‘’]([^\"“”'‘’]{3,80})[\"“”'‘’]")
# Letter-prefixed account or claim codes such as "DEL-880" or "CR-2025-A".
_CODE_IDENTIFIER = re.compile(r"\b[A-Z]{2,6}-[A-Z0-9]*\d[A-Z0-9-]*\b")
_LIST_CUE = re.compile(r"\b(?:list|enumerate|inventory|all|every|each|name the|show me the)\b")
_PLURAL_QUESTION_CUE = re.compile(r"\b(?:which|what|who)\b")
_RELATIONSHIP_CUE = re.compile(
    r"\b(?:associated|association|associate|belongs?|belonging|relationships?|related|relation|parent|umbrella|"
    r"under which|which investor|whose|zugeordnet|gehört)\b"
)
_HOLDINGS_CUE = re.compile(r"\b(?:holdings?|quantit(?:y|ies)|positions?|nominal|bestand|menge|stückzahl)\b")
_BETWEEN_CUE = re.compile(r"\bbetween\b.+\band\b|\bdifferences?\b|\bchanged?\s+(?:in|from)\b|\bfrom\b.+\bto\b")
_COMPARISON_CUE = re.compile(
    r"\b(?:compare|comparison|versus|vs\.?|differ|differs|differences?|each isin|for each isin|each available date)\b"
)
# An explicit quantitative operation, as opposed to a bare "between … and …".
_QUANTITATIVE_CUE = re.compile(
    r"\b(?:compare|comparison|differences?|calculate|net change|quantity changes?|"
    r"quantit(?:y|ies) on (?:both|each)|quantit(?:y|ies) (?:at|for) (?:both|each))\b"
)
# Caveats and negative instructions ("do not treat this as a legal
# assignment") mention legal words without asking a legal question.
_CAVEAT_NOUN = re.compile(r"\b(?:caveats?|disclaimers?|warnings?|limitations?)\b")
_NEGATED_INSTRUCTION = re.compile(
    r"\b(?:(?:do|does|did|should|must|can|could)\s*(?:not\b|n['’]t\b)|cannot\b|never\b)\s*(?:be\s+|by itself\s+|itself\s+)?"
    r"(?:infer|assume|treat|conclude|imply|interpret|present|describe|characteri[sz]e|equate|read|establish|show|prove|"
    r"mean|state|suggest|claim|count|indicate|represent)\w*"
    r"|\bwithout\s+(?:infer|assum|treat|conclud|impl|interpret|present|describ|characteri[sz]|equat|stat|suggest|claim)\w*"
    r"|\b(?:is|are)\s+not\s+(?:evidence|proof)\b|\brather than\b|\binstead of\b"
)
_CLAUSE_BREAK = re.compile(r"[.;:!?\n,]|\band\b|\bplus\b|\balso\b")

# (role, pattern for the singular noun, pattern for the plural noun).
# Subfund is matched first so "sub-fund" is not also read as "fund".
_ROLE_NOUNS: tuple[tuple[frozenset[str], str, str], ...] = (
    (frozenset({ROLE_SUBFUND, ROLE_VEHICLE_OR_SUBFUND}), r"sub-?\s?fund|teilfonds|compartment", r"sub-?\s?funds|teilfonds|compartments"),
    (frozenset({ROLE_INVESTOR, ROLE_INVESTOR_OR_ENTITY}), r"investor|anleger", r"investors|anleger"),
    (frozenset({ROLE_VEHICLE, ROLE_VEHICLE_OR_SUBFUND}), r"(?:investment\s+)?vehicle|fund|fonds", r"(?:investment\s+)?vehicles|funds|fonds"),
    (frozenset({ROLE_CUSTODIAN}), r"custodian|depotbank|depositary|verwahrstelle", r"custodians|depotbanken|depositaries"),
    (frozenset({ROLE_BROKER}), r"broker|dealer|broker-dealer", r"brokers|dealers|broker-dealers"),
    (frozenset({ROLE_CLAIMANT}), r"claimant|plaintiff|kläger(?:in)?", r"claimants|plaintiffs|kläger(?:innen)?"),
    (ALL_ROLES, r"entity|organi[sz]ation|party|compan(?:y)", r"entities|organi[sz]ations|parties|companies"),
)


@dataclass(frozen=True)
class QuestionIntent:
    kind: str
    # Roles an inventory question asks for; empty for other intents.
    roles: frozenset[str] = frozenset()
    # Literal identifiers a specific lookup should search for first.
    identifiers: tuple[str, ...] = ()
    # A holdings comparison that also asks an affirmative legal question; the
    # comparison cannot answer it, so the answer says so separately.
    legal_question: bool = False


def _role_mentions(query: str) -> tuple[frozenset[str], bool]:
    """Return the roles named in a question and whether any was plural."""
    roles: set[str] = set()
    plural = False
    remaining = query
    for role_set, singular, plural_pattern in _ROLE_NOUNS:
        plural_match = re.search(rf"\b(?:{plural_pattern})\b", remaining)
        singular_match = re.search(rf"\b(?:{singular})\b", remaining)
        if plural_match or singular_match:
            roles |= role_set
            plural = plural or plural_match is not None
            remaining = re.sub(rf"\b(?:{plural_pattern}|{singular})\b", " ", remaining)
    return frozenset(roles), plural


def lookup_identifiers(question: str) -> tuple[str, ...]:
    """Literal identifiers (ISIN, account-like numbers, quoted names) in a question."""
    found: list[str] = []
    for match in (*_ISIN.findall(question), *_QUOTED.findall(question), *_CODE_IDENTIFIER.findall(question), *_NUMERIC_IDENTIFIER.findall(question)):
        value = match.strip()
        if len(value) >= 4 and value.casefold() not in {item.casefold() for item in found}:
            found.append(value)
    return tuple(found[:6])


def primary_request_text(question: str) -> str:
    """The question without its caveat requests and negative instructions.

    "Compare the holdings …; do not treat a change as a legal claim
    assignment" asks for a comparison: the legal words only qualify how the
    answer is presented, so they must not choose the route.  A clause that
    requests caveats is dropped entirely; a negative instruction is dropped
    from its cue to the end of its clause.  Everything else is kept, so an
    affirmative legal question ("Which subfund is not participating?") still
    routes as one.
    """
    kept: list[str] = []
    for clause in _CLAUSE_BREAK.split(question.casefold()):
        if _CAVEAT_NOUN.search(clause):
            continue
        negated = _NEGATED_INSTRUCTION.search(clause)
        kept.append(clause[:negated.start()] if negated else clause)
    return " ".join(" ".join(kept).split())


def classify_question(
    question: str, *, calculation: bool, legal_status: bool, change: bool,
) -> QuestionIntent:
    """Choose one retrieval strategy, most specific rule first.

    ``calculation``, ``legal_status`` and ``change`` come from the existing
    Matter Review keyword rules, evaluated on ``primary_request_text`` for
    calculation and legal status.  Precedence matters: the broad change rule
    matches words such as "holdings", so it is consulted only after the
    structured holdings, relationship and inventory rules.  An explicit
    quantitative holdings comparison ("compare … quantities …, calculate
    differences") is the primary operation even when the question also asks
    about legal status; that part is answered as a separate caveat.
    """
    query = " ".join(question.casefold().split())
    roles, plural = _role_mentions(query)
    identifiers = lookup_identifiers(question)
    if calculation:
        return QuestionIntent(INTENT_CALCULATION)
    holdings_comparison = bool(_HOLDINGS_CUE.search(query) and (_COMPARISON_CUE.search(query) or _BETWEEN_CUE.search(query)))
    if legal_status and not (holdings_comparison and _QUANTITATIVE_CUE.search(query)):
        return QuestionIntent(INTENT_LEGAL_STATUS)
    if holdings_comparison:
        return QuestionIntent(INTENT_HOLDINGS_COMPARISON, roles=roles, identifiers=identifiers, legal_question=legal_status)
    # A relationship needs two kinds of entity, e.g. investors and subfunds.
    entity_kinds = sum(bool(roles & group) for group in (
        frozenset({ROLE_INVESTOR, ROLE_INVESTOR_OR_ENTITY}),
        frozenset({ROLE_SUBFUND, ROLE_VEHICLE, ROLE_VEHICLE_OR_SUBFUND}),
    ))
    if entity_kinds == 2 and _RELATIONSHIP_CUE.search(query):
        return QuestionIntent(INTENT_RELATIONSHIP, roles=roles)
    # "Which fund is named?" is a lookup; "Which funds…" or "List all …" asks
    # for every matching entity.
    if roles and (_LIST_CUE.search(query) or (plural and _PLURAL_QUESTION_CUE.search(query))):
        return QuestionIntent(INTENT_INVENTORY, roles=roles)
    if change:
        return QuestionIntent(INTENT_CHANGE, identifiers=identifiers)
    if _COMPARISON_CUE.search(query):
        return QuestionIntent(INTENT_COMPARISON, identifiers=identifiers)
    if identifiers:
        return QuestionIntent(INTENT_LOOKUP, identifiers=identifiers)
    return QuestionIntent(INTENT_GENERAL)


_NAME_TOKEN = re.compile(r"[A-ZÄÖÜ][\w&'.-]*|[A-Z]\b")
# Capitalised sentence and instruction words that never start an entity name.
_LEADING_WORDS = frozenset({
    "Compare", "Show", "List", "Using", "Use", "Identify", "Calculate", "Cite", "Which", "What", "Who", "Where",
    "When", "How", "Please", "Give", "Find", "Name", "Does", "Did", "Do", "Is", "Are", "Was", "Were", "Explain",
    "Summarize", "Summarise", "Between", "For", "In", "The", "And", "Only", "Tell", "Distinguish",
})


_PREFIX_ABBREVIATIONS = frozenset({"no", "nr", "st", "mr", "mrs", "ms", "dr", "mt", "ft"})


_ROLE_WORDS = frozenset({"fund", "subfund", "sub-fund", "participant", "class", "series", "compartment", "portfolio"})


def is_contextual_phrase(phrase: str) -> bool:
    """A phrase that is not a literal document name.

    "NOT APPROVED" is an instruction in capitals; "Fund B" or "Participant
    A" is a role-and-marker label resolved against the matter's records.
    """
    words = phrase.split()
    if len(words) >= 2 and all(word.isalpha() and word.isupper() and len(word) > 1 for word in words):
        return True
    return len(words) == 2 and words[0].casefold() in _ROLE_WORDS and re.fullmatch(r"[A-Z]|\d{1,2}|[IVX]{1,4}", words[1]) is not None


def question_entity_names(question: str) -> list[tuple[str, ...]]:
    """Capitalised multi-word phrases that may name an entity.

    Each phrase is returned with its shorter suffixes (dropping a leading
    sentence word such as "Compare"); the caller keeps only phrases actually
    present in the scoped documents, so nothing here is treated as a fact.
    """
    phrases: list[tuple[str, ...]] = []

    def flush(run: list[str]) -> None:
        while run and run[-1] in {"of", "de", "&"}:
            run.pop()
        while run and run[0] in _LEADING_WORDS:
            run.pop(0)
        if len(run) >= 2:
            phrases.append(tuple(" ".join(run[index:]) for index in range(len(run) - 1)))

    for segment in re.split(r"[,;:()?!\n]|\band\b|\bor\b|\bbetween\b|\bfor\b", question):
        run: list[str] = []
        for word in segment.split():
            cleaned = re.sub(r"['’]s$", "", word.strip(".\"'“”‘’"))
            if (cleaned and _NAME_TOKEN.fullmatch(cleaned)) or (run and cleaned in {"of", "de", "&"}):
                run.append(cleaned)
                # A full stop ends the sentence and the name ("… Fund C. Compare …"),
                # except after a title such as "No." or "St." that precedes more name.
                if word.rstrip("\"'”’)").endswith(".") and cleaned.casefold() not in _PREFIX_ABBREVIATIONS:
                    flush(run)
                    run = []
                continue
            flush(run)
            run = []
        flush(run)
    return phrases
