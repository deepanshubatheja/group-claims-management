import { useEffect, useState } from "react";
import BackendStatus from "./BackendStatus";
import StorageStatus from "./StorageStatus";
import { aiConnectionMessage, platformName } from "./workspace-ui";

export default function SettingsScreen({ appInfo, desktopState }: { appInfo: AppInfo | null; desktopState: "connecting" | "connected" | "error" }) {
  const [ai, setAI] = useState<AIStatus | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [savingKey, setSavingKey] = useState(false);
  const [removingKey, setRemovingKey] = useState(false);
  const [checkingConnection, setCheckingConnection] = useState(false);
  const [updatingAuthorization, setUpdatingAuthorization] = useState(false);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [credentialNotice, setCredentialNotice] = useState<string | null>(null);
  const refreshAIStatus = () => { void window.desktopAPI.getAIStatus().then((response) => { if (response.ok) setAI(response.ai); }); };
  const saveKey = () => {
    if (savingKey || !apiKey.trim()) return;
    setSavingKey(true); setConnectionError(null); setCredentialNotice(null);
    void window.desktopAPI.saveAIApiKey(apiKey).then((response) => {
      if (response.ok) { setApiKey(""); setCredentialNotice(response.message); refreshAIStatus(); }
      else setConnectionError(response.error.message);
    }).catch(() => setConnectionError("The API key could not be saved.")).finally(() => setSavingKey(false));
  };
  const removeKey = () => {
    if (removingKey) return;
    setRemovingKey(true); setConnectionError(null); setCredentialNotice(null);
    void window.desktopAPI.removeAIApiKey().then((response) => {
      if (response.ok) { setApiKey(""); setCredentialNotice(response.message); refreshAIStatus(); }
      else setConnectionError(response.error.message);
    }).catch(() => setConnectionError("The stored API key could not be removed.")).finally(() => setRemovingKey(false));
  };
  const checkConnection = () => {
    if (checkingConnection) return;
    setCheckingConnection(true); setConnectionError(null);
    void window.desktopAPI.checkAIConnection().then((response) => { if (!response.ok) setConnectionError(response.error.message); refreshAIStatus(); }).catch(() => setConnectionError("The connection check could not be completed.")).finally(() => setCheckingConnection(false));
  };
  const updateAuthorization = () => {
    if (updatingAuthorization) return;
    setUpdatingAuthorization(true); setConnectionError(null);
    void window.desktopAPI.setAIAuthorization(!(ai?.authorized ?? false)).then((response) => { if (response.ok) setAI(response.ai); else setConnectionError(response.error.message); }).catch(() => setConnectionError("Cloud-processing authorization could not be updated.")).finally(() => { setUpdatingAuthorization(false); refreshAIStatus(); });
  };
  useEffect(() => { refreshAIStatus(); }, []);
  const lastCheck = aiConnectionMessage(ai?.lastConnectionCheck ?? null);
  const hasStoredKey = ai?.credentialPresent === true;
  return <>
    <header className="page-header"><div><p className="eyebrow">Settings</p><h1>Settings</h1></div></header>
    <section className="settings-section"><div className="settings-card"><h2>About</h2><p>{appInfo?.name ?? "ClaimFlow"} · Version {appInfo?.version ?? "Unavailable"}</p><p>Operating system: {platformName(appInfo?.platform)}</p></div></section>
    <section className="settings-section"><div className="settings-card"><h2>AI Connection</h2><p>Save your OpenAI API key in macOS Keychain. ClaimFlow never displays the saved key and never sends document content without an explicit authorization.</p><label htmlFor="openai-api-key">{hasStoredKey ? "Replace API key" : "OpenAI API key"}</label><div className="settings-key-row"><input id="openai-api-key" type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={hasStoredKey ? "Enter a replacement key" : "Paste API key"} /><button className="secondary-button" type="button" onClick={saveKey} disabled={savingKey || !apiKey.trim()}>{savingKey ? "Saving…" : hasStoredKey ? "Replace API Key" : "Save API Key"}</button>{hasStoredKey && <button className="secondary-button" type="button" onClick={removeKey} disabled={removingKey}>{removingKey ? "Removing…" : "Remove API Key"}</button>}</div><p>{ai === null ? "Checking AI configuration…" : ai.credentialPresent ? "An API key is available to this running application." : "No saved API key."}</p><p>Provider: {ai?.provider ?? "Not configured"} · Model: {ai?.model ?? "Not configured"}</p><button className="secondary-button" type="button" onClick={checkConnection} disabled={checkingConnection || !ai?.credentialPresent}>{checkingConnection ? "Checking OpenAI connection…" : "Check Connection"}</button>{credentialNotice && <p className="ai-connection-result">{credentialNotice}</p>}{lastCheck && <p className="ai-connection-result">Last explicit API check: {lastCheck}</p>}{connectionError && <p className="viewer-error" role="alert">{connectionError}</p>}<p className="settings-detail">Check Connection uses OpenAI’s Models metadata endpoint only when clicked. It sends no document content and does not generate a model response.</p></div></section>
    <section className="settings-section"><div className="settings-card"><h2>Cloud AI authorization</h2><p>Cloud AI is off by default. When enabled, choosing an AI action sends only the bounded extracted text needed for that action and your question to OpenAI. Original files stay local. Do not enable Cloud AI for material you are not authorized to share.</p><button className="secondary-button" type="button" onClick={updateAuthorization} disabled={updatingAuthorization || ai === null}>{updatingAuthorization ? "Updating authorization…" : ai?.authorized ? "Turn off Cloud AI" : "Enable Cloud AI"}</button>{ai?.authorized && <p className="ai-connection-result">Cloud AI is enabled. Each Analyze or Ask AI action is recorded and checks its local scope and cost before content is sent.</p>}</div></section>
    <section className="settings-section"><div className="settings-card"><h2>Diagnostics</h2><p>Desktop environment: {desktopState}</p><BackendStatus /><StorageStatus /></div></section>
  </>;
}
