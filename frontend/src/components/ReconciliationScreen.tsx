import { useCallback, useEffect, useMemo, useState } from "react";
import { candidateBusinessDate, candidateDetails, identityQuestionPrompt, isCandidateBatchEligible, potentialHoldingConflict, rereadSummary, type IdentityQuestionPrompt } from "./reconciliation-ui";

type RecordValue = Record<string, unknown>;
type Props = { mode: "holdings" | "issues"; onOpenSource(documentId: string, reference: string): void };
const asRecord = (value: unknown): RecordValue | null => typeof value === "object" && value !== null && !Array.isArray(value) ? value as RecordValue : null;
const list = (value: unknown): RecordValue[] => Array.isArray(value) ? value.map(asRecord).filter((item): item is RecordValue => item !== null) : [];
const text = (value: unknown, fallback = "—"): string => typeof value === "string" || typeof value === "number" ? String(value) : fallback;
const source = (value: unknown): RecordValue | null => asRecord(value);

function SourceLink({ value, onOpenSource }: { value: unknown; onOpenSource: Props["onOpenSource"] }) {
  const item = source(value); const documentId = item?.sourceDocumentId; const reference = item?.sourceReference;
  if (typeof documentId !== "string" || typeof reference !== "string") return <span className="muted">Source reference unavailable</span>;
  return <button className="text-button source-link" type="button" onClick={() => onOpenSource(documentId, reference)}>View {reference}</button>;
}

function CandidateCard({ candidate, onOpenSource, onConfirm, onReject }: {
  candidate: RecordValue;
  onOpenSource: Props["onOpenSource"];
  onConfirm(candidate: RecordValue): void;
  onReject(candidate: RecordValue): void;
}) {
  const date = candidateBusinessDate(candidate);
  const details = candidateDetails(candidate);
  const conflict = potentialHoldingConflict(candidate);
  const observations = conflict?.observations.map(asRecord).filter((item): item is RecordValue => item !== null) ?? [];
  const reviewRequired = conflict !== null || text(candidate.validationStatus) === "review_required";
  const statusLabel = text(candidate.status) === "confirmed" ? "confirmed business record" : reviewRequired ? "review required" : "supported evidence";
  const issueDate = text(candidate.documentIssueDate, "");
  return <article className="candidate-row">
    <div>
      <strong>{text(candidate.type).replaceAll("_", " ")}</strong>
      <p><strong>{date.label}</strong> {date.value} · {text(candidate.origin)} · {statusLabel}</p>
      {issueDate && <p>Document issued {issueDate}</p>}
      <dl className="candidate-details">
        {details.map((detail) => <div key={detail.label}><dt>{detail.label}</dt><dd>{detail.value}</dd></div>)}
      </dl>
      {conflict && <div className="candidate-conflict" role="alert">
        <strong>Potential conflict — review required.</strong>
        <p>Same account, security, and as-of date have competing nominal amounts. Difference: {conflict.difference}.</p>
        <ul>{observations.map((observation) => <li key={text(observation.candidateId)}>{text(observation.quantity)} · {text(observation.subfund)} · <SourceLink value={observation} onOpenSource={onOpenSource} /></li>)}</ul>
        <p>This candidate is excluded from routine batch confirmation. No balance has been selected.</p>
      </div>}
      {!conflict && <p>{text(candidate.reviewReason, "Ready for the permitted review workflow.")}</p>}
      <SourceLink value={{ sourceDocumentId: candidate.sourceDocumentId, sourceReference: candidate.sourceReference }} onOpenSource={onOpenSource} />
    </div>
    <div className="candidate-actions">
      {!conflict && <button className="secondary-button" type="button" onClick={() => onConfirm(candidate)}>Confirm</button>}
      <button className="text-button" type="button" onClick={() => onReject(candidate)}>Reject</button>
    </div>
  </article>;
}

export default function ReconciliationScreen({ mode, onOpenSource }: Props) {
  const [fromDate, setFromDate] = useState(""); const [toDate, setToDate] = useState(""); const [subfund, setSubfund] = useState("");
  const [timeline, setTimeline] = useState<RecordValue | null>(null);
  const [assignments, setAssignments] = useState<RecordValue | null>(null); const [issues, setIssues] = useState<RecordValue | null>(null);
  const [candidates, setCandidates] = useState<RecordValue[]>([]); const [message, setMessage] = useState(""); const [loading, setLoading] = useState(true);
  const [identityQuestions, setIdentityQuestions] = useState<IdentityQuestionPrompt[]>([]);
  const [notice, setNotice] = useState(""); const [rereading, setRereading] = useState(false);
  const filters = useMemo(() => ({ ...(fromDate ? { fromDate } : {}), ...(toDate ? { toDate } : {}), ...(subfund.trim() ? { subfund: subfund.trim() } : {}) }), [fromDate, toDate, subfund]);
  const refresh = useCallback(async () => {
    setLoading(true); setMessage("");
    try {
      const [timelineResponse, assignmentResponse, issueResponse, candidateResponse, identityResponse] = await Promise.all([
        window.desktopAPI.getReconciliationTimeline(filters),
        window.desktopAPI.getReconciliationAssignments({ ...(fromDate ? { fromDate } : {}), ...(toDate ? { toDate } : {}) }),
        window.desktopAPI.getReconciliationDiscrepancies(filters), window.desktopAPI.getReconciliationCandidates("candidate"),
        window.desktopAPI.getIdentityQuestions("open"),
      ]);
      const failure = [timelineResponse, assignmentResponse, issueResponse, candidateResponse, identityResponse].find((result) => !result.ok);
      if (failure && !failure.ok) { setMessage(failure.error.message); return; }
      if (timelineResponse.ok) setTimeline(timelineResponse.result);
      if (assignmentResponse.ok) setAssignments(assignmentResponse.result); if (issueResponse.ok) setIssues(issueResponse.result);
      if (candidateResponse.ok) setCandidates(list(candidateResponse.result.candidates));
      if (identityResponse.ok) setIdentityQuestions(list(identityResponse.result.questions).map(identityQuestionPrompt).filter((item): item is IdentityQuestionPrompt => item !== null));
    } catch { setMessage("Local reconciliation data is temporarily unavailable. Try again."); }
    finally { setLoading(false); }
  }, [filters, fromDate, toDate]);
  useEffect(() => { void refresh(); }, [refresh]);
  const confirm = async (candidate: RecordValue) => {
    const id = text(candidate.id, ""); if (!id || !window.confirm("Confirm this evidence-backed record? This creates a local audited record.")) return;
    const response = await window.desktopAPI.confirmReconciliationCandidate(id); if (!response.ok) setMessage(response.error.message); else await refresh();
  };
  const reject = async (candidate: RecordValue) => {
    const id = text(candidate.id, ""); const reason = window.prompt("Why should this provisional record be rejected?");
    if (!id || reason === null) return; const response = await window.desktopAPI.rejectReconciliationCandidate(id, reason);
    if (!response.ok) setMessage(response.error.message); else await refresh();
  };
  const batch = async () => {
    const ids = candidates.filter(isCandidateBatchEligible).map((candidate) => text(candidate.id, "")).filter(Boolean);
    if (!ids.length || !window.confirm(`Confirm ${ids.length} routine, non-conflicting records? This creates local audited records.`)) return;
    const response = await window.desktopAPI.applyReconciliationPolicy(ids); if (!response.ok) setMessage(response.error.message); else await refresh();
  };
  const answerIdentity = async (question: IdentityQuestionPrompt, decision: "same" | "distinct") => {
    const prompt = decision === "same"
      ? `Record "${question.observedText}" as another name for "${question.suggestedLabel}"? Future documents using this name will link to it.`
      : `Keep "${question.observedText}" separate from "${question.suggestedLabel}"? It will become its own record when confirmed.`;
    if (!window.confirm(prompt)) return;
    const response = await window.desktopAPI.resolveIdentityQuestion(question.id, decision);
    if (!response.ok) setMessage(response.error.message); else await refresh();
  };
  const reread = async () => {
    if (!window.confirm("Re-read all imported documents with the current reading rules? New records are added for review only; nothing is confirmed and earlier records are kept.")) return;
    setRereading(true); setNotice(""); setMessage("");
    try {
      const response = await window.desktopAPI.rereadDocuments();
      if (!response.ok) { setMessage(response.error.message); return; }
      setNotice(rereadSummary(response.result)); await refresh();
    } finally { setRereading(false); }
  };
  const events = list(timeline?.events); const assignmentEvents = list(assignments?.events); const issueRows = list(issues?.issues);
  const batchEligibleCandidates = candidates.filter(isCandidateBatchEligible);
  return <><header className="page-header"><div><p className="eyebrow">{mode === "issues" ? "Evidence review" : "Holdings & history"}</p><h1>{mode === "issues" ? "Open issues" : "Holdings and ownership history"}</h1><p className="page-subtitle">{mode === "issues" ? "Conflicting evidence remains open until a separate, audited resolution is available." : "Documented positions, reconstructed balances, and legal claim-right events remain distinct."}</p></div><div className="candidate-actions">{mode === "holdings" && <button className="secondary-button" type="button" onClick={() => void reread()} disabled={loading || rereading}>{rereading ? "Re-reading…" : "Re-read documents"}</button>}<button className="secondary-button" type="button" onClick={() => void refresh()} disabled={loading}>Refresh</button></div></header>
    <section className="dashboard-card reconciliation-filters"><label>From<input type="date" value={fromDate} onChange={(event) => setFromDate(event.target.value)} /></label><label>To<input type="date" value={toDate} onChange={(event) => setToDate(event.target.value)} /></label><label>Subfund<input value={subfund} maxLength={120} placeholder="Filter by subfund" onChange={(event) => setSubfund(event.target.value)} /></label></section>
    {message && <p className="viewer-warning" role="alert">{message}</p>}{notice && <p className="viewer-state" role="status">{notice}</p>}{loading && <p className="viewer-state" role="status">Loading local records…</p>}
    {mode === "holdings" && <>
      <section className="dashboard-card"><div className="dashboard-section-heading"><div><p className="section-label">Dated holdings</p><h2>Documented snapshots and transfers</h2></div></div>{events.length === 0 ? <p className="support-note">No confirmed holdings records match these filters.</p> : <div className="answer-table"><table><thead><tr><th>Date</th><th>Type</th><th>Investor / subfund</th><th>Account</th><th>ISIN</th><th className="numeric">Nominal</th><th>Evidence</th></tr></thead><tbody>{events.map((event, index) => { const record = asRecord(event.record) ?? {}; const transfer = text(event.kind) === "securities_transfer"; const party = transfer ? asRecord(record.from) : null; return <tr key={`${text(record.id)}:${index}`}><td>{text(event.date)}</td><td>{transfer ? "Securities transfer" : "Documented holding"}</td><td>{transfer ? `${text(party?.investor)} / ${text(party?.subfund)}` : `${text(record.investor)} / ${text(record.subfund)}`}</td><td>{transfer ? `${text(party?.account)} → ${text(asRecord(record.to)?.account)}` : text(record.account)}</td><td>{text(record.isin)}</td><td className="numeric">{text(record.quantity)}</td><td><SourceLink value={record.source} onOpenSource={onOpenSource} /></td></tr>; })}</tbody></table></div>}</section>
      <section className="dashboard-card"><p className="section-label">Legal claim-right assignments</p><h2>Separate from securities holdings</h2>{assignmentEvents.length === 0 ? <p className="support-note">No confirmed claim-right assignments match these filters.</p> : <div className="answer-table"><table><thead><tr><th>Effective date</th><th>Claim</th><th>From</th><th>To</th><th>Assigned</th><th>Evidence</th></tr></thead><tbody>{assignmentEvents.map((event) => <tr key={text(event.id)}><td>{text(event.effectiveDate)}</td><td>{text(event.claimReference)}</td><td>{text(event.fromEntity)}</td><td>{text(event.toEntity)}</td><td>{text(event.percentage)}%</td><td><SourceLink value={event.source} onOpenSource={onOpenSource} /></td></tr>)}</tbody></table></div>}<p className="support-note">A claim-right assignment does not change a securities balance or establish an invoice recipient.</p></section>
      <section className="dashboard-card"><div className="dashboard-section-heading"><div><p className="section-label">Candidate review</p><h2>Provisional extracted records</h2></div>{batchEligibleCandidates.length > 0 && <button className="secondary-button" type="button" onClick={() => void batch()}>Confirm {batchEligibleCandidates.length} eligible routine records</button>}</div>{candidates.length === 0 ? <p className="support-note">No provisional records need review.</p> : <><p className="support-note">Potentially conflicting observations are excluded from routine batch confirmation. Confirmed records appear in the dated-history tables above.</p><div className="candidate-list">{candidates.map((candidate) => <CandidateCard key={text(candidate.id)} candidate={candidate} onOpenSource={onOpenSource} onConfirm={confirm} onReject={reject} />)}</div></>}</section>
    </>}
    {mode === "issues" && <section className="dashboard-card"><p className="section-label">Possible same records</p><h2>Is this the same record?</h2>{identityQuestions.length === 0 ? <p className="support-note">No names or accounts need matching.</p> : <div className="issue-list">{identityQuestions.map((question) => <article key={question.id} className="issue-card"><h3>{question.recordKind}: “{question.observedText}”</h3><p>Possibly the same as <strong>{question.suggestedLabel}</strong>. {question.basis}</p>{question.knownAliases.length > 0 && <p className="support-note">Known names: {question.knownAliases.join(", ")}</p>}<div className="candidate-actions"><button className="secondary-button" type="button" onClick={() => void answerIdentity(question, "same")}>Same record</button><button className="text-button" type="button" onClick={() => void answerIdentity(question, "distinct")}>Different record</button></div></article>)}</div>}</section>}
    {mode === "issues" && <section className="dashboard-card"><p className="section-label">Unresolved discrepancies</p><h2>Evidence requiring review</h2>{issueRows.length === 0 ? <p className="support-note">No open discrepancies match these filters.</p> : <div className="issue-list">{issueRows.map((issue) => { const candidate = asRecord(issue.candidate) ?? {}; const competing = list(issue.documentedCompetingValues); const observations = list(issue.observations); const differences = Array.isArray(issue.differenceAmounts) ? issue.differenceAmounts : []; const displayObservations = observations.length > 0 ? observations : [{ quantity: issue.candidateValue, source: { sourceDocumentId: candidate.sourceDocumentId, sourceReference: candidate.sourceReference } }, ...competing]; return <article key={text(issue.id)} className="issue-card"><h3>{text(issue.reason).replaceAll("_", " ")}</h3><p>As of {text(issue.asOfDate)} · {text(issue.subfund, "Subfund unknown")} · account {text(issue.account)} · {text(issue.isin)}</p><dl><dt>Status</dt><dd>{text(issue.status)}</dd><dt>Difference</dt><dd>{differences.map((amount) => text(amount)).join(", ")}</dd><dt>Competing observations</dt><dd>{displayObservations.map((item) => text(item.quantity)).join(" versus ")}</dd></dl>{displayObservations.map((item, index) => <p key={`${text(item.candidateId, text(item.id, String(index)))}:${index}`}><SourceLink value={item.source} onOpenSource={onOpenSource} /></p>)}<p className="support-note">Resolution is not available in this screen; no balance is changed here.</p></article>; })}</div>}</section>}
  </>;
}
