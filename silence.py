import asyncio
import logging
import math
import random
import time
import uuid
from dataclasses import dataclass, field

from .commands import parse_scopes, valid_identity


logger = logging.getLogger(__name__)
DEFAULT_REPLIES = (
    "才说几句就嫌我吵啦？",
    "哼，又多一个嫌我话多的。",
    "我只是想多陪你们聊两句嘛。",
    "好吧，我先把话咽回去。",
    "这票我记在小本本上了。",
)
DEFAULT_START = "好吧，我安静 {seconds} 秒，你们聊。"
DEFAULT_END = "我回来了，刚才的话题继续吧。"


@dataclass(frozen=True)
class SilenceSettings:
    enabled: bool
    scopes: frozenset[tuple[str, str, str]]
    vote_replies: tuple[str, ...]
    start_text: str
    end_text: str

    @classmethod
    def from_config(cls, config):
        replies = config.get("silence_vote_replies", DEFAULT_REPLIES)
        if not isinstance(replies, (list, tuple)):
            replies = DEFAULT_REPLIES
        replies = tuple(
            text.strip()[:80] for text in replies
            if isinstance(text, str) and text.strip()
        ) or DEFAULT_REPLIES

        def template(key, default):
            value = config.get(key, default)
            return value.strip()[:200] if isinstance(value, str) and value.strip() else default

        return cls(
            config.get("silence_enabled") is True,
            parse_scopes(config.get("allowed_groups", [])),
            replies, template("silence_start_text", DEFAULT_START),
            template("silence_end_text", DEFAULT_END),
        )


@dataclass(frozen=True)
class SilenceTarget:
    scope: tuple[str, str, str]
    platform_name: str
    origin: str

    def __post_init__(self):
        if (
            not isinstance(self.scope, tuple) or len(self.scope) != 3
            or not all(valid_identity(v) for v in self.scope)
            or not valid_identity(self.platform_name)
            or ":" in self.scope[0]
            or self.origin != f"{self.scope[0]}:GroupMessage:{self.scope[2]}"
        ):
            raise ValueError("沉默群身份或发送目标无效")


@dataclass
class _Round:
    target: SilenceTarget
    seconds: int
    vote_until: float
    replies: tuple[str, ...]
    start_text: str
    end_text: str
    round_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    voters: set[str] = field(default_factory=set)
    phase: str = "voting"
    deadline: float = 0.0
    mute_until: float = 0.0


class SilenceManager:
    def __init__(
        self, settings, load, save, resume, *,
        wall=time.time, mono=time.monotonic, sleep=asyncio.sleep,
        choose=random.choice, send_timeout=5.0, store_timeout=2.0,
    ):
        self.settings = settings
        self._load, self._save, self._resume = load, save, resume
        self._wall, self._mono, self._sleep = wall, mono, sleep
        self._choose = choose
        self._send_timeout, self._store_timeout = send_timeout, store_timeout
        self._rounds = {}
        self._locks = {}
        self._seen = {}
        self._generations = {}
        self._tasks = set()
        self._votes = set()
        self._timers = {}
        self._targets = {}
        self._store_lock = asyncio.Lock()
        self._closed = False
        self.ready = False

    async def initialize(self):
        if self._closed or not self.settings.enabled or not self.settings.scopes:
            return False
        try:
            saved = await asyncio.wait_for(self._load(), self._store_timeout)
            if self._closed:
                return False
            self._restore(saved)
        except Exception as error:
            logger.warning("沉默状态读取失败，本次加载不启用：%s", type(error).__name__)
            return False
        self.ready = True
        for state in self._rounds.values():
            self._schedule(state)
        return self.ready

    def _restore(self, saved):
        if saved is None:
            return
        if not isinstance(saved, dict) or saved.get("version") != 1:
            raise ValueError("沉默状态版本无效")
        rows = saved.get("groups")
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("沉默群状态无效")
        restored, seen_groups, targets = {}, {}, {}
        now = self._wall()
        for row in rows:
            target = SilenceTarget(
                tuple(row["scope"]), row["platform_name"], row["origin"]
            )
            if target.scope in targets:
                raise ValueError("重复群状态")
            if not self._allowed(target):
                continue
            seen = row["seen"]
            if not isinstance(seen, dict) or not all(
                valid_identity(key) and type(until) in (int, float)
                and math.isfinite(until) for key, until in seen.items()
            ):
                raise ValueError("消息去重状态无效")
            seen_groups[target.scope] = {
                key: until for key, until in seen.items() if until > now
            }
            targets[target.scope] = target
            muted = row.get("muted")
            if muted is None:
                continue
            until, seconds = muted["until"], muted["seconds"]
            end_text = muted["end_text"]
            if (
                type(until) not in (int, float) or not math.isfinite(until)
                or type(seconds) is not int or not 1 <= seconds <= 3600
                or until - now > 3600 + self._send_timeout + self._store_timeout
                or not isinstance(end_text, str) or not 1 <= len(end_text) <= 200
                or not valid_identity(muted["round_id"])
            ):
                raise ValueError("沉默期限无效")
            if until <= now:
                continue
            restored[target.scope] = _Round(
                target, seconds, 0, self.settings.vote_replies,
                self.settings.start_text, end_text,
                round_id=muted["round_id"], phase="muted",
                deadline=self._mono() + until - now, mute_until=until,
            )
        self._rounds, self._seen, self._targets = restored, seen_groups, targets

    async def _persist(self):
        async def write():
            async with self._store_lock:
                rows = []
                for scope, target in self._targets.items():
                    state = self._rounds.get(scope)
                    seen = {
                        key: until for key, until in self._seen.get(scope, {}).items()
                        if until > self._wall()
                    }
                    row = {
                        "scope": list(scope), "platform_name": target.platform_name,
                        "origin": target.origin, "seen": seen, "muted": None,
                    }
                    if state and state.phase != "voting" and state.deadline > self._mono():
                        row["muted"] = {
                            "until": self._wall() + state.deadline - self._mono(),
                            "seconds": state.seconds, "round_id": state.round_id,
                            "end_text": state.end_text,
                        }
                    if seen or row["muted"]:
                        rows.append(row)
                await self._save({"version": 1, "groups": rows})
        try:
            await asyncio.wait_for(write(), self._store_timeout)
            return True
        except Exception as error:
            logger.warning("沉默状态保存失败：%s", type(error).__name__)
            return False

    def is_muted(self, scope):
        state = self._rounds.get(scope)
        return bool(
            self.ready and state and state.phase != "voting"
            and state.deadline > self._mono()
        )

    def _allowed(self, target):
        scope = target.scope
        return scope in self.settings.scopes or (
            target.platform_name == "qq_official"
            and (scope[0], "qq_official", scope[2]) in self.settings.scopes
        )

    async def _send(self, send, *args):
        if self._closed:
            return False
        try:
            await asyncio.wait_for(send(*args), self._send_timeout)
            return True
        except Exception as error:
            logger.warning("沉默控制消息发送失败：%s", type(error).__name__)
            return False

    async def vote(self, target, sender_id, message_id, seconds, reply, on_enter=None):
        task = asyncio.current_task()
        self._votes.add(task)
        try:
            await self._vote(target, sender_id, message_id, seconds, reply, on_enter)
        finally:
            self._votes.discard(task)

    async def _vote(self, target, sender_id, message_id, seconds, reply, on_enter):
        if (
            not self.ready or not self._allowed(target)
            or not valid_identity(sender_id) or not valid_identity(message_id)
            or sender_id == target.scope[1]
        ):
            return
        silent_on_arrival = self.is_muted(target.scope)
        generation = self._generations.get(target.scope, 0)
        async with self._locks.setdefault(target.scope, asyncio.Lock()):
            if self._closed:
                return
            self._targets[target.scope] = target
            state = self._rounds.get(target.scope)
            if state and state.phase != "voting" and state.deadline <= self._mono():
                await self._finish(state)
            seen = self._seen.setdefault(target.scope, {})
            now = self._wall()
            for key in [key for key, until in seen.items() if until <= now]:
                del seen[key]
            if message_id in seen:
                return
            seen[message_id] = (
                now + 120 + 3600 + 120 + self._store_timeout * 2 + self._send_timeout
            )
            if (
                silent_on_arrival or self.is_muted(target.scope)
                or generation != self._generations.get(target.scope, 0)
            ):
                await self._persist()
                return
            if type(seconds) is not int or not 1 <= seconds <= 3600:
                await self._persist()
                await self._send(reply, "用法：/闭嘴 <秒数>，请输入 1–3600 的整数，例如 /闭嘴 10。")
                return
            state = self._rounds.get(target.scope)
            if state is None or (
                state.phase == "voting" and state.vote_until <= self._mono()
            ):
                state = _Round(
                    target, seconds, self._mono() + 120,
                    self.settings.vote_replies,
                    self.settings.start_text, self.settings.end_text,
                )
                self._rounds[target.scope] = state
            if seconds != state.seconds:
                await self._persist()
                await self._send(reply,
                    f"本轮请求安静 {state.seconds} 秒，目前 {len(state.voters)}/5 人，"
                    f"请使用 /闭嘴 {state.seconds}。"
                )
                return
            if sender_id in state.voters:
                await self._persist()
                await self._send(reply,
                    f"你本轮已经投过票了，当前 {len(state.voters)}/5 人，"
                    f"目标 {state.seconds} 秒。"
                )
                return
            state.voters.add(sender_id)
            count = len(state.voters)
            text = self._choose(state.replies)
            if count < 5:
                text += f" 当前 {count}/5 人，希望我安静 {seconds} 秒。"
            else:
                self._generations[target.scope] = generation + 1
                state.phase = "entering"
                fallback = self._store_timeout + self._send_timeout + seconds
                state.deadline = self._mono() + fallback
                state.mute_until = self._wall() + fallback
                self._schedule(state)
                text += f" 当前 5/5 人，安静 {seconds} 秒。"
                text += state.start_text.replace("{seconds}", str(seconds))
                if on_enter is not None:
                    try:
                        await asyncio.wait_for(on_enter(), self._store_timeout)
                    except Exception as error:
                        logger.warning("沉默活动停止失败：%s", type(error).__name__)
                        del self._rounds[target.scope]
                        self._timers[target.scope].cancel()
                        await self._persist()
                        await self._send(reply, "沉默拦截准备失败，本次未进入沉默，请稍后重试。")
                        return
                state.deadline = self._mono() + fallback
                state.mute_until = self._wall() + fallback
                self._schedule(state)
            saved = await self._persist()
            if count == 5 and not saved:
                del self._rounds[target.scope]
                timer = self._timers.get(target.scope)
                if timer:
                    timer.cancel()
                await self._send(reply, "沉默状态保存失败，本次未进入沉默，请稍后重试。")
                return
            await self._send(reply, text)
            if count == 5:
                state.phase = "muted"
                state.deadline = self._mono() + seconds
                state.mute_until = self._wall() + seconds
                self._schedule(state)
                await self._persist()

    def _schedule(self, state):
        previous = self._timers.get(state.target.scope)
        if previous:
            previous.cancel()
        task = asyncio.create_task(self._timer(state))
        self._timers[state.target.scope] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _timer(self, state):
        try:
            while not self._closed and self._rounds.get(state.target.scope) is state:
                remaining = state.deadline - self._mono()
                if remaining > 0:
                    await self._sleep(remaining)
                    continue
                async with self._locks.setdefault(state.target.scope, asyncio.Lock()):
                    await self._finish(state)
                return
        finally:
            if self._timers.get(state.target.scope) is asyncio.current_task():
                self._timers.pop(state.target.scope, None)

    async def _finish(self, state):
        if self._closed or self._rounds.get(state.target.scope) is not state:
            return
        if state.deadline > self._mono():
            return
        del self._rounds[state.target.scope]
        if not await self._persist():
            return
        await self._send(self._resume,
            state.target, state.end_text.replace("{seconds}", str(state.seconds))
        )

    async def close(self):
        self._closed = True
        self.ready = False
        tasks = list((self._tasks | self._votes) - {asyncio.current_task()})
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
