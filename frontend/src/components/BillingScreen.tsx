import { useCallback, useEffect, useState } from "react";
import { billingAgreementSelection, linkedBillingAgreementId } from "./billing-ui";

type RecordValue = Record<string, unknown>;
type Props = { onOpenSource(documentId: string, reference: string): void };
const record = (value: unknown): RecordValue | null => typeof value === "object" && value !== null && !Array.isArray(value) ? value as RecordValue : null;
const rows = (value: unknown): RecordValue[] => Array.isArray(value) ? value.map(record).filter((item): item is RecordValue => item !== null) : [];
const text = (value: unknown, fallback = "—") => typeof value === "string" || typeof value === "number" ? String(value) : fallback;

function EvidenceLink({ documentId, reference, onOpenSource }: { documentId: unknown; reference: unknown; onOpenSource: Props["onOpenSource"] }) {
  return typeof documentId === "string" && typeof reference === "string" ? <button type="button" className="text-button source-link" onClick={() => onOpenSource(documentId, reference)}>View {reference}</button> : <span className="muted">Source reference unavailable</span>;
}

export default function BillingScreen({ onOpenSource }: Props) {
  const [candidates, setCandidates] = useState<RecordValue[]>([]); const [rules, setRules] = useState<RecordValue[]>([]);
  const [selectedRule, setSelectedRule] = useState<RecordValue | null>(null); const [preview, setPreview] = useState<RecordValue | null>(null); const [runs, setRuns] = useState<RecordValue[]>([]);
  const [message, setMessage] = useState(""); const [loading, setLoading] = useState(true);
  const loadRule = useCallback(async (ruleId: string) => {
    const [ruleResponse, previewResponse, runResponse] = await Promise.all([window.desktopAPI.getBillingRule(ruleId), window.desktopAPI.previewBillingCalculation({ billingAgreementId: ruleId }), window.desktopAPI.getBillingCalculationRuns(ruleId)]);
    const failure = [ruleResponse, previewResponse, runResponse].find((result) => !result.ok);
    if (failure && !failure.ok) { setMessage(failure.error.message); return; }
    if (ruleResponse.ok) setSelectedRule(ruleResponse.result); if (previewResponse.ok) setPreview(previewResponse.result); if (runResponse.ok) setRuns(rows(runResponse.result.runs));
  }, []);
  const refresh = useCallback(async (preferredAgreementId?: string) => {
    setLoading(true); setMessage("");
    try {
      const response = await window.desktopAPI.getBillingRuleCandidates();
      if (!response.ok) { setMessage(response.error.message); return; }
      const all = rows(response.result.candidates); setCandidates(all.filter((item) => text(item.status) === "candidate"));
      const approved = all.filter((item) => text(item.status) === "approved"); setRules(approved);
      const selection = billingAgreementSelection(selectedRule, approved);
      const agreementId = preferredAgreementId ?? selection.billingAgreementId;
      if (selection.integrityError) setMessage(selection.integrityError);
      if (agreementId) await loadRule(agreementId);
      else { setSelectedRule(null); setPreview(null); setRuns([]); }
    } catch { setMessage("Local billing records are temporarily unavailable. Try again."); }
    finally { setLoading(false); }
  }, [loadRule, selectedRule?.id]);
  useEffect(() => { void refresh(); }, [refresh]);
  const approve = async (candidate: RecordValue) => {
    const id = text(candidate.id, ""); if (!id || !window.confirm("Approve this evidence-backed billing rule for local calculations? This does not create an invoice.")) return;
    const response = await window.desktopAPI.approveBillingRule(id);
    if (!response.ok) { setMessage(response.error.message); return; }
    const selection = billingAgreementSelection(response.result, []);
    if (!selection.billingAgreementId) { setMessage("Approved billing rule is missing its agreement identifier. It cannot be loaded or calculated."); return; }
    setSelectedRule(response.result); await refresh(selection.billingAgreementId);
  };
  const save = async () => {
    const id = selectedRule?.id; if (typeof id !== "string" || !window.confirm("Save this local calculation run? Earlier calculation runs will remain unchanged.")) return;
    const response = await window.desktopAPI.saveBillingCalculation({ billingAgreementId: id }); if (!response.ok) setMessage(response.error.message); else await loadRule(id);
  };
  const allocations = rows(preview?.allocations); const relatedIssues = rows(preview?.relatedIssues);
  return <><header className="page-header"><div><p className="eyebrow">Billing</p><h1>Evidence-backed pro-rata calculations</h1><p className="page-subtitle">Calculations are local, auditable, and are not invoices. Invoice recipients remain unresolved unless separately documented.</p></div><button className="secondary-button" type="button" onClick={() => void refresh()} disabled={loading}>Refresh</button></header>
    {message && <p className="viewer-warning" role="alert">{message}</p>}{loading && <p className="viewer-state" role="status">Loading local billing records…</p>}
    <section className="dashboard-card"><p className="section-label">Billing-rule review</p><h2>Provisional approved-instruction candidates</h2>{candidates.length === 0 ? <p className="support-note">No billing rules are awaiting approval.</p> : <div className="candidate-list">{candidates.map((candidate) => { const payload = record(candidate.payload) ?? {}; return <article className="candidate-row" key={text(candidate.id)}><div><strong>{text(payload.currency)} {text(payload.feePool)} fee pool</strong><p>{text(payload.periodStart)} to {text(payload.periodEnd)} · {text(candidate.documentIssueDate)}</p><EvidenceLink documentId={candidate.sourceDocumentId} reference={candidate.sourceReference} onOpenSource={onOpenSource} /></div><button className="secondary-button" type="button" onClick={() => void approve(candidate)}>Approve rule</button></article>; })}</div>}</section>
    <section className="dashboard-card"><p className="section-label">Approved calculations</p><h2>Rule and allocation preview</h2>{rules.length === 0 ? <p className="support-note">Approve a supported billing instruction before creating a calculation.</p> : <><label className="billing-rule-picker">Approved rule<select value={text(selectedRule?.id, "")} onChange={(event) => void loadRule(event.target.value)}>{rules.flatMap((rule) => { const agreementId = linkedBillingAgreementId(rule); return agreementId === null ? [] : [<option value={agreementId} key={text(rule.id)}>{text(record(rule.payload)?.periodStart)} to {text(record(rule.payload)?.periodEnd)} · {text(record(rule.payload)?.currency)} {text(record(rule.payload)?.feePool)}</option>]; })}</select></label>{selectedRule && <p className="support-note">Rule source: <EvidenceLink documentId={selectedRule.sourceDocumentId} reference={selectedRule.sourceReference} onOpenSource={onOpenSource} /> · Invoice recipient: {text(selectedRule.invoiceRecipientStatus)}</p>}{preview && <><div className="billing-summary"><strong>{text(preview.status)}</strong><span>{text(preview.currency)} {text(preview.feePool)} · eligible nominal {text(preview.totalEligibleAmount)}</span></div><div className="answer-table"><table><thead><tr><th>Billing participant</th><th>Eligibility</th><th className="numeric">Basis</th><th className="numeric">Share</th><th className="numeric">Calculated allocation</th><th className="numeric">Issued invoices</th><th className="numeric">Difference</th><th className="numeric">Outstanding balance</th><th>Invoice recipient</th><th>Invoice evidence</th></tr></thead><tbody>{allocations.map((item) => <tr key={text(item.participantKey)}><td>{text(item.participantLabel)}</td><td>{text(item.eligibilityStatus)}<small>{text(item.eligibilityReason, "")}</small></td><td className="numeric">{text(item.eligibleBasis)}</td><td className="numeric">{text(item.share)}</td><td className="numeric">{text(item.allocatedFee)} {text(item.currency, "")}</td><td className="numeric">{item.verifiedInvoicedAmount === null || item.verifiedInvoicedAmount === undefined ? <small>{text(item.issuedInvoiceStatus, "not recorded")}</small> : <>{text(item.verifiedInvoicedAmount)} {text(item.currency, "")}</>}</td><td className="numeric">{item.allocationInvoiceDifference === null || item.allocationInvoiceDifference === undefined ? <small>not comparable</small> : <>{text(item.allocationInvoiceDifference)} {text(item.currency, "")}</>}</td><td className="numeric">{item.outstandingBalance === null || item.outstandingBalance === undefined ? <small>{text(item.outstandingStatus, "not available")}</small> : <>{text(item.outstandingBalance)} {text(item.currency, "")}</>}</td><td>{text(item.invoiceRecipient, "unresolved")}</td><td>{rows(item.invoiceEvidence).length === 0 ? <small>Not available</small> : rows(item.invoiceEvidence).map((evidence, index) => <span key={`${text(evidence.documentId)}-${text(evidence.sourceReference)}-${index}`}><EvidenceLink documentId={evidence.documentId} reference={evidence.sourceReference} onOpenSource={onOpenSource} />{index + 1 < rows(item.invoiceEvidence).length ? " · " : ""}</span>)}</td></tr>)}</tbody></table></div><p className="support-note">Rounding: {text(preview.roundingMethod)}. Allocated total: {text(preview.currency)} {text(preview.allocatedTotal)}. Calculated allocations are not invoices.</p>{relatedIssues.length > 0 && <div className="viewer-warning"><strong>Related unresolved evidence</strong>{relatedIssues.map((issue) => <p key={text(issue.id)}>{text(issue.reason)} · <EvidenceLink documentId={issue.sourceDocumentId} reference={issue.sourceReference} onOpenSource={onOpenSource} /></p>)}</div>}<button className="primary-button" type="button" onClick={() => void save()}>Save calculation run</button></>}</>}</section>
    <section className="dashboard-card"><p className="section-label">Calculation history</p><h2>Earlier runs remain unchanged</h2>{runs.length === 0 ? <p className="support-note">No saved calculation runs for this rule.</p> : <div className="answer-table"><table><thead><tr><th>Version</th><th>Status</th><th>Allocation date</th><th>Fee pool</th><th>Saved</th></tr></thead><tbody>{runs.map((run) => <tr key={text(run.id)}><td>{text(run.version)}</td><td>{text(run.status)}</td><td>{text(run.allocationDate)}</td><td>{text(run.currency)} {text(run.feePool)}</td><td>{text(run.createdAt)}</td></tr>)}</tbody></table></div>}</section>
  </>;
}
