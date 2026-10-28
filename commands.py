import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_COMMANDS = {
    "bind": "/网站绑定",
    "confirm": "/网站绑定确认",
    "unbind": "/网站解绑",
    "profile": "/网站资料",
    "checkin": "/网站签到",
    "latest": "/网站最新",
    "search": "/网站搜索",
    "points_add": "/加积分",
    "points_subtract": "/减积分",
}
ARGUMENT_ACTIONS = frozenset({
    "confirm", "unbind", "search", "points_add", "points_subtract",
})
PRIMARY_ACTIONS = frozenset({
    "bind", "profile", "checkin", "latest", "search",
    "points_add", "points_subtract",
})


def valid_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and not any(c.isspace() or unicodedata.category(c).startswith("C") for c in value)
        and "|" not in value
    )


@dataclass(frozen=True)
class Actor:
    platform_id: str
    bot_self_id: str
    group_id: str
    sender_id: str

    def __post_init__(self):
        if not all(valid_identity(v) for v in self.scope + (self.sender_id,)):
            raise ValueError("事件身份缺失或无效")

    @property
    def scope(self) -> tuple[str, str, str]:
        return self.platform_id, self.bot_self_id, self.group_id


@dataclass(frozen=True)
class CommandMatch:
    action: str
    argument: str
    target_id: str | None = None


class CommandRouter:
    def __init__(self, commands: Mapping[str, str]):
        if (
            not isinstance(commands, Mapping) or not PRIMARY_ACTIONS <= set(commands)
            or set(commands) - set(DEFAULT_COMMANDS)
        ):
            raise ValueError("必须配置五项主指令，确认和解绑仅作为可选兼容入口")
        self.commands = dict(commands)
        for name in self.commands.values():
            if (
                not isinstance(name, str)
                or not 1 <= len(name) <= 32
                or any(c.isspace() or unicodedata.category(c).startswith("C") for c in name)
                or re.fullmatch(r"[0-9]{6}", name)
            ):
                raise ValueError("指令必须为不含空白或控制字符的 1–32 个字符")
        if len(set(self.commands.values())) != len(self.commands):
            raise ValueError("网站指令不能重名")
        self._by_name = {name: action for action, name in self.commands.items()}

    def match(self, text: str) -> CommandMatch | None:
        if not isinstance(text, str) or len(text) > 512:
            return None
        if any(unicodedata.category(c).startswith("C") and c != "\t" for c in text):
            return None
        parts = re.split(r"\s+", text.strip(), maxsplit=1)
        action = self._by_name.get(parts[0])
        if action is None:
            return None
        argument = parts[1].strip() if len(parts) == 2 else ""
        if argument and action not in ARGUMENT_ACTIONS:
            return None
        return CommandMatch(action, argument)


def parse_scopes(values: object) -> frozenset[tuple[str, str, str]]:
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError("允许群必须为列表，最多 100 项")
    scopes = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("群范围格式必须为 平台实例ID|机器人ID|群ID")
        fields = tuple(value.split("|"))
        if len(fields) != 3 or not all(valid_identity(v) for v in fields):
            raise ValueError("群范围格式必须为 平台实例ID|机器人ID|群ID")
        scopes.add(fields)
    return frozenset(scopes)
