import BackendStatus from "./BackendStatus";
import StorageStatus from "./StorageStatus";
import { platformName } from "./workspace-ui";

export default function SettingsScreen({ appInfo, desktopState }: { appInfo: AppInfo | null; desktopState: "connecting" | "connected" | "error" }) {
  return <><header className="page-header"><div><p className="eyebrow">Settings</p><h1>Settings</h1></div></header><section className="settings-section"><div className="settings-card"><h2>About</h2><p>{appInfo?.name ?? "Group Claims Management"} · Version {appInfo?.version ?? "Unavailable"}</p><p>Operating system: {platformName(appInfo?.platform)}</p></div></section><section className="settings-section"><div className="settings-card"><h2>Diagnostics</h2><p>Desktop environment: {desktopState}</p><BackendStatus /><StorageStatus /></div></section></>;
}
