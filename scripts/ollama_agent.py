"""Run a local Ollama tool-calling model against Telegram MCP and project RAG."""
import argparse
import asyncio
import json
import sys
from contextlib import AsyncExitStack

import aiohttp
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

READ_ONLY = {'tg_list_accounts', 'tg_get_account_status', 'tg_get_dialogs',
             'tg_get_chat_history', 'tg_proxy_pool_status', 'project_knowledge_query'}


async def run(args):
    async with AsyncExitStack() as stack:
        tools = {}
        schema = []
        for command, parameters in (('./scripts/run-server', []),
                                    ('./venv/bin/python', ['scripts/rag_mcp.py'])):
            streams = await stack.enter_async_context(stdio_client(
                StdioServerParameters(command=command, args=parameters)
            ))
            session = await stack.enter_async_context(ClientSession(*streams))
            await session.initialize()
            for tool in (await session.list_tools()).tools:
                if tool.name in READ_ONLY or tool.name in args.allow_tool:
                    tools[tool.name] = session
                    schema.append({'type': 'function', 'function': {
                        'name': tool.name, 'description': tool.description or '',
                        'parameters': tool.inputSchema,
                    }})
        unknown = set(args.allow_tool) - tools.keys()
        if unknown:
            raise ValueError('Unknown allowed tool: ' + ', '.join(sorted(unknown)))
        messages = [{'role': 'system', 'content':
            'For project questions query project_knowledge_query first. '
            'Use Telegram tools for current account state. Report actual tool results. '
            'Never request a password for conversion or invent completion. After receiving the requested tool result, answer without repeating the call.'},
            {'role': 'user', 'content': args.prompt}]
        http = await stack.enter_async_context(aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=240)
        ))
        used = 0
        seen = set()
        for _ in range(8):
            async with http.post('http://127.0.0.1:11434/api/chat', json={
                'model': args.model, 'messages': messages, 'stream': False,
                'tools': schema, 'options': {'temperature': 0, 'num_predict': 512},
            }) as response:
                response.raise_for_status()
                message = (await response.json())['message']
            calls = message.get('tool_calls') or []
            if not calls:
                if args.require_tool and not used:
                    raise RuntimeError('Model did not call an MCP tool')
                print(message.get('content', ''))
                return
            messages.append(message)
            if used + len(calls) > 16:
                raise RuntimeError('Tool call budget exceeded')
            for call in calls:
                function = call['function']
                name = function['name']
                if name not in tools:
                    raise ValueError('Tool is not enabled')
                arguments = function.get('arguments') or {}
                call_key = name, json.dumps(arguments, sort_keys=True)
                if call_key in seen:
                    messages.append({'role': 'tool', 'tool_name': name,
                                     'content': 'Already executed. Use the previous result.'})
                    continue
                seen.add(call_key)
                result = await tools[name].call_tool(name, arguments)
                used += 1
                print(f'MCP {name}: {"error" if result.isError else "ok"}', file=sys.stderr)
                messages.append({'role': 'tool', 'tool_name': name,
                                 'content': result.model_dump_json()})
        raise RuntimeError('Model exceeded the tool round limit')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='qwen2.5:0.5b-instruct')
    parser.add_argument('--allow-tool', action='append', default=[])
    parser.add_argument('--require-tool', action='store_true')
    parser.add_argument('prompt')
    asyncio.run(run(parser.parse_args()))
