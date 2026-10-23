"""Short-lived loopback-only login UI. Passwords are used in memory and never logged."""
from http.server import BaseHTTPRequestHandler,HTTPServer
from importlib.resources import files
import json
from pathlib import Path
import secrets
import time
import webbrowser

from .auth import password_login,save_auth,_identity
import httpx
from .client import AuthenticationError
from .private import private_directory,save_private_json,load_private_json


def create_setup_server(data_dir,*,moodle_http=None,it_http=None):
    data=private_directory(data_dir)
    state={'moodle':{'connected':False},'it':{'connected':False}}
    if (data/'auth.json').exists():
        http=moodle_http or httpx.Client(timeout=15,follow_redirects=False)
        try:
            auth=load_private_json(data/'auth.json')
            from .models import SITE
            if auth.get('site')==SITE:
                state['moodle']={'connected':True,'user':_identity(auth,http)}
        except Exception:
            pass  # Existing expired auth remains untouched until a successful login.
        finally:
            if moodle_http is None:http.close()
    route='/setup/'+secrets.token_urlsafe(24)
    csrf=secrets.token_urlsafe(32)
    nonce=secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass

        def respond(self,status,body,html=False):
            content=body.encode('utf-8') if isinstance(body,str) else json.dumps(body,ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','text/html; charset=utf-8' if html else 'application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(content)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Frame-Options','DENY')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers();self.wfile.write(content)

        def valid_host(self):
            return self.headers.get('Host')==self.server.origin.removeprefix('http://')

        def do_GET(self):
            if not self.valid_host():return self.respond(403,{'error':'Local setup host rejected.'})
            if self.path==route:
                page=files('university_mcp').joinpath('setup_page.html').read_text(encoding='utf-8')
                return self.respond(200,page.replace('__NONCE__',nonce).replace('__ROUTE__',route).replace('__CSRF__',csrf),True)
            if self.path==route+'/status':return self.respond(200,state)
            return self.respond(404,{'error':'Setup link is unavailable.'})

        def do_POST(self):
            if (not self.valid_host() or self.headers.get('Origin')!=self.server.origin
                    or self.headers.get('X-Setup-CSRF')!=csrf):
                return self.respond(403,{'error':'Open the local setup link again.'})
            if self.path not in (route+'/moodle',route+'/it',route+'/finish'):
                return self.respond(404,{'error':'Unknown setup action.'})
            try:size=int(self.headers.get('Content-Length','0'))
            except ValueError:return self.respond(400,{'error':'Invalid request size.'})
            if not 1<=size<=12000:return self.respond(413,{'error':'Request is too large or empty.'})
            if self.headers.get_content_type()!='application/json' or self.headers.get('Transfer-Encoding'):
                return self.respond(400,{'error':'Use the local login form.'})
            try:
                payload=json.loads(self.rfile.read(size).decode('utf-8'))
                if not isinstance(payload,dict):raise ValueError()
                if self.path==route+'/finish':
                    if not state['moodle']['connected']:
                        return self.respond(400,{'error':'Connect the study portal first.'})
                    self.server.finished=True
                    return self.respond(200,{'finished':True,'message':'Restart the MCP connection in Codex.'})
                username=payload.get('username');password=payload.get('password')
                if (not isinstance(username,str) or not 1<=len(username.strip())<=250
                        or not isinstance(password,str) or not 1<=len(password)<=4096):
                    raise ValueError()
                if self.path==route+'/moodle':
                    auth,identity=password_login(username.strip(),password,http=moodle_http)
                    save_auth(auth,data/'auth.json',account_id=identity['userid'])
                    state['moodle']={'connected':True,'user':identity}
                    return self.respond(200,{'connected':True,'user':identity})
                from .it_auth import password_login_it
                auth,report=password_login_it(username.strip(),password,http=it_http)
                save_private_json(data/'it-auth.json',auth)
                state['it']={'connected':True,'user':report['student']}
                return self.respond(200,state['it'])
            except AuthenticationError as exc:
                return self.respond(400,{'error':str(exc)})
            except (ValueError,UnicodeError,KeyError):
                return self.respond(400,{'error':'Check the login fields and try again.'})
            except Exception:
                return self.respond(500,{'error':'The connection could not be saved. Check local permissions and retry.'})

    server=HTTPServer(('127.0.0.1',0),Handler)
    server.timeout=1
    server.origin=f'http://127.0.0.1:{server.server_port}'
    server.route,server.csrf,server.finished=route,csrf,False
    return server


def run_setup(data_dir,*,open_browser=True):
    with create_setup_server(data_dir) as server:
        url=server.origin+server.route
        print('3/3 Sign in privately in your browser:',flush=True)
        print(url,flush=True)
        print('Do not send your password to the agent. This link expires in 15 minutes.',flush=True)
        if open_browser:
            try:webbrowser.open(url)
            except webbrowser.Error:pass
        expires=time.monotonic()+900
        while time.monotonic()<expires and not server.finished:server.handle_request()
        if server.finished:
            print('Connected. Restart university_portal in Codex, then ask it to list your courses.',flush=True)
            return True
        print('Setup window expired. Any successful login was saved; rerun setup or doctor to continue.',flush=True)
        return False
