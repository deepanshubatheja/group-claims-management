export const GET_APP_INFO_CHANNEL = "app:get-info" as const;

export interface AppInfo {
  name: string;
  version: string;
  platform: NodeJS.Platform;
}
