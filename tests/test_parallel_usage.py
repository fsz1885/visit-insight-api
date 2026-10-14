import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import httpx

from visit_workflow.config import Settings
from visit_workflow.llm import KimiClient, WorkflowError, Cancelled
from visit_workflow.models import Claim
from visit_workflow.pipeline import parallel_nodes
from visit_workflow.usage import UsageLedger, read_records, summarize


class ParallelTests(unittest.TestCase):
    def test_parallel_overlap_limit_and_order(self):
        barrier=threading.Barrier(3)
        lock=threading.Lock()
        active=peak=0
        def task(i):
            nonlocal active,peak
            with lock:
                active+=1
                peak=max(peak,active)
            barrier.wait(timeout=3)
            time.sleep((3-i%3)*.01)
            with lock: active-=1
            return i
        result=parallel_nodes([lambda i=i:task(i) for i in range(6)],3,threading.Event())
        self.assertEqual(result,list(range(6)))
        self.assertEqual(peak,3)

    def test_failure_stops_pending_work(self):
        cancel=threading.Event()
        def failure(): raise ValueError('bad branch')
        def follower():
            if not cancel.wait(2): raise AssertionError('not cancelled')
            return 1
        with self.assertRaisesRegex(ValueError,'bad branch'):
            parallel_nodes([failure,follower,follower],2,cancel)
        self.assertTrue(cancel.is_set())


class UsageTests(unittest.TestCase):
    def test_concurrent_durable_ledger_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'usage.json'
            ledger=UsageLedger(path)
            def record(i):
                call=ledger.start('node-'+str(i%3),1,1)
                ledger.finish(call,'success',{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15,
                    'cached_tokens':4,'prompt_tokens_details':{'cached_tokens':4},'completion_tokens_details':{'reasoning_tokens':2}},.1)
            with ThreadPoolExecutor(max_workers=3) as pool: list(pool.map(record,range(15)))
            self.assertEqual(summarize(read_records(path))['total_tokens'],225)
            ledger.cache_hit('node-0')
            pending=ledger.start('unfinished',1,1)
            resumed=UsageLedger(path)
            stats=resumed.summary()
            self.assertEqual(stats['cached_input_tokens'],60)
            self.assertEqual(stats['reasoning_tokens'],30)
            self.assertEqual(stats['total_tokens'],225)
            self.assertEqual(stats['unknown_usage_requests'],1)
            self.assertEqual(stats['cache_hits'],1)
            self.assertEqual(next(r for r in resumed.records if r.get('call_id')==pending)['status'],'interrupted')

    def test_legacy_and_missing_usage_not_invented(self):
        summary=summarize([{'node':'old','usage':{'prompt_tokens':3,'completion_tokens':4}}, {'node':'empty','usage':{}}])
        self.assertEqual(summary['total_tokens'],7)
        self.assertEqual(summary['unknown_usage_requests'],1)
        self.assertEqual(summary['legacy_requests'],2)
        self.assertTrue(summary['partial'])

    def test_schema_retry_transport_retry_and_cache_count_once(self):
        original=httpx.Client
        calls=[]
        good=json.dumps({'text':'未知','status':'待验证','evidence_refs':[]},ensure_ascii=False)
        def handler(request):
            calls.append(request)
            if len(calls)==1:return httpx.Response(429,headers={'retry-after':'0'},text='{}')
            content='invalid JSON' if len(calls)==2 else good
            packet={'choices':[{'delta':{'content':content},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}
            return httpx.Response(200,text='data: '+json.dumps(packet)+'\n\ndata: [DONE]\n\n')
        with tempfile.TemporaryDirectory() as tmp, patch('visit_workflow.llm.httpx.Client',side_effect=lambda **kw:original(transport=httpx.MockTransport(handler))):
            path=Path(tmp)
            client=KimiClient(Settings(api_key='test'),usage_path=path/'usage.json')
            client.structured('n','plan',{},Claim,path/'nodes')
            client.structured('n','plan',{},Claim,path/'nodes')
            stats=client.ledger.summary()
            self.assertEqual(stats['requests'],3)
            self.assertEqual(stats['total_tokens'],30)
            self.assertEqual(stats['unknown_usage_requests'],1)
            self.assertEqual(stats['cache_hits'],1)
            self.assertEqual(stats['failed_requests'],1)
            self.assertEqual([r['attempt'] for r in client.usage if r['status']!='cache_hit'],[1,1,2])

    def test_truncated_output_still_records_provider_usage(self):
        original=httpx.Client
        def handler(request):
            return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}],"usage":{"total_tokens":22}}\n\ndata: [DONE]\n\n')
        with patch('visit_workflow.llm.httpx.Client',side_effect=lambda **kw:original(transport=httpx.MockTransport(handler))):
            client=KimiClient(Settings(api_key='test'))
            with self.assertRaises(WorkflowError):client.complete([])
        self.assertEqual(client.ledger.summary()['total_tokens'],22)
        self.assertEqual(client.usage[0]['status'],'failed')

    def test_cancel_before_call_does_not_count_request(self):
        cancel=threading.Event();cancel.set()
        client=KimiClient(Settings(api_key='test'),cancel)
        with self.assertRaises(Cancelled):client.complete([])
        self.assertEqual(client.ledger.summary()['requests'],0)


if __name__=='__main__':unittest.main()
