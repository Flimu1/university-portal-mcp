"""Exercise actual local HTTP routes; university responses alone are synthetic."""
from contextlib import contextmanager
import json
from pathlib import Path
import stat
import sys
import threading
from urllib.parse import parse_qs
import httpx
import pytest


@contextmanager
def wizard(tmp_path, it_http=None):
    from university_mcp.setup_web import create_setup_server
    def portal(r):
        if r.url.path=='/login/token.php':
            args=parse_qs(r.content.decode())
            if args['password']==['bad']:return httpx.Response(200,json={'error':'bad SECRET','token':'invalid'})
            return httpx.Response(200,json={'token':('a' if args['username']==['one'] else 'b')*32})
        return httpx.Response(200,json={'userid':9991 if b'aaaaaaaa' in r.content else 9992,'fullname':'Example Student','functions':[]})
    server=create_setup_server(tmp_path,moodle_http=httpx.Client(transport=httpx.MockTransport(portal)),it_http=it_http)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=server.origin,trust_env=False) as client:yield server,client
    finally:server.shutdown();server.server_close();thread.join()


def post(server,client,kind='moodle',**changes):
    headers={'Origin':server.origin,'X-Setup-CSRF':server.csrf};headers.update(changes.pop('headers',{}))
    return client.post(server.route+'/'+kind,headers=headers,json={'username':'one','password':'very-private-password',**changes})


def test_login_page_and_real_post_persist_own_token_not_password(tmp_path):
    with wizard(tmp_path) as (s,c):
        page=c.get(s.route)
        assert page.status_code==200 and 'no-store' in page.headers['cache-control']
        assert page.headers['x-frame-options']=='DENY'
        assert 'unsafe-eval' not in page.headers['content-security-policy']
        result=post(s,c)
        assert result.status_code==200 and result.json()['connected']
        auth=json.loads((tmp_path/'auth.json').read_text())
        assert auth['token']=='a'*32 and auth['account_id']==9991
        contents=''.join(p.read_text() for p in tmp_path.rglob('*.json'))
        assert 'very-private-password' not in contents
        assert 'a'*32 not in result.text
        if sys.platform!='win32':assert stat.S_IMODE((tmp_path/'auth.json').stat().st_mode)==0o600
        status=c.get(s.route+'/status').json()
        assert status['moodle']['connected'] and status['moodle']['user']['userid']==9991


def test_cross_origin_host_csrf_and_unknown_routes_are_rejected(tmp_path):
    with wizard(tmp_path) as (s,c):
        for headers in [{'Origin':'https://evil.example'},{'X-Setup-CSRF':'wrong'},{'Host':'evil.example'}]:
            assert post(s,c,headers=headers).status_code==403
        assert c.get(s.route,headers={'Host':'evil.example'}).status_code==403
        assert c.get('/guess').status_code==404
        assert not (tmp_path/'auth.json').exists()


def test_bad_login_is_retryable_and_response_contains_no_password(tmp_path):
    with wizard(tmp_path) as (s,c):
        bad=post(s,c,password='bad')
        assert bad.status_code==400 and 'SECRET' not in bad.text
        assert not (tmp_path/'auth.json').exists()
        assert post(s,c).json()['connected']


def test_malformed_or_oversized_payload_does_not_touch_auth(tmp_path):
    with wizard(tmp_path) as (s,c):
        headers={'Origin':s.origin,'X-Setup-CSRF':s.csrf,'Content-Type':'application/json'}
        for body in ['not-json','[]','{}','x'*20000]:
            assert c.post(s.route+'/moodle',headers=headers,content=body).status_code in (400,413)
        assert not (tmp_path/'auth.json').exists()


def test_refreshing_account_separates_moodle_cache(tmp_path,monkeypatch):
    from university_mcp.server import _load_services
    monkeypatch.setenv('UNIVERSITY_MCP_AUTH_FILE',str(tmp_path/'auth.json'))
    monkeypatch.setenv('UNIVERSITY_MCP_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('UNIVERSITY_MCP_DOWNLOAD_DIR',str(tmp_path/'materials'))
    with wizard(tmp_path) as (s,c):
        post(s,c);one,first_files=_load_services()
        post(s,c,username='two');two,second_files=_load_services()
        assert one.data_dir!=two.data_dir
        assert first_files.download_dir!=second_files.download_dir
        one.http.close();two.http.close()


def test_finish_requires_moodle_login(tmp_path):
    with wizard(tmp_path) as (s,c):
        assert post(s,c,kind='finish').status_code==400
        post(s,c)
        assert post(s,c,kind='finish').status_code==200 and s.finished

def test_saved_moodle_token_is_verified_when_reopening_for_it_refresh(tmp_path):
    with wizard(tmp_path) as (s,c):post(s,c)
    with wizard(tmp_path) as (s,c):
        status=c.get(s.route+'/status').json()
        assert status['moodle']['connected'] and status['moodle']['user']['userid']==9991
        assert post(s,c,kind='finish').status_code==200


def test_optional_it_login_uses_own_credentials_and_private_file(tmp_path):
    from test_it_auth import login_transport
    transport,_=login_transport()
    with wizard(tmp_path,it_http=transport) as (s,c):
        result=post(s,c,kind='it',username='student',password='private-password')
        assert result.status_code==200 and result.json()['connected']
        saved=json.loads((tmp_path/'it-auth.json').read_text())
        assert saved['student_id']=='9991' and saved['cookie']=='ASPSESSIONIDTEST=FIXTURESESSION'
        assert 'FIXTURESESSION' not in result.text and 'private-password' not in result.text

def test_stalled_connection_does_not_block_another_browser_request(tmp_path):
    import socket
    with wizard(tmp_path) as (s,c):
        stalled=socket.create_connection(('127.0.0.1',s.server_port))
        try:
            stalled.sendall(b'G')
            response=c.get(s.route,timeout=1)
            assert response.status_code==200
        finally:stalled.close()

def test_second_account_cannot_reuse_first_accounts_download(tmp_path,monkeypatch):
    from university_mcp.server import _load_services
    from university_mcp.client import PortalError
    from test_files import material
    monkeypatch.setenv('UNIVERSITY_MCP_AUTH_FILE',str(tmp_path/'auth.json'))
    monkeypatch.setenv('UNIVERSITY_MCP_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('UNIVERSITY_MCP_DOWNLOAD_DIR',str(tmp_path/'materials'))
    with wizard(tmp_path) as (s,c):
        post(s,c);one,first=_load_services()
        one.http=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b'Private account one',headers={'content-type':'text/plain'})))
        first.download(material('notes.txt'))
        post(s,c,username='two');two,second=_load_services()
        two.http=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(403)))
        with pytest.raises(PortalError):second.download(material('notes.txt'))
        one.http.close();two.http.close()
