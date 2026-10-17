export type AnalysisJobState = "queued" | "running" | "completed" | "failed" | "blocked" | null;

export type AnalysisValidationCounts = {
  available: boolean;
  proposalsReturned: number;
  schemaValid: number;
  evidenceValidated: number;
  persisted: number;
  rejected: number;
  rejectedByReason: Record<string, number>;
};

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : 0;
}

export function analysisValidationCounts(coverage: Record<string, unknown> | null): AnalysisValidationCounts {
  const validation = coverage?.validation;
  const available = validation !== null && typeof validation === "object";
  const source = available ? validation as Record<string, unknown> : {};
  const rawReasons = source.rejectedByReason;
  const rejectedByReason: Record<string, number> = {};
  if (rawReasons !== null && typeof rawReasons === "object") {
    for (const [reason, value] of Object.entries(rawReasons as Record<string, unknown>)) {
      const total = count(value);
      if (total > 0) rejectedByReason[reason] = total;
    }
  }
  return {
    available, proposalsReturned: count(source.proposalsReturned), schemaValid: count(source.schemaValid),
    evidenceValidated: count(source.evidenceValidated), persisted: count(source.persisted),
    rejected: count(source.rejected), rejectedByReason,
  };
}

export function validationReasonLabel(reason: string): string {
  const labels: Record<string, string> = {
    schema_invalid: "invalid structured proposal",
    unsupported_proposal_type: "unsupported proposal type",
    source_context_mismatch: "document or extraction reference mismatch",
    source_reference_not_found: "source reference not found",
    evidence_snippet_mismatch: "evidence snippet did not match the source",
    proposed_value_not_in_source: "proposed value was not found in the source",
    invalid_isin: "invalid ISIN",
  };
  return labels[reason] ?? "unsupported evidence";
}

export function proposalTypeLabel(proposalType: string): string {
  const labels: Record<string, string> = {
    investor_legal_entity: "Investor / legal entity",
    investment_vehicle_subfund: "Investment vehicle / subfund",
    account: "Accounts",
    security: "Securities",
    holding_position: "Holding positions",
    legal_claim_right: "Legal claims / rights",
    rights_assignment: "Possible rights assignments",
    invoice_recipient: "Possible invoice recipients",
  };
  return labels[proposalType] ?? "Other observations";
}

export function findingFieldLabel(fieldName: string): string {
  const labels: Record<string, string> = {
    account_number: "Account number",
    accountNumber: "Account number",
    name: "Name",
    isin: "ISIN",
    security_description: "Security description",
    quantity: "Quantity",
    unit: "Unit",
    currency: "Currency",
    as_of_date: "As-of date",
    transaction_date: "Transaction date",
    entity_type: "Entity type",
    separate_legal_personality: "Separate legal personality",
    legal_claimant: "Legal claimant",
    invoice_recipient: "Invoice recipient",
  };
  return labels[fieldName] ?? fieldName.replace(/([a-z])([A-Z])/g, "$1 $2").replaceAll("_", " ");
}

export type AnalysisOverviewFinding = {
  proposalType: string;
  fieldName: string;
  value: unknown;
  unknown: boolean;
  sourceReference: string;
  evidenceSnippet: string | null;
};

export function analysisOverviewFromFindings(findings: AnalysisOverviewFinding[]): { text: string | null; citations: Array<{ sourceReference: string; evidenceSnippet: string }> } {
  const supported = findings.filter((finding) => !finding.unknown && finding.value !== null && finding.value !== undefined).slice(0, 3);
  if (supported.length === 0) return { text: null, citations: [] };
  const observations = supported.map((finding) => `${proposalTypeLabel(finding.proposalType)} — ${findingFieldLabel(finding.fieldName)}: ${String(finding.value)}`);
  const citations = supported.flatMap((finding) => finding.evidenceSnippet ? [{ sourceReference: finding.sourceReference, evidenceSnippet: finding.evidenceSnippet }] : []);
  return { text: `The analysis identified ${observations.join("; ")}.`, citations };
}

export function canStartDocumentAnalysis(
  globalAuthorization: boolean,
  providerConfigured: boolean,
  costWithinLimit: boolean,
  jobState: AnalysisJobState,
): boolean {
  return globalAuthorization && providerConfigured && costWithinLimit
    && jobState !== "queued" && jobState !== "running";
}

/** Keeps a conversation open even when the preceding turn was unsupported. */
export function conversationContextAnswer(answer: string | null, unavailableAnswer: string): string {
  return answer?.trim() || unavailableAnswer;
}

export function analysisFailureMessage(code: string | null): string | null {
  if (code === null) return null;
  if (code === "AI_AUTHORIZATION_WITHDRAWN") return "Cloud-processing authorization was withdrawn before analysis completed.";
  if (code === "CODEX_LOGIN_REQUIRED") return "Connect ChatGPT in Settings, then try the analysis again.";
  if (code === "AI_AUTHENTICATION_FAILED") {
    return "OpenAI rejected the configured API key. The application passed a non-empty credential, so restarting alone cannot repair a revoked, incorrect, or wrong-project key. Create or copy an active OpenAI Platform project key, export it in the same terminal that starts npm run dev, then restart the application.";
  }
  if (code === "AI_OUTPUT_LIMIT_REACHED") {
    return "The AI provider reached its output limit before completing the structured response. No findings were stored.";
  }
  if (code === "AI_INCOMPLETE") {
    return "The AI provider stopped before completing the structured response. No findings were stored.";
  }
  if (code === "AI_RESPONSE_TEXT_MISSING" || code === "AI_RESPONSE_JSON_INVALID" || code === "AI_RESPONSE_CONTRACT_INVALID") {
    return "The AI provider did not return the required structured result. No findings were stored.";
  }
  return "AI analysis did not complete. No confirmed records were changed.";
}
