/// <reference types="vite/client" />

interface AppInfo {
  name: string;
  version: string;
  platform: string;
}

interface BackendHealth {
  status: "ok";
  applicationVersion: string;
  pythonVersion: string;
  platform: {
    system: string;
    release: string;
    machine: string;
  };
}

type BackendHealthResponse =
  | { ok: true; health: BackendHealth }
  | { ok: false; error: { code: string; message: string } };

interface StorageStatus {
  status: "ready" | "error";
  databaseStatus: "connected" | "unavailable";
  schemaVersion: number | null;
  directoriesAvailable: boolean;
}

type StorageStatusResponse =
  | { ok: true; storage: StorageStatus }
  | { ok: false; error: { code: string; message: string } };

interface DesktopAPI {
  getAppInfo(): Promise<AppInfo>;
  getBackendHealth(): Promise<BackendHealthResponse>;
  getStorageStatus(): Promise<StorageStatusResponse>;
}

interface Window {
  desktopAPI: DesktopAPI;
}
