import assert from "node:assert/strict";
import { mkdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { discoverDroppedImportableFiles, discoverImportableFiles } from "../src/folder-import";

test("discovers supported nested files while skipping links and unsupported files", async () => {
  const root = path.join(os.tmpdir(), `group-claims-folder-${Date.now()}`);
  mkdirSync(path.join(root, "nested"), { recursive: true });
  writeFileSync(path.join(root, "one.csv"), "id\n1\n");
  writeFileSync(path.join(root, "nested", "two.pdf"), "%PDF-synthetic");
  writeFileSync(path.join(root, "ignored.txt"), "ignore");
  try { symlinkSync(path.join(root, "one.csv"), path.join(root, "linked.csv")); } catch { /* Some Windows test hosts do not allow symlinks. */ }
  try {
    const result = await discoverImportableFiles(root);
    assert.deepEqual(result.files.map((file) => file.relativePath).sort(), ["nested/two.pdf", "one.csv"]);
    assert.ok(result.skippedFiles >= 1);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("creates one bounded import plan for dropped files and folders without duplicating nested files", async () => {
  const root = path.join(os.tmpdir(), `group-claims-drop-${Date.now()}`);
  const folder = path.join(root, "folder");
  const direct = path.join(root, "direct.csv");
  const nested = path.join(folder, "nested.pdf");
  mkdirSync(folder, { recursive: true });
  writeFileSync(direct, "account\nA1\n");
  writeFileSync(nested, "%PDF-synthetic");
  try {
    const result = await discoverDroppedImportableFiles([direct, folder, nested, path.join(root, "not-supported.txt")]);
    assert.deepEqual(result.files.map((file) => file.relativePath).sort(), ["direct.csv", "folder/nested.pdf"]);
    assert.equal(result.files.length, 2);
    assert.ok(result.skippedFiles >= 1);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
