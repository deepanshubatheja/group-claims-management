export const workspacePages = ["Dashboard", "Documents", "Holdings", "Discrepancies", "Billing", "Reports", "AI Assistant", "Settings"] as const;
export type WorkspacePage = (typeof workspacePages)[number];
export function isAvailablePage(page: WorkspacePage): boolean { return page === "Dashboard" || page === "Documents" || page === "Settings"; }
export function recentDocuments(documents: DocumentSummary[], maximum = 5): DocumentSummary[] { return [...documents].sort((first, second) => Date.parse(second.imported_at) - Date.parse(first.imported_at)).slice(0, maximum); }
export function platformName(platform: string | undefined): string { return platform === "darwin" ? "macOS" : platform === "win32" ? "Windows" : platform || "Unavailable"; }
export type WorkspaceNotice = "backend" | "storage";
export function workspaceNotices(backendAvailable: boolean | null, storageAvailable: boolean | null): WorkspaceNotice[] { const notices: WorkspaceNotice[] = []; if (backendAvailable === false) notices.push("backend"); if (storageAvailable === false) notices.push("storage"); return notices; }
