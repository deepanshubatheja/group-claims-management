/// <reference types="vite/client" />

interface AppInfo {
  name: string;
  version: string;
  platform: string;
}

interface DesktopAPI {
  getAppInfo(): Promise<AppInfo>;
}

interface Window {
  desktopAPI: DesktopAPI;
}
