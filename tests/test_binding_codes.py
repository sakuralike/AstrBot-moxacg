import asyncio
import hashlib
import json
import time
import types
import unittest
from unittest.mock import AsyncMock

from astrbot_plugin_zibll.client import BridgeError, PROTOCOL
from astrbot_plugin_zibll.commands import Actor, CommandMatch, CommandRouter, DEFAULT_COMMANDS
from astrbot_plugin_zibll.service import event_identity, execute
from test_foundations import (
    At, Plain, Reply, StubEvent, config, load_main_with_framework_stubs,
)
import test_foundations as foundations


class CodeServiceTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = foundations.ClientTests.asyncTearDown
    run_command = foundations.ClientTests.run_command

    async def asyncSetUp(self):
        await foundations.ClientTests.asyncSetUp(self)
        self.client.binding_code_available = True
        self.actor = Actor("onebot-instance", "001", "group-1", "user-1")
        self.event = StubEvent("000001")
        self.event.message_obj = types.SimpleNamespace(message_id="code-1", timestamp=int(time.time()))
        self.client.pending_bindings[self.actor] = ("a" * 64, time.monotonic() + 600)

    async def code(self, data):
        self.payload = {"code": "ok", "data": data}
        return await execute(self.client, CommandMatch("binding_code", "000001"), self.actor, self.event)

    async def test_code_string_and_success_cleanup(self):
        text, _ = await self.code({"state": "bound", "uid": 123, "nickname": "Member"})
        request = json.loads(self.requests[-1].raw)
        self.assertEqual(request["code"], "000001")
        self.assertEqual(request["session_id"], "a" * 64)
        self.assertNotIn("000001", text)
        self.assertIn("?az_bindings=1", text)
        self.assertNotIn(self.actor, self.client.pending_bindings)
        self.assertNotEqual(request["request_id"], event_identity(self.event, self.actor)["request_id"])

    async def test_wrong_code_keeps_pending_but_lock_clears(self):
        text, _ = await self.code({"state": "code_invalid"})
        self.assertIn("无效", text)
        self.assertIn(self.actor, self.client.pending_bindings)
        text, _ = await self.code({"state": "too_many_attempts"})
        self.assertIn("上限", text)
        self.assertNotIn(self.actor, self.client.pending_bindings)

    async def test_expired_or_missing_pending_never_requests(self):
        self.client.pending_bindings[self.actor] = ("a" * 64, time.monotonic() - 1)
        with self.assertRaises(BridgeError) as error:
            await self.code({})
        self.assertEqual(error.exception.code, "session_expired")
        self.assertEqual(self.requests, [])

    async def test_lost_redeem_queries_status_once_without_code(self):
        request = AsyncMock(side_effect=[
            BridgeError("network_error"), {"state": "bound", "uid": 123, "nickname": "Member"},
        ])
        self.client.request = request
        text, _ = await self.code({})
        self.assertIn("绑定成功", text)
        self.assertEqual([call.args[0] for call in request.await_args_list], [
            "/binding/code/redeem", "/binding/code/status",
        ])
        self.assertNotIn("code", request.await_args_list[1].args[1])

    async def test_unknown_result_does_not_redeem_again(self):
        self.client.request = AsyncMock(side_effect=[BridgeError("network_error"), BridgeError("network_error")])
        text, _ = await self.code({})
        self.assertIn("暂未确认", text)
        self.assertEqual(self.client.request.await_count, 2)
        self.assertIn(self.actor, self.client.pending_bindings)

    async def test_old_site_blocks_only_new_binding(self):
        self.client.binding_code_available = False
        with self.assertRaises(BridgeError) as error:
            await execute(self.client, CommandMatch("bind", ""), self.actor, self.event)
        self.assertEqual(error.exception.code, "binding_upgrade_required")
        self.assertEqual(self.requests, [])
        text, _ = await self.run_command("/网站最新", {"items": []})
        self.assertIn("未找到", text)

    async def test_capability_requires_flag_and_all_new_routes(self):
        base = {"protocol": PROTOCOL, "public_site_url": self.base}
        for data, expected in (
            (base, False),
            ({**base, "binding_code": True, "actions": ["binding/code/start"]}, False),
            ({**base, "binding_code": True, "actions": [
                "binding/code/start", "binding/code/redeem", "binding/code/status",
            ]}, True),
        ):
            self.payload = {"code": "ok", "data": data}
            await self.client.capabilities()
            self.assertIs(self.client.binding_code_available, expected)

    async def test_old_duplicate_start_does_not_drop_current_pending(self):
        pending = self.client.pending_bindings[self.actor]
        self.client.request = AsyncMock(return_value={"state": "already_started"})
        text, _ = await execute(self.client, CommandMatch("bind", ""), self.actor, self.event)
        self.assertIn("已处理", text)
        self.assertEqual(self.client.pending_bindings[self.actor], pending)
        self.client.request.side_effect = BridgeError("network_error")
        with self.assertRaises(BridgeError):
            await execute(self.client, CommandMatch("bind", ""), self.actor, self.event)
        self.assertNotIn(self.actor, self.client.pending_bindings)


class CodeFilterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_main_with_framework_stubs()
        self.plugin = self.module.ZibllPlugin(object(), config())
        self.plugin.connected = True
        self.plugin.client.binding_code_available = True
        self.module.ScopedCommandFilter.code_ready = True
        self.actor = Actor("onebot-instance", "001", "group-1", "user-1")

    async def asyncTearDown(self):
        await self.plugin.terminate()

    def event(self, text="000001", message_id="code-1"):
        event = StubEvent(text)
        event.messages = [At("001"), Plain(text)]
        event.message_obj = types.SimpleNamespace(message_id=message_id, timestamp=int(time.time()))
        return event

    def pending(self):
        self.plugin.client.pending_bindings[self.actor] = ("a" * 64, time.monotonic() + 600)

    async def use_qq_official(self, scopes=None):
        await self.plugin.terminate()
        self.plugin = self.module.ZibllPlugin(object(), {
            **config(), "allowed_groups": scopes or ["qq-instance|qq_official|group-1"],
        })
        self.plugin.connected = True
        self.plugin.client.binding_code_available = True
        self.module.ScopedCommandFilter.code_ready = True
        self.actor = Actor("qq-instance", "qq_official", "group-1", "user-1")

    def qq_event(self, text="000001", message_id="code-1", bot="bot-openid"):
        event = self.event(text, message_id)
        event.platform_name = "qq_official"
        event.platform = "qq-instance"
        event.bot = bot
        event.messages = [At(bot), Plain(text)]
        return event

    async def test_qq_official_bind_then_mentioned_code_uses_same_actor(self):
        await self.use_qq_official()
        self.plugin.client.request = AsyncMock(side_effect=[
            {
                "state": "pending",
                "session_id": hashlib.sha256(("b" * 64).encode()).hexdigest(),
                "expires_in": 600,
                "url": "https://example.com/?az_bind=" + "b" * 64,
            },
            {"state": "bound", "uid": 1, "nickname": "Member"},
        ])
        start = self.qq_event("/网站绑定", "start-1", bot="qq_official")
        start.messages = [Plain("/网站绑定")]
        self.assertEqual(len([r async for r in self.plugin.website_command(start)]), 1)
        self.plugin._cooldowns.clear()
        code = self.qq_event()
        self.assertTrue(self.module.ScopedCommandFilter().filter(code, {}))
        results = [r async for r in self.plugin.website_command(code)]
        self.assertEqual(len(results), 1)
        self.assertIn("绑定成功", results[0].text)
        self.assertTrue(code.stopped)
        self.assertFalse(code.llm)
        requests = self.plugin.client.request.await_args_list
        self.assertEqual([call.args[0] for call in requests], [
            "/binding/code/start", "/binding/code/redeem",
        ])
        self.assertEqual(requests[0].args[1]["actor"], requests[1].args[1]["actor"])
        self.assertEqual(requests[1].args[1]["actor"]["bot_self_id"], "qq_official")
        self.assertFalse(self.plugin.client.pending_bindings)
        duplicate = self.qq_event()
        duplicate.message_obj.timestamp = code.message_obj.timestamp
        self.assertEqual([r async for r in self.plugin.website_command(duplicate)], [])
        self.assertTrue(duplicate.stopped)
        self.assertFalse(duplicate.llm)
        self.assertEqual(self.plugin.client.request.await_count, 2)

    async def test_qq_official_code_keeps_scope_and_sender_isolation(self):
        await self.use_qq_official([
            "qq-instance|qq_official|group-1", "qq-instance|qq_official|group-2",
            "second-qq-instance|qq_official|group-1",
        ])
        self.pending()
        self.plugin.client.request = AsyncMock()
        for field, value in (
            ("platform", "second-qq-instance"), ("platform", "unlisted-instance"),
            ("group", "group-2"), ("group", "unlisted-group"), ("sender", "user-2"),
            ("sender", "bot-openid"), ("sender", "qq_official"), ("bot", "other-bot"),
            ("bot", None), ("platform_name", "aiocqhttp"), ("kind", "private"),
        ):
            with self.subTest(field=field, value=value):
                event = self.qq_event()
                setattr(event, field, value)
                self.assertEqual([r async for r in self.plugin.website_command(event)], [])
                self.assertFalse(event.stopped)
                self.assertIsNone(event.llm)
        for components in (
            [Plain("000001")], [At("other"), Plain("000001")],
            [At("qq_official"), Plain("000001")],
            [At("bot-openid"), At("other"), Plain("000001")],
            [At("bot-openid"), Reply("m"), Plain("000001")],
        ):
            event = self.qq_event()
            event.messages = components
            self.assertIsNone(self.module.ScopedCommandFilter.match(event))
        self.plugin.client.request.assert_not_awaited()

    async def test_qq_official_no_active_pending_leaves_numeric_chat_untouched(self):
        await self.use_qq_official()
        for deadline in (None, time.monotonic() - 1):
            if deadline is not None:
                self.plugin.client.pending_bindings[self.actor] = ("a" * 64, deadline)
            event = self.qq_event()
            self.assertEqual([r async for r in self.plugin.website_command(event)], [])
            self.assertFalse(event.stopped)
            self.assertIsNone(event.llm)

    async def test_qq_official_explicit_bot_scope_is_not_widened(self):
        await self.use_qq_official(["qq-instance|bot-openid|group-1"])
        actor = Actor("qq-instance", "bot-openid", "group-1", "user-1")
        self.plugin.client.pending_bindings[actor] = ("a" * 64, time.monotonic() + 600)
        for bot in ("qq_official", "other-bot"):
            self.assertIsNone(self.module.ScopedCommandFilter.match(self.qq_event(bot=bot)))
        event = self.qq_event()
        self.assertEqual(self.module.ScopedCommandFilter.match(event).action, "binding_code")
        self.assertEqual(self.module.event_actor(event, self.plugin.settings), actor)

    async def test_qq_official_identity_stays_stable_with_both_scopes_allowed(self):
        await self.use_qq_official([
            "qq-instance|qq_official|group-1", "qq-instance|bot-openid|group-1",
        ])
        self.plugin.client.request = AsyncMock(return_value={
            "state": "pending",
            "session_id": hashlib.sha256(("b" * 64).encode()).hexdigest(),
            "expires_in": 600, "url": "https://example.com/?az_bind=" + "b" * 64,
        })
        start = self.qq_event("/网站绑定", "start-1")
        self.assertEqual(len([r async for r in self.plugin.website_command(start)]), 1)
        self.assertIn(self.actor, self.plugin.client.pending_bindings)
        for bot in ("qq_official", "bot-openid"):
            event = self.qq_event(bot=bot)
            self.assertEqual(self.module.ScopedCommandFilter.match(event).action, "binding_code")
            self.assertEqual(self.module.event_actor(event, self.plugin.settings), self.actor)

    async def test_no_pending_numeric_chat_untouched(self):
        event = self.event()
        self.assertEqual([r async for r in self.plugin.website_command(event)], [])
        self.assertFalse(event.stopped)
        self.assertIsNone(event.llm)

    def test_code_filter_rejects_mixed_content_and_unwoken_messages(self):
        self.pending()
        for text in ("hello 000001", "000001 extra", "０１２３４５", "00000", "0000000", "00\n0001"):
            self.assertIsNone(self.module.ScopedCommandFilter.match(self.event(text)))
        event = self.event()
        for components in (
            [Reply("m"), Plain("000001")], [At("other"), Plain("000001")],
            [object(), Plain("000001")], [Plain(123456)],
        ):
            event.messages = components
            self.assertIsNone(self.module.ScopedCommandFilter.match(event))
        event.messages = [At("001"), Plain(" 000001 ")]
        self.assertEqual(self.module.ScopedCommandFilter.match(event).argument, "000001")
        event.is_at_or_wake_command = False
        self.assertIsNone(self.module.ScopedCommandFilter.match(event))
        event.is_at_or_wake_command = True
        event.messages = [Plain("000001")]
        self.assertIsNone(self.module.ScopedCommandFilter.match(event))
        event.messages = [At("001"), Plain("000001")]
        event.group = "other"
        self.assertIsNone(self.module.ScopedCommandFilter.match(event))

    async def test_success_duplicate_consumed_without_second_reply_or_ai(self):
        self.pending()
        self.plugin.client.request = AsyncMock(return_value={"state": "bound", "uid": 1, "nickname": "Member"})
        event = self.event()
        results = [r async for r in self.plugin.website_command(event)]
        self.assertEqual(len(results), 1)
        self.assertTrue(event.stopped)
        self.assertFalse(event.llm)
        self.assertIs(results[0].use_markdown_, False)
        duplicate = self.event()
        duplicate.message_obj.timestamp = event.message_obj.timestamp
        self.assertEqual([r async for r in self.plugin.website_command(duplicate)], [])
        self.assertTrue(duplicate.stopped)
        self.assertFalse(duplicate.llm)
        self.assertEqual(self.plugin.client.request.await_count, 1)
        ordinary = self.event(message_id="another")
        self.assertEqual([r async for r in self.plugin.website_command(ordinary)], [])
        self.assertFalse(ordinary.stopped)
        modified = self.event("000002")
        modified.message_obj.timestamp = event.message_obj.timestamp
        results = [r async for r in self.plugin.website_command(modified)]
        self.assertIn("冲突", results[0].text)
        self.assertEqual(self.plugin.client.request.await_count, 1)

    async def test_wrong_code_and_duplicate_never_reach_ai(self):
        self.pending()
        self.plugin.client.request = AsyncMock(return_value={"state": "code_invalid"})
        event = self.event()
        results = [r async for r in self.plugin.website_command(event)]
        self.assertTrue(event.stopped)
        self.assertFalse(event.llm)
        self.assertEqual(len(results), 1)
        duplicate = self.event()
        duplicate.message_obj.timestamp = event.message_obj.timestamp
        self.assertEqual([r async for r in self.plugin.website_command(duplicate)], [])
        self.assertEqual(self.plugin.client.request.await_count, 1)

    async def test_missing_message_identity_rejected_before_request(self):
        self.pending()
        self.plugin.client.request = AsyncMock()
        event = StubEvent("000001")
        event.messages = [At("001"), Plain("000001")]
        results = [r async for r in self.plugin.website_command(event)]
        self.assertIn("消息 ID", results[0].text)
        self.plugin.client.request.assert_not_awaited()

    async def test_actor_requests_are_serialized(self):
        gate = asyncio.Event()

        async def response(*args):
            await gate.wait()
            return {"state": "pending", "session_id": hashlib.sha256(("b" * 64).encode()).hexdigest(), "expires_in": 600, "url": "https://example.com/?az_bind=" + "b" * 64}

        self.plugin.client.request = AsyncMock(side_effect=response)

        async def run(event):
            return [r async for r in self.plugin.website_command(event)]

        first = asyncio.create_task(run(self.event("/网站绑定", "start-1")))
        await asyncio.sleep(0)
        self.plugin._cooldowns.clear()
        second = await run(self.event("/网站绑定", "start-2"))
        self.assertIn("频繁", second[0].text)
        gate.set()
        await first
        self.assertEqual(self.plugin.client.request.await_count, 1)
        self.assertEqual(self.plugin.client.pending_bindings[self.actor][0], hashlib.sha256(("b" * 64).encode()).hexdigest())

    async def test_reload_clears_pending_and_processed_state(self):
        self.pending()
        self.plugin._processed[(self.actor, "code-1")] = ("hash", time.monotonic() + 180)
        await self.plugin.terminate()
        self.plugin = self.module.ZibllPlugin(object(), config())
        self.assertIsNone(self.module.ScopedCommandFilter.match(self.event()))
        self.assertFalse(self.plugin.client.pending_bindings)

    def test_five_main_commands_and_renamed_legacy_aliases(self):
        primary = {k: v for k, v in DEFAULT_COMMANDS.items() if k not in {"confirm", "unbind"}}
        self.assertEqual(CommandRouter(primary).match("/网站绑定").action, "bind")
        renamed = {**primary, "confirm": "/old-confirm", "unbind": "/old-revoke"}
        self.assertEqual(CommandRouter(renamed).match("/old-confirm 123").action, "confirm")
        for values in ({**renamed, "confirm": primary["bind"]}, {**primary, "bind": "123456"}):
            with self.assertRaises(ValueError):
                CommandRouter(values)
