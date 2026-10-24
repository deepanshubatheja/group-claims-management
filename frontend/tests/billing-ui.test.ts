import assert from "node:assert/strict";
import test from "node:test";
import { billingAgreementSelection, linkedBillingAgreementId } from "../src/components/billing-ui.js";

const candidateId = "11111111-1111-4111-8111-111111111111";
const agreementId = "22222222-2222-4222-8222-222222222222";

test("keeps candidate and approved agreement identifiers separate", () => {
  const approvedCandidate = { id: candidateId, status: "approved", billingAgreementId: agreementId };
  assert.equal(linkedBillingAgreementId(approvedCandidate), agreementId);
  assert.notEqual(approvedCandidate.id, linkedBillingAgreementId(approvedCandidate));
});

test("uses the returned agreement immediately after approval and after refresh", () => {
  assert.deepEqual(billingAgreementSelection({ id: agreementId }, []), { billingAgreementId: agreementId, integrityError: null });
  assert.deepEqual(billingAgreementSelection(null, [{ id: candidateId, status: "approved", billingAgreementId: agreementId }]), { billingAgreementId: agreementId, integrityError: null });
});

test("never falls back to a candidate ID when an approved linkage is missing", () => {
  const selection = billingAgreementSelection(null, [{ id: candidateId, status: "approved", billingAgreementId: null }]);
  assert.equal(selection.billingAgreementId, null);
  assert.match(selection.integrityError ?? "", /missing its linked agreement/);
  assert.match(billingAgreementSelection({ id: agreementId }, [{ id: candidateId, status: "approved", billingAgreementId: null }]).integrityError ?? "", /missing its linked agreement/);
});
