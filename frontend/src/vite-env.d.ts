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

interface DesktopAPI {
  getAppInfo(): Promise<AppInfo>;
  getBackendHealth(): Promise<BackendHealthResponse>;
}

interface Window {
  desktopAPI: DesktopAPI;
}
