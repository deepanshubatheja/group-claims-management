import { useEffect, useState } from "react";
import DashboardScreen from "./components/DashboardScreen";
import DocumentsScreen from "./components/DocumentsScreen";
import MatterReviewScreen from "./components/MatterReviewScreen";
import SettingsScreen from "./components/SettingsScreen";
import { isAvailablePage, workspacePages, type WorkspacePage } from "./components/workspace-ui";

type ConnectionState = "connecting" | "connected" | "error";

function App() {
  const [appInfo, setAppInfo] = useState<AppInfo | null>(null);
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const [activePage, setActivePage] = useState<WorkspacePage>("Dashboard");
  const [requestedDocumentId, setRequestedDocumentId] = useState<string | null>(null);
  const [requestedSource, setRequestedSource] = useState<{ documentId: string; sourceReference: string } | null>(null);

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
    setRequestedSource(null);
    setActivePage("Documents");
  };
  const openSource = (documentId: string, sourceReference: string) => {
    setRequestedDocumentId(documentId);
    setRequestedSource({ documentId, sourceReference });
    setActivePage("Documents");
  };
  const openDocuments = () => {
    setRequestedDocumentId(null);
    setActivePage("Documents");
  };

  return <div className="app-shell">
    <aside className="sidebar" aria-label="Primary navigation">
      <div className="brand"><span className="brand-mark" aria-hidden="true">GC</span><span>Group Claims</span></div>
      <nav className="navigation">
        {workspacePages.filter(isAvailablePage).map((item) => {
          const isActive = item === activePage;
          return <button key={item} className={`navigation-item${isActive ? " active" : ""}`} type="button" aria-current={isActive ? "page" : undefined} onClick={() => setActivePage(item)}>
            <span>{item}</span>
          </button>;
        })}
      </nav>
      <p className="sidebar-note">Single-user local workspace</p>
    </aside>
    <main className="main-content">
      {activePage === "Dashboard" && <DashboardScreen onOpenDocument={openDocument} onViewDocuments={openDocuments} />}
      {activePage === "Documents" && <DocumentsScreen requestedDocumentId={requestedDocumentId} onRequestedDocumentHandled={() => setRequestedDocumentId(null)} citedSource={requestedSource} onCloseCitedSource={() => setRequestedSource(null)} />}
      <div hidden={activePage !== "Matter Review"}><MatterReviewScreen isActive={activePage === "Matter Review"} onOpenSource={openSource} /></div>
      {activePage === "Settings" && <SettingsScreen appInfo={appInfo} desktopState={connectionState} />}
    </main>
  </div>;
}

export default App;
