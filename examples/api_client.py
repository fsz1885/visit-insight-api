"""Submit a transcript, poll the job, and save its HTML report."""
import argparse
import os
import time
from pathlib import Path
import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file', type=Path)
    parser.add_argument('--url', default='http://localhost:8767')
    parser.add_argument('--model', choices=['deepseek-flash','deepseek-v4-pro','k3'], default='deepseek-v4-pro')
    parser.add_argument('--output', type=Path, default=Path('report.html'))
    parser.add_argument('--wait-seconds', type=int, default=7200)
    args = parser.parse_args()
    token = os.environ.get('VISIT_API_TOKEN')
    if not token:
        parser.error('Set VISIT_API_TOKEN first')
    with httpx.Client(base_url=args.url.rstrip('/'), headers={'Authorization':f'Bearer {token}'}, timeout=30) as client:
        r = client.post('/api/v1/jobs', json={
            'filename':args.file.name, 'text':args.file.read_text(encoding='utf-8-sig'),
            'options':{'provider':'kimi' if args.model=='k3' else 'deepseek', 'model':args.model, 'max_concurrency':8}})
        r.raise_for_status()
        job=r.json()
        print('Job:',job['id'],flush=True)
        deadline=time.monotonic()+args.wait_seconds
        previous=None
        while True:
            r=client.get(job['links']['status']); r.raise_for_status(); job=r.json()
            state=(job['status'],job['progress'])
            if state!=previous:
                print(*state,flush=True); previous=state
            if job['status'] in {'complete','needs_review'}:
                report=client.get(job['links']['report_html']); report.raise_for_status()
                args.output.write_bytes(report.content)
                print('Saved:',args.output.resolve())
                if job['status']=='needs_review': print('Report requires review before use.')
                return
            if job['status'] in {'failed','cancelled','interrupted'}:
                raise SystemExit('Task stopped: '+job.get('message',job['status']))
            if time.monotonic()>=deadline:
                raise SystemExit('Polling timed out; task continues. Query job '+job['id']+' before submitting again.')
            time.sleep(4)


if __name__=='__main__':
    main()
