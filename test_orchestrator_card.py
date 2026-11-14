"""Fixed combination presentation from synthetic records, without a bridge service."""
import json
import os
from pathlib import Path
import subprocess
import unittest

import task_card
from viewer import PROFILE_KEYS, SUMMARY_KEYS


class OrchestratorCardTests(unittest.TestCase):
    def test_public_configuration_from_each_record_source(self):
        metadata = {'mode': 'orchestrated', 'effort_mode': 'fixed',
                    'label': 'Planner 规划 · Worker 执行',
                    'components': [{'model': 'planner', 'role': 'planner',
                                    'label': 'Planner', 'private': 'not public'}],
                    'private': 'not public'}
        for source in ('state', 'report', 'profile'):
            with self.subTest(source=source):
                value = {'state': {'job_id': 'synthetic', 'execution_mode': 'workspace'},
                         source: {'model_execution': metadata}}
                card = task_card.compact_card(value)
                self.assertEqual(card['model_execution']['label'], metadata['label'])
                self.assertNotIn('private', json.dumps(card))
        self.assertIn('model_execution', PROFILE_KEYS)
        self.assertIn('model_execution', SUMMARY_KEYS)
        ordinary = task_card.compact_card({'state': {'model': 'ordinary', 'effort': 'high'}})
        self.assertEqual(ordinary['requested_effort'], 'high')
        self.assertEqual(ordinary['model_execution'], {})

    def test_rendered_fixed_configuration_and_ordinary_regression(self):
        preview = Path(os.environ.get('GEMINI_BRIDGE_PREVIEW_PYTHON') or
                       Path(__file__).resolve().parent/'.venv-preview/bin/python')
        if not preview.exists():
            self.skipTest('Existing preview environment unavailable')
        script = r'''
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
root = Path.cwd()
execution = {'mode':'orchestrated','effort_mode':'fixed','label':'Planner 规划 · Worker 执行'}
card = {'job_id':'synthetic','title':'固定组合显示检查','status':'completed_unreviewed',
        'requested_model':'combo','requested_effort':'fixed','model_execution':execution,
        'observed_model':'combo','review':'pending','created_at':1,'updated_at':2,
        'progress':{'stage':'completed','label':'执行结束'},'response':'测试结果'}
detail = {'state':{'job_id':'synthetic','title':card['title'],'kind':'copy','model':'combo',
                  'effort':'fixed','status':card['status'],'model_execution':execution,
                  'execution_mode':'workspace'},
          'report':{'observed_model':'combo'},'progress':{},'submission':{'prompt':'显示检查'}}
with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True)
    for width in (420,1100):
        for native in (False,True):
            page=browser.new_page(viewport={'width':width,'height':900})
            errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.route('**/*',lambda route:route.abort())
            template='native-panel' if native else 'task-card'
            html=(root/'web'/f'{template}.html').read_text()
            html=html.replace('__PANEL_SCRIPT__',(root/'web/native-panel.js').read_text()) if native else html.replace('__CARD_SCRIPT__',(root/'web/task-card.js').read_text()).replace('__CARD_COPY__',(root/'web/task-card-copy.json').read_text())
            page.set_content(html)
            if native:
                page.evaluate('(v)=>{receive({structuredContent:{card:v.card},_meta:{panel_detail:v.detail,panel_card:v.card}});applyMode("fullscreen")}',{'card':card,'detail':detail})
            else:
                page.evaluate('(v)=>render({structuredContent:{card:v}})',card)
            assert execution['label'] in page.locator('#model').inner_text()
            assert page.locator('#effort').inner_text()=='思考 系统固定'
            assert 'High' not in page.locator('#effort').inner_text()
            if native:
                for tab in ('submission','review'):
                    page.locator('[data-tab="'+tab+'"]').click()
                    assert '系统固定' in page.locator('#content').inner_text()
                assert '未逐步追踪' in page.locator('#content').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            page.wait_for_timeout(350)  # Allow the existing pane transition to settle for visual QA.
            page.screenshot(path=f'/tmp/orchestrator-{template}-{width}.png',full_page=True)
            page.evaluate('renderModelHeader({requested_model:"ordinary-high",observed_model:"ordinary-high",requested_effort:"high"})')
            assert page.locator('#effort').inner_text()=='思考 High'
            assert page.locator('#model').inner_text()=='模型 · Ordinary'
            for verdict,label in [('accepted','已采纳'),('accepted_with_changes','修订后采纳'),('revision_requested','待修改'),('rejected','未采纳'),('partially_accepted','部分采纳')]:
                review={'verdict':verdict,'note':'保留有效结论，局部问题另行处理。','accepted_items':['有效结论 <安全文本>'],'issues':['补充一个依据'],'changes':['Codex 已修订措辞'],
                        'history':[{'verdict':'revision_requested','note':'请解释一个前提','reviewed_at':1},{'verdict':verdict,'note':'复验记录','reviewed_at':2}]}
                card.update(review=verdict,review_note=review['note'],accepted_items=review['accepted_items'],review_issues=review['issues'],review_changes=review['changes'])
                detail['state']['review']=verdict;detail['acceptance']=review
                if native:
                    page.evaluate('(v)=>receive({structuredContent:{card:v.card},_meta:{panel_detail:v.detail,panel_card:v.card}})',{'card':card,'detail':detail})
                    page.locator('[data-tab="review"]').click()
                    text=page.locator('#content').inner_text()
                    assert '有效结论 <安全文本>' in text and '补充一个依据' in text and 'Codex 已修订措辞' in text
                    assert page.locator('[data-details-key="review-history"]').count()==1
                else:
                    page.evaluate('(v)=>render({structuredContent:{card:v}})',card)
                    assert '有效结论 <安全文本>' in page.locator('#review-note').text_content()
                assert page.locator('#status').inner_text()==label
            page.wait_for_timeout(350)
            page.screenshot(path=f'/tmp/bridge-partial-{template}-{width}.png',full_page=True)
            assert not errors, errors
            page.close()
        page=browser.new_page(viewport={'width':width,'height':900})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        html=(root/'web/index.html').read_text().replace('<link rel="stylesheet" href="/style.css">','<style>'+(root/'web/style.css').read_text()+'</style>').replace('<script src="/app.js" defer></script>','')
        page.set_content(html)
        source=(root/'web/app.js').read_text()
        # Omit startup/refresh listeners: render the real views with inert static records.
        source=source[:source.index("$('history-toggle').addEventListener")]
        page.add_script_tag(content=source)
        page.evaluate('(d)=>{app.detail=d;renderHeader();setTab("submission")}',detail)
        assert execution['label'] in page.locator('#task-model').inner_text()
        assert '系统固定' in page.locator('#content').inner_text()
        page.evaluate('setTab("review")')
        assert '系统固定' in page.locator('#content').inner_text()
        assert '未逐步追踪' in page.locator('#content').inner_text()
        page.wait_for_timeout(350)  # Existing pane animation starts transparent.
        page.screenshot(path=f'/tmp/orchestrator-local-detail-{width}.png',full_page=True)
        page.evaluate('setTab("progress")')
        page.locator('details summary').click()
        assert '已报告组合选项' in page.locator('#content').inner_text()
        page.evaluate('(d)=>{app.detail=d;renderHeader();setTab("review")}',detail)
        assert '部分采纳' in page.locator('#content').inner_text()
        assert '有效结论 <安全文本>' in page.locator('#content').inner_text()
        assert '检查记录 · 2 次' in page.locator('#content').inner_text()
        page.wait_for_timeout(350)
        page.screenshot(path=f'/tmp/bridge-partial-local-detail-{width}.png',full_page=True)
        assert not errors,errors
        page.close()
    browser.close()
print('Rendered legacy/native/local fixed configuration at 420/1100 px; ordinary High preserved')
'''
        result = subprocess.run([str(preview), '-c', script], cwd=task_card.ROOT,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
