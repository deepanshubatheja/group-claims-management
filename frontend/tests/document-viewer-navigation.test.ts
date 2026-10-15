import assert from "node:assert/strict";
import test from "node:test";
import {
  nextPdfPage,
  previousPdfPage,
  evidenceTarget,
  spreadsheetRequest,
} from "../src/components/document-viewer-navigation.js";
import { loadDocumentPreview } from "../src/components/document-viewer-client.js";

const parsedWorkbook: DocumentContentResponse = {
  ok: true,
  document: { id: "document-1", original_filename: "holdings.xlsx", file_size_bytes: 64, imported_at: "2026-01-01T00:00:00Z", file_format: "xlsx", processing_status: "parsed", processing_error: null, ai_review_status: "new" },
  contentType: "spreadsheet",
  worksheets: [{ name: "Holdings", maxRow: 30, maxColumn: 10 }],
  grid: { sheetName: "Holdings", rowStart: 1, rowLimit: 10, columnStart: 1, columnLimit: 10, cells: [{ source_reference: "Holdings!A1", value_text: "000123" }] },
  references: [],
  hasMore: true,
};

test("loads a selected parsed workbook through mocked typed IPC", async () => {
  const requests: Array<{ id: string; options: DocumentContentOptions | undefined }> = [];
  const result = await loadDocumentPreview({
    getDocumentContent: async (id, options) => {
      requests.push({ id, options });
      return parsedWorkbook;
    },
  }, "document-1", spreadsheetRequest("Holdings", 1, 1));

  assert.equal(result.state, "ready");
  assert.deepEqual(requests, [{ id: "document-1", options: spreadsheetRequest("Holdings", 1, 1) }]);
  if (result.state === "ready") assert.equal(result.response.grid?.sheetName, "Holdings");
});

test("shows a controlled viewer error when typed IPC retrieval fails", async () => {
  const result = await loadDocumentPreview({
    getDocumentContent: async () => ({ ok: false, error: { code: "BACKEND_UNAVAILABLE", message: "Preview service unavailable." } }),
  }, "document-1");

  assert.deepEqual(result, { state: "error", message: "Preview service unavailable." });
});

test("navigates every source page of a five-page PDF", () => {
  assert.equal(previousPdfPage({ pageNumber: 1, pageCount: 5, hasPrevious: false, hasNext: true }), null);
  assert.equal(nextPdfPage({ pageNumber: 1, pageCount: 5, hasPrevious: false, hasNext: true }), 2);
  assert.equal(previousPdfPage({ pageNumber: 2, pageCount: 5, hasPrevious: true, hasNext: true }), 1);
  assert.equal(nextPdfPage({ pageNumber: 4, pageCount: 5, hasPrevious: true, hasNext: true }), 5);
  assert.equal(nextPdfPage({ pageNumber: 5, pageCount: 5, hasPrevious: true, hasNext: false }), null);
  assert.equal(previousPdfPage({ pageNumber: 4, pageCount: 2, hasPrevious: true, hasNext: true, previousPageNumber: 1, nextPageNumber: 9 }), 1);
  assert.equal(nextPdfPage({ pageNumber: 4, pageCount: 2, hasPrevious: true, hasNext: true, previousPageNumber: 1, nextPageNumber: 9 }), 9);
});

test("disables both PDF controls for a single source page", () => {
  const singlePage = { pageNumber: 1, pageCount: 1, hasPrevious: false, hasNext: false };
  assert.equal(previousPdfPage(singlePage), null);
  assert.equal(nextPdfPage(singlePage), null);
});

test("creates bounded coordinate requests for worksheet switching and navigation", () => {
  assert.deepEqual(spreadsheetRequest("Holdings", 1, 1), {
    sheetName: "Holdings", rowStart: 1, rowLimit: 10, columnStart: 1, columnLimit: 10,
  });
  assert.deepEqual(spreadsheetRequest("Notes", 21, 11), {
    sheetName: "Notes", rowStart: 21, rowLimit: 10, columnStart: 11, columnLimit: 10,
  });
});

test("parses only canonical AI evidence references for local navigation", () => {
  assert.deepEqual(evidenceTarget("page 1 block 3"), { kind: "pdf", pageNumber: 1, blockIndex: 3 });
  assert.deepEqual(evidenceTarget("page 5"), { kind: "pdf", pageNumber: 5, blockIndex: null });
  assert.deepEqual(evidenceTarget("Holdings!AA21"), { kind: "spreadsheet", sheetName: "Holdings", rowNumber: 21, columnNumber: 27 });
  assert.equal(evidenceTarget("page 0 block 1"), null);
  assert.equal(evidenceTarget("file:///private-document"), null);
});
