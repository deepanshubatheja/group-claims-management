import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { isAIProvider, readAIProviderPreference, saveAIProviderPreference, selectedAIProvider } from "../src/ai-provider-settings";

test("defaults to Codex, accepts an environment fallback, and prefers a saved choice", () => {
  assert.equal(selectedAIProvider(null, undefined), "codex");
  assert.equal(selectedAIProvider(null, "openai"), "openai");
  assert.equal(selectedAIProvider("codex", "openai"), "codex");
  assert.equal(selectedAIProvider(null, "invalid"), "codex");
  assert.equal(isAIProvider("other"), false);
});

test("persists only a validated provider in the local app-data directory", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "claimflow-provider-"));
  try {
    assert.equal(await readAIProviderPreference(root), null);
    await saveAIProviderPreference(root, "openai");
    assert.equal(await readAIProviderPreference(root), "openai");
    assert.deepEqual(JSON.parse(await readFile(path.join(root, "ai-provider.json"), "utf8")), { provider: "openai" });
    await saveAIProviderPreference(root, "codex");
    assert.equal(await readAIProviderPreference(root), "codex");
    await assert.rejects(saveAIProviderPreference(root, "other" as "codex"), TypeError);
    assert.equal(await readAIProviderPreference(root), "codex");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("ignores a malformed preference instead of trusting an unknown provider", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "claimflow-provider-"));
  try {
    await writeFile(path.join(root, "ai-provider.json"), '{"provider":"unknown"}');
    assert.equal(await readAIProviderPreference(root), null);
    await writeFile(path.join(root, "ai-provider.json"), "{not-json}");
    assert.equal(await readAIProviderPreference(root), null);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
