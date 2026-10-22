"""Optional OpenID Connect login (e.g. Keycloak) that grants remote users the host's admin controls.

Configured by oidc.json beside start.sh, or else by the OAUTH_URL, OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET
environment variables (plus optional OIDC_PUBLIC_URL and OIDC_POST_LOGOUT_REDIRECT=0). Without either,
only localhost is admin, as before.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CONFIG_FILE = "oidc.json"
SESSION_COOKIE = "mw_session"
LOGIN_COOKIE = "mw_login"
LOGIN_TIMEOUT = 600
SESSION_LIMIT = 7 * 24 * 3600  # refresh tokens normally expire much earlier
DEFAULT_ADMIN_CLIENT_ROLES = {"realm-management": ["realm-admin"]}


class AuthError(Exception):
    pass


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt_claims(token: str) -> dict:
    """Decodes a JWT payload. Only use for tokens received directly from the token endpoint over TLS."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (AttributeError, IndexError, ValueError) as error:
        raise AuthError("The identity provider returned a malformed token") from error
    if not isinstance(claims, dict):
        raise AuthError("The identity provider returned a malformed token")
    return claims


def load(root: Path, environ=None):
    environ = os.environ if environ is None else environ
    path = Path(root) / CONFIG_FILE
    if not path.is_file():
        if not environ.get("OAUTH_URL"):
            return None
        config = {"issuer": environ["OAUTH_URL"], "client_id": environ.get("OAUTH_CLIENT_ID"),
                  "client_secret": environ.get("OAUTH_CLIENT_SECRET") or None,
                  "public_url": environ.get("OIDC_PUBLIC_URL") or None}
        if environ.get("OIDC_POST_LOGOUT_REDIRECT"):
            config["post_logout_redirect"] = environ["OIDC_POST_LOGOUT_REDIRECT"].lower() not in ("0", "false", "no")
        return OIDC(config, source="OAUTH_* environment variables")
    try:
        config = json.loads(path.read_text())
    except ValueError as error:
        raise ValueError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(config, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return OIDC(config, source=path)


class OIDC:
    def __init__(self, config: dict, source="OIDC config", fetch=None):
        def text(key, required=True):
            value = config.get(key)
            if value is None and not required:
                return None
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{source}: \"{key}\" must be a non-empty string")
            return value.strip()

        self.issuer = text("issuer").rstrip("/")
        if not self.issuer.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise ValueError(f"{source}: \"issuer\" must use https")
        self.client_id = text("client_id")
        self.client_secret = text("client_secret", required=False)
        self.public_url = (text("public_url", required=False) or "").rstrip("/") or None
        roles = config.get("admin_roles", ["admin"])
        client_roles = config.get("admin_client_roles", DEFAULT_ADMIN_CLIENT_ROLES)
        if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
            raise ValueError(f"{source}: \"admin_roles\" must be a list of realm role names")
        if not isinstance(client_roles, dict) or not all(
                isinstance(names, list) and all(isinstance(role, str) for role in names)
                for names in client_roles.values()):
            raise ValueError(f"{source}: \"admin_client_roles\" must map client IDs to lists of role names")
        self.admin_roles, self.admin_client_roles = set(roles), client_roles
        self.post_logout_redirect = config.get("post_logout_redirect", True)
        if not isinstance(self.post_logout_redirect, bool):
            raise ValueError(f"{source}: \"post_logout_redirect\" must be true or false")
        self._fetch = fetch or self._http
        self._metadata = None
        self.lock = threading.Lock()
        self.pending = {}   # state -> login attempt
        self.sessions = {}  # session id -> signed-in user

    # ---- HTTP to the identity provider
    @staticmethod
    def _http(url, form=None):
        data = urlencode(form).encode() if form is not None else None
        request = Request(url, data=data, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=10) as response:
                return json.loads(response.read())
        except HTTPError as error:
            try:
                detail = json.loads(error.read()).get("error_description") or error.reason
            except (ValueError, AttributeError):
                detail = error.reason
            raise AuthError(f"Identity provider rejected the request: {detail}") from error
        except (URLError, OSError, ValueError) as error:
            raise AuthError(f"Could not reach the identity provider: {error}") from error

    def metadata(self) -> dict:
        if self._metadata is None:
            metadata = self._fetch(f"{self.issuer}/.well-known/openid-configuration")
            if not isinstance(metadata, dict) or metadata.get("issuer", "").rstrip("/") != self.issuer:
                raise AuthError("The identity provider's discovery document has a different issuer")
            for key in ("authorization_endpoint", "token_endpoint"):
                if not isinstance(metadata.get(key), str):
                    raise AuthError(f"The identity provider's discovery document has no {key}")
            self._metadata = metadata
        return self._metadata

    def _token(self, form: dict) -> dict:
        form = {**form, "client_id": self.client_id}
        if self.client_secret:
            form["client_secret"] = self.client_secret
        tokens = self._fetch(self.metadata()["token_endpoint"], form)
        if not isinstance(tokens, dict) or not isinstance(tokens.get("access_token"), str):
            raise AuthError("The identity provider returned no access token")
        return tokens

    # ---- Login flow (authorization code + PKCE)
    def base_url(self, host: str, scheme: str) -> str:
        return self.public_url or f"{scheme}://{host}"

    def begin(self, base_url: str) -> tuple[str, str]:
        """Returns (authorization URL, state). The state must also be bound to the browser by a cookie."""
        metadata = self.metadata()
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        redirect_uri = f"{base_url}/auth/callback"
        now = time.time()
        with self.lock:
            self.pending = {key: value for key, value in self.pending.items()
                            if value["created"] > now - LOGIN_TIMEOUT}
            self.pending[state] = {"nonce": nonce, "verifier": verifier, "redirect_uri": redirect_uri,
                                   "created": now}
        query = urlencode({
            "response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
            "scope": "openid profile", "state": state, "nonce": nonce,
            "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
        })
        return f"{metadata['authorization_endpoint']}?{query}", state

    def finish(self, state: str, browser_state: str, code: str) -> str:
        """Redeems the authorization code and returns a new session ID."""
        with self.lock:
            attempt = self.pending.pop(state, None) if state else None
        if attempt is None or not secrets.compare_digest(state, browser_state or "") \
                or attempt["created"] < time.time() - LOGIN_TIMEOUT:
            raise AuthError("This login link expired or was opened in another browser. Try again.")
        if not code:
            raise AuthError("The identity provider returned no authorization code")
        tokens = self._token({"grant_type": "authorization_code", "code": code,
                              "redirect_uri": attempt["redirect_uri"], "code_verifier": attempt["verifier"]})
        identity = self._check_id_token(tokens.get("id_token"), attempt["nonce"])
        session_id = secrets.token_urlsafe(32)
        with self.lock:
            self.sessions[session_id] = self._session(tokens, identity, time.time())
        return session_id

    def _check_id_token(self, token, nonce) -> dict:
        # The token comes straight from the token endpoint over TLS, so its signature need not be checked
        # (OpenID Connect Core 3.1.3.7); the claims still have to be ours.
        if not isinstance(token, str):
            raise AuthError("The identity provider returned no ID token")
        claims = jwt_claims(token)
        audience = claims.get("aud")
        audience = audience if isinstance(audience, list) else [audience]
        if claims.get("iss", "").rstrip("/") != self.issuer or self.client_id not in audience \
                or (len(audience) > 1 and claims.get("azp") != self.client_id):
            raise AuthError("The ID token was issued for someone else")
        if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < time.time() - 60:
            raise AuthError("The ID token has expired")
        if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise AuthError("The ID token does not belong to this login")
        return claims

    def is_admin(self, access: dict) -> bool:
        realm = access.get("realm_access")
        realm_roles = set(realm.get("roles") or []) if isinstance(realm, dict) else set()
        if realm_roles & self.admin_roles:
            return True
        resources = access.get("resource_access")
        resources = resources if isinstance(resources, dict) else {}
        for client, roles in self.admin_client_roles.items():
            granted = resources.get(client)
            if isinstance(granted, dict) and set(granted.get("roles") or []) & set(roles):
                return True
        return False

    def _session(self, tokens: dict, identity: dict, now: float, created=None) -> dict:
        access = jwt_claims(tokens["access_token"])
        lifetime = tokens.get("expires_in")
        expires = now + lifetime if isinstance(lifetime, (int, float)) else access.get("exp", now + 60)
        return {
            "name": identity.get("preferred_username") or identity.get("name") or identity.get("email")
                    or identity.get("sub", "user"),
            "sub": identity.get("sub"),
            "admin": self.is_admin(access),
            "expires": expires,
            "refresh_token": tokens.get("refresh_token"),
            "id_token": tokens.get("id_token"),
            "created": created or now,
        }

    def user(self, session_id):
        """Returns the signed-in user for a session ID, refreshing expired tokens, or None."""
        if not session_id:
            return None
        with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            return None
        now = time.time()
        if session["expires"] > now:
            return session
        if not session["refresh_token"] or session["created"] < now - SESSION_LIMIT:
            self.logout(session_id)
            return None
        try:
            tokens = self._token({"grant_type": "refresh_token", "refresh_token": session["refresh_token"]})
            if jwt_claims(tokens["access_token"]).get("sub") != session["sub"]:
                raise AuthError("The refreshed token belongs to another user")
            tokens = {"refresh_token": session["refresh_token"], "id_token": session["id_token"], **tokens}
            fresh = self._session(tokens, {"sub": session["sub"], "preferred_username": session["name"]},
                                  now, session["created"])
        except AuthError:
            self.logout(session_id)  # signed out or revoked at the identity provider
            return None
        with self.lock:
            if session_id in self.sessions:
                self.sessions[session_id] = fresh
        return fresh

    def logout(self, session_id, base_url=None):
        """Forgets the session; returns the identity provider's logout URL when one is known."""
        with self.lock:
            session = self.sessions.pop(session_id, None) if session_id else None
        if base_url is None:
            return None
        try:
            endpoint = self.metadata().get("end_session_endpoint")
        except AuthError:
            return None
        if not endpoint:
            return None
        query = {"client_id": self.client_id}
        if self.post_logout_redirect:
            query["post_logout_redirect_uri"] = f"{base_url}/"
        if session and session.get("id_token"):
            query["id_token_hint"] = session["id_token"]
        return f"{endpoint}?{urlencode(query)}"
