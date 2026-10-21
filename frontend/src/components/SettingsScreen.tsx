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
  const [connectingCodex, setConnectingCodex] = useState(false);
  const [switchingProvider, setSwitchingProvider] = useState(false);
  const [updatingAuthorization, setUpdatingAuthorization] = useState(false);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [credentialNotice, setCredentialNotice] = useState<string | null>(null);
  const refreshAIStatus = () => { void window.desktopAPI.getAIStatus().then((response) => { if (response.ok) setAI(response.ai); }); };
  const saveKey = () => {
    if (savingKey || switchingProvider || !apiKey.trim()) return;
    setSavingKey(true); setConnectionError(null); setCredentialNotice(null);
    void window.desktopAPI.saveAIApiKey(apiKey).then((response) => {
      if (response.ok) { setApiKey(""); setCredentialNotice(response.message); refreshAIStatus(); }
      else setConnectionError(response.error.message);
    }).catch(() => setConnectionError("The API key could not be saved.")).finally(() => setSavingKey(false));
  };
  const removeKey = () => {
    if (removingKey || switchingProvider) return;
    setRemovingKey(true); setConnectionError(null); setCredentialNotice(null);
    void window.desktopAPI.removeAIApiKey().then((response) => {
      if (response.ok) { setApiKey(""); setCredentialNotice(response.message); refreshAIStatus(); }
      else setConnectionError(response.error.message);
    }).catch(() => setConnectionError("The stored API key could not be removed.")).finally(() => setRemovingKey(false));
  };
  const checkConnection = () => {
    if (checkingConnection || switchingProvider) return;
    setCheckingConnection(true); setConnectionError(null);
    void window.desktopAPI.checkAIConnection().then((response) => { if (!response.ok) setConnectionError(response.error.message); refreshAIStatus(); }).catch(() => setConnectionError("The connection check could not be completed.")).finally(() => setCheckingConnection(false));
  };
  const connectCodex = () => {
    if (connectingCodex || switchingProvider) return;
    setConnectingCodex(true); setConnectionError(null); setCredentialNotice(null);
    void window.desktopAPI.connectCodex().then((response) => {
      if (response.ok) setCredentialNotice(response.message);
      else { setConnectingCodex(false); setConnectionError(response.error.message); }
    }).catch(() => { setConnectingCodex(false); setConnectionError("ChatGPT sign-in could not be started."); });
  };
  const disconnectCodex = () => {
    if (switchingProvider) return;
    setConnectingCodex(false); setConnectionError(null);
    void window.desktopAPI.disconnectCodex().then((response) => {
      if (response.ok) { setCredentialNotice(response.message); refreshAIStatus(); }
      else setConnectionError(response.error.message);
    }).catch(() => setConnectionError("ChatGPT could not be disconnected."));
  };
  const switchProvider = (provider: "codex" | "openai") => {
    if (switchingProvider || connectingCodex || provider === ai?.provider) return;
    setSwitchingProvider(true); setConnectingCodex(false); setConnectionError(null); setCredentialNotice(null);
    void window.desktopAPI.setAIProvider(provider).then((response) => {
      if (response.ok) {
        setAI(response.ai); setApiKey("");
        setCredentialNotice(`Switched to ${provider === "codex" ? "Codex" : "OpenAI API"}. Cloud AI authorization is off; enable it again before using AI.`);
      } else setConnectionError(response.error.message);
    }).catch(() => setConnectionError("The AI provider could not be changed.")).finally(() => setSwitchingProvider(false));
  };
  const updateAuthorization = () => {
    if (updatingAuthorization || switchingProvider) return;
    setUpdatingAuthorization(true); setConnectionError(null);
    void window.desktopAPI.setAIAuthorization(!(ai?.authorized ?? false)).then((response) => { if (response.ok) setAI(response.ai); else setConnectionError(response.error.message); }).catch(() => setConnectionError("Cloud-processing authorization could not be updated.")).finally(() => { setUpdatingAuthorization(false); refreshAIStatus(); });
  };
  useEffect(() => { refreshAIStatus(); }, []);
  useEffect(() => {
    if (!connectingCodex) return;
    const timer = window.setInterval(() => {
      void window.desktopAPI.getAIStatus().then((response) => {
        if (response.ok) { setAI(response.ai); if (response.ai.credentialPresent) { setConnectingCodex(false); setCredentialNotice("ChatGPT is connected. You can now enable Cloud AI below."); } }
      });
    }, 2000);
    return () => window.clearInterval(timer);
  }, [connectingCodex]);
  const lastCheck = aiConnectionMessage(ai?.lastConnectionCheck ?? null);
  const hasStoredKey = ai?.credentialPresent === true;
  return <>
    <header className="page-header"><div><p className="eyebrow">Settings</p><h1>Settings</h1></div></header>
    <section className="settings-section"><div className="settings-card"><h2>About</h2><p>{appInfo?.name ?? "ClaimFlow"} · Version {appInfo?.version ?? "Unavailable"}</p><p>Operating system: {platformName(appInfo?.platform)}</p></div></section>
    <section className="settings-section"><div className="settings-card"><h2>AI Connection</h2>
      <label className="settings-provider" htmlFor="ai-provider">AI provider<select id="ai-provider" value={ai?.provider === "openai" ? "openai" : "codex"} disabled={ai === null || switchingProvider || connectingCodex || savingKey || removingKey || updatingAuthorization} onChange={(event) => { const provider = event.target.value; if (provider === "codex" || provider === "openai") switchProvider(provider); }}><option value="codex">Codex (ChatGPT sign-in)</option><option value="openai">OpenAI API key</option></select></label>
      {switchingProvider && <p className="settings-detail" role="status">Switching provider…</p>}
      {ai?.provider === "codex" ? <><p>Connect your ChatGPT account to use Codex. No OpenAI API key is needed. Sign-in opens in your browser; ClaimFlow stores the session in your operating system’s credential store.</p><p>{ai.credentialPresent ? "ChatGPT is connected." : connectingCodex ? "Waiting for ChatGPT sign-in…" : "ChatGPT is not connected."}</p><div className="settings-key-row">{ai.credentialPresent ? <button className="secondary-button" type="button" onClick={disconnectCodex} disabled={switchingProvider}>Disconnect ChatGPT</button> : <button className="secondary-button" type="button" onClick={connectCodex} disabled={connectingCodex || switchingProvider}>{connectingCodex ? "Waiting for sign-in…" : "Connect ChatGPT"}</button>}</div><p className="settings-detail">Your plan’s Codex usage limits apply. ClaimFlow cannot show a USD API estimate or enforce an output-token cap for Codex.</p></> : <><p>Save your OpenAI API key in macOS Keychain. ClaimFlow never displays the saved key.</p><label htmlFor="openai-api-key">{hasStoredKey ? "Replace API key" : "OpenAI API key"}</label><div className="settings-key-row"><input id="openai-api-key" type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={hasStoredKey ? "Enter a replacement key" : "Paste API key"} disabled={switchingProvider} /><button className="secondary-button" type="button" onClick={saveKey} disabled={savingKey || switchingProvider || !apiKey.trim()}>{savingKey ? "Saving…" : hasStoredKey ? "Replace API Key" : "Save API Key"}</button>{hasStoredKey && <button className="secondary-button" type="button" onClick={removeKey} disabled={removingKey || switchingProvider}>{removingKey ? "Removing…" : "Remove API Key"}</button>}</div><p>{ai === null ? "Checking AI configuration…" : ai.credentialPresent ? "An API key is available to this running application." : "No saved API key."}</p></>}
      <p>Provider: {ai?.provider ?? "Not configured"}{ai?.model ? ` · Model: ${ai.model}` : ""}</p><button className="secondary-button" type="button" onClick={checkConnection} disabled={checkingConnection || switchingProvider || !ai?.credentialPresent}>{checkingConnection ? "Checking connection…" : "Check Connection"}</button>{credentialNotice && <p className="ai-connection-result">{credentialNotice}</p>}{lastCheck && <p className="ai-connection-result">Last explicit connection check: {lastCheck}</p>}{connectionError && <p className="viewer-error" role="alert">{connectionError}</p>}<p className="settings-detail">Check Connection verifies account access only when clicked. It sends no document content and does not generate a model response.</p></div></section>
    <section className="settings-section"><div className="settings-card"><h2>Cloud AI authorization</h2><p>Cloud AI is off by default. When enabled, choosing an AI action sends only the bounded extracted text needed for that action and your question to the selected provider. Original files stay local. Switching providers turns authorization off until you enable it again. Do not enable Cloud AI for material you are not authorized to share.</p><button className="secondary-button" type="button" onClick={updateAuthorization} disabled={updatingAuthorization || switchingProvider || ai === null}>{updatingAuthorization ? "Updating authorization…" : ai?.authorized ? "Turn off Cloud AI" : "Enable Cloud AI"}</button>{ai?.authorized && <p className="ai-connection-result">Cloud AI is enabled. Each Analyze or Ask AI action is recorded and checks its local scope before content is sent.</p>}</div></section>
    <section className="settings-section"><div className="settings-card"><h2>Diagnostics</h2><p>Desktop environment: {desktopState}</p><BackendStatus /><StorageStatus /></div></section>
  </>;
}
