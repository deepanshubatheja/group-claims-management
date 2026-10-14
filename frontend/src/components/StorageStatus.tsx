import { useCallback, useEffect, useState } from "react";

type StorageConnectionState = "initializing" | "ready" | "error";

export default function StorageStatus() {
  const [connectionState, setConnectionState] =
    useState<StorageConnectionState>("initializing");
  const [storage, setStorage] = useState<StorageStatus | null>(null);
  const [message, setMessage] = useState("Checking local storage.");

  const checkStorage = useCallback(async () => {
    setConnectionState("initializing");
    setMessage("Checking local storage.");

    try {
      const response = await window.desktopAPI.getStorageStatus();
      if (!response.ok) {
        setStorage(null);
        setConnectionState("error");
        setMessage(response.error.message);
        return;
      }

      setStorage(response.storage);
      if (response.storage.status === "ready") {
        setConnectionState("ready");
        setMessage("Local storage and the application database are ready.");
      } else {
        setConnectionState("error");
        setMessage("Local storage could not be initialized.");
      }
    } catch {
      setStorage(null);
      setConnectionState("error");
      setMessage("The desktop application could not check storage status.");
    }
  }, []);

  useEffect(() => {
    void checkStorage();
  }, [checkStorage]);

  return (
    <section className="storage-card" aria-labelledby="storage-heading">
      <div className="storage-card-header">
        <div>
          <p className="section-label">Local persistence</p>
          <h2 id="storage-heading">Storage status</h2>
        </div>
        <span className={`storage-status ${connectionState}`} role="status">
          <span className="status-dot" aria-hidden="true" />
          {connectionState === "initializing"
            ? "Initializing"
            : connectionState === "ready"
              ? "Ready"
              : "Error"}
        </span>
      </div>

      <p className="storage-message">{message}</p>

      {storage !== null && (
        <dl className="storage-details">
          <div>
            <dt>Database</dt>
            <dd>{storage.databaseStatus === "connected" ? "Connected" : "Unavailable"}</dd>
          </div>
          <div>
            <dt>Schema version</dt>
            <dd>{storage.schemaVersion ?? "Unavailable"}</dd>
          </div>
          <div>
            <dt>Local storage</dt>
            <dd>{storage.directoriesAvailable ? "Available" : "Unavailable"}</dd>
          </div>
        </dl>
      )}
    </section>
  );
}
