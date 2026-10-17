import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { conversationContextAnswer } from "./analysis-ui";

function matterQuestionFailure(code: string | null): string {
  if (code === "AI_AUTHORIZATION_WITHDRAWN") return "Cloud-processing authorization was withdrawn before this question could be sent.";
  if (code === "INVALID_AI_REQUEST") return "The selected analyses could not be prepared as a locally citable question. Refresh the review and try again.";
  if (code === "AI_ANSWER_EVIDENCE_UNSUPPORTED") return "The response did not include evidence supported by the selected documents.";
  if (code === "AI_OUTPUT_LIMIT_REACHED" || code === "AI_INCOMPLETE") return "The response could not be completed within the configured answer limit.";
  return "The AI could not answer this question. No records were changed.";
}

function requestFailure(error: unknown, fallback: string): string {
  return error instanceof Error && error.message.trim() ? error.message : fallback;
}

function elapsedTime(createdAt: string): string {
  const started = Date.parse(createdAt);
  if (Number.isNaN(started)) return "Timing unavailable";
  const seconds = Math.max(0, Math.floor((Date.now() - started) / 1000));
  if (seconds < 60) return `${seconds}s elapsed`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s elapsed`;
}

type MatterQuestionTurn = {
  id: string;
  question: string;
  answer: string | null;
  unknown: boolean;
  citations: MatterQuestionCitation[];
  parentId: string | null;
};

type MatterFollowUp = { id: string; question: string; answer: string; };

interface MatterReviewScreenProps { isActive: boolean; }

export default function MatterReviewScreen({ isActive }: MatterReviewScreenProps) {
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [review, setReview] = useState<MatterReviewResult | null>(null);
  const [message, setMessage] = useState("Loading available documents…");
  const [loadingReview, setLoadingReview] = useState(false);
  const [batchPreview, setBatchPreview] = useState<MatterAnalysisBatchPreview | null>(null);
  const [batch, setBatch] = useState<MatterAnalysisBatch | null>(null);
  // Global Cloud AI is enabled once in Settings.  An Analyze or Ask AI click
  // remains the explicit action for its bounded scope, so a second checkbox
  // here would be repetitive rather than an additional safeguard.
  const [batchLoading, setBatchLoading] = useState(false);
  const [batchMessage, setBatchMessage] = useState("");
  const [questionDraft, setQuestionDraft] = useState("");
  const [questionPreview, setQuestionPreview] = useState<MatterQuestionPreview | null>(null);
  const [questionPreviewText, setQuestionPreviewText] = useState("");
  const [questionPreviewFollowUpId, setQuestionPreviewFollowUpId] = useState<string | null>(null);
  const questionAuthorized = true;
  const setQuestionAuthorized = (_value: boolean) => undefined;
  const [questionJob, setQuestionJob] = useState<MatterQuestionStatus | null>(null);
  const [questionPendingText, setQuestionPendingText] = useState("");
  const [questionPendingParentId, setQuestionPendingParentId] = useState<string | null>(null);
  const [questionTurns, setQuestionTurns] = useState<MatterQuestionTurn[]>([]);
  const [questionFollowUp, setQuestionFollowUp] = useState<MatterFollowUp | null>(null);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [conversationDraft, setConversationDraft] = useState("");
  const [questionLoading, setQuestionLoading] = useState(false);
  const [questionMessage, setQuestionMessage] = useState("");
  const knownDocumentIds = useRef(new Set<string>());
  const preserveScrollY = useRef<number | null>(null);
  const automaticReviewSelection = useRef<string | null>(null);
  const [batchReviewDocumentIds, setBatchReviewDocumentIds] = useState<string[]>([]);

  const refreshAvailableDocuments = useCallback(async () => {
    try {
      const response = await window.desktopAPI.listDocuments();
      if (!response.ok) { setMessage("Documents could not be loaded."); return; }
      const parsed = response.documents.filter((document) => document.processing_status === "parsed");
      const newlyAvailable = parsed.filter((document) => !knownDocumentIds.current.has(document.id) && document.ai_review_status === "new").map((document) => document.id);
      knownDocumentIds.current = new Set(parsed.map((document) => document.id));
      setDocuments(parsed);
      setSelectedIds((current) => {
        const available = new Set(parsed.map((document) => document.id));
        return [...new Set([...current.filter((documentId) => available.has(documentId)), ...newlyAvailable])].slice(0, 20);
      });
      setMessage(parsed.length ? "" : "No parsed documents are available for review.");
    } catch { setMessage("Documents could not be loaded."); }
  }, []);

  useEffect(() => { if (isActive) void refreshAvailableDocuments(); }, [isActive, refreshAvailableDocuments]);

  const selected = useMemo(() => new Set(selectedIds), [selectedIds]);
  const selectableDocumentIds = useMemo(() => documents.slice(0, 20).map((document) => document.id), [documents]);
  const allSelectableDocumentsSelected = selectableDocumentIds.length > 0 && selectableDocumentIds.every((documentId) => selected.has(documentId));
  const selectedNewDocumentIds = useMemo(
    () => selectedIds.filter((documentId) => documents.find((document) => document.id === documentId)?.ai_review_status === "new"),
    [documents, selectedIds],
  );
  const selectedReviewedDocumentCount = selectedIds.length - selectedNewDocumentIds.length;
  const documentNames = useMemo(() => new Map((review?.documents ?? []).map((document) => [document.id, document.filename])), [review]);
  const batchRunning = Boolean(batch && ["queued", "running"].includes(batch.status));

  const loadReview = useCallback(async (documentIds = selectedIds) => {
    if (documentIds.length === 0) { setMessage("Select at least one parsed document."); return; }
    setLoadingReview(true); setMessage("");
    try {
      const response = await window.desktopAPI.getMatterReview(documentIds);
      if (!response.ok) { setMessage(response.error.message); return; }
      setReview(response.review);
    } catch { setMessage("The consolidated review is temporarily unavailable."); }
    finally { setLoadingReview(false); }
  }, [selectedIds]);

  useEffect(() => {
    const hasCompletedReview = documents.some((document) => selected.has(document.id) && document.ai_review_status === "reviewed");
    const selectionKey = selectedIds.join("|");
    if (isActive && hasCompletedReview && review === null && !loadingReview && !batchRunning && automaticReviewSelection.current !== selectionKey) {
      automaticReviewSelection.current = selectionKey;
      void loadReview();
    }
  }, [batchRunning, documents, isActive, loadReview, loadingReview, review, selected, selectedIds]);

  // Completion adds summary content below the action. Preserve the current
  // reading position rather than allowing focus/DOM growth to move the page.
  useLayoutEffect(() => {
    if (preserveScrollY.current === null || review === null) return;
    window.scrollTo({ top: preserveScrollY.current, behavior: "auto" });
    preserveScrollY.current = null;
  }, [review]);

  const clearReviewState = () => {
    setReview(null); setBatchPreview(null); setBatch(null); setBatchMessage("");
    setBatchReviewDocumentIds([]);
    automaticReviewSelection.current = null;
    setQuestionPreview(null); setQuestionPreviewText(""); setQuestionPreviewFollowUpId(null); setQuestionAuthorized(false); setQuestionJob(null);
    setQuestionPendingText(""); setQuestionPendingParentId(null); setQuestionTurns([]); setQuestionFollowUp(null); setActiveConversationId(null); setConversationDraft(""); setQuestionMessage("");
  };
  const toggleDocument = (documentId: string) => {
    if (!selected.has(documentId) && selectedIds.length >= 20) {
      setMessage("Select up to 20 documents for one consolidated review.");
      return;
    }
    setSelectedIds((current) => current.includes(documentId) ? current.filter((id) => id !== documentId) : [...current, documentId]);
    clearReviewState();
  };
  const toggleAllDocuments = () => {
    setSelectedIds(allSelectableDocumentsSelected ? [] : selectableDocumentIds);
    clearReviewState();
  };

  const analyzeNewSelectedDocuments = async () => {
    if (selectedIds.length === 0) { setBatchMessage("Select at least one parsed document."); return; }
    const documentsToReview = selectedNewDocumentIds;
    if (documentsToReview.length === 0) { setBatchMessage("All selected documents already have an AI review for their current extraction. Their summaries and Q&A remain available below."); return; }
    if (documentsToReview.length > 20) { setBatchMessage("Select up to 20 documents for one analysis batch."); return; }
    setBatchLoading(true); setBatchMessage(""); setBatch(null);
    try {
      const response = await window.desktopAPI.previewMatterAnalysisBatch(documentsToReview);
      if (!response.ok) { setBatchMessage(response.error.message); return; }
      // Analyze selected documents is the user's explicit authorization for
      // this bounded batch.  When the local preflight is ready, start it in
      // the same action; keep preflight details on screen only when a setting
      // or cost limit prevents a transfer.
      if (!response.preview.ready) { setBatchPreview(response.preview); return; }
      const startResponse = await window.desktopAPI.startMatterAnalysisBatch(documentsToReview);
      if (!startResponse.ok) { setBatchMessage(startResponse.error.message); return; }
      setBatchReviewDocumentIds(selectedIds);
      setBatch(startResponse.batch);
    } catch (error) { setBatchMessage(requestFailure(error, "The analysis plan is temporarily unavailable.")); }
    finally { setBatchLoading(false); }
  };

  useEffect(() => {
    if (!batch || !["queued", "running"].includes(batch.status)) return;
    const timer = window.setTimeout(() => {
      void window.desktopAPI.getMatterAnalysisBatchStatus(batch.batchId).then((response) => {
        if (!response.ok) { setBatchMessage(response.error.message); return; }
        if (["completed", "partial"].includes(response.batch.status)) preserveScrollY.current = window.scrollY;
        setBatch(response.batch);
        if (["completed", "partial"].includes(response.batch.status)) {
          const reviewedDocuments = batchReviewDocumentIds.length > 0 ? batchReviewDocumentIds : response.batch.documents.map((document) => document.documentId);
          void loadReview(reviewedDocuments).finally(() => { void refreshAvailableDocuments(); });
        }
      }).catch(() => setBatchMessage("The analysis status is temporarily unavailable."));
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [batch, loadReview, refreshAvailableDocuments]);

  useEffect(() => {
    if (!questionJob || !["queued", "running"].includes(questionJob.status)) return;
    const timer = window.setTimeout(() => {
      void window.desktopAPI.getMatterQuestionStatus(questionJob.questionId).then((response) => {
        if (!response.ok) { setQuestionMessage(response.error.message); return; }
        setQuestionJob(response.question);
      }).catch(() => setQuestionMessage("The Q&A status is temporarily unavailable."));
    }, 900);
    return () => window.clearTimeout(timer);
  }, [questionJob]);
  useEffect(() => {
    if (!questionJob || questionJob.status !== "completed") return;
    setQuestionTurns((current) => current.some((turn) => turn.id === questionJob.questionId) ? current : [...current, {
      id: questionJob.questionId, question: questionPendingText, answer: questionJob.answer,
      unknown: questionJob.unknown, citations: questionJob.citations, parentId: questionPendingParentId,
    }]);
    setQuestionFollowUp(null); setQuestionAuthorized(false); setQuestionPreview(null); setQuestionPreviewText(""); setQuestionPreviewFollowUpId(null);
  }, [questionJob, questionPendingParentId, questionPendingText]);

  const activeQuestionText = questionFollowUp ? conversationDraft.trim() : questionDraft.trim();
  const prepareQuestion = useCallback(async (
    questionOverride?: string, followUpOverride?: MatterFollowUp | null, submitWhenReady = false,
  ) => {
    // The root composer is the only caller without an explicit question.
    // Conversation planning always passes its current draft explicitly so a
    // follow-up state update cannot cause another planning cycle.
    const question = (questionOverride ?? questionDraft).trim();
    const followUp = followUpOverride === undefined ? questionFollowUp : followUpOverride;
    if (!question || selectedIds.length === 0) return;
    setQuestionLoading(true); setQuestionMessage(""); setQuestionAuthorized(false);
    try {
      const response = await window.desktopAPI.previewMatterQuestion(selectedIds, question, followUp ? { question: followUp.question, answer: followUp.answer } : undefined);
      if (!response.ok) { setQuestionMessage(response.error.message); return; }
      setQuestionPreview(response.preview); setQuestionPreviewText(question); setQuestionPreviewFollowUpId(followUp?.id ?? null);
      // A deliberate Ask AI click checks the local scope first, then submits
      // only if the resulting request is ready. Typing never reaches here.
      const readyToSubmit = response.preview.localOnly || (
        response.preview.globalAuthorization
        && response.preview.configured
        && response.preview.cost.withinLimit
      );
      if (submitWhenReady && readyToSubmit) {
        const answerResponse = await window.desktopAPI.askMatterQuestion(
          selectedIds, question, followUp ? { question: followUp.question, answer: followUp.answer } : undefined,
        );
        if (!answerResponse.ok) { setQuestionMessage(answerResponse.error.message); return; }
        setQuestionPendingText(question); setQuestionPendingParentId(followUp?.id ?? null); setQuestionJob(answerResponse.question);
        if (followUp) setConversationDraft("");
        else setQuestionDraft("");
        setQuestionFollowUp(null); setQuestionPreview(null); setQuestionPreviewText(""); setQuestionPreviewFollowUpId(null); setQuestionAuthorized(false);
      }
    } catch (error) { setQuestionMessage(requestFailure(error, "The Q&A plan is temporarily unavailable.")); }
    finally { setQuestionLoading(false); }
  }, [questionDraft, selectedIds]);
  const askQuestion = async () => {
    const question = activeQuestionText;
    if (!questionPreview || questionPreviewText !== question || questionPreviewFollowUpId !== (questionFollowUp?.id ?? null) || !questionAuthorized) return;
    setQuestionLoading(true); setQuestionMessage("");
    try {
      const response = await window.desktopAPI.askMatterQuestion(selectedIds, question, questionFollowUp ? { question: questionFollowUp.question, answer: questionFollowUp.answer } : undefined);
      if (!response.ok) { setQuestionMessage(response.error.message); return; }
      setQuestionPendingText(question); setQuestionPendingParentId(questionFollowUp?.id ?? null); setQuestionJob(response.question);
      // A submitted follow-up belongs in the conversation transcript, not in
      // the composer. Clear it immediately so the field is ready for the
      // next turn while the answer is being prepared.
      if (questionFollowUp) setConversationDraft("");
      else setQuestionDraft("");
      setQuestionFollowUp(null); setQuestionPreview(null); setQuestionPreviewText(""); setQuestionPreviewFollowUpId(null); setQuestionAuthorized(false);
    } catch (error) { setQuestionMessage(requestFailure(error, "The Q&A is temporarily unavailable.")); }
    finally { setQuestionLoading(false); }
  };
  const updateQuestion = (value: string) => { setQuestionFollowUp(null); setQuestionDraft(value); setQuestionAuthorized(false); setQuestionMessage(""); };
  const openConversation = (turn: MatterQuestionTurn) => {
    const rootId = turn.parentId ?? turn.id;
    setActiveConversationId(rootId); setConversationDraft(""); setQuestionFollowUp(null); setQuestionPreview(null); setQuestionPreviewText(""); setQuestionPreviewFollowUpId(null); setQuestionAuthorized(false); setQuestionMessage("");
  };
  const closeConversation = () => {
    setActiveConversationId(null); setConversationDraft(""); setQuestionFollowUp(null); setQuestionPreview(null); setQuestionPreviewText(""); setQuestionPreviewFollowUpId(null); setQuestionAuthorized(false);
  };
  const currentQuestionPreview = questionPreviewText === activeQuestionText && questionPreviewFollowUpId === (questionFollowUp?.id ?? null) ? questionPreview : null;
  const askingQuestion = questionLoading || questionJob?.status === "queued" || questionJob?.status === "running";
  const canAskQuestion = Boolean(currentQuestionPreview && questionAuthorized && (currentQuestionPreview.localOnly || (currentQuestionPreview.globalAuthorization && currentQuestionPreview.configured && currentQuestionPreview.cost.withinLimit)) && !askingQuestion);
  const conversationTurns = useMemo(() => {
    if (!activeConversationId) return [];
    const included = new Set<string>([activeConversationId]);
    let changed = true;
    while (changed) {
      changed = false;
      for (const turn of questionTurns) {
        if (turn.parentId && included.has(turn.parentId) && !included.has(turn.id)) { included.add(turn.id); changed = true; }
      }
    }
    return questionTurns.filter((turn) => included.has(turn.id));
  }, [activeConversationId, questionTurns]);
  const latestConversationTurn = conversationTurns[conversationTurns.length - 1] ?? null;

  return <>
    <header className="page-header"><div><p className="eyebrow">Matter review</p><h1>Review a matter</h1><p className="page-subtitle">Review concise document summaries, then ask questions across the selected material.</p></div></header>
    <section className="matter-notice"><strong>For review only</strong><p>AI answers are provisional. They do not confirm legal entitlement, claim transfers, or billing responsibility.</p></section>
    <section className="documents-card matter-selection">
      <div><div><p className="section-label">Documents</p><h2>Select documents</h2><p>Choose up to 20 parsed documents to review together. {selectedIds.length > 0 && `${selectedReviewedDocumentCount} reviewed · ${selectedNewDocumentIds.length} new.`}</p></div><div className="matter-selection-actions"><label className="select-all-control"><input type="checkbox" checked={allSelectableDocumentsSelected} disabled={documents.length === 0} onChange={toggleAllDocuments} /><span>Select all{documents.length > 20 ? " (first 20)" : ""}</span></label><button className="secondary-button" type="button" disabled={loadingReview || selectedIds.length === 0} onClick={() => void loadReview()}>{loadingReview ? "Refreshing…" : "Refresh reviewed summaries"}</button></div></div>
      {documents.map((document) => <label className="matter-document" key={document.id}><input type="checkbox" checked={selected.has(document.id)} onChange={() => toggleDocument(document.id)} /><span><strong>{document.original_filename}</strong><small>{document.file_format.toUpperCase()} · imported {new Date(document.imported_at).toLocaleDateString()}</small></span><span className={`review-label ${document.ai_review_status}`}>{document.ai_review_status === "reviewed" ? "AI reviewed" : "New"}</span></label>)}
      {message && <p className="viewer-state" role="status">{message}</p>}
      <div className="matter-batch-action"><p className="section-label">AI review</p>
        {!batchPreview && <button className="primary-button" type="button" disabled={batchLoading || selectedNewDocumentIds.length === 0 || batchRunning} onClick={() => void analyzeNewSelectedDocuments()}>{batchLoading ? "Starting analysis…" : `Analyze ${selectedNewDocumentIds.length} new selected document${selectedNewDocumentIds.length === 1 ? "" : "s"}`}</button>}
        {batchPreview && <><p>One bounded AI request will be made for each selected document. No automatic retries are used.</p><p>{batchPreview.estimatedCostUsd === null ? "Codex uses your connected ChatGPT plan. Plan usage limits apply; no USD API estimate is available." : `Estimated total: USD ${batchPreview.estimatedCostUsd} (batch limit: USD ${batchPreview.costLimitUsd}). This is an estimate, not a bill.`}</p>
          {!batchPreview.globalAuthorization && <p className="viewer-warning">Cloud processing is disabled. Enable it in Settings before starting.</p>}{!batchPreview.configured && <p className="viewer-warning">Connect the AI provider in Settings.</p>}{!batchPreview.withinCostLimit && <p className="viewer-warning">The combined estimate exceeds the development batch limit; no content will be sent.</p>}
          {batchPreview.documents.filter((document) => !document.ready).map((document) => <p className="viewer-warning" key={document.documentId}>{document.filename} cannot be included ({document.errorCode ?? "analysis preparation unavailable"}).</p>)}
          <p className="support-note">Analysis did not start because the local preflight needs attention. Original files remain local until the issue above is resolved.</p>
          <button className="text-button" type="button" disabled={batchLoading || batchRunning} onClick={() => { setBatchPreview(null); }}>Change selection</button></>}
        {batch && <div className="matter-batch-progress" role="status"><strong>{batch.status === "completed" ? "Analysis complete" : batch.status === "partial" ? "Analysis completed with unavailable documents" : <><span className="loading-spinner" aria-hidden="true" />Analyzing selected documents</>}</strong><p>{batch.summary.completed} of {batch.summary.total} document{batch.summary.total === 1 ? "" : "s"} complete; {batch.summary.failed} failed; {batch.summary.pending} remaining.</p>{batchRunning && <p className="batch-timing">One document is processed at a time · {elapsedTime(batch.createdAt)}</p>}{batch.documents.filter((document) => document.status === "failed" || document.status === "blocked").map((document) => <p className="viewer-warning" key={document.documentId}>{document.filename}: {document.errorCode ?? "analysis unavailable"}</p>)}</div>}
        {batchMessage && <p className="viewer-error" role="alert">{batchMessage}</p>}
      </div>
    </section>
    {review && <>
      <section className="documents-card matter-outcomes"><div><p className="section-label">Document summaries</p><h2>What the selected documents show</h2><p>{review.summary.analyzedDocuments} of {review.summary.selectedDocuments} selected document{review.summary.selectedDocuments === 1 ? " has" : "s have"} a completed AI review.</p></div>
        {review.summary.documentsWithoutCompletedAnalysis.length > 0 && <p className="viewer-warning">{review.summary.documentsWithoutCompletedAnalysis.length} selected document{review.summary.documentsWithoutCompletedAnalysis.length === 1 ? " has" : "s have"} not been reviewed yet. Use “Analyze {selectedNewDocumentIds.length} new selected document{selectedNewDocumentIds.length === 1 ? "" : "s"}” above; refresh only reloads existing summaries.</p>}{review.summary.findingsTruncated && <p className="viewer-warning">Some source material was outside the bounded analysis. Ask a focused question to review what was included.</p>}
        {review.summaries.length > 0 && <div className="matter-summaries">{review.summaries.map((summary) => <article key={summary.documentId}><p className="section-label">{documentNames.get(summary.documentId) ?? "Document"}</p><strong>Summary</strong><p>{summary.unknown || !summary.text ? "The reviewed material did not establish a concise supported summary." : summary.text}</p>{summary.citations.length > 0 && <details className="answer-evidence"><summary>Supporting evidence</summary>{summary.citations.map((citation) => <p className="answer-citation" key={`${summary.documentId}:${citation.sourceReference}`}><span>{documentNames.get(summary.documentId) ?? "Document"} · {citation.sourceReference}</span>{citation.evidenceSnippet}</p>)}</details>}</article>)}</div>}
      </section>
      {review.summary.analyzedDocuments > 0 && <section className="documents-card matter-question"><div><p className="section-label">Ask a question</p><h3>Ask about the selected documents</h3><p>Answers use locally cited evidence, including question-relevant source excerpts. A holdings movement is kept separate from an explicit change to litigation participation or rights.</p></div>
        {!questionFollowUp && <><label className="question-field">Your question<textarea value={questionDraft} maxLength={600} placeholder="For example: What changes, transfers, or unresolved rights are identified across these documents?" onChange={(event) => updateQuestion(event.target.value)} /></label><small>{questionDraft.length}/600</small>
          {questionDraft.trim() && <div className="question-consent"><p>{currentQuestionPreview?.localOnly ? "This document-backed calculation runs locally. No content will be sent to cloud AI." : `Choosing Ask AI sends your question and bounded, question-relevant extracted excerpts, summaries, and findings to ${currentQuestionPreview?.provider === "codex" ? "Codex through your ChatGPT account" : "the selected cloud AI provider"}. Original files are not uploaded.`}</p>{!currentQuestionPreview && <p className="support-note">Ask AI checks the local scope before anything is sent.</p>}{currentQuestionPreview && <>{currentQuestionPreview.scope.truncated && <p className="viewer-warning">Only part of the selected evidence can be included in this question.</p>}{currentQuestionPreview.scope.comparisonCoverageComplete === false && <p className="viewer-warning">This comparison currently has evidence from {currentQuestionPreview.scope.documentsWithEvidence ?? 0} of {currentQuestionPreview.scope.documentsSelected ?? selectedIds.length} selected documents. It cannot be presented as a complete all-document comparison.</p>}{!currentQuestionPreview.localOnly && !currentQuestionPreview.globalAuthorization && <p className="viewer-warning">Enable Cloud AI in Settings before asking.</p>}{!currentQuestionPreview.localOnly && !currentQuestionPreview.cost.withinLimit && <p className="viewer-warning">This question exceeds the current development safety limit.</p>}</>}<button className="primary-button" type="button" disabled={askingQuestion || !questionDraft.trim() || (Boolean(currentQuestionPreview) && !canAskQuestion)} onClick={() => { if (!currentQuestionPreview) void prepareQuestion(undefined, undefined, true); else void askQuestion(); }}>{questionLoading ? "Preparing…" : askingQuestion ? "Answering…" : "Ask AI"}</button></div>}</>}
        {questionTurns.some((turn) => turn.parentId === null) && <div className="question-transcript">{questionTurns.filter((turn) => turn.parentId === null).map((turn) => <article className="question-answer" key={turn.id}><p className="section-label">Your question</p><p>{turn.question}</p><p className="section-label">Provisional answer</p>{turn.unknown || !turn.answer ? <p>The selected evidence does not establish an answer to this question.</p> : <p>{turn.answer}</p>}{turn.citations.length > 0 && <details className="answer-evidence"><summary>Supporting evidence</summary>{turn.citations.map((citation) => <p className="answer-citation" key={`${citation.documentId}:${citation.sourceReference}`}><span>{citation.filename} · {citation.sourceReference}</span>{citation.evidenceSnippet}</p>)}</details>}<button className="secondary-button conversation-button" type="button" onClick={() => openConversation(turn)}>Continue conversation</button></article>)}</div>}
        {questionJob?.status === "failed" && <p className="viewer-error" role="alert">{matterQuestionFailure(questionJob.errorCode)}</p>}{questionMessage && <p className="viewer-error" role="alert">{questionMessage}</p>}
      </section>}
    </>}
    {activeConversationId && latestConversationTurn && <div className="conversation-overlay" role="presentation" onMouseDown={closeConversation}>
      <section className="conversation-dialog" role="dialog" aria-modal="true" aria-label="Follow-up conversation" onMouseDown={(event) => event.stopPropagation()}>
        <header>
          <div><p className="section-label">Conversation</p><h2>Follow up on this answer</h2><p>The same selected documents remain in scope. Every answer must cite local evidence.</p></div>
          <button className="text-button" type="button" onClick={closeConversation}>Close</button>
        </header>
        <div className="conversation-messages">
          {conversationTurns.map((turn) => <article key={turn.id} className={`conversation-message${turn.parentId ? " follow-up" : ""}`}>
            <p className="section-label">{turn.parentId ? "Follow-up question" : "Your question"}</p><p>{turn.question}</p><p className="section-label">Provisional answer</p>
            {turn.unknown || !turn.answer ? <p>The selected evidence does not establish an answer to this question.</p> : <p>{turn.answer}</p>}
            {turn.citations.length > 0 && <details className="answer-evidence"><summary>Supporting evidence</summary>{turn.citations.map((citation) => <p className="answer-citation" key={`${citation.documentId}:${citation.sourceReference}`}><span>{citation.filename} · {citation.sourceReference}</span>{citation.evidenceSnippet}</p>)}</details>}
          </article>)}
        </div>
        <footer className="conversation-composer">
          <label>Ask a follow-up<textarea value={conversationDraft} maxLength={600} placeholder="For example: What source supports that conclusion?" onChange={(event) => { setConversationDraft(event.target.value); setQuestionFollowUp(null); setQuestionPreview(null); setQuestionPreviewText(""); setQuestionPreviewFollowUpId(null); setQuestionAuthorized(false); setQuestionMessage(""); }} /></label>
          <small>{conversationDraft.length}/600</small>
          {conversationDraft.trim() && <>
            {!currentQuestionPreview && <p className="support-note">Click Ask AI to check the local evidence scope.</p>}
            {currentQuestionPreview && <><p>{currentQuestionPreview.localOnly ? "This document-backed calculation runs locally. No content will be sent to cloud AI." : "This follow-up includes the previous question and answer as context, but only cited document evidence can support the response."}</p>{currentQuestionPreview.scope.truncated && <p className="viewer-warning">Only part of the selected evidence can be included in this follow-up.</p>}{currentQuestionPreview.scope.comparisonCoverageComplete === false && <p className="viewer-warning">This comparison currently has evidence from {currentQuestionPreview.scope.documentsWithEvidence ?? 0} of {currentQuestionPreview.scope.documentsSelected ?? selectedIds.length} selected documents.</p>}</>}
            <button className="primary-button" type="button" disabled={!conversationDraft.trim() || askingQuestion || (Boolean(currentQuestionPreview) && !canAskQuestion)} onClick={() => {
              if (currentQuestionPreview) { void askQuestion(); return; }
              const parentAnswer = conversationContextAnswer(latestConversationTurn.answer, "The selected evidence does not establish an answer to this question.");
              const followUp = { id: latestConversationTurn.id, question: latestConversationTurn.question, answer: parentAnswer };
              void prepareQuestion(conversationDraft, followUp, true);
            }}>{questionLoading ? "Checking…" : askingQuestion ? "Answering…" : "Ask AI"}</button>
            {questionJob?.status === "failed" && <p className="viewer-error" role="alert">{matterQuestionFailure(questionJob.errorCode)}</p>}
            {questionMessage && <p className="viewer-error" role="alert">{questionMessage}</p>}
          </>}
        </footer>
      </section>
    </div>}
  </>;
}
