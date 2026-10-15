import { useEffect, useMemo, useRef, useState } from "react";
import { analysisFailureMessage, analysisOverviewFromFindings, analysisValidationCounts, canStartDocumentAnalysis, findingFieldLabel, proposalTypeLabel } from "./analysis-ui";

function number(value: unknown, fallback = 0): number { return typeof value === "number" && Number.isFinite(value) ? value : fallback; }
function formatBytes(bytes: number): string { return bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`; }

function questionFailureMessage(code: string | null): string {
  if (code === "AI_AUTHORIZATION_WITHDRAWN") return "Cloud-processing authorization was withdrawn before this question could be sent.";
  if (code === "AI_ANSWER_EVIDENCE_UNSUPPORTED") return "The AI response did not provide locally supported evidence, so no answer was shown.";
  if (code === "AI_RESPONSE_CONTRACT_INVALID" || code === "AI_RESPONSE_JSON_INVALID") return "OpenAI did not return a usable cited answer. No answer was shown; try a more focused question.";
  if (code === "AI_OUTPUT_LIMIT_REACHED" || code === "AI_INCOMPLETE") return "OpenAI could not complete the concise cited answer within its configured response allowance. No answer was shown and the request was not retried automatically.";
  return analysisFailureMessage(code) ?? "The AI could not answer this question. No records were changed.";
}

type QuestionTurn = { id: string; question: string; answer: string | null; unknown: boolean; citations: DocumentQuestionCitation[]; usage: DocumentQuestionStatus["usage"]; };

function AnalysisQuestionPanel({ document }: { document: DocumentSummary }) {
  const [draft, setDraft] = useState("");
  const [preview, setPreview] = useState<DocumentQuestionPreview | null>(null);
  const [previewQuestion, setPreviewQuestion] = useState("");
  const [authorized, setAuthorized] = useState(false);
  const [job, setJob] = useState<DocumentQuestionStatus | null>(null);
  const [message, setMessage] = useState("");
  const [checkingScope, setCheckingScope] = useState(false);
  const [scopeRequest, setScopeRequest] = useState("");
  const [scopeRequestVersion, setScopeRequestVersion] = useState(0);
  const [pendingQuestion, setPendingQuestion] = useState("");
  const [turns, setTurns] = useState<QuestionTurn[]>([]);

  useEffect(() => { setDraft(""); setPreview(null); setPreviewQuestion(""); setAuthorized(false); setJob(null); setMessage(""); setCheckingScope(false); setScopeRequest(""); setScopeRequestVersion(0); setPendingQuestion(""); setTurns([]); }, [document.id]);
  useEffect(() => {
    if (!job || (job.status !== "queued" && job.status !== "running")) return;
    let active = true;
    const poll = window.setInterval(() => {
      void window.desktopAPI.getDocumentQuestionStatus(job.questionId).then((response) => {
        if (!active) return;
        if (response.ok) setJob(response.question);
        else setMessage(response.error.message);
      }).catch(() => { if (active) setMessage("AI Q&A is temporarily unavailable."); });
    }, 900);
    return () => { active = false; window.clearInterval(poll); };
  }, [job?.questionId, job?.status]);
  useEffect(() => {
    if (!job || job.status !== "completed") return;
    setTurns((current) => current.some((turn) => turn.id === job.questionId) ? current : [...current, {
      id: job.questionId, question: pendingQuestion, answer: job.answer, unknown: job.unknown,
      citations: job.citations, usage: job.usage,
    }]);
  }, [job, pendingQuestion]);

  useEffect(() => {
    const question = scopeRequest;
    if (!question) { setPreview(null); setPreviewQuestion(""); setCheckingScope(false); return; }
    let active = true;
    setCheckingScope(true);
    void window.desktopAPI.previewDocumentQuestion(document.id, question).then((response) => {
      if (!active) return;
      if (response.ok) { setPreview(response.preview); setPreviewQuestion(question); }
      else setMessage(response.error.message);
    }).catch(() => { if (active) setMessage("AI Q&A is temporarily unavailable."); })
      .finally(() => { if (active) setCheckingScope(false); });
    return () => { active = false; };
  }, [document.id, scopeRequest, scopeRequestVersion]);
  const updateDraft = (value: string) => {
    setDraft(value);
    setAuthorized((current) => current ? false : current);
    setMessage((current) => current ? "" : current);
    if (!value.trim()) setScopeRequest("");
  };
  const requestScope = () => {
    const question = draft.trim();
    if (!question) return;
    setScopeRequest(question);
    setScopeRequestVersion((current) => current + 1);
  };
  const ask = async () => {
    if (!preview || previewQuestion !== draft.trim() || !authorized || job?.status === "running" || job?.status === "queued") return;
    setMessage("");
    try {
      const response = await window.desktopAPI.askDocumentQuestion(document.id, draft);
      if (response.ok) { setPendingQuestion(draft.trim()); setJob(response.question); } else setMessage(response.error.message);
    } catch { setMessage("AI Q&A is temporarily unavailable."); }
  };
  const asking = job?.status === "queued" || job?.status === "running";
  const previewIsCurrent = previewQuestion === draft.trim();
  const currentPreview = previewIsCurrent && !checkingScope ? preview : null;
  const scopeIsReady = currentPreview !== null;
  const canSubmit = Boolean(currentPreview && authorized && currentPreview.globalAuthorization && currentPreview.configured && currentPreview.cost.withinLimit && !asking);
  const startOrAsk = () => {
    if (!scopeIsReady) { requestScope(); return; }
    void ask();
  };
  return <section className="analysis-question"><div><p className="section-label">Document Q&A</p><h4>Ask about this document</h4><p>Ask a focused question and receive a provisional answer supported by the document evidence.</p></div>
    <label className="question-field">Your question<textarea value={draft} maxLength={600} placeholder="For example: Which accounts are identified in this document?" onChange={(event) => updateDraft(event.target.value)} /></label>
    <div className="question-actions"><span>{draft.length}/600</span></div>
    {draft.trim() && <div className="question-consent"><p>Your question and relevant extracted excerpts will be sent to OpenAI. The original file is not uploaded. Answers are provisional and must be reviewed against the cited evidence.</p>{!currentPreview && <p className="support-note">{checkingScope ? "Checking the local answer scope…" : "Click Ask AI to review the local answer scope before authorizing transmission."}</p>}{currentPreview && <>{currentPreview.scope.truncated && <p className="viewer-warning">Only part of this document can be included in this question. The answer may not cover excluded material.</p>}{!currentPreview.globalAuthorization && <p className="viewer-warning">Enable cloud-processing authorization in Settings before asking.</p>}{!currentPreview.cost.withinLimit && <p className="viewer-warning">This question cannot be sent within the current development safety limit.</p>}<label className="analysis-consent"><input type="checkbox" checked={authorized} onChange={(event) => setAuthorized(event.target.checked)} /> I authorize OpenAI to process this question and the relevant extracted excerpts.</label></>}<button className="primary-button" type="button" disabled={!draft.trim() || checkingScope || asking || (scopeIsReady && !canSubmit)} onClick={startOrAsk}>{checkingScope ? "Checking…" : asking ? "Answering…" : "Ask AI"}</button></div>}
    {turns.length > 0 && <div className="question-transcript">{turns.map((turn) => <article className="question-answer" key={turn.id}><p className="section-label">Your question</p><p>{turn.question}</p><p className="section-label">Provisional answer</p>{turn.unknown || !turn.answer ? <p>The available evidence does not establish an answer to this question.</p> : <p>{turn.answer}</p>}{turn.citations.length > 0 && <details className="answer-evidence"><summary>Supporting evidence</summary>{turn.citations.map((citation) => <p key={`${citation.sourceReference}:${citation.evidenceSnippet}`} className="answer-citation"><span>{citation.sourceReference}</span>{citation.evidenceSnippet}</p>)}</details>}</article>)}</div>}
    {job?.status === "failed" && <p className="viewer-error" role="alert">{questionFailureMessage(job.errorCode)}</p>}
    {message && <p className="viewer-state" role="status">{message}</p>}
  </section>;
}

function AIAnalysisPanel({ document }: { document: DocumentSummary }) {
  const [preview, setPreview] = useState<DocumentAnalysisPreview | null>(null);
  const [job, setJob] = useState<DocumentAnalysisJob | null>(null);
  const [findings, setFindings] = useState<DocumentAnalysisFinding[]>([]);
  const [message, setMessage] = useState("Checking AI analysis availability…");
  const [documentAuthorized, setDocumentAuthorized] = useState(false);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    let active = true;
    setJob(null); setFindings([]); setPreview(null); setMessage("Checking AI analysis availability…"); setDocumentAuthorized(false); setStarting(false);
    void window.desktopAPI.getDocumentAnalysisPreview(document.id).then((response) => {
      if (!active) return;
      if (response.ok) { setPreview(response.preview); setJob(response.preview.latestJob); setMessage(""); }
      else setMessage(response.error.message);
    }).catch(() => { if (active) setMessage("AI analysis is temporarily unavailable."); });
    return () => { active = false; };
  }, [document.id]);
  useEffect(() => {
    if (!job || job.status !== "completed") return;
    let active = true;
    void window.desktopAPI.getDocumentAnalysisFindings(job.jobId).then((items) => {
      if (!active) return;
      if (items.ok) setFindings(items.findings); else setMessage(items.error.message);
    }).catch(() => { if (active) setMessage("AI findings are temporarily unavailable."); });
    return () => { active = false; };
  }, [job?.jobId, job?.status]);
  useEffect(() => {
    if (!job || (job.status !== "queued" && job.status !== "running")) return;
    let active = true;
    const poll = window.setInterval(() => {
      void window.desktopAPI.getDocumentAnalysisStatus(job.jobId).then((response) => {
        if (!active) return;
        if (!response.ok) { setMessage(response.error.message); return; }
        setJob(response.job);
        if (response.job.status === "failed") setMessage(analysisFailureMessage(response.job.errorCode) ?? "AI analysis did not complete.");
      }).catch(() => { if (active) setMessage("AI analysis status is temporarily unavailable."); });
    }, 1000);
    return () => { active = false; window.clearInterval(poll); };
  }, [job?.jobId, job?.status]);
  const start = async () => {
    if (starting) return;
    setStarting(true); setMessage("Starting authorized AI analysis…");
    try { const response = await window.desktopAPI.startDocumentAnalysis(document.id); if (response.ok) { setJob(response.job); setMessage(""); } else setMessage(response.error.message); }
    catch { setMessage("AI analysis is temporarily unavailable."); }
    finally { setStarting(false); }
  };
  const groups = useMemo(() => findings.reduce<Record<string, DocumentAnalysisFinding[]>>((result, finding) => { (result[finding.proposalType] ??= []).push(finding); return result; }, {}), [findings]);
  const coverage = job?.coverage ?? null;
  const chunksProcessed = number(coverage?.chunksProcessed);
  const chunksTotal = number(coverage?.chunksTotal);
  const discarded = number(coverage?.discardedUnsupportedEvidence);
  const validation = analysisValidationCounts(coverage);
  const fallbackSummary = analysisOverviewFromFindings(findings);
  const hasStoredSummary = Boolean(job?.summary.text && !job.summary.unknown);
  const summaryText = hasStoredSummary ? job?.summary.text ?? null : fallbackSummary.text;
  const summaryCitations = hasStoredSummary ? job?.summary.citations ?? [] : fallbackSummary.citations;
  const partial = coverage?.truncated === true || discarded > 0 || (job?.status === "failed" && chunksProcessed > 0);
  if (document.processing_status !== "parsed") return <p className="viewer-state">This document must finish deterministic parsing before AI analysis is available.</p>;
  return <section className="analysis-panel"><div className="analysis-heading"><div><p className="section-label">AI review</p><h3>Document analysis</h3><p>Findings are provisional and supported by the cited document evidence.</p></div>{job?.status === "completed" && <span className="analysis-status">Analysis available</span>}</div>
    {preview && findings.length === 0 && <div className="analysis-setup"><p>Review this document with AI</p><p>Only relevant extracted material is shared with OpenAI. The original document stays in this workspace.</p>{preview.scope.truncated && <p className="viewer-warning">This review will cover only part of the document.</p>}{!preview.globalAuthorization && <p className="viewer-warning">Enable cloud-processing authorization in Settings before starting.</p>}<label className="analysis-consent"><input type="checkbox" checked={documentAuthorized} onChange={(event) => setDocumentAuthorized(event.target.checked)} /> I authorize OpenAI to process the relevant extracted content for this review.</label><button className="primary-button" type="button" disabled={starting || !canStartDocumentAnalysis(documentAuthorized, preview.globalAuthorization, preview.configured, preview.cost.withinLimit, job?.status ?? null)} onClick={() => void start()}>{starting ? "Starting analysis…" : "Analyze document"}</button></div>}
    {preview && findings.length > 0 && <details className="analysis-repeat"><summary>Run a new AI review</summary><p>A new review sends the relevant extracted content to OpenAI again. Earlier findings remain available for inspection.</p><label className="analysis-consent"><input type="checkbox" checked={documentAuthorized} onChange={(event) => setDocumentAuthorized(event.target.checked)} /> I authorize a new AI review of this document.</label><button className="secondary-button" type="button" disabled={starting || !canStartDocumentAnalysis(documentAuthorized, preview.globalAuthorization, preview.configured, preview.cost.withinLimit, job?.status ?? null)} onClick={() => void start()}>{starting ? "Starting analysis…" : "Run new analysis"}</button></details>}
    {job && <div className="analysis-meta"><span>{job.status === "completed" ? "Analysis completed" : `Analysis status: ${job.status}`}</span>{chunksTotal > 0 && chunksProcessed < chunksTotal && <span>Partial coverage</span>}</div>}
    {job?.status === "completed" && <section className="analysis-summary"><p className="section-label">{hasStoredSummary ? "AI summary" : "Analysis overview"}</p><h4>What was analyzed</h4>{summaryText ? <p>{summaryText}</p> : <p>The analyzed material does not establish a concise supported summary.</p>}{summaryCitations.length > 0 && <details className="answer-evidence"><summary>Summary evidence</summary>{summaryCitations.map((citation) => <p key={`${citation.sourceReference}:${citation.evidenceSnippet}`} className="answer-citation"><span>{citation.sourceReference}</span>{citation.evidenceSnippet}</p>)}</details>}<small>{hasStoredSummary ? "This provisional AI summary covers the material included in this analysis, not necessarily every part of the document." : "This overview is generated locally from the evidence-validated findings in this review."}</small></section>}
    {validation.available && validation.rejected > 0 && <p className="viewer-warning">Some potential findings were not shown because they could not be supported by the document evidence.</p>}
    {partial && <p className="viewer-warning">Review is partial or contains unsupported evidence. Do not rely on it as complete.</p>}
    {message && <p className="viewer-state" role="status">{message}</p>}
    {findings.length > 0 && <div className="analysis-findings"><div className="findings-heading"><div><h4>Provisional findings</h4><p>{findings.length} evidence-linked observation{findings.length === 1 ? "" : "s"} from this document.</p></div></div>{Object.entries(groups).map(([proposalType, entries]) => <section key={proposalType} className="analysis-finding-group"><h5>{proposalTypeLabel(proposalType)}</h5>{entries.map((finding) => <article key={finding.id}><strong>{findingFieldLabel(finding.fieldName)}</strong><span>{finding.unknown ? "Unknown" : String(finding.value ?? "Unknown")}</span>{finding.evidenceSnippet && <details className="finding-evidence"><summary>View supporting evidence</summary><small><b>{finding.sourceReference}</b>{finding.evidenceSnippet}</small></details>}</article>)}</section>)}</div>}
    {job?.status === "completed" && findings.length === 0 && <p className="viewer-state">No evidence-validated provisional findings were returned.</p>}
    <AnalysisQuestionPanel document={document} />
  </section>;
}

interface DocumentsScreenProps { requestedDocumentId?: string | null; onRequestedDocumentHandled?(): void; }

export default function DocumentsScreen({ requestedDocumentId = null, onRequestedDocumentHandled }: DocumentsScreenProps) {
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [message, setMessage] = useState("Loading imported documents…");
  const [activeDocument, setActiveDocument] = useState<DocumentSummary | null>(null);
  const [search, setSearch] = useState("");
  const [removing, setRemoving] = useState(false);
  const activeDocumentRef = useRef<HTMLElement>(null);
  const refresh = async () => { const response = await window.desktopAPI.listDocuments(); if (response.ok) { setDocuments(response.documents); setMessage(response.documents.length ? "" : "No documents have been imported yet."); } else setMessage("Imported documents could not be loaded. Please try again."); };
  useEffect(() => { void refresh(); }, []);
  useEffect(() => { if (!requestedDocumentId) return; const document = documents.find((item) => item.id === requestedDocumentId); if (document) { setActiveDocument(document); onRequestedDocumentHandled?.(); } }, [documents, onRequestedDocumentHandled, requestedDocumentId]);
  useEffect(() => {
    if (!activeDocument) return;
    const frame = window.requestAnimationFrame(() => {
      activeDocumentRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      activeDocumentRef.current?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeDocument?.id]);
  const removeActiveDocument = async () => {
    if (!activeDocument || removing) return;
    const confirmed = window.confirm(`Remove “${activeDocument.original_filename}” from this workspace?\n\nThis removes its local imported copy, deterministic extraction, and provisional AI results. Your original source file is not changed.`);
    if (!confirmed) return;
    setRemoving(true);
    try {
      const response = await window.desktopAPI.removeDocument(activeDocument.id);
      if (!response.ok) { setMessage(response.error.message); return; }
      setActiveDocument(null);
      await refresh();
      setMessage("Document removed from this workspace. The original file was not changed.");
    } catch {
      setMessage("The document could not be removed. Please try again.");
    } finally { setRemoving(false); }
  };
  const visibleDocuments = documents.filter((item) => item.original_filename.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  return <><header className="page-header documents-header"><div><p className="eyebrow">Documents</p><h1>Documents</h1><p className="page-subtitle">Choose a document to review its findings and ask focused questions.</p></div></header>
    <section className="documents-card document-library"><div className="library-heading"><div><h2>All documents</h2><p>{documents.length === 1 ? "1 imported document" : `${documents.length} imported documents`}</p></div><label className="document-search">Find a document<input type="search" value={search} placeholder="Search by file name" onChange={(event) => setSearch(event.target.value)} /></label></div><p className="storage-message" role="status">{message}</p>{visibleDocuments.map((item) => <button className={`document-row${activeDocument?.id === item.id ? " selected" : ""}`} type="button" key={item.id} onClick={() => setActiveDocument(item)}><span><strong>{item.original_filename}</strong><small>{item.file_format.toUpperCase()} · {formatBytes(item.file_size_bytes)} · Imported {new Date(item.imported_at).toLocaleDateString()}</small></span><span className="document-row-labels"><span className={`review-label ${item.ai_review_status}`}>{item.ai_review_status === "reviewed" ? "AI reviewed" : "New"}</span><span className={`document-status ${item.processing_status}`}>{item.processing_status}</span></span></button>)}{documents.length > 0 && visibleDocuments.length === 0 && <p className="dashboard-empty">No documents match that file name.</p>}</section>
    {activeDocument && <section className="documents-card analysis-workspace" ref={activeDocumentRef} tabIndex={-1}><div className="document-heading"><div><p className="section-label">Selected document</p><h2>{activeDocument.original_filename}</h2><p className="support-note">Findings remain provisional and link back to supporting evidence.</p></div><div className="document-actions"><span className={`document-status ${activeDocument.processing_status}`}>{activeDocument.processing_status}</span><button className="danger-button" type="button" disabled={removing} onClick={() => void removeActiveDocument()}>{removing ? "Removing…" : "Remove document"}</button></div></div><AIAnalysisPanel document={activeDocument} /></section>}
  </>;
}
