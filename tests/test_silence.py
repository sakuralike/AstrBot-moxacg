import asyncio
import copy
import unittest

from astrbot_plugin_zibll.silence import (
    SilenceManager,
    SilenceSettings,
    SilenceTarget,
)


class Clock:
    def __init__(self):
        self.now = 1000.0
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


class MemoryStore:
    def __init__(self):
        self.value = None

    async def load(self):
        return copy.deepcopy(self.value)

    async def save(self, value):
        self.value = copy.deepcopy(value)


class SilenceManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.store = MemoryStore()
        self.replies = []
        self.resumed = []
        self.target = SilenceTarget(("p", "bot", "group"), "qq_official", "p:GroupMessage:group")
        self.settings = SilenceSettings.from_config({
            "silence_enabled": True,
            "allowed_groups": ["p|bot|group", "p|bot|other"],
            "silence_vote_replies": ["哼。"],
            "silence_start_text": "安静 {seconds} 秒。",
            "silence_end_text": "回来了。",
        })
        self.managers = []
        self.manager = await self.new_manager()

    async def asyncTearDown(self):
        for manager in self.managers:
            await manager.close()

    async def reply(self, text):
        self.replies.append(text)

    async def resume(self, target, text):
        self.resumed.append((target, text))

    async def new_manager(self, **kwargs):
        manager = SilenceManager(
            self.settings, self.store.load, self.store.save, self.resume,
            wall=self.clock.time, mono=self.clock.time, sleep=self.clock.sleep,
            choose=lambda items: items[0], **kwargs,
        )
        self.managers.append(manager)
        await manager.initialize()
        return manager

    async def vote(self, sender, message=None, seconds=10, target=None, **kwargs):
        await self.manager.vote(
            target or self.target, sender, message or sender, seconds,
            self.reply, **kwargs,
        )

    async def test_five_distinct_members_trigger_one_combined_final_reply(self):
        for i in range(1, 6):
            await self.vote(f"user-{i}")
            self.assertEqual(self.manager.is_muted(self.target.scope), i == 5)
        self.assertEqual(len(self.replies), 5)
        self.assertEqual(
            self.replies[-1], "哼。 当前 5/5 人，安静 10 秒。安静 10 秒。"
        )
        await self.vote("user-6")
        self.assertEqual(len(self.replies), 5)

    async def test_replayed_message_and_duplicate_member_do_not_add_votes(self):
        await self.vote("same", "message-1")
        await self.vote("different", "message-1")
        self.assertEqual(len(self.replies), 1)
        for number in range(2, 6):
            await self.vote("same", f"message-{number}")
        self.assertFalse(self.manager.is_muted(self.target.scope))
        self.assertTrue(all("已经投过票" in text for text in self.replies[1:]))
        await self.vote("new", "message-6")
        self.assertIn("2/5", self.replies[-1])

    async def test_duration_is_locked_and_only_positive_integer_seconds_count(self):
        for index, value in enumerate((None, 0, -1, 1.5, True, "10", 3601)):
            await self.vote("invalid", f"invalid-{index}", seconds=value)
            self.assertIn("1–3600", self.replies[-1])
        await self.vote("first", seconds=10)
        await self.vote("second", seconds=60)
        self.assertIn("/闭嘴 10", self.replies[-1])
        await self.vote("second", "second-correct", seconds=10)
        self.assertIn("2/5", self.replies[-1])

    async def test_vote_window_expires_without_extending_or_reusing_old_messages(self):
        await self.vote("first")
        await self.clock.advance(119)
        await self.vote("second")
        self.assertIn("2/5", self.replies[-1])
        await self.clock.advance(1)
        await self.vote("third")
        self.assertIn("1/5", self.replies[-1])
        count = len(self.replies)
        await self.vote("first")
        self.assertEqual(len(self.replies), count)

    async def test_expiration_restores_once_and_silent_votes_do_not_extend_it(self):
        for number in range(5):
            await self.vote(f"u-{number}")
        await self.clock.advance(9)
        await self.vote("late")
        self.assertTrue(self.manager.is_muted(self.target.scope))
        self.assertEqual(len(self.replies), 5)
        await self.clock.advance(1)
        self.assertFalse(self.manager.is_muted(self.target.scope))
        self.assertEqual(self.resumed, [(self.target, "回来了。")])
        await self.clock.advance(30)
        self.assertEqual(len(self.resumed), 1)
        await self.vote("new")
        self.assertIn("1/5", self.replies[-1])

    async def test_concurrent_votes_are_ordered_and_silence_starts_before_final_send(self):
        gates = []

        async def slow_reply(text):
            await asyncio.sleep(0)
            gates.append((text, self.manager.is_muted(self.target.scope)))

        await asyncio.gather(*(
            self.manager.vote(self.target, f"u-{n}", f"m-{n}", 10, slow_reply)
            for n in range(8)
        ))
        self.assertEqual(len(gates), 5)
        self.assertEqual([muted for _, muted in gates], [False] * 4 + [True])
        self.assertTrue(all(f"{n}/5" in gates[n - 1][0] for n in range(1, 6)))

    async def test_queued_sixth_vote_is_discarded_even_after_short_silence_expires(self):
        await self.manager.close()
        first_send = asyncio.Event()
        release_first_send = asyncio.Event()
        final_save = asyncio.Event()
        release_final_save = asyncio.Event()
        writes = 0

        async def slow_save(snapshot):
            nonlocal writes
            writes += 1
            if writes == 6:
                final_save.set()
                await release_final_save.wait()
            await self.store.save(snapshot)

        async def slow_first_reply(text):
            if not first_send.is_set():
                first_send.set()
                await release_first_send.wait()
            self.replies.append(text)

        self.manager = SilenceManager(
            self.settings, self.store.load, slow_save, self.resume,
            wall=self.clock.time, mono=self.clock.time, sleep=self.clock.sleep,
        )
        self.managers.append(self.manager)
        await self.manager.initialize()
        tasks = [asyncio.create_task(self.manager.vote(
            self.target, "u-0", "m-0", 1, slow_first_reply
        ))]
        await first_send.wait()
        for number in range(1, 6):
            tasks.append(asyncio.create_task(self.manager.vote(
                self.target, f"u-{number}", f"m-{number}", 1, slow_first_reply
            )))
            await asyncio.sleep(0)
        release_first_send.set()
        await final_save.wait()
        await self.clock.advance(2)
        release_final_save.set()
        await asyncio.gather(*tasks)
        self.assertFalse(self.manager.is_muted(self.target.scope))
        self.assertEqual(len(self.replies), 5)
        self.assertEqual(len(self.resumed), 1)
        await self.vote("u-5", "m-5", seconds=1)
        self.assertEqual(len(self.replies), 5)
        await self.vote("new", seconds=1)
        self.assertIn("1/5", self.replies[-1])

    async def test_reply_failures_do_not_trap_silence(self):
        async def broken_reply(text):
            raise OSError("offline")

        for number in range(5):
            await self.manager.vote(
                self.target, f"u-{number}", f"m-{number}", 10, broken_reply
            )
        self.assertTrue(self.manager.is_muted(self.target.scope))
        await self.clock.advance(10)
        self.assertFalse(self.manager.is_muted(self.target.scope))
        self.assertEqual(len(self.resumed), 1)

    async def test_reload_resumes_remaining_duration_and_keeps_message_deduplication(self):
        for number in range(5):
            await self.vote(f"u-{number}")
        await self.clock.advance(3)
        await self.manager.close()
        self.manager = await self.new_manager()
        self.assertTrue(self.manager.is_muted(self.target.scope))
        await self.clock.advance(6)
        self.assertTrue(self.manager.is_muted(self.target.scope))
        await self.clock.advance(1)
        self.assertFalse(self.manager.is_muted(self.target.scope))
        self.assertEqual(len(self.resumed), 1)
        count = len(self.replies)
        await self.vote("u-0")
        self.assertEqual(len(self.replies), count)

    async def test_enter_callback_runs_once_while_muted_before_final_reply(self):
        entered = []

        async def on_enter():
            entered.append((self.manager.is_muted(self.target.scope), len(self.replies)))

        for number in range(6):
            await self.vote(f"u-{number}", on_enter=on_enter)
        self.assertEqual(entered, [(True, 4)])

    async def test_failed_enter_guard_aborts_without_claiming_silence(self):
        async def broken_enter():
            raise OSError("guard unavailable")

        for number in range(4):
            await self.vote(f"u-{number}")
        await self.vote("last", on_enter=broken_enter)
        self.assertFalse(self.manager.is_muted(self.target.scope))
        self.assertIn("未进入沉默", self.replies[-1])
        await self.clock.advance(30)
        self.assertEqual(self.resumed, [])

    async def test_unfinished_votes_reset_on_reload_but_expired_silence_is_not_announced(self):
        await self.vote("first")
        await self.manager.close()
        self.manager = await self.new_manager()
        await self.vote("second")
        self.assertIn("1/5", self.replies[-1])
        count = len(self.replies)
        await self.vote("first")
        self.assertEqual(len(self.replies), count)
        for number in range(4):
            await self.vote(f"other-{number}")
        await self.manager.close()
        await self.clock.advance(11)
        self.manager = await self.new_manager()
        self.assertFalse(self.manager.is_muted(self.target.scope))
        self.assertEqual(self.resumed, [])

    async def test_initial_read_failure_or_invalid_state_disables_only_this_manager(self):
        for value in ({}, {"version": 1, "groups": [{"scope": []}]}):
            self.store.value = value
            manager = await self.new_manager()
            self.assertFalse(manager.ready)

        async def broken_load():
            raise OSError("offline")

        manager = SilenceManager(self.settings, broken_load, self.store.save, self.resume)
        self.managers.append(manager)
        self.assertFalse(await manager.initialize())
        self.assertFalse(manager.is_muted(self.target.scope))

    async def test_entering_write_failure_aborts_silence(self):
        for number in range(4):
            await self.vote(f"u-{number}")

        async def broken_save(snapshot):
            raise OSError("offline")

        manager = SilenceManager(
            self.settings, self.store.load, broken_save, self.resume,
            wall=self.clock.time, mono=self.clock.time, sleep=self.clock.sleep,
        )
        self.managers.append(manager)
        await manager.initialize()
        for number in range(5):
            await manager.vote(self.target, f"new-{number}", f"new-{number}", 10, self.reply)
        self.assertFalse(manager.is_muted(self.target.scope))
        self.assertIn("未进入沉默", self.replies[-1])
        await self.clock.advance(30)
        self.assertEqual(self.resumed, [])

    async def test_close_cancels_inflight_final_send_and_preserves_a_bounded_deadline(self):
        entered = asyncio.Event()

        async def slow_final(text):
            entered.set()
            await asyncio.Event().wait()
            self.replies.append(text)

        for number in range(4):
            await self.vote(f"u-{number}")
        task = asyncio.create_task(self.manager.vote(
            self.target, "last", "last", 10, slow_final
        ))
        await entered.wait()
        await self.manager.close()
        self.assertTrue(task.done())
        self.assertEqual(len(self.replies), 4)
        self.manager = await self.new_manager()
        self.assertTrue(self.manager.is_muted(self.target.scope))
        await self.clock.advance(17)
        self.assertFalse(self.manager.is_muted(self.target.scope))

    async def test_close_during_initial_load_does_not_restore_or_schedule_silence(self):
        for number in range(5):
            await self.vote(f"u-{number}")
        await self.manager.close()
        loading = asyncio.Event()
        released = asyncio.Event()

        async def slow_load():
            loading.set()
            await released.wait()
            return await self.store.load()

        manager = SilenceManager(
            self.settings, slow_load, self.store.save, self.resume,
            wall=self.clock.time, mono=self.clock.time, sleep=self.clock.sleep,
        )
        self.managers.append(manager)
        task = asyncio.create_task(manager.initialize())
        await loading.wait()
        await manager.close()
        released.set()
        self.assertFalse(await task)
        self.assertFalse(manager.ready)
        self.assertFalse(manager.is_muted(self.target.scope))
        self.assertFalse(manager._tasks)
        await self.clock.advance(20)
        self.assertEqual(self.resumed, [])

    async def test_persisted_origin_cannot_redirect_recovery_to_another_group(self):
        for number in range(5):
            await self.vote(f"u-{number}")
        await self.manager.close()
        self.store.value["groups"][0]["origin"] = "p:GroupMessage:other"
        self.manager = await self.new_manager()
        self.assertFalse(self.manager.ready)
        await self.clock.advance(11)
        self.assertEqual(self.resumed, [])

    async def test_send_and_storage_waits_timeout_instead_of_sticking_in_entering(self):
        await self.manager.close()
        self.manager = await self.new_manager(send_timeout=0.01, store_timeout=0.01)

        async def hung_reply(text):
            await asyncio.Event().wait()

        for number in range(4):
            await self.vote(f"u-{number}")
        await asyncio.wait_for(self.manager.vote(
            self.target, "last", "last", 10, hung_reply
        ), 0.3)
        self.assertTrue(self.manager.is_muted(self.target.scope))
        await self.clock.advance(10)
        self.assertFalse(self.manager.is_muted(self.target.scope))
        self.assertEqual(len(self.resumed), 1)

        async def hung_load():
            await asyncio.Event().wait()

        manager = SilenceManager(
            self.settings, hung_load, self.store.save, self.resume, store_timeout=0.01
        )
        self.managers.append(manager)
        self.assertFalse(await asyncio.wait_for(manager.initialize(), 0.3))

    async def test_groups_and_bots_are_isolated_and_out_of_scope_votes_are_ignored(self):
        other = SilenceTarget(("p", "bot", "other"), "qq_official", "p:GroupMessage:other")
        outsider = SilenceTarget(("p", "wrong", "group"), "qq_official", "p:GroupMessage:group")
        await self.vote("other-user", target=other)
        for number in range(5):
            await self.vote(f"u-{number}")
        self.assertTrue(self.manager.is_muted(self.target.scope))
        self.assertFalse(self.manager.is_muted(other.scope))
        count = len(self.replies)
        await self.vote("outsider", target=outsider)
        await self.vote("bot", "self-msg", target=other)
        self.assertEqual(len(self.replies), count)
        await self.vote("other-second", target=other)
        self.assertIn("2/5", self.replies[-1])

    async def test_two_groups_persist_without_overwriting_each_other(self):
        other = SilenceTarget(("p", "bot", "other"), "qq_official", "p:GroupMessage:other")
        await asyncio.gather(*(
            self.manager.vote(target, f"u-{n}", f"msg-{n}", 10, self.reply)
            for target in (self.target, other) for n in range(5)
        ))
        await self.manager.close()
        self.manager = await self.new_manager()
        self.assertTrue(self.manager.is_muted(self.target.scope))
        self.assertTrue(self.manager.is_muted(other.scope))
        await self.clock.advance(10)
        self.assertEqual({target.scope for target, _ in self.resumed}, {self.target.scope, other.scope})

    async def test_config_defaults_and_unknown_placeholders_are_safe(self):
        settings = SilenceSettings.from_config({
            "silence_enabled": True, "allowed_groups": ["p|bot|group"],
            "silence_vote_replies": ["", None],
            "silence_start_text": "{unknown} {seconds}",
            "silence_end_text": "",
        })
        self.assertTrue(settings.vote_replies)
        self.assertTrue(settings.end_text)
        self.manager.settings = settings
        for number in range(5):
            await self.vote(f"u-{number}", seconds=1)
        self.assertIn("{unknown} 1", self.replies[-1])
        await self.clock.advance(1)
        self.assertFalse(self.manager.is_muted(self.target.scope))

    async def test_resume_failure_restores_response_without_retrying(self):
        attempts = []

        async def broken_resume(target, text):
            attempts.append(text)
            raise OSError("platform refused")

        await self.manager.close()
        self.manager = SilenceManager(
            self.settings, self.store.load, self.store.save, broken_resume,
            wall=self.clock.time, mono=self.clock.time, sleep=self.clock.sleep,
        )
        self.managers.append(self.manager)
        await self.manager.initialize()
        for number in range(5):
            await self.vote(f"u-{number}")
        await self.clock.advance(10)
        self.assertFalse(self.manager.is_muted(self.target.scope))
        await self.clock.advance(100)
        self.assertEqual(attempts, ["回来了。"])
        await self.vote("new")
        self.assertIn("1/5", self.replies[-1])

    async def test_final_persist_failure_keeps_in_memory_deadline(self):
        writes = 0

        async def failing_final_save(snapshot):
            nonlocal writes
            writes += 1
            if writes == 6:
                raise OSError("disk unavailable")
            await self.store.save(snapshot)

        await self.manager.close()
        self.manager = SilenceManager(
            self.settings, self.store.load, failing_final_save, self.resume,
            wall=self.clock.time, mono=self.clock.time, sleep=self.clock.sleep,
        )
        self.managers.append(self.manager)
        await self.manager.initialize()
        for number in range(5):
            await self.vote(f"u-{number}")
        self.assertTrue(self.manager.is_muted(self.target.scope))
        await self.clock.advance(10)
        self.assertFalse(self.manager.is_muted(self.target.scope))
        self.assertEqual(self.resumed, [(self.target, "回来了。")])


if __name__ == "__main__":
    unittest.main()
