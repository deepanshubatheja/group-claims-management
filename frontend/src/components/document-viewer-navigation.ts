export interface PdfPagination {
  pageNumber: number;
  pageCount: number;
  hasPrevious: boolean;
  hasNext: boolean;
  previousPageNumber?: number;
  nextPageNumber?: number;
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
