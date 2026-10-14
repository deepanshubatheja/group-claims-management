import assert from "node:assert/strict";
import { mkdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { discoverImportableFiles } from "../src/folder-import";

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
