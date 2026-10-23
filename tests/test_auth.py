import base64
import json
import stat
import sys
from urllib.parse import parse_qs

import httpx
import pytest

from test_client import SITE


def test_browser_session_exchange_validates_account_and_persists_only_api_token(tmp_path):
    from university_mcp.auth import exchange_session, save_auth
    token = "a" * 32
    payload = base64.b64encode(("site-id:::" + token + ":::unused-private-token").encode()).decode()

    def handle(request):
        assert request.url.host == "eng.sbcde.by"
        if request.url.path.endswith("launch.php"):
            assert request.headers["Cookie"] == "MoodleSession=fixture-session"
            assert request.url.params["confirmed"] == "1"
            return httpx.Response(200, text=f'<html><a id="launchapp" href="moodlemobile://token={payload}">Launch</a></html>')
        assert request.url.path.endswith("server.php")
        assert parse_qs(request.content.decode())["wstoken"] == [token]
        return httpx.Response(200, json={"userid": 9991, "fullname": "Student", "functions": []})

    auth, identity = exchange_session({"site": SITE, "cookie": "MoodleSession=fixture-session"},
                                      http=httpx.Client(transport=httpx.MockTransport(handle)))
    path = tmp_path / "private" / "auth.json"
    save_auth(auth, path)
    assert json.loads(path.read_text()) == {"site": SITE, "token": token}
    if sys.platform != "win32": assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert identity["userid"] == 9991
    assert "fixture-session" not in path.read_text() and "unused-private-token" not in path.read_text()


def test_browser_exchange_rejects_other_origin_before_using_cookie():
    from university_mcp.auth import exchange_session
    from university_mcp.client import AuthenticationError
    with pytest.raises(AuthenticationError):
        exchange_session({"site": "https://external.example", "cookie": "MoodleSession=fixture"})


def test_login_error_never_echoes_password_or_server_debug_data():
    from university_mcp.auth import password_login
    from university_mcp.client import AuthenticationError
    def handle(request):
        assert request.url.path == "/login/token.php" and request.method == "POST"
        return httpx.Response(200, json={"error": "secret-password", "errorcode": "invalidlogin", "stacktrace": "private"})
    with pytest.raises(AuthenticationError) as exc:
        password_login("student", "secret-password", http=httpx.Client(transport=httpx.MockTransport(handle)))
    assert "secret-password" not in str(exc.value) and "private" not in str(exc.value)
