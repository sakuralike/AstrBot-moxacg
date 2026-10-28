import asyncio
import json
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from astrbot_plugin_zibll.client import BridgeError
from test_foundations import At, Plain, Reply, StubEvent, config, load_main_with_framework_stubs


def balance_config():
    return {
        "balance_enabled": True,
        "allowed_groups": ["onebot-instance|001|group-1"],
        "deepseek_api_key": "test-deepseek-balance-key",
    }


class BalanceCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_main_with_framework_stubs()
        self.plugin = self.module.ZibllPlugin(object(), balance_config())
        self.plugin.balance_client.query = AsyncMock(return_value="DeepSeek 当前余额\nCNY 12.34")

    async def asyncTearDown(self):
        await self.plugin.terminate()

    def event(self, text="/模型余额"):
        event = StubEvent(text)
        event.admin = True
        return event

    async def query(self, event):
        return [reply async for reply in self.plugin.balance_command(event)]

    async def replace_plugin(self, values):
        await self.plugin.terminate()
        self.plugin = self.module.ZibllPlugin(object(), values)
        if self.plugin.balance_client is not None:
            self.plugin.balance_client.query = AsyncMock(return_value="CNY 12.34")

    async def test_balance_works_without_website_credentials_or_connection(self):
        self.assertIsNone(self.plugin.client)
        self.assertIsNone(self.plugin.settings)
        await self.plugin.initialize()
        self.assertFalse(self.plugin.connected)
        event = self.event()
        replies = await self.query(event)
        self.assertEqual(len(replies), 1)
        self.assertIn("12.34", replies[0].text)
        self.plugin.balance_client.query.assert_awaited_once_with("")
        self.assertTrue(event.stopped)
        self.assertIs(event.llm, False)
        self.assertTrue(replies[0].stopped)
        self.assertIs(replies[0].use_markdown_, False)

    async def test_invalid_website_config_does_not_disable_balance(self):
        with self.assertLogs("astrbot-foundation-tests", level="ERROR"):
            await self.replace_plugin({**balance_config(), "enabled": True})
        self.assertIsNone(self.plugin.client)
        self.assertEqual(len(await self.query(self.event())), 1)
        self.plugin.balance_client.query.assert_awaited_once()

    async def test_website_failure_does_not_route_balance_through_bridge(self):
        await self.replace_plugin({**config(), **balance_config()})
        self.plugin.client.capabilities = AsyncMock(side_effect=BridgeError("network_error"))
        self.plugin.client.request = AsyncMock()
        with self.assertLogs("astrbot-foundation-tests", level="WARNING"):
            await self.plugin.initialize()
        self.assertFalse(self.plugin.connected)
        self.assertEqual(len(await self.query(self.event("/模型余额 DeepSeek"))), 1)
        self.plugin.balance_client.query.assert_awaited_once_with("DeepSeek")
        self.plugin.client.request.assert_not_awaited()
        self.assertEqual(
            [reply async for reply in self.plugin.website_command(self.event())], [],
        )

    async def test_non_admin_and_uncertain_permissions_never_query(self):
        for permission in (False, None, 1, "admin"):
            with self.subTest(permission=permission):
                event = self.event()
                event.admin = permission
                replies = await self.query(event)
                self.assertIn("仅允许 AstrBot 管理员", replies[0].text)
                self.assertTrue(event.stopped)
                self.assertIs(event.llm, False)
        self.plugin.balance_client.query.assert_not_awaited()

    async def test_permission_error_fails_closed_without_exposing_details(self):
        event = self.event()

        def unavailable():
            raise RuntimeError("private-permission-details")

        event.is_admin = unavailable
        replies = await self.query(event)
        self.assertIn("管理员", replies[0].text)
        self.assertNotIn("private-permission-details", replies[0].text)
        self.plugin.balance_client.query.assert_not_awaited()

    async def test_disallowed_scope_self_private_and_unwoken_messages_do_not_match(self):
        for field, value in (
            ("kind", "private"), ("platform", "other"), ("bot", "other"),
            ("group", "other"), ("sender", "001"), ("sender", None),
            ("is_at_or_wake_command", False),
        ):
            with self.subTest(field=field):
                event = self.event()
                setattr(event, field, value)
                self.assertIsNone(self.module.ScopedBalanceFilter.match(event))
                self.assertEqual(await self.query(event), [])
                self.assertFalse(event.stopped)
                self.assertIsNone(event.llm)
        self.plugin.balance_client.query.assert_not_awaited()

    async def test_text_boundaries_media_and_foreign_mentions_do_not_match(self):
        for messages in (
            [Plain("普通聊天")], [Plain("/模型余额extra")],
            [Plain("前缀 /模型余额")], [Plain("/模型余额\nDeepSeek")],
            [Plain("/模型余额\u200b")], [Plain("/模型余额 " + "x" * 513)],
            [At("other"), Plain("/模型余额")],
            [Plain("/模型余额 <@other>")],
            [Plain('<qqbot-at-user id="other"/> /模型余额')],
            [Reply("message-id"), Plain("/模型余额")],
            [object(), Plain("/模型余额")], [Plain(123)], None, [],
        ):
            with self.subTest(messages=messages):
                event = self.event()
                event.messages = messages
                self.assertIsNone(self.module.ScopedBalanceFilter.match(event))
                self.assertEqual(await self.query(event), [])
        self.plugin.balance_client.query.assert_not_awaited()

    async def test_qq_official_openid_and_scope_alias(self):
        await self.replace_plugin({
            **balance_config(),
            "allowed_groups": ["qq-instance|qq_official|group-openid"],
        })
        event = self.event()
        event.platform_name = "qq_official"
        event.platform = "qq-instance"
        event.bot = "app-id"
        event.group = "group-openid"
        event.sender = "member-openid"
        event.messages = [At("app-id"), Plain("/模型余额 DeepSeek")]
        event.message_obj = types.SimpleNamespace(raw_message=types.SimpleNamespace(
            mentions=[types.SimpleNamespace(id="app-id")],
        ))
        match = self.module.ScopedBalanceFilter.match(event)
        self.assertEqual(match.argument, "DeepSeek")
        self.assertEqual(len(await self.query(event)), 1)
        event.message_obj.raw_message.mentions.append(
            types.SimpleNamespace(id="other-member-openid")
        )
        self.assertIsNone(self.module.ScopedBalanceFilter.match(event))

    async def test_custom_command_and_bot_mention(self):
        await self.replace_plugin({**balance_config(), "balance_command": "/余额"})
        self.assertIsNone(self.module.ScopedBalanceFilter.match(self.event()))
        event = self.event()
        event.messages = [At("001"), Plain("/余额\tDeepSeek")]
        self.assertEqual(len(await self.query(event)), 1)
        self.plugin.balance_client.query.assert_awaited_once_with("DeepSeek")

    async def test_disabled_and_empty_scope_do_not_enable_balance(self):
        for values in (
            {}, {**balance_config(), "balance_enabled": False},
            {**balance_config(), "balance_enabled": "true"},
        ):
            await self.replace_plugin(values)
            self.assertIsNone(self.plugin.balance_client)
            self.assertIsNone(self.module.ScopedBalanceFilter.match(self.event()))
            self.assertEqual(await self.query(self.event()), [])
        with self.assertLogs("astrbot-foundation-tests", level="WARNING"):
            await self.replace_plugin({**balance_config(), "allowed_groups": []})
        self.assertIsNone(self.plugin.balance_client)

    async def test_balance_config_error_does_not_disable_website_commands(self):
        with self.assertLogs("astrbot-foundation-tests", level="ERROR"):
            await self.replace_plugin({
                **config(), **balance_config(), "balance_command": "/网站资料",
            })
        self.assertIsNone(self.plugin.balance_client)
        self.assertIsNotNone(self.plugin.client)
        self.assertEqual(
            self.module.ScopedCommandFilter.match(StubEvent()).action, "profile",
        )
        self.assertIsNone(self.module.ScopedBalanceFilter.match(StubEvent()))

    async def test_cooldown_prevents_repeated_queries(self):
        await self.query(self.event())
        second = await self.query(self.event())
        self.assertIn("频繁", second[0].text)
        self.plugin.balance_client.query.assert_awaited_once()

    async def test_inflight_blocks_queries_even_after_cooldown(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_query(argument):
            started.set()
            await release.wait()
            return "CNY 12.34"

        self.plugin.balance_client.query.side_effect = slow_query
        first = asyncio.create_task(self.query(self.event()))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            self.plugin._balance_cooldown_until = 0
            second = await self.query(self.event())
            self.assertIn("频繁", second[0].text)
            self.plugin.balance_client.query.assert_awaited_once()
        finally:
            release.set()
            await first
        self.assertFalse(self.plugin._balance_inflight)

    async def test_unexpected_query_error_is_redacted_and_releases_inflight(self):
        self.plugin.balance_client.query.side_effect = RuntimeError("secret-balance-value")
        with self.assertLogs("astrbot-foundation-tests", level="WARNING") as captured:
            replies = await self.query(self.event())
        self.assertIn("查询失败", replies[0].text)
        self.assertNotIn("secret-balance-value", replies[0].text + "".join(captured.output))
        self.assertFalse(self.plugin._balance_inflight)

    async def test_old_instance_termination_preserves_new_filter_and_closes_client(self):
        old = self.plugin
        old.balance_client.close = AsyncMock()
        self.plugin = self.module.ZibllPlugin(object(), {
            **balance_config(), "balance_command": "/新余额",
        })
        self.plugin.balance_client.query = AsyncMock(return_value="CNY 12.34")
        old_event = self.event()
        self.assertEqual([reply async for reply in old.balance_command(old_event)], [])
        self.assertFalse(old_event.stopped)
        await old.terminate()
        old.balance_client.close.assert_awaited_once()
        self.assertEqual(len(await self.query(self.event("/新余额"))), 1)
        self.assertIs(self.module.ScopedBalanceFilter.settings, self.plugin.balance_settings)

    async def test_termination_suppresses_pending_reply(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_query(argument):
            started.set()
            await release.wait()
            return "CNY 12.34"

        self.plugin.balance_client.query.side_effect = slow_query
        task = asyncio.create_task(self.query(self.event()))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            await self.plugin.terminate()
        finally:
            release.set()
        self.assertEqual(await task, [])
        self.assertIsNone(self.module.ScopedBalanceFilter.settings)

    def test_schema_keeps_balance_disabled_and_credentials_masked(self):
        schema = json.loads(
            (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIs(schema["balance_enabled"]["default"], False)
        self.assertEqual(schema["balance_command"]["default"], "/模型余额")
        self.assertIs(schema["deepseek_api_key"]["secret"], True)
        compatible = schema["compatible_balance"]["items"]
        self.assertIs(compatible["enabled"]["default"], False)
        self.assertIs(compatible["api_key"]["secret"], True)
        self.assertEqual(compatible["base_url"]["default"], "")
        self.assertEqual(compatible["api_key"]["default"], "")
        self.assertIs(compatible["credential"]["secret"], True)
        self.assertEqual(compatible["url"]["default"], "")
        self.assertEqual(compatible["unit"]["default"], "")
        self.assertEqual(compatible["divisor"]["default"], "1")
