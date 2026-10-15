import { spawn } from "node:child_process";

export type CredentialOperation = "read" | "write" | "remove";

export interface CredentialHelperResponse {
  status: "found" | "missing" | "ok" | "error";
  value?: string;
  code?: string;
}

export type CredentialHelperRunner = (
  helperPath: string,
  request: { operation: CredentialOperation; value?: string },
) => Promise<CredentialHelperResponse>;

export class CredentialStoreError extends Error {
  public constructor(public readonly code: "CREDENTIAL_STORE_UNAVAILABLE" | "INVALID_CREDENTIAL") {
    super(
      code === "INVALID_CREDENTIAL"
        ? "Enter a valid OpenAI API key."
        : "macOS Keychain is unavailable. Your API key was not changed.",
    );
  }
}

function normalizeCredential(value: string): string {
  const normalized = value.trim();
  if (!normalized || normalized.length > 1024) {
    throw new CredentialStoreError("INVALID_CREDENTIAL");
  }
  return normalized;
}

function parseResponse(value: string): CredentialHelperResponse {
  try {
    const parsed: unknown = JSON.parse(value);
    if (
      typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
      && ["found", "missing", "ok", "error"].includes(String((parsed as Record<string, unknown>).status))
      && ((parsed as Record<string, unknown>).value === undefined || typeof (parsed as Record<string, unknown>).value === "string")
      && ((parsed as Record<string, unknown>).code === undefined || typeof (parsed as Record<string, unknown>).code === "string")
    ) {
      return parsed as CredentialHelperResponse;
    }
  } catch {
    // Keychain helper output is deliberately not included in an error.
  }
  throw new CredentialStoreError("CREDENTIAL_STORE_UNAVAILABLE");
}

export const runCredentialHelper: CredentialHelperRunner = (helperPath, request) =>
  new Promise((resolve, reject) => {
    const child = spawn(helperPath, [], { stdio: ["pipe", "pipe", "ignore"], shell: false, windowsHide: true });
    let output = "";
    let settled = false;
    const fail = () => {
      if (!settled) {
        settled = true;
        reject(new CredentialStoreError("CREDENTIAL_STORE_UNAVAILABLE"));
      }
    };
    const timer = setTimeout(() => {
      child.kill();
      fail();
    }, 5_000);
    child.once("error", fail);
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => { output += chunk; });
    child.once("exit", (code) => {
      clearTimeout(timer);
      if (settled || code !== 0) return fail();
      try {
        settled = true;
        resolve(parseResponse(output.trim()));
      } catch (error) {
        settled = true;
        reject(error);
      }
    });
    child.stdin.end(JSON.stringify(request));
  });

/** macOS-only Keychain adapter. It never returns a stored secret to the renderer. */
export class MacOSCredentialStore {
  public constructor(
    private readonly helperPath: string | undefined,
    private readonly runner: CredentialHelperRunner = runCredentialHelper,
    private readonly platform: NodeJS.Platform = process.platform,
  ) {}

  public async read(): Promise<string | null> {
    const result = await this.invoke({ operation: "read" });
    if (result.status === "missing") return null;
    if (result.status !== "found" || typeof result.value !== "string") {
      throw new CredentialStoreError("CREDENTIAL_STORE_UNAVAILABLE");
    }
    return normalizeCredential(result.value);
  }

  public async save(value: string): Promise<void> {
    const result = await this.invoke({ operation: "write", value: normalizeCredential(value) });
    if (result.status !== "ok") throw new CredentialStoreError("CREDENTIAL_STORE_UNAVAILABLE");
  }

  public async remove(): Promise<void> {
    const result = await this.invoke({ operation: "remove" });
    if (result.status !== "ok") throw new CredentialStoreError("CREDENTIAL_STORE_UNAVAILABLE");
  }

  private async invoke(request: { operation: CredentialOperation; value?: string }): Promise<CredentialHelperResponse> {
    if (this.platform !== "darwin" || !this.helperPath) {
      throw new CredentialStoreError("CREDENTIAL_STORE_UNAVAILABLE");
    }
    try {
      const result = await this.runner(this.helperPath, request);
      if (result.status === "error") throw new CredentialStoreError("CREDENTIAL_STORE_UNAVAILABLE");
      return result;
    } catch (error) {
      if (error instanceof CredentialStoreError) throw error;
      throw new CredentialStoreError("CREDENTIAL_STORE_UNAVAILABLE");
    }
  }
}
