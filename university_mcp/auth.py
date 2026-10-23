"""Explicit local authentication setup; passwords and browser cookies are never persisted."""
import argparse
import base64
import getpass
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import parse_qs

from bs4 import BeautifulSoup
import httpx

from .client import AuthenticationError, MoodleClient
from .models import SITE


def _identity(auth: dict, http: httpx.Client) -> dict:
    response = http.post(SITE + "/webservice/rest/server.php", data={
        "wstoken": auth["token"], "moodlewsrestformat": "json", "wsfunction": "core_webservice_get_site_info"})
    data = response.json()
    if response.status_code != 200 or "userid" not in data:
        raise AuthenticationError("The portal token could not be validated. Re-authenticate locally.")
    return {"userid": data["userid"], "name": data.get("fullname"), "function_count": len(data.get("functions", []))}


def exchange_session(session: dict, *, http: httpx.Client | None = None):
    if session.get("site") != SITE or not re.fullmatch(r"MoodleSession=[A-Za-z0-9,\-]{5,200}", session.get("cookie", "")):
        raise AuthenticationError("Only the current eng.sbcde.by Moodle session can be connected.")
    own_http = http is None
    http = http or httpx.Client(timeout=30, follow_redirects=False)
    try:
        response = http.get(SITE + "/admin/tool/mobile/launch.php", params={
            "service": "moodle_mobile_app", "passport": secrets.token_hex(16), "urlscheme": "moodlemobile", "confirmed": 1},
            headers={"Cookie": session["cookie"]})
        if response.status_code != 200:
            raise AuthenticationError("Browser session did not authorize mobile API login. Sign in to the portal again or use --password-login.")
        soup = BeautifulSoup(response.text, "html.parser")
        link = soup.select_one('a[href^="moodlemobile://token="]')
        if not link:
            raise AuthenticationError("Portal did not return a mobile API token. Sign in again or use --password-login.")
        fields = base64.b64decode(link["href"].split("token=", 1)[1]).decode().split(":::")
        if len(fields) < 2 or not re.fullmatch(r"[a-f0-9]{32}", fields[1]):
            raise AuthenticationError("Portal returned an unexpected mobile token format.")
        auth = {"site": SITE, "token": fields[1]}
        return auth, _identity(auth, http)
    except AuthenticationError:
        raise
    except Exception as exc:
        raise AuthenticationError(f"Portal authentication failed ({type(exc).__name__}). No credentials were logged.") from None
    finally:
        if own_http:
            http.close()


def password_login(username: str, password: str, *, http: httpx.Client | None = None):
    own_http = http is None
    http = http or httpx.Client(timeout=30, follow_redirects=False)
    try:
        response = http.post(SITE + "/login/token.php", data={
            "username": username, "password": password, "service": "moodle_mobile_app"})
        data = response.json()
        if response.status_code != 200 or not re.fullmatch(r"[a-f0-9]{32}", data.get("token", "")):
            raise AuthenticationError("Portal login failed. Check username/password, mobile-service permissions or MFA requirements.")
        auth = {"site": SITE, "token": data["token"]}
        return auth, _identity(auth, http)
    except AuthenticationError:
        raise
    except Exception as exc:
        raise AuthenticationError(f"Portal login failed ({type(exc).__name__}). No password was saved or logged.") from None
    finally:
        if own_http:
            http.close()


def save_auth(auth: dict, path: Path, *, account_id: int | None = None):
    from .private import save_private_json
    value = {"site": auth["site"], "token": auth["token"]}
    if account_id is not None:
        if type(account_id) is not int or account_id <= 0:
            raise AuthenticationError("The portal account identity is invalid.")
        value["account_id"] = account_id
    save_private_json(path, value)


def browser_setup(auth_path: Path):
    route = "/connect/" + secrets.token_urlsafe(24)
    outcome = {}

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path != route:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write((f'<html><title>University MCP local setup</title><h1>Connect portal session locally</h1>'
                             f'<form method="post" action="{route}"><label for="session">Portal session</label>'
                             '<input id="session" name="session" type="password" autocomplete="off">'
                             '<button>Connect</button></form></html>').encode())

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if self.path != route or self.headers.get("Origin") != origin:
                self.send_error(403)
                return
            size = int(self.headers.get("Content-Length", "0"))
            if not 1 <= size <= 12000:
                self.send_error(413)
                return
            try:
                session = json.loads(parse_qs(self.rfile.read(size).decode())["session"][0])
                auth, identity = exchange_session(session)
                save_auth(auth, auth_path, account_id=identity["userid"])
                outcome["identity"] = identity
                message = "Connected. You may close this page. Clear the clipboard before continuing."
                status = 200
            except (AuthenticationError, ValueError, KeyError) as exc:
                message = str(exc) if isinstance(exc, AuthenticationError) else "Invalid session payload."
                status = 400
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(message.encode())

    with HTTPServer(("127.0.0.1", 0), Receiver) as server:
        server.timeout = 1
        print("In the eng.sbcde.by Safari Web Inspector console, run:", flush=True)
        print("copy(JSON.stringify({site:location.origin,cookie:document.cookie.split(';').find(x=>x.trim().startsWith('MoodleSession='))?.trim()}))", flush=True)
        print(f"Then open http://127.0.0.1:{server.server_port}{route}, paste into the hidden field and Connect.", flush=True)
        print("After connecting, clear the clipboard in the portal console with: copy('')", flush=True)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline and not outcome:
            server.handle_request()
    if not outcome:
        raise AuthenticationError("Local browser setup timed out. No authentication changes were saved.")
    return outcome["identity"]


def main():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-file", default=os.environ.get("UNIVERSITY_MCP_AUTH_FILE", "auth.json"))
    parser.add_argument("--password-login", action="store_true", help="Prompt privately for portal credentials; save only the API token")
    args = parser.parse_args()
    try:
        if args.password_login:
            auth, identity = password_login(input("Portal username: "), getpass.getpass("Portal password (not saved): "))
            save_auth(auth, Path(args.auth_file), account_id=identity["userid"])
        else:
            identity = browser_setup(Path(args.auth_file))
        print(f"Connected user {identity['userid']} ({identity.get('name', '')}); API functions: {identity['function_count']}. Token saved privately; restart the MCP connection.")
    except AuthenticationError as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
