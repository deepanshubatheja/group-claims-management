"""Resolve documents a question names explicitly (by file name, date, or type).

Question-level references only ever *narrow* the already-authorized scope.
A date or type must be supported by stored metadata (the file name and
format); nothing is guessed from content, and ambiguity is reported for the
user to clarify instead of choosing a document.

A date narrows the scope only when the question attaches it to a document
("the spreadsheet dated March 28", "the March 28 and April 30 holdings
spreadsheets").  A date the facts are asked about ("holdings as of
28 February 2025") is not a document reference, and neither is a
descriptive phrase such as "the opening statement" or "the transfer
notice".  When a question also describes other documents, a dated
reference is one document among several, not a restriction.
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
_NOUN = r"(?:spreadsheets?|workbooks?|excel|xlsx|xls|csv|files?|documents?|statements?|reports?|pdfs?|sheets?|registers?)"
# Words joining dates in one reference: "March 28 and April 30", "28.03.2023, 30.04.2023".
_DATE_JOIN = re.compile(r"\s*(?:,|and|or|&|to|vs\.?|versus)?\s*(?:the\s+)?", re.IGNORECASE)
# "<document> dated|from|of|for <date>" — the text just before the dates.
_NOUN_BEFORE = re.compile(rf"\b{_NOUN}\s+(?:dated|from|of|for)\s+(?:the\s+)?$", re.IGNORECASE)
# "<date> [up to two describing words] <document>" — the text just after the dates.
_NOUN_AFTER = re.compile(
    r"^(?:\s+(?!(?:using|use|with|from|in|by|per|to|of|for|and|or|the|a|an|its|their|this|that|these|those)\b)"
    rf"[A-Za-z][\w\-]*){{0,2}}\s+{_NOUN}\b",
    re.IGNORECASE,
)
# A date the facts are stated at, never a document's own date.
_FACT_DATE_BEFORE = re.compile(r"\b(?:as\s+(?:of|at)|effective(?:\s+(?:as\s+of|from|on))?)\s+(?:the\s+)?$", re.IGNORECASE)
# Nouns that describe another document ("the opening statement", "the transfer notice").
_OTHER_DOCUMENT = re.compile(
    r"\b(?:spreadsheets?|workbooks?|statements?|reports?|pdfs?|notices?|instructions?|letters?|confirmations?)\b",
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


def _date_spans(question: str) -> list[tuple[int, int, DateReference]]:
    spans: list[tuple[int, int, DateReference]] = []
    for pattern, numeric in _QUESTION_DATES:
        for match in pattern.finditer(question):
            if any(start < match.end() and match.start() < end for start, end, _date in spans):
                continue
            try:
                month = int(match.group("m")) if numeric else _MONTHS[match.group("mon").casefold()]
                day, year = int(match.group("d")), match.group("y")
            except (KeyError, ValueError):
                continue
            if 1 <= month <= 12 and 1 <= day <= 31:
                spans.append((match.start(), match.end(), DateReference(int(year) if year else None, month, day, match.group(0).strip())))
    return sorted(spans, key=lambda span: span[0])


def question_dates(question: str) -> list[DateReference]:
    return [date for _start, _end, date in _date_spans(question)]


def document_date_references(question: str) -> tuple[list[DateReference], list[tuple[int, int]]]:
    """Dates the question attaches to a document, and the text spans of those references.

    Adjacent dates joined by "and", "or" or a comma form one reference
    ("the March 28 and April 30 holdings spreadsheets").  A group counts
    only when a document noun directly precedes it with "dated", "from",
    "of" or "for", or follows it within two describing words.  A group
    introduced by "as of", "as at" or "effective" is a fact date.
    """
    groups: list[list[tuple[int, int, DateReference]]] = []
    for span in _date_spans(question):
        if groups and _DATE_JOIN.fullmatch(question[groups[-1][-1][1]:span[0]]):
            groups[-1].append(span)
        else:
            groups.append([span])
    dates: list[DateReference] = []
    spans: list[tuple[int, int]] = []
    for group in groups:
        start, end = group[0][0], group[-1][1]
        before, after = question[:start], question[end:]
        if _FACT_DATE_BEFORE.search(before):
            continue
        preceding = _NOUN_BEFORE.search(before)
        following = _NOUN_AFTER.match(after)
        if preceding is None and following is None:
            continue
        dates.extend(date for _start, _end, date in group)
        spans.append((preceding.start() if preceding else start, end + (following.end() if following else 0)))
    return dates, spans


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


def resolve_document_references(question: str, documents: Sequence[CandidateDocument]) -> DocumentConstraint:
    """Resolve explicit document references against every matter document.

    ``documents`` lists all documents in the matter with whether each is in
    the current authorized scope, so an out-of-scope request is reported
    instead of being silently replaced by an in-scope document.
    """
    filenames = [match.group(0) for match in _FILENAME.finditer(question)]
    dates, reference_spans = document_date_references(question)
    if dates and not filenames:
        # "Compare the 28 February statement with the transfer notice" names
        # one document among others it only describes: that is a mention,
        # not a request to use the dated document alone.
        outside = "".join(
            question[previous_end:start] + " "
            for previous_end, start in zip([0, *(end for _start, end in reference_spans)], [start for start, _end in reference_spans] + [len(question)])
        )
        if _OTHER_DOCUMENT.search(outside):
            dates = []
    if not filenames and not dates:
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

    for name in filenames:
        matches = [document for document in documents if document.filename.casefold() == name.casefold()]
        if len(matches) == 1:
            accept(matches[0], "the file name", None)
        elif matches:
            problems.append(f"More than one document is named “{name}”. Select the intended one.")
        else:
            problems.append(f"No document named “{name}” is in this matter.")

    if dates:
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
    return DocumentConstraint(True, tuple(resolved), tuple(problems))
