import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, unlink, writeFile } from "node:fs/promises";
import path from "node:path";

export type AIProvider = "codex" | "openai";

const SETTINGS_FILENAME = "ai-provider.json";

export function isAIProvider(value: unknown): value is AIProvider {
  return value === "codex" || value === "openai";
}

export function selectedAIProvider(preference: AIProvider | null, environment: string | undefined): AIProvider {
  return preference ?? (isAIProvider(environment) ? environment : "codex");
}

export async function readAIProviderPreference(storageRoot: string): Promise<AIProvider | null> {
  let contents: string;
  try {
    contents = await readFile(path.join(storageRoot, SETTINGS_FILENAME), "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
    throw error;
  }
  try {
    const parsed: unknown = JSON.parse(contents);
    if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
      const provider = (parsed as Record<string, unknown>).provider;
      if (isAIProvider(provider)) return provider;
    }
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
  }
  return null;
}

export async function saveAIProviderPreference(storageRoot: string, provider: AIProvider): Promise<void> {
  if (!isAIProvider(provider)) throw new TypeError("Invalid AI provider.");
  await mkdir(storageRoot, { recursive: true, mode: 0o700 });
  const temporaryPath = path.join(storageRoot, `.ai-provider-${randomUUID()}.tmp`);
  try {
    await writeFile(temporaryPath, JSON.stringify({ provider }) + "\n", { encoding: "utf8", mode: 0o600, flag: "wx" });
    await rename(temporaryPath, path.join(storageRoot, SETTINGS_FILENAME));
  } finally {
    try { await unlink(temporaryPath); } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }
}
