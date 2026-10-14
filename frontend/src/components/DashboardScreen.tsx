import { useCallback, useEffect, useState } from "react";
import { recentDocuments, workspaceNotices } from "./workspace-ui";

interface DashboardScreenProps { onOpenDocument(documentId: string): void; }

export default function DashboardScreen({ onOpenDocument }: DashboardScreenProps) {
  const [backendAvailable, setBackendAvailable] = useState<boolean | null>(null);
  const [storageAvailable, setStorageAvailable] = useState<boolean | null>(null);
  const [message, setMessage] = useState("");
  const [showChoices, setShowChoices] = useState(false);
  const [folderPreview, setFolderPreview] = useState<FolderImportPreview | null>(null);
  const [folderProgress, setFolderProgress] = useState<FolderImportProgress | null>(null);
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const refresh = useCallback(async () => {
    const [backend, storage] = await Promise.allSettled([window.desktopAPI.getBackendHealth(), window.desktopAPI.getStorageStatus()]);
    setBackendAvailable(backend.status === "fulfilled" && backend.value.ok);
    setStorageAvailable(storage.status === "fulfilled" && storage.value.ok && storage.value.storage.status === "ready" && storage.value.storage.databaseStatus === "connected" && storage.value.storage.directoriesAvailable);
  }, []);
  const refreshDocuments = useCallback(async () => { const response = await window.desktopAPI.listDocuments(); if (response.ok) setDocuments(response.documents); }, []);
  useEffect(() => { void refresh(); void refreshDocuments(); }, [refresh, refreshDocuments]);
  useEffect(() => window.desktopAPI.onFolderImportProgress((progress) => setFolderProgress(progress)), []);
  const importFiles = async () => { setShowChoices(false); const response = await window.desktopAPI.selectAndImportDocuments(); if (!response.ok) setMessage("Documents could not be imported. Please try again."); else if (!response.cancelled) { setMessage(response.documents.some((item) => !item.ok) ? "Some documents could not be imported." : "Documents imported."); await refreshDocuments(); } };
  const chooseFolder = async () => { setShowChoices(false); const response = await window.desktopAPI.selectFolderForImport(); if (!response.ok) setMessage("The folder could not be imported. Please try again."); else if (response.preview) setFolderPreview(response.preview); };
  const confirmFolder = async () => { if (!folderPreview) return; const response = await window.desktopAPI.confirmFolderImport(folderPreview.authorizationId); setFolderPreview(null); if (!response.ok) setMessage(response.error.message); else { setFolderProgress(response.result); setMessage(response.result.failed ? "Folder import completed with some failures." : "Folder import complete."); await refreshDocuments(); } };
  const cancelFolder = async () => { if (folderPreview) await window.desktopAPI.cancelFolderImport(folderPreview.authorizationId); setFolderPreview(null); };
  const notices = workspaceNotices(backendAvailable, storageAvailable);
  const latestDocuments = recentDocuments(documents);
  return <><header className="page-header dashboard-header"><div><p className="eyebrow">Dashboard</p><h1>Dashboard</h1><p className="page-subtitle">Overview of your claims workspace</p></div></header><section className="dashboard-welcome"><p>Import source documents to begin reviewing your claims workspace.</p><div className="dashboard-import-control"><button className="primary-button" type="button" onClick={() => setShowChoices((visible) => !visible)}>Import Documents</button>{showChoices && <div className="import-choice-menu"><button type="button" onClick={() => void importFiles()}>Choose files</button><button type="button" onClick={() => void chooseFolder()}>Choose folder</button></div>}</div></section>{notices.includes("backend") && <section className="workspace-notice" role="alert"><p>Document processing is temporarily unavailable.</p><button className="retry-button" type="button" onClick={() => void refresh()}>Retry Connection</button></section>}{notices.includes("storage") && <section className="workspace-notice" role="alert"><p>Local storage is unavailable. Please check disk space or folder permissions.</p><button className="retry-button" type="button" onClick={() => void refresh()}>Retry Connection</button></section>}{folderPreview && <section className="dashboard-card folder-import-confirmation"><h2>Import folder?</h2><p>{folderPreview.supportedFiles} supported files · {folderPreview.totalBytes.toLocaleString()} bytes. {folderPreview.skippedFiles + folderPreview.skippedLinks} files will be skipped.</p><div className="import-actions"><button className="primary-button" type="button" disabled={folderPreview.supportedFiles === 0} onClick={() => void confirmFolder()}>Start import</button><button className="secondary-button" type="button" onClick={() => void cancelFolder()}>Cancel</button></div></section>}{folderProgress && <p className="folder-import-progress" role="status">Processed {folderProgress.processed} of {folderProgress.discovered} · Imported {folderProgress.imported} · Duplicates {folderProgress.duplicates} · Failed {folderProgress.failed}</p>}{message && <p className="dashboard-message" role="status">{message}</p>}{latestDocuments.length > 0 && <section className="dashboard-card dashboard-recent"><h2>Recent documents</h2><div className="recent-document-list">{latestDocuments.map((document) => <button className="recent-document" key={document.id} type="button" onClick={() => onOpenDocument(document.id)}><span><strong>{document.original_filename}</strong><small>Imported {new Date(document.imported_at).toLocaleString()}</small></span><span className={`document-status ${document.processing_status}`}>{document.processing_status}</span></button>)}</div></section>}</>;
}
