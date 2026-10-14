import { useEffect, useState } from "react";
import BackendStatus from "./components/BackendStatus";
import StorageStatus from "./components/StorageStatus";
import DocumentsScreen from "./components/DocumentsScreen";

const navigationItems = [
  "Dashboard",
  "Documents",
  "Holdings",
  "Discrepancies",
  "Billing",
  "Reports",
  "AI Assistant",
] as const;

type ConnectionState = "connecting" | "connected" | "error";

function App() {
  const [appInfo, setAppInfo] = useState<AppInfo | null>(null);
  const [connectionState, setConnectionState] =
    useState<ConnectionState>("connecting");
  const [activePage, setActivePage] = useState<(typeof navigationItems)[number]>("Dashboard");

  useEffect(() => {
    let isCurrent = true;

    void window.desktopAPI
      .getAppInfo()
      .then((info) => {
        if (!isCurrent) {
          return;
        }

        setAppInfo(info);
        setConnectionState("connected");
      })
      .catch(() => {
        if (isCurrent) {
          setConnectionState("error");
        }
      });

    return () => {
      isCurrent = false;
    };
  }, []);

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="Primary navigation">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            GC
          </span>
          <span>Group Claims</span>
        </div>

        <nav className="navigation">
          {navigationItems.map((item) => {
            const isActive = item === activePage;
            const enabled = item === "Dashboard" || item === "Documents";

            return (
              <button
                key={item}
                className={`navigation-item${isActive ? " active" : ""}`}
                type="button"
                aria-current={isActive ? "page" : undefined}
                disabled={!enabled}
                onClick={() => setActivePage(item)}
              >
                <span>{item}</span>
                {!enabled && <span className="coming-soon">Coming soon</span>}
              </button>
            );
          })}
        </nav>

        <p className="sidebar-note">Single-user local workspace</p>
      </aside>

      <main className="main-content">
        {activePage === "Documents" ? <DocumentsScreen /> : <>
        <header className="page-header">
          <div>
            <p className="eyebrow">Dashboard</p>
            <h1>Group Claims Management</h1>
          </div>
          <span className={`connection-status ${connectionState}`} role="status">
            <span className="status-dot" aria-hidden="true" />
            {connectionState === "connected"
              ? "Desktop environment connected"
              : connectionState === "connecting"
                ? "Connecting to desktop environment"
                : "Desktop environment unavailable"}
          </span>
        </header>

        <section className="welcome-card" aria-labelledby="welcome-heading">
          <p className="section-label">Workspace ready</p>
          <h2 id="welcome-heading">Welcome to your local claims workspace.</h2>
          <p>
            The desktop shell is connected and ready for the next development
            phase. No claim, holding, document, or billing data is displayed.
          </p>
        </section>

        <section className="environment-card" aria-labelledby="environment-heading">
          <div>
            <p className="section-label">Application environment</p>
            <h2 id="environment-heading">Desktop application details</h2>
          </div>
          <dl>
            <div>
              <dt>Application</dt>
              <dd>{appInfo?.name ?? "Loading…"}</dd>
            </div>
            <div>
              <dt>Version</dt>
              <dd>{appInfo?.version ?? "Loading…"}</dd>
            </div>
            <div>
              <dt>Platform</dt>
              <dd>{appInfo?.platform ?? "Loading…"}</dd>
            </div>
          </dl>
        </section>

        <BackendStatus />
        <StorageStatus />
        </>}
      </main>
    </div>
  );
}

export default App;
