import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from visit_workflow.config import Settings
from visit_workflow.llm import KimiClient, WorkflowError, safe_api_error
from visit_workflow.server import create_app
from visit_workflow.usage import summarize


HEADERS = {'X-Visit-Workflow': '1'}


def deepseek_options():
    return {**Settings().options(), 'provider':'deepseek', 'model':'deepseek-flash',
            'base_url':'https://api.deepseek.com', 'max_tokens':32000}


class ProviderTests(unittest.TestCase):
    def test_switch_separates_keys_persists_profiles_and_snapshots_jobs(self):
        base = Settings(api_key='kimi-private-secret')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = create_app(root, base)
            with TestClient(app) as client:
                # Switching without a DeepSeek key never reuses the Kimi key.
                result = client.post('/api/config', headers=HEADERS, json=deepseek_options())
                self.assertEqual(result.status_code, 200)
                self.assertFalse(result.json()['configured'])
                response = client.post('/api/config/test', headers=HEADERS)
                self.assertEqual(response.status_code, 400)
                self.assertIn('DeepSeek', response.json()['detail'])
                options = {**deepseek_options(), 'api_key':'deepseek-private-secret'}
                self.assertEqual(client.post('/api/config', headers=HEADERS, json=options).status_code, 200)
                with patch.object(app.state.manager, 'start') as start:
                    response = client.post('/api/jobs', headers=HEADERS, files={'file':('visit.txt','客户：看四房'.encode())})
                    self.assertEqual(response.status_code, 200)
                    snapshot = start.call_args.args[1]
                    client.post('/api/config', headers=HEADERS, json=base.options())
                    self.assertEqual(snapshot.provider, 'deepseek')
                    self.assertEqual(snapshot.api_key, 'deepseek-private-secret')
                self.assertTrue(client.get('/api/config').json()['configured'])
                client.post('/api/config', headers=HEADERS, json=deepseek_options())
                self.assertTrue(client.get('/api/config').json()['configured'])
                saved = (root/'.model-settings.json').read_text('utf-8')
                public = client.get('/api/config').text
                for secret in ['kimi-private-secret', 'deepseek-private-secret']:
                    self.assertNotIn(secret, saved)
                    self.assertNotIn(secret, public)
                self.assertEqual(set(json.loads(saved)['profiles']), {'kimi','deepseek'})
            # A temporary DeepSeek key does not reappear after restart or inherit Kimi's.
            with TestClient(create_app(root, base)) as client:
                cfg = client.get('/api/config').json()
                self.assertEqual(cfg['provider'], 'deepseek')
                self.assertFalse(cfg['configured'])
                client.post('/api/config', headers=HEADERS, json=base.options())
                self.assertTrue(client.get('/api/config').json()['configured'])

    def test_unsaved_connection_uses_draft_key_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp, TestClient(create_app(Path(tmp), Settings(api_key='kimi-secret'))) as client:
            with patch('visit_workflow.server.KimiClient') as factory:
                factory.return_value.complete.return_value = ('{}', {})
                result = client.post('/api/config/test', headers=HEADERS,
                                     json={**deepseek_options(), 'api_key':'draft-deepseek-key'})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(factory.call_args.args[0].api_key, 'draft-deepseek-key')
                self.assertEqual(factory.call_args.args[0].provider, 'deepseek')
            cfg = client.get('/api/config').json()
            self.assertEqual(cfg['provider'], 'kimi')
            self.assertFalse(next(p for p in cfg['providers'] if p['id']=='deepseek')['configured'])
            self.assertFalse((Path(tmp)/'.model-settings.json').exists())

    def test_streaming_thinking_payload_and_usage(self):
        original = httpx.Client
        requests = []
        def handler(request):
            requests.append(request)
            chunks = [
                {'choices':[{'delta':{'reasoning_content':'not report content'},'finish_reason':None}]},
                {'choices':[{'delta':{'content':'{"ok":true}'},'finish_reason':'stop'}]},
                {'choices':[], 'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120,
                                       'prompt_cache_hit_tokens':75,'completion_tokens_details':{'reasoning_tokens':10}}},
            ]
            return httpx.Response(200, text=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n')
        with patch('visit_workflow.llm.httpx.Client', side_effect=lambda **kwargs:original(transport=httpx.MockTransport(handler))):
            for effort in ['low','high','max','none']:
                settings = Settings().select({**deepseek_options(),'effort':effort}, 'deepseek-test-key')
                model = KimiClient(settings)
                content, usage = model.complete([{'role':'user','content':'Return JSON'}])
                self.assertEqual(content, '{"ok":true}')
                request = requests[-1]
                self.assertEqual(str(request.url), 'https://api.deepseek.com/chat/completions')
                self.assertEqual(request.headers['Authorization'], 'Bearer deepseek-test-key')
                payload = json.loads(request.content)
                self.assertEqual(payload['model'], 'deepseek-flash')
                self.assertEqual(payload['thinking']['type'], 'disabled' if effort=='none' else 'enabled')
                self.assertEqual(payload.get('reasoning_effort'), None if effort=='none' else effort)
                self.assertEqual(model.usage[-1]['provider'], 'deepseek')
                totals = summarize(model.usage)
                self.assertEqual(totals['total_tokens'],120)
                self.assertEqual(totals['cached_input_tokens'],75)
                self.assertEqual(totals['reasoning_tokens'],10)

    def test_provider_url_model_and_limit_validation(self):
        for bad in [{'base_url':'https://api.kimi.com/coding/v1'}, {'base_url':'https://api.deepseek.com.evil.test'},
                    {'base_url':'http://127.0.0.1/v1'}, {'model':'k3'}, {'max_tokens':393217}, {'provider':'unknown'}]:
            with self.assertRaises(ValueError):
                Settings().select({**deepseek_options(), **bad})
        Settings().select({**deepseek_options(), 'max_tokens':393216, 'base_url':'https://api.deepseek.com/v1'})
        with self.assertRaises(ValueError):
            Settings(effort='none')
        error = safe_api_error(402, json.dumps({'error':{'message':'test-secret'}}), 'test-secret', 'DeepSeek')
        self.assertIn('DeepSeek', error)
        self.assertNotIn('test-secret', error)

    def test_local_multi_provider_loading_and_saved_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); runs = root/'runs'; runs.mkdir()
            (root/'model.json').write_text(json.dumps({'models':[
                {'id':'k3','apiKey':'kimi-local-secret'},
                {'provider':'deepseek','model':'deepseek-flash','api_key':'deepseek-local-secret'}]}))
            (runs/'.model-settings.json').write_text(json.dumps(deepseek_options()))
            with patch('visit_workflow.config.ROOT', root), patch('visit_workflow.config.RUNS', runs), patch.dict('os.environ', {}, clear=True):
                selected = Settings.load()
                self.assertEqual(selected.provider, 'deepseek')
                self.assertEqual(selected.api_key, 'deepseek-local-secret')
                self.assertEqual(selected.select(Settings().options()).api_key, 'kimi-local-secret')
                with patch.dict('os.environ', {'DEEPSEEK_API_KEY':'deepseek-env-secret'}):
                    self.assertEqual(Settings.load().api_key, 'deepseek-env-secret')


if __name__ == '__main__':
    unittest.main()
