import { useEffect, useState } from "react";
import DashboardScreen from "./components/DashboardScreen";
import DocumentsScreen from "./components/DocumentsScreen";
import SettingsScreen from "./components/SettingsScreen";
import { isAvailablePage, workspacePages, type WorkspacePage } from "./components/workspace-ui";

type ConnectionState = "connecting" | "connected" | "error";

function App() {
  const [appInfo, setAppInfo] = useState<AppInfo | null>(null);
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const [activePage, setActivePage] = useState<WorkspacePage>("Dashboard");
  const [requestedDocumentId, setRequestedDocumentId] = useState<string | null>(null);

  useEffect(() => {
    let isCurrent = true;
    void window.desktopAPI.getAppInfo().then((info) => {
      if (!isCurrent) return;
      setAppInfo(info);
      setConnectionState("connected");
    }).catch(() => {
      if (isCurrent) setConnectionState("error");
    });
    return () => { isCurrent = false; };
  }, []);

  const openDocument = (documentId: string) => {
    setRequestedDocumentId(documentId);
    setActivePage("Documents");
  };

  return <div className="app-shell">
    <aside className="sidebar" aria-label="Primary navigation">
      <div className="brand"><span className="brand-mark" aria-hidden="true">GC</span><span>Group Claims</span></div>
      <nav className="navigation">
        {workspacePages.map((item) => {
          const isActive = item === activePage;
          const enabled = isAvailablePage(item);
          return <button key={item} className={`navigation-item${isActive ? " active" : ""}`} type="button" aria-current={isActive ? "page" : undefined} disabled={!enabled} onClick={() => setActivePage(item)}>
            <span>{item}</span>{!enabled && <span className="coming-soon">Coming soon</span>}
          </button>;
        })}
      </nav>
      <p className="sidebar-note">Single-user local workspace</p>
    </aside>
    <main className="main-content">
      {activePage === "Dashboard" && <DashboardScreen onOpenDocument={openDocument} />}
      {activePage === "Documents" && <DocumentsScreen requestedDocumentId={requestedDocumentId} onRequestedDocumentHandled={() => setRequestedDocumentId(null)} />}
      {activePage === "Settings" && <SettingsScreen appInfo={appInfo} desktopState={connectionState} />}
    </main>
  </div>;
}

export default App;
