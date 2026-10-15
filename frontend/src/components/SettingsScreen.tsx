import { useEffect, useState } from "react";
import BackendStatus from "./BackendStatus";
import StorageStatus from "./StorageStatus";
import { aiConnectionMessage, platformName } from "./workspace-ui";

export default function SettingsScreen({ appInfo, desktopState }: { appInfo: AppInfo | null; desktopState: "connecting" | "connected" | "error" }) {
  const [ai, setAI] = useState<AIStatus | null>(null);
  const [checkingConnection, setCheckingConnection] = useState(false);
  const [updatingAuthorization, setUpdatingAuthorization] = useState(false);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const refreshAIStatus = () => {
    void window.desktopAPI.getAIStatus().then((response) => {
      if (response.ok) setAI(response.ai);
    });
  };
  const checkConnection = () => {
    if (checkingConnection) return;
    setCheckingConnection(true); setConnectionError(null);
    void window.desktopAPI.checkAIConnection().then((response) => {
      if (!response.ok) setConnectionError(response.error.message);
      refreshAIStatus();
    }).catch(() => setConnectionError("The connection check could not be completed."))
      .finally(() => setCheckingConnection(false));
  };
  const updateAuthorization = () => {
    if (updatingAuthorization) return;
    setUpdatingAuthorization(true);
    setConnectionError(null);
    const authorized = !(ai?.authorized ?? false);
    void window.desktopAPI.setAIAuthorization(authorized).then((response) => {
      if (response.ok) {
        setAI(response.ai);
      } else {
        // This is deliberately a controlled IPC error.  Never leave the
        // user guessing whether consent was recorded locally.
        setConnectionError(response.error.message);
      }
    }).catch(() => setConnectionError("Cloud-processing authorization could not be updated."))
      .finally(() => {
        setUpdatingAuthorization(false);
        refreshAIStatus();
      });
  };
  useEffect(() => { refreshAIStatus(); }, []);
  const lastCheck = aiConnectionMessage(ai?.lastConnectionCheck ?? null);
  return <>
    <header className="page-header"><div><p className="eyebrow">Settings</p><h1>Settings</h1></div></header>
    <section className="settings-section"><div className="settings-card"><h2>About</h2><p>{appInfo?.name ?? "Group Claims Management"} · Version {appInfo?.version ?? "Unavailable"}</p><p>Operating system: {platformName(appInfo?.platform)}</p></div></section>
    <section className="settings-section"><div className="settings-card"><h2>AI Processing</h2><p>Cloud AI is off by default. No document content is sent unless you explicitly authorize a document-level request.</p><p>{ai?.message ?? "Checking AI configuration…"}</p><p>Provider: {ai?.provider ?? "Not configured"} · Model: {ai?.model ?? "Not configured"}</p><p>Backend API key detection: {ai === null ? "Checking…" : ai.credentialPresent ? "Key present in this running backend" : "No key detected"}. The key itself is never displayed, stored, or sent through the app.</p><button className="secondary-button" type="button" onClick={refreshAIStatus}>Refresh AI configuration</button><button className="secondary-button" type="button" onClick={checkConnection} disabled={checkingConnection}>{checkingConnection ? "Checking OpenAI connection…" : "Check API Connection"}</button><button className="secondary-button" type="button" onClick={updateAuthorization} disabled={updatingAuthorization || ai === null}>{updatingAuthorization ? "Updating authorization…" : ai?.authorized ? "Withdraw cloud-processing authorization" : "Authorize cloud-processing preparation"}</button>{ai?.authorized && <p className="ai-connection-result">Cloud-processing preparation is authorized locally. Each document analysis or question still needs its own explicit authorization.</p>}{lastCheck && <p className="ai-connection-result">Last explicit API check: {lastCheck}</p>}{connectionError && <p className="viewer-error" role="alert">{connectionError}</p>}<p className="settings-detail">Changing cloud-processing authorization is local only: it does not contact OpenAI, send a document, or generate a response. The connection check uses OpenAI’s Models metadata endpoint only when you click it.</p></div></section>
    <section className="settings-section"><div className="settings-card"><h2>Diagnostics</h2><p>Desktop environment: {desktopState}</p><BackendStatus /><StorageStatus /></div></section>
  </>;
}
