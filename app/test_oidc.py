import base64
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

spec = importlib.util.spec_from_file_location("room_site", Path(__file__).with_name("room.py"))
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)
oidc = site.oidc

ISSUER = "https://sso.example/realms/test"


def jwt(claims):
    encode = lambda data: base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


class FakeProvider:
    """Answers discovery and token requests like Keycloak would."""

    def __init__(self):
        self.roles = {"realm_access": {"roles": ["offline_access"]}}
        self.nonce = None
        self.requests = []
        self.refresh_fails = False

    def __call__(self, url, form=None):
        self.requests.append((url, form))
        if url.endswith("/.well-known/openid-configuration"):
            return {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/auth",
                    "token_endpoint": f"{ISSUER}/token", "end_session_endpoint": f"{ISSUER}/logout"}
        if form["grant_type"] == "refresh_token" and self.refresh_fails:
            raise oidc.AuthError("Token is not active")
        now = int(time.time())
        return {"access_token": jwt({"sub": "u1", "exp": now + 300, **self.roles}), "expires_in": 300,
                "refresh_token": "refresh", "id_token": jwt({"iss": ISSUER, "aud": "room", "sub": "u1", "exp": now + 300,
                                                             "nonce": self.nonce, "preferred_username": "vincent"})}


class OIDCTests(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider()
        self.auth = oidc.OIDC({"issuer": ISSUER, "client_id": "room", "client_secret": "s3cret"}, fetch=self.provider)

    def login(self):
        url, state = self.auth.begin("http://room.example:8085")
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["redirect_uri"], ["http://room.example:8085/auth/callback"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.provider.nonce = query["nonce"][0]
        return self.auth.finish(state, state, "code")

    def test_admin_is_decided_by_keycloak_roles(self):
        self.assertFalse(self.auth.user(self.login())["admin"])
        self.provider.roles = {"resource_access": {"realm-management": {"roles": ["realm-admin"]}}}
        self.assertTrue(self.auth.user(self.login())["admin"])
        self.provider.roles = {"realm_access": {"roles": ["admin"]}}
        user = self.auth.user(self.login())
        self.assertEqual((user["name"], user["admin"]), ("vincent", True))
        token_form = self.provider.requests[-1][1]
        self.assertEqual((token_form["client_secret"], token_form["grant_type"]), ("s3cret", "authorization_code"))

    def test_rejects_foreign_state_nonce_and_audience(self):
        url, state = self.auth.begin("http://room.example")
        self.provider.nonce = parse_qs(urlparse(url).query)["nonce"][0]
        with self.assertRaises(oidc.AuthError):
            self.auth.finish(state, "other-browser", "code")
        url, state = self.auth.begin("http://room.example")
        self.provider.nonce = "replayed"
        with self.assertRaises(oidc.AuthError):
            self.auth.finish(state, state, "code")
        with self.assertRaises(oidc.AuthError):
            self.auth._check_id_token(jwt({"iss": ISSUER, "aud": "other", "exp": time.time() + 60}), None)
        self.assertEqual(self.auth.sessions, {})

    def test_expired_session_refreshes_or_ends(self):
        session = self.login()
        self.auth.sessions[session]["expires"] = 0
        self.provider.roles = {"realm_access": {"roles": ["admin"]}}
        self.assertTrue(self.auth.user(session)["admin"])
        self.assertEqual(self.provider.requests[-1][1]["grant_type"], "refresh_token")
        self.auth.sessions[session]["expires"] = 0
        self.provider.refresh_fails = True
        self.assertIsNone(self.auth.user(session))
        self.assertNotIn(session, self.auth.sessions)

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            oidc.OIDC({"issuer": "http://sso.example", "client_id": "room"})
        with self.assertRaises(ValueError):
            oidc.OIDC({"issuer": ISSUER})


class LoginEndpointTests(unittest.TestCase):
    def setUp(self):
        self.provider = FakeProvider()
        self.context = {
            "oidc": oidc.OIDC({"issuer": ISSUER, "client_id": "room"}, fetch=self.provider),
            "graph": {"seed": "S1", "players": []}, "checks": {}, "downloads": [], "lan": "192.0.2.1",
            "tracker": SimpleNamespace(state={}, feed=[]),
            "server": SimpleNamespace(running=True, port=38281, log=["secret log"], command=lambda text: None),
        }
        self.server = site.WebServer(("127.0.0.1", 0), site.make_handler(self.context))
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.host = f"room.example:{self.server.server_port}"
        remote = patch.object(self.server.RequestHandlerClass, "_local", return_value=False)
        remote.start()
        self.addCleanup(remote.stop)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, cookie=None, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        headers = {"Host": self.host, **(headers or {})}
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def login(self):
        response, _ = self.request("GET", "/auth/login")
        self.assertEqual(response.status, 303)
        query = parse_qs(urlparse(response.getheader("Location")).query)
        self.assertEqual(query["redirect_uri"], [f"http://{self.host}/auth/callback"])
        login_cookie = response.getheader("Set-Cookie").split(";")[0]
        self.assertIn("HttpOnly", response.getheader("Set-Cookie"))
        self.provider.nonce = query["nonce"][0]
        state = query["state"][0]
        response, _ = self.request("GET", f"/auth/callback?state={state}&code=abc", cookie=login_cookie)
        self.assertEqual((response.status, response.getheader("Location")), (303, "/"))
        return next(cookie.split(";")[0] for cookie in response.headers.get_all("Set-Cookie")
                    if cookie.startswith(oidc.SESSION_COOKIE))

    def status(self, cookie=None):
        return json.loads(self.request("GET", "/api/status", cookie)[1])

    def test_remote_visitor_sees_login_until_signed_in(self):
        status = self.status()
        self.assertEqual((status["admin"], status["auth"], status["log"]), (False, {"login": True, "user": None}, []))
        user = self.login()
        status = self.status(user)
        self.assertEqual((status["admin"], status["auth"]), (False, {"login": False, "user": "vincent"}))
        self.assertEqual(self.request("GET", "/api/config", user)[0].status, 403)

    def test_keycloak_admin_gets_host_controls(self):
        self.provider.roles = {"resource_access": {"realm-management": {"roles": ["realm-admin"]}}}
        admin = self.login()
        status = self.status(admin)
        self.assertEqual((status["admin"], status["log"]), (True, ["secret log"]))
        body = json.dumps({"command": "/status"})
        json_type = {"Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/api/command", admin, body, json_type)[0].status, 200)
        self.assertEqual(self.request("POST", "/api/command", admin, body,
                                      {**json_type, "Origin": "http://evil.example"})[0].status, 403)
        self.assertEqual(self.request("POST", "/api/command", admin, body, {"Content-Type": "text/plain"})[0].status, 403)
        self.assertEqual(self.request("POST", "/api/command", None, body, json_type)[0].status, 403)
        response, _ = self.request("POST", "/auth/logout", admin, headers={"Origin": f"http://{self.host}"})
        self.assertEqual(response.status, 303)
        self.assertTrue(response.getheader("Location").startswith(f"{ISSUER}/logout?"))
        self.assertFalse(self.status(admin)["admin"])

    def test_callback_without_matching_browser_cookie_fails(self):
        response, _ = self.request("GET", "/auth/login")
        state = parse_qs(urlparse(response.getheader("Location")).query)["state"][0]
        response, body = self.request("GET", f"/auth/callback?state={state}&code=abc")
        self.assertEqual(response.status, 400)
        self.assertIn(b"Login failed", body)

    def test_without_config_there_is_no_login(self):
        self.context["oidc"] = None
        status = self.status()
        self.assertEqual((status["admin"], status["auth"]), (False, {"login": False, "user": None}))
        self.assertEqual(self.request("GET", "/auth/login")[0].status, 404)
        with patch.object(self.server.RequestHandlerClass, "_local", return_value=True):
            self.assertTrue(self.status()["admin"])

    def test_missing_config_file_disables_login(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(oidc.load(Path(root), environ={}))

    def test_environment_config_and_logout_without_redirect(self):
        with tempfile.TemporaryDirectory() as root:
            auth = oidc.load(Path(root), environ={"OAUTH_URL": ISSUER, "OAUTH_CLIENT_ID": "room",
                                                  "OAUTH_CLIENT_SECRET": "s3cret",
                                                  "OIDC_PUBLIC_URL": "https://room.example/",
                                                  "OIDC_POST_LOGOUT_REDIRECT": "0"})
            (Path(root) / oidc.CONFIG_FILE).write_text(json.dumps({"issuer": ISSUER, "client_id": "file"}))
            self.assertEqual(oidc.load(Path(root), environ={"OAUTH_URL": ISSUER}).client_id, "file")
        self.assertEqual((auth.issuer, auth.client_id, auth.client_secret), (ISSUER, "room", "s3cret"))
        self.assertEqual(auth.base_url("internal:8085", "http"), "https://room.example")
        auth._fetch = self.provider
        query = parse_qs(urlparse(auth.logout(None, auth.base_url("x", "http"))).query)
        self.assertEqual(query, {"client_id": ["room"]})
        self.context["oidc"].post_logout_redirect = True
        query = parse_qs(urlparse(self.context["oidc"].logout(None, "https://room.example")).query)
        self.assertEqual(query["post_logout_redirect_uri"], ["https://room.example/"])

    def test_localhost_needs_no_login(self):
        with patch.object(self.server.RequestHandlerClass, "_local", return_value=True):
            status = self.status()
        self.assertEqual((status["admin"], status["auth"]["login"]), (True, False))


if __name__ == "__main__":
    unittest.main()
