from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from visit_workflow.config import ROOT, Settings
from visit_workflow.demo import demo_draft, build_demo
from visit_workflow.ingest import read_file, chunk_segments, resolve_visit_date
from visit_workflow.jobs import create_run
from visit_workflow.llm import KimiClient, WorkflowError, parse_object, safe_api_error, normalize_reference_format
from visit_workflow.models import Analysis, Audit, Claim, Draft, Extraction, Meta
from visit_workflow.pipeline import run_pipeline, validate_refs, validate_draft, numeric_checks
from visit_workflow.render import render_report
from visit_workflow.server import create_app


class IngestTests(unittest.TestCase):
    def test_filename_visit_date_and_manual_override(self):
        for suffix in ['20261005', '2026-10-05', '2026_10_05']:
            self.assertEqual(resolve_visit_date(Meta(), f'7625包女士_淼_{suffix}.txt').visit_date, '2026-10-05')
        for name in ['客户_20260230.txt', '20261005_客户.txt', '客户.txt', '客户_120261005.txt']:
            self.assertEqual(resolve_visit_date(Meta(), name).visit_date, '')
        with tempfile.TemporaryDirectory() as tmp:
            for supplied, expected in [('', '2026-10-05'), ('2026-10-04', '2026-10-04')]:
                folder = create_run('客户：看房'.encode(), '客户_20261005.txt', Meta(visit_date=supplied), Path(tmp))
                saved = json.loads((folder/'input.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['meta']['visit_date'], expected)

    def test_header_followed_by_content_and_ratio(self):
        text = '客户 00:00:01\n我想看四房。\n销售 00:00:04\n车位配比是1:1，月供8:2分配。'
        _, segments, _ = read_file(text.encode(), 'visit.txt')
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[1].speaker, '销售')
        self.assertEqual(segments[1].start_sec, 4)
        self.assertIn('1:1', segments[1].text)

    def test_plain_no_invented_time(self):
        _, segments, warnings = read_file('我们预算三百万\n主要看四房'.encode(), 'visit.txt')
        self.assertEqual([s.line_start for s in segments], [1, 2])
        self.assertTrue(all(s.start_sec is None for s in segments))
        self.assertTrue(warnings)

    def test_subtitle_ranges(self):
        text = '1\n00:00:01,200 --> 00:00:03,500\n客户：要四房\n\n2\n00:00:04,000 --> 00:00:06,000\n销售：好的\n'
        _, s, _ = read_file(text.encode(), 'visit.srt')
        self.assertEqual(s[0].start_sec, 1.2)
        self.assertEqual(s[0].end_sec, 3.5)
        self.assertEqual(s[1].speaker, '销售')

    def test_encodings(self):
        for enc in ['utf-8-sig', 'utf-16', 'gb18030']:
            text, _, _ = read_file('客户：想看四房'.encode(enc), 'visit.txt')
            self.assertIn('四房', text)

    def test_json_and_bounds(self):
        _, s, _ = read_file(json.dumps({'segments':[{'text':'你好','speaker':'S1','start':65,'end':70}]}).encode(), 'visit.json')
        self.assertEqual(s[0].start_sec, 65)
        with self.assertRaises(ValueError):
            read_file(b'[{"text":"x","start":80,"end":70}]','visit.json')
        with self.assertRaises(ValueError):
            read_file(b'[{"text":"x","start":"00:99"}]','visit.json')

    def test_reject_pdf_and_empty(self):
        with self.assertRaises(ValueError): read_file(b'%PDF','report.pdf')
        with self.assertRaises(ValueError): read_file(b'  ','visit.txt')

    def test_chunk_coverage(self):
        _, segments, _ = read_file(('客户：'+('房'*900)+'\n') .encode()*30, 'visit.txt')
        chunks = chunk_segments(segments, 5000)
        self.assertGreater(len(chunks),1)
        self.assertEqual({s.id for s in segments},{s.id for c in chunks for s in c})


class EvidenceTests(unittest.TestCase):
    def test_known_claim_needs_evidence(self):
        with self.assertRaises(ValueError): Claim(text='预算300万',status='明确',evidence_refs=[])
        Claim(text='未提及',status='待验证',evidence_refs=[])

    def test_bad_reference_and_bad_blocker(self):
        draft = demo_draft()
        ids = set(build_demo()['evidence'])
        validate_draft(draft, ids)
        with self.assertRaises(ValueError): validate_refs({'evidence_refs':['E99999']}, ids)
        draft.plan.actions[0].blocker_ref='does-not-exist'
        with self.assertRaises(ValueError): validate_draft(draft, ids)

    def test_numeric_inconsistent_scenario(self):
        facts=[{'subject':'A房','numeric':{'kind':kind,'amount':value,'unit':'万元','scenario':'同日报价'},'evidence_refs':['E00001']} for kind,value in [('总价',300),('首付',90),('贷款',250)]]
        issues=numeric_checks({'facts':facts,'conflicts':[],'possible_multiple_visits':False})
        self.assertEqual(len(issues),1)
        self.assertIn('不一致', issues[0]['problem'])

    def test_inconsistent_audit_verdict(self):
        result=Audit(verdict='pass',issues=[{'severity':'blocker','path':'analysis','problem':'x','evidence_refs':[],'required_action':'remove'}])
        self.assertEqual(result.verdict,'needs_review')

    def test_report_escapes_untrusted_text_and_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            report=build_demo()
            report['meta']['client_alias']='<img src=x onerror=alert(1)>'
            report['evidence']['E00001']['text']='</script><script>alert(1)</script>'
            path=Path(tmp)/'report.html';render_report(report,path)
            text=path.read_text(encoding='utf-8')
            self.assertNotIn('<img src=x',text)
            self.assertIn('&lt;img src=x',text)
            self.assertNotIn('<script>alert(1)</script>',text)
            import re
            targets=set(re.findall(r'id="evidence-(E\d+)"',text))
            refs=set(re.findall(r'href="#evidence-(E\d+)"',text))
            self.assertTrue(refs.issubset(targets))
            self.assertNotIn('https://',text)


class ModelTests(unittest.TestCase):
    def test_reference_zero_padding_does_not_guess_ids(self):
        value=normalize_reference_format({'evidence_refs':['E000105','E2','F0001','E999999']})
        self.assertEqual(value['evidence_refs'],['E00105','E00002','F0001','E999999'])
        with self.assertRaises(ValueError): validate_refs(value, {'E00105','E00002'})

    def test_summary_is_concise(self):
        from visit_workflow.models import Summary
        with self.assertRaises(ValueError): Summary(text='长'*181,status='推断',evidence_refs=['E00001'])

    def test_json_fence_and_reject_trailing(self):
        self.assertEqual(parse_object('```json\n{"ok":true}\n```'), {'ok':True})
        with self.assertRaises(ValueError): parse_object('{"ok":true} trailing')

    def test_no_wrong_model_or_endpoint(self):
        with self.assertRaises(ValueError): Settings(base_url='https://evil.example/coding/v1')
        with self.assertRaises(ValueError): Settings(effort='none')
        key='sk-example-private'
        message=safe_api_error(401,json.dumps({'error':{'message':'key '+key}}),key)
        self.assertNotIn(key,message)

    def test_cache_and_schema_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            client=KimiClient(Settings(api_key='unit-test'))
            good=json.dumps({'text':'未提供预算','status':'待验证','evidence_refs':[]},ensure_ascii=False)
            with patch.object(client,'complete',side_effect=[('bad JSON',{}),(good,{})]) as complete:
                one=client.structured('test','plan',{'input':'x'},Claim,Path(tmp))
                two=client.structured('test','plan',{'input':'x'},Claim,Path(tmp))
                self.assertEqual(one,two)
                self.assertEqual(complete.call_count,2)

    def test_stream_payload_k3_and_truncation(self):
        import httpx
        original_client=httpx.Client
        captured=[]
        def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"{}"}}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"total_tokens":1}}\n\ndata: [DONE]\n\n')
        with patch('visit_workflow.llm.httpx.Client',side_effect=lambda **kw:original_client(transport=httpx.MockTransport(handler))):
            text,usage=KimiClient(Settings(api_key='unit-test')).complete([{'role':'user','content':'test'}])
        self.assertEqual(text,'{}')
        self.assertEqual(captured[0]['model'],'k3')
        self.assertEqual(captured[0]['reasoning_effort'],'high')


class PipelineTests(unittest.TestCase):
    def test_complete_pipeline_and_repair_branch(self):
        draft=demo_draft()
        class FixtureClient:
            usage=[]
            names=[]
            def check_cancel(self):pass
            def structured(self,name,prompt,data,schema,cache,validator=None):
                self.names.append(name)
                if prompt=='extract':
                    obj=Extraction(speakers=[],facts=[],excluded=[],unknowns=[],conflicts=[],possible_multiple_visits=False)
                elif prompt.startswith('analyze-'):obj=schema.model_validate({k:v for k,v in draft.analysis.model_dump().items() if k in schema.model_fields})
                elif prompt=='plan':obj=draft.plan
                elif prompt=='repair':obj=draft
                elif name=='audit-0':obj=Audit(verdict='needs_review',issues=[{'severity':'blocker','path':'analysis','problem':'fixture review','evidence_refs':[],'required_action':'correct'}])
                else:obj=Audit(verdict='pass',issues=[])
                if validator:validator(obj)
                return obj
        with tempfile.TemporaryDirectory() as tmp:
            folder=create_run((ROOT/'examples/示例接访.txt').read_bytes(),'sample.txt',Meta(),Path(tmp))
            client=FixtureClient()
            report=run_pipeline(folder,Settings(api_key='unit-test'),client=client)
            self.assertEqual(report['quality']['report_status'],'complete')
            self.assertIn('repair-0',client.names)
            self.assertTrue((folder/'report.html').exists())
            self.assertTrue((folder/'evidence.json').exists())


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=create_app(Path(self.tmp.name), Settings(api_key='sensitive-test-key'))
        self.client=TestClient(self.app)
        self.headers={'X-Visit-Workflow':'1'}

    def tearDown(self):
        self.app.state.manager.close();self.client.close();self.tmp.cleanup()

    def test_config_redaction_and_csrf(self):
        response=self.client.get('/api/config')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('sensitive-test-key',response.text)
        self.assertEqual(self.client.post('/api/config/key',json={'api_key':'new-test-key'}).status_code,403)
        response=self.client.post('/api/config/key',headers={**self.headers,'Origin':'https://evil.example'},json={'api_key':'new-test-key'})
        self.assertEqual(response.status_code,403)

    def test_lan_access_retains_cross_site_protection(self):
        self.assertEqual(self.client.get('/healthz', headers={'Host':'10.3.71.17:8767'}).status_code,403)
        app=create_app(Path(self.tmp.name)/'lan', Settings(api_key='unit-test'), allow_remote=True)
        with TestClient(app,base_url='http://10.3.71.17:8767') as client:
            self.assertEqual(client.get('/healthz').status_code,200)
            self.assertEqual(client.post('/api/config/key',headers={**self.headers,'Origin':'https://evil.example'},json={'api_key':'replacement-key'}).status_code,403)
            self.assertEqual(client.post('/api/config/key',headers={**self.headers,'Origin':'http://10.3.71.17:8767'},json={'api_key':'replacement-key'}).status_code,200)

    def test_upload_and_invalid_source_path(self):
        with patch.object(self.app.state.manager,'start'):
            response=self.client.post('/api/jobs',headers=self.headers,files={'file':('x.txt','客户：看四房'.encode(),'text/plain')})
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(self.client.get('/api/jobs').json()),1)
        response=self.client.post('/api/jobs/from-source',headers=self.headers,json={'filename':'../model.json','meta':{}})
        self.assertEqual(response.status_code,400)
        response=self.client.get('/reports/abcd/model.json')
        self.assertEqual(response.status_code,404)

    def test_project_subfolder_sources(self):
        source_root=Path(self.tmp.name)/'workspace'
        source=source_root/'录音文本'/'方松'/'客户_20261005.txt'
        source.parent.mkdir(parents=True)
        source.write_text('客户：看看四房。',encoding='utf-8')
        outside=source_root/'private.txt'
        outside.write_text('not a visit',encoding='utf-8')
        with patch('visit_workflow.server.ROOT',source_root), patch.object(self.app.state.manager,'start'):
            self.assertEqual(self.client.get('/api/sources').json()[0]['name'],'方松/客户_20261005.txt')
            response=self.client.post('/api/jobs/from-source',headers=self.headers,json={'filename':'方松/客户_20261005.txt','meta':{}})
            self.assertEqual(response.status_code,200)
            response=self.client.post('/api/jobs/from-source',headers=self.headers,json={'filename':'../private.txt','meta':{}})
            self.assertEqual(response.status_code,400)


if __name__=='__main__': unittest.main()
