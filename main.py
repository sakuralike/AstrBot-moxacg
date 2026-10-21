import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.star import Context, Star

from .client import BridgeClient, BridgeError, Settings
from .commands import Actor, CommandMatch
from .service import ERROR_MESSAGES, execute


class ScopedCommandFilter(filter.CustomFilter):
    settings: Settings | None = None

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
            actor = Actor(
                event.get_platform_id(), event.get_self_id(),
                event.get_group_id(), event.get_sender_id(),
            )
        except (ValueError, TypeError, AttributeError):
            return None
        if actor.scope not in settings.scopes or actor.sender_id == actor.bot_self_id:
            return None
        text = []
        messages = event.get_messages()
        if not isinstance(messages, (list, tuple)):
            return None
        for component in messages:
            if isinstance(component, Plain) and isinstance(component.text, str):
                text.append(component.text)
            elif isinstance(component, At) and str(component.qq) == actor.bot_self_id:
                text.append(" ")
            else:
                return None
        return settings.router.match("".join(text))

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return self.match(event) is not None


class ZibllPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.settings = None
        self.client = None
        self.connected = False
        self._terminated = False
        self._cooldowns = {}
        ScopedCommandFilter.settings = None
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

    async def initialize(self):
        self.connected = False
        if self.client is None or self._terminated:
            return
        try:
            await self.client.capabilities()
        except BridgeError as error:
            logger.warning("AstrBot-Zibll 连接验证失败：%s", error.code)
            return
        if not self._terminated:
            self.connected = True
            logger.info("AstrBot-Zibll 认证连通验证通过。")

    @filter.custom_filter(ScopedCommandFilter)
    async def website_command(self, event: AstrMessageEvent):
        if self._terminated or ScopedCommandFilter.settings is not self.settings:
            return
        match = ScopedCommandFilter.match(event)
        if match is None:
            return
        event.should_call_llm(False)
        event.stop_event()
        if not self.connected:
            yield event.plain_result(
                "网站连接尚未通过验证，请管理员检查配置并重载插件。"
            ).use_markdown(False).stop_event()
            return
        actor = Actor(
            event.get_platform_id(), event.get_self_id(),
            event.get_group_id(), event.get_sender_id(),
        )
        now = time.monotonic()
        self._cooldowns = {key: deadline for key, deadline in self._cooldowns.items() if deadline > now}
        if actor in self._cooldowns or len(self._cooldowns) >= 1000:
            yield event.plain_result("操作过于频繁，请稍后再试。").use_markdown(False).stop_event()
            return
        self._cooldowns[actor] = now + 3
        try:
            replies = await execute(self.client, match, actor, event)
        except BridgeError as error:
            text = ERROR_MESSAGES.get(error.code, "网站响应异常，请管理员检查连接及桥接插件。")
            replies = (text, None)
            logger.warning("AstrBot-Zibll 请求失败：%s", error.code)
        except (TypeError, ValueError, KeyError, OverflowError):
            replies = ("网站返回了无法识别的数据，请管理员检查桥接插件。", None)
            logger.warning("AstrBot-Zibll 请求失败：invalid_response")
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
        if ScopedCommandFilter.settings is self.settings:
            ScopedCommandFilter.settings = None
        self.connected = False
        if self.client is not None:
            await self.client.close()
