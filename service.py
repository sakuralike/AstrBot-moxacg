import asyncio
import hashlib
import html
import json
import math
import re
import time
import unicodedata
from dataclasses import asdict
from urllib.parse import parse_qs, urlsplit

from .avatar import fetch_avatar
from .client import BridgeClient, BridgeError
from .commands import Actor, CommandMatch

ERROR_MESSAGES = {
    "not_bound": "尚未绑定网站账号，请先发起网站绑定。",
    "already_bound": "已有绑定，请先解绑后再更换账号。",
    "challenge_expired": "绑定链接已过期或失效，请重新发起绑定。",
    "candidate_mismatch": "UID 与网站授权账号不符，未完成绑定。请先查看待确认账号。",
    "candidate_locked": "该链接已有候选账号，请重新发起绑定。",
    "binding_conflict": "该网站账号在本群已有绑定，不能覆盖。",
    "website_authorization_required": "请先打开绑定链接，在网站登录并授权。",
    "confirmation_expired": "解绑确认已过期，请重新发送解绑指令。",
    "binding_changed": "绑定状态已变化，请重新查询。",
    "account_unavailable": "网站账号已失效，请撤销旧授权后重新绑定。",
    "account_restricted": "网站账号处于限制状态，无法执行此操作。",
    "checkin_disabled": "网站尚未开启签到。",
    "busy": "网站正在处理同一账号的操作，请稍后查询。",
    "rate_limited": "操作过于频繁，请稍后再试。",
    "event_expired": "这条消息已过期，未执行签到，请重新发送指令。",
    "event_identity_required": "适配器未提供有效消息 ID 或时间，无法安全执行签到。",
    "request_conflict": "消息标识冲突，已停止重复操作。",
    "authentication_failed": "网站认证失败，请管理员检查密钥和服务器时间。",
    "forbidden": "网站拒绝请求，请管理员检查群白名单及 HTTPS 配置。",
    "unavailable": "网站桥接或主题能力暂不可用，请管理员检查。",
    "network_error": "网站连接超时或失败，请稍后查询。",
    "invalid_parameters": "指令参数无效，请检查输入。",
    "code_invalid": "授权码无效、已过期或不属于本次绑定，请核对本人的授权页面。",
    "session_expired": "本次绑定会话已结束，请重新发送绑定指令。",
    "too_many_attempts": "本次授权码错误次数已达上限，请重新发起绑定。",
    "binding_upgrade_required": "网站桥接尚不支持六位授权码，请管理员先更新网站端插件；其他网站功能不受影响。",
    "binding_event_required": "适配器未提供有效消息 ID 或时间，无法安全绑定，请管理员检查。",
    "admin_required": "仅 AstrBot 管理员可以执行积分操作。",
    "target_not_bound": "目标群员尚未绑定网站账号，无法操作积分。",
    "points_disabled": "网站积分功能未开启。",
    "insufficient_points": "目标用户积分不足，未执行扣分。",
    "points_busy": "网站正在处理该用户的积分操作，请稍后再试。",
    "points_uncertain": "积分操作结果需要核实，已停止重复执行；请在网站查看积分记录。",
    "points_update_failed": "网站积分更新失败，未确认完成，请稍后查询。",
    "points_update_uncertain": "积分操作结果需要核实，已停止重复执行；请在网站查看积分记录。",
    "uncertain": "积分操作结果需要核实，已停止重复执行；请在网站查看积分记录。",
    "blindbox_disabled": "盲盒功能当前未开放。",
    "blindbox_config_invalid": "盲盒配置无效，请联系管理员检查网站设置。",
    "blindbox_no_chances": "开盲盒次数不足，请稍后再试。",
    "blindbox_exchange_limit": "今日兑换次数已达上限，本次未扣积分。",
    "blindbox_unresolved": "盲盒操作结果未知，未自动重复执行；请稍后查询或联系管理员核对。",
}


def safe_text(value: object, limit: int = 160) -> str:
    if not isinstance(value, (str, int)):
        raise BridgeError("invalid_response")
    text = re.sub(r"<[^>]*>", "", html.unescape(str(value)))
    text = "".join(c for c in text if not unicodedata.category(c).startswith("C"))
    return text.replace("[", "［").replace("]", "］").replace("@", "＠")[:limit]


def safe_link(value: object, public_site: str, binding: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        raise BridgeError("invalid_response")
    url, public = urlsplit(value), urlsplit(public_site)
    if url.scheme != public.scheme or url.netloc != public.netloc or url.username is not None:
        raise BridgeError("invalid_response")
    if any(c in value for c in "\\[]"):
        raise BridgeError("invalid_response")
    if binding:
        query = parse_qs(url.query, keep_blank_values=True)
        if (
            set(query) != {"az_bind"} or len(query["az_bind"]) != 1
            or not re.fullmatch(r"[0-9a-f]{64}", query["az_bind"][0])
            or url.fragment or url.path != public.path.rstrip("/") + "/"
        ):
            raise BridgeError("invalid_response")
    return value


def number(data: dict, key: str, disabled: str = "网站未启用") -> str:
    value = data.get(key)
    if value is None:
        return disabled
    if type(value) is not int:
        raise BridgeError("invalid_response")
    return str(value)


def checkin_text(data: dict) -> str:
    state = data.get("state")
    if state in {"uncertain", "pending"}:
        return "签到结果需要核实，已停止重复执行；请在网站查看签到记录，必要时联系站长核对奖励。"
    if state == "not_started":
        return "未查到本次签到操作记录，本次不自动重试。请先到网站查看签到状态。"
    labels = {"completed": "签到完成", "already_checked_in": "今日已签到"}
    if state not in labels:
        raise BridgeError("invalid_response")
    return (
        f"{labels[state]}（网站日期：{safe_text(data.get('site_day'))}）\n"
        f"当前积分：{number(data, 'points')}\n"
        f"连续签到：{number(data, 'continuous_days')} 天\n"
        f"累计签到：{number(data, 'total_days')} 天"
    )


def points_event_identity(event, actor: Actor) -> dict:
    return event_identity(event, actor, "points")


def parse_points_amount(argument: str, limit: int) -> int:
    if not isinstance(argument, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)", argument):
        raise BridgeError("invalid_parameters")
    try:
        amount = int(argument, 10)
    except (TypeError, ValueError, OverflowError):
        raise BridgeError("invalid_parameters") from None
    if amount <= 0 or amount > limit:
        raise BridgeError("invalid_parameters")
    return amount


def points_text(
    data: dict,
    action: str,
    requested_amount: int,
    expected_request_id: str,
) -> str:
    if action not in {"points_add", "points_subtract"}:
        raise BridgeError("invalid_response")
    state = data.get("state")
    if state in {"uncertain", "pending"}:
        return "积分操作结果需要核实，已停止重复执行；请在网站查看积分记录。"
    if state in {"not_processed", "not_started"}:
        return "尚未查到本次积分操作记录，未自动重试；请先在网站核对积分。"
    if state == "rejected":
        error_code = data.get("error_code")
        if not isinstance(error_code, str):
            raise BridgeError("invalid_response")
        raise BridgeError(error_code)
    if state != "completed":
        raise BridgeError("invalid_response")
    if (
        data.get("request_id") != expected_request_id
        or data.get("action") != ("add" if action == "points_add" else "subtract")
    ):
        raise BridgeError("invalid_response")
    nickname = safe_text(data.get("nickname"))
    uid = number(data, "uid")
    if int(uid) <= 0:
        raise BridgeError("invalid_response")
    amount = data.get("amount")
    if type(amount) is not int or amount <= 0:
        raise BridgeError("invalid_response")
    if amount != requested_amount:
        raise BridgeError("invalid_response")
    points_before = data.get("points_before")
    points_after = data.get("points_after")
    if (
        type(points_before) is not int or points_before < 0
        or type(points_after) is not int or points_after < 0
        or points_after != (
            points_before + amount
            if action == "points_add"
            else points_before - amount
        )
    ):
        raise BridgeError("invalid_response")
    label = "加积分" if action == "points_add" else "减积分"
    return (
        f"{label}完成：{nickname}（UID：{uid}）\n"
        f"变更积分：{amount}\n当前积分：{points_after}"
    )


def _blindbox_count(data: dict, key: str) -> int:
    value = data.get(key)
    if type(value) is not int or value < 0:
        raise BridgeError("invalid_response")
    return value


def _blindbox_snapshot(data: dict) -> tuple[int, int, int]:
    free_remaining = _blindbox_count(data, "free_remaining")
    exchange_remaining = _blindbox_count(data, "exchange_remaining")
    available = _blindbox_count(data, "available")
    if available != free_remaining + exchange_remaining:
        raise BridgeError("invalid_response")
    return free_remaining, exchange_remaining, available


def blindbox_text(
    data: dict,
    action: str,
    expected_request_id: str,
    exchange_command: str,
    open_command: str,
) -> str:
    if action not in {"blindbox_open", "blindbox_exchange"}:
        raise BridgeError("invalid_response")
    if data.get("request_id") != expected_request_id:
        raise BridgeError("invalid_response")
    state = data.get("state")
    if (
        state in {"completed", "rejected"}
        and data.get("action") != ("open" if action == "blindbox_open" else "exchange")
    ):
        raise BridgeError("invalid_response")
    if state in {"pending", "uncertain"}:
        return "盲盒操作结果需要核实，已停止重复执行；请稍后查询或联系管理员核对。"
    if state in {"not_started"}:
        return "盲盒操作结果暂未确认，未自动重复执行；请稍后查询或联系管理员核对。"
    if state == "rejected":
        error_code = data.get("error_code")
        if not isinstance(error_code, str):
            raise BridgeError("invalid_response")
        if error_code == "blindbox_disabled":
            return "盲盒功能当前未开放。"
        if error_code == "blindbox_config_invalid":
            return "盲盒配置无效，请联系管理员检查网站设置。"
        if error_code == "blindbox_no_chances":
            exchange_limit = data.get("exchange_limit")
            exchange_bought = data.get("exchange_bought")
            cost = data.get("cost_points", data.get("exchange_cost_points"))
            if (
                type(exchange_limit) is int and exchange_limit >= 0
                and type(exchange_bought) is int and 0 <= exchange_bought <= exchange_limit
                and type(cost) is int and cost > 0
            ):
                remaining_exchange = exchange_limit - exchange_bought
                if remaining_exchange:
                    return (
                        "开盲盒次数不足。\n"
                        f"发送 {exchange_command} 可消耗 {cost} 积分兑换 1 次。\n"
                        f"今日还可兑换 {remaining_exchange} 次，兑换次数仅今日有效。"
                    )
                return (
                    "今日开盲盒次数已用完，"
                    f"今日兑换也已达到 {exchange_limit} 次上限。"
                )
            return "开盲盒次数不足，请稍后再试。"
        if error_code == "blindbox_exchange_limit":
            exchange_limit = data.get("exchange_limit")
            exchange_bought = data.get("exchange_bought")
            if (
                type(exchange_limit) is int and exchange_limit >= 0
                and type(exchange_bought) is int and 0 <= exchange_bought <= exchange_limit
            ):
                return (
                    f"今日兑换次数已达上限：{exchange_bought}/{exchange_limit} 次。\n"
                    "本次未扣积分。"
                )
            return "今日兑换次数已达上限，本次未扣积分。"
        if error_code == "insufficient_points":
            cost = data.get("cost_points")
            if type(cost) is int and cost > 0:
                return (
                    f"积分不足，兑换 1 次需要 {cost} 积分。\n"
                    "本次未扣积分，未增加次数。"
                )
            return "积分不足，本次未扣积分，未增加次数。"
        raise BridgeError(error_code)
    if state != "completed":
        raise BridgeError("invalid_response")
    uid = data.get("uid")
    if type(uid) is not int or uid <= 0:
        raise BridgeError("invalid_response")
    site_day = safe_text(data.get("site_day"), 32)
    timezone = safe_text(data.get("timezone"), 64)
    free_remaining, exchange_remaining, available = _blindbox_snapshot(data)
    if action == "blindbox_open":
        prize = data.get("prize")
        if not isinstance(prize, dict):
            raise BridgeError("invalid_response")
        prize_name = safe_text(prize.get("name"), 80)
        if not prize_name:
            raise BridgeError("invalid_response")
        prize_type = prize.get("type")
        if prize_type == "points":
            points = prize.get("points", data.get("points_delta"))
            if type(points) is not int or points <= 0:
                raise BridgeError("invalid_response")
            points_before = data.get("points_before")
            points_after = data.get("points_after")
            if (
                type(points_before) is not int or points_before < 0
                or type(points_after) is not int or points_after < 0
                or points_after != points_before + points
            ):
                raise BridgeError("invalid_response")
            reward = f"获得：{points} 积分"
        elif prize_type == "text":
            text_prize = safe_text(prize.get("text"), 500)
            if not text_prize:
                raise BridgeError("invalid_response")
            reward = f"获得：{text_prize}"
        else:
            raise BridgeError("invalid_response")
        return (
            "盲盒开启成功！\n"
            f"{reward}\n"
            f"今日剩余：{available} 次（免费 {free_remaining} 次，兑换 {exchange_remaining} 次）"
        )
    cost = data.get("cost_points", data.get("exchange_cost_points"))
    before = data.get("points_before")
    after = data.get("points_after")
    bought = data.get("exchange_bought")
    limit = data.get("exchange_limit")
    if (
        type(cost) is not int or cost <= 0
        or type(before) is not int or before < 0
        or type(after) is not int or after < 0
        or after != before - cost
        or type(bought) is not int or bought < 0
        or type(limit) is not int or limit < 0 or bought > limit
    ):
        raise BridgeError("invalid_response")
    return (
        "兑换成功！\n"
        f"消耗：{cost} 积分\n"
        "增加：1 次开盲盒机会\n"
        f"今日剩余：{available} 次（免费 {free_remaining} 次，兑换 {exchange_remaining} 次）\n"
        f"今日已兑换：{bought}/{limit} 次\n"
        f"有效期：{site_day}（{timezone}）\n"
        f"发送 {open_command} 使用。"
    )


def event_identity(event, actor: Actor, action: str = "checkin") -> dict:
    message = getattr(event, "message_obj", None)
    message_id = getattr(message, "message_id", None)
    timestamp = getattr(message, "timestamp", None)
    if (
        not isinstance(message_id, (str, int)) or isinstance(message_id, bool)
        or not str(message_id) or len(str(message_id)) > 256
        or type(timestamp) not in {int, float} or not math.isfinite(timestamp)
        or timestamp != int(timestamp)
        or abs(time.time() - timestamp) > 180
    ):
        raise BridgeError("event_identity_required")
    key = json.dumps([asdict(actor), action, str(message_id)], sort_keys=True, ensure_ascii=False)
    return {"request_id": hashlib.sha256(key.encode()).hexdigest(), "event_time": int(timestamp)}


async def execute_blindbox(
    client: BridgeClient,
    action: str,
    actor: Actor,
    event,
) -> str:
    if action not in {"blindbox_open", "blindbox_exchange"}:
        raise BridgeError("invalid_parameters")
    route_name = "blindbox/open" if action == "blindbox_open" else "blindbox/exchange"
    identity = event_identity(event, actor, route_name)
    body = {"actor": asdict(actor), **identity}
    query_status = False
    try:
        data = await client.request("/" + route_name, body)
        if data.get("request_id") != identity["request_id"]:
            raise BridgeError("invalid_response")
    except BridgeError as error:
        if error.code not in {
            "network_error", "http_error", "unavailable",
            "invalid_response", "response_too_large", "uncertain",
            "blindbox_unresolved",
        }:
            raise
        query_status = True
    if not query_status and data.get("state") in {"pending", "uncertain"}:
        query_status = True
    if query_status:
        try:
            data = await client.request("/blindbox/status", {
                "actor": asdict(actor),
                "request_id": identity["request_id"],
            })
        except BridgeError:
            return "盲盒操作结果未知，未自动重复执行；请稍后查询或联系管理员核对。"
    return blindbox_text(
        data,
        action,
        identity["request_id"],
        client.settings.router.commands["blindbox_exchange"],
        client.settings.router.commands["blindbox_open"],
    )


async def execute(
    client: BridgeClient, match: CommandMatch, actor: Actor, event,
) -> tuple[str, bytes | None] | list[tuple[str, bytes | None]]:
    settings = client.settings
    commands = settings.router.commands
    payload = {"actor": asdict(actor)}
    action, argument = match.action, match.argument
    if action == "bind":
        if not client.binding_code_available:
            raise BridgeError("binding_upgrade_required")
        identity = binding_event_identity(event, actor, "binding/code/start")
        try:
            data = await client.request("/binding/code/start", {**payload, **identity})
        except BridgeError as error:
            if error.code in {"network_error", "http_error", "unavailable", "invalid_response", "response_too_large"}:
                client.pending_bindings.pop(actor, None)
            raise
        if data.get("state") == "bound":
            client.pending_bindings.pop(actor, None)
            return binding_success(data, settings.public_site_url), None
        if data.get("state") == "already_started":
            return "这条绑定消息已处理，请使用先前的授权链接；没有收到链接时，请重新发送一条绑定指令。", None
        client.pending_bindings.pop(actor, None)
        session_id, expires_in = data.get("session_id"), data.get("expires_in")
        if (
            data.get("state") != "pending" or not isinstance(session_id, str)
            or not re.fullmatch(r"[0-9a-f]{64}", session_id)
            or type(expires_in) is not int or not 0 < expires_in <= 600
        ):
            raise BridgeError("invalid_response")
        link = safe_link(data.get("url"), settings.public_site_url, True)
        if hashlib.sha256(parse_qs(urlsplit(link).query)["az_bind"][0].encode()).hexdigest() != session_id:
            raise BridgeError("invalid_response")
        client.pending_bindings[actor] = (session_id, time.monotonic() + expires_in)
        return (
            f"请打开网站授权页，登录后获取六位授权码：\n{link}\n"
            "请在 10 分钟内完成，生成的码最多有效 5 分钟。\n"
            "核对网站账号后，回本群 @机器人 发送页面上的六位码。\n"
            "不要使用他人发给你的码；插件重载后需重新发起绑定。", None
        )
    if action == "confirm":
        return f"无需再确认 UID。请发送 {commands['bind']}，登录网站后回原群 @机器人 发送六位授权码。", None
    if action == "unbind":
        return f"请登录网站授权管理页，点击对应群聊的撤销按钮：\n{settings.public_site_url}/?az_bindings=1", None
    if action == "binding_code":
        return await redeem_code(client, actor, argument, event), None
    if action == "profile":
        data = await client.request("/profile", payload)
        text = (
            f"昵称：{safe_text(data.get('nickname'))}\nUID：{number(data, 'uid')}\n"
            f"加入网站天数：{number(data, 'joined_days', '不可用')}\n"
            f"等级：{('Lv.' + number(data, 'level')) if data.get('level') is not None else '网站未启用'}\n"
            f"积分：{number(data, 'points')}\n已发布文章：{number(data, 'post_count')} 篇\n"
            f"评论：{number(data, 'comment_count')} 条"
        )
        try:
            image = await fetch_avatar(data.get("avatar_url", ""), settings)
        except BridgeError:
            return text + "\n头像暂不可用，请检查头像域名白名单。", None
        return text, image
    if action == "checkin":
        body = {**payload, **event_identity(event, actor)}
        try:
            data = await client.request("/checkin", body)
        except BridgeError as error:
            if error.code not in {"network_error", "http_error", "unavailable", "invalid_response", "response_too_large"}:
                raise
            try:
                data = await client.request("/checkin/status", body)
            except BridgeError:
                return "签到请求结果未知，未自动重试；请先在网站查看记录并核对奖励。", None
        return checkin_text(data), None
    if action in {"blindbox_open", "blindbox_exchange"}:
        if argument:
            return f"用法：{commands[action]}（不带参数，每次操作 1 次）", None
        return await execute_blindbox(client, action, actor, event), None
    if action in {"points_add", "points_subtract"}:
        target_id = match.target_id
        if not isinstance(target_id, str):
            raise BridgeError("invalid_parameters")
        amount = parse_points_amount(argument, settings.max_points_per_operation)
        target_actor = Actor(
            actor.platform_id, actor.bot_self_id, actor.group_id, target_id,
        )
        identity = points_event_identity(event, actor)
        body = {
            "actor": asdict(actor),
            "target_actor": asdict(target_actor),
            "amount": amount,
            **identity,
        }
        route = "/points/add" if action == "points_add" else "/points/subtract"
        query_status = False
        try:
            data = await client.request(route, body)
        except BridgeError as error:
            if error.code not in {
                "network_error", "http_error", "unavailable",
                "invalid_response", "response_too_large", "uncertain",
            }:
                raise
            query_status = True
        if not query_status and data.get("state") in {"pending", "uncertain"}:
            query_status = True
        if query_status:
            try:
                data = await client.request("/points/status", {
                    "actor": asdict(actor),
                    "request_id": identity["request_id"],
                })
            except BridgeError:
                return "积分操作结果未知，未自动重试；请先在网站核对积分记录。", None
        return points_text(data, action, amount, identity["request_id"]), None
    if action in {"latest", "search"}:
        if action == "search" and not 1 <= len(argument.strip()) <= 60:
            return f"用法：{commands['search']} 关键词（1–60 字）", None
        body = {**payload, "count": settings.latest_count if action == "latest" else settings.search_count}
        if action == "search":
            body["keyword"] = argument.strip()
        data = await client.request("/articles/" + action, body)
        items = data.get("items")
        if not isinstance(items, list) or len(items) > 10:
            raise BridgeError("invalid_response")
        if not items:
            return "未找到可公开展示的文章。", None
        lines = ["最新文章" if action == "latest" else "文章搜索结果"]
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict):
                raise BridgeError("invalid_response")
            lines.append(
                f"{index}. {safe_text(item.get('title'))}\n"
                f"{safe_text(item.get('published_at'), 30)}\n"
                f"{safe_link(item.get('url'), settings.public_site_url)}"
            )
        if action == "search":
            async def download_cover(item):
                url = item.get("cover_url")
                if not isinstance(url, str) or not url:
                    return None
                try:
                    return await fetch_avatar(url, settings)
                except BridgeError:
                    return None

            images = await asyncio.gather(*(download_cover(item) for item in items))
            if any(image is not None for image in images):
                return [
                    ((lines[0] + "\n" if index == 0 else "") + lines[index + 1], image)
                    for index, image in enumerate(images)
                ]
        return "\n".join(lines), None
    raise BridgeError("invalid_parameters")


def binding_event_identity(event, actor: Actor, action: str) -> dict:
    try:
        return event_identity(event, actor, action)
    except BridgeError:
        raise BridgeError("binding_event_required") from None


def binding_success(data: dict, public_site: str) -> str:
    return (
        f"绑定成功：{safe_text(data.get('nickname'))}（UID：{number(data, 'uid')}）\n"
        f"解除绑定请进入网站授权管理页：\n{public_site}/?az_bindings=1"
    )


async def redeem_code(client: BridgeClient, actor: Actor, code: str, event) -> str:
    pending = client.pending_bindings.get(actor)
    if not pending or pending[1] <= time.monotonic():
        client.pending_bindings.pop(actor, None)
        raise BridgeError("session_expired")
    if not re.fullmatch(r"[0-9]{6}", code):
        raise BridgeError("invalid_parameters")
    identity = binding_event_identity(event, actor, "binding/code/redeem")
    body = {"actor": asdict(actor), "session_id": pending[0], "code": code, **identity}
    try:
        data = await client.request("/binding/code/redeem", body)
    except BridgeError as error:
        if error.code in {"session_expired", "too_many_attempts", "binding_conflict"}:
            client.pending_bindings.pop(actor, None)
        if error.code not in {"network_error", "http_error", "unavailable", "invalid_response", "response_too_large"}:
            raise
        try:
            data = await client.request("/binding/code/status", {
                "actor": asdict(actor), "session_id": pending[0], "request_id": identity["request_id"],
            })
        except BridgeError:
            return "绑定结果暂未确认，未自动重复兑换；请到网站授权管理页查看，或重新发送绑定指令核对当前状态。"
    state = data.get("state")
    if state in {"bound", "revoked", "too_many_attempts"}:
        client.pending_bindings.pop(actor, None)
    if state == "bound":
        return binding_success(data, client.settings.public_site_url)
    if state in {"code_invalid", "too_many_attempts"}:
        return ERROR_MESSAGES[state]
    if state == "revoked":
        return "该消息已处理，但授权现已失效；没有重新建立绑定。"
    if state == "not_processed":
        return "尚未查到本次兑换结果，未自动重试；请先在网站管理页核对授权。"
    raise BridgeError("invalid_response")
