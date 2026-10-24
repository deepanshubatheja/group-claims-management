export type BillingRecord = Record<string, unknown>;

const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i;

function identifier(value: unknown): string | null {
  return typeof value === "string" && UUID.test(value) ? value : null;
}

/** The candidate ID and the approved agreement ID are deliberately separate. */
export function linkedBillingAgreementId(candidate: BillingRecord): string | null {
  return identifier(candidate.billingAgreementId);
}

/** Select only a verified agreement ID; candidate IDs must never be used as a fallback. */
export function billingAgreementSelection(
  selectedRule: BillingRecord | null,
  approvedCandidates: readonly BillingRecord[],
): { billingAgreementId: string | null; integrityError: string | null } {
  const missingLink = approvedCandidates.some((candidate) => linkedBillingAgreementId(candidate) === null);
  const integrityError = missingLink
    ? "An approved billing rule is missing its linked agreement. It cannot be loaded or calculated."
    : null;
  const selectedId = selectedRule === null ? null : identifier(selectedRule.id);
  if (selectedId !== null) return { billingAgreementId: selectedId, integrityError };
  if (approvedCandidates.length === 0) return { billingAgreementId: null, integrityError: null };

  const linked = approvedCandidates.map(linkedBillingAgreementId).find((id): id is string => id !== null) ?? null;
  return {
    billingAgreementId: linked,
    integrityError,
  };
}
