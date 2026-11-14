"""Content review may retain useful parts without changing execution evidence."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import jobs
import mcp_server
import task_card
from retry_policy import decision


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.job_id='20261114T000000Z-12345678'
        self.job=self.root/'runs'/self.job_id;self.job.mkdir(parents=True)
        self.state={'job_id':self.job_id,'status':'completed_unreviewed','review':'pending','acceptance':['Preserve the supported findings']}
        self.report={'job_id':self.job_id,'response':'Useful supported findings and one disputed point','failure_reasons':[]}
        self.save()
        self.owner=jobs.Jobs.__new__(jobs.Jobs);self.owner.lock=threading.RLock()
        self.patch=patch.object(jobs,'ROOT',self.root);self.patch.start();self.addCleanup(self.patch.stop)

    def save(self):
        (self.job/'state.json').write_text(json.dumps(self.state))
        (self.job/'report.json').write_text(json.dumps(self.report))

    def review(self,verdict,**fields):
        return self.owner.review(self.job_id,{'verdict':verdict,'note':'Keep supported findings; correct only the disputed point.',**fields})

    def test_partial_explanation_and_revised_acceptance_keep_history(self):
        with patch.object(jobs,'run_job') as generate:
            self.review('partially_accepted',accepted_items=['Supported findings'],issues=['One disputed point'])
            self.review('revision_requested',accepted_items=['Supported findings'],issues=['Explain the disputed assumption'])
            state=self.review('accepted_with_changes',accepted_items=['Supported findings','Corrected point'],changes=['Codex corrected the unsupported assumption'])
        generate.assert_not_called()
        self.assertEqual(state['review'],'accepted_with_changes')
        self.assertEqual(state['status'],'completed_unreviewed')
        record=json.loads((self.job/'acceptance.json').read_text())
        self.assertEqual([x['verdict'] for x in record['history']],['partially_accepted','revision_requested','accepted_with_changes'])
        self.assertEqual(record['history'][0]['accepted_items'],['Supported findings'])
        self.assertEqual(json.loads((self.job/'report.json').read_text())['response'],self.report['response'])

    def test_legacy_review_is_preserved_when_reconsidered(self):
        old={'verdict':'rejected','note':'Earlier assessment','reviewed_at':1}
        (self.job/'acceptance.json').write_text(json.dumps(old))
        self.review('partially_accepted',accepted_items=['Rechecked finding'])
        history=json.loads((self.job/'acceptance.json').read_text())['history']
        self.assertEqual(history[0],old);self.assertEqual(len(history),2)

    def test_usable_partial_delivery_does_not_erase_execution_failure(self):
        self.state['status']='failed_or_unverified';self.report['failure_reasons']=['output_contract_warning'];self.save()
        self.review('partially_accepted',accepted_items=['Verified useful paragraph'],issues=['Incomplete appendix'])
        self.assertEqual(self.owner.state(self.job_id)['status'],'failed_or_unverified')
        stored=json.loads((self.job/'report.json').read_text())
        self.assertEqual(stored['failure_reasons'],['output_contract_warning'])
        self.assertEqual(stored['artifact_review'],'partially_accepted')
        self.assertFalse(decision(self.owner.state(self.job_id),stored)['allowed'])

    def test_no_output_cannot_be_mislabeled_as_adopted(self):
        self.state['status']='cancelled';self.report['response']='Agent execution terminated due to error.';self.save()
        with self.assertRaisesRegex(ValueError,'No existing content'):self.review('partially_accepted')
        self.review('revision_requested',issues=['No usable result returned'])
        self.state['status']='running';self.save()
        with self.assertRaisesRegex(ValueError,'execution to stop'):self.review('accepted')

    def test_card_projects_review_details_without_internal_extensions(self):
        self.review('partially_accepted',accepted_items=['Adopted <text>'],issues=['Question'],changes=['Small edit'])
        record=json.loads((self.job/'acceptance.json').read_text());record['private']='not for card';record['history'][0]['private']='not for card'
        card=task_card.compact_card({'state':self.owner.state(self.job_id),'report':self.report},record)
        self.assertEqual(card['accepted_items'],['Adopted <text>'])
        self.assertEqual(card['review_issues'],['Question']);self.assertEqual(card['review_changes'],['Small edit'])
        self.assertNotIn('not for card',json.dumps(card))

    def test_mcp_forwards_new_verdict_and_optional_items_without_generation(self):
        args={'job_id':self.job_id,'verdict':'partially_accepted','note':'Useful paragraphs retained','accepted_items':['A'],'issues':['B']}
        with patch.object(mcp_server,'service_request',return_value={}) as service:
            mcp_server.call_tool('bridge_review',args)
        service.assert_called_once_with('/jobs/'+self.job_id+'/review',{k:v for k,v in args.items() if k!='job_id'})
        tool=next(x for x in mcp_server.TOOLS if x['name']=='bridge_review')
        self.assertIn('accepted_with_changes',tool['inputSchema']['properties']['verdict']['enum'])


if __name__=='__main__':unittest.main()
