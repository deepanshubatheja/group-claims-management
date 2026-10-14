import { useCallback, useEffect, useState } from "react";

type BackendConnectionState = "starting" | "connected" | "disconnected" | "error";

function platformLabel(platform: BackendHealth["platform"]): string {
  return `${platform.system} ${platform.release} (${platform.machine})`;
}

export default function BackendStatus() {
  const [connectionState, setConnectionState] =
    useState<BackendConnectionState>("starting");
  const [health, setHealth] = useState<BackendHealth | null>(null);
  const [message, setMessage] = useState("Checking the local backend connection.");

  const checkBackend = useCallback(async () => {
    setConnectionState("starting");
    setMessage("Checking the local backend connection.");

    try {
      const response = await window.desktopAPI.getBackendHealth();
      if (!response.ok) {
        setHealth(null);
        setConnectionState("disconnected");
        setMessage(response.error.message);
        return;
      }

      setHealth(response.health);
      setConnectionState("connected");
      setMessage("The local Python backend is responding.");
    } catch {
      setHealth(null);
      setConnectionState("error");
      setMessage("The desktop application could not check backend status.");
    }
  }, []);

  useEffect(() => {
    void checkBackend();
  }, [checkBackend]);

  return (
    <section className="backend-card" aria-labelledby="backend-heading">
      <div className="backend-card-header">
        <div>
          <p className="section-label">Local backend</p>
          <h2 id="backend-heading">Backend status</h2>
        </div>
        <span className={`backend-status ${connectionState}`} role="status">
          <span className="status-dot" aria-hidden="true" />
          {connectionState === "starting"
            ? "Starting"
            : connectionState === "connected"
              ? "Connected"
              : connectionState === "disconnected"
                ? "Disconnected"
                : "Error"}
        </span>
      </div>

      <p className="backend-message">{message}</p>

      {health !== null && (
        <dl className="backend-details">
          <div>
            <dt>Backend version</dt>
            <dd>{health.applicationVersion}</dd>
          </div>
          <div>
            <dt>Python runtime</dt>
            <dd>{health.pythonVersion}</dd>
          </div>
          <div>
            <dt>Platform</dt>
            <dd>{platformLabel(health.platform)}</dd>
          </div>
        </dl>
      )}

      <button
        className="retry-button"
        type="button"
        onClick={() => void checkBackend()}
        disabled={connectionState === "starting"}
      >
        Retry Connection
      </button>
    </section>
  );
}
