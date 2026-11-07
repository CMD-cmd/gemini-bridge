"""Native UI probe must not change ordinary cards or dispatch a model."""
import json
import unittest
from unittest.mock import Mock, patch

from mcp.server import Server
import task_card


class PanelIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def result(self, enabled):
        value = {'state':{'job_id':'existing-task','status':'completed_unreviewed',
                          'review':'accepted'}, 'report':{'response':'compact delivery'}}
        full = {'prompt':'private selected input', 'report':{'response':'full delivery'}}
        registration = task_card.register(Server('test-card'))
        handler = Mock(return_value={'result':{'content':[{'type':'text','text':json.dumps(value)}]}})
        with patch('mcp_server.handle', handler), \
             patch.object(task_card, 'review_record', return_value=None), \
             patch.object(task_card, 'panel_probe_enabled', return_value=enabled), \
             patch.object(task_card, 'panel_snapshot', return_value=full) as snapshot:
            result = await registration['call_tool']('bridge_native_panel' if enabled else 'bridge_task_card', {'job_id':'existing-task'})
        self.assertEqual(handler.call_args.args[0]['params']['name'], 'bridge_status')
        self.assertEqual(handler.call_count, 1)
        self.assertNotIn('private selected input', json.dumps(result.structuredContent))
        self.assertNotIn('private selected input', ''.join(item.text for item in result.content))
        return result, snapshot

    async def test_ordinary_cards_keep_original_resource_and_no_full_record_read(self):
        result, snapshot = await self.result(False)
        self.assertEqual(result.meta, task_card.META)
        snapshot.assert_not_called()

    async def test_opted_in_task_returns_full_record_only_in_widget_metadata(self):
        result, snapshot = await self.result(True)
        self.assertEqual(result.meta['ui']['resourceUri'], task_card.PANEL_URI)
        self.assertEqual(result.meta['panel_detail']['report']['response'], 'full delivery')
        self.assertEqual(result.structuredContent, {'job_id':'existing-task'})
        self.assertEqual(result.meta['panel_card']['response'], 'compact delivery')
        snapshot.assert_called_once_with('existing-task')

    async def test_empty_thread_entrypoint_offers_picker_without_reading_or_dispatching(self):
        registration = task_card.register(Server('empty-entrypoint'))
        with patch.object(task_card, 'panel_probe_enabled', return_value=False), \
             patch('mcp_server.handle') as handler:
            result = await registration['call_tool']('bridge_native_panel', {})
        self.assertEqual(result.structuredContent, {'needs_job_id':True})
        self.assertFalse(result.isError)
        handler.assert_not_called()



class RealtimeContractTests(unittest.TestCase):
    def test_activity_projection_survives_compact_card(self):
        activity={'current_action':'读取文件','recent_events':[{'action':'读取文件'}],
                  'public_response_preview':'公开回复','children':[{'short_id':'abcd1234','status':'远端未知'}]}
        card=task_card.compact_card({'state':{'job_id':'existing-task','status':'running'},'progress':{'activity':activity}})
        self.assertEqual(card['progress']['activity'],activity)

    def test_only_matching_verified_entries_receive_fixed_tool_provenance(self):
        entries=[{'id':'1','name':'notes.json','sha256':'a','attachment_url':'/safe/link'},
                 {'id':'2','name':'result.md','sha256':'b'}]
        manifest=[{'id':'1','sha256':'a','source':'trusted_project_tool','path':'PRIVATE_SOURCE'},
                  {'id':'2','sha256':'wrong','source':'trusted_project_tool'}]
        projected=task_card.delivery_sources(entries,manifest)
        self.assertEqual(projected[0]['source'],'trusted_project_tool')
        self.assertEqual(projected[0]['attachment_url'],'/safe/link')
        self.assertNotIn('source',projected[1])
        self.assertNotIn('PRIVATE_SOURCE',json.dumps(projected))
        self.assertNotIn('source',entries[0])
        self.assertIn('native-panel-v5.html',str(task_card.LEGACY_PANEL_URIS))
        self.assertTrue(task_card.PANEL_URI.endswith('native-panel-v9.html'))

    def test_polling_uses_only_read_tools_and_stops_at_terminal(self):
        panel=(task_card.ROOT/'web/native-panel.js').read_text()
        legacy=(task_card.ROOT/'web/task-card.js').read_text()
        self.assertIn('delay=document.hidden||complete()?15000:2000',panel)
        self.assertIn("['queued','running','cancelling'].includes(card.status)",panel)
        self.assertIn("name:'bridge_status'",panel)
        self.assertNotIn('ui/message',panel)
        self.assertNotIn('sendFollowUpMessage',panel)
        self.assertIn('delay=document.hidden||terminal.has(card?.status)?15000:2000',legacy)
        self.assertIn('needsLiveRefresh()',legacy)
        refresh=legacy[legacy.index('async function refresh('):legacy.index("$('refresh').addEventListener")]
        self.assertIn("name:'bridge_task_card'",refresh)
        self.assertNotIn('ui/message',refresh)
        self.assertNotIn('sendFollowUpMessage',refresh)

    def test_dynamic_activity_is_escaped_or_text_only(self):
        panel=(task_card.ROOT/'web/native-panel.js').read_text()
        legacy=(task_card.ROOT/'web/task-card.js').read_text()
        self.assertIn('escapeHTML(a.public_response_preview)',panel)
        self.assertIn('escapeHTML(c.role)',panel)
        self.assertIn('pre.textContent=a.public_response_preview',legacy)
        html=task_card.panel_resource()
        self.assertIn('activity-events',html)

class RealtimeDisclosureBrowserTests(unittest.TestCase):
    def test_open_details_focus_and_scroll_survive_native_and_legacy_refresh(self):
        import subprocess
        python=task_card.ROOT/'.venv-preview/bin/python'
        if not python.exists():self.skipTest('Existing preview browser environment unavailable')
        script=r'''import json
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path.cwd()
with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True)
    context=browser.new_context()
    context.route('**/*',lambda route:route.abort())
    for native in (True,False):
        page=context.new_page()
        if native:
            html=(root/'web/native-panel.html').read_text().replace('__PANEL_SCRIPT__',(root/'web/native-panel.js').read_text())
        else:
            html=(root/'web/task-card.html').read_text().replace('__CARD_COPY__',(root/'web/task-card-copy.json').read_text()).replace('__CARD_SCRIPT__',(root/'web/task-card.js').read_text())
        page.set_content(html)
        activity={'current_action':'读取文件','last_effective_activity_at':1700000000,'recent_events':[{'action':'读取文件','state':'done'}],'public_response_preview':'\n'.join('公开回复 '+str(i) for i in range(200))}
        card={'job_id':'view-test','title':'Synthetic view','status':'running','review':'pending','created_at':1700000000,'progress':{'activity':activity}}
        send='receive' if native else 'render'
        def update():page.evaluate('(v)=>'+send+'(v)',{'structuredContent':{'card':card}})
        update()
        if native:
            page.evaluate("localExpanded=true;syncViewerVisibility();applyHostContext({displayMode:'inline',theme:'dark'})")
            assert not page.locator('#viewer').evaluate('(e)=>e.hidden'), 'Repeated host context must retain expanded preview'
            page.evaluate("applyMode('fullscreen');applyMode('inline')")
            assert page.locator('#viewer').evaluate('(e)=>e.hidden'), 'Explicit return from details must collapse'
            page.evaluate("applyMode('fullscreen')")
        details=page.locator('[data-details-key="recent-events"]')
        details.evaluate('(el)=>el.open=true')
        summary=details.locator('summary');summary.focus()
        activity['recent_events'].append({'action':'生成预览','state':'running'})
        update()
        assert details.evaluate('(el)=>el.open')
        assert page.evaluate("document.activeElement.tagName==='SUMMARY'&&document.activeElement.parentElement.dataset.detailsKey==='recent-events'")
        pre=page.locator('[data-focus-key="public-response"]')
        pre.evaluate("e=>{e.style.maxHeight='100px';e.style.overflow='auto';e.scrollTop=45;e.focus()}")
        activity['public_response_preview']+='\n另一段公开回复'
        update()
        assert page.evaluate("document.activeElement.dataset.focusKey==='public-response'")
        assert details.evaluate('(el)=>el.open')
        assert pre.evaluate('(el)=>el.scrollTop')>0
        assert len(page.screenshot())>100
        original='Wait for subagent...\nWait for subagent...\nFinal response remains intact.'
        card.update(status='completed_unreviewed',review='accepted',response=original,task_view_url='http://127.0.0.1:8123/?job=view-test')
        card['deliverables']=[{'id':str(i),'name':name,'bytes':12,'sha256':'a','attachment_url':'/api/jobs/view-test/deliveries/'+str(i)+'?ticket=synthetic',**({'source':'trusted_project_tool'} if i==4 else {})} for i,name in enumerate(('result.md','changes.patch','ordinary.json','pubmed_receipt.json'),1)]
        update()
        source=page.locator('[data-details-key="source-records"]')
        assert not source.evaluate('(el)=>el.open')
        assert 'pubmed_receipt.json' in source.text_content()
        summary=page.locator('#delivery-line' if native else '#description').text_content()
        assert '3 个交付文件' in summary and 'Wait' not in summary and 'pubmed_receipt' not in summary
        if native:
            assert page.evaluate('response()')==original
            assert page.locator('[data-delivery-link]').count()==4
        else:
            assert page.locator('#response').text_content()==original
            assert page.locator('#delivery-files a').count()==4
        source.locator('summary').click()
        update()
        assert source.evaluate('(el)=>el.open')
        assert len(page.screenshot())>100
        card['deliverables']=[{'id':str(i),'name':'file-'+str(i)+'.md','bytes':12,'attachment_url':'/api/jobs/view-test/deliveries/'+str(i)+'?ticket=synthetic'} for i in range(33)]
        card['response']='See [result.md](file:///selected/result.md).'
        update()
        files=page.locator('[data-details-key="delivery-files"]')
        assert not files.evaluate('(e)=>e.open')
        assert files.locator('a').count()==33
        files.locator('summary').click()
        update()
        assert files.evaluate('(e)=>e.open')
        if native:
            assert 'file:///' not in page.locator('.prose').inner_text()
            assert page.evaluate('response()')==card['response']
            page.evaluate("async()=>{connected=true;const original=waitForMode;waitForMode=async()=>false;window.openai={requestDisplayMode:async()=>({mode:'fullscreen'})};try{await display('inline')}finally{connected=false;waitForMode=original}}")
            assert '面板的关闭按钮' in page.locator('#notice').text_content()
            assert '正在收起' not in page.locator('#notice').text_content()
        page.close()
    context.close();browser.close()
print('native and legacy disclosure/focus/scroll preserved')
'''
        result=subprocess.run([str(python),'-c',script],cwd=task_card.ROOT,capture_output=True,text=True,timeout=25)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('native and legacy',result.stdout)

if __name__ == '__main__':
    unittest.main()
