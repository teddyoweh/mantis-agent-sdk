"""Parent-supplied HTTP MCP: discovery/call over real sockets, no model account."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mantis_agent.headless import _environment_mcp_servers, _dump
from mantis_agent.query import query
from mantis_agent.types import AssistantMessage, TextBlock, UserMessage


@pytest.mark.parametrize('raw', ['secret', '[]', '{"x":{"type":"stdio","command":"secret"}}',
                                  '{"x":{"type":"http","url":"http://localhost","headers":[]}}'])
def test_environment_rejects_without_echoing(monkeypatch, raw):
    monkeypatch.setenv('MANTIS_MCP_SERVERS', raw)
    with pytest.raises(ValueError, match='MANTIS_MCP_SERVERS') as exc:
        _environment_mcp_servers()
    assert 'secret' not in str(exc.value)


@pytest.mark.parametrize('early_close', [False, True])
def test_http_discovery_call_and_shutdown(monkeypatch, early_close):
    calls = []
    token = 'Bearer private-test-token'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.headers.get('Authorization') == token
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(body['method'])
            method = body['method']
            if method == 'initialize':
                result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'test', 'version': '1'}}
            elif method == 'tools/list':
                result = {'tools': [{'name': 'hello', 'description': 'Say hello',
                                    'inputSchema': {'type': 'object', 'properties': {}}}]}
            elif method == 'tools/call':
                assert body['params']['name'] == 'hello'
                result = {'content': [{'type': 'text', 'text': 'hello from Universe'}]}
            elif 'id' not in body:
                self.send_response(202)
                self.end_headers()
                return
            else:
                result = {}
            data = json.dumps({'jsonrpc': '2.0', 'id': body['id'], 'result': result}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {'universe': {'type': 'http', 'url': f'http://127.0.0.1:{server.server_port}/mcp',
                           'headers': {'Authorization': token}}}
    monkeypatch.setenv('MANTIS_MCP_SERVERS', json.dumps(config))
    from mantis_agent.agent import Agent
    from mantis_agent.mcp.manager import MCPManager
    stopped = []
    original_stop = MCPManager.stop

    async def stop(self, **kwargs):
        await original_stop(self, **kwargs)
        stopped.append(self)

    async def run_iter(self, messages):
        assert messages[0].content == 'previous turn'
        remote = self.tools.get('mcp__universe__hello')
        assert remote is not None
        result = await remote.fn()
        assert 'hello from Universe' in str(result)
        yield AssistantMessage(content=[TextBlock(text='done')], stop_reason='end_turn')

    monkeypatch.setattr(MCPManager, 'stop', stop)
    monkeypatch.setattr(Agent, 'run_iter', run_iter)

    async def run():
        stream = query(prompt='new turn', options={
            'model': 'gpt-5.4', 'backend': 'http://127.0.0.1:1',
            'session_id': 'resume-test', 'messages': [UserMessage(content='previous turn')],
            'mcp_servers': list(_environment_mcp_servers().items()),
        })
        init = await stream.__anext__()
        assert init.mcp_servers == [{'name': 'universe', 'status': 'connected'}]
        assert 'mcp__universe__hello' in init.tools
        assert token not in _dump(init) and 'Authorization' not in _dump(init)
        assert config['universe']['url'] not in _dump(init)
        if early_close:
            await stream.aclose()
        else:
            frames = [frame async for frame in stream]
            assert frames[-1].result == 'done', frames[-1].errors
            assert all(frame.session_id == 'resume-test' for frame in frames)
            assert 'previous turn' not in ''.join(_dump(frame) for frame in frames)
        assert len(stopped) == 1
        assert not stopped[0].clients

    try:
        asyncio.run(run())
        assert 'tools/list' in calls
        assert ('tools/call' in calls) is (not early_close)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
