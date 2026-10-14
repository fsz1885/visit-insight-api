import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from visit_workflow.config import Settings
from visit_workflow.demo import build_demo
from visit_workflow.server import create_app


class McpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = create_app(self.root, Settings(api_key='private-provider-secret'), api_token='test-service-token')
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.headers = {'Authorization':'Bearer test-service-token',
                        'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-06-18'}

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.tmp.cleanup()

    def rpc(self, method, params=None):
        r=self.client.post('/mcp',headers=self.headers,json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})
        self.assertEqual(r.status_code,200,r.text)
        return r.json()['result']

    def call(self, name, **arguments):
        return self.rpc('tools/call',{'name':name,'arguments':arguments})

    def test_initialize_discovery_and_auth(self):
        result=self.rpc('initialize',{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'test','version':'1'}})
        self.assertEqual(result['serverInfo']['name'],'visit-insight')
        tools={t['name']:t for t in self.rpc('tools/list')['tools']}
        self.assertEqual(len(tools),9)
        self.assertTrue(tools['get_visit_report']['annotations']['readOnlyHint'])
        self.assertFalse(tools['submit_visit_report']['annotations']['idempotentHint'])
        result=self.call('get_visit_service_info')
        self.assertFalse(result.get('isError'))
        self.assertEqual(result['structuredContent']['model'],'k3')
        self.assertNotIn('private-provider-secret',json.dumps(result))
        for headers in [{}, {'Authorization':'Bearer wrong'},{'Authorization':'Basic YXBpOnRlc3Qtc2VydmljZS10b2tlbg=='}]:
            self.assertEqual(self.client.post('/mcp',headers=headers,json={}).status_code,401)
        self.assertEqual(self.client.post('/mcp',headers={**self.headers,'Origin':'https://evil.example'},json={}).status_code,403)
        with tempfile.TemporaryDirectory() as tmp, TestClient(create_app(Path(tmp),Settings(),api_token='')) as c:
            self.assertEqual(c.post('/mcp',json={}).status_code,503)

    def test_submission_poll_report_evidence_and_usage(self):
        with patch.object(self.app.state.manager,'start') as start:
            result=self.call('submit_visit_report',text='客户：想看四房。',filename='客户_20260830.txt',max_concurrency=8)
            self.assertFalse(result.get('isError'),result)
            job=result['structuredContent']; job_id=job['id']
            self.assertEqual(start.call_args.args[1].max_concurrency,8)
        status=self.call('get_visit_report_status',job_id=job_id)['structuredContent']
        self.assertEqual(status['id'],job_id)
        self.assertNotIn('events',status)
        self.assertTrue(self.call('get_visit_report',job_id=job_id)['isError'])
        report=build_demo()
        report['quality']['report_status']='needs_review'
        report['quality']['issues']=[{'severity':'warning','problem':'fixture review'}]
        folder=self.root/job_id
        (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False),encoding='utf-8')
        (folder/'evidence.json').write_text(json.dumps(report['evidence'],ensure_ascii=False),encoding='utf-8')
        self.app.state.manager.update(job_id,status='needs_review',progress=100)
        summary=self.call('get_visit_report',job_id=job_id)['structuredContent']
        self.assertEqual(summary['status'],'needs_review')
        self.assertNotIn('evidence',summary)
        self.assertEqual(len(summary['issues']),1)
        evidence=self.call('get_visit_evidence',job_id=job_id,evidence_ids=['E00001','E99999'])['structuredContent']
        self.assertEqual(list(evidence['evidence']),['E00001'])
        self.assertEqual(evidence['missing'],['E99999'])
        usage=self.call('get_visit_token_usage',job_id=job_id)['structuredContent']
        self.assertIn('summary',usage)
        self.assertNotIn('calls',usage)
        jobs=self.call('list_visit_reports',limit=1)['structuredContent']
        self.assertEqual(jobs['total'],1)
        self.assertEqual(jobs['jobs'][0]['id'],job_id)

    def test_invalid_input_and_control_tools(self):
        with patch.object(self.app.state.manager,'start') as start:
            for params in [{'text':''},{'text':'客户：四房','max_concurrency':9},
                           {'text':'客户：四房','provider':'deepseek','model':'deepseek-v4-pro'}]:
                self.assertTrue(self.call('submit_visit_report',**params)['isError'])
            start.assert_not_called()
        self.assertTrue(self.call('get_visit_report_status',job_id='../../model.json')['isError'])
        self.assertTrue(self.call('get_visit_report_status',job_id='0123456789abcdef')['isError'])
        self.assertTrue(self.call('get_visit_evidence',job_id='0123456789abcdef',evidence_ids=['x']*21)['isError'])
        with patch.object(self.app.state.manager,'start'):
            job=self.call('submit_visit_report',text='客户：四房')['structuredContent']
        with patch.object(self.app.state.manager,'stop') as stop:
            self.assertFalse(self.call('stop_visit_report',job_id=job['id']).get('isError'))
            stop.assert_called_once_with(job['id'])
        self.app.state.manager.update(job['id'],status='interrupted')
        with patch.object(self.app.state.manager,'start') as start:
            self.assertFalse(self.call('resume_visit_report',job_id=job['id']).get('isError'))
            start.assert_called_once()


if __name__=='__main__':
    unittest.main()
