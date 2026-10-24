"""Resolve documents a question names explicitly (by file name, date, or type).

Question-level references only ever *narrow* the already-authorized scope.
A date or type must be supported by stored metadata (the file name and
format); nothing is guessed from content, and ambiguity is reported for the
user to clarify instead of choosing a document.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_MONTHS = {
    "january": 1, "jan": 1, "januar": 1, "february": 2, "feb": 2, "februar": 2, "march": 3, "mar": 3, "märz": 3,
    "maerz": 3, "april": 4, "apr": 4, "may": 5, "mai": 5, "june": 6, "jun": 6, "juni": 6, "july": 7, "jul": 7,
    "juli": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "oktober": 10,
    "november": 11, "nov": 11, "december": 12, "dec": 12, "dezember": 12,
}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_QUESTION_DATES = (
    (re.compile(r"\b(?P<y>\d{4})[-_/.](?P<m>\d{1,2})[-_/.](?P<d>\d{1,2})\b"), True),
    (re.compile(r"\b(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})\b"), True),
    (re.compile(rf"\b(?P<mon>{_MONTH})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(?P<y>\d{{4}}))?\b", re.IGNORECASE), False),
    (re.compile(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\.?\s+(?P<mon>{_MONTH})\.?(?:,?\s+(?P<y>\d{{4}}))?\b", re.IGNORECASE), False),
)
_FILENAME_DATES = (
    re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})[-_.]?(?P<m>0[1-9]|1[0-2])[-_.]?(?P<d>0[1-9]|[12]\d|3[01])(?!\d)"),
    re.compile(r"(?<!\d)(?P<d>0[1-9]|[12]\d|3[01])[-_.](?P<m>0[1-9]|1[0-2])[-_.](?P<y>(?:19|20)\d{2})(?!\d)"),
)
_FILENAME = re.compile(r"[\w\-.()]+\.(?:xlsx|xls|csv|pdf)\b", re.IGNORECASE)
_NUMBERED_FILENAME_PREFIX = re.compile(r"^0*(?P<number>[1-9][0-9]{0,3})(?:[_. -]+)")
_DOCUMENT_NUMBER = re.compile(
    r"\b(?:document|doc|file)\s*(?:no\.?|number)?\s*0*(?P<number>[1-9][0-9]{0,3})\b",
    re.IGNORECASE,
)
_DOCUMENT_NUMBER_RANGE = re.compile(
    r"\b(?:documents|docs|files)\s*(?:no\.?|numbers?)?\s*0*(?P<first>[1-9][0-9]{0,3})\s*"
    r"(?:through|to|[-–])\s*0*(?P<last>[1-9][0-9]{0,3})\b",
    re.IGNORECASE,
)
_ALL_DOCUMENTS = re.compile(
    r"\b(?:all|every)\s+(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+)?"
    r"(?:synthetic\s+)?(?:source\s+)?documents?\b",
    re.IGNORECASE,
)
_DOCUMENT_NOUN = re.compile(
    r"\b(?:spreadsheets?|workbooks?|excel|xlsx|xls|csv|files?|documents?|statements?|reports?|pdfs?|sheets?|registers?)\b",
    re.IGNORECASE,
)
_SPREADSHEET_NOUN = re.compile(r"\b(?:spreadsheets?|workbooks?|excel|xlsx|xls|csv|sheets?)\b", re.IGNORECASE)
_PDF_NOUN = re.compile(r"\bpdfs?\b", re.IGNORECASE)
_SPREADSHEET_FORMATS = frozenset({"xlsx", "xls", "csv"})
_DESCRIPTOR_STOPWORDS = frozenset({
    "using", "only", "with", "from", "that", "this", "these", "those", "dated", "both", "between", "show", "file",
    "files", "document", "documents", "spreadsheet", "spreadsheets", "workbook", "workbooks", "sheet", "sheets",
    "xlsx", "csv", "excel", "report", "reports", "statement", "statements", "compare", "and", "the",
})
_FILENAME_TITLE_STOPWORDS = frozenset({
    "document", "documents", "doc", "file", "files", "pdf", "xlsx", "xls", "csv",
    "the", "and", "or", "with", "from", "for", "dated",
})


@dataclass(frozen=True)
class DateReference:
    year: int | None
    month: int
    day: int
    text: str

    def label(self) -> str:
        return self.text


@dataclass(frozen=True)
class CandidateDocument:
    document_id: str
    filename: str
    file_format: str
    in_scope: bool


@dataclass(frozen=True)
class ResolvedDocument:
    document_id: str
    filename: str
    matched_by: str
    date: str | None


@dataclass(frozen=True)
class DocumentConstraint:
    requested: bool
    resolved: tuple[ResolvedDocument, ...] = ()
    # Unresolved, ambiguous, or out-of-scope references; any problem means the
    # question cannot be answered as asked without clarification.
    problems: tuple[str, ...] = ()
    # True only for an explicit stable ID, filename, numbered prefix, or
    # descriptive filename title. Date-only wording is a factual target in a
    # historical question, not necessarily a document-selection request.
    explicit: bool = False


def question_dates(question: str) -> list[DateReference]:
    dates: list[DateReference] = []
    taken: list[tuple[int, int]] = []
    for pattern, numeric in _QUESTION_DATES:
        for match in pattern.finditer(question):
            if any(start < match.end() and match.start() < end for start, end in taken):
                continue
            try:
                month = int(match.group("m")) if numeric else _MONTHS[match.group("mon").casefold()]
                day, year = int(match.group("d")), match.group("y")
            except (KeyError, ValueError):
                continue
            if 1 <= month <= 12 and 1 <= day <= 31:
                taken.append((match.start(), match.end()))
                dates.append(DateReference(int(year) if year else None, month, day, match.group(0).strip()))
    return sorted(dates, key=lambda date: question.find(date.text))


def filename_dates(filename: str) -> list[tuple[int, int, int]]:
    found: list[tuple[int, int, int]] = []
    for pattern in _FILENAME_DATES:
        for match in pattern.finditer(filename):
            value = (int(match.group("y")), int(match.group("m")), int(match.group("d")))
            if value not in found:
                found.append(value)
    return found


def _format(document: CandidateDocument) -> str:
    extension = document.filename.rsplit(".", 1)[-1].casefold() if "." in document.filename else ""
    return document.file_format.casefold() if document.file_format and document.file_format != "unknown" else extension


def _tokens(text: str) -> set[str]:
    return {token for token in re.split(r"[^0-9a-zäöüß]+", text.casefold()) if token}


def _numbered_prefix(filename: str) -> int | None:
    """Return an intentional leading document number, never an embedded date."""
    match = _NUMBERED_FILENAME_PREFIX.match(filename)
    return int(match.group("number")) if match else None


def _title_words(filename: str) -> tuple[str, ...]:
    """Return meaningful filename-title words in their original order."""
    values = re.split(r"[^0-9a-zäöüß]+", filename.rsplit(".", 1)[0].casefold())
    return tuple(
        value for value in values
        if len(value) >= 3 and not value.isdigit() and value not in _MONTHS and value not in _FILENAME_TITLE_STOPWORDS
    )


def _mentions_title(question: str, title: Sequence[str]) -> bool:
    """Match a filename title as an ordered phrase, never a bag of topics.

    A question about an "assignment of legal claim rights" should not select
    a file named ``legal_rights_assignment`` merely because it contains the
    same words in a different order.  Requiring the ordered filename title
    avoids treating ordinary analytical language as a hidden file selector.
    """
    if len(title) < 2:
        return False
    pattern = r"(?<![0-9a-zäöüß])" + r"[^0-9a-zäöüß]+".join(re.escape(word) for word in title) + r"(?![0-9a-zäöüß])"
    return re.search(pattern, question.casefold()) is not None


def resolve_document_references(question: str, documents: Sequence[CandidateDocument]) -> DocumentConstraint:
    """Resolve explicit document references against every matter document.

    ``documents`` lists all documents in the matter with whether each is in
    the current authorized scope, so an out-of-scope request is reported
    instead of being silently replaced by an in-scope document.
    """
    filenames = [match.group(0) for match in _FILENAME.finditer(question)]
    question_folded = question.casefold()
    stable_ids = [document for document in documents if len(document.document_id) >= 8 and document.document_id.casefold() in question_folded]
    requested_numbers: list[int] = []
    for match in _DOCUMENT_NUMBER_RANGE.finditer(question):
        first, last = int(match.group("first")), int(match.group("last"))
        if first <= last and last - first < 100:
            requested_numbers.extend(range(first, last + 1))
    for match in _DOCUMENT_NUMBER.finditer(question):
        requested_numbers.append(int(match.group("number")))
    requested_numbers = list(dict.fromkeys(requested_numbers))
    dates = question_dates(question) if _DOCUMENT_NOUN.search(question) else []
    has_descriptive_title = not _ALL_DOCUMENTS.search(question) and any(
        _mentions_title(question, _title_words(document.filename))
        for document in documents
    )
    explicit = bool(filenames or stable_ids or requested_numbers or has_descriptive_title)
    if not explicit and not dates:
        return DocumentConstraint(False)
    resolved: list[ResolvedDocument] = []
    problems: list[str] = []

    def accept(document: CandidateDocument, matched_by: str, date: str | None) -> None:
        if not document.in_scope:
            problems.append(
                f"“{document.filename}” matches {matched_by} but is not in the current document scope; "
                "add it to the selected documents or ask about all documents."
            )
        elif document.document_id not in {item.document_id for item in resolved}:
            resolved.append(ResolvedDocument(document.document_id, document.filename, matched_by, date))

    def resolve_matches(
        matches: Sequence[CandidateDocument], *, reference: str, no_match: str, ambiguous: str, date: str | None = None,
    ) -> None:
        """Resolve only one permitted candidate; report an out-of-scope target separately."""
        in_scope = [document for document in matches if document.in_scope]
        if len(in_scope) == 1:
            accept(in_scope[0], reference, date)
        elif len(in_scope) > 1:
            problems.append(ambiguous + " (" + ", ".join(f"“{document.filename}”" for document in in_scope[:5]) + ").")
        elif matches:
            # Do not use a document outside the allowed scope merely because
            # it has a stronger-looking name, ID, or number.
            document = matches[0]
            problems.append(
                f"“{document.filename}” matches {reference} but is not in the current document scope; "
                "add it to the selected documents or ask about all documents."
            )
        else:
            problems.append(no_match)

    # Precedence 1: a stable storage identifier is explicit and can never be
    # confused with a file date, page number, or monetary amount.
    for document in stable_ids:
        resolve_matches(
            [document], reference="the document ID", no_match="", ambiguous="More than one document uses this document ID",
        )

    # Precedence 2: full filenames are exact, but still must be unique within
    # the permitted scope.
    for name in filenames:
        matches = [document for document in documents if document.filename.casefold() == name.casefold()]
        resolve_matches(
            matches,
            reference="the file name",
            no_match=f"No document named “{name}” is in this matter.",
            ambiguous=f"More than one document is named “{name}”. Select the intended one",
        )

    # Precedence 3: users may refer to a fixture or numbered document as
    # "document 04".  Match only a deliberate leading filename prefix; an
    # embedded date (for example 2025-02-28) is never a document number.
    for number in requested_numbers:
        matches = [document for document in documents if _numbered_prefix(document.filename) == number]
        resolve_matches(
            matches,
            reference=f"document {number:02d} by its numbered file prefix",
            no_match=f"No document numbered {number:02d} is in this matter.",
            ambiguous=f"More than one document has the numbered file prefix {number:02d}. Select the intended one",
        )

    # Precedence 4: resolve a descriptive title only when the meaningful
    # filename words appear as one ordered phrase in the question.  This
    # avoids treating a generic analytical topic as an implicit file choice.
    title_matches: dict[tuple[str, ...], list[CandidateDocument]] = {}
    if has_descriptive_title:
        for document in documents:
            title = _title_words(document.filename)
            if _mentions_title(question, title):
                title_matches.setdefault(title, []).append(document)
    for title, matches in title_matches.items():
        label = " ".join(title)
        resolve_matches(
            matches,
            reference="the descriptive file title",
            no_match="",
            ambiguous=f"More than one document matches the descriptive title “{label}”. Select the intended one",
        )

    # Precedence 5: a date is a last-resort reference.  Once a higher-
    # precedence reference is explicit, its date wording must not turn an
    # otherwise unambiguous request into an error.
    if dates and not resolved and not problems:
        candidates = list(documents)
        if _SPREADSHEET_NOUN.search(question):
            candidates = [document for document in candidates if _format(document) in _SPREADSHEET_FORMATS]
            kind = "spreadsheet"
        elif _PDF_NOUN.search(question):
            candidates = [document for document in candidates if _format(document) == "pdf"]
            kind = "PDF"
        else:
            kind = "document"
        months = set(_MONTHS)
        descriptors = {
            token for token in _tokens(question)
            if len(token) >= 4 and token not in _DESCRIPTOR_STOPWORDS and token not in months and not token.isdigit()
        }
        for date in dates:
            matches = [
                document for document in candidates
                if any(
                    (month, day) == (date.month, date.day) and (date.year is None or year == date.year)
                    for year, month, day in filename_dates(document.filename)
                )
            ]
            if len(matches) > 1 and descriptors:
                described = [document for document in matches if _tokens(document.filename) & descriptors]
                matches = described or matches
            if len(matches) > 1:
                in_scope = [document for document in matches if document.in_scope]
                matches = in_scope if len(in_scope) == 1 else matches
            iso = None
            if len(matches) == 1:
                year, month, day = next(
                    value for value in filename_dates(matches[0].filename)
                    if (value[1], value[2]) == (date.month, date.day)
                )
                iso = f"{year:04d}-{month:02d}-{day:02d}"
                accept(matches[0], f"the date {date.text} in its file name", iso)
            elif matches:
                problems.append(
                    f"More than one {kind} carries the date {date.text} in its file name ("
                    + ", ".join(f"“{document.filename}”" for document in matches[:5])
                    + "). Name the intended file or select it."
                )
            else:
                problems.append(
                    f"No {kind} in this matter carries the date {date.text} in its file name. "
                    "Name the file explicitly or select it; document dates are not guessed from content."
                )
    return DocumentConstraint(True, tuple(resolved), tuple(problems), explicit)
