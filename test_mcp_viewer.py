"""Viewer integration must never change dispatch, review, or execution outcomes."""
import json
import sys
import unittest
from unittest.mock import patch
import mcp_server


class ViewerIntegrationTests(unittest.TestCase):
    def test_legacy_status_does_not_require_optional_http_dependencies(self):
        original={'state':{'job_id':'legacy','status':'running'}}
        with patch.object(mcp_server,'call_tool',return_value=original), \
             patch.object(mcp_server.progress,'enrich',side_effect=lambda v:v), \
             patch.object(mcp_server.viewer,'attach_links',side_effect=lambda v:v), \
             patch('cli_updates.attach_notice',side_effect=lambda v, **kwargs:v), \
             patch.dict(sys.modules,{'task_card':None}):
            result=mcp_server.handle({'jsonrpc':'2.0','id':1,'method':'tools/call',
                'params':{'name':'bridge_status','arguments':{'job_id':'legacy'}}})['result']
        self.assertNotIn('isError',result)
        self.assertEqual(json.loads(result['content'][0]['text'])['state'],original['state'])

    def test_app_status_metadata_does_not_render_another_panel_or_leak_input(self):
        value = {'state':{'job_id':'viewer-integration','status':'completed_unreviewed','review':'accepted'}}
        with patch.object(mcp_server,'call_tool',return_value=value), \
             patch.object(mcp_server.progress,'enrich',side_effect=lambda v:v), \
             patch.object(mcp_server.viewer,'attach_links',side_effect=lambda v:v), \
             patch('cli_updates.attach_notice',side_effect=lambda v, **kwargs:v), \
             patch('task_card.review_record',return_value={'note':'Checked against source'}), \
             patch('task_card.panel_snapshot',return_value={'prompt':'selected private input','report':{'response':'full delivery'}}):
            result=mcp_server.handle({'jsonrpc':'2.0','id':1,'method':'tools/call',
                'params':{'name':'bridge_status','arguments':{'job_id':'viewer-integration'}}})['result']
        self.assertNotIn('selected private input',json.dumps(result['content']))
        self.assertEqual(result['_meta']['panel_detail']['report']['response'],'full delivery')
        self.assertEqual(result['_meta']['panel_card']['review_note'],'Checked against source')
        self.assertNotIn('ui',result['_meta'])
        self.assertNotIn('openai/outputTemplate',result['_meta'])

    def call(self, name, args):
        result = mcp_server.handle({'jsonrpc':'2.0','id':1,'method':'tools/call',
                                   'params':{'name':name,'arguments':args}})['result']
        self.assertNotIn('isError', result)
        return json.loads(result['content'][0]['text'])

    def test_submit_status_review_share_one_view_url(self):
        state = {'job_id':'viewer-integration','status':'completed_unreviewed','review':'accepted'}
        with patch.object(mcp_server, 'service_request') as service, \
             patch.object(mcp_server.progress, 'enrich', side_effect=lambda v:v), \
             patch.object(mcp_server.viewer, 'ensure_running', return_value={'url':'http://127.0.0.1:12345/'}), \
             patch('cli_updates.attach_notice', side_effect=lambda v, **kwargs:v):
            service.return_value = state
            submitted = self.call('bridge_submit', {'prompt':'synthetic contract', 'dispatch_mode':'auto'})
            service.assert_called_with('/jobs', {'prompt':'synthetic contract', 'dispatch_mode':'auto'})
            service.return_value = {'state':state, 'report':{'response':'original'}}
            status = self.call('bridge_status', {'job_id':state['job_id']})
            reviewed = self.call('bridge_review', {'job_id':state['job_id'], 'verdict':'accepted','note':'checked'})
            self.assertEqual(submitted['task_view_url'], status['task_view_url'])
            self.assertEqual(status['task_view_url'], reviewed['task_view_url'])
            self.assertEqual(status['state'], state)
            self.assertEqual(status['report']['response'], 'original')
            service.assert_called_with('/jobs/viewer-integration/review', {'verdict':'accepted','note':'checked'})

    def test_viewer_failure_does_not_turn_success_into_tool_error(self):
        original = {'job_id':'viewer-integration','status':'queued'}
        with patch.object(mcp_server, 'call_tool', return_value=original), \
             patch.object(mcp_server.progress, 'enrich', side_effect=lambda v:v), \
             patch.object(mcp_server.viewer, 'ensure_running', side_effect=OSError('unavailable')), \
             patch('cli_updates.attach_notice', side_effect=lambda v, **kwargs:v):
            value = self.call('bridge_submit', {'prompt':'synthetic'})
        self.assertEqual(value['status'], 'queued')
        self.assertIn('viewer_error', value)
        self.assertEqual(original, {'job_id':'viewer-integration','status':'queued'})


if __name__ == '__main__':
    unittest.main()
