export const GET_APP_INFO_CHANNEL = "app:get-info" as const;
export const GET_BACKEND_HEALTH_CHANNEL = "app:get-backend-health" as const;
export const GET_STORAGE_STATUS_CHANNEL = "app:get-storage-status" as const;

export interface AppInfo {
  name: string;
  version: string;
  platform: NodeJS.Platform;
}

export interface BackendHealth {
  status: "ok";
  applicationVersion: string;
  pythonVersion: string;
  platform: {
    system: string;
    release: string;
    machine: string;
  };
}

export interface BackendHealthFailure {
  ok: false;
  error: {
    code: string;
    message: string;
  };
}

export interface BackendHealthSuccess {
  ok: true;
  health: BackendHealth;
}

export type BackendHealthResponse = BackendHealthSuccess | BackendHealthFailure;

export interface StorageStatus {
  status: "ready" | "error";
  databaseStatus: "connected" | "unavailable";
  schemaVersion: number | null;
  directoriesAvailable: boolean;
}

export interface StorageStatusFailure {
  ok: false;
  error: {
    code: string;
    message: string;
  };
}

export interface StorageStatusSuccess {
  ok: true;
  storage: StorageStatus;
}

export type StorageStatusResponse = StorageStatusSuccess | StorageStatusFailure;
