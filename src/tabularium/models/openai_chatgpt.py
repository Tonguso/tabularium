"""Sign in with ChatGPT plan usage for an agent (local loopback OAuth client).

Credentials are DPAPI-protected for the current Windows user. The OAuth flow is
interactive and must be initiated on the PC running the agent.

One ChatGPTPlan per agent: the store, its lock and the names shown during sign-in are that agent's own. Until
2026-10-14 they were module globals set by whichever agent imported the module first; the locks were bound before
any agent set them, so all four agents locked %LOCALAPPDATA%\\Agent\\..., and the dashboard's per-agent status read
failed silently.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

try:
    import win32crypt                                    # Windows DPAPI, the desktop extra (pywin32)
except ImportError:                                      # the connection cannot be stored, everything else works
    win32crypt = None
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils

from tabularium.memory.memory_store import memory_lock


def _dpapi():
    if win32crypt is None:
        raise RuntimeError("Storing a ChatGPT connection needs pywin32 (pip install tabularium[desktop]).")
    return win32crypt


def _dpapi():
    if win32crypt is None:
        raise RuntimeError("Storing a ChatGPT connection needs pywin32 (pip install tabularium[desktop]).")
    return win32crypt

AUTH_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _verify_id_token(token: str, client_id: str, nonce: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed ID token")
    header = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
    claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    config = requests.get("https://auth.openai.com/.well-known/openid-configuration", timeout=15)
    config.raise_for_status()
    jwks = requests.get(config.json()["jwks_uri"], timeout=15)
    jwks.raise_for_status()
    jwk = next((k for k in jwks.json()["keys"] if k.get("kid") == header.get("kid")), None)
    if not jwk:
        raise ValueError("No matching signing key")
    signed = (parts[0] + "." + parts[1]).encode("ascii")
    signature = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
    alg = header.get("alg")
    digest = {"RS256": hashes.SHA256, "RS384": hashes.SHA384,
              "RS512": hashes.SHA512, "ES256": hashes.SHA256}.get(alg)
    if not digest:
        raise ValueError("Unsupported ID-token signature algorithm")
    if alg.startswith("RS") and jwk.get("kty") == "RSA":
        number = lambda v: int.from_bytes(base64.urlsafe_b64decode(v + "=" * (-len(v) % 4)), "big")
        key = rsa.RSAPublicNumbers(number(jwk["e"]), number(jwk["n"])).public_key()
        key.verify(signature, signed, padding.PKCS1v15(), digest())
    elif alg == "ES256" and jwk.get("kty") == "EC":
        number = lambda v: int.from_bytes(base64.urlsafe_b64decode(v + "=" * (-len(v) % 4)), "big")
        key = ec.EllipticCurvePublicNumbers(number(jwk["x"]), number(jwk["y"]), ec.SECP256R1()).public_key()
        raw = utils.encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
        key.verify(raw, signed, ec.ECDSA(digest()))
    else:
        raise ValueError("ID-token key type does not match its algorithm")
    aud = claims.get("aud")
    if (claims.get("iss") != ISSUER or client_id not in (aud if isinstance(aud, list) else [aud])
            or claims.get("nonce") != nonce or claims.get("exp", 0) < time.time() - 60):
        raise ValueError("ID-token claims did not match this sign-in")
    return claims


def _callback_server(expected_state: str, title: str, timeout: int = 300):
    result: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/auth/callback" or query.get("state", [""])[0] != expected_state:
                self.send_error(400, "Invalid OAuth callback")
                return
            result.update({k: v[0] for k, v in query.items() if v})
            page = ("<html><title>" + title + " connected</title><body>You can return to Telegram.</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            done.set()

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    uri = f"http://127.0.0.1:{server.server_port}/auth/callback"

    def serve():
        end = time.monotonic() + timeout
        while not done.is_set() and time.monotonic() < end:
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return server, thread, done, result, uri


def _locked(method):
    """Serialise a method on this agent's own sign-in store (a file lock, so background workers share it)."""
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with memory_lock(self.store):
            return method(self, *args, **kwargs)
    return wrapped


class ChatGPTPlan:
    def __init__(self, name: str):
        self.name = name
        self.store = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / name / "openai_chatgpt.dat"
        self.host_id_file = self.store.with_name("openai_host_id.txt")

    def _host_id(self) -> str:
        if self.host_id_file.exists():
            return self.host_id_file.read_text(encoding="ascii").strip()
        value = "urn:uuid:" + str(__import__("uuid").uuid4())
        self.host_id_file.parent.mkdir(parents=True, exist_ok=True)
        self.host_id_file.write_text(value, encoding="ascii")
        return value

    @_locked
    def _save(self, profile: dict) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(profile, separators=(",", ":")).encode("utf-8")
        encrypted = _dpapi().CryptProtectData(raw, self.name + " ChatGPT connection", None, None, None, 0)
        temp = self.store.with_suffix(".tmp")
        temp.write_bytes(encrypted)
        os.replace(temp, self.store)

    def load(self) -> dict | None:
        if not self.store.exists():
            return None
        try:
            raw = _dpapi().CryptUnprotectData(self.store.read_bytes(), None, None, None, 0)[1]
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError("Saved ChatGPT sign-in could not be decrypted; reconnect with /openai connect.") from exc

    def connect(self) -> tuple[str, bool]:
        existing = self.load()
        requested_client_id = existing.get("client_id") if existing else "dynamic_agent_client"
        state = _b64(secrets.token_bytes(32))
        nonce = _b64(secrets.token_bytes(32))
        verifier = _b64(secrets.token_bytes(48))
        challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
        server, thread, done, result, redirect_uri = _callback_server(state, self.name)
        params = {
            "client_id": requested_client_id,
            "ext_agent_host_id": self._host_id(), "response_type": "code", "redirect_uri": redirect_uri,
            "scope": SCOPES, "resource": RESOURCE, "state": state, "nonce": nonce,
            "code_challenge_method": "S256", "code_challenge": challenge,
        }
        if existing:
            if existing.get("id_token"):
                params["id_token_hint"] = existing["id_token"]
            if existing.get("email"):
                params["login_hint"] = existing["email"]
        else:
            params["agent_name_hint"] = self.name
        url = AUTH_URL + "?" + urllib.parse.urlencode(params)
        try:
            webbrowser.open(url)
            if not done.wait(300):
                raise TimeoutError("ChatGPT sign-in timed out. Send /openai connect to try again.")
        finally:
            server.server_close()
        thread.join(timeout=2)
        if result.get("error"):
            raise RuntimeError("ChatGPT sign-in was declined or failed: " + result["error"])
        code, callback_client_id = result.get("code"), result.get("client_id")
        if existing:
            if callback_client_id and callback_client_id != requested_client_id:
                raise RuntimeError("ChatGPT returned a different client registration than the selected account.")
            client_id = requested_client_id
        else:
            client_id = callback_client_id
        if not code or not client_id or client_id == "dynamic_agent_client":
            raise RuntimeError(f"ChatGPT did not complete {self.name}'s client registration.")
        token = requests.post(TOKEN_URL, data={
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "code_verifier": verifier, "redirect_uri": redirect_uri, "resource": RESOURCE,
        }, timeout=30)
        token.raise_for_status()
        data = token.json()
        scopes = data.get("scope", "").split()
        if not data.get("access_token") or not data.get("refresh_token") or not data.get("id_token"):
            raise RuntimeError("OpenAI returned an incomplete sign-in response.")
        try:
            claims = _verify_id_token(data["id_token"], client_id, nonce)
            email = claims.get("email", "ChatGPT account")
        except Exception as exc:
            raise RuntimeError("Could not validate the ChatGPT sign-in identity.") from exc
        profile = {**data, "client_id": client_id, "email": email, "scopes": scopes,
                   "expires_at": time.time() + int(data.get("expires_in", 3600)),
                   "saved_at": int(time.time()), "ext_agent_host_id": self._host_id()}
        self._save(profile)
        return email, PLAN_SCOPE in scopes

    @_locked
    def access_token(self, force_refresh: bool = False) -> str | None:
        profile = self.load()
        if not profile or PLAN_SCOPE not in profile.get("scopes", []):
            return None
        # A retry interval, not an assumed OpenAI allowance reset time.
        if profile.get("quota_exhausted") and time.time() < profile.get("quota_retry_at", 0):
            return None
        if not force_refresh and profile.get("expires_at", 0) > time.time() + 60:
            return profile["access_token"]
        response = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token", "client_id": profile["client_id"],
            "refresh_token": profile["refresh_token"], "resource": RESOURCE,
        }, timeout=30)
        response.raise_for_status()
        data = response.json()
        profile.update(data)
        profile["scopes"] = data.get("scope", " ".join(profile["scopes"])).split()
        profile["expires_at"] = time.time() + int(data.get("expires_in", 3600))
        self._save(profile)
        if PLAN_SCOPE not in profile["scopes"]:
            return None
        return profile["access_token"]

    def status(self) -> str:
        profile = self.load()
        if not profile:
            return "ChatGPT plan: not connected. Connect with /openai connect. API key remains the fallback."
        enabled = PLAN_SCOPE in profile.get("scopes", [])
        if profile.get("quota_exhausted"):
            plan = "limit reached; API fallback active; subscription retried on the first new task after a 5-hour cooldown (/openai resume retries sooner)"
        else:
            plan = "enabled" if enabled else "not authorized for ChatGPT plan usage"
        return f"ChatGPT account: {profile.get('email', 'connected')}\nPlan usage: {plan}\nAPI key: fallback"

    @_locked
    def mark_quota_exhausted(self) -> None:
        profile = self.load()
        if profile:
            profile["quota_exhausted"] = True
            profile["quota_retry_at"] = time.time() + 5 * 60 * 60
            self._save(profile)

    @_locked
    def mark_plan_available(self) -> bool:
        """Clear the cooldown only after a successful plan response; report recovery once."""
        profile = self.load()
        if not profile or not profile.get("quota_exhausted"):
            return False
        profile.pop("quota_exhausted", None)
        profile.pop("quota_retry_at", None)
        self._save(profile)
        return True

    @_locked
    def resume_plan(self) -> bool:
        profile = self.load()
        if not profile or PLAN_SCOPE not in profile.get("scopes", []):
            return False
        profile.pop("quota_exhausted", None)
        profile.pop("quota_retry_at", None)
        self._save(profile)
        return True

    @_locked
    def disconnect(self) -> None:
        profile = self.load()
        if not profile:
            return
        try:
            config = requests.get("https://auth.openai.com/.well-known/openid-configuration", timeout=15).json()
            endpoint = config.get("revocation_endpoint")
            if endpoint:
                requests.post(endpoint, data={"token": profile["refresh_token"],
                    "token_type_hint": "refresh_token", "client_id": profile["client_id"]}, timeout=15)
        finally:
            self.store.unlink(missing_ok=True)
