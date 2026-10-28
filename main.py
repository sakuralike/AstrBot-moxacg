import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Mapping

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.star import Context, Star

from .balance import BalanceClient, BalanceSettings
from .client import BridgeClient, BridgeError, Settings
from .commands import Actor, CommandMatch, valid_identity
from .service import ERROR_MESSAGES, binding_event_identity, execute

ADAPTER_PROBE_COMMAND = "/适配器核查"
PROBE_ID_FIELDS = {
    "id", "qq", "user_id", "sender_id", "target_id", "member_openid",
    "user_openid", "openid", "group_id", "group_openid", "self_id",
}
PROBE_OMIT_FIELDS = {
    "api", "attachments", "avatar", "cookie", "icon", "secret",
    "token", "url", "username", "authorization", "signature",
}
PROBE_SENSITIVE_PARTS = (
    "token", "secret", "pass" + "word", "cookie", "authorization", "signature",
)
PROBE_OBJECT_FIELDS = {
    "author", "bot", "content", "group_id", "group_openid", "id", "is_you",
    "member_openid", "message", "message_type", "mentions", "msg_elements",
    "raw_data", "raw_message", "self_id", "sender", "type", "user_openid",
}
MENTION_ID_PATTERNS = (
    re.compile(r"<@!?([^>\s]+)>"),
    re.compile(r'<qqbot-at-user\s+id=["\']([^"\']+)["\']'),
)
POINT_ACTIONS = frozenset({"points_add", "points_subtract"})
POINTS_OPERATOR_REQUIRED = "未配置为积分操作员，无法执行积分操作。"
POINTS_UNAVAILABLE = "网站积分管理功能当前不可用，请检查网站桥接插件和积分设置。"


def _probe_id(value: object) -> dict:
    return {
        "type": type(value).__name__,
        "sha256_12": hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12],
    }


def _probe_fields(value: object) -> set[str]:
    names = set()
    try:
        names.update(vars(value))
    except TypeError:
        pass
    for item in type(value).__mro__:
        slots = getattr(item, "__slots__", ())
        if isinstance(slots, str):
            names.add(slots)
        else:
            names.update(name for name in slots if isinstance(name, str))
    return names


def _probe_target_count(event: AstrMessageEvent, messages: list) -> int:
    target_ids = []
    self_id = str(event.get_self_id())
    for component in messages:
        if isinstance(component, At):
            mention_id = str(component.qq)
            if mention_id != self_id:
                target_ids.append(mention_id)
        elif isinstance(component, Plain) and isinstance(component.text, str):
            for pattern in MENTION_ID_PATTERNS:
                target_ids.extend(
                    mention_id
                    for mention_id in pattern.findall(component.text)
                    if mention_id != self_id
                )
    if target_ids:
        return len(target_ids)
    raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
    mentions = getattr(raw_message, "mentions", None)
    if isinstance(mentions, (list, tuple)):
        return sum(
            1
            for mention in mentions
            if str(getattr(mention, "id", "")) not in {"", self_id}
        )
    return 0


def _field(value: object, name: str):
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _raw_mention_ids(event: AstrMessageEvent) -> list[str]:
    raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
    mentions = _field(raw_message, "mentions")
    if not isinstance(mentions, (list, tuple)) or not mentions:
        mentions = _field(_field(raw_message, "raw_data"), "mentions")
    if not isinstance(mentions, (list, tuple)):
        return []
    result = []
    for mention in mentions:
        value = next(
            (
                _field(mention, name)
                for name in ("id", "qq", "user_id", "member_openid", "user_openid")
                if _field(mention, name) is not None
            ),
            None,
        )
        if value is not None and valid_identity(str(value)):
            result.append(str(value))
    return result


def _clean_plain_mentions(text: str) -> tuple[str, list[str]]:
    ids = []
    cleaned = text
    for pattern in MENTION_ID_PATTERNS:
        ids.extend(pattern.findall(cleaned))
        cleaned = pattern.sub(" ", cleaned)
    return cleaned, [str(value) for value in ids if valid_identity(str(value))]


def _target_ids(event: AstrMessageEvent, messages: list, self_id: str) -> list[str]:
    component_ids = []
    for component in messages:
        if isinstance(component, At):
            raw_value = getattr(component, "qq", None)
            value = str(raw_value) if raw_value is not None else ""
            if value and value != self_id and value.lower() != "all" and valid_identity(value):
                component_ids.append(value)
    if event.get_platform_name() != "qq_official" or component_ids:
        return component_ids

    raw_ids = _raw_mention_ids(event)
    raw_targets = {
        value for value in raw_ids
        if value != self_id and value.lower() != "all"
    }
    ids = []
    for component in messages:
        if isinstance(component, Plain) and isinstance(component.text, str):
            _, embedded = _clean_plain_mentions(component.text)
            ids.extend(value for value in embedded if value in raw_targets)
    return ids


def _probe_value(value: object, field: str = "", depth: int = 0):
    key = field.lower()
    if any(item in key for item in PROBE_SENSITIVE_PARTS):
        return "<omitted>"
    if key in PROBE_OMIT_FIELDS:
        return "<omitted>"
    if key in PROBE_ID_FIELDS and value is not None:
        return _probe_id(value)
    if depth >= 5:
        return {"type": type(value).__name__}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if key in {"content", "text", "message_str"}:
            return {
                "length": len(value),
                "mention_ids": [
                    _probe_id(match.group(1))
                    for pattern in MENTION_ID_PATTERNS
                    for match in pattern.finditer(value)
                ][:20],
            }
        return {"type": "str", "length": len(value)}
    if isinstance(value, bytes):
        return {"type": "bytes", "length": len(value)}
    if isinstance(value, Mapping):
        return {
            str(name): _probe_value(item, str(name), depth + 1)
            for name, item in list(value.items())[:40]
        }
    if isinstance(value, (list, tuple, set)):
        return [_probe_value(item, field, depth + 1) for item in list(value)[:20]]
    available = sorted(
        name for name in _probe_fields(value)
        if not name.startswith("_") and not any(
            item in name.lower()
            for item in PROBE_SENSITIVE_PARTS
        )
    )
    selected = {
        name: _probe_value(getattr(value, name), name, depth + 1)
        for name in available
        if name in PROBE_OBJECT_FIELDS
    }
    return {"type": type(value).__name__, "fields": available[:80], "values": selected}


def _adapter_probe_report(event: AstrMessageEvent) -> dict:
    message_obj = getattr(event, "message_obj", None)
    raw_message = getattr(message_obj, "raw_message", None)
    messages = event.get_messages()
    components = []
    for component in messages if isinstance(messages, (list, tuple)) else []:
        if isinstance(component, Plain):
            components.append({
                "type": "Plain",
                "text": _probe_value(component.text, "text"),
            })
        elif isinstance(component, At):
            components.append({"type": "At", "qq": _probe_id(component.qq)})
        else:
            components.append({"type": type(component).__name__})
    raw_fields = {
        name: _probe_value(getattr(raw_message, name), name)
        for name in (
            "mentions", "msg_elements", "raw_data", "message_type", "content",
            "author", "group_openid", "id",
        )
        if raw_message is not None and hasattr(raw_message, name)
    }
    return {
        "platform_name": event.get_platform_name(),
        "scope": {
            "platform_id": _probe_id(event.get_platform_id()),
            "bot_self_id": _probe_id(event.get_self_id()),
            "group_id": _probe_id(event.get_group_id()),
            "sender_id": _probe_id(event.get_sender_id()),
        },
        "message_type": str(getattr(message_obj, "type", "")),
        "event_messages": components,
        "raw_message_type": type(raw_message).__name__ if raw_message is not None else None,
        "raw_message_fields": [
            name for name in sorted(_probe_fields(raw_message))
            if not any(item in name.lower() for item in PROBE_SENSITIVE_PARTS)
        ][:80] if raw_message is not None else [],
        "raw_message": raw_fields,
    }


def event_actor(event: AstrMessageEvent, settings: Settings | BalanceSettings) -> Actor:
    actor = Actor(
        event.get_platform_id(), event.get_self_id(),
        event.get_group_id(), event.get_sender_id(),
    )
    scope = (actor.platform_id, "qq_official", actor.group_id)
    if event.get_platform_name() == "qq_official" and scope in settings.scopes:
        return Actor(*scope, actor.sender_id)
    return actor


class ScopedBalanceFilter(filter.CustomFilter):
    settings: BalanceSettings | None = None

    @classmethod
    def match(cls, event: AstrMessageEvent) -> CommandMatch | None:
        settings = cls.settings
        if settings is None or not event.is_at_or_wake_command:
            return None
        if not filter.EventMessageTypeFilter(
            filter.EventMessageType.GROUP_MESSAGE
        ).filter(event, {}):
            return None
        try:
            actor = event_actor(event, settings)
        except (ValueError, TypeError, AttributeError):
            return None
        if actor.scope not in settings.scopes or actor.sender_id in {
            actor.bot_self_id, event.get_self_id(),
        }:
            return None
        messages = event.get_messages()
        if not isinstance(messages, (list, tuple)):
            return None
        text = []
        self_id = str(event.get_self_id())
        for component in messages:
            if isinstance(component, Plain) and isinstance(component.text, str):
                cleaned, mention_ids = _clean_plain_mentions(component.text)
                if any(value != self_id for value in mention_ids):
                    return None
                text.append(cleaned)
            elif isinstance(component, At) and str(component.qq) == self_id:
                text.append(" ")
            else:
                return None
        if any(value != self_id for value in _raw_mention_ids(event)):
            return None
        value = "".join(text)
        if len(value) > 512 or any(
            unicodedata.category(c).startswith("C") and c != "\t" for c in value
        ):
            return None
        parts = value.strip().split(maxsplit=1)
        if not parts or parts[0] != settings.command:
            return None
        return CommandMatch("balance", parts[1].strip() if len(parts) == 2 else "")

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return self.match(event) is not None


class ScopedCommandFilter(filter.CustomFilter):
    settings: Settings | None = None
    pending = {}
    processed = {}
    code_ready = False
    points_ready = False

    @classmethod
    def match(cls, event: AstrMessageEvent) -> CommandMatch | None:
        settings = cls.settings
        if settings is None or not event.is_at_or_wake_command:
            return None
        if not filter.EventMessageTypeFilter(
            filter.EventMessageType.GROUP_MESSAGE
        ).filter(event, {}):
            return None
        try:
            actor = event_actor(event, settings)
        except (ValueError, TypeError, AttributeError):
            return None
        if actor.scope not in settings.scopes or actor.sender_id in {
            actor.bot_self_id, event.get_self_id(),
        }:
            return None
        text = []
        mentioned = False
        messages = event.get_messages()
        if not isinstance(messages, (list, tuple)):
            return None
        if event.get_platform_name() == "qq_official":
            probe_text = []
            bot_mentioned = False
            probe_safe = True
            for component in messages:
                if isinstance(component, Plain) and isinstance(component.text, str):
                    probe_text.append(component.text)
                elif isinstance(component, At):
                    bot_mentioned = bot_mentioned or str(component.qq) == event.get_self_id()
                else:
                    probe_safe = False
                    break
            parts = "".join(probe_text).strip().split(maxsplit=1)
            target_count = _probe_target_count(event, messages)
            if (
                probe_safe and bot_mentioned and parts
                and parts[0] == ADAPTER_PROBE_COMMAND
                and target_count in {0, 1}
            ):
                return CommandMatch("adapter_probe", str(target_count))
        target_ids = _target_ids(event, messages, str(event.get_self_id()))
        for component in messages:
            if isinstance(component, Plain) and isinstance(component.text, str):
                cleaned, _ = _clean_plain_mentions(component.text)
                text.append(cleaned)
            elif isinstance(component, At) and str(component.qq) == event.get_self_id():
                text.append(" ")
                mentioned = True
            elif isinstance(component, At):
                text.append(" ")
            else:
                return None
        value = "".join(text)
        match = settings.router.match(value)
        if match is not None:
            if match.action in POINT_ACTIONS:
                target_id = target_ids[0] if len(target_ids) == 1 else None
                return CommandMatch(match.action, match.argument, target_id)
            if target_ids:
                return None
            return match
        if (
            target_ids
            or not cls.code_ready
            or not mentioned
            or not re.fullmatch(r"[0-9]{6}", value.strip())
        ):
            return None
        pending = cls.pending.get(actor)
        message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        processed = cls.processed.get((actor, str(message_id))) if message_id is not None else None
        now = time.monotonic()
        if (pending and pending[1] > now) or (processed and processed[1] > now):
            return CommandMatch("binding_code", value.strip())
        return None

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return self.match(event) is not None


class ZibllPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.settings = None
        self.client = None
        self.balance_settings = None
        self.balance_client = None
        self.connected = False
        self._terminated = False
        self._balance_inflight = False
        self._balance_cooldown_until = 0.0
        self._cooldowns = {}
        self._processed = {}
        self._inflight = set()
        ScopedCommandFilter.settings = None
        ScopedCommandFilter.pending = {}
        ScopedCommandFilter.processed = {}
        ScopedCommandFilter.code_ready = False
        ScopedCommandFilter.points_ready = False
        ScopedBalanceFilter.settings = None
        if config.get("balance_enabled", False) is True:
            try:
                balance_settings = BalanceSettings.from_config(config)
            except ValueError:
                logger.error("模型余额配置无效，请检查余额指令、允许群及超时配置。")
            else:
                if balance_settings.scopes:
                    self.balance_settings = balance_settings
                    self.balance_client = BalanceClient(balance_settings)
                    ScopedBalanceFilter.settings = balance_settings
                else:
                    logger.warning("模型余额未配置允许群，查询保持关闭。")
        if config.get("enabled", False) is not True:
            return
        try:
            self.settings = Settings.from_config(config)
        except ValueError as error:
            logger.error("AstrBot-Zibll 配置无效：%s", error)
            return
        if not self.settings.scopes:
            logger.warning("AstrBot-Zibll 未配置允许群，业务保持关闭。")
            return
        self.client = BridgeClient(self.settings)
        ScopedCommandFilter.settings = self.settings
        ScopedCommandFilter.pending = self.client.pending_bindings
        ScopedCommandFilter.processed = self._processed

    async def initialize(self):
        self.connected = False
        if ScopedCommandFilter.settings is self.settings:
            ScopedCommandFilter.code_ready = False
            ScopedCommandFilter.points_ready = False
        if self.client is None or self._terminated:
            return
        try:
            await self.client.capabilities()
        except BridgeError as error:
            logger.warning("AstrBot-Zibll 连接验证失败：%s", error.code)
            return
        if not self._terminated and ScopedCommandFilter.settings is self.settings:
            self.connected = True
            ScopedCommandFilter.code_ready = self.client.binding_code_available
            ScopedCommandFilter.points_ready = self.client.points_management_available
            logger.info("AstrBot-Zibll 认证连通验证通过。")

    @filter.custom_filter(ScopedBalanceFilter, priority=100)
    async def balance_command(self, event: AstrMessageEvent):
        if (
            self._terminated or self.balance_client is None
            or ScopedBalanceFilter.settings is not self.balance_settings
        ):
            return
        match = ScopedBalanceFilter.match(event)
        if match is None:
            return
        event.should_call_llm(False)
        event.stop_event()
        try:
            is_admin = event.is_admin() is True
        except Exception:
            is_admin = False
        if not is_admin:
            yield event.plain_result(
                "模型余额仅允许 AstrBot 管理员查询。"
            ).use_markdown(False).stop_event()
            return
        now = time.monotonic()
        if self._balance_inflight or now < self._balance_cooldown_until:
            yield event.plain_result(
                "操作过于频繁，请稍后再试。"
            ).use_markdown(False).stop_event()
            return
        self._balance_cooldown_until = now + 3
        self._balance_inflight = True
        try:
            text = await self.balance_client.query(match.argument)
        except Exception:
            text = "余额查询失败，请检查余额配置或稍后重试。"
            logger.warning("模型余额查询失败。")
        finally:
            self._balance_inflight = False
        if self._terminated or ScopedBalanceFilter.settings is not self.balance_settings:
            return
        yield event.plain_result(text).use_markdown(False).stop_event()

    @filter.custom_filter(ScopedCommandFilter, priority=100)
    async def website_command(self, event: AstrMessageEvent):
        if self._terminated or ScopedCommandFilter.settings is not self.settings:
            return
        match = ScopedCommandFilter.match(event)
        if match is None:
            return
        event.should_call_llm(False)
        event.stop_event()
        if match.action == "adapter_probe":
            if match.argument == "1":
                try:
                    is_admin = event.is_admin()
                except (AttributeError, TypeError, ValueError):
                    is_admin = False
                if not is_admin:
                    yield event.plain_result(
                        "带目标成员的适配器核查仅允许 AstrBot 管理员执行。"
                    ).use_markdown(False).stop_event()
                    return
            try:
                report = _adapter_probe_report(event)
                logger.info(
                    "QQ Official 适配器核查（ID 已哈希，文本/头像/附件已省略）：%s",
                    json.dumps(report, ensure_ascii=False, separators=(",", ":")),
                )
            except (AttributeError, TypeError, ValueError, RecursionError):
                logger.warning("QQ Official 适配器核查：事件结构读取失败。")
            yield event.plain_result(
                "适配器核查信息已记录到 AstrBot 日志。管理员请发送 @机器人 /适配器核查 @目标群员；"
                "目标群员再单独发送 @机器人 /适配器核查，对照两条日志中的 ID 哈希。"
            ).use_markdown(False).stop_event()
            return
        if not self.connected:
            yield event.plain_result(
                "网站连接尚未通过验证，请管理员检查配置并重载插件。"
            ).use_markdown(False).stop_event()
            return
        if match.action in POINT_ACTIONS:
            actor = event_actor(event, self.settings)
            if actor.sender_id not in self.settings.points_operator_ids:
                yield event.plain_result(POINTS_OPERATOR_REQUIRED).use_markdown(False).stop_event()
                return
            target_ids = _target_ids(
                event, event.get_messages(), str(event.get_self_id()),
            )
            if len(target_ids) != 1 or match.target_id != target_ids[0]:
                command = self.settings.router.commands[match.action]
                yield event.plain_result(
                    f"用法：{command} 正整数 @群成员"
                ).use_markdown(False).stop_event()
                return
            if not self.client.points_management_available:
                yield event.plain_result(POINTS_UNAVAILABLE).use_markdown(False).stop_event()
                return
        actor = event_actor(event, self.settings)
        now = time.monotonic()
        for key, value in list(self._processed.items()):
            if value[1] <= now:
                del self._processed[key]
        for key, value in list(self.client.pending_bindings.items()):
            if value[1] <= now:
                del self.client.pending_bindings[key]
        if match.action in {"bind", "binding_code"}:
            try:
                identity = binding_event_identity(event, actor, "binding/" + match.action)
            except BridgeError as error:
                yield event.plain_result(ERROR_MESSAGES[error.code]).use_markdown(False).stop_event()
                return
            key = (actor, str(event.message_obj.message_id))
            fingerprint = hashlib.sha256(
                (match.action + ":" + match.argument + ":" + str(identity["event_time"])).encode()
            ).hexdigest()
            previous = self._processed.get(key)
            if previous:
                if previous[0] != fingerprint:
                    yield event.plain_result(ERROR_MESSAGES["request_conflict"]).use_markdown(False).stop_event()
                return
            if len(self._processed) >= 1000 or len(self.client.pending_bindings) >= 1000:
                yield event.plain_result(ERROR_MESSAGES["rate_limited"]).use_markdown(False).stop_event()
                return
            self._processed[key] = (fingerprint, now + 180)
        self._cooldowns = {key: deadline for key, deadline in self._cooldowns.items() if deadline > now}
        if actor in self._inflight or actor in self._cooldowns or len(self._cooldowns) >= 1000:
            yield event.plain_result("操作过于频繁，请稍后再试。").use_markdown(False).stop_event()
            return
        self._cooldowns[actor] = now + 3
        self._inflight.add(actor)
        try:
            replies = await execute(self.client, match, actor, event)
        except BridgeError as error:
            text = ERROR_MESSAGES.get(error.code, "网站响应异常，请管理员检查连接及桥接插件。")
            replies = (text, None)
            logger.warning("AstrBot-Zibll 请求失败：%s", error.code)
        except (TypeError, ValueError, KeyError, OverflowError):
            replies = ("网站返回了无法识别的数据，请管理员检查桥接插件。", None)
            logger.warning("AstrBot-Zibll 请求失败：invalid_response")
        finally:
            self._inflight.discard(actor)
        if self._terminated:
            self.client.pending_bindings.clear()
            return
        if isinstance(replies, tuple):
            replies = [replies]
        for text, image in replies:
            if image is not None:
                yield event.chain_result([Image.fromBytes(image), Plain(text)]).stop_event()
            else:
                yield event.plain_result(text).use_markdown(False).stop_event()

    @filter.on_decorating_result()
    async def qq_pc_compatible_reply(self, event: AstrMessageEvent):
        if self._terminated or event.get_platform_name() != "qq_official":
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        if all(isinstance(component, (Plain, At, Reply)) for component in result.chain):
            result.use_markdown(False)

    async def terminate(self):
        self._terminated = True
        if ScopedBalanceFilter.settings is self.balance_settings:
            ScopedBalanceFilter.settings = None
        if ScopedCommandFilter.settings is self.settings:
            ScopedCommandFilter.settings = None
            ScopedCommandFilter.pending = {}
            ScopedCommandFilter.processed = {}
            ScopedCommandFilter.code_ready = False
            ScopedCommandFilter.points_ready = False
        self._processed.clear()
        self.connected = False
        if self.client is not None:
            await self.client.close()
        if self.balance_client is not None:
            await self.balance_client.close()
