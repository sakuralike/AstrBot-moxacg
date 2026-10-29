import asyncio
import sys
import time
import types
import unittest
from contextlib import asynccontextmanager
from functools import partial
from unittest.mock import AsyncMock, Mock, patch

from test_foundations import (
    At, Plain, Reply, StubEvent, StubResult, config, load_main_with_framework_stubs,
)


def silence_config(**values):
    return {
        "silence_enabled": True,
        "allowed_groups": ["qq-instance|app-1|group-1"],
        "silence_vote_replies": ["Count this vote."],
        "silence_start_text": "Quiet for {seconds} seconds.",
        "silence_end_text": "Back again.",
        **values,
    }


def sent_text(event):
    return [
        "".join(component.text for component in message.chain if isinstance(component, Plain))
        for message in event.sent
    ]


class Clock:
    def __init__(self):
        self.now = time.time()
        self.waiters = []

    def time(self):
        return self.now

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + seconds, future))
        await future

    async def advance(self, seconds):
        self.now += seconds
        for _ in range(30):
            for deadline, future in self.waiters:
                if deadline <= self.now and not future.done():
                    future.set_result(None)
            await asyncio.sleep(0)


class GuardRegistration:
    def __init__(self, callback):
        self.callback = callback
        self.closed = False

    def close(self):
        self.closed = True


class SendGuard(types.ModuleType):
    API_VERSION = 1
    SUPPORTED_PLATFORMS = {"qq_official"}

    def __init__(self):
        super().__init__("astrbot.core.utils.group_send_guard")
        self.registrations = []
        self.controls = []

    def register(self, callback):
        registration = GuardRegistration(callback)
        self.registrations.append(registration)
        return registration

    @asynccontextmanager
    async def control_message(self, registration, platform_id, group_openid, timeout=5):
        if registration.closed:
            raise RuntimeError("closed_registration")
        self.controls.append((platform_id, group_openid))
        yield


class SilencePluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.guard = SendGuard()
        self.utils = types.ModuleType("astrbot.core.utils")
        self.utils.group_send_guard = self.guard
        self.guard_patch = patch.dict(sys.modules, {
            "astrbot.core": types.ModuleType("astrbot.core"),
            self.utils.__name__: self.utils,
            self.guard.__name__: self.guard,
        })
        self.guard_patch.start()
        self.addCleanup(self.guard_patch.stop)
        self.module = load_main_with_framework_stubs()
        self.clock = Clock()
        self.clock_patch = patch.object(self.module, "SilenceManager", partial(
            self.module.SilenceManager,
            wall=self.clock.time, mono=self.clock.time, sleep=self.clock.sleep,
        ))
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.registry = types.SimpleNamespace(
            stop_all=Mock(), _events={}, _background_tasks={},
        )
        registry_module = types.ModuleType("astrbot.core.utils.active_event_registry")
        registry_module.active_event_registry = self.registry
        self.registry_patch = patch.dict(sys.modules, {
            registry_module.__name__: registry_module,
        })
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)
        self.platforms = {
            "qq-instance": types.SimpleNamespace(
                appid="app-1", meta=lambda: types.SimpleNamespace(name="qq_official"),
                _allow_group_proactive_send=True, remember_session_scene=Mock(),
            ),
            "qq-instance-b": types.SimpleNamespace(
                appid="app-2", meta=lambda: types.SimpleNamespace(name="qq_official"),
                _allow_group_proactive_send=True, remember_session_scene=Mock(),
            ),
        }
        self.context = types.SimpleNamespace(
            plugin_kv={}, send_message=AsyncMock(return_value=True),
            get_platform_inst=Mock(side_effect=self.platforms.get),
        )
        self.plugin = self.module.ZibllPlugin(self.context, silence_config())
        await self.plugin.initialize()

    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def replace_plugin(self, values):
        await self.plugin.terminate()
        self.plugin = self.module.ZibllPlugin(self.context, values)
        await self.plugin.initialize()

    async def mute(self):
        events = []
        for number in range(5):
            event = self.event(sender=f"member-{number}", message_id=f"vote-{number}")
            await self.plugin.silence_guard(event)
            events.append(event)
        return events

    def event(self, text="/闭嘴 10", *, sender="member-1", message_id="vote-1"):
        event = StubEvent(text)
        event.platform = "qq-instance"
        event.platform_name = "qq_official"
        event.bot = "app-1"
        event.sender = sender
        event.message_obj = types.SimpleNamespace(
            message_id=message_id, timestamp=int(time.time()),
        )
        return event

    async def test_voting_works_without_website_credentials_or_connection(self):
        self.assertIsNone(self.plugin.client)
        event = self.event()
        await self.plugin.silence_guard(event)
        self.assertEqual(len(event.sent), 1)
        self.assertIn("1/5", sent_text(event)[0])
        self.assertIn("10", sent_text(event)[0])
        self.assertIn("Count this vote.", sent_text(event)[0])
        self.assertTrue(event.stopped)
        self.assertIs(event.sent[0].use_markdown_, False)
        self.assertFalse(self.plugin.silence_manager.is_muted(("qq-instance", "app-1", "group-1")))

    async def test_only_allowed_woken_group_commands_start_a_vote(self):
        for field, value in (
            ("kind", "private"), ("platform", "another-instance"),
            ("bot", "another-bot"), ("group", "another-group"),
            ("is_at_or_wake_command", False),
        ):
            with self.subTest(field=field):
                event = self.event()
                setattr(event, field, value)
                await self.plugin.silence_guard(event)
                self.assertEqual(event.sent, [])
                self.assertFalse(event.stopped)
        for text in ("ordinary chat", "/闭嘴10", "please /闭嘴 10", "/another 10"):
            with self.subTest(text=text):
                event = self.event(text)
                await self.plugin.silence_guard(event)
                self.assertEqual(event.sent, [])
                self.assertFalse(event.stopped)
        valid = self.event()
        await self.plugin.silence_guard(valid)
        self.assertIn("1/5", sent_text(valid)[0])

    async def test_qq_scope_alias_does_not_merge_real_bots_or_member_sessions(self):
        await self.replace_plugin(silence_config(
            allowed_groups=[
                "qq-instance|qq_official|group-openid",
                "qq-instance-b|qq_official|group-openid",
            ],
        ))
        events = []
        for platform, bot, sender, message_id in (
            ("qq-instance", "app-1", "member-a", "a-1"),
            ("qq-instance", "app-1", "member-b", "a-2"),
            ("qq-instance-b", "app-2", "member-a", "a-1"),
        ):
            event = self.event(sender=sender, message_id=message_id)
            event.platform_name = "qq_official"
            event.platform = platform
            event.bot = bot
            event.group = "group-openid"
            event.messages = [At(bot), Plain("/闭嘴 10")]
            await self.plugin.silence_guard(event)
            events.append(event)
        self.assertNotEqual(events[0].unified_msg_origin, events[1].unified_msg_origin)
        self.assertIn("1/5", sent_text(events[0])[0])
        self.assertIn("2/5", sent_text(events[1])[0])
        self.assertIn("1/5", sent_text(events[2])[0])

    async def test_only_clean_text_and_mentions_of_this_bot_can_vote(self):
        for components in (
            [At("another-member"), Plain("/闭嘴 10")],
            [Reply("quoted-id"), Plain("/闭嘴 10")],
            [object(), Plain("/闭嘴 10")],
            [Plain("/闭嘴 10 <@another-member>")],
            [Plain('<qqbot-at-user id="another-member"/> /闭嘴 10')],
            [Plain(123)], [], None,
        ):
            with self.subTest(components=components):
                event = self.event()
                event.messages = components
                await self.plugin.silence_guard(event)
                self.assertEqual(event.sent, [])
                self.assertFalse(event.stopped)
        raw_mention = self.event()
        raw_mention.message_obj.raw_message = types.SimpleNamespace(
            mentions=[types.SimpleNamespace(id="another-member")],
        )
        await self.plugin.silence_guard(raw_mention)
        self.assertEqual(raw_mention.sent, [])
        self.assertFalse(raw_mention.stopped)
        for index, components in enumerate((
            [At("app-1"), Plain(" /闭嘴 10 ")],
            [Plain("<@app-1> /闭嘴 10")],
            [Plain('<qqbot-at-user id="app-1"/> /闭嘴 10')],
        )):
            event = self.event(sender=f"member-{index}", message_id=f"vote-{index}")
            event.messages = components
            await self.plugin.silence_guard(event)
            self.assertIn(f"{index + 1}/5", sent_text(event)[0])

    async def test_sender_and_message_identity_are_used_without_nickname_fallback(self):
        for sender, message_id in (
            ("app-1", "self"), ("", "missing-sender"), (None, "missing-sender"),
            ("member-1", None), ("member-1", ""), ("member-1", True),
        ):
            with self.subTest(sender=sender, message_id=message_id):
                event = self.event(sender=sender, message_id=message_id)
                await self.plugin.silence_guard(event)
                self.assertEqual(event.sent, [])
        first = self.event(sender="member-a", message_id="one")
        first.sender_name = "Same nickname"
        await self.plugin.silence_guard(first)
        duplicate = self.event(sender="member-a", message_id="one")
        await self.plugin.silence_guard(duplicate)
        self.assertEqual(duplicate.sent, [])
        repeat_member = self.event(sender="member-a", message_id="two")
        await self.plugin.silence_guard(repeat_member)
        self.assertIn("1/5", sent_text(repeat_member)[0])
        second = self.event(sender="member-b", message_id="three")
        second.sender_name = "Same nickname"
        await self.plugin.silence_guard(second)
        self.assertIn("2/5", sent_text(second)[0])

    async def test_filter_never_wakes_ordinary_chat_and_matches_silent_unwoken_events(self):
        command_filter = self.module.ScopedSilenceFilter()
        ordinary = self.event("ordinary chat")
        ordinary.is_at_or_wake_command = False
        ordinary.is_wake = False
        self.assertFalse(command_filter.filter(ordinary, {}))
        self.assertFalse(ordinary.is_at_or_wake_command)
        self.assertFalse(ordinary.is_wake)
        ordinary.is_wake = True
        self.assertTrue(command_filter.filter(ordinary, {}))
        for field, value in (
            ("kind", "private"), ("platform", "other"),
            ("bot", "other"), ("group", "other"),
        ):
            event = self.event()
            setattr(event, field, value)
            self.assertFalse(command_filter.filter(event, {}))
        for number in range(5):
            event = self.event(sender=f"member-{number}", message_id=f"vote-{number}")
            self.assertTrue(command_filter.filter(event, {}))
            await self.plugin.silence_guard(event)
        unwoken = self.event("ordinary chat")
        unwoken.is_at_or_wake_command = False
        self.assertTrue(command_filter.filter(unwoken, {}))
        await self.plugin.silence_guard(unwoken)
        self.assertTrue(unwoken.stopped)
        self.assertEqual(unwoken.sent, [])
        await self.replace_plugin(silence_config(silence_enabled=False))
        self.assertFalse(command_filter.filter(self.event(), {}))

    async def test_filter_discards_muted_commands_before_wait_and_after_expiry(self):
        await self.plugin.terminate()
        self.plugin = self.module.ZibllPlugin(self.context, {
            **config(), **silence_config(),
        })
        self.plugin.client.capabilities = AsyncMock(return_value={})
        await self.plugin.initialize()
        self.plugin.client.blindbox_available = True
        await self.mute()
        event = self.event("/开盲盒", message_id="during-silence")
        event.result = StubResult("Stale result")
        self.assertTrue(self.module.ScopedSilenceFilter().filter(event, {}))
        with self.subTest(stage="waking"):
            self.assertTrue(event.stopped)
            self.assertTrue(event.get_extra("zibll_silence_discarded", False))
            self.assertIsNone(event.result)
        await self.clock.advance(10)
        with patch.object(
            self.module, "execute", AsyncMock(return_value=("Unexpected transaction", None)),
        ) as execute:
            replies = [reply async for reply in self.plugin.website_command(event)]
        with self.subTest(stage="after_expiry"):
            execute.assert_not_awaited()
            self.assertEqual(replies, [])
            self.assertTrue(event.stopped)
            self.assertIsNone(event.result)

    async def test_fifth_vote_discards_events_queued_between_filter_and_handler(self):
        command_filter = self.module.ScopedSilenceFilter()
        queued = [
            self.event("/开盲盒", sender="member-a", message_id="queued-a"),
            self.event("/兑换次数", sender="member-b", message_id="queued-b"),
        ]
        for event in queued:
            self.assertTrue(command_filter.filter(event, {}))
            self.assertFalse(event.stopped)
        other_group = self.event("/开盲盒", message_id="another-group")
        other_group.group = "another-group"
        self.assertFalse(command_filter.filter(other_group, {}))
        await self.mute()
        for event in queued:
            with self.subTest(stage="fifth_vote", sender=event.sender):
                self.assertTrue(event.stopped)
                self.assertTrue(event.get_extra("zibll_silence_discarded", False))
        self.assertFalse(other_group.stopped)
        await self.clock.advance(10)
        for event in queued:
            await self.plugin.silence_guard(event)
            with self.subTest(stage="after_expiry", sender=event.sender):
                self.assertTrue(event.stopped)
                self.assertTrue(event.get_extra("zibll_silence_discarded", False))

    async def test_blindbox_refresh_cannot_start_transactions_after_silence_begins(self):
        for expire_before_release in (False, True):
            with self.subTest(expire_before_release=expire_before_release):
                await self.plugin.terminate()
                self.context.plugin_kv.clear()
                self.plugin = self.module.ZibllPlugin(self.context, {
                    **config(), **silence_config(),
                })
                self.plugin.client.capabilities = AsyncMock(return_value={})
                await self.plugin.initialize()
                started = asyncio.Event()
                release = asyncio.Event()

                async def refresh():
                    started.set()
                    await release.wait()
                    self.plugin.client.blindbox_available = True

                self.plugin._refresh_blindbox_capabilities = AsyncMock(side_effect=refresh)
                event = self.event("/开盲盒", message_id="waiting-for-capabilities")
                await self.plugin.silence_guard(event)

                async def collect():
                    return [reply async for reply in self.plugin.website_command(event)]

                with patch.object(
                    self.module, "execute",
                    AsyncMock(return_value=("Unexpected transaction", None)),
                ) as execute:
                    pending = asyncio.create_task(collect())
                    try:
                        await asyncio.wait_for(started.wait(), 1)
                        await self.mute()
                        if expire_before_release:
                            await self.clock.advance(10)
                        release.set()
                        replies = await asyncio.wait_for(pending, 1)
                        execute.assert_not_awaited()
                        self.assertEqual(replies, [])
                        self.assertTrue(event.get_extra("zibll_silence_discarded", False))
                    finally:
                        release.set()
                        if not pending.done():
                            pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)

    async def test_cold_start_initializes_when_platforms_load_without_resetting_votes(self):
        await self.plugin.terminate()
        platforms = dict(self.platforms)
        self.platforms.clear()
        registrations = len(self.guard.registrations)
        self.plugin = self.module.ZibllPlugin(self.context, silence_config())
        await self.plugin.initialize()
        self.assertFalse(self.plugin.silence_manager.ready)
        self.assertEqual(len(self.guard.registrations), registrations)
        self.platforms.update(platforms)
        for handler in self.module.astrbot_loaded_handlers:
            await handler(self.plugin)
        self.assertTrue(self.plugin.silence_manager.ready)
        self.assertEqual(len(self.guard.registrations), registrations + 1)
        first = self.event(sender="member-a", message_id="cold-vote-a")
        await self.plugin.silence_guard(first)
        self.assertIn("1/5", sent_text(first)[0])
        for handler in self.module.astrbot_loaded_handlers:
            await handler(self.plugin)
        self.assertEqual(len(self.guard.registrations), registrations + 1)
        second = self.event(sender="member-b", message_id="cold-vote-b")
        await self.plugin.silence_guard(second)
        self.assertIn("2/5", sent_text(second)[0])

    async def test_termination_during_failed_state_load_does_not_reclose_missing_guard(self):
        await self.plugin.terminate()
        self.plugin = self.module.ZibllPlugin(self.context, silence_config())
        started = asyncio.Event()
        release = asyncio.Event()

        async def load(*args):
            started.set()
            await release.wait()
            raise RuntimeError("state_unavailable")

        self.plugin.get_kv_data = AsyncMock(side_effect=load)
        pending = asyncio.create_task(self.plugin.initialize())
        try:
            await asyncio.wait_for(started.wait(), 1)
            registration = self.guard.registrations[-1]
            await self.plugin.terminate()
            self.assertTrue(registration.closed)
            release.set()
            await asyncio.wait_for(pending, 1)
            self.assertIsNone(self.plugin._silence_registration)
            self.assertFalse(self.plugin.silence_manager.ready)
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_all_existing_business_commands_are_blocked_before_external_requests(self):
        values = {
            **config(), **silence_config(),
            "balance_enabled": True,
            "deepseek_api_key": "fake-test-key",
            "token_usage_enabled": True,
        }
        await self.plugin.terminate()
        self.plugin = self.module.ZibllPlugin(self.context, values)
        self.plugin.client.capabilities = AsyncMock(return_value={})
        self.plugin.client.request = AsyncMock()
        self.plugin.balance_client.query = AsyncMock()
        self.plugin.usage_client.query = AsyncMock()
        await self.plugin.initialize()
        await self.mute()
        for command, handler in (
            ("/网站签到", self.plugin.website_command),
            ("/开盲盒", self.plugin.website_command),
            ("/兑换次数", self.plugin.website_command),
            ("/加积分 10", self.plugin.website_command),
            ("/模型余额", self.plugin.balance_command),
            ("/模型用量", self.plugin.usage_command),
        ):
            with self.subTest(command=command):
                event = self.event(command)
                event.admin = True
                replies = [reply async for reply in handler(event)]
                self.assertEqual(replies, [])
                self.assertTrue(event.stopped)
                self.assertIsNone(event.result)
        self.plugin.client.request.assert_not_awaited()
        self.plugin.balance_client.query.assert_not_awaited()
        self.plugin.usage_client.query.assert_not_awaited()

    async def test_old_results_from_all_member_sessions_stay_discarded_after_expiry(self):
        old_events = []
        for sender in ("member-a", "member-b"):
            event = self.event("ordinary conversation", sender=sender, message_id=sender)
            await self.plugin.silence_guard(event)
            await self.plugin.silence_llm_guard(event, object())
            self.assertFalse(event.stopped)
            old_events.append(event)
        other_group = self.event("other group")
        other_group.group = "another-group"
        await self.plugin.silence_guard(other_group)
        await self.mute()
        for event in old_events:
            self.assertTrue(event.stopped)
            self.registry.stop_all.assert_any_call(
                event.unified_msg_origin, exclude=unittest.mock.ANY,
            )
        self.assertFalse(other_group.stopped)
        await self.clock.advance(10)
        self.assertFalse(self.plugin.silence_manager.is_muted(("qq-instance", "app-1", "group-1")))
        for event in old_events:
            event.result = StubResult("Late ordinary result")
            await self.plugin.qq_pc_compatible_reply(event)
            self.assertIsNone(event.result)
            self.assertTrue(event.stopped)
        fresh = self.event("fresh conversation", message_id="after-expiry")
        fresh.result = StubResult("Fresh result")
        await self.plugin.silence_guard(fresh)
        await self.plugin.silence_llm_guard(fresh, object())
        await self.plugin.qq_pc_compatible_reply(fresh)
        self.assertFalse(fresh.stopped)
        self.assertEqual(fresh.result.text, "Fresh result")

    async def test_short_qq_silence_resumes_via_fifth_vote_event_only(self):
        await self.replace_plugin(silence_config(
            allowed_groups=["qq-instance|app-1|group-openid"],
        ))
        events = []
        for number in range(5):
            event = self.event(sender=f"member-{number}", message_id=f"qq-vote-{number}")
            event.platform = "qq-instance"
            event.platform_name = "qq_official"
            event.bot = "app-1"
            event.group = "group-openid"
            await self.plugin.silence_guard(event)
            events.append(event)
        self.assertIn("Count this vote.", sent_text(events[-1])[0])
        self.assertIn("5/5", sent_text(events[-1])[0])
        self.assertIn("Quiet for 10 seconds.", sent_text(events[-1])[0])
        self.assertEqual([len(event.sent) for event in events], [1, 1, 1, 1, 1])
        await self.clock.advance(10)
        self.assertEqual([len(event.sent) for event in events], [1, 1, 1, 1, 2])
        self.assertEqual(sent_text(events[-1])[-1], "Back again.")
        self.context.send_message.assert_not_awaited()
        await self.clock.advance(30)
        self.assertEqual(len(events[-1].sent), 2)

    async def test_reload_resumes_remaining_time_and_uses_real_group_route(self):
        values = silence_config(allowed_groups=["qq-instance|qq_official|group-openid"])
        await self.replace_plugin(values)
        events = []
        for number in range(5):
            event = self.event(sender=f"member-{number}", message_id=f"reload-{number}")
            event.platform = "qq-instance"
            event.platform_name = "qq_official"
            event.bot = "app-1"
            event.group = "group-openid"
            await self.plugin.silence_guard(event)
            events.append(event)
        platform = self.platforms["qq-instance"]
        await self.clock.advance(3)
        await self.replace_plugin(values)
        scope = ("qq-instance", "app-1", "group-openid")
        self.assertTrue(self.plugin.silence_manager.is_muted(scope))
        await self.clock.advance(6)
        self.assertTrue(self.plugin.silence_manager.is_muted(scope))
        self.context.send_message.assert_not_awaited()
        await self.clock.advance(1)
        self.assertFalse(self.plugin.silence_manager.is_muted(scope))
        self.context.send_message.assert_awaited_once()
        route, message = self.context.send_message.await_args.args
        self.assertEqual(route, "qq-instance:GroupMessage:group-openid")
        self.assertEqual(message.chain[0].text, "Back again.")
        self.assertIs(message.use_markdown_, False)
        platform.remember_session_scene.assert_called_once_with("group-openid", "group")
        self.assertEqual([len(event.sent) for event in events], [1, 1, 1, 1, 1])

    async def test_missing_core_guard_disables_only_silence(self):
        await self.plugin.terminate()
        with patch.object(self.module, "_group_send_guard", None):
            with self.assertLogs("astrbot-foundation-tests", level="WARNING"):
                self.plugin = self.module.ZibllPlugin(
                    self.context, {**config(), **silence_config()},
                )
                self.plugin.client.capabilities = AsyncMock(return_value={})
                await self.plugin.initialize()
        self.assertIsNotNone(self.plugin.client)
        self.assertTrue(self.plugin.connected)
        event = self.event()
        await self.plugin.silence_guard(event)
        self.assertEqual(event.sent, [])
        self.assertFalse(event.stopped)
        self.assertFalse(self.module.ScopedSilenceFilter().filter(event, {}))

    async def test_guard_decision_covers_direct_sends_and_control_messages_use_permits(self):
        registration = self.guard.registrations[-1]
        old = self.event("already running", message_id="old-request")
        await self.plugin.silence_guard(old)
        self.assertTrue(registration.callback("qq-instance", "group-1", old))
        events = await self.mute()
        self.assertFalse(registration.callback("qq-instance", "group-1", None))
        self.assertFalse(registration.callback("qq-instance", "group-1", self.event("new")))
        self.assertTrue(registration.callback("qq-instance", "another-group", None))
        self.assertTrue(registration.callback("qq-instance-b", "group-1", None))
        self.assertEqual(self.guard.controls, [("qq-instance", "group-1")] * 5)
        self.assertEqual(len(events[-1].sent), 1)
        await self.clock.advance(10)
        self.assertTrue(registration.callback("qq-instance", "group-1", None))
        self.assertFalse(registration.callback("qq-instance", "group-1", old))
        self.assertEqual(self.guard.controls, [("qq-instance", "group-1")] * 6)
        self.assertEqual(sent_text(events[-1])[-1], "Back again.")
        await self.plugin.terminate()
        self.assertTrue(registration.closed)

    async def test_qq_raw_bot_identity_and_alias_share_adapter_app_state(self):
        await self.replace_plugin(silence_config(
            allowed_groups=["qq-instance|qq_official|group-1"],
        ))
        first = self.event(sender="member-a", message_id="alias-vote")
        first.bot = "qq_official"
        first.messages = [At("qq_official"), Plain("/闭嘴 10")]
        await self.plugin.silence_guard(first)
        second = self.event(sender="member-b", message_id="openid-vote")
        second.bot = "bot-openid"
        second.messages = [At("bot-openid"), Plain("/闭嘴 10")]
        await self.plugin.silence_guard(second)
        self.assertIn("1/5", sent_text(first)[0])
        self.assertIn("2/5", sent_text(second)[0])
        for number in range(3, 6):
            event = self.event(sender=f"member-{number}", message_id=f"message-{number}")
            event.bot = "bot-openid"
            await self.plugin.silence_guard(event)
        self.assertTrue(self.plugin.silence_manager.is_muted(("qq-instance", "app-1", "group-1")))
        self.assertFalse(self.plugin.silence_manager.is_muted(("qq-instance", "qq_official", "group-1")))
        self.assertFalse(self.plugin.silence_manager.is_muted(("qq-instance", "bot-openid", "group-1")))

    async def test_unsupported_adapter_stays_disabled_instead_of_partial_silence(self):
        self.platforms["unsupported"] = types.SimpleNamespace(
            appid="001", meta=lambda: types.SimpleNamespace(name="aiocqhttp"),
        )
        with self.assertLogs("astrbot-foundation-tests", level="WARNING"):
            await self.replace_plugin(silence_config(
                allowed_groups=["unsupported|001|group-1"],
            ))
        event = self.event()
        event.platform = "unsupported"
        event.platform_name = "aiocqhttp"
        event.bot = "001"
        await self.plugin.silence_guard(event)
        self.assertEqual(event.sent, [])
        self.assertFalse(event.stopped)
        self.assertFalse(self.module.ScopedSilenceFilter().filter(event, {}))

    async def test_disabling_then_reenabling_does_not_restore_cancelled_silence(self):
        events = await self.mute()
        await self.replace_plugin(silence_config(silence_enabled=False))
        await self.replace_plugin(silence_config())
        self.assertFalse(self.plugin.silence_manager.is_muted(("qq-instance", "app-1", "group-1")))
        await self.clock.advance(10)
        self.assertEqual([len(event.sent) for event in events], [1, 1, 1, 1, 1])
        self.context.send_message.assert_not_awaited()
        fresh = self.event(message_id="fresh-round", sender="new-member")
        await self.plugin.silence_guard(fresh)
        self.assertIn("1/5", sent_text(fresh)[0])

    async def test_state_read_failure_disables_silence_without_disabling_website(self):
        await self.plugin.terminate()
        self.plugin = self.module.ZibllPlugin(
            self.context, {**config(), **silence_config()},
        )
        self.plugin.get_kv_data = AsyncMock(side_effect=OSError("test-storage-unavailable"))
        self.plugin.client.capabilities = AsyncMock(return_value={})
        with self.assertLogs(level="WARNING"):
            await self.plugin.initialize()
        self.assertTrue(self.plugin.connected)
        self.assertFalse(self.plugin.silence_manager.ready)
        event = self.event()
        await self.plugin.silence_guard(event)
        self.assertFalse(event.stopped)
        self.assertEqual(event.sent, [])
        self.context.send_message.assert_not_awaited()

    async def test_old_instance_termination_keeps_current_filter_and_registration(self):
        old = self.plugin
        old_registration = self.guard.registrations[-1]
        self.plugin = self.module.ZibllPlugin(self.context, silence_config())
        await self.plugin.initialize()
        current_registration = self.guard.registrations[-1]
        await old.terminate()
        self.assertTrue(old_registration.closed)
        self.assertFalse(current_registration.closed)
        event = self.event()
        self.assertTrue(self.module.ScopedSilenceFilter().filter(event, {}))
        await self.plugin.silence_guard(event)
        self.assertIn("1/5", sent_text(event)[0])
        old_event = self.event(message_id="old-instance")
        await old.silence_guard(old_event)
        self.assertEqual(old_event.sent, [])
        self.assertFalse(old_event.stopped)

    async def test_invalid_parameters_do_not_create_a_vote(self):
        for index, text in enumerate((
            "/闭嘴", "/闭嘴 0", "/闭嘴 -1", "/闭嘴 1.5",
            "/闭嘴 10 extra", "/闭嘴 3601", "/闭嘴 " + "9" * 600,
            "/闭嘴\n10",
        )):
            with self.subTest(text=text[:40]):
                event = self.event(text, message_id=f"invalid-{index}")
                await self.plugin.silence_guard(event)
                self.assertTrue(event.stopped)
                self.assertEqual(len(event.sent), 1)
                self.assertIn("/闭嘴", sent_text(event)[0])
        valid = self.event(message_id="valid")
        await self.plugin.silence_guard(valid)
        self.assertIn("1/5", sent_text(valid)[0])


if __name__ == "__main__":
    unittest.main()
