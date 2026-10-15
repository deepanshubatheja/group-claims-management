export interface PdfPagination {
  pageNumber: number;
  pageCount: number;
  hasPrevious: boolean;
  hasNext: boolean;
  previousPageNumber?: number;
  nextPageNumber?: number;
}

export type EvidenceTarget =
  | { kind: "pdf"; pageNumber: number; blockIndex: number | null }
  | { kind: "spreadsheet"; sheetName: string; rowNumber: number; columnNumber: number }
  | null;

function columnNumber(name: string): number {
  return [...name].reduce((total, character) => total * 26 + character.charCodeAt(0) - 64, 0);
}

/** Parse only canonical, backend-validated evidence references for local navigation. */
export function evidenceTarget(sourceReference: string): EvidenceTarget {
  const pdf = /^page\s+([1-9][0-9]*)(?:\s+block\s+([0-9]+))?$/i.exec(sourceReference);
  if (pdf) {
    return { kind: "pdf", pageNumber: Number(pdf[1]), blockIndex: pdf[2] === undefined ? null : Number(pdf[2]) };
  }
  const spreadsheet = /^(.+)!([A-Z]+)([1-9][0-9]*)$/i.exec(sourceReference);
  if (spreadsheet) {
    return {
      kind: "spreadsheet",
      sheetName: spreadsheet[1],
      columnNumber: columnNumber(spreadsheet[2].toUpperCase()),
      rowNumber: Number(spreadsheet[3]),
    };
  }
  return null;
}

export function previousPdfPage(pagination: PdfPagination): number | null {
  if (!pagination.hasPrevious) return null;
  return pagination.previousPageNumber ?? (pagination.pageNumber > 1 ? pagination.pageNumber - 1 : null);
}

export function nextPdfPage(pagination: PdfPagination): number | null {
  if (!pagination.hasNext) return null;
  return pagination.nextPageNumber ?? (pagination.pageNumber < pagination.pageCount ? pagination.pageNumber + 1 : null);
}

export function spreadsheetRequest(sheetName: string, rowStart: number, columnStart: number) {
  return { sheetName, rowStart, rowLimit: 10, columnStart, columnLimit: 10 };
}
