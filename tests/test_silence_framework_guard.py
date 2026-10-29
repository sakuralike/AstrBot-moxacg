import asyncio
import hashlib
import os
import sys
import time
import types
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_silence_framework as baseline


PATCHED_SOURCE = Path(os.environ.get(
    "ASTRBOT_V4293_PATCHED_SOURCE", r"D:\kfhj\AstrBot-v4.29.3-silence-patched",
))
PATCHED_BLOBS = {
    "platform/sources/qqofficial/qqofficial_message_event.py": "302f830b875f2a17659cd70218f329da60689ced",
    "platform/sources/qqofficial/qqofficial_platform_adapter.py": "215c1d4125b4d996c1fbb21429eb70f91e0c8733",
    "utils/group_send_guard.py": "34ea32552f3282981b7ecafb869c2c373ca87556",
}


def load_guard():
    path = PATCHED_SOURCE / "astrbot/core/utils/group_send_guard.py"
    data = path.read_bytes()
    blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    if blob != PATCHED_BLOBS["utils/group_send_guard.py"]:
        raise AssertionError("The reviewed group send guard has changed")
    namespace = {"__name__": __name__}
    exec(compile(data, str(path), "exec"), namespace)
    return SimpleNamespace(**namespace)


@unittest.skipUnless(PATCHED_SOURCE.is_dir(), "Set ASTRBOT_V4293_PATCHED_SOURCE to the patched source")
class SilenceFrameworkGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.object(baseline, "SOURCE", PATCHED_SOURCE), patch.dict(
            baseline.BLOBS, PATCHED_BLOBS,
        ):
            self.framework = baseline.ExactFramework()
        self.guard = load_guard()
        self.framework.ns["group_send_guard"] = self.guard
        self.muted = {("qq1", "group1")}
        self.registration = self.guard.register(
            lambda platform, group, event: (platform, group) not in self.muted,
        )

    async def asyncTearDown(self):
        self.registration.close()

    async def test_qq_direct_and_proactive_sends_are_blocked(self):
        f = self.framework
        event = f.event()
        with self.assertRaises(self.guard.GroupSendBlocked):
            await event.send(f.result("direct"))
        context, adapter = f.context()
        with self.assertRaises(self.guard.GroupSendBlocked):
            await context.send_message(event.unified_msg_origin, f.result("proactive"))
        event.bot.api.post_group_message.assert_not_awaited()
        adapter.client.api.post_group_message.assert_not_awaited()

    async def test_streaming_flush_is_blocked_when_silence_begins_midstream(self):
        f = self.framework
        self.muted.clear()
        event = f.event()

        async def stream():
            yield f.result("before")
            self.muted.add(("qq1", "group1"))
            event.clear_result()
            event.stop_event()
            yield f.result("after")

        result = f.result()
        result.result_content_type = f.ns["ResultContentType"].STREAMING_RESULT
        result.async_stream = stream()
        event.set_result(result)
        await f.respond().process(event)
        event.bot.api.post_group_message.assert_not_awaited()

    async def test_segmented_reply_checks_permission_again_between_segments(self):
        f = self.framework
        self.muted.clear()
        event = f.event()
        event.set_result(f.result("first", "second"))

        async def transport(**kwargs):
            self.muted.add(("qq1", "group1"))
            return {"id": "sent"}

        event.bot.api.post_group_message.side_effect = transport
        await f.respond(segmented=True).process(event)
        event.bot.api.post_group_message.assert_awaited_once()
        self.assertEqual(event.bot.api.post_group_message.await_args.kwargs["content"], "first")

    async def test_group_and_platform_scopes_are_independent(self):
        f = self.framework
        other_group = f.event("group2")
        other_platform = f.event()
        other_platform.platform_meta.id = "qq2"
        await other_group.send(f.result("other group"))
        await other_platform.send(f.result("other platform"))
        other_group.bot.api.post_group_message.assert_awaited_once()
        other_platform.bot.api.post_group_message.assert_awaited_once()
        with self.assertRaises(self.guard.GroupSendBlocked):
            await f.event().send(f.result("muted"))

    async def test_control_message_is_consumed_once_and_does_not_survive_context(self):
        f = self.framework
        event = f.event()
        async with self.guard.control_message(self.registration, "qq1", "group1"):
            await event.send(f.result("closing"))
            with self.assertRaises(self.guard.GroupSendBlocked):
                await event.send(f.result("second"))
        with self.assertRaises(self.guard.GroupSendBlocked):
            await event.send(f.result("third"))
        event.bot.api.post_group_message.assert_awaited_once()

    async def test_child_task_cannot_inherit_control_permission(self):
        f = self.framework
        event = f.event()
        async with self.guard.control_message(self.registration, "qq1", "group1"):
            child = asyncio.create_task(event.send(f.result("child")))
            await asyncio.sleep(0)
            self.assertFalse(child.done())
        with self.assertRaises(self.guard.GroupSendBlocked):
            await child
        event.bot.api.post_group_message.assert_not_awaited()

    async def test_control_drains_previous_send_before_closing_text(self):
        f = self.framework
        self.muted.clear()
        old, closing = f.event(), f.event()
        entered, release = asyncio.Event(), asyncio.Event()
        order = []

        async def old_transport(**kwargs):
            order.append("old started")
            entered.set()
            await release.wait()
            order.append("old finished")
            return {"id": "old"}

        async def closing_transport(**kwargs):
            order.append("closing")
            return {"id": "closing"}

        old.bot.api.post_group_message.side_effect = old_transport
        closing.bot.api.post_group_message.side_effect = closing_transport
        sending = asyncio.create_task(old.send(f.result("old")))
        await entered.wait()
        self.muted.add(("qq1", "group1"))

        async def finish():
            async with self.guard.control_message(self.registration, "qq1", "group1"):
                await closing.send(f.result("closing"))

        finishing = asyncio.create_task(finish())
        await asyncio.sleep(0)
        self.assertFalse(finishing.done())
        closing.bot.api.post_group_message.assert_not_awaited()
        release.set()
        await asyncio.gather(sending, finishing)
        self.assertEqual(order, ["old started", "old finished", "closing"])

    async def test_control_drain_timeout_is_bounded_and_releases_its_waiter(self):
        f = self.framework
        self.muted.clear()
        old = f.event()
        entered, release = asyncio.Event(), asyncio.Event()

        async def transport(**kwargs):
            entered.set()
            await release.wait()
            return {"id": "old"}

        old.bot.api.post_group_message.side_effect = transport
        sending = asyncio.create_task(old.send(f.result("old")))
        await entered.wait()
        self.muted.add(("qq1", "group1"))
        try:
            with self.assertRaises(TimeoutError):
                async with self.guard.control_message(
                    self.registration, "qq1", "group1", timeout=0.01,
                ):
                    self.fail("The in-flight send has not drained")
        finally:
            release.set()
            await sending
        final = f.event()
        async with self.guard.control_message(self.registration, "qq1", "group1"):
            await final.send(f.result("final"))
        final.bot.api.post_group_message.assert_awaited_once()

    async def test_control_send_timeout_releases_lock_and_permission(self):
        f = self.framework
        event = f.event()
        async def transport(**kwargs):
            await asyncio.Event().wait()

        event.bot.api.post_group_message.side_effect = transport
        with self.assertRaises(TimeoutError):
            async with self.guard.control_message(
                self.registration, "qq1", "group1", timeout=0.01,
            ):
                await event.send(f.result("hung"))
        replacement = f.event()
        with self.assertRaises(self.guard.GroupSendBlocked):
            await replacement.send(f.result("no leaked permission"))
        async with self.guard.control_message(self.registration, "qq1", "group1"):
            await replacement.send(f.result("new control"))
        replacement.bot.api.post_group_message.assert_awaited_once()

    async def test_qq_retry_rechecks_permission_after_failed_attempt(self):
        f = self.framework
        self.muted.clear()
        event = f.event()

        async def transport(**kwargs):
            self.muted.add(("qq1", "group1"))
            raise baseline.TransportFailure("reply rejected")

        event.bot.api.post_group_message.side_effect = transport
        with self.assertRaises(self.guard.GroupSendBlocked):
            await event.send(f.result("old reply"))
        event.bot.api.post_group_message.assert_awaited_once()

    async def test_control_permits_same_message_retry_but_not_second_message(self):
        f = self.framework
        event = f.event()

        async def transport(**payload):
            if "msg_id" in payload:
                raise baseline.TransportFailure("reply rejected")
            return {"id": "delivered"}

        event.bot.api.post_group_message.side_effect = transport
        async with self.guard.control_message(self.registration, "qq1", "group1"):
            await event.send(f.result("closing"))
            with self.assertRaises(self.guard.GroupSendBlocked):
                await event.send(f.result("unrelated"))
        self.assertEqual(event.bot.api.post_group_message.await_count, 2)

    async def test_control_proactive_message_uses_same_guard(self):
        f = self.framework
        context, adapter = f.context()
        async with self.guard.control_message(self.registration, "qq1", "group1"):
            self.assertTrue(await context.send_message(
                f.event().unified_msg_origin, f.result("recovery"),
            ))
        adapter.client.api.post_group_message.assert_awaited_once()
        with self.assertRaises(self.guard.GroupSendBlocked):
            await context.send_message(f.event().unified_msg_origin, f.result("later"))

    async def test_unregister_removes_policy_and_invalidates_control_handle(self):
        f = self.framework
        self.registration.close()
        await f.event().send(f.result("unloaded"))
        with self.assertRaises(RuntimeError):
            async with self.guard.control_message(self.registration, "qq1", "group1"):
                self.fail("Closed registration was accepted")

    async def test_control_cannot_bypass_another_registration(self):
        f = self.framework
        second = self.guard.register(lambda platform, group, event: False)
        try:
            async with self.guard.control_message(self.registration, "qq1", "group1"):
                with self.assertRaises(self.guard.GroupSendBlocked):
                    await f.event().send(f.result("blocked by another plugin"))
        finally:
            second.close()

    async def test_permission_callback_failure_blocks_output(self):
        f = self.framework

        def fail(platform, group, event):
            raise RuntimeError("state unavailable")

        self.registration.close()
        second = self.guard.register(fail)
        event = f.event()
        try:
            with self.assertRaises(self.guard.GroupSendBlocked):
                await event.send(f.result("blocked"))
        finally:
            second.close()
        event.bot.api.post_group_message.assert_not_awaited()

    async def test_real_plugin_manager_and_qq_guard_complete_vote_mute_resume_cycle(self):
        f = self.framework
        self.registration.close()
        package = types.ModuleType("astrbot_plugin_zibll")
        package.__path__ = [str(Path(__file__).resolve().parents[1])]
        package_modules = (
            {} if "astrbot_plugin_zibll" in sys.modules
            else {"astrbot_plugin_zibll": package}
        )
        registry_module = types.ModuleType("astrbot.core.utils.active_event_registry")
        registry_module.active_event_registry = f.ns["ActiveEventRegistry"]()
        with patch.dict(sys.modules, {
            **package_modules,
            registry_module.__name__: registry_module,
        }):
            from test_foundations import load_main_with_framework_stubs
            from test_silence_plugin import Clock, silence_config

            module = load_main_with_framework_stubs()
            module._group_send_guard = self.guard
            module.Plain = f.ns["Plain"]
            module.At = f.ns["At"]
            module.MessageChain = f.ns["MessageChain"]
            module.filter.EventMessageTypeFilter = f.ns["EventMessageTypeFilter"]
            module.filter.EventMessageType = f.ns["EventMessageType"]
            clock = Clock()
            context, adapter = f.context()
            context.plugin_kv = {}
            adapter.appid = "bot1"
            settings = silence_config(
                allowed_groups=["qq1|bot1|group1"],
                silence_start_text="Closing for {seconds} seconds.",
                silence_end_text="Custom opening.",
            )
            with patch.object(module, "SilenceManager", partial(
                module.SilenceManager, wall=clock.time, mono=clock.time, sleep=clock.sleep,
            )):
                plugin = module.ZibllPlugin(context, settings)
                await plugin.initialize()
                try:
                    self.assertTrue(plugin.silence_manager.ready)
                    votes = []
                    for index in range(5):
                        event = f.event(sender=f"voter{index}")
                        event.message_str = "/\u95ed\u5634 10"
                        event.message_obj.message = [f.ns["Plain"](event.message_str)]
                        event.message_obj.message_id = f"vote{index}"
                        event.message_obj.timestamp = int(time.time())
                        event.is_at_or_wake_command = True
                        event.plain_result = f.result
                        await plugin.silence_guard(event)
                        event.bot.api.post_group_message.assert_awaited_once()
                        votes.append(event)
                    closing = votes[-1].bot.api.post_group_message
                    self.assertIn("5/5", closing.await_args.kwargs["content"])
                    self.assertIn("Closing for 10 seconds.", closing.await_args.kwargs["content"])
                    self.assertTrue(plugin.silence_manager.is_muted(("qq1", "bot1", "group1")))

                    ordinary = f.event(sender="ordinary")
                    ordinary.set_result(f.result("ordinary reply"))
                    await plugin.silence_guard(ordinary)
                    await f.respond().process(ordinary)
                    self.assertTrue(ordinary.is_stopped())
                    ordinary.bot.api.post_group_message.assert_not_awaited()
                    direct = f.event(sender="direct")
                    with self.assertRaises(self.guard.GroupSendBlocked):
                        await direct.send(f.result("direct reply"))
                    direct.bot.api.post_group_message.assert_not_awaited()
                    with self.assertRaises(self.guard.GroupSendBlocked):
                        await context.send_message(direct.unified_msg_origin, f.result("proactive"))
                    adapter.client.api.post_group_message.assert_not_awaited()
                    other = f.event("othergroup")
                    await other.send(f.result("other group reply"))
                    other.bot.api.post_group_message.assert_awaited_once()

                    await clock.advance(10)
                    self.assertFalse(plugin.silence_manager.is_muted(("qq1", "bot1", "group1")))
                    self.assertEqual(
                        [call.kwargs["content"] for call in closing.await_args_list].count("Custom opening."),
                        1,
                    )
                    await clock.advance(60)
                    self.assertEqual(closing.await_count, 2)
                    recovered = f.event(sender="recovered")
                    await recovered.send(f.result("normal replies restored"))
                    recovered.bot.api.post_group_message.assert_awaited_once()
                    with self.assertRaises(self.guard.GroupSendBlocked):
                        await ordinary.send(f.result("old discarded result"))
                finally:
                    await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
