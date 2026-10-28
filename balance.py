import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, DecimalException, localcontext
from urllib.parse import urlsplit

import aiohttp

from .commands import DEFAULT_COMMANDS, parse_scopes

DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
MAX_RESPONSE_BYTES = 65536
ERROR_MESSAGES = {
    "not_configured": "余额接口或凭据未配置。",
    "invalid_config": "余额配置无效，请管理员检查后台配置。",
    "authentication_failed": "余额查询认证失败，请管理员检查凭据及权限。",
    "rate_limited": "余额接口请求过于频繁，请稍后再试。",
    "unavailable": "余额服务暂不可用，请稍后再试。",
    "timeout": "余额查询超时，请稍后再试。",
    "network_error": "余额接口连接失败，请稍后再试。",
    "redirect_refused": "余额接口返回重定向，请管理员检查固定查询地址。",
    "http_error": "余额接口请求失败，请管理员检查接口配置。",
    "provider_error": "余额服务返回查询失败，请管理员检查接口和凭据。",
    "invalid_response": "余额接口返回了无法识别的数据。",
    "response_too_large": "余额接口响应过大，已停止读取。",
    "client_closed": "余额查询已停止。",
}


class BalanceError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _label(value: object, maximum: int) -> str:
    if (
        not isinstance(value, str) or not 1 <= len(value) <= maximum
        or value != value.strip()
        or any(
            unicodedata.category(c).startswith("C") or c.isspace() and c != " "
            for c in value
        )
    ):
        raise ValueError("余额名称或单位格式无效")
    return value


def _credential(value: object) -> str:
    if (
        not isinstance(value, str) or len(value) > 4096
        or any(not 33 <= ord(c) <= 126 for c in value)
    ):
        raise ValueError("余额凭据必须为不含空白的文本")
    return value


def _amount(value: object) -> Decimal:
    if (
        isinstance(value, bool) or not isinstance(value, (str, int, Decimal))
        or isinstance(value, str) and (
            len(value) > 80 or not re.fullmatch(
                r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", value,
            )
        )
    ):
        raise BalanceError("invalid_response")
    try:
        amount = Decimal(value)
    except (DecimalException, ValueError):
        raise BalanceError("invalid_response") from None
    if (
        not amount.is_finite() or len(amount.as_tuple().digits) > 36
        or not -18 <= amount.as_tuple().exponent <= 18
        or amount.adjusted() > 18
    ):
        raise BalanceError("invalid_response")
    return amount


def _format_amount(value: Decimal) -> str:
    text = format(abs(value) if value.is_zero() else value, "f")
    if "." not in text:
        return text + ".00"
    if len(text.rsplit(".", 1)[1]) == 1:
        return text + "0"
    return text


def _balance_url(value: object, allow_key_quota: bool = False) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > 2048
        or any(c.isspace() or unicodedata.category(c).startswith("C") for c in value)
        or any(c in value for c in "\\?#%")
    ):
        raise ValueError("余额地址必须为不含查询参数的固定 HTTPS URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("余额地址格式无效") from None
    if (
        parsed.scheme != "https" or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or port is not None and not 1 <= port <= 65535
        or any(part in {".", ".."} for part in parsed.path.split("/"))
        or not allow_key_quota and parsed.path.rstrip("/").lower().endswith("/api/usage/token")
    ):
        raise ValueError("余额地址必须是固定 HTTPS 接口")
    return value


def _base_url(value: object) -> str:
    return _balance_url(value, allow_key_quota=True)


def _base_path(base_url: str) -> tuple[str, str]:
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    for suffix in ("/v1/responses", "/v1"):
        if lowered.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return f"{parsed.scheme}://{parsed.netloc}", path


def _is_new_api_url(base_url: str) -> bool:
    parsed = urlsplit(base_url)
    known_names = {"new-api", "one-api"}
    host_labels = (parsed.hostname or "").rstrip(".").casefold().split(".")
    path_segments = parsed.path.casefold().split("/")
    return any(label in known_names for label in (*host_labels, *path_segments))


@dataclass(frozen=True)
class CompatibleBalanceSettings:
    name: str
    url: str
    credential: str = field(repr=False)
    method: str
    balance_path: tuple[str, ...]
    unit: str
    divisor: Decimal
    user_id: str
    result_kind: str

    @classmethod
    def from_config(cls, config: Mapping):
        name = _label(config.get("name", "兼容服务商"), 32)
        if name.casefold() == "deepseek":
            raise ValueError("兼容服务商名称不能使用 DeepSeek")
        legacy_configured = bool(config.get("url") or config.get("credential"))
        if legacy_configured:
            url = _balance_url(config.get("url", ""))
            credential = _credential(config.get("credential", ""))
            if not credential:
                raise ValueError("兼容服务商余额凭据未配置")
            method = config.get("method", "GET")
            if method not in {"GET", "POST"}:
                raise ValueError("余额请求方法只能为 GET 或 POST")
            path = config.get("balance_path", "")
            if (
                not isinstance(path, str) or len(path) > 128
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*){0,7}", path)
            ):
                raise ValueError("余额字段必须为简单点分对象路径")
            unit = _label(config.get("unit", ""), 16)
            if any(c.isspace() for c in unit):
                raise ValueError("余额单位不能包含空白")
            try:
                divisor = _amount(config.get("divisor", "1"))
            except BalanceError:
                raise ValueError("余额换算除数无效") from None
            if divisor <= 0:
                raise ValueError("余额换算除数必须大于零")
            user_id = config.get("user_id", "")
            if not isinstance(user_id, str) or user_id and not re.fullmatch(r"[1-9][0-9]{0,19}", user_id):
                raise ValueError("New API 用户 ID 必须为正整数字符串")
            return cls(
                name, url, credential, method, tuple(path.split(".")), unit,
                divisor, user_id, "account",
            )

        base_url = _base_url(config.get("base_url", ""))
        api_key = _credential(config.get("api_key", ""))
        if not api_key:
            raise ValueError("兼容服务商 API Key 未配置")
        parsed = urlsplit(base_url)
        if parsed.hostname.casefold() == "api.deepseek.com":
            return cls(
                name, DEEPSEEK_BALANCE_URL, api_key, "GET", (), "", Decimal(1), "",
                "deepseek",
            )
        known_sources = {
            "api.moonshot.cn": (
                "https://api.moonshot.cn/v1/users/me/balance",
                ("data", "available_balance"),
                "CNY",
                Decimal(1),
                "known",
            ),
            "api.moonshot.ai": (
                "https://api.moonshot.ai/v1/users/me/balance",
                ("data", "available_balance"),
                "USD",
                Decimal(1),
                "known",
            ),
            "openrouter.ai": (
                "https://openrouter.ai/api/v1/credits",
                (),
                "USD",
                Decimal(1),
                "openrouter",
            ),
            "api.stepfun.com": (
                "https://api.stepfun.com/v1/accounts",
                ("balance",),
                "CNY",
                Decimal(1),
                "known",
            ),
            "api.stepfun.ai": (
                "https://api.stepfun.ai/v1/accounts",
                ("balance",),
                "USD",
                Decimal(1),
                "known",
            ),
            "api.commandcode.ai": (
                "https://api.commandcode.ai/alpha/billing/credits",
                (),
                "USD",
                Decimal(1),
                "commandcode",
            ),
            "ollama.com": (
                "https://ollama.com/api/balance",
                (),
                "USD",
                Decimal(1),
                "ollama",
            ),
            "aihubmix.com": (
                "https://aihubmix.com/dashboard/billing/remain",
                ("total_usage",),
                "USD",
                Decimal(1),
                "aihubmix_key",
            ),
        }
        known = known_sources.get(parsed.hostname.casefold())
        if known is not None:
            endpoint, path, unit, divisor, result_kind = known
            return cls(
                name, endpoint, api_key, "GET", path, unit, divisor, "",
                result_kind,
            )
        if _is_new_api_url(base_url):
            origin, prefix = _base_path(base_url)
            base_path = parsed.path.rstrip("/").lower()
            if base_path.endswith("/api/user/self"):
                raise ValueError("New API 账户接口需要账户 access token，不能使用模型 API Key")
            if base_path.endswith("/api/usage/token"):
                endpoint = base_url.rstrip("/")
            else:
                endpoint = origin + prefix + "/api/usage/token"
            return cls(
                name, _balance_url(endpoint, allow_key_quota=True), api_key, "GET",
                ("data", "total_available"), "USD", Decimal("500000"), "",
                "api_key_quota",
            )
        raise LookupError("余额接口无法从该服务商 Base URL 推导")


@dataclass(frozen=True)
class BalanceSettings:
    command: str
    scopes: frozenset[tuple[str, str, str]]
    timeout: int
    deepseek_api_key: str = field(repr=False)
    compatible: CompatibleBalanceSettings | None
    deepseek_error: str | None = None
    compatible_error: str | None = None
    compatible_name: str = "兼容服务商"

    @classmethod
    def from_config(cls, config: Mapping):
        command = config.get("balance_command", "/模型余额")
        if (
            not isinstance(command, str) or not 1 <= len(command) <= 32
            or any(c.isspace() or unicodedata.category(c).startswith("C") for c in command)
            or re.fullmatch(r"[0-9]{6}", command)
        ):
            raise ValueError("余额指令必须为不含空白或控制字符的 1–32 个字符")
        reserved = set(DEFAULT_COMMANDS.values()) | {"/适配器核查"}
        configured_commands = config.get("commands", {})
        if isinstance(configured_commands, Mapping):
            reserved.update(value for value in configured_commands.values() if isinstance(value, str))
        if command in reserved:
            raise ValueError("余额指令不能与网站指令或适配器核查指令重名")
        timeout = config.get("request_timeout", 15)
        if type(timeout) is not int or not 3 <= timeout <= 30:
            raise ValueError("余额请求超时必须为 3–30 秒")
        scopes = parse_scopes(config.get("allowed_groups", []))
        deepseek_error = None
        try:
            key = _credential(config.get("deepseek_api_key", ""))
        except ValueError:
            key, deepseek_error = "", "invalid_config"
        compatible, compatible_error = None, None
        name = "兼容服务商"
        values = config.get("compatible_balance", {})
        if not isinstance(values, Mapping) or type(values.get("enabled", False)) is not bool:
            compatible_error = "invalid_config"
        elif values.get("enabled", False):
            try:
                name = _label(values.get("name", name), 32)
                secrets = (
                    key,
                    values.get("credential", ""),
                    values.get("api_key", ""),
                )
                if name.casefold() == "deepseek" or any(
                    isinstance(secret, str) and secret and secret in name for secret in secrets
                ):
                    name = "兼容服务商"
                    raise ValueError("兼容服务商名称无效")
                legacy_configured = bool(values.get("url") or values.get("credential"))
                auto_configured = bool(values.get("base_url") or values.get("api_key"))
                if legacy_configured:
                    if any(values.get(key, "") == "" for key in ("url", "credential", "balance_path", "unit")):
                        compatible_error = "not_configured"
                    else:
                        compatible = CompatibleBalanceSettings.from_config(values)
                elif auto_configured:
                    if not values.get("base_url") or not values.get("api_key"):
                        compatible_error = "not_configured"
                    else:
                        try:
                            compatible = CompatibleBalanceSettings.from_config(values)
                        except LookupError:
                            compatible_error = "not_configured"
                else:
                    compatible_error = "not_configured"
                if compatible is not None and any(
                    isinstance(secret, str) and secret and secret in compatible.unit
                    for secret in secrets
                ):
                    compatible = None
                    raise ValueError("余额单位无效")
            except (ValueError, TypeError):
                compatible_error = "invalid_config"
        return cls(command, scopes, timeout, key, compatible, deepseek_error, compatible_error, name)


def _reject_constant(value: str):
    raise ValueError("invalid JSON number")


class BalanceClient:
    def __init__(self, settings: BalanceSettings):
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._closed = False

    async def _request(self, url: str, credential: str, method: str = "GET", user_id: str = "") -> dict:
        if self._closed:
            raise BalanceError("client_closed")
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.settings.timeout, connect=3),
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
            )
        headers = {"Authorization": "Bearer " + credential, "Accept": "application/json"}
        if user_id:
            headers["New-Api-User"] = user_id
        try:
            async with self._session.request(
                method, url, headers=headers, allow_redirects=False,
            ) as response:
                if response.status != 200:
                    if 300 <= response.status < 400:
                        code = "redirect_refused"
                    elif response.status in {401, 403}:
                        code = "authentication_failed"
                    elif response.status == 429:
                        code = "rate_limited"
                    elif response.status >= 500:
                        code = "unavailable"
                    else:
                        code = "http_error"
                    raise BalanceError(code)
                if response.content_type != "application/json" and not response.content_type.endswith("+json"):
                    raise BalanceError("invalid_response")
                if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
                    raise BalanceError("response_too_large")
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise BalanceError("response_too_large")
                payload = json.loads(
                    body, parse_float=Decimal, parse_int=Decimal, parse_constant=_reject_constant,
                )
        except TimeoutError:
            raise BalanceError("timeout") from None
        except aiohttp.ClientError:
            raise BalanceError("network_error") from None
        except (ValueError, UnicodeError, RecursionError, DecimalException):
            raise BalanceError("invalid_response") from None
        if not isinstance(payload, dict):
            raise BalanceError("invalid_response")
        if "success" in payload and payload["success"] is not True:
            raise BalanceError("provider_error")
        if payload.get("error") is not None:
            raise BalanceError("provider_error")
        return payload

    async def _deepseek(self) -> str:
        if self.settings.deepseek_error:
            raise BalanceError(self.settings.deepseek_error)
        if not self.settings.deepseek_api_key:
            raise BalanceError("not_configured")
        return await self._deepseek_balance(
            DEEPSEEK_BALANCE_URL, self.settings.deepseek_api_key, "DeepSeek",
        )

    async def _deepseek_balance(self, url: str, credential: str, title: str) -> str:
        payload = await self._request(url, credential)
        available, infos = payload.get("is_available"), payload.get("balance_infos")
        if type(available) is not bool or not isinstance(infos, list) or not 1 <= len(infos) <= 10:
            raise BalanceError("invalid_response")
        lines = [title + " 当前余额", "可用：" + ("是" if available else "否")]
        currencies = set()
        for info in infos:
            if not isinstance(info, dict):
                raise BalanceError("invalid_response")
            currency = info.get("currency")
            if (
                not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)
                or currency in currencies
            ):
                raise BalanceError("invalid_response")
            currencies.add(currency)
            for label, key in (
                ("总余额", "total_balance"), ("赠送余额", "granted_balance"),
                ("充值余额", "topped_up_balance"),
            ):
                lines.append(f"{label}：{currency} {_format_amount(_amount(info.get(key)))}")
        lines.append("来源：DeepSeek 官方账户余额")
        return "\n".join(lines)

    async def _compatible(self) -> str:
        if self.settings.compatible_error:
            raise BalanceError(self.settings.compatible_error)
        source = self.settings.compatible
        if source is None:
            raise BalanceError("not_configured")
        if source.result_kind == "deepseek":
            return await self._deepseek_balance(source.url, source.credential, source.name)
        value = await self._request(source.url, source.credential, source.method, source.user_id)
        if source.result_kind == "openrouter":
            if not isinstance(value.get("data"), dict):
                raise BalanceError("invalid_response")
            data = value["data"]
            credits = _amount(data.get("total_credits"))
            usage_value = data.get("total_usage", 0)
            usage = Decimal(0) if usage_value is None else _amount(usage_value)
            value = credits - usage
        elif source.result_kind == "commandcode":
            credits = value.get("credits")
            if not isinstance(credits, dict):
                raise BalanceError("invalid_response")
            value = Decimal(0)
            found = False
            for key in ("monthlyCredits", "purchasedCredits", "freeCredits"):
                if key in credits:
                    amount = _amount(credits[key])
                    value += amount if amount > 0 else Decimal(0)
                    found = True
            if not found:
                raise BalanceError("invalid_response")
        elif source.result_kind == "ollama":
            purchased = value.get("purchased")
            if not isinstance(purchased, dict) or "balance_usd" not in purchased:
                raise BalanceError("invalid_response")
            value = purchased["balance_usd"]
        else:
            for key in source.balance_path:
                if not isinstance(value, dict) or key not in value:
                    raise BalanceError("invalid_response")
                value = value[key]
        with localcontext() as context:
            context.prec = 64
            amount = _amount(value) / source.divisor
        if source.result_kind == "aihubmix_key" and amount < 0:
            raise BalanceError("provider_error")
        if source.result_kind == "api_key_quota":
            return (
                f"{source.name} API Key 剩余额度\n"
                f"额度：{source.unit} {_format_amount(amount)}\n"
                "来源：自动推导的 API Key 额度接口"
            )
        return (
            f"{source.name} 当前账户余额\n余额：{source.unit} {_format_amount(amount)}\n"
            "来源：管理员配置的账户余额接口"
        )

    async def query(self, argument: str) -> str:
        selected = argument.strip().casefold()
        if not selected:
            targets = (("DeepSeek", self._deepseek), (self.settings.compatible_name, self._compatible))
        elif selected == "deepseek":
            targets = (("DeepSeek", self._deepseek),)
        elif selected in {"兼容", "兼容服务商", self.settings.compatible_name.casefold()}:
            targets = ((self.settings.compatible_name, self._compatible),)
        else:
            return f"用法：{self.settings.command} [DeepSeek 或 兼容]"
        results = []
        for name, query in targets:
            try:
                results.append(await query())
            except BalanceError as error:
                results.append(f"{name}：{ERROR_MESSAGES[error.code]}")
        results.append("查询时间：" + datetime.now().astimezone().isoformat(timespec="seconds"))
        return "\n\n".join(results)

    async def close(self):
        self._closed = True
        if self._session is not None:
            await self._session.close()
