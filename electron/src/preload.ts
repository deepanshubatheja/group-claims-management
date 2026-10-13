import { contextBridge, ipcRenderer } from "electron";
import type { AppInfo } from "./ipc-contract";

// Sandboxed preloads cannot load arbitrary local CommonJS modules at runtime.
// Keep this one allowlisted channel local while sharing the response type.
const GET_APP_INFO_CHANNEL = "app:get-info" as const;

function isAppInfo(value: unknown): value is AppInfo {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }

  const candidate = value as Record<string, unknown>;

  return (
    typeof candidate.name === "string" &&
    candidate.name.length > 0 &&
    typeof candidate.version === "string" &&
    candidate.version.length > 0 &&
    (candidate.platform === "darwin" ||
      candidate.platform === "win32" ||
      candidate.platform === "linux")
  );
}

const desktopApi = Object.freeze({
  async getAppInfo(): Promise<AppInfo> {
    const response: unknown = await ipcRenderer.invoke(GET_APP_INFO_CHANNEL);

    if (!isAppInfo(response)) {
      throw new Error("The desktop application returned invalid application information.");
    }

    return Object.freeze({ ...response });
  },
});

contextBridge.exposeInMainWorld("desktopAPI", desktopApi);
