import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import aiohttp

from .commands import DEFAULT_COMMANDS, CommandRouter, parse_scopes

NAMESPACE = "/astrbot-zibll/v1"
PROTOCOL = "AZ-HMAC-SHA256"
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MAX_RESPONSE_BYTES = 65536


def site_url(value: object, allow_local_http: bool) -> str:
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError("网站地址必须为有效的 HTTPS URL")
    if any(ord(c) < 32 for c in value) or any(c in value for c in "\\?#%"):
        raise ValueError("网站地址不能包含查询参数、片段或转义路径")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("网站地址格式无效") from None
    if not parsed.hostname or parsed.username is not None:
        raise ValueError("网站地址不能包含账号或密码")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("网站端口无效")
    if any(part in {".", ".."} for part in parsed.path.split("/")):
        raise ValueError("网站路径不能包含相对路径段")
    local = parsed.hostname == "localhost"
    try:
        local = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (
        allow_local_http and local and parsed.scheme == "http"
    ):
        raise ValueError("仅显式开启本机调试时允许环回地址使用 HTTP")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    api_base_url: str
    public_site_url: str
    client_id: str
    key_id: str
    secret: str = field(repr=False)
    scopes: frozenset[tuple[str, str, str]]
    router: CommandRouter
    timeout: int
    allow_local_http: bool
    latest_count: int = 5
    search_count: int = 5
    avatar_hosts: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, config: dict):
        local_http = config.get("allow_local_http", False)
        if type(local_http) is not bool:
            raise ValueError("本机 HTTP 选项必须为布尔值")
        api = site_url(config.get("api_base_url", ""), local_http)
        if not api.endswith("/wp-json" + NAMESPACE):
            raise ValueError("API 基址必须以 /wp-json/astrbot-zibll/v1 结尾")
        public = site_url(config.get("public_site_url", ""), local_http)
        client, key = config.get("client_id", ""), config.get("key_id", "")
        if not all(isinstance(v, str) and ID_PATTERN.fullmatch(v) for v in (client, key)):
            raise ValueError("client_id 和 key_id 仅允许 1–64 位英文字母、数字、下划线或连字符")
        secret = config.get("shared_secret", "")
        if not isinstance(secret, str) or not re.fullmatch(r"[0-9a-f]{64}", secret):
            raise ValueError("共享密钥必须为网站后台生成的 64 位小写十六进制值")
        timeout = config.get("request_timeout", 15)
        if type(timeout) is not int or not 3 <= timeout <= 30:
            raise ValueError("请求超时必须为 3–30 秒")
        counts = [config.get("latest_count", 5), config.get("search_count", 5)]
        if any(type(v) is not int or not 1 <= v <= 10 for v in counts):
            raise ValueError("文章条数必须为 1–10")
        hosts = config.get("avatar_hosts", [])
        if not isinstance(hosts, list) or len(hosts) > 30:
            raise ValueError("头像域名必须为列表，最多 30 项")
        if any(not isinstance(h, str) or not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", h) for h in hosts):
            raise ValueError("头像域名不含协议、端口、路径或通配符")
        return cls(
            api, public, client, key, secret,
            parse_scopes(config.get("allowed_groups", [])),
            CommandRouter(config.get("commands", DEFAULT_COMMANDS)),
            timeout, local_http, *counts,
            frozenset([urlsplit(public).hostname, *(h.lower() for h in hosts)]),
        )


def sign_request(
    client_id: str,
    key_id: str,
    secret: str,
    method: str,
    route: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> str:
    canonical = "\n".join(
        (
            PROTOCOL, client_id, key_id, method, route, timestamp, nonce,
            hashlib.sha256(body).hexdigest(),
        )
    ).encode("utf-8")
    return hmac.new(bytes.fromhex(secret), canonical, hashlib.sha256).hexdigest()


class BridgeError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class BridgeClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._closed = False

    async def request(self, path: str, payload: dict | None = None) -> dict:
        if self._closed:
            raise BridgeError("client_closed")
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.settings.timeout, connect=3),
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
            )
        method = "GET" if payload is None else "POST"
        raw = b"" if payload is None else json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        now, nonce = str(int(time.time())), secrets.token_hex(16)
        signature = sign_request(
            self.settings.client_id, self.settings.key_id, self.settings.secret,
            method, NAMESPACE + path, now, nonce, raw,
        )
        headers = {
            "X-AZ-Client": self.settings.client_id,
            "X-AZ-Key": self.settings.key_id,
            "X-AZ-Time": now,
            "X-AZ-Nonce": nonce,
            "X-AZ-Signature": signature,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            async with self._session.request(
                method, self.settings.api_base_url + path, data=raw if payload is not None else None,
                headers=headers,
                allow_redirects=False,
            ) as response:
                status = response.status
                if response.content_type != "application/json":
                    if status != 200:
                        raise BridgeError({
                            401: "authentication_failed", 403: "forbidden",
                            409: "replay", 429: "rate_limited", 503: "unavailable",
                        }.get(status, "http_error"))
                    raise BridgeError("invalid_response")
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise BridgeError("response_too_large")
                payload = json.loads(body)
                if status != 200:
                    code = {
                        401: "authentication_failed", 403: "forbidden",
                        409: "replay", 429: "rate_limited", 503: "unavailable",
                    }.get(status, "http_error")
                    if isinstance(payload, dict) and isinstance(payload.get("code"), str) and payload["code"] in {
                        "not_bound", "already_bound", "challenge_expired", "candidate_mismatch",
                        "candidate_locked", "binding_conflict", "website_authorization_required",
                        "confirmation_expired", "binding_changed", "account_unavailable",
                        "account_restricted", "checkin_disabled", "busy", "event_expired",
                        "event_identity_required", "request_conflict", "invalid_parameters",
                    }:
                        code = payload["code"]
                    raise BridgeError(code)
        except (aiohttp.ClientError, TimeoutError):
            raise BridgeError("network_error") from None
        except (ValueError, UnicodeError, RecursionError):
            raise BridgeError("invalid_response") from None
        if not isinstance(payload, dict) or payload.get("code") != "ok":
            raise BridgeError("invalid_response")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BridgeError("invalid_response")
        return data

    async def capabilities(self) -> dict:
        data = await self.request("/capabilities")
        if data.get("protocol") != PROTOCOL:
            raise BridgeError("protocol_mismatch")
        try:
            actual_site = site_url(data.get("public_site_url", ""), self.settings.allow_local_http)
        except ValueError:
            raise BridgeError("invalid_response") from None
        if actual_site != self.settings.public_site_url:
            raise BridgeError("site_mismatch")
        return data

    async def close(self):
        self._closed = True
        if self._session is not None:
            await self._session.close()
