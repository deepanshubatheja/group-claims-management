export type ReconciliationCandidate = Record<string, unknown>;

type CandidatePayload = Record<string, unknown>;

export interface CandidateDate {
  label: "As of" | "Effective" | "Date";
  value: string;
}

export interface CandidateDetail {
  label: string;
  value: string;
}

export interface PotentialHoldingConflict {
  type: "competing_dated_holding";
  account: string;
  isin: string;
  asOfDate: string;
  difference: string;
  observations: Array<Record<string, unknown>>;
}

const record = (value: unknown): Record<string, unknown> | null =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;

const value = (item: Record<string, unknown>, key: string, fallback = "Unknown"): string => {
  const candidate = item[key];
  return typeof candidate === "string" && candidate.trim() ? candidate : fallback;
};

const payload = (candidate: ReconciliationCandidate): CandidatePayload => record(candidate.payload) ?? {};

export function candidateBusinessDate(candidate: ReconciliationCandidate): CandidateDate {
  const type = value(candidate, "type", "");
  const details = payload(candidate);
  if (type === "holding_snapshot") return { label: "As of", value: value(details, "positionDate", "Date unknown") };
  if (type === "securities_transfer" || type === "claim_assignment") {
    return { label: "Effective", value: value(details, "effectiveDate", "Date unknown") };
  }
  return { label: "Date", value: "Date unknown" };
}

export function candidateDetails(candidate: ReconciliationCandidate): CandidateDetail[] {
  const type = value(candidate, "type", "");
  const details = payload(candidate);
  const date = candidateBusinessDate(candidate);
  if (type === "holding_snapshot") {
    return [
      { label: "Investor", value: value(details, "investor") },
      { label: "Subfund", value: value(details, "subfund") },
      { label: "Custodian account", value: value(details, "account") },
      { label: "ISIN", value: value(details, "isin") },
      { label: "Nominal amount", value: value(details, "quantity") },
      { label: date.label, value: date.value },
    ];
  }
  if (type === "securities_transfer") {
    return [
      { label: "From", value: `${value(details, "fromSubfund")} / ${value(details, "fromAccount")}` },
      { label: "To", value: `${value(details, "toSubfund")} / ${value(details, "toAccount")}` },
      { label: "ISIN", value: value(details, "isin") },
      { label: "Transferred nominal", value: value(details, "quantity") },
      { label: date.label, value: date.value },
    ];
  }
  if (type === "claim_assignment") {
    return [
      { label: "Claim reference", value: value(details, "claimReference") },
      { label: "Assignor", value: value(details, "fromEntity") },
      { label: "Assignee", value: value(details, "toEntity") },
      { label: "Assigned", value: `${value(details, "percentage")}%` },
      { label: date.label, value: date.value },
    ];
  }
  return [{ label: "Date", value: "Date unknown" }];
}

export function potentialHoldingConflict(candidate: ReconciliationCandidate): PotentialHoldingConflict | null {
  const item = record(candidate.potentialConflict);
  if (
    item?.type !== "competing_dated_holding"
    || typeof item.account !== "string"
    || typeof item.isin !== "string"
    || typeof item.asOfDate !== "string"
    || typeof item.difference !== "string"
    || !Array.isArray(item.observations)
  ) return null;
  return {
    type: "competing_dated_holding",
    account: item.account,
    isin: item.isin,
    asOfDate: item.asOfDate,
    difference: item.difference,
    observations: item.observations.map(record).filter((observation): observation is Record<string, unknown> => observation !== null),
  };
}

export function isCandidateBatchEligible(candidate: ReconciliationCandidate): boolean {
  return value(candidate, "status", "") === "candidate"
    && value(candidate, "type", "") !== "claim_assignment"
    && value(candidate, "validationStatus", "") !== "review_required"
    && potentialHoldingConflict(candidate) === null;
}
