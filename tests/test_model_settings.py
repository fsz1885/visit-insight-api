import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient
from visit_workflow.config import Settings
from visit_workflow.server import create_app
from visit_workflow.llm import KimiClient
from visit_workflow.models import Claim


class ModelSettingsTests(unittest.TestCase):
    def test_saved_options_persist_without_key_and_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); base=Settings(api_key='private-test-secret')
            app=create_app(root,base)
            headers={'X-Visit-Workflow':'1'}
            options={**base.options(),'model':'kimi-for-coding','effort':'low','max_tokens':12000,'timeout':120,'max_concurrency':2}
            with TestClient(app) as client:
                self.assertEqual(client.post('/api/config',json=options).status_code,403)
                self.assertEqual(client.post('/api/config',headers=headers,json=options).status_code,200)
                self.assertNotIn(base.api_key,(root/'.model-settings.json').read_text())
                self.assertNotIn(base.api_key,client.get('/api/config').text)
                for bad in [{'model':'made-up'},{'effort':'none'},{'timeout':0},{'max_tokens':1},{'max_concurrency':9},{'base_url':'https://evil.example/v1'}]:
                    response=client.post('/api/config',headers=headers,json={**options,**bad})
                    self.assertIn(response.status_code,[400,422])
                with patch.object(app.state.manager,'start') as start:
                    response=client.post('/api/jobs',headers=headers,files={'file':('a.txt','客户：看四房'.encode())})
                    self.assertEqual(response.status_code,200)
                    snapshot=start.call_args.args[1]
                    client.post('/api/config',headers=headers,json=base.options())
                    self.assertEqual(snapshot.model,'kimi-for-coding')
                client.post('/api/config',headers=headers,json=options)
            with TestClient(create_app(root,base)) as client:
                self.assertEqual(client.get('/api/config').json()['model'],'kimi-for-coding')

    def test_draft_connection_test_does_not_save(self):
        with tempfile.TemporaryDirectory() as tmp, TestClient(create_app(Path(tmp),Settings(api_key='test'))) as client:
            options={**Settings().options(),'model':'k3-256k','effort':'max'}
            with patch('visit_workflow.server.KimiClient') as factory:
                factory.return_value.complete.return_value=('{}',{'total_tokens':2})
                result=client.post('/api/config/test',headers={'X-Visit-Workflow':'1'},json=options)
                self.assertEqual(result.json()['model'],'k3-256k')
                self.assertEqual(factory.call_args.args[0].effort,'max')
            self.assertEqual(client.get('/api/config').json()['model'],'k3')
            self.assertFalse((Path(tmp)/'.model-settings.json').exists())

    def test_model_parameter_payload_ledger_and_cache(self):
        original=httpx.Client; payloads=[]
        good=json.dumps({'text':'未知','status':'待验证','evidence_refs':[]})
        def handler(request):
            payloads.append(json.loads(request.content))
            packet={'choices':[{'delta':{'content':good},'finish_reason':'stop'}],'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
            return httpx.Response(200,text='data: '+json.dumps(packet)+'\n\ndata: [DONE]\n\n')
        with tempfile.TemporaryDirectory() as tmp, patch('visit_workflow.llm.httpx.Client',side_effect=lambda **kwargs:original(transport=httpx.MockTransport(handler))):
            path=Path(tmp)
            for model,effort,tokens in [('k3','high',22000),('k3-256k','low',4096),('kimi-for-coding','max',8192)]:
                client=KimiClient(Settings(api_key='test',model=model,effort=effort,max_tokens=tokens),usage_path=path/'usage.json')
                client.structured('same','plan',{},Claim,path/'nodes')
                client.structured('same','plan',{},Claim,path/'nodes')
                self.assertEqual(payloads[-1]['model'],model)
                self.assertEqual(payloads[-1]['reasoning_effort'],effort)
                self.assertEqual(payloads[-1]['max_tokens'],tokens)
                self.assertEqual(client.usage[-1]['model'],model)
            self.assertEqual(len(payloads),3)


if __name__=='__main__':unittest.main()
