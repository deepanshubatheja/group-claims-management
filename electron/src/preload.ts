import { contextBridge, ipcRenderer } from "electron";
import type { AppInfo, BackendHealthResponse, StorageStatusResponse } from "./ipc-contract";

// Sandboxed preloads cannot load arbitrary local CommonJS modules at runtime.
// Keep this one allowlisted channel local while sharing the response type.
const GET_APP_INFO_CHANNEL = "app:get-info" as const;
const GET_BACKEND_HEALTH_CHANNEL = "app:get-backend-health" as const;
const GET_STORAGE_STATUS_CHANNEL = "app:get-storage-status" as const;

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

function isBackendHealthResponse(value: unknown): value is BackendHealthResponse {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }

  const candidate = value as Record<string, unknown>;
  if (candidate.ok === false) {
    return (
      typeof candidate.error === "object" &&
      candidate.error !== null &&
      typeof (candidate.error as Record<string, unknown>).code === "string" &&
      typeof (candidate.error as Record<string, unknown>).message === "string"
    );
  }

  if (candidate.ok !== true || typeof candidate.health !== "object" || candidate.health === null) {
    return false;
  }

  const health = candidate.health as Record<string, unknown>;
  const platform = health.platform;
  return (
    health.status === "ok" &&
    typeof health.applicationVersion === "string" &&
    health.applicationVersion.length > 0 &&
    typeof health.pythonVersion === "string" &&
    health.pythonVersion.length > 0 &&
    typeof platform === "object" &&
    platform !== null &&
    typeof (platform as Record<string, unknown>).system === "string" &&
    typeof (platform as Record<string, unknown>).release === "string" &&
    typeof (platform as Record<string, unknown>).machine === "string"
  );
}

function isStorageStatusResponse(value: unknown): value is StorageStatusResponse {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }

  const candidate = value as Record<string, unknown>;
  if (candidate.ok === false) {
    return (
      typeof candidate.error === "object" &&
      candidate.error !== null &&
      typeof (candidate.error as Record<string, unknown>).code === "string" &&
      typeof (candidate.error as Record<string, unknown>).message === "string"
    );
  }

  if (candidate.ok !== true || typeof candidate.storage !== "object" || candidate.storage === null) {
    return false;
  }

  const storage = candidate.storage as Record<string, unknown>;
  return (
    (storage.status === "ready" || storage.status === "error") &&
    (storage.databaseStatus === "connected" || storage.databaseStatus === "unavailable") &&
    (storage.schemaVersion === null ||
      (typeof storage.schemaVersion === "number" &&
        Number.isInteger(storage.schemaVersion) &&
        storage.schemaVersion >= 0)) &&
    typeof storage.directoriesAvailable === "boolean"
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
  async getBackendHealth(): Promise<BackendHealthResponse> {
    const response: unknown = await ipcRenderer.invoke(GET_BACKEND_HEALTH_CHANNEL);

    if (!isBackendHealthResponse(response)) {
      throw new Error("The desktop application returned invalid backend health information.");
    }

    return Object.freeze(response);
  },
  async getStorageStatus(): Promise<StorageStatusResponse> {
    const response: unknown = await ipcRenderer.invoke(GET_STORAGE_STATUS_CHANNEL);

    if (!isStorageStatusResponse(response)) {
      throw new Error("The desktop application returned invalid storage status information.");
    }

    return Object.freeze(response);
  },
});

contextBridge.exposeInMainWorld("desktopAPI", desktopApi);
