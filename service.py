import hashlib
import html
import json
import math
import re
import time
import unicodedata
from dataclasses import asdict
from urllib.parse import urlsplit

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
    if binding and not url.query.startswith("az_bind="):
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


def event_identity(event, actor: Actor) -> dict:
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
    key = json.dumps([asdict(actor), "checkin", str(message_id)], sort_keys=True, ensure_ascii=False)
    return {"request_id": hashlib.sha256(key.encode()).hexdigest(), "event_time": int(timestamp)}


async def execute(client: BridgeClient, match: CommandMatch, actor: Actor, event) -> tuple[str, bytes | None]:
    settings = client.settings
    commands = settings.router.commands
    payload = {"actor": asdict(actor)}
    action, argument = match.action, match.argument
    if action == "bind":
        data = await client.request("/binding/start", payload)
        link = safe_link(data.get("url"), settings.public_site_url, True)
        return (
            f"请在 10 分钟内打开网站并登录授权：\n{link}\n"
            f"授权后回到本群发送 {commands['confirm']} 核对账号，再发送 "
            f"{commands['confirm']} UID 完成绑定。\n"
            "不要确认他人的 UID；链接被误用时重新发起绑定。", None
        )
    if action == "confirm":
        if argument and not re.fullmatch(r"[1-9][0-9]{0,17}", argument):
            return f"用法：{commands['confirm']} 或 {commands['confirm']} UID", None
        data = await client.request("/binding/status", payload)
        if data.get("state") == "awaiting_website":
            return ERROR_MESSAGES["website_authorization_required"], None
        uid = number(data, "uid", "不可用")
        nickname = safe_text(data.get("nickname"))
        if data.get("state") == "bound":
            return f"已绑定：{nickname}（UID：{uid}）", None
        if data.get("state") != "awaiting_confirmation":
            raise BridgeError("invalid_response")
        if not argument:
            return f"待确认网站账号：{nickname}（UID：{uid}）\n确认是本人后发送：{commands['confirm']} {uid}", None
        await client.request("/binding/confirm", {
            **payload, "expected_uid": int(argument), "challenge_id": data.get("challenge_id"),
        })
        return f"绑定成功：{nickname}（UID：{uid}）", None
    if action == "unbind":
        if argument not in {"", "确认"}:
            return f"用法：{commands['unbind']} 或 {commands['unbind']} 确认", None
        data = await client.request("/binding/revoke", {**payload, "confirm": argument == "确认"})
        if data.get("state") == "revoked":
            return "已解除本群网站授权。", None
        if data.get("state") != "confirm_required":
            raise BridgeError("invalid_response")
        return f"将解绑网站 UID：{number(data, 'uid')}。两分钟内发送：{commands['unbind']} 确认", None
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
        return "\n".join(lines), None
    raise BridgeError("invalid_parameters")
