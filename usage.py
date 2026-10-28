from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import unicodedata
from collections.abc import Mapping

from .commands import DEFAULT_COMMANDS, parse_scopes


ERROR_MESSAGES = {
    "invalid_argument": "用法：/模型用量 [今天 或 本月]",
    "not_configured": "Tokens 统计未配置允许群。",
    "database_unavailable": "AstrBot Tokens 统计暂不可用。",
    "query_failed": "AstrBot Tokens 统计查询失败，请稍后再试。",
    "client_closed": "Tokens 统计已停止。",
}


class TokenUsageError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _command(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 32
        or any(
            c.isspace() or unicodedata.category(c).startswith("C")
            for c in value
        )
        or re.fullmatch(r"[0-9]{6}", value)
    ):
        raise ValueError("Tokens 指令格式无效")
    return value


@dataclass(frozen=True)
class TokenUsageSettings:
    command: str
    scopes: frozenset[tuple[str, str, str]]

    @classmethod
    def from_config(cls, config: Mapping):
        command = _command(config.get("token_usage_command", "/模型用量"))
        reserved = set(DEFAULT_COMMANDS.values()) | {"/适配器核查"}
        configured_commands = config.get("commands", {})
        if isinstance(configured_commands, Mapping):
            reserved.update(
                value
                for value in configured_commands.values()
                if isinstance(value, str)
            )
        balance_command = config.get("balance_command", "/模型余额")
        if isinstance(balance_command, str):
            reserved.add(balance_command)
        if command in reserved:
            raise ValueError("Tokens 指令不能与其他指令重名")
        return cls(command, parse_scopes(config.get("allowed_groups", [])))


def _period(argument: str) -> tuple[str, datetime, datetime]:
    if not isinstance(argument, str):
        raise TokenUsageError("invalid_argument")
    selected = argument.strip()
    local_now = datetime.now().astimezone()
    if not selected or selected == "今天":
        start_local = local_now.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        label = "今天"
    elif selected == "本月":
        start_local = local_now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        label = "本月"
    else:
        raise TokenUsageError("invalid_argument")
    return (
        label,
        start_local.astimezone(timezone.utc),
        local_now.astimezone(timezone.utc),
    )


def _text(value: object, fallback: str, maximum: int = 96) -> str:
    if not isinstance(value, str):
        return fallback
    value = "".join(
        char
        for char in value.strip()
        if not unicodedata.category(char).startswith("C")
    )
    if not value:
        return fallback
    return value[:maximum]


def _nonnegative_int(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0


class TokenUsageClient:
    def __init__(self, context):
        self.context = context
        self._closed = False

    async def _fetch_records(self, start_utc: datetime, end_utc: datetime):
        if self._closed:
            raise TokenUsageError("client_closed")
        try:
            from sqlalchemy import select
            from astrbot.core.db.po import ProviderStat
        except (ImportError, ModuleNotFoundError):
            raise TokenUsageError("database_unavailable") from None

        try:
            db = self.context.get_db()
            # AstrBot v4.29.3 stores ProviderStat.created_at as a naive UTC
            # DateTime, so bind UTC wall-clock values without tzinfo.
            start_value = start_utc.astimezone(timezone.utc).replace(tzinfo=None)
            end_value = end_utc.astimezone(timezone.utc).replace(tzinfo=None)
            async with db.get_db() as session:
                result = await session.execute(
                    select(ProviderStat)
                    .where(
                        ProviderStat.agent_type == "internal",
                        ProviderStat.created_at >= start_value,
                        ProviderStat.created_at < end_value,
                    )
                    .order_by(ProviderStat.created_at.asc())
                )
                return list(result.scalars().all())
        except TokenUsageError:
            raise
        except (AttributeError, TypeError, ValueError, KeyError):
            raise TokenUsageError("database_unavailable") from None
        except Exception:
            raise TokenUsageError("query_failed") from None

    async def query(self, argument: str = "") -> str:
        label, start_utc, end_utc = _period(argument)
        records = await self._fetch_records(start_utc, end_utc)
        grouped: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: {
                "calls": 0,
                "input_other": 0,
                "input_cached": 0,
                "output": 0,
            }
        )
        for record in records:
            key = (
                _text(getattr(record, "provider_id", None), "未知供应商"),
                _text(getattr(record, "provider_model", None), "未知模型"),
            )
            item = grouped[key]
            item["calls"] += 1
            item["input_other"] += _nonnegative_int(
                getattr(record, "token_input_other", 0)
            )
            item["input_cached"] += _nonnegative_int(
                getattr(record, "token_input_cached", 0)
            )
            item["output"] += _nonnegative_int(
                getattr(record, "token_output", 0)
            )

        input_other = sum(item["input_other"] for item in grouped.values())
        input_cached = sum(item["input_cached"] for item in grouped.values())
        output = sum(item["output"] for item in grouped.values())
        total = input_other + input_cached + output
        calls = sum(item["calls"] for item in grouped.values())
        lines = [
            f"AstrBot 模型用量（{label}）",
            f"调用次数：{calls}",
            f"输入 Tokens：{input_other + input_cached}",
            f"  非缓存输入：{input_other}",
            f"  缓存输入：{input_cached}",
            f"输出 Tokens：{output}",
            f"总 Tokens：{total}",
        ]
        if not grouped:
            lines.append("暂无已记录的内部 Agent 用量。")
            return "\n".join(lines)

        lines.append("按供应商/模型：")
        ordered = sorted(
            grouped.items(),
            key=lambda item: (
                item[1]["input_other"]
                + item[1]["input_cached"]
                + item[1]["output"],
                item[0],
            ),
            reverse=True,
        )
        for (provider_id, provider_model), item in ordered:
            item_total = (
                item["input_other"] + item["input_cached"] + item["output"]
            )
            lines.append(
                f"- {provider_id} / {provider_model}："
                f"{item_total} Tokens（调用 {item['calls']} 次，"
                f"输入 {item['input_other'] + item['input_cached']}，"
                f"输出 {item['output']}）"
            )
        return "\n".join(lines)

    async def close(self):
        self._closed = True
