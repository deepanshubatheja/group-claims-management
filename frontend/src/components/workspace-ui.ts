export const workspacePages = ["Dashboard", "Documents", "Matter Review", "Holdings & History", "Issues", "Billing", "Reports", "AI Assistant", "Settings"] as const;
export type WorkspacePage = (typeof workspacePages)[number];
export function isAvailablePage(page: WorkspacePage): boolean { return page === "Dashboard" || page === "Documents" || page === "Matter Review" || page === "Holdings & History" || page === "Issues" || page === "Billing" || page === "Settings"; }
export function recentDocuments(documents: DocumentSummary[], maximum = 5): DocumentSummary[] { return [...documents].sort((first, second) => Date.parse(second.imported_at) - Date.parse(first.imported_at)).slice(0, maximum); }
export function platformName(platform: string | undefined): string { return platform === "darwin" ? "macOS" : platform === "win32" ? "Windows" : platform || "Unavailable"; }

export function aiConnectionMessage(check: AIConnectionCheck | null): string | null {
  if (check === null) return null;
  return {
    available: "The selected AI provider accepted the connected account. No document content was sent and no model response was generated.",
    provider_not_configured: "No AI provider is configured for the last explicit connection check.",
    api_key_missing: "The last explicit connection check found no usable API key in the running backend.",
    codex_login_required: "Connect ChatGPT in Settings before checking Codex.",
    authentication_rejected: "OpenAI rejected the configured credential during the last explicit connection check.",
    permission_denied: "OpenAI accepted the request but denied permission for the last explicit connection check.",
    rate_limited: "The last explicit connection check was rate-limited.",
    network_unavailable: "The last explicit connection check could not reach OpenAI.",
    timeout: "The last explicit connection check timed out.",
    provider_error: "The AI provider could not complete the last explicit connection check. Try again later.",
  }[check.outcome];
}
export type WorkspaceNotice = "backend" | "storage";
export function workspaceNotices(backendAvailable: boolean | null, storageAvailable: boolean | null): WorkspaceNotice[] { const notices: WorkspaceNotice[] = []; if (backendAvailable === false) notices.push("backend"); if (storageAvailable === false) notices.push("storage"); return notices; }
