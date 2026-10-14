import assert from "node:assert/strict";
import test from "node:test";
import { isAvailablePage, platformName, recentDocuments, workspaceNotices } from "../src/components/workspace-ui.js";

const document = (id: string, importedAt: string): DocumentSummary => ({ id, original_filename: `${id}.pdf`, file_size_bytes: 1, imported_at: importedAt, file_format: "pdf", processing_status: "parsed", processing_error: null });

test("uses persisted document metadata for recent documents", () => {
  assert.deepEqual(recentDocuments([document("old", "2025-01-01T00:00:00Z"), document("new", "2026-01-01T00:00:00Z")]).map((item) => item.id), ["new", "old"]);
});
test("keeps unavailable workspace areas disabled and settings available", () => {
  assert.equal(isAvailablePage("Settings"), true);
  assert.equal(isAvailablePage("Holdings"), false);
});
test("uses friendly platform labels and only genuine failure notices", () => {
  assert.equal(platformName("darwin"), "macOS");
  assert.deepEqual(workspaceNotices(true, true), []);
  assert.deepEqual(workspaceNotices(false, false), ["backend", "storage"]);
});
