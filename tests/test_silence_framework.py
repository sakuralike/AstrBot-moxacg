import abc
import ast
import asyncio
import copy
import enum
import hashlib
import logging
import math
import os
import random
import traceback
import unittest
import uuid
from collections import defaultdict
from collections.abc import AsyncGenerator
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock


SOURCE = Path(os.environ.get(
    "ASTRBOT_V4293_SOURCE", r"D:\kfhj\AstrBot-v4.29.3-silence-ref",
))
COMMIT = "f61a7f42696da6df25dc7a249ec9b8d54ed9da18"
BLOBS = {
    "message/message_event_result.py": "72dc481a2389675f0bb6cb9821870d8ccc762073",
    "platform/message_session.py": "89639941eb4ba09114dcc9758c70b5856020a1f2",
    "platform/message_type.py": "25b7cdc481de2ab2d788c80307ba848d3ad37b77",
    "platform/astr_message_event.py": "3b700a7c9c75f8ed204dc572a019b1cc7aad9a3d",
    "pipeline/respond/stage.py": "fa90ec70e41f02e13487b4a49b7430181146579e",
    "pipeline/result_decorate/stage.py": "892095e25825b2395131046b37cadfb4172469eb",
    "pipeline/waking_check/stage.py": "31487a468c50bc87967b494d0f358382e513bd36",
    "pipeline/scheduler.py": "45b04ca55867054b6fa32842d8c39cfb92ef4285",
    "star/context.py": "f547daf618df45032a24b618836bdc81138086b6",
    "star/filter/event_message_type.py": "604fc3ed3b781c2e92b053281065c4a4bdc5e9ce",
    "platform/sources/qqofficial/qqofficial_message_event.py": "274932d09e0aaeac172f35e9adbd540aaac4efaa",
    "platform/sources/qqofficial/qqofficial_platform_adapter.py": "46cd25366af7e5d135b79a59fe8a1d2300aac71d",
    "utils/active_event_registry.py": "11aa3054024e097a3deb13f92eb4a040d4cd36f6",
}


class Plain:
    type = "Plain"

    def __init__(self, text=""):
        self.text = text


class BoundaryComponent:
    def __init__(self, **values):
        self.__dict__.update(values)


class BoundaryPlatform:
    async def send_by_session(self, session, message_chain):
        pass


class TransportFailure(Exception):
    pass


class ExactFramework:
    """Execute hash-pinned, unchanged methods with inert service/transport boundaries."""

    def __init__(self):
        component_names = (
            "Plain Face Record Video At AtAll Image Reply Poke Node Nodes File Json "
            "Share Music Forward Location Contact Shake Dice RPS Unknown"
        ).split()
        components = {
            name: type(name, (BoundaryComponent,), {"type": name})
            for name in component_names
        }
        components["Plain"] = Plain
        logger = logging.getLogger("silence-exact-framework")
        logger.disabled = True
        self.hooks = AsyncMock(return_value=False)
        self.handlers = []
        self.ns = {
            "__name__": __name__,
            "abc": abc, "asyncio": asyncio, "copy": copy, "enum": enum,
            "Enum": enum.Enum, "hashlib": hashlib, "math": math, "random": random,
            "traceback": traceback, "uuid": uuid, "defaultdict": defaultdict,
            "AsyncGenerator": AsyncGenerator, "dataclass": dataclass, "field": field,
            "SimpleNamespace": SimpleNamespace, "cast": cast, "logger": logger,
            "deprecated": lambda *args, **kwargs: lambda value: value,
            "register_stage": lambda value: value,
            "Stage": object, "HandlerFilter": object, "Platform": BoundaryPlatform,
            "BaseMessageComponent": BoundaryComponent,
            "Comp": SimpleNamespace(**components),
            "ComponentType": SimpleNamespace(**{name: name for name in component_names}),
            "Metric": SimpleNamespace(upload=AsyncMock()),
            "bounded_text": lambda value, size: value[:size],
            "streaming_response_event": ContextVar("streaming", default=None),
            "current_message_event": ContextVar("current", default=None),
            "call_event_hook": self.hooks,
            "EventType": SimpleNamespace(
                OnAfterMessageSentEvent="after", OnDecoratingResultEvent="decorate",
                AdapterMessageEvent="adapter",
            ),
            "star_handlers_registry": SimpleNamespace(
                get_handlers_by_event_type=lambda *args, **kwargs: self.handlers,
            ),
            "star_map": {"silence.test": SimpleNamespace(name="silence")},
            "MarkdownPayload": lambda **values: values,
            "PlatformMetadata": SimpleNamespace,
            "_QQOFFICIAL_SEND_API_ERRORS": (TransportFailure,),
            "botpy": SimpleNamespace(
                message=SimpleNamespace(**{
                    name: type(name, (), {})
                    for name in ("Message", "GroupMessage", "DirectMessage", "C2CMessage")
                }),
                errors=SimpleNamespace(ServerError=TransportFailure),
            ),
            "PermissionTypeFilter": type("PermissionTypeFilter", (), {}),
            "CommandGroupFilter": type("CommandGroupFilter", (), {}),
            "SessionPluginManager": SimpleNamespace(
                filter_handlers_by_session=AsyncMock(side_effect=lambda event, handlers: handlers),
            ),
            "FILTERS": [], "USER_SESSIONS": {},
            **components,
        }
        self.load("platform/message_type.py", {"MessageType": None})
        self.load("platform/message_session.py", {"MessageSession": None})
        self.ns["MessageSesion"] = self.ns["MessageSession"]
        self.load("message/message_event_result.py", {
            "MessageChain": None, "EventResultType": None,
            "ResultContentType": None, "MessageEventResult": None,
        })
        self.load("platform/astr_message_event.py", {"AstrMessageEvent": {
            "unified_msg_origin", "session_id", "get_platform_name", "get_platform_id",
            "get_message_type", "get_messages", "get_self_id", "get_group_id",
            "get_sender_name", "get_sender_id", "get_extra", "set_extra",
            "set_result", "get_result", "clear_result", "stop_event", "is_stopped",
            "_outline_chain", "send", "send_streaming", "is_private_chat",
        }})
        self.load("platform/sources/qqofficial/qqofficial_message_event.py", {
            "QQOfficialMessageEvent": {
                "send", "send_streaming", "_post_send", "_post_send_one",
                "_close_stream_segment", "_append_stream_delta",
                "_extract_response_message_id", "_split_message_chain_by_media",
                "_parse_to_qqofficial", "_send_with_markdown_fallback",
                "_extract_self_ref_idx",
            },
        })
        self.load("platform/sources/qqofficial/qqofficial_platform_adapter.py", {
            "QQOfficialPlatformAdapter": {
                "send_by_session", "_send_by_session_common",
                "remember_session_message_id", "remember_session_scene",
                "_extract_message_id", "meta", "get_client",
            },
        })
        self.load("pipeline/respond/stage.py", {"RespondStage": {
            "process", "_is_empty_message_chain", "_calc_comp_interval", "_word_cnt",
            "is_seg_reply_required", "_extract_comp",
        }})
        self.load("pipeline/result_decorate/stage.py", {"ResultDecorateStage": {"process"}})
        self.load("pipeline/waking_check/stage.py", {"WakingCheckStage": {"process"}})
        self.load("pipeline/scheduler.py", {"PipelineScheduler": {"_process_stages"}})
        self.load("star/context.py", {"Context": {
            "send_message", "get_platform", "get_platform_inst",
        }})
        self.load("star/filter/event_message_type.py", {
            "EventMessageType": None, "EventMessageTypeFilter": None,
            "MESSAGE_TYPE_2_EVENT_MESSAGE_TYPE": None,
        })
        self.load("utils/active_event_registry.py", {"ActiveEventRegistry": None})

    def load(self, relative, selected):
        path = SOURCE / "astrbot" / "core" / relative
        data = path.read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if blob != BLOBS[relative]:
            raise AssertionError(f"{path} does not match AstrBot {COMMIT}")
        tree = ast.parse(data, filename=str(path))
        body = []
        for node in tree.body:
            name = getattr(node, "name", None)
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
            if name not in selected:
                continue
            if isinstance(node, ast.ClassDef) and selected[name] is not None:
                node.body = [
                    child for child in node.body
                    if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    or child.name in selected[name]
                ]
                node.decorator_list = []
            body.append(node)
        compiled = ast.Module(
            body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *body],
            type_ignores=[],
        )
        ast.fix_missing_locations(compiled)
        exec(compile(compiled, str(path), "exec"), self.ns)

    def event(self, group="group1", sender="member1"):
        event = self.ns["QQOfficialMessageEvent"]()
        raw = self.ns["botpy"].message.GroupMessage()
        raw.group_openid = group
        event.message_obj = SimpleNamespace(
            raw_message=raw, group_id=group, self_id="bot1", message_id="incoming",
            sender=SimpleNamespace(user_id=sender, nickname=sender),
            message=[Plain("ordinary chat")],
        )
        event.message_str = "ordinary chat"
        event.platform_meta = SimpleNamespace(id="qq1", name="qq_official")
        event.session = self.ns["MessageSession"](
            "qq1", self.ns["MessageType"].GROUP_MESSAGE, f"{sender}_{group}",
        )
        event._result = None
        event._force_stopped = False
        event._extras = {}
        event._execution_active = False
        event.plugins_name = None
        event.is_wake = False
        event.is_at_or_wake_command = False
        event.send_buffer = None
        event.trace = SimpleNamespace(record=Mock())
        event.bot = SimpleNamespace(api=SimpleNamespace(
            post_group_message=AsyncMock(return_value={"id": "sent"}),
        ))
        return event

    def result(self, *texts):
        result = self.ns["MessageEventResult"]([Plain(text) for text in texts])
        result.use_markdown_ = False
        return result

    def respond(self, segmented=False):
        stage = self.ns["RespondStage"]()
        stage.config = {}
        stage.platform_settings = {}
        stage.enable_seg = segmented
        stage.only_llm_result = False
        stage.interval_method = "random"
        stage.interval = [0, 0]
        return stage

    def decorator(self, callback):
        self.handlers[:] = [SimpleNamespace(
            handler=callback, handler_module_path="silence.test", handler_name="guard",
        )]
        stage = self.ns["ResultDecorateStage"]()
        stage.content_safe_check_reply = False
        return stage

    def context(self, remembered=True):
        adapter = self.ns["QQOfficialPlatformAdapter"]()
        adapter.config = {"id": "qq1"}
        adapter.client = SimpleNamespace(api=SimpleNamespace(
            post_group_message=AsyncMock(return_value={"id": "sent"}),
        ))
        adapter._session_last_message_id = {}
        adapter._session_scene = {"group1": "group"} if remembered else {}
        adapter._allow_group_proactive_send = True
        adapter.use_markdown_default = False
        context = self.ns["Context"]()
        context.platform_manager = SimpleNamespace(platform_insts=[adapter])
        context.get_config = lambda **kwargs: {}
        return context, adapter

    async def run_stages(self, event, *stages):
        scheduler = self.ns["PipelineScheduler"]()
        scheduler.stages = stages
        await scheduler._process_stages(event)


@unittest.skipUnless(SOURCE.is_dir(), "Set ASTRBOT_V4293_SOURCE to the pinned v4.29.3 source")
class SilenceFrameworkBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.framework = ExactFramework()

    async def test_clear_then_stop_blocks_ordinary_decorated_result(self):
        f = self.framework
        event = f.event()
        event.set_result(f.result("old reply"))

        async def silence(candidate):
            candidate.clear_result()
            candidate.stop_event()

        await f.run_stages(event, f.decorator(silence), f.respond())
        self.assertTrue(event.is_stopped())
        event.bot.api.post_group_message.assert_not_awaited()

    async def test_stop_without_clear_retains_result_and_scheduler_can_send_it(self):
        f = self.framework
        event = f.event()
        event.set_result(f.result("old reply"))

        async def silence(candidate):
            candidate.stop_event()

        await f.run_stages(event, f.decorator(silence), f.respond())
        self.assertTrue(event.is_stopped())
        self.assertEqual(event.bot.api.post_group_message.await_args.kwargs["content"], "old reply")

    async def test_streaming_skips_decorator_and_flushes_after_midstream_stop(self):
        f = self.framework
        event = f.event()
        decorate = AsyncMock()

        async def stream():
            yield f.result("before")
            event.clear_result()
            event.stop_event()
            yield f.result("after")

        result = f.result("stream placeholder")
        result.result_content_type = f.ns["ResultContentType"].STREAMING_RESULT
        result.async_stream = stream()
        event.set_result(result)
        await f.run_stages(event, f.decorator(decorate), f.respond())
        decorate.assert_not_awaited()
        f.hooks.assert_not_awaited()
        self.assertTrue(event.is_stopped())
        self.assertEqual(event.bot.api.post_group_message.await_args.kwargs["content"], "beforeafter")

    async def test_segmented_reply_keeps_sending_after_first_send_stops_event(self):
        f = self.framework
        event = f.event()
        event.set_result(f.result("first", "second"))

        async def transport(**kwargs):
            event.clear_result()
            event.stop_event()
            return {"id": "sent"}

        event.bot.api.post_group_message.side_effect = transport
        await f.respond(segmented=True).process(event)
        self.assertTrue(event.is_stopped())
        self.assertEqual(
            [call.kwargs["content"] for call in event.bot.api.post_group_message.await_args_list],
            ["first", "second"],
        )

    async def test_direct_event_send_has_no_stopped_or_decorator_guard(self):
        f = self.framework
        event = f.event()
        event.clear_result()
        event.stop_event()
        await event.send(f.result("direct reply"))
        event.bot.api.post_group_message.assert_awaited_once()
        f.hooks.assert_not_awaited()

    async def test_context_proactive_send_ignores_stopped_current_event(self):
        f = self.framework
        context, adapter = f.context()
        event = f.event()
        event.stop_event()
        token = f.ns["current_message_event"].set(event)
        try:
            found = await context.send_message(event.unified_msg_origin, f.result("proactive"))
        finally:
            f.ns["current_message_event"].reset(token)
        self.assertTrue(found)
        self.assertIs(context.get_platform_inst("qq1"), adapter)
        self.assertIsNone(context.get_platform("qq1"))
        self.assertEqual(adapter.client.api.post_group_message.await_args.kwargs["group_openid"], "group1")
        f.hooks.assert_not_awaited()

    async def test_cold_qq_context_true_does_not_prove_delivery(self):
        f = self.framework
        context, adapter = f.context(remembered=False)
        session = f.event().unified_msg_origin
        self.assertTrue(await context.send_message(session, f.result("resume")))
        adapter.client.api.post_group_message.assert_not_awaited()
        adapter.remember_session_scene("group1", "group")
        self.assertTrue(await context.send_message(session, f.result("resume")))
        adapter.client.api.post_group_message.assert_awaited_once()

    async def test_qq_fallback_retries_after_stop_without_rechecking(self):
        f = self.framework
        event = f.event()

        async def transport(**payload):
            if "msg_id" in payload:
                event.stop_event()
                raise TransportFailure("stale reply reference")
            return {"id": "sent"}

        event.bot.api.post_group_message.side_effect = transport
        await event.send(f.result("retry"))
        self.assertTrue(event.is_stopped())
        self.assertEqual(event.bot.api.post_group_message.await_count, 2)
        self.assertNotIn("msg_id", event.bot.api.post_group_message.await_args.kwargs)

    async def test_after_sent_hook_runs_even_when_transport_failed(self):
        f = self.framework
        event = f.event()
        event.set_result(f.result("not delivered"))
        event.bot.api.post_group_message.side_effect = RuntimeError("offline")
        await f.respond().process(event)
        f.hooks.assert_awaited_once()
        self.assertIsNone(event.get_result())

    async def test_registry_stops_tracked_tasks_but_not_other_sessions_or_untracked_tasks(self):
        f = self.framework
        registry = f.ns["ActiveEventRegistry"]()
        old, current, other, background = (
            f.event(), f.event(), f.event("othergroup"), f.event(),
        )
        registry.register(old)
        registry.register(current)
        registry.register(other)
        cancelled = Mock()
        registry.register_agent_stop_callback(old, cancelled)
        tracked = asyncio.create_task(asyncio.Event().wait())
        untracked = asyncio.create_task(asyncio.Event().wait())
        other_task = asyncio.create_task(asyncio.Event().wait())
        registry.register_background_task(background, tracked)
        registry.register_background_task(other, other_task)
        await asyncio.sleep(0)
        try:
            self.assertEqual(registry.stop_all(old.unified_msg_origin, exclude=current), 2)
            await asyncio.sleep(0)
            self.assertTrue(old.is_stopped())
            self.assertTrue(background.is_stopped())
            self.assertTrue(tracked.cancelled())
            self.assertTrue(old.get_extra("agent_stop_requested"))
            cancelled.assert_called_once()
            self.assertFalse(current.is_stopped())
            self.assertFalse(other.is_stopped())
            self.assertFalse(other_task.done())
            self.assertFalse(untracked.done())
        finally:
            for task in (tracked, untracked, other_task):
                task.cancel()
            await asyncio.gather(tracked, untracked, other_task, return_exceptions=True)

    async def test_group_type_filter_alone_wakes_unaddressed_group_chat(self):
        f = self.framework
        event = f.event()
        group_filter = f.ns["EventMessageTypeFilter"](f.ns["EventMessageType"].GROUP_MESSAGE)
        f.handlers[:] = [SimpleNamespace(
            event_filters=[group_filter], handler_module_path="silence.test",
            handler_full_name="silence.test.guard",
        )]
        stage = f.ns["WakingCheckStage"]()
        stage.ctx = SimpleNamespace(astrbot_config={"admins_id": [], "wake_prefix": ["/"]})
        stage.ignore_bot_self_message = False
        stage.ignore_at_all = False
        stage.friend_message_needs_wake_prefix = False
        stage.disable_builtin_commands = False
        stage._umo_auto_name_recorder = SimpleNamespace(schedule=Mock())
        await stage.process(event)
        self.assertTrue(event.is_wake)
        self.assertFalse(event.is_at_or_wake_command)
        self.assertFalse(event.is_stopped())
        group_filter.filter = lambda event, cfg: False
        ignored = f.event()
        await stage.process(ignored)
        self.assertFalse(ignored.is_wake)
        self.assertTrue(ignored.is_stopped())


if __name__ == "__main__":
    unittest.main()
