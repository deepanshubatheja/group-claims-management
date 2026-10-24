from __future__ import annotations
import subprocess, sys, tempfile, unittest
from pathlib import Path
import pymupdf
from group_claims_backend.services.document_import import DocumentImportService
from group_claims_backend.services.reconciliation import ReconciliationError, ReconciliationService
from group_claims_backend.storage.service import StorageService

ROOT=Path(__file__).resolve().parents[2]
GENERATOR_ROOT=ROOT/'scripts'/'validation-sprint'
NAMES=('01_opening_holdings_2025-01-31.xlsx','02_partial_transfer_effective_2025-02-15.pdf','03_claim_right_assignment_effective_2025-02-18.pdf','04_later_holdings_2025-02-28.xlsx','05_contradictory_custodian_statement_2025-02-28.pdf','06_approved_billing_instruction_2025-03-01.pdf')
ISSUE_DATES={'01_opening_holdings_2025-01-31.xlsx':'2025-02-02','02_partial_transfer_effective_2025-02-15.pdf':'2025-02-20','03_claim_right_assignment_effective_2025-02-18.pdf':'2025-02-21','04_later_holdings_2025-02-28.xlsx':'2025-03-03','05_contradictory_custodian_statement_2025-02-28.pdf':'2025-03-10','06_approved_billing_instruction_2025-03-01.pdf':'2025-03-01'}

class ReconciliationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); sources=Path(self.tmp.name)/'sources';sources.mkdir()
  subprocess.run([sys.executable,str(GENERATOR_ROOT/'build_synthetic_pdfs.py'),str(sources)],check=True)
  subprocess.run([sys.executable,str(GENERATOR_ROOT/'build_synthetic_workbooks.py'),str(sources)],check=True)
  self.storage=StorageService(str(Path(self.tmp.name)/'app'));self.assertEqual(self.storage.initialize().status,'ready')
  docs=DocumentImportService(self.storage).import_authorized_paths([str(sources/name) for name in NAMES])['documents'];self.ids={x['document']['original_filename']:x['document']['id'] for x in docs};self.service=ReconciliationService(self.storage)
 def tearDown(self):self.tmp.cleanup()
 def stage(self,kind,filename,reference,payload):return self.service.stage({'type':kind,'sourceDocumentId':self.ids[filename],'sourceReference':reference,'evidenceSnippet':'Synthetic source evidence','documentIssueDate':ISSUE_DATES[filename],'origin':'deterministic','payload':payload})
 def confirm(self,candidate):return self.service.confirm({'candidateId':candidate['id'],'acknowledged':True})
 def test_confirmed_snapshots_transfer_assignment_and_issue_have_provenance(self):
  opening='01_opening_holdings_2025-01-31.xlsx';later='04_later_holdings_2025-02-28.xlsx'; investor='Redwood Investors LP'; isin='XS0123456789'
  positions=[('Horizon Credit Fund A (SF-A)','CUST-ALPHA-01','600000'),('Horizon Credit Fund B (SF-B)','CUST-ALPHA-02','800000'),('Meridian Income Fund C (SF-C)','CUST-ALPHA-03','500000')]
  for index,(fund,account,quantity) in enumerate(positions,8):self.confirm(self.stage('holding_snapshot',opening,f'Holdings!A{index}',{'investor':investor,'subfund':fund,'account':account,'isin':isin,'quantity':quantity,'positionDate':'2025-01-31'}))
  transfer=self.stage('securities_transfer','02_partial_transfer_effective_2025-02-15.pdf','page 1',{'investor':investor,'fromSubfund':'Horizon Credit Fund A (SF-A)','fromAccount':'CUST-ALPHA-01','toSubfund':'Horizon Credit Fund B (SF-B)','toAccount':'CUST-ALPHA-02','isin':isin,'quantity':'200000','effectiveDate':'2025-02-15'});self.confirm(transfer)
  before={x['account']:x['quantity'] for x in self.service.holdings_as_of({'asOfDate':'2025-02-14'})['positions']};after={x['account']:x['quantity'] for x in self.service.holdings_as_of({'asOfDate':'2025-02-16'})['positions']};self.assertEqual(before,{'CUST-ALPHA-01':'600000','CUST-ALPHA-02':'800000','CUST-ALPHA-03':'500000'});self.assertEqual(after,{'CUST-ALPHA-01':'400000','CUST-ALPHA-02':'1000000','CUST-ALPHA-03':'500000'})
  for index,(fund,account,quantity) in enumerate([('Horizon Credit Fund A (SF-A)','CUST-ALPHA-01','400000'),('Horizon Credit Fund B (SF-B)','CUST-ALPHA-02','1000000'),('Meridian Income Fund C (SF-C)','CUST-ALPHA-03','500000')],8):
   staged=self.stage('holding_snapshot',later,f'Holdings!A{index}',{'investor':investor,'subfund':fund,'account':account,'isin':isin,'quantity':quantity,'positionDate':'2025-02-28'})
   if account=='CUST-ALPHA-02':
    with self.assertRaises(ReconciliationError):self.confirm(staged)
   else:self.confirm(staged)
  contradiction=self.stage('holding_snapshot','05_contradictory_custodian_statement_2025-02-28.pdf','page 1',{'investor':investor,'subfund':'Horizon Credit Fund B (SF-B)','account':'CUST-ALPHA-02','isin':isin,'quantity':'900000','positionDate':'2025-02-28'})
  with self.assertRaises(ReconciliationError) as conflict:self.confirm(contradiction)
  self.assertEqual(conflict.exception.code,'CONFLICT_REQUIRES_REVIEW')
  policy=self.service.apply_policy({'candidateIds':[contradiction['id']],'policy':'reviewed_batch','acknowledged':True})
  self.assertEqual(policy['reviewRequired'][0]['reason'],'conflicting_dated_holding')
  assignment=self.stage('claim_assignment','03_claim_right_assignment_effective_2025-02-18.pdf','page 1',{'claimReference':'CR-2025-ALPHA','fromEntity':'Redwood Investors LP','toEntity':'Northstar Claims SPV','isin':isin,'effectiveDate':'2025-02-18'});self.confirm(assignment)
  with self.storage.database.connection() as c:
   self.assertEqual(c.execute('SELECT COUNT(*) FROM holding_positions').fetchone()[0],5);self.assertEqual(c.execute('SELECT COUNT(*) FROM securities_transfers').fetchone()[0],1);self.assertEqual(c.execute('SELECT COUNT(*) FROM claim_right_assignments').fetchone()[0],1);self.assertEqual(c.execute('SELECT effective_date FROM claim_right_assignments').fetchone()[0],'2025-02-18');self.assertEqual(c.execute('SELECT COUNT(*) FROM issues WHERE status="open"').fetchone()[0],1);self.assertEqual(c.execute('SELECT COUNT(*) FROM evidence_references').fetchone()[0],7)
  on_date=self.service.holdings_as_of({'asOfDate':'2025-02-28'})['positions'];self.assertEqual(next(x['reconstructionState'] for x in on_date if x['account']=='CUST-ALPHA-02'),'reconstructed')
  duplicate=self.stage('securities_transfer','02_partial_transfer_effective_2025-02-15.pdf','page 1',{'investor':investor,'fromSubfund':'Horizon Credit Fund A (SF-A)','fromAccount':'CUST-ALPHA-01','toSubfund':'Horizon Credit Fund B (SF-B)','toAccount':'CUST-ALPHA-02','isin':isin,'quantity':'200000','effectiveDate':'2025-02-15'});self.confirm(duplicate)
  with self.storage.database.connection() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM securities_transfers').fetchone()[0],1)

 def test_candidates_keep_distinct_dates_and_reject_unvalidated_ai_output(self):
  filename='01_opening_holdings_2025-01-31.xlsx';payload={'investor':'Redwood Investors LP','subfund':'Horizon Credit Fund A (SF-A)','account':'CUST-ALPHA-01','isin':'XS0123456789','quantity':'600000','positionDate':'2025-01-31'}
  candidate=self.stage('holding_snapshot',filename,'Holdings!A8',payload)
  self.assertEqual(candidate['status'],'candidate');self.assertEqual(candidate['origin'],'deterministic');self.assertEqual(candidate['documentIssueDate'],'2025-02-02')
  with self.assertRaises(ReconciliationError) as blocked:self.service.confirm({'candidateId':candidate['id'],'acknowledged':False})
  self.assertEqual(blocked.exception.code,'CONFIRMATION_REQUIRED')
  ai_params={'type':'holding_snapshot','sourceDocumentId':self.ids[filename],'sourceReference':'Holdings!A8','evidenceSnippet':'Synthetic source evidence','documentIssueDate':'2025-02-02','origin':'ai_validated','payload':payload}
  with self.assertRaises(ReconciliationError) as rejected:self.service.stage(ai_params)
  self.assertEqual(rejected.exception.code,'UNVALIDATED_AI_PROPOSAL')
  unsupported=dict(payload);unsupported['quantity']='700000'
  with self.assertRaises(ReconciliationError) as unsupported_error:self.stage('holding_snapshot',filename,'Holdings!A8',unsupported)
  self.assertEqual(unsupported_error.exception.code,'UNSUPPORTED_EVIDENCE')
  with self.storage.database.connection() as c:
   row=c.execute('SELECT d.imported_at,c.document_issue_date,c.created_at,p.position_date FROM reconciliation_candidates c JOIN source_documents d ON d.id=c.source_document_id LEFT JOIN holding_positions p ON p.candidate_id=c.id WHERE c.id=?',(candidate['id'],)).fetchone()
  self.assertIsNotNone(row['imported_at']);self.assertEqual(row['document_issue_date'],'2025-02-02');self.assertIsNotNone(row['created_at']);self.assertIsNone(row['position_date'])

 def test_duplicate_notice_does_not_duplicate_transfer_and_overdraw_is_rejected(self):
  investor='Redwood Investors LP';isin='XS0123456789';opening='01_opening_holdings_2025-01-31.xlsx';base={'investor':investor,'subfund':'Horizon Credit Fund A (SF-A)','account':'CUST-ALPHA-01','isin':isin,'quantity':'600000','positionDate':'2025-01-31'}
  self.confirm(self.stage('holding_snapshot',opening,'Holdings!A8',base))
  original={'investor':investor,'fromSubfund':'Horizon Credit Fund A (SF-A)','fromAccount':'CUST-ALPHA-01','toSubfund':'Horizon Credit Fund B (SF-B)','toAccount':'CUST-ALPHA-02','isin':isin,'quantity':'200000','effectiveDate':'2025-02-15'}
  self.confirm(self.stage('securities_transfer','02_partial_transfer_effective_2025-02-15.pdf','page 1',original))
  duplicate=self._import_notice('duplicate-transfer.pdf',self._transfer_notice('200,000','15 February 2025','20 February 2025')+'\nDuplicate custodian notice.')
  duplicate_candidate=self.service.stage({'type':'securities_transfer','sourceDocumentId':duplicate,'sourceReference':'page 1','evidenceSnippet':'Duplicate source','documentIssueDate':'2025-02-20','origin':'deterministic','payload':original})
  self.confirm(duplicate_candidate)
  overdraw=self._import_notice('overdraw-transfer.pdf',self._transfer_notice('500,000','16 February 2025','22 February 2025'))
  overdraw_payload=dict(original);overdraw_payload.update({'quantity':'500000','effectiveDate':'2025-02-16'})
  candidate=self.service.stage({'type':'securities_transfer','sourceDocumentId':overdraw,'sourceReference':'page 1','evidenceSnippet':'Overdraw source','documentIssueDate':'2025-02-22','origin':'deterministic','payload':overdraw_payload})
  with self.assertRaises(ReconciliationError) as rejected:self.confirm(candidate)
  self.assertEqual(rejected.exception.code,'IMPOSSIBLE_TRANSFER')
  with self.storage.database.connection() as c:
   self.assertEqual(c.execute('SELECT COUNT(*) FROM securities_transfers').fetchone()[0],1)
   self.assertEqual(c.execute("SELECT lifecycle_status FROM reconciliation_candidates WHERE id=?",(candidate['id'],)).fetchone()[0],'candidate')

 def _import_notice(self,filename,text):
  path=Path(self.tmp.name)/'sources'/filename;pdf=pymupdf.open();pdf.new_page().insert_textbox(pymupdf.Rect(72,72,520,720),text,fontsize=10);pdf.save(path);pdf.close()
  result=DocumentImportService(self.storage).import_authorized_paths([str(path)])['documents'][0]
  self.assertTrue(result['ok']);return result['document']['id']

 @staticmethod
 def _transfer_notice(quantity,effective_date,issue_date):
  return f'''SYNTHETIC - PARTIAL SECURITIES TRANSFER NOTICE\nIssue date: {issue_date}\nEffective date: {effective_date}\nInvestor group: Redwood Investors LP\nAtlas Custody Bank confirms a partial securities transfer of {quantity} nominal units of ISIN XS0123456789 from Horizon Credit Fund A (SF-A), account CUST-ALPHA-01, to Horizon Credit Fund B (SF-B), account CUST-ALPHA-02.'''
