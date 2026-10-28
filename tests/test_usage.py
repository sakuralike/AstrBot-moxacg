import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
import json
from unittest.mock import AsyncMock, patch

from astrbot_plugin_zibll.usage import (
    TokenUsageClient,
    TokenUsageError,
    TokenUsageSettings,
)
from test_foundations import StubEvent, load_main_with_framework_stubs


def usage_config():
    return {
        "token_usage_enabled": True,
        "token_usage_command": "/模型用量",
        "allowed_groups": ["onebot-instance|001|group-1"],
    }


class TokenUsageSettingsTests(unittest.TestCase):
    def test_defaults_and_command_conflicts(self):
        settings = TokenUsageSettings.from_config(usage_config())
        self.assertEqual(settings.command, "/模型用量")
        self.assertEqual(
            settings.scopes, frozenset({("onebot-instance", "001", "group-1")})
        )
        for command in ("/网站资料", "/模型余额", "/自定义"):
            config = {**usage_config(), "token_usage_command": command}
            if command == "/自定义":
                config["balance_command"] = command
            with self.subTest(command=command), self.assertRaises(ValueError):
                TokenUsageSettings.from_config(config)

    def test_schema_defaults(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIs(schema["token_usage_enabled"]["default"], False)
        self.assertEqual(schema["token_usage_command"]["default"], "/模型用量")


class TokenUsageClientTests(unittest.IsolatedAsyncioTestCase):
    def records(self):
        return [
            types.SimpleNamespace(
                agent_type="internal",
                provider_id="provider-a",
                provider_model="model-a",
                token_input_other=10,
                token_input_cached=2,
                token_output=5,
                created_at=datetime.now(timezone.utc),
            ),
            types.SimpleNamespace(
                agent_type="internal",
                provider_id="provider-a",
                provider_model="model-a",
                token_input_other=3,
                token_input_cached=0,
                token_output=4,
                created_at=datetime.now(timezone.utc),
            ),
            types.SimpleNamespace(
                agent_type="internal",
                provider_id="provider-b",
                provider_model=None,
                token_input_other=0,
                token_input_cached=1,
                token_output=0,
                created_at=datetime.now(timezone.utc),
            ),
        ]

    async def test_aggregates_provider_model_tokens(self):
        client = TokenUsageClient(object())
        with patch.object(
            client, "_fetch_records", new=AsyncMock(return_value=self.records())
        ):
            text = await client.query("今天")
        self.assertIn("调用次数：3", text)
        self.assertIn("输入 Tokens：16", text)
        self.assertIn("非缓存输入：13", text)
        self.assertIn("缓存输入：3", text)
        self.assertIn("输出 Tokens：9", text)
        self.assertIn("总 Tokens：25", text)
        self.assertIn("provider-a / model-a：24 Tokens", text)
        self.assertIn("provider-b / 未知模型：1 Tokens", text)

    async def test_empty_and_invalid_periods(self):
        client = TokenUsageClient(object())
        with patch.object(
            client, "_fetch_records", new=AsyncMock(return_value=[])
        ):
            text = await client.query("本月")
        self.assertIn("暂无已记录的内部 Agent 用量", text)
        with self.assertRaises(TokenUsageError) as caught:
            await client.query("昨天")
        self.assertEqual(caught.exception.code, "invalid_argument")

    async def test_closed_client_does_not_query(self):
        client = TokenUsageClient(object())
        await client.close()
        with self.assertRaises(TokenUsageError) as caught:
            await client.query()
        self.assertEqual(caught.exception.code, "client_closed")


class TokenUsageCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_main_with_framework_stubs()
        self.plugin = self.module.ZibllPlugin(object(), usage_config())
        self.plugin.usage_client.query = AsyncMock(
            return_value="AstrBot 模型用量（今天）\n总 Tokens：25"
        )

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def query(self, event):
        return [reply async for reply in self.plugin.usage_command(event)]

    async def test_admin_query_and_period_argument(self):
        event = StubEvent("/模型用量 本月")
        event.admin = True
        replies = await self.query(event)
        self.assertEqual(len(replies), 1)
        self.assertIn("总 Tokens：25", replies[0].text)
        self.plugin.usage_client.query.assert_awaited_once_with("本月")
        self.assertIs(event.llm, False)
        self.assertTrue(event.stopped)

    async def test_non_admin_and_unmatched_scope_do_not_query(self):
        event = StubEvent("/模型用量")
        self.assertEqual(len(await self.query(event)), 1)
        self.assertIn("仅允许 AstrBot 管理员", (await self.query(event))[0].text)
        self.plugin.usage_client.query.assert_not_awaited()
        event.admin = True
        event.group = "other-group"
        self.assertIsNone(self.module.ScopedUsageFilter.match(event))

    async def test_invalid_period_is_redacted(self):
        event = StubEvent("/模型用量 昨天")
        event.admin = True
        self.plugin.usage_client.query.side_effect = TokenUsageError(
            "invalid_argument"
        )
        replies = await self.query(event)
        self.assertIn("用法：/模型用量", replies[0].text)
        self.assertNotIn("invalid_argument", replies[0].text)
