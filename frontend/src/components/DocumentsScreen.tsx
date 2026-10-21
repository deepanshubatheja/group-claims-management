import { useEffect, useMemo, useRef, useState } from "react";
import CitedSourcePanel from "./CitedSourcePanel";
import { analysisFailureMessage, analysisOverviewFromFindings, analysisValidationCounts, canStartDocumentAnalysis, conversationContextAnswer, findingFieldLabel, proposalTypeLabel } from "./analysis-ui";

function number(value: unknown, fallback = 0): number { return typeof value === "number" && Number.isFinite(value) ? value : fallback; }
function formatBytes(bytes: number): string { return bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`; }

function questionFailureMessage(code: string | null): string {
  if (code === "AI_AUTHORIZATION_WITHDRAWN") return "Cloud-processing authorization was withdrawn before this question could be sent.";
  if (code === "AI_ANSWER_EVIDENCE_UNSUPPORTED") return "The AI response did not provide locally supported evidence, so no answer was shown.";
  if (code === "AI_RESPONSE_CONTRACT_INVALID" || code === "AI_RESPONSE_JSON_INVALID") return "The AI provider did not return a usable cited answer. No answer was shown; try a more focused question.";
  if (code === "AI_OUTPUT_LIMIT_REACHED" || code === "AI_INCOMPLETE") return "The AI provider could not complete the concise cited answer. No answer was shown and the request was not retried automatically.";
  return analysisFailureMessage(code) ?? "The AI could not answer this question. No records were changed.";
}

type QuestionTurn = { id: string; question: string; answer: string | null; unknown: boolean; citations: DocumentQuestionCitation[]; usage: DocumentQuestionStatus["usage"]; parentId: string | null; };
type QuestionFollowUp = DocumentQuestionFollowUp & { id: string; };

function AnalysisQuestionPanel({ document }: { document: DocumentSummary }) {
  const [draft, setDraft] = useState("");
  const [conversationDraft, setConversationDraft] = useState("");
  const [preview, setPreview] = useState<DocumentQuestionPreview | null>(null);
  const [previewQuestion, setPreviewQuestion] = useState("");
  const [previewParentId, setPreviewParentId] = useState<string | null>(null);
  const [job, setJob] = useState<DocumentQuestionStatus | null>(null);
  const [message, setMessage] = useState("");
  const [checkingScope, setCheckingScope] = useState(false);
  const [pendingQuestion, setPendingQuestion] = useState("");
  const [pendingParentId, setPendingParentId] = useState<string | null>(null);
  const [turns, setTurns] = useState<QuestionTurn[]>([]);
  const [questionFollowUp, setQuestionFollowUp] = useState<QuestionFollowUp | null>(null);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);

  useEffect(() => { setDraft(""); setConversationDraft(""); setPreview(null); setPreviewQuestion(""); setPreviewParentId(null); setJob(null); setMessage(""); setCheckingScope(false); setPendingQuestion(""); setPendingParentId(null); setTurns([]); setQuestionFollowUp(null); setActiveConversationId(null); }, [document.id]);
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
      citations: job.citations, usage: job.usage, parentId: pendingParentId,
    }]);
  }, [job, pendingParentId, pendingQuestion]);

  const submitQuestion = async (question: string, followUp?: QuestionFollowUp) => {
    const trimmedQuestion = question.trim();
    if (!trimmedQuestion || job?.status === "running" || job?.status === "queued") return;
    setCheckingScope(true);
    setMessage("");
    try {
      const previewResponse = await window.desktopAPI.previewDocumentQuestion(document.id, trimmedQuestion, followUp);
      if (!previewResponse.ok) { setMessage(previewResponse.error.message); return; }
      setPreview(previewResponse.preview); setPreviewQuestion(trimmedQuestion); setPreviewParentId(followUp?.id ?? null);
      const ready = previewResponse.preview.globalAuthorization && previewResponse.preview.configured && previewResponse.preview.cost.withinLimit;
      if (!ready) return;
      const response = await window.desktopAPI.askDocumentQuestion(document.id, trimmedQuestion, followUp);
      if (response.ok) {
        setPendingQuestion(trimmedQuestion); setPendingParentId(followUp?.id ?? null); setJob(response.question);
        if (followUp) setConversationDraft(""); else setDraft("");
        setQuestionFollowUp(null); setPreview(null); setPreviewQuestion(""); setPreviewParentId(null);
      } else setMessage(response.error.message);
    } catch (error) {
      setMessage(error instanceof Error && error.message.trim() ? error.message : "AI Q&A is temporarily unavailable.");
    }
    finally { setCheckingScope(false); }
  };
  const asking = job?.status === "queued" || job?.status === "running";
  const activeQuestion = questionFollowUp ? conversationDraft.trim() : draft.trim();
  const currentPreview = previewQuestion === activeQuestion && previewParentId === (questionFollowUp?.id ?? null) && !checkingScope ? preview : null;
  const canSubmit = Boolean(currentPreview && currentPreview.globalAuthorization && currentPreview.configured && currentPreview.cost.withinLimit && !asking);
  const rootTurns = turns.filter((turn) => turn.parentId === null);
  const conversationTurns = useMemo(() => {
    if (!activeConversationId) return [];
    const included = new Set<string>([activeConversationId]);
    let changed = true;
    while (changed) {
      changed = false;
      for (const turn of turns) {
        if (turn.parentId && included.has(turn.parentId) && !included.has(turn.id)) { included.add(turn.id); changed = true; }
      }
    }
    return turns.filter((turn) => included.has(turn.id));
  }, [activeConversationId, turns]);
  const latestConversationTurn = conversationTurns[conversationTurns.length - 1] ?? null;
  const openConversation = (turn: QuestionTurn) => {
    setActiveConversationId(turn.parentId ?? turn.id); setConversationDraft(""); setQuestionFollowUp(null);
    setPreview(null); setPreviewQuestion(""); setPreviewParentId(null); setMessage("");
  };
  return <section className="analysis-question"><div><p className="section-label">Document Q&A</p><h4>Ask about this document</h4><p>Ask a focused question and receive a provisional answer supported by the document evidence.</p></div>
    <label className="question-field">Your question<textarea value={draft} maxLength={600} placeholder="For example: Which accounts are identified in this document?" onChange={(event) => { setDraft(event.target.value); setPreview(null); setPreviewQuestion(""); setPreviewParentId(null); setMessage(""); }} /></label>
    <div className="question-actions"><span>{draft.length}/600</span></div>
    {draft.trim() && <div className="question-consent"><p>When you choose Ask AI, ClaimFlow sends this question and bounded extracted excerpts to {currentPreview?.provider === "codex" ? "Codex through your ChatGPT account" : "the selected cloud AI provider"}. The original file is not uploaded. Answers are provisional and cite the local evidence used.</p>{!currentPreview && <p className="support-note">Ask AI checks the local answer scope before anything is sent.</p>}{currentPreview && <>{currentPreview.scope.truncated && <p className="viewer-warning">Only part of this document can be included in this question. The answer may not cover excluded material.</p>}{!currentPreview.globalAuthorization && <p className="viewer-warning">Enable Cloud AI in Settings before asking.</p>}{!currentPreview.cost.withinLimit && <p className="viewer-warning">This question cannot be sent within the current development safety limit.</p>}</>}<button className="primary-button" type="button" disabled={!draft.trim() || checkingScope || asking || (Boolean(currentPreview) && !canSubmit)} onClick={() => void submitQuestion(draft)}>{checkingScope ? "Checking…" : asking ? "Answering…" : "Ask AI"}</button></div>}
    {rootTurns.length > 0 && <div className="question-transcript">{rootTurns.map((turn) => <article className="question-answer" key={turn.id}><p className="section-label">Your question</p><p>{turn.question}</p><p className="section-label">Provisional answer</p>{turn.unknown || !turn.answer ? <p>The available evidence does not establish an answer to this question.</p> : <p>{turn.answer}</p>}{turn.citations.length > 0 && <details className="answer-evidence"><summary>Supporting evidence</summary>{turn.citations.map((citation) => <p key={`${citation.sourceReference}:${citation.evidenceSnippet}`} className="answer-citation"><span>{citation.sourceReference}</span>{citation.evidenceSnippet}</p>)}</details>}<button className="secondary-button conversation-button" type="button" onClick={() => openConversation(turn)}>Continue conversation</button></article>)}</div>}
    {job?.status === "failed" && <p className="viewer-error" role="alert">{questionFailureMessage(job.errorCode)}</p>}
    {message && <p className="viewer-state" role="status">{message}</p>}
    {activeConversationId && latestConversationTurn && <div className="conversation-overlay" role="presentation" onMouseDown={() => setActiveConversationId(null)}><section className="conversation-dialog" role="dialog" aria-modal="true" aria-label="Document question conversation" onMouseDown={(event) => event.stopPropagation()}><header><div><p className="section-label">Conversation</p><h2>Follow up on this answer</h2><p>This document remains in scope. Every answer must cite local document evidence.</p></div><button className="text-button" type="button" onClick={() => setActiveConversationId(null)}>Close</button></header><div className="conversation-messages">{conversationTurns.map((turn) => <article key={turn.id} className={`conversation-message${turn.parentId ? " follow-up" : ""}`}><p className="section-label">{turn.parentId ? "Follow-up question" : "Your question"}</p><p>{turn.question}</p><p className="section-label">Provisional answer</p>{turn.unknown || !turn.answer ? <p>The available evidence does not establish an answer to this question.</p> : <p>{turn.answer}</p>}{turn.citations.length > 0 && <details className="answer-evidence"><summary>Supporting evidence</summary>{turn.citations.map((citation) => <p key={`${citation.sourceReference}:${citation.evidenceSnippet}`} className="answer-citation"><span>{citation.sourceReference}</span>{citation.evidenceSnippet}</p>)}</details>}</article>)}</div><footer className="conversation-composer"><label>Ask a follow-up<textarea value={conversationDraft} maxLength={600} placeholder="For example: What source supports that conclusion?" onChange={(event) => { setConversationDraft(event.target.value); setQuestionFollowUp(null); setPreview(null); setPreviewQuestion(""); setPreviewParentId(null); setMessage(""); }} /></label><small>{conversationDraft.length}/600</small>{conversationDraft.trim() && <><p className="support-note">Ask AI checks the local answer scope before anything is sent.</p>{currentPreview && <>{currentPreview.scope.truncated && <p className="viewer-warning">Only part of this document can be included in this follow-up.</p>}{!currentPreview.globalAuthorization && <p className="viewer-warning">Enable Cloud AI in Settings before asking.</p>}{!currentPreview.cost.withinLimit && <p className="viewer-warning">This question cannot be sent within the current development safety limit.</p>}</>}<button className="primary-button" type="button" disabled={!conversationDraft.trim() || checkingScope || asking || (Boolean(currentPreview) && !canSubmit)} onClick={() => { const answer = conversationContextAnswer(latestConversationTurn.answer, "The available evidence does not establish an answer to this question."); const followUp = { id: latestConversationTurn.id, question: latestConversationTurn.question, answer }; void submitQuestion(conversationDraft, followUp); }}>{checkingScope ? "Checking…" : asking ? "Answering…" : "Ask AI"}</button>{job?.status === "failed" && <p className="viewer-error" role="alert">{questionFailureMessage(job.errorCode)}</p>}{message && <p className="viewer-error" role="alert">{message}</p>}</>}</footer></section></div>}
  </section>;
}

function AIAnalysisPanel({ document }: { document: DocumentSummary }) {
  const [preview, setPreview] = useState<DocumentAnalysisPreview | null>(null);
  const [job, setJob] = useState<DocumentAnalysisJob | null>(null);
  const [findings, setFindings] = useState<DocumentAnalysisFinding[]>([]);
  const [message, setMessage] = useState("Checking AI analysis availability…");
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    let active = true;
    setJob(null); setFindings([]); setPreview(null); setMessage("Checking AI analysis availability…"); setStarting(false);
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
    {preview && findings.length === 0 && <div className="analysis-setup"><p>Review this document with AI</p><p>Choosing Analyze document sends bounded extracted text to {preview.provider === "codex" ? "Codex through your ChatGPT account" : "the selected cloud AI provider"}. The original file remains in this workspace. Results are provisional and must be reviewed against cited evidence.</p>{preview.scope.truncated && <p className="viewer-warning">This review will cover only part of the document.</p>}{!preview.globalAuthorization && <p className="viewer-warning">Enable Cloud AI in Settings before starting.</p>}<button className="primary-button" type="button" disabled={starting || !canStartDocumentAnalysis(preview.globalAuthorization, preview.configured, preview.cost.withinLimit, job?.status ?? null)} onClick={() => void start()}>{starting ? "Starting analysis…" : "Analyze document"}</button></div>}
    {preview && findings.length > 0 && <details className="analysis-repeat"><summary>Run a new AI review</summary><p>Choosing this action sends bounded extracted text to the selected cloud AI provider again. Earlier findings remain available for inspection.</p><button className="secondary-button" type="button" disabled={starting || !canStartDocumentAnalysis(preview.globalAuthorization, preview.configured, preview.cost.withinLimit, job?.status ?? null)} onClick={() => void start()}>{starting ? "Starting analysis…" : "Run new analysis"}</button></details>}
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

interface DocumentsScreenProps { requestedDocumentId?: string | null; onRequestedDocumentHandled?(): void; citedSource?: { documentId: string; sourceReference: string } | null; onCloseCitedSource?(): void; }

export default function DocumentsScreen({ requestedDocumentId = null, onRequestedDocumentHandled, citedSource = null, onCloseCitedSource }: DocumentsScreenProps) {
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [message, setMessage] = useState("Loading imported documents…");
  const [activeDocument, setActiveDocument] = useState<DocumentSummary | null>(null);
  const [search, setSearch] = useState("");
  const [removing, setRemoving] = useState(false);
  const [retryingDocumentId, setRetryingDocumentId] = useState<string | null>(null);
  const [selectedDocumentIds, setSelectedDocumentIds] = useState<string[]>([]);
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
  const removeDocuments = async (documentIds: string[]) => {
    if (documentIds.length === 0 || removing) return;
    const selectedDocuments = documents.filter((document) => documentIds.includes(document.id));
    const confirmed = window.confirm(`Remove ${selectedDocuments.length === 1 ? `“${selectedDocuments[0]?.original_filename}”` : `${selectedDocuments.length} documents`} from this workspace?\n\nThis removes local imported copies, deterministic extractions, and provisional AI results. Original source files are not changed.`);
    if (!confirmed) return;
    setRemoving(true);
    try {
      const results = await Promise.all(documentIds.map((documentId) => window.desktopAPI.removeDocument(documentId)));
      const failed = results.filter((response): response is DocumentFailure => !response.ok);
      const removedIds = new Set(documentIds.filter((_documentId, index) => results[index]?.ok));
      setSelectedDocumentIds((current) => current.filter((documentId) => !removedIds.has(documentId)));
      if (activeDocument && removedIds.has(activeDocument.id)) setActiveDocument(null);
      await refresh();
      setMessage(failed.length ? `${removedIds.size} document${removedIds.size === 1 ? " was" : "s were"} removed. ${failed.length} could not be removed: ${failed[0]?.error.message}` : `${removedIds.size} document${removedIds.size === 1 ? " was" : "s were"} removed from this workspace. Original source files were not changed.`);
    } catch {
      setMessage("The selected documents could not be removed. Please try again.");
    } finally { setRemoving(false); }
  };
  const retryParsing = async (documentId: string) => {
    if (retryingDocumentId) return;
    setRetryingDocumentId(documentId);
    setMessage("Parsing the saved document copy…");
    try {
      const response = await window.desktopAPI.retryDocumentParsing(documentId);
      if (response.ok) {
        setActiveDocument(response.document);
        await refresh();
        setMessage(response.document.processing_status === "parsed" ? "Document parsed successfully." : `Parsing finished with status: ${response.document.processing_status}.`);
      } else setMessage(response.error.message);
    } catch {
      setMessage("The document could not be parsed. Please try again.");
    } finally { setRetryingDocumentId(null); }
  };
  const visibleDocuments = documents.filter((item) => item.original_filename.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()));
  const selectedVisibleIds = visibleDocuments.filter((document) => selectedDocumentIds.includes(document.id)).map((document) => document.id);
  const allVisibleSelected = visibleDocuments.length > 0 && selectedVisibleIds.length === visibleDocuments.length;
  const toggleDocumentSelection = (documentId: string) => setSelectedDocumentIds((current) => current.includes(documentId) ? current.filter((id) => id !== documentId) : [...current, documentId]);
  const toggleVisibleSelection = () => setSelectedDocumentIds((current) => {
    const selected = new Set(current);
    if (allVisibleSelected) visibleDocuments.forEach((document) => selected.delete(document.id));
    else visibleDocuments.forEach((document) => selected.add(document.id));
    return [...selected];
  });
  return <><header className="page-header documents-header"><div><p className="eyebrow">Documents</p><h1>Documents</h1><p className="page-subtitle">Choose a document to review its findings and ask focused questions.</p></div></header>
    <section className="documents-card document-library"><div className="library-heading"><div><h2>All documents</h2><p>{documents.length === 1 ? "1 imported document" : `${documents.length} imported documents`}</p></div><label className="document-search">Find a document<input type="search" value={search} placeholder="Search by file name" onChange={(event) => setSearch(event.target.value)} /></label></div>{visibleDocuments.length > 0 && <div className="document-bulk-actions"><label className="select-all-control"><input type="checkbox" checked={allVisibleSelected} onChange={toggleVisibleSelection} /><span>Select all shown</span></label>{selectedDocumentIds.length > 0 && <button className="danger-button" type="button" disabled={removing} onClick={() => void removeDocuments(selectedDocumentIds)}>{removing ? "Removing…" : `Remove selected (${selectedDocumentIds.length})`}</button>}</div>}<p className="storage-message" role="status">{message}</p>{visibleDocuments.map((item) => <div className={`document-row${activeDocument?.id === item.id ? " selected" : ""}`} key={item.id}><label className="document-select" aria-label={`Select ${item.original_filename}`}><input type="checkbox" checked={selectedDocumentIds.includes(item.id)} onChange={() => toggleDocumentSelection(item.id)} /></label><button type="button" onClick={() => setActiveDocument(item)}><span><strong>{item.original_filename}</strong><small>{item.file_format.toUpperCase()} · {formatBytes(item.file_size_bytes)} · Imported {new Date(item.imported_at).toLocaleDateString()}</small></span><span className="document-row-labels"><span className={`review-label ${item.ai_review_status}`}>{item.ai_review_status === "reviewed" ? "AI reviewed" : "New"}</span><span className={`document-status ${item.processing_status}`}>{item.processing_status}</span></span></button></div>)}{documents.length > 0 && visibleDocuments.length === 0 && <p className="dashboard-empty">No documents match that file name.</p>}</section>
    {activeDocument && <section className="documents-card analysis-workspace" ref={activeDocumentRef} tabIndex={-1}><div className="document-heading"><div><p className="section-label">Selected document</p><h2>{activeDocument.original_filename}</h2><p className="support-note">Findings remain provisional and link back to supporting evidence.</p></div><div className="document-actions"><span className={`document-status ${activeDocument.processing_status}`}>{activeDocument.processing_status}</span>{(activeDocument.processing_status === "imported" || activeDocument.processing_status === "failed") && <button className="secondary-button" type="button" disabled={retryingDocumentId !== null || removing} onClick={() => void retryParsing(activeDocument.id)}>{retryingDocumentId === activeDocument.id ? "Parsing…" : "Retry parsing"}</button>}<button className="danger-button" type="button" disabled={removing || retryingDocumentId !== null} onClick={() => void removeDocuments([activeDocument.id])}>{removing ? "Removing…" : "Remove document"}</button></div></div>{citedSource && citedSource.documentId === activeDocument.id && <CitedSourcePanel documentId={citedSource.documentId} sourceReference={citedSource.sourceReference} onClose={() => onCloseCitedSource?.()} />}<AIAnalysisPanel document={activeDocument} /></section>}
  </>;
}
