import assert from "node:assert/strict";
import test from "node:test";
import { CredentialStoreError, MacOSCredentialStore, type CredentialHelperRunner } from "../src/credential-store";

test("saves, reloads, replaces, and removes a credential without returning it from save operations", async () => {
  let stored: string | null = null;
  const runner: CredentialHelperRunner = async (_path, request) => {
    if (request.operation === "read") return stored === null ? { status: "missing" } : { status: "found", value: stored };
    if (request.operation === "write") { stored = request.value ?? null; return { status: "ok" }; }
    stored = null;
    return { status: "ok" };
  };
  const first = new MacOSCredentialStore("/trusted/helper", runner, "darwin");
  await first.save(" first-synthetic-key ");
  assert.equal(await new MacOSCredentialStore("/trusted/helper", runner, "darwin").read(), "first-synthetic-key");
  await first.save("replacement-synthetic-key");
  assert.equal(await first.read(), "replacement-synthetic-key");
  await first.remove();
  assert.equal(await first.read(), null);
});

test("rejects invalid credentials and helper failures without including secret text", async () => {
  const runner: CredentialHelperRunner = async () => ({ status: "error", code: "keychain_unavailable" });
  const store = new MacOSCredentialStore("/trusted/helper", runner, "darwin");
  await assert.rejects(store.save("   "), (error: unknown) => error instanceof CredentialStoreError && error.code === "INVALID_CREDENTIAL");
  await assert.rejects(store.save("secret-that-must-not-appear"), (error: unknown) => {
    assert.ok(error instanceof CredentialStoreError);
    assert.equal(error.message.includes("secret-that-must-not-appear"), false);
    return true;
  });
});

test("does not attempt a platform credential operation when Keychain is unavailable", async () => {
  const store = new MacOSCredentialStore(undefined, async () => ({ status: "ok" }), "darwin");
  await assert.rejects(store.read(), (error: unknown) => error instanceof CredentialStoreError && error.code === "CREDENTIAL_STORE_UNAVAILABLE");
});
