import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from visit_workflow.config import Settings
from visit_workflow.server import create_app


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = create_app(self.root, Settings(api_key='kimi-test', credentials={'deepseek':'deepseek-test'}), api_token='service-test')
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.headers = {'Authorization': 'Bearer service-test'}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_auth_covers_legacy_and_reports(self):
        for path in ['/api/config','/api/jobs','/api/v1/jobs/0123456789abcdef','/reports/0123456789abcdef/report.html','/']:
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get('/healthz').status_code, 200)
        self.assertEqual(self.client.get('/api/config',headers=self.headers).status_code,200)
        self.assertEqual(self.client.get('/api/config',headers={'Authorization':'Bearer wrong'}).status_code,401)
        basic = base64.b64encode(b'api:service-test').decode()
        self.assertEqual(self.client.get('/',headers={'Authorization':'Basic '+basic}).status_code,200)
        self.assertEqual(self.client.get('/openapi.json').status_code,200)
        schema=self.client.get('/openapi.json').json()
        self.assertIn('security',schema['paths']['/api/v1/jobs']['post'])

    def test_submit_snapshot_does_not_change_global_config(self):
        with patch.object(self.app.state.manager,'start') as start:
            response=self.client.post('/api/v1/jobs',headers=self.headers,json={
                'text':'客户：我想看四房。', 'filename':'客户_20260830.txt',
                'options':{'provider':'deepseek','model':'deepseek-v4-pro','max_concurrency':8}})
            self.assertEqual(response.status_code,202,response.text)
            result=response.json(); selected=start.call_args.args[1]
            self.assertEqual(selected.api_key,'deepseek-test')
            self.assertEqual(selected.model,'deepseek-v4-pro')
            self.assertEqual(selected.max_concurrency,8)
            self.assertEqual(self.client.get('/api/config',headers=self.headers).json()['model'],'k3')
            self.assertEqual(self.client.get(result['links']['status'],headers=self.headers).status_code,200)
            self.assertEqual(self.client.get(result['links']['usage'],headers=self.headers).status_code,200)
            self.assertEqual(self.client.get(result['links']['report_html'],headers=self.headers).status_code,404)
            folder=self.root/result['id']
            meta=json.loads((folder/'input.json').read_text('utf-8'))['meta']
            self.assertEqual(meta['visit_date'],'2026-08-30')
            (folder/'report.html').write_text('<html>test report</html>',encoding='utf-8')
            self.assertEqual(self.client.get(result['links']['report_html'],headers=self.headers).status_code,200)
            self.assertNotIn('deepseek-test',response.text)

    def test_invalid_requests_never_enqueue(self):
        with patch.object(self.app.state.manager,'start') as start:
            for body in [
                {'text':''}, {'text':'客户：四房','options':{'model':'unknown'}},
                {'text':'客户：四房','options':{'api_key':'not-allowed'}},
                {'text':'客户：四房','options':{'max_concurrency':9}},
                {'text':'客户：四房','filename':'bad.exe'},
                {'text':'中'*800000}]:
                r=self.client.post('/api/v1/jobs',headers=self.headers,json=body)
                self.assertIn(r.status_code,[400,422],r.text[:100])
            start.assert_not_called()
        r=self.client.post('/api/v1/jobs',headers={**self.headers,'Origin':'https://external.example'},json={'text':'客户：四房'})
        self.assertEqual(r.status_code,403)

    def test_upload_and_missing_key(self):
        with patch.object(self.app.state.manager,'start'):
            r=self.client.post('/api/v1/jobs/upload',headers=self.headers,files={'file':('visit.txt','客户：四房'.encode())})
            self.assertEqual(r.status_code,202,r.text)
        with tempfile.TemporaryDirectory() as tmp, TestClient(create_app(Path(tmp),Settings(),api_token='service-test')) as c:
            r=c.post('/api/v1/jobs',headers=self.headers,json={'text':'客户：四房'})
            self.assertEqual(r.status_code,400)


if __name__ == '__main__':
    unittest.main()
