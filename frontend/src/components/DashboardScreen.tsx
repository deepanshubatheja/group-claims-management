import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { recentDocuments, workspaceNotices } from "./workspace-ui";

interface DashboardScreenProps { onOpenDocument(documentId: string): void; onViewDocuments(): void; }

export default function DashboardScreen({ onOpenDocument, onViewDocuments }: DashboardScreenProps) {
  const [backendAvailable, setBackendAvailable] = useState<boolean | null>(null);
  const [storageAvailable, setStorageAvailable] = useState<boolean | null>(null);
  const [message, setMessage] = useState("");
  const [showChoices, setShowChoices] = useState(false);
  const [folderPreview, setFolderPreview] = useState<FolderImportPreview | null>(null);
  const [folderProgress, setFolderProgress] = useState<FolderImportProgress | null>(null);
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [isDragActive, setIsDragActive] = useState(false);
  const [stagedDropNames, setStagedDropNames] = useState<string[]>([]);
  const importControlRef = useRef<HTMLDivElement>(null);
  const dragDepthRef = useRef(0);
  const stagedDropFilesRef = useRef<File[]>([]);
  const refresh = useCallback(async () => {
    const [backend, storage] = await Promise.allSettled([window.desktopAPI.getBackendHealth(), window.desktopAPI.getStorageStatus()]);
    setBackendAvailable(backend.status === "fulfilled" && backend.value.ok);
    setStorageAvailable(storage.status === "fulfilled" && storage.value.ok && storage.value.storage.status === "ready" && storage.value.storage.databaseStatus === "connected" && storage.value.storage.directoriesAvailable);
  }, []);
  const refreshDocuments = useCallback(async () => { const response = await window.desktopAPI.listDocuments(); if (response.ok) setDocuments(response.documents); }, []);
  useEffect(() => { void refresh(); void refreshDocuments(); }, [refresh, refreshDocuments]);
  useEffect(() => window.desktopAPI.onFolderImportProgress((progress) => setFolderProgress(progress)), []);
  useEffect(() => {
    if (!showChoices) return;
    const closeOnOutsidePress = (event: PointerEvent) => {
      if (importControlRef.current?.contains(event.target as Node) !== true) setShowChoices(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setShowChoices(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePress);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePress);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [showChoices]);
  const importFiles = async () => { setShowChoices(false); const response = await window.desktopAPI.selectAndImportDocuments(); if (!response.ok) setMessage("Documents could not be imported. Please try again."); else if (!response.cancelled) { setMessage(response.documents.some((item) => !item.ok) ? "Some documents could not be imported." : "Documents imported."); await refreshDocuments(); } };
  const chooseFolder = async () => { setShowChoices(false); const response = await window.desktopAPI.selectFolderForImport(); if (!response.ok) setMessage("The folder could not be imported. Please try again."); else if (response.preview) setFolderPreview(response.preview); };
  const onDragEnter = (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    dragDepthRef.current += 1;
    setIsDragActive(true);
  };
  const onDragLeave = (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setIsDragActive(false);
  };
  const onDrop = async (event: DragEvent<HTMLElement>) => {
    event.preventDefault();
    dragDepthRef.current = 0;
    setIsDragActive(false);
    setShowChoices(false);
    const files = Array.from(event.dataTransfer.files);
    if (files.length === 0) { setMessage("Drop local files or folders to import them."); return; }
    const stagedFiles = [...stagedDropFilesRef.current, ...files];
    if (stagedFiles.length > 20) {
      setMessage("Add up to 20 files or folders to one staged import.");
      return;
    }
    try {
      // Keep DOM File objects only in this renderer session. Their native
      // paths remain inside preload/main, which rebuilds one validated import
      // plan from the complete staged set on every drop.
      const response = await window.desktopAPI.importDroppedDocuments(stagedFiles);
      if (!response.ok) { setMessage(response.error.message); return; }
      if (!response.preview) { setMessage("No supported documents were found in the dropped items."); return; }
      const previousAuthorizationId = folderPreview?.authorizationId;
      stagedDropFilesRef.current = stagedFiles;
      setStagedDropNames(stagedFiles.map((file) => file.name));
      setFolderProgress(null);
      setFolderPreview(response.preview);
      setMessage("");
      if (previousAuthorizationId && previousAuthorizationId !== response.preview.authorizationId) {
        void window.desktopAPI.cancelFolderImport(previousAuthorizationId);
      }
    } catch {
      setMessage("The dropped items could not be prepared for import. Try again with local files or folders.");
    }
  };
  const clearStagedDropFiles = () => { stagedDropFilesRef.current = []; setStagedDropNames([]); };
  const confirmFolder = async () => { if (!folderPreview) return; const response = await window.desktopAPI.confirmFolderImport(folderPreview.authorizationId); setFolderPreview(null); clearStagedDropFiles(); if (!response.ok) setMessage(response.error.message); else { setFolderProgress(response.result); setMessage(response.result.failed ? "Folder import completed with some failures." : "Folder import complete."); await refreshDocuments(); } };
  const cancelFolder = async () => { if (folderPreview) await window.desktopAPI.cancelFolderImport(folderPreview.authorizationId); setFolderPreview(null); clearStagedDropFiles(); };
  const notices = workspaceNotices(backendAvailable, storageAvailable);
  const latestDocuments = recentDocuments(documents);
  return <><header className="page-header dashboard-header"><div><p className="eyebrow">Dashboard</p><h1>Your workspace</h1><p className="page-subtitle">Documents, evidence-linked reviews, and matter-level insights in one place.</p></div></header>
    <section className={`dashboard-hero dashboard-drop-target${isDragActive ? " is-drag-active" : ""}`} onDragEnter={onDragEnter} onDragOver={(event) => event.preventDefault()} onDragLeave={onDragLeave} onDrop={(event) => void onDrop(event)}><div><p className="section-label">Add documents</p><h2>Start with your source material</h2><p>Choose individual files or a folder.</p><p className="dashboard-drop-hint">Drag &amp; drop files or folders here</p></div><div className="dashboard-import-control" ref={importControlRef}><button className="primary-button" type="button" aria-expanded={showChoices} aria-haspopup="menu" onClick={() => setShowChoices((visible) => !visible)}>Import documents</button>{showChoices && <div className="import-choice-menu" role="menu"><button type="button" role="menuitem" onClick={() => void importFiles()}>Choose files</button><button type="button" role="menuitem" onClick={() => void chooseFolder()}>Choose folder</button></div>}</div>{isDragActive && <div className="dashboard-drop-overlay" aria-hidden="true">Drop files or folders to prepare import</div>}</section>
    {notices.includes("backend") && <section className="workspace-notice" role="alert"><p>Document processing is temporarily unavailable.</p><button className="retry-button" type="button" onClick={() => void refresh()}>Retry Connection</button></section>}{notices.includes("storage") && <section className="workspace-notice" role="alert"><p>Local storage is unavailable. Please check disk space or folder permissions.</p><button className="retry-button" type="button" onClick={() => void refresh()}>Retry Connection</button></section>}
    {folderPreview && <section className="dashboard-card folder-import-confirmation"><p className="section-label">Import review</p><h2>Ready to import these items?</h2><p>{folderPreview.supportedFiles} supported files are ready to import. {folderPreview.skippedFiles + folderPreview.skippedLinks > 0 ? `${folderPreview.skippedFiles + folderPreview.skippedLinks} unsupported, linked, or unavailable items will be skipped.` : ""} {folderPreview.limitReached ? "The folder-import safety limit was reached; remaining items are excluded." : ""}</p>{stagedDropNames.length > 0 && <><p className="staged-import-label">Staged drop items</p><ul className="staged-import-list">{stagedDropNames.map((name, index) => <li key={`${name}:${index}`}>{name}</li>)}</ul></>}<div className="import-actions"><button className="primary-button" type="button" disabled={folderPreview.supportedFiles === 0} onClick={() => void confirmFolder()}>Start import</button><button className="secondary-button" type="button" onClick={() => void cancelFolder()}>Cancel</button></div></section>}
    {folderProgress && <p className="folder-import-progress" role="status">Import progress: {folderProgress.processed} of {folderProgress.discovered} files processed · {folderProgress.imported} imported · {folderProgress.duplicates} already added · {folderProgress.failed} need attention</p>}{message && <p className="dashboard-message" role="status">{message}</p>}
    <section className="dashboard-overview"><article className="dashboard-count"><p className="section-label">Documents</p><strong>{documents.length}</strong><span>{documents.length === 1 ? "document available" : "documents available"}</span></article><article className="dashboard-help"><h2>Ready when you are</h2><p>Open a document to review its findings, or bring several together in Matter Review.</p><button className="text-button" type="button" onClick={onViewDocuments}>View documents</button></article></section>
    <section className="dashboard-card dashboard-recent"><div className="dashboard-section-heading"><div><p className="section-label">Recently added</p><h2>Latest documents</h2></div>{latestDocuments.length > 0 && <button className="text-button" type="button" onClick={onViewDocuments}>View all documents</button>}</div>{latestDocuments.length > 0 ? <div className="recent-document-list">{latestDocuments.map((document) => <button className="recent-document" key={document.id} type="button" onClick={() => onOpenDocument(document.id)}><span><strong>{document.original_filename}</strong><small>Imported {new Date(document.imported_at).toLocaleDateString()}</small></span><span className="document-row-labels"><span className={`review-label ${document.ai_review_status}`}>{document.ai_review_status === "reviewed" ? "AI reviewed" : "New"}</span><span className={`document-status ${document.processing_status}`}>{document.processing_status}</span></span></button>)}</div> : <div className="dashboard-empty"><strong>No documents uploaded yet.</strong><p>Import documents to begin reviewing your workspace.</p><button className="text-button" type="button" onClick={() => setShowChoices(true)}>Import documents</button></div>}</section>
  </>;
}
