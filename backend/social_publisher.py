"""Official OAuth publishing adapters for YouTube, TikTok, Facebook, and Instagram.

OAuth credentials and encrypted provider tokens stay in local ``.daemoncut``
state. Rendered media and the Meta fallback bundle are resolved into the user's
Google Drive tree; no media or token material is written into the Git checkout.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from backend import config

LOGGER = logging.getLogger("daemoncut.publisher")
PLATFORMS = ("youtube", "tiktok", "facebook", "instagram")
VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".m4v"})
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg"})
SUBTITLE_SUFFIXES = frozenset({".srt"})
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_SUBTITLE_BYTES = 20 * 1024 * 1024
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
YOUTUBE_UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TIKTOK_API = "https://open.tiktokapis.com"
TIKTOK_AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"


class SocialPublisherError(RuntimeError):
    """Base error for OAuth, upload, and publishing operations."""


class PlatformNotConfigured(SocialPublisherError):
    """Raised when an individual platform has not been independently set up."""


class OAuthFlowError(SocialPublisherError):
    """Invalid, expired, denied, or incomplete OAuth callback."""


class TokenVaultError(SocialPublisherError):
    """Encrypted token storage is unavailable or cannot be decrypted."""


class ProviderRequestError(SocialPublisherError):
    """An official provider API rejected a request or was unreachable."""


@dataclass(frozen=True)
class HTTPResult:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        if not self.body:
            return {}
        try:
            value = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderRequestError("The provider returned an invalid JSON response.") from exc
        if not isinstance(value, dict):
            raise ProviderRequestError("The provider returned an unexpected response shape.")
        return value


Transport = Callable[[urllib.request.Request, float], Any]


def _secure_mode(path: Path) -> None:
    """Apply owner-only file permissions where the operating system supports them."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows enforces ACLs rather than POSIX mode bits; the runtime file is
        # still excluded from Git and remains inside the user's profile.
        pass


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _secure_mode(temporary)
        os.replace(temporary, path)
        _secure_mode(path)
    finally:
        temporary.unlink(missing_ok=True)


class OAuthTokenVault:
    """Fernet-encrypted platform tokens in an owner-only local runtime vault."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        key_path: str | Path | None = None,
        encryption_key: bytes | str | None = None,
    ) -> None:
        self.path = Path(path or (config.RUNTIME_DIR / "oauth_tokens.json"))
        self.key_path = Path(key_path or (self.path.parent / "oauth_vault.key"))
        self._provided_key = encryption_key.encode("ascii") if isinstance(encryption_key, str) else encryption_key
        self._lock = threading.RLock()

    def _fernet(self, *, create: bool) -> Any:
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:
            raise TokenVaultError("Encrypted OAuth token storage requires cryptography from requirements.txt") from exc
        key = self._provided_key
        if key is None:
            if self.key_path.exists():
                try:
                    key = self.key_path.read_bytes().strip()
                except OSError as exc:
                    raise TokenVaultError("The local OAuth vault key could not be read.") from exc
            elif create:
                key = Fernet.generate_key()
                _atomic_write(self.key_path, key + b"\n")
            else:
                return None
        try:
            return Fernet(key)
        except (TypeError, ValueError) as exc:
            raise TokenVaultError("The local OAuth vault key is invalid.") from exc

    def _read_locked(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        fernet = self._fernet(create=False)
        if fernet is None:
            raise TokenVaultError("The OAuth token vault exists but its encryption key is missing.")
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            ciphertext = envelope.get("encrypted_payload") if isinstance(envelope, dict) else None
            if not isinstance(ciphertext, str) or envelope.get("version") != 1:
                raise TokenVaultError("The OAuth token vault format is not recognized.")
            plaintext = fernet.decrypt(ciphertext.encode("ascii"))
            payload = json.loads(plaintext.decode("utf-8"))
        except TokenVaultError:
            raise
        except Exception as exc:
            raise TokenVaultError("The OAuth token vault could not be decrypted or parsed.") from exc
        if not isinstance(payload, dict):
            raise TokenVaultError("The OAuth token vault payload is invalid.")
        return {str(key): value for key, value in payload.items() if isinstance(value, dict)}

    def get(self, platform: str) -> dict[str, Any] | None:
        key = platform.casefold()
        with self._lock:
            value = self._read_locked().get(key)
            return dict(value) if value is not None else None

    def store(self, platform: str, token: Mapping[str, Any]) -> None:
        key = platform.casefold()
        if key not in {"youtube", "tiktok", "meta"}:
            raise ValueError("Token vault accepts only youtube, tiktok, or meta records")
        if not isinstance(token, Mapping) or not all(isinstance(name, str) for name in token):
            raise ValueError("token must be a mapping with string keys")
        with self._lock:
            payload = self._read_locked()
            payload[key] = dict(token)
            fernet = self._fernet(create=True)
            ciphertext = fernet.encrypt(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            envelope = json.dumps(
                {"version": 1, "encrypted_payload": ciphertext.decode("ascii")},
                separators=(",", ":"),
            ).encode("utf-8")
            _atomic_write(self.path, envelope + b"\n")

    def delete(self, platform: str) -> None:
        key = platform.casefold()
        with self._lock:
            payload = self._read_locked()
            if key not in payload:
                return
            payload.pop(key, None)
            if not payload:
                self.path.unlink(missing_ok=True)
                return
            fernet = self._fernet(create=True)
            ciphertext = fernet.encrypt(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            _atomic_write(
                self.path,
                json.dumps({"version": 1, "encrypted_payload": ciphertext.decode("ascii")}, separators=(",", ":")).encode("utf-8") + b"\n",
            )


@dataclass(frozen=True)
class PendingOAuth:
    platform: str
    verifier: str | None
    created_at: float


class OAuthStateStore:
    """One-time CSRF state and PKCE verifier store with bounded lifetime."""

    def __init__(self, *, ttl_seconds: int = 600, capacity: int = 1_000) -> None:
        if not 60 <= ttl_seconds <= 3_600 or not 1 <= capacity <= 10_000:
            raise ValueError("OAuth state limits are outside the supported range")
        self.ttl_seconds = ttl_seconds
        self.capacity = capacity
        self._pending: dict[str, PendingOAuth] = {}
        self._lock = threading.RLock()

    def begin(self, platform: str, *, verifier: str | None = None) -> str:
        now = time.time()
        state = secrets.token_urlsafe(32)
        with self._lock:
            self._prune(now)
            while len(self._pending) >= self.capacity:
                oldest = min(self._pending, key=lambda key: self._pending[key].created_at)
                self._pending.pop(oldest, None)
            self._pending[state] = PendingOAuth(platform, verifier, now)
        return state

    def consume(self, state: str) -> PendingOAuth:
        if not isinstance(state, str) or not state:
            raise OAuthFlowError("OAuth state is missing.")
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None or time.time() - pending.created_at > self.ttl_seconds:
            raise OAuthFlowError("OAuth state is invalid or expired; restart authorization from DaemonCut.")
        return pending

    def _prune(self, now: float) -> None:
        expired = [key for key, value in self._pending.items() if now - value.created_at > self.ttl_seconds]
        for key in expired:
            self._pending.pop(key, None)


class SocialPublisher:
    """Independent official OAuth publishing adapters with a Meta Drive fallback."""

    def __init__(
        self,
        *,
        vault: OAuthTokenVault | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        journal_path: str | Path | None = None,
    ) -> None:
        self.vault = vault or OAuthTokenVault()
        self.transport = transport or self._urlopen
        self.sleep = sleep
        self.states = OAuthStateStore()
        self.journal_path = Path(journal_path or (config.RUNTIME_DIR / "publish_operations.json"))
        self._journal_lock = threading.RLock()

    @staticmethod
    def _urlopen(request: urllib.request.Request, timeout: float) -> Any:
        return urllib.request.urlopen(request, timeout=timeout)

    @staticmethod
    def _header(headers: Mapping[str, Any], name: str) -> str | None:
        for key, value in headers.items():
            if str(key).casefold() == name.casefold():
                return str(value)
        return None

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        accepted_statuses: Sequence[int] = (200, 201, 202, 204),
        response_limit: int = 2 * 1024 * 1024,
    ) -> HTTPResult:
        if not url.startswith("https://"):
            raise ProviderRequestError("Publishing providers must use HTTPS.")
        request_headers = {"User-Agent": "DaemonCut-LocalPublisher/1.0", "Accept": "application/json"}
        request_headers.update(dict(headers or {}))
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            try:
                response = self.transport(request, float(config.SOCIAL_HTTP_TIMEOUT_SECONDS))
            except TypeError:
                # Friendly seam for test doubles accepting only the Request.
                response = self.transport(request)  # type: ignore[call-arg]
            with response:
                status_value = getattr(response, "status", None)
                status = int(status_value if status_value is not None else response.getcode())
                response_headers = dict(getattr(response, "headers", {}) or {})
                payload = response.read(response_limit + 1)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            response_headers = dict(exc.headers or {})
            try:
                payload = exc.read(response_limit + 1)
            finally:
                exc.close()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderRequestError("Could not reach the selected publishing provider.") from exc
        if len(payload) > response_limit:
            raise ProviderRequestError("The provider response exceeded the configured safety limit.")
        result = HTTPResult(status, response_headers, payload)
        if status not in accepted_statuses:
            raise ProviderRequestError(f"Publishing provider returned HTTP {status}.")
        return result

    def _post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        accepted_statuses: Sequence[int] = (200, 201, 202),
    ) -> tuple[HTTPResult, dict[str, Any]]:
        outgoing = {"Content-Type": "application/json; charset=UTF-8", **dict(headers or {})}
        response = self._request(
            url,
            method="POST",
            headers=outgoing,
            body=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            accepted_statuses=accepted_statuses,
        )
        parsed = response.json()
        self._check_provider_error(parsed)
        return response, parsed

    def _post_form(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        accepted_statuses: Sequence[int] = (200, 201, 202),
    ) -> tuple[HTTPResult, dict[str, Any]]:
        outgoing = {"Content-Type": "application/x-www-form-urlencoded", **dict(headers or {})}
        body = urllib.parse.urlencode({key: value for key, value in payload.items() if value is not None}).encode("utf-8")
        response = self._request(url, method="POST", headers=outgoing, body=body, accepted_statuses=accepted_statuses)
        parsed = response.json()
        self._check_provider_error(parsed)
        return response, parsed

    @staticmethod
    def _check_provider_error(payload: Mapping[str, Any]) -> None:
        error = payload.get("error")
        if isinstance(error, Mapping):
            code = str(error.get("code", "")).casefold()
            message = str(error.get("message", "")).casefold()
            if code not in {"", "0", "ok", "success"} or (message and message not in {"ok", "success"}):
                raise ProviderRequestError("The provider returned an application-level error.")
        if payload.get("error_description") and not payload.get("access_token"):
            raise ProviderRequestError("The provider rejected OAuth authorization.")

    @staticmethod
    def _expiry(token: Mapping[str, Any], *, field: str = "expires_in") -> float | None:
        try:
            value = float(token.get(field))
        except (TypeError, ValueError, OverflowError):
            return None
        if value <= 0:
            return None
        return time.time() + value

    @staticmethod
    def _url(platform: str, parameters: Mapping[str, Any], *, base: str) -> str:
        return f"{base}?{urllib.parse.urlencode(parameters)}"

    def authorization_url(self, platform: str) -> dict[str, str]:
        normalized = platform.casefold()
        if normalized not in PLATFORMS:
            raise ValueError(f"platform must be one of {', '.join(PLATFORMS)}")
        redirect_uri = config.OAUTH_REDIRECT_URI
        redirect = urllib.parse.urlsplit(redirect_uri)
        is_local_http = redirect.scheme == "http" and (redirect.hostname or "").casefold() in {"localhost", "127.0.0.1", "::1"}
        if not is_local_http and redirect.scheme != "https":
            raise ValueError("OAUTH_REDIRECT_URI must use localhost HTTP or HTTPS")
        if normalized == "youtube":
            if not config.YOUTUBE_OAUTH_CLIENT_ID or not config.YOUTUBE_OAUTH_CLIENT_SECRET:
                raise PlatformNotConfigured("YouTube OAuth client ID and secret are not configured.")
            state = self.states.begin("youtube")
            url = self._url("youtube", {
                "client_id": config.YOUTUBE_OAUTH_CLIENT_ID,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.force-ssl https://www.googleapis.com/auth/youtube.readonly",
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
                "state": state,
            }, base=GOOGLE_AUTHORIZE_URL)
        elif normalized == "tiktok":
            if not config.TIKTOK_CLIENT_KEY or not config.TIKTOK_CLIENT_SECRET:
                raise PlatformNotConfigured("TikTok client key and secret are not configured.")
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
            state = self.states.begin("tiktok", verifier=verifier)
            url = self._url("tiktok", {
                "client_key": config.TIKTOK_CLIENT_KEY,
                "response_type": "code",
                "scope": "user.info.basic,video.upload,video.publish",
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }, base=TIKTOK_AUTHORIZE_URL)
        else:
            if not config.META_APP_ID or not config.META_APP_SECRET:
                raise PlatformNotConfigured("Meta App ID and App Secret are not configured.")
            state = self.states.begin("meta")
            url = self._url("meta", {
                "client_id": config.META_APP_ID,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "pages_show_list,pages_read_engagement,pages_manage_posts,publish_video,instagram_basic,instagram_content_publish",
                "state": state,
            }, base=f"https://www.facebook.com/{config.META_GRAPH_API_VERSION}/dialog/oauth")
        return {"platform": normalized, "authorization_url": url, "state": state, "redirect_uri": redirect_uri}

    def complete_oauth(self, *, state: str, code: str | None, error: str | None = None) -> dict[str, Any]:
        pending = self.states.consume(state)
        if error:
            raise OAuthFlowError(f"The {pending.platform.title()} authorization was denied or cancelled.")
        if not isinstance(code, str) or not code.strip() or len(code) > 8_192:
            raise OAuthFlowError("OAuth callback did not include a valid authorization code.")
        platform = pending.platform
        if platform == "youtube":
            token = self._exchange_google_code(code.strip())
            token.setdefault("channel_id", self._youtube_channel_id(str(token.get("access_token", ""))))
            self.vault.store("youtube", token)
            return {"platform": platform, "connected": True, "account_id": token.get("channel_id")}
        if platform == "tiktok":
            token = self._exchange_tiktok_code(code.strip(), pending.verifier)
            self.vault.store("tiktok", token)
            return {"platform": platform, "connected": True, "account_id": token.get("open_id")}
        token = self._exchange_meta_code(code.strip())
        token = self._select_meta_page(token)
        self.vault.store("meta", token)
        return {"platform": platform, "connected": True, "account_id": token.get("page_id")}

    def _exchange_google_code(self, code: str) -> dict[str, Any]:
        _, payload = self._post_form(GOOGLE_TOKEN_URL, {
            "code": code,
            "client_id": config.YOUTUBE_OAUTH_CLIENT_ID,
            "client_secret": config.YOUTUBE_OAUTH_CLIENT_SECRET,
            "redirect_uri": config.OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        })
        if not isinstance(payload.get("access_token"), str):
            raise OAuthFlowError("YouTube did not return an access token.")
        result = dict(payload)
        result["expires_at"] = self._expiry(payload)
        return result

    def _exchange_tiktok_code(self, code: str, verifier: str | None) -> dict[str, Any]:
        if not verifier:
            raise OAuthFlowError("TikTok authorization is missing its PKCE verifier.")
        _, payload = self._post_form(TIKTOK_TOKEN_URL, {
            "client_key": config.TIKTOK_CLIENT_KEY,
            "client_secret": config.TIKTOK_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": config.OAUTH_REDIRECT_URI,
            "code_verifier": verifier,
        })
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        if not isinstance(data.get("access_token"), str):
            raise OAuthFlowError("TikTok did not return an access token.")
        result = dict(data)
        result["expires_at"] = self._expiry(data)
        if data.get("refresh_expires_in"):
            result["refresh_expires_at"] = self._expiry(data, field="refresh_expires_in")
        return result

    def _exchange_meta_code(self, code: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({
            "client_id": config.META_APP_ID,
            "client_secret": config.META_APP_SECRET,
            "redirect_uri": config.OAUTH_REDIRECT_URI,
            "code": code,
        })
        response = self._request(
            f"https://graph.facebook.com/{config.META_GRAPH_API_VERSION}/oauth/access_token?{query}",
            accepted_statuses=(200,),
        )
        payload = response.json()
        if not isinstance(payload.get("access_token"), str):
            raise OAuthFlowError("Meta did not return an access token.")
        payload["expires_at"] = self._expiry(payload)
        return payload

    def _select_meta_page(self, user_token: Mapping[str, Any]) -> dict[str, Any]:
        access = str(user_token.get("access_token", ""))
        if not access:
            return dict(user_token)
        try:
            _, payload = self._get_json(
                f"https://graph.facebook.com/{config.META_GRAPH_API_VERSION}/me/accounts?fields=id,name,access_token,instagram_business_account",
                headers={"Authorization": f"Bearer {access}"},
            )
            pages = payload.get("data", [])
        except SocialPublisherError:
            return dict(user_token)
        if not isinstance(pages, list) or not pages:
            return dict(user_token)
        page = next((item for item in pages if isinstance(item, Mapping) and str(item.get("id")) == config.META_PAGE_ID), None)
        if page is None and not config.META_PAGE_ID:
            page = next((item for item in pages if isinstance(item, Mapping)), None)
        if not isinstance(page, Mapping):
            return dict(user_token)
        result = dict(user_token)
        result["page_id"] = str(page.get("id", ""))
        result["page_name"] = str(page.get("name", ""))
        result["page_access_token"] = str(page.get("access_token", access))
        instagram = page.get("instagram_business_account")
        if isinstance(instagram, Mapping) and instagram.get("id"):
            result["instagram_user_id"] = str(instagram["id"])
        return result

    def _get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        accepted_statuses: Sequence[int] = (200, 201),
    ) -> tuple[HTTPResult, dict[str, Any]]:
        response = self._request(url, headers=headers, accepted_statuses=accepted_statuses)
        payload = response.json()
        self._check_provider_error(payload)
        return response, payload

    def _youtube_channel_id(self, access_token: str) -> str | None:
        if not access_token:
            return None
        try:
            _, payload = self._get_json(
                f"{YOUTUBE_API}/channels?part=id,snippet&mine=true",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            items = payload.get("items", [])
            if items and isinstance(items[0], Mapping):
                return str(items[0].get("id", "")) or None
        except SocialPublisherError:
            LOGGER.info("YouTube OAuth completed; channel discovery can be retried later.")
        return None

    @staticmethod
    def _token_is_fresh(token: Mapping[str, Any]) -> bool:
        try:
            return float(token.get("expires_at", 0)) > time.time() + 60
        except (TypeError, ValueError):
            return bool(token.get("access_token"))

    def _access_token(self, provider: str) -> tuple[str, dict[str, Any]]:
        token = self.vault.get(provider)
        if token is None:
            raise PlatformNotConfigured(f"{provider.title()} is not connected. Authorize it from the DaemonCut key vault.")
        if self._token_is_fresh(token) and token.get("access_token"):
            return str(token["access_token"]), token
        refreshed = self._refresh_token(provider, token)
        if refreshed is None or not refreshed.get("access_token"):
            raise PlatformNotConfigured(f"{provider.title()} access has expired; reconnect it in the key vault.")
        self.vault.store(provider, refreshed)
        return str(refreshed["access_token"]), refreshed

    def _refresh_token(self, provider: str, token: Mapping[str, Any]) -> dict[str, Any] | None:
        refresh = token.get("refresh_token")
        if not refresh:
            if token.get("access_token") and not token.get("expires_at"):
                return dict(token)
            return None
        if provider == "youtube":
            _, renewed = self._post_form(GOOGLE_TOKEN_URL, {
                "client_id": config.YOUTUBE_OAUTH_CLIENT_ID,
                "client_secret": config.YOUTUBE_OAUTH_CLIENT_SECRET,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            })
        elif provider == "tiktok":
            _, renewed = self._post_form(TIKTOK_TOKEN_URL, {
                "client_key": config.TIKTOK_CLIENT_KEY,
                "client_secret": config.TIKTOK_CLIENT_SECRET,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            })
            if isinstance(renewed.get("data"), Mapping):
                renewed = dict(renewed["data"])
        else:
            if not config.META_APP_ID or not config.META_APP_SECRET:
                return None
            _, renewed = self._get_json(
                "https://graph.facebook.com/oauth/access_token?" + urllib.parse.urlencode({
                    "grant_type": "fb_exchange_token",
                    "client_id": config.META_APP_ID,
                    "client_secret": config.META_APP_SECRET,
                    "fb_exchange_token": token.get("access_token", ""),
                })
            )
        merged = dict(token)
        merged.update(renewed)
        merged["expires_at"] = self._expiry(renewed)
        return merged

    def _meta_token_and_ids(self) -> tuple[str | None, str | None, str | None, dict[str, Any] | None]:
        direct = config.META_PAGE_ACCESS_TOKEN
        token_record = self.vault.get("meta")
        if token_record and not self._token_is_fresh(token_record):
            try:
                renewed = self._refresh_token("meta", token_record)
                if renewed and renewed.get("access_token"):
                    renewed = self._select_meta_page(renewed)
                    self.vault.store("meta", renewed)
                    token_record = renewed
            except SocialPublisherError:
                LOGGER.info("Meta token refresh failed; the Drive export fallback remains available.")
        token = direct or (
            str(token_record.get("page_access_token") or token_record.get("access_token") or "")
            if token_record else ""
        )
        page_id = config.META_PAGE_ID or (str(token_record.get("page_id", "")) if token_record else "")
        instagram_id = config.META_INSTAGRAM_USER_ID or (str(token_record.get("instagram_user_id", "")) if token_record else "")
        return token or None, page_id or None, instagram_id or None, token_record

    def platform_status(self) -> dict[str, dict[str, Any]]:
        try:
            youtube = self.vault.get("youtube")
            tiktok = self.vault.get("tiktok")
            meta_token, page_id, instagram_id, meta = self._meta_token_and_ids()
        except TokenVaultError:
            youtube = tiktok = meta = None
            meta_token = page_id = instagram_id = None
            vault_error = True
        else:
            vault_error = False
        statuses: dict[str, dict[str, Any]] = {}
        for platform in PLATFORMS:
            if platform == "youtube":
                creds = bool(config.YOUTUBE_OAUTH_CLIENT_ID and config.YOUTUBE_OAUTH_CLIENT_SECRET)
                connected = bool(youtube and (youtube.get("access_token") or youtube.get("refresh_token")))
                account_id = youtube.get("channel_id") if youtube else None
            elif platform == "tiktok":
                creds = bool(config.TIKTOK_CLIENT_KEY and config.TIKTOK_CLIENT_SECRET)
                connected = bool(tiktok and (tiktok.get("access_token") or tiktok.get("refresh_token")))
                account_id = tiktok.get("open_id") if tiktok else None
            else:
                creds = bool(config.META_API_ENABLED and (config.META_PAGE_ACCESS_TOKEN or (config.META_APP_ID and config.META_APP_SECRET)))
                required_id = page_id if platform == "facebook" else instagram_id
                connected = bool(config.META_API_ENABLED and meta_token and required_id)
                account_id = required_id
            if vault_error:
                label = "vault_error"
            elif connected:
                label = "connected"
            elif platform in {"facebook", "instagram"}:
                label = "ready_to_post" if not config.META_API_ENABLED or not creds or not meta_token else "needs_account_id"
            else:
                label = "needs_authorization" if creds else "needs_configuration"
            statuses[platform] = {
                "platform": platform,
                "status": label,
                "configured": creds,
                "connected": connected,
                "can_authorize": creds and not vault_error,
                "account_id": str(account_id) if account_id else None,
                "fallback_available": platform in {"facebook", "instagram"},
                "message": self._status_message(platform, label),
            }
        return statuses

    @staticmethod
    def _status_message(platform: str, status: str) -> str:
        if status == "connected":
            return "Official account connected."
        if platform in {"facebook", "instagram"} and status == "ready_to_post":
            return "Meta publishing is not connected; the Drive export bundle is ready as a fallback."
        if status == "vault_error":
            return "The encrypted local token vault needs attention."
        if status == "needs_account_id":
            return "Add the Meta Page or Instagram business account ID."
        if status == "needs_authorization":
            return "Client credentials are present; complete official OAuth authorization."
        return "Add this platform's client credentials in the local key vault."

    def is_connected(self, platform: str) -> bool:
        normalized = platform.casefold()
        return bool(self.platform_status().get(normalized, {}).get("connected"))

    def _journal(self) -> dict[str, dict[str, Any]]:
        if not self.journal_path.exists():
            return {}
        try:
            payload = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            LOGGER.warning("The local publishing journal could not be read; database leases remain active.")
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(key): value for key, value in payload.items() if isinstance(value, dict)}

    def _journal_get(self, platform: str, idempotency_key: str) -> dict[str, Any] | None:
        key = f"{platform}:{idempotency_key}"
        with self._journal_lock:
            return dict(self._journal().get(key, {})) or None

    def _journal_put(self, platform: str, idempotency_key: str, value: Mapping[str, Any]) -> None:
        key = f"{platform}:{idempotency_key}"
        with self._journal_lock:
            payload = self._journal()
            payload[key] = {**dict(value), "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            # Retain only the newest 2,000 lightweight operation records.
            if len(payload) > 2_000:
                ordered = sorted(payload.items(), key=lambda item: str(item[1].get("updated_at", "")))
                payload = dict(ordered[-2_000:])
            _atomic_write(self.journal_path, json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")

    @staticmethod
    def _safe_component(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-_")[:100]
        if not cleaned:
            raise ValueError("episode_id must contain letters or numbers")
        return cleaned

    @staticmethod
    def _nested_value(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
        for name in names:
            value = mapping.get(name)
            if value not in (None, ""):
                return value
        for key in ("render", "render_result", "thumbnail", "thumbnails", "audio", "subtitles", "assets"):
            nested = mapping.get(key)
            if isinstance(nested, Mapping):
                value = SocialPublisher._nested_value(nested, names)
                if value not in (None, ""):
                    return value
            elif isinstance(nested, list):
                for item in nested:
                    if isinstance(item, Mapping):
                        value = SocialPublisher._nested_value(item, names)
                        if value not in (None, ""):
                            return value
        return None

    def _drive_asset(self, raw: Any, *, suffixes: frozenset[str], label: str, required: bool = False) -> Path | None:
        if raw in (None, ""):
            if required:
                raise SocialPublisherError(f"This episode has no {label} path in its Drive metadata.")
            return None
        if not isinstance(raw, (str, os.PathLike)):
            raise SocialPublisherError(f"The {label} path is invalid.")
        candidate = Path(raw).expanduser().resolve(strict=False)
        drive_root = config.resolve_drive_path().resolve(strict=False)
        try:
            candidate.relative_to(drive_root)
        except ValueError as exc:
            raise SocialPublisherError(f"The {label} must be stored under the configured Google Drive folder.") from exc
        if candidate.suffix.casefold() not in suffixes:
            raise SocialPublisherError(f"The {label} must use one of: {', '.join(sorted(suffixes))}.")
        if not candidate.is_file():
            raise SocialPublisherError(f"The {label} file is unavailable in the local Drive workspace.")
        return candidate

    def _episode_assets(self, episode: Mapping[str, Any]) -> tuple[Path, Path | None, Path | None]:
        metadata = episode.get("metadata", {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        combined = dict(metadata)
        for key in ("video_path", "rendered_path", "cover_path", "thumbnail_path", "srt_path", "subtitle_path"):
            if episode.get(key) not in (None, ""):
                combined[key] = episode[key]
        video_raw = self._nested_value(combined, ("rendered_path", "video_path", "media_path", "asset_path", "master_path", "path", "output_path"))
        cover_raw = self._nested_value(combined, ("cover_path", "thumbnail_path", "image_path", "poster_path"))
        srt_raw = self._nested_value(combined, ("srt_path", "subtitle_path", "captions_path", "caption_path"))
        video = self._drive_asset(video_raw, suffixes=VIDEO_SUFFIXES, label="rendered video", required=True)
        assert video is not None
        cover = self._drive_asset(cover_raw, suffixes=IMAGE_SUFFIXES, label="cover image")
        subtitles = self._drive_asset(srt_raw, suffixes=SUBTITLE_SUFFIXES, label="SRT subtitles")
        return video, cover, subtitles

    def export_ready_to_post_bundle(self, episode: Mapping[str, Any]) -> dict[str, Any]:
        """Copy a complete manual-post package into Drive's archive folder."""
        episode_id = self._safe_component(str(episode.get("episode_id", "")))
        video, cover, subtitles = self._episode_assets(episode)
        if video.suffix.casefold() != ".mp4":
            raise SocialPublisherError("The Ready-to-Post bundle requires a rendered MP4 master.")
        root = config.resolve_drive_path("renders_archive", "ready_to_post", episode_id)
        root.mkdir(parents=True, exist_ok=True)
        video_target = root / "rendered.mp4"
        if video.resolve() != video_target.resolve():
            self._copy_atomically(video, video_target)
        cover_target = root / "cover.jpg"
        if cover:
            self._copy_atomically(cover, cover_target)
        else:
            self._create_cover(cover_target, str(episode.get("title", "DaemonCut short")))
        srt_target = root / "captions.srt"
        if subtitles:
            if subtitles.stat().st_size > MAX_SUBTITLE_BYTES:
                raise SocialPublisherError("The SRT sidecar exceeds the 20 MiB publishing limit.")
            self._copy_atomically(subtitles, srt_target)
        else:
            self._write_generated_srt(srt_target, episode)
        post_copy = self._post_copy(episode)
        _atomic_write(root / "post_copy.txt", post_copy.encode("utf-8"))
        return {
            "status": "ready_to_post",
            "remote_id": str(root),
            "bundle_path": str(root),
            "files": {
                "video": str(video_target),
                "cover": str(cover_target),
                "subtitles": str(srt_target),
                "post_copy": str(root / "post_copy.txt"),
            },
        }

    @staticmethod
    def _copy_atomically(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        try:
            with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=config.SOCIAL_UPLOAD_CHUNK_BYTES)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _create_cover(path: Path, title: str) -> None:
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError as exc:
            raise SocialPublisherError("Pillow is required to create a placeholder Drive cover image.") from exc
        width, height = 1280, 720
        image = Image.new("RGB", (width, height), config.UI_PALETTE["matte_obsidian"])
        draw = ImageDraw.Draw(image)
        border = config.UI_PALETTE["structural_border"]
        brass = config.UI_PALETTE["muted_brass"]
        parchment = config.UI_PALETTE["warm_parchment"]
        draw.rectangle((24, 24, width - 25, height - 25), outline=border, width=2)
        draw.rectangle((48, 48, 58, height - 48), fill=brass)
        draw.text((88, 86), "DAEMONCUT  /  READY TO POST", fill=brass, font=ImageFont.load_default())
        words = title.strip()[:180].split()
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) > 22 and current:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        y = 260
        for line in lines[:4]:
            draw.text((88, y), line, fill=parchment, font=ImageFont.load_default())
            y += 54
        draw.line((88, height - 114, width - 88, height - 114), fill=border, width=2)
        draw.text((88, height - 92), "LOCAL STUDIO MASTER", fill=brass, font=ImageFont.load_default())
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
        try:
            image.save(temporary, format="JPEG", quality=92, optimize=True)
            _secure_mode(temporary)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_generated_srt(path: Path, episode: Mapping[str, Any]) -> None:
        content = str(episode.get("script") or episode.get("hook") or episode.get("title") or "")
        content = re.sub(r"\s+", " ", content).strip()
        if not content:
            content = str(episode.get("title", "DaemonCut short"))
        line = content[:500]
        payload = f"1\n00:00:00,000 --> 00:00:08,000\n{line}\n"
        _atomic_write(path, payload.encode("utf-8"))

    @staticmethod
    def _post_copy(episode: Mapping[str, Any]) -> str:
        metadata = episode.get("metadata", {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        title = str(episode.get("title", "Untitled short")).strip()[:200]
        caption = str(metadata.get("caption") or metadata.get("description") or episode.get("script") or episode.get("hook") or title).strip()[:5_000]
        raw_tags = metadata.get("hashtags", [])
        if isinstance(raw_tags, str):
            raw_tags = raw_tags.split()
        tags = []
        if isinstance(raw_tags, Sequence):
            for tag in raw_tags:
                clean = re.sub(r"[^\w]", "", str(tag).lstrip("#"), flags=re.UNICODE)
                if clean and f"#{clean}" not in tags:
                    tags.append(f"#{clean}")
                if len(tags) >= 15:
                    break
        if not tags:
            tags = ["#Shorts", "#DaemonCut"]
        comment = str(metadata.get("first_comment") or "Which moment stood out to you? Share your take below.").strip()[:1_000]
        return (
            f"TITLE\n{title}\n\nCAPTION\n{caption}\n\nHASHTAGS\n{' '.join(tags)}\n\nFIRST COMMENT\n{comment}\n"
        )

    def publish_episode(
        self,
        episode: Mapping[str, Any],
        *,
        platforms: Sequence[str] | None = None,
        idempotency_key: str | None = None,
    ) -> list[dict[str, Any]]:
        requested = tuple(platform.casefold() for platform in (platforms or PLATFORMS))
        if not requested or any(platform not in PLATFORMS for platform in requested):
            raise ValueError(f"platforms must be selected from {', '.join(PLATFORMS)}")
        if len(set(requested)) != len(requested):
            requested = tuple(dict.fromkeys(requested))
        base_key = idempotency_key or f"manual:{episode.get('episode_id', uuid.uuid4().hex)}"
        results: list[dict[str, Any]] = []
        for platform in requested:
            try:
                result = self._publish_platform(platform, episode, idempotency_key=base_key)
            except PlatformNotConfigured as exc:
                result = {"platform": platform, "status": "not_configured", "message": str(exc)}
            except (SocialPublisherError, OSError, ValueError) as exc:
                result = {"platform": platform, "status": "failed", "message": str(exc)[:300]}
                LOGGER.warning("Publishing to %s failed (%s).", platform, type(exc).__name__)
            results.append({"platform": platform, **result})
        return results

    def scheduler_upload_handler(
        self,
        episode: Mapping[str, Any],
        channel: Mapping[str, Any],
        *,
        idempotency_key: str,
        lock_token: str,
        database: Any,
    ) -> dict[str, Any]:
        del lock_token, database
        platform = str(channel.get("platform", "")).casefold()
        if platform not in PLATFORMS:
            raise PlatformNotConfigured(f"Unsupported publishing platform {platform!r}.")
        result = self._publish_platform(platform, episode, idempotency_key=idempotency_key)
        if result.get("status") not in {"published", "processing", "ready_to_post", "already_published"}:
            raise SocialPublisherError(str(result.get("message", "Publishing did not complete.")))
        return result

    def _publish_platform(self, platform: str, episode: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        journal = self._journal_get(platform, idempotency_key)
        if journal and journal.get("status") in {"published", "processing", "ready_to_post"}:
            return {**journal, "status": "already_published" if journal.get("status") == "published" else journal["status"]}
        if platform in {"facebook", "instagram"} and not config.META_API_ENABLED:
            result = self.export_ready_to_post_bundle(episode)
            self._journal_put(platform, idempotency_key, result)
            return result
        if platform == "facebook" or platform == "instagram":
            try:
                token, page_id, instagram_id, _meta = self._meta_token_and_ids()
                target_id = page_id if platform == "facebook" else instagram_id
                if not token or not target_id:
                    raise PlatformNotConfigured("Meta OAuth or the required account ID is unavailable.")
            except TokenVaultError:
                token = None
                target_id = None
            if not token or not target_id:
                result = self.export_ready_to_post_bundle(episode)
                result["message"] = "Meta API is not connected; exported a Ready-to-Post Drive bundle instead."
                self._journal_put(platform, idempotency_key, result)
                return result
            return self._publish_meta(platform, episode, idempotency_key=idempotency_key, access_token=token, target_id=target_id)
        if platform == "youtube":
            access_token, _token = self._access_token("youtube")
            return self._publish_youtube(episode, idempotency_key=idempotency_key, access_token=access_token)
        if platform == "tiktok":
            access_token, _token = self._access_token("tiktok")
            return self._publish_tiktok(episode, idempotency_key=idempotency_key, access_token=access_token)
        raise ValueError(f"Unsupported platform {platform!r}")

    def _episode_copy(self, episode: Mapping[str, Any]) -> tuple[str, str, list[str], str]:
        post = self._post_copy(episode)
        fields: dict[str, str] = {}
        current = ""
        for line in post.splitlines():
            if line in {"TITLE", "CAPTION", "HASHTAGS", "FIRST COMMENT"}:
                current = line
                fields[current] = ""
            elif current:
                fields[current] += ("\n" if fields[current] else "") + line
        return (
            fields.get("TITLE", "Untitled short"),
            fields.get("CAPTION", ""),
            fields.get("HASHTAGS", "").split(),
            fields.get("FIRST COMMENT", ""),
        )

    def _publish_youtube(self, episode: Mapping[str, Any], *, idempotency_key: str, access_token: str) -> dict[str, Any]:
        video, cover, subtitles = self._episode_assets(episode)
        title, caption, tags, first_comment = self._episode_copy(episode)
        if not subtitles:
            scratch = config.TEMP_DIR / f"publish_{uuid.uuid4().hex}.srt"
            self._write_generated_srt(scratch, episode)
            subtitles = scratch
            remove_subtitles = True
        else:
            remove_subtitles = False
        try:
            remote_id = self._youtube_upload_video(video, title=title, description=caption, tags=tags, access_token=access_token)
            self._journal_put("youtube", idempotency_key, {"status": "uploaded", "remote_id": remote_id})
            warnings: list[str] = []
            if cover:
                try:
                    self._youtube_set_thumbnail(remote_id, cover, access_token)
                except SocialPublisherError:
                    warnings.append("Video posted, but the custom thumbnail could not be attached.")
            if subtitles:
                try:
                    self._youtube_upload_captions(remote_id, subtitles, access_token)
                except SocialPublisherError:
                    warnings.append("Video posted, but the SRT captions could not be attached.")
            if first_comment:
                try:
                    self._youtube_first_comment(remote_id, first_comment, access_token)
                except SocialPublisherError:
                    warnings.append("Video posted, but the first comment could not be added.")
            result = {"status": "published", "remote_id": remote_id, "warnings": warnings}
            self._journal_put("youtube", idempotency_key, result)
            return result
        finally:
            if remove_subtitles and subtitles:
                subtitles.unlink(missing_ok=True)

    def _youtube_upload_video(
        self,
        video: Path,
        *,
        title: str,
        description: str,
        tags: Sequence[str],
        access_token: str,
    ) -> str:
        size = video.stat().st_size
        if size <= 0:
            raise SocialPublisherError("The rendered video is empty.")
        privacy = config.YOUTUBE_PRIVACY_STATUS.casefold()
        if privacy not in {"private", "unlisted", "public"}:
            raise SocialPublisherError("YOUTUBE_PRIVACY_STATUS must be private, unlisted, or public.")
        metadata = {
            "snippet": {
                "title": title[:100],
                "description": description[:5_000],
                "tags": [tag.lstrip("#")[:100] for tag in tags[:15]],
                "categoryId": "22",
            },
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }
        start = self._request(
            f"{YOUTUBE_UPLOAD_API}/videos?uploadType=resumable&part=snippet,status",
            method="POST",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(size),
                "X-Upload-Content-Type": "video/mp4",
            },
            body=json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
            accepted_statuses=(200, 201),
        )
        location = self._header(start.headers, "Location")
        if not location or not location.startswith("https://"):
            raise ProviderRequestError("YouTube did not provide a secure resumable upload URL.")
        offset = 0
        video_id: str | None = None
        chunk_bytes = min(config.SOCIAL_UPLOAD_CHUNK_BYTES, 8 * 1024 * 1024)
        with video.open("rb") as handle:
            while offset < size:
                handle.seek(offset)
                chunk = handle.read(min(chunk_bytes, size - offset))
                if not chunk:
                    raise SocialPublisherError("The video file ended before the resumable upload completed.")
                end = offset + len(chunk) - 1
                response = self._request(
                    location,
                    method="PUT",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{size}",
                    },
                    body=chunk,
                    accepted_statuses=(200, 201, 308),
                )
                if response.status in {200, 201}:
                    payload = response.json()
                    video_id = str(payload.get("id", "")) or None
                    offset = size
                else:
                    range_header = self._header(response.headers, "Range")
                    match = re.search(r"bytes=0-(\d+)", range_header or "")
                    next_offset = int(match.group(1)) + 1 if match else end + 1
                    if next_offset <= offset or next_offset > size:
                        raise ProviderRequestError("YouTube returned an invalid resumable upload offset.")
                    offset = next_offset
        if not video_id:
            raise ProviderRequestError("YouTube did not return a video ID after upload.")
        return video_id

    def _youtube_set_thumbnail(self, video_id: str, image: Path, access_token: str) -> None:
        if image.stat().st_size > MAX_METADATA_BYTES:
            raise SocialPublisherError("YouTube custom thumbnails must not exceed 2 MiB.")
        self._request(
            f"{YOUTUBE_UPLOAD_API}/thumbnails/set?videoId={urllib.parse.quote(video_id)}&uploadType=media",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "image/jpeg"},
            body=image.read_bytes(),
            accepted_statuses=(200, 201),
        )

    def _youtube_upload_captions(self, video_id: str, subtitles: Path, access_token: str) -> None:
        if subtitles.stat().st_size > MAX_SUBTITLE_BYTES:
            raise SocialPublisherError("The SRT sidecar exceeds the 20 MiB caption limit.")
        boundary = f"daemoncut-{uuid.uuid4().hex}"
        metadata = json.dumps({"snippet": {"videoId": video_id, "language": "en", "name": "English captions", "isDraft": False}}).encode("utf-8")
        content = subtitles.read_bytes()
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode("ascii")
            + metadata
            + f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\nContent-Disposition: attachment; filename=\"captions.srt\"\r\n\r\n".encode("ascii")
            + content
            + f"\r\n--{boundary}--\r\n".encode("ascii")
        )
        self._request(
            f"{YOUTUBE_UPLOAD_API}/captions?part=snippet&uploadType=multipart",
            method="POST",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": f"multipart/related; boundary={boundary}"},
            body=body,
            accepted_statuses=(200, 201),
        )

    def _youtube_first_comment(self, video_id: str, comment: str, access_token: str) -> None:
        self._post_json(
            f"{YOUTUBE_API}/commentThreads?part=snippet",
            {"snippet": {"videoId": video_id, "topLevelComment": {"snippet": {"textOriginal": comment[:1_000]}}}},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def _publish_tiktok(self, episode: Mapping[str, Any], *, idempotency_key: str, access_token: str) -> dict[str, Any]:
        video, _cover, _srt = self._episode_assets(episode)
        title, caption, hashtags, _comment = self._episode_copy(episode)
        privacy = config.TIKTOK_PRIVACY_LEVEL
        if privacy not in {"PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"}:
            raise SocialPublisherError("TIKTOK_PRIVACY_LEVEL is not a supported privacy option.")
        size = video.stat().st_size
        chunk_size = min(max(config.SOCIAL_UPLOAD_CHUNK_BYTES, 5 * 1024 * 1024), 64 * 1024 * 1024)
        chunk_count = max(1, (size + chunk_size - 1) // chunk_size)
        _, initialized = self._post_json(
            f"{TIKTOK_API}/v2/post/publish/video/init/",
            {
                "post_info": {
                    "title": (f"{caption or title} {' '.join(hashtags)}".strip())[:2_200],
                    "privacy_level": privacy,
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                    "video_cover_timestamp_ms": 1_000,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": chunk_count,
                },
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )
        data = initialized.get("data") if isinstance(initialized.get("data"), Mapping) else initialized
        publish_id = str(data.get("publish_id", ""))
        upload_url = str(data.get("upload_url", ""))
        self._validate_provider_upload_url(upload_url, allowed_suffixes=("tiktokapis.com", "tiktokcdn.com"))
        if not publish_id or not upload_url:
            raise ProviderRequestError("TikTok did not return a publish ID and upload URL.")
        offset = 0
        with video.open("rb") as handle:
            while offset < size:
                handle.seek(offset)
                chunk = handle.read(min(chunk_size, size - offset))
                if not chunk:
                    raise SocialPublisherError("The video file ended before TikTok's upload completed.")
                end = offset + len(chunk) - 1
                result = self._request(
                    upload_url,
                    method="PUT",
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{size}",
                    },
                    body=chunk,
                    accepted_statuses=(200, 201, 206),
                )
                offset = end + 1
                if result.status == 206:
                    range_header = self._header(result.headers, "Range")
                    match = re.search(r"bytes=0-(\d+)", range_header or "")
                    if match:
                        offset = int(match.group(1)) + 1
        state = self._wait_tiktok_status(publish_id, access_token)
        status = "published" if state == "PUBLISH_COMPLETE" else "processing"
        result = {"status": status, "remote_id": publish_id, "provider_status": state, "hashtags": hashtags}
        self._journal_put("tiktok", idempotency_key, result)
        return result

    @staticmethod
    def _validate_provider_upload_url(url: str, *, allowed_suffixes: Sequence[str]) -> None:
        parts = urllib.parse.urlsplit(url)
        hostname = (parts.hostname or "").casefold()
        try:
            port = parts.port
        except ValueError as exc:
            raise ProviderRequestError("The provider returned a malformed upload URL.") from exc
        if parts.scheme != "https" or not hostname or parts.username or parts.password or port not in (None, 443):
            raise ProviderRequestError("The provider returned an unsafe upload URL.")
        if not any(hostname == suffix or hostname.endswith("." + suffix) for suffix in allowed_suffixes):
            raise ProviderRequestError("The provider returned an unexpected upload host.")

    def _wait_tiktok_status(self, publish_id: str, access_token: str) -> str:
        state = "PROCESSING_UPLOAD"
        for attempt in range(config.SOCIAL_STATUS_POLL_ATTEMPTS):
            _, payload = self._post_json(
                f"{TIKTOK_API}/v2/post/publish/status/fetch/",
                {"publish_id": publish_id},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
            state = str(data.get("status", "PROCESSING_UPLOAD"))
            if state in {"PUBLISH_COMPLETE", "FAILED"}:
                break
            if attempt + 1 < config.SOCIAL_STATUS_POLL_ATTEMPTS:
                self.sleep(config.SOCIAL_STATUS_POLL_SECONDS)
        if state == "FAILED":
            raise ProviderRequestError("TikTok reports that this publish operation failed.")
        return state

    def _publish_meta(
        self,
        platform: str,
        episode: Mapping[str, Any],
        *,
        idempotency_key: str,
        access_token: str,
        target_id: str,
    ) -> dict[str, Any]:
        video, cover, _subtitles = self._episode_assets(episode)
        title, caption, hashtags, first_comment = self._episode_copy(episode)
        caption_with_tags = " ".join(part for part in (caption, " ".join(hashtags)) if part).strip()
        if platform == "facebook":
            remote_id = self._publish_facebook_reel(video, title, caption_with_tags, target_id, access_token)
            if cover:
                try:
                    self._meta_set_thumbnail(remote_id, cover, access_token)
                except SocialPublisherError:
                    LOGGER.info("Facebook Reel posted; its custom thumbnail could not be attached.")
            if first_comment:
                try:
                    self._meta_comment(remote_id, first_comment, access_token)
                except SocialPublisherError:
                    LOGGER.info("Facebook Reel posted; its first comment could not be attached.")
        else:
            remote_id = self._publish_instagram_reel(video, caption_with_tags, target_id, access_token)
            if first_comment:
                try:
                    self._meta_comment(remote_id, first_comment, access_token)
                except SocialPublisherError:
                    LOGGER.info("Instagram Reel posted; its first comment could not be attached.")
        result = {"status": "published", "remote_id": remote_id}
        self._journal_put(platform, idempotency_key, result)
        return result

    def _meta_url(self, path: str) -> str:
        return f"https://graph.facebook.com/{config.META_GRAPH_API_VERSION}/{path.lstrip('/')}"

    def _publish_facebook_reel(self, video: Path, title: str, caption: str, page_id: str, access_token: str) -> str:
        start_url = self._meta_url(f"{urllib.parse.quote(page_id, safe='')}/video_reels")
        _, start = self._post_form(start_url, {"upload_phase": "start", "access_token": access_token})
        video_id = str(start.get("video_id", ""))
        upload_url = str(start.get("upload_url", ""))
        self._validate_provider_upload_url(upload_url, allowed_suffixes=("facebook.com",))
        if not video_id or not upload_url:
            raise ProviderRequestError("Meta did not return a secure Reels upload session.")
        self._upload_meta_chunks(video, upload_url, access_token)
        _, finished = self._post_form(start_url, {
            "upload_phase": "finish",
            "video_id": video_id,
            "video_state": "PUBLISHED",
            "title": title[:255],
            "description": caption[:5_000],
            "access_token": access_token,
        })
        return str(finished.get("id") or finished.get("video_id") or video_id)

    def _upload_meta_chunks(self, video: Path, upload_url: str, access_token: str) -> None:
        size = video.stat().st_size
        chunk_size = min(config.SOCIAL_UPLOAD_CHUNK_BYTES, 32 * 1024 * 1024)
        offset = 0
        with video.open("rb") as handle:
            while offset < size:
                handle.seek(offset)
                chunk = handle.read(min(chunk_size, size - offset))
                if not chunk:
                    raise SocialPublisherError("The video file ended before Meta's upload completed.")
                response = self._request(
                    upload_url,
                    method="POST",
                    headers={
                        "Authorization": f"OAuth {access_token}",
                        "offset": str(offset),
                        "file_size": str(size),
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(len(chunk)),
                    },
                    body=chunk,
                    accepted_statuses=(200, 201),
                )
                payload = response.json()
                try:
                    next_offset = int(payload.get("end_offset", offset + len(chunk)))
                except (TypeError, ValueError):
                    next_offset = offset + len(chunk)
                if next_offset <= offset or next_offset > size:
                    raise ProviderRequestError("Meta returned an invalid upload offset.")
                offset = next_offset

    def _publish_instagram_reel(self, video: Path, caption: str, instagram_id: str, access_token: str) -> str:
        size = video.stat().st_size
        _, created = self._post_form(self._meta_url(f"{urllib.parse.quote(instagram_id, safe='')}/media"), {
            "media_type": "REELS",
            "upload_type": "resumable",
            "file_size": size,
            "caption": caption[:2_200],
            "share_to_feed": "true",
            "thumb_offset": 1_000,
            "access_token": access_token,
        })
        container_id = str(created.get("id", ""))
        if not container_id:
            raise ProviderRequestError("Instagram did not return a Reel media container.")
        upload_url = f"https://rupload.facebook.com/ig-api-upload/{urllib.parse.quote(container_id, safe='')}"
        self._validate_provider_upload_url(upload_url, allowed_suffixes=("facebook.com",))
        self._upload_meta_chunks(video, upload_url, access_token)
        self._wait_meta_container(container_id, access_token)
        _, published = self._post_form(self._meta_url(f"{urllib.parse.quote(instagram_id, safe='')}/media_publish"), {
            "creation_id": container_id,
            "access_token": access_token,
        })
        remote_id = str(published.get("id", ""))
        if not remote_id:
            raise ProviderRequestError("Instagram did not return a published Reel ID.")
        return remote_id

    def _wait_meta_container(self, container_id: str, access_token: str) -> None:
        for attempt in range(config.SOCIAL_STATUS_POLL_ATTEMPTS):
            _, payload = self._get_json(
                self._meta_url(f"{urllib.parse.quote(container_id, safe='')}?fields=status_code"),
                headers={"Authorization": f"Bearer {access_token}"},
            )
            status = str(payload.get("status_code", "IN_PROGRESS"))
            if status == "FINISHED":
                return
            if status in {"ERROR", "EXPIRED"}:
                raise ProviderRequestError("Instagram could not process this Reel upload.")
            if attempt + 1 < config.SOCIAL_STATUS_POLL_ATTEMPTS:
                self.sleep(config.SOCIAL_STATUS_POLL_SECONDS)
        raise ProviderRequestError("Instagram Reel processing timed out; the Drive master is still available.")

    def _meta_comment(self, media_id: str, message: str, access_token: str) -> None:
        self._post_form(self._meta_url(f"{urllib.parse.quote(media_id, safe='')}/comments"), {
            "message": message[:1_000],
            "access_token": access_token,
        })

    def _meta_set_thumbnail(self, video_id: str, cover: Path, access_token: str) -> None:
        if cover.stat().st_size > MAX_METADATA_BYTES:
            raise SocialPublisherError("The Meta cover image exceeds the 2 MiB API limit.")
        boundary = f"daemoncut-{uuid.uuid4().hex}"
        content = cover.read_bytes()
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"source\"; filename=\"cover.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode("ascii")
            + content
            + f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"is_preferred\"\r\n\r\ntrue\r\n--{boundary}--\r\n".encode("ascii")
        )
        self._request(
            self._meta_url(f"{urllib.parse.quote(video_id, safe='')}/thumbnails"),
            method="POST",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
            body=body,
            accepted_statuses=(200, 201),
        )


__all__ = [
    "HTTPResult",
    "OAuthStateStore",
    "OAuthTokenVault",
    "OAuthFlowError",
    "PlatformNotConfigured",
    "ProviderRequestError",
    "SocialPublisher",
    "SocialPublisherError",
    "PLATFORMS",
]
