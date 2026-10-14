"""Read-only MCP connectivity check; no paid report generation."""
import argparse
import asyncio
import os
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def check(url, token):
    async with streamablehttp_client(url, headers={'Authorization':f'Bearer {token}'}) as (read, write, _):
        async with ClientSession(read,write) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            info = await session.call_tool('get_visit_service_info',{})
            if info.isError:
                raise RuntimeError('MCP connected but service configuration could not be read')
            print('Server:',initialized.serverInfo.name)
            print('Tools:',', '.join(t.name for t in tools.tools))
            data=info.structuredContent or {}
            print('Default model:',data.get('model'),'configured:',data.get('configured'))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://localhost:8768/mcp')
    args=parser.parse_args()
    token=os.environ.get('VISIT_API_TOKEN','')
    if not token: parser.error('Set VISIT_API_TOKEN first')
    asyncio.run(check(args.url,token))
