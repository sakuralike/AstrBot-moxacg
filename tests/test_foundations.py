import asyncio
import dataclasses
import hashlib
import importlib.util
import json
import logging
import socket
import sys
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web

from astrbot_plugin_zibll.avatar import allowed_address, fetch_avatar, validate_image
from astrbot_plugin_zibll.client import (
    NAMESPACE,
    PROTOCOL,
    BridgeClient,
    BridgeError,
    Settings,
    sign_request,
    site_url,
)
from astrbot_plugin_zibll.commands import (
    DEFAULT_COMMANDS,
    Actor,
    CommandMatch,
    CommandRouter,
    parse_scopes,
)
from astrbot_plugin_zibll.service import event_identity, execute, safe_link, safe_text

TEST_SECRET = bytes(range(32)).hex()
VECTOR_SIGNATURE = "1ed994284ea18d59fd0f2b21b84d58c44946154c4003fc0755b011c8ce95cfb2"


def config():
    return {
        "enabled": True,
        "api_base_url": "https://example.com/wp-json" + NAMESPACE,
        "public_site_url": "https://example.com",
        "client_id": "test-client",
        "key_id": "test-key",
        "shared_secret": TEST_SECRET,
        "allowed_groups": ["onebot-instance|001|group-1"],
        "commands": dict(DEFAULT_COMMANDS),
    }


class ConfigurationTests(unittest.TestCase):
    def test_defaults_and_exact_boundaries(self):
        router = CommandRouter(DEFAULT_COMMANDS)
        for action, command in DEFAULT_COMMANDS.items():
            with self.subTest(action=action):
                self.assertEqual(router.match(command).action, action)
                self.assertIsNone(router.match("hello " + command))
                self.assertIsNone(router.match(command + "extra"))
        self.assertIsNone(router.match("/网站资料 123"))
        self.assertEqual(router.match(" /网站搜索\t子比  主题 ").argument, "子比  主题")
        self.assertEqual(router.match("/网站绑定确认 123").action, "confirm")

    def test_renames_and_invalid_commands(self):
        renamed = {key: "cmd_" + key for key in DEFAULT_COMMANDS}
        router = CommandRouter(renamed)
        for action in renamed:
            self.assertEqual(router.match(renamed[action]).action, action)
            self.assertIsNone(router.match(DEFAULT_COMMANDS[action]))
        for value in ("", "bad command", "/\x00", "/\u200b", "x" * 33, 123):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CommandRouter({**DEFAULT_COMMANDS, "profile": value})
        for values in ({}, None, {**DEFAULT_COMMANDS, "profile": DEFAULT_COMMANDS["bind"]}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                CommandRouter(values)

    def test_untrusted_text(self):
        router = CommandRouter(DEFAULT_COMMANDS)
        for text in (
            "/网站搜索 a\nb", "/网站搜索 \x00", "/网站资料\u200b", "x" * 513,
            "", "普通聊天", None,
        ):
            with self.subTest(text=text):
                self.assertIsNone(router.match(text))

    def test_scope_and_identity(self):
        self.assertFalse(parse_scopes([]))
        actor = Actor("onebot-instance", "001", "group-1", "999999999999999999999999999")
        self.assertEqual(actor.bot_self_id, "001")
        self.assertIn(actor.scope, parse_scopes(config()["allowed_groups"]))
        for value in ("", 1, "a b", "a|b", "a\x00", "x" * 129):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Actor("p", "b", "g", value)
        for value in (None, ["*"], [123], ["p|b|"], ["p|b|g|s"], ["p|b|g"] * 101):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_scopes(value)

    def test_url_validation(self):
        self.assertEqual(site_url("https://example.com/blog/", False), "https://example.com/blog")
        for url in (
            "http://example.com", "https://user:secret@example.com",
            "https://@example.com", "https://example.com?token=1",
            "https://example.com/#a", "https://example.com/%2e",
            "https://example.com/../x", "https://example.com:99999",
            "https://example.com\\@localhost", "https://example.com\n",
            "http://localhost.evil", "ftp://localhost", "",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                site_url(url, True)
        for url in ("http://localhost:18797", "http://127.0.0.1", "http://[::1]"):
            self.assertEqual(site_url(url, True), url)
            with self.assertRaises(ValueError):
                site_url(url, False)

    def test_settings_fail_closed_and_redact_secret(self):
        settings = Settings.from_config(config())
        self.assertNotIn(TEST_SECRET, repr(settings))
        self.assertEqual(settings.timeout, 15)
        self.assertEqual(settings.points_operator_ids, frozenset())
        for key, value in (
            ("shared_secret", "bad"), ("shared_secret", TEST_SECRET.upper()),
            ("request_timeout", True), ("request_timeout", 31), ("client_id", "a\nb"),
            ("key_id", ""), ("api_base_url", "https://example.com"),
            ("allow_local_http", "true"), ("points_operator_ids", ["a b"]),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                Settings.from_config({**config(), key: value})
        self.assertEqual(
            Settings.from_config({
                **config(), "points_operator_ids": ["qq-openid", "123456"],
            }).points_operator_ids,
            frozenset({"qq-openid", "123456"}),
        )

    def test_cross_language_signature_vector(self):
        actual = sign_request(
            "test-client", "test-key", TEST_SECRET, "POST",
            NAMESPACE + "/articles/search", "1792584000", "0123456789abcdef" * 2,
            '{"keyword":"子比"}'.encode("utf-8"),
        )
        self.assertEqual(actual, VECTOR_SIGNATURE)

    def test_schema_has_matching_defaults_and_safe_initial_state(self):
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertFalse(schema["enabled"]["default"])
        self.assertTrue(schema["shared_secret"]["secret"])
        self.assertEqual(schema["allowed_groups"]["default"], [])
        self.assertEqual(schema["points_operator_ids"]["default"], [])
        self.assertEqual(
            {key: value["default"] for key, value in schema["commands"]["items"].items()},
            DEFAULT_COMMANDS,
        )


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def _setupAsyncioRunner(self):
        if sys.platform == "win32" and self._testMethodName in {
            "test_read_connection_retry_is_bounded_and_reuses_nonce",
            "test_lost_checkin_response_only_queries_status",
        }:
            # Native asyncio also raises WinError 10022 on these injected EOFs.
            self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=asyncio.SelectorEventLoop)
        else:
            super()._setupAsyncioRunner()

    async def asyncSetUp(self):
        self.requests = []
        self.reply = None
        self.loop_errors = []
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: self.loop_errors.append(context)
        )
        test = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle(self):
                try:
                    super().handle()
                except (ConnectionResetError, BrokenPipeError):
                    pass

            def do_GET(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                test.requests.append(types.SimpleNamespace(
                    headers=dict(self.headers), raw=raw, path=self.path, method=self.command,
                ))
                response = test.reply(self) if test.reply else web.json_response(test.payload)
                if response is None:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_WR)
                    self.connection.settimeout(2)
                    try:
                        while self.rfile.read(1):
                            pass
                    except OSError:
                        pass
                    return
                body = response.body or b""
                self.send_response(response.status)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET

            def log_message(self, *args):
                pass

        # A blocking server keeps the Windows Proactor under test on the client only.
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.settings = Settings.from_config({
            **config(),
            "api_base_url": self.base + "/wp-json" + NAMESPACE,
            "public_site_url": self.base,
            "allow_local_http": True,
        })
        self.client = BridgeClient(self.settings)
        self.payload = {
            "code": "ok",
            "data": {"protocol": PROTOCOL, "public_site_url": self.base},
        }

    async def asyncTearDown(self):
        await self.client.close()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join(timeout=1)
        await asyncio.sleep(0.05)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.loop_errors, [])

    async def assert_error(self, code):
        with self.assertRaises(BridgeError) as caught:
            await self.client.capabilities()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(TEST_SECRET, str(caught.exception))

    async def test_authenticated_request_and_session_reuse(self):
        self.assertEqual(await self.client.capabilities(), self.payload["data"])
        session = self.client._session
        await self.client.capabilities()
        self.assertIs(session, self.client._session)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.timeout.connect, 3)
        self.assertEqual(session.timeout.total, 15)
        self.assertTrue(session.connector._ssl)
        headers = self.requests[0].headers
        self.assertEqual(
            headers["X-AZ-Signature"],
            sign_request(
                "test-client", "test-key", TEST_SECRET, "GET", NAMESPACE + "/capabilities",
                headers["X-AZ-Time"], headers["X-AZ-Nonce"], b"",
            ),
        )
        self.assertNotEqual(headers["X-AZ-Nonce"], self.requests[1].headers["X-AZ-Nonce"])

    async def test_error_mapping_without_retries(self):
        for status, code in (
            (401, "authentication_failed"), (403, "forbidden"), (409, "replay"),
            (429, "rate_limited"), (503, "unavailable"), (500, "http_error"),
        ):
            self.reply = lambda request: web.Response(status=status)
            before = len(self.requests)
            await self.assert_error(code)
            self.assertEqual(len(self.requests), before + 1)

    async def test_redirect_is_not_followed(self):
        self.reply = lambda request: web.Response(
            status=302, headers={"Location": self.base + "/stolen"}
        )
        await self.assert_error("http_error")
        self.assertEqual(len(self.requests), 1)

    async def test_malformed_response(self):
        for response in (
            web.Response(text="<html>error</html>"),
            web.Response(text="{", content_type="application/json"),
            web.json_response([]),
            web.json_response({"code": "not_ok"}),
            web.Response(body=b"[" * 2000 + b"]" * 2000, content_type="application/json"),
        ):
            self.reply = lambda request: response
            await self.assert_error("invalid_response")

    async def test_protocol_and_site_mismatch(self):
        self.payload["data"]["protocol"] = "other"
        await self.assert_error("protocol_mismatch")
        self.payload["data"]["protocol"] = PROTOCOL
        self.payload["data"]["public_site_url"] = "https://other.example.com"
        await self.assert_error("site_mismatch")
        self.payload["data"]["public_site_url"] = "http://other.example.com"
        await self.assert_error("invalid_response")

    async def test_response_size_limit(self):
        self.reply = lambda request: web.Response(
            body=b" " * 65537, content_type="application/json"
        )
        await self.assert_error("response_too_large")

    async def test_network_error(self):
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        await self.assert_error("network_error")

    async def test_read_connection_retry_is_bounded_and_reuses_nonce(self):
        self.reply = lambda request: None
        await self.assert_error("network_error")
        self.assertIn(len(self.requests), (1, 2))
        self.assertEqual(len({request.headers["X-AZ-Nonce"] for request in self.requests}), 1)

    async def test_close_prevents_reopen(self):
        await self.client.capabilities()
        session = self.client._session
        await self.client.close()
        self.assertTrue(session.closed)
        await self.assert_error("client_closed")

    async def run_command(self, text, data):
        self.payload = {"code": "ok", "data": data}
        event = StubEvent(text)
        event.message_obj = types.SimpleNamespace(message_id="message-123", timestamp=int(time.time()))
        actor = Actor(event.platform, event.bot, event.group, event.sender)
        return await execute(self.client, self.settings.router.match(text), actor, event)

    async def test_binding_start_and_signed_actor_body(self):
        self.client.binding_code_available = True
        text, image = await self.run_command("/网站绑定", {
            "state": "pending", "session_id": hashlib.sha256(("a" * 64).encode()).hexdigest(), "expires_in": 600,
            "url": self.base + "/?az_bind=" + "a" * 64,
        })
        self.assertIn("六位", text)
        self.assertNotIn("/网站绑定确认", text)
        self.assertIsNone(image)
        request = self.requests[-1]
        body = json.loads(request.raw)
        self.assertEqual(body["actor"]["sender_id"], "user-1")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["X-AZ-Signature"], sign_request(
            "test-client", "test-key", TEST_SECRET, "POST", NAMESPACE + "/binding/code/start",
            request.headers["X-AZ-Time"], request.headers["X-AZ-Nonce"], request.raw,
        ))

    async def test_old_confirmation_and_unbind_only_guide(self):
        text, _ = await self.run_command("/网站绑定确认 123", {})
        self.assertIn("无需再确认", text)
        text, _ = await self.run_command("/网站解绑 确认", {})
        self.assertIn("?az_bindings=1", text)
        self.assertEqual(self.requests, [])

    async def test_search_parameters_and_safe_output(self):
        text, _ = await self.run_command("/网站搜索 子比", {
            "items": [{"title": "<b>子比</b> [CQ:at,qq=all]", "published_at": "2026-10-21", "url": self.base + "/?p=123"}],
        })
        self.assertNotIn("[CQ:", text)
        self.assertNotIn("<b>", text)
        self.assertEqual(json.loads(self.requests[-1].raw)["keyword"], "子比")
        before = len(self.requests)
        text, _ = await self.run_command("/网站搜索", {})
        self.assertIn("用法", text)
        self.assertEqual(len(self.requests), before)

    async def test_search_covers_keep_article_order_when_downloads_finish_out_of_order(self):
        second_done = asyncio.Event()

        async def download(url, settings):
            self.assertIs(settings, self.settings)
            if url.endswith("/first.png"):
                await second_done.wait()
                return b"first-image"
            second_done.set()
            return b"second-image"

        items = [
            {"title": title, "published_at": "2026-10-22", "url": self.base + f"/?p={index}",
             "cover_url": self.base + f"/{title}.png"}
            for index, title in enumerate(("first", "second"), 1)
        ]
        with patch("astrbot_plugin_zibll.service.fetch_avatar", side_effect=download):
            results = await asyncio.wait_for(self.run_command("/网站搜索 test", {"items": items}), 2)
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 2)
        self.assertEqual([image for _, image in results], [b"first-image", b"second-image"])
        for index, (text, _) in enumerate(results, 1):
            self.assertIn(f"{index}. {items[index - 1]['title']}", text)
            self.assertIn(items[index - 1]["url"], text)
        self.assertTrue(results[0][0].startswith("文章搜索结果\n"))

    async def test_search_without_usable_covers_keeps_text(self):
        for cover in ("", None, [], "http://169.254.169.254/latest", "https://other.example.com/cover.png"):
            with self.subTest(cover=cover):
                text, image = await self.run_command("/网站搜索 test", {"items": [{
                    "title": "Article", "published_at": "2026-10-22", "url": self.base + "/?p=1",
                    "cover_url": cover,
                }]})
                self.assertIn("1. Article", text)
                self.assertIn(self.base + "/?p=1", text)
                self.assertIsNone(image)
        self.assertEqual(len(self.requests), 5)

    async def test_latest_does_not_download_covers(self):
        with patch("astrbot_plugin_zibll.service.fetch_avatar", new_callable=AsyncMock) as download:
            text, image = await self.run_command("/网站最新", {"items": [{
                "title": "Article", "published_at": "2026-10-22", "url": self.base + "/?p=1",
                "cover_url": self.base + "/cover.png",
            }]})
        download.assert_not_awaited()
        self.assertTrue(text.startswith("最新文章\n1. Article"))
        self.assertIsNone(image)

    async def test_search_validates_all_articles_before_downloading_covers(self):
        with patch("astrbot_plugin_zibll.service.fetch_avatar", new_callable=AsyncMock) as download:
            with self.assertRaises(BridgeError):
                await self.run_command("/网站搜索 test", {"items": [
                    {"title": "First", "published_at": "2026-10-22", "url": self.base + "/?p=1",
                     "cover_url": self.base + "/cover.png"},
                    {"title": "Second", "published_at": "2026-10-22", "url": "https://other.example.com/"},
                ]})
        download.assert_not_awaited()

    async def test_group_handler_sends_search_covers_without_credentials_and_falls_back(self):
        image = (Path(__file__).resolve().parents[1] / "logo.png").read_bytes()
        self.payload = {"code": "ok", "data": {"items": [
            {"title": "First", "published_at": "2026-10-22", "url": self.base + "/?p=1",
             "cover_url": self.base + "/cover.png"},
            {"title": "Second", "published_at": "2026-10-22", "url": self.base + "/?p=2"},
            {"title": "Third", "published_at": "2026-10-22", "url": self.base + "/?p=3",
             "cover_url": self.base + "/redirect.png"},
            {"title": "Fourth", "published_at": "2026-10-22", "url": self.base + "/?p=4",
             "cover_url": self.base + "/invalid.png"},
        ]}}

        def reply(request):
            if request.path == "/cover.png":
                return web.Response(body=image, content_type="image/png")
            if request.path == "/redirect.png":
                return web.Response(status=302, headers={"Location": "http://169.254.169.254/latest"})
            if request.path == "/invalid.png":
                return web.Response(body=b"not an image", content_type="image/png")
            return web.json_response(self.payload)

        self.reply = reply
        module = load_main_with_framework_stubs()
        plugin = module.ZibllPlugin(object(), config())
        plugin.client = self.client
        plugin.settings = self.settings
        module.ScopedCommandFilter.settings = self.settings
        plugin.connected = True
        event = StubEvent("/网站搜索 test")
        results = [result async for result in plugin.website_command(event)]
        self.assertEqual(len(results), 4)
        self.assertEqual(results[0].components[0], image)
        self.assertIsNone(results[0].use_markdown_)
        self.assertTrue(all(result.use_markdown_ is False for result in results[1:]))
        texts = [results[0].components[1].text, *(result.text for result in results[1:])]
        for index, text in enumerate(texts, 1):
            self.assertIn(self.base + f"/?p={index}", text)
            self.assertIn(f"{index}. ", text)
        self.assertTrue(all(result.stopped for result in results))
        self.assertTrue(event.stopped)
        self.assertFalse(event.llm)
        self.assertEqual(len(self.requests), 4)
        for request in self.requests[1:]:
            self.assertEqual(request.method, "GET")
            self.assertFalse(any(name.lower().startswith("x-az-") for name in request.headers))
            self.assertNotIn("Cookie", request.headers)
            self.assertNotIn("Authorization", request.headers)
        await plugin.terminate()

    async def test_profile_text_and_avatar_failure_are_explicit(self):
        text, image = await self.run_command("/网站资料", {
            "nickname": "User", "uid": 123, "joined_days": 4, "level": None,
            "points": 50, "post_count": 2, "comment_count": 3,
            "avatar_url": "http://169.254.169.254/avatar",
        })
        self.assertIsNone(image)
        for item in ("UID：123", "50", "网站未启用", "头像暂不可用"):
            self.assertIn(item, text)

    async def test_lost_checkin_response_only_queries_status(self):
        def reply(request):
            if request.path.endswith("/checkin"):
                return None
            return web.json_response({"code": "ok", "data": {
                "state": "completed", "site_day": "2026-10-21", "points": 10,
                "continuous_days": 1, "total_days": 1,
            }})
        self.reply = reply
        text, _ = await self.run_command("/网站签到", {})
        self.assertIn("签到完成", text)
        self.assertEqual([r.path.split(NAMESPACE)[1] for r in self.requests], ["/checkin", "/checkin/status"])
        self.assertEqual(self.requests[0].raw, self.requests[1].raw)

    async def test_uncertain_checkin_never_claims_reward(self):
        text, _ = await self.run_command("/网站签到", {"state": "uncertain"})
        self.assertIn("需要核实", text)
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("签到完成", text)

    async def test_website_business_error_code_preserved(self):
        self.reply = lambda request: web.json_response({"code": "not_bound", "message": TEST_SECRET}, status=404)
        with self.assertRaises(BridgeError) as caught:
            await self.client.request("/profile", {})
        self.assertEqual(caught.exception.code, "not_bound")
        self.assertNotIn(TEST_SECRET, str(caught.exception))

    async def test_avatar_download_does_not_send_bridge_credentials(self):
        image = (Path(__file__).resolve().parents[1] / "logo.png").read_bytes()
        self.reply = lambda request: web.Response(body=image, content_type="image/png")
        self.assertEqual(await fetch_avatar(self.base + "/avatar.png", self.settings), image)
        self.assertNotIn("X-AZ-Signature", self.requests[0].headers)
        self.reply = lambda request: web.Response(status=302, headers={"Location": "http://169.254.169.254/latest"})
        with self.assertRaises(BridgeError):
            await fetch_avatar(self.base + "/redirect", self.settings)
        self.assertEqual(len(self.requests), 2)

    async def test_avatar_rejects_nonimage_and_oversize(self):
        for body in (b"not an image", b"x" * (2 * 1024 * 1024 + 1)):
            self.reply = lambda request: web.Response(body=body, content_type="image/png")
            with self.assertRaises(BridgeError):
                await fetch_avatar(self.base + "/fake", self.settings)

    async def test_group_handler_returns_avatar_and_all_profile_fields(self):
        image = (Path(__file__).resolve().parents[1] / "logo.png").read_bytes()
        self.payload = {"code": "ok", "data": {
            "nickname": "Member", "uid": 123, "joined_days": 7, "level": 2,
            "points": 50, "post_count": 4, "comment_count": 5,
            "avatar_url": self.base + "/avatar.png",
        }}
        self.reply = lambda request: (
            web.Response(body=image, content_type="image/png")
            if request.path.endswith("/avatar.png") else web.json_response(self.payload)
        )
        module = load_main_with_framework_stubs()
        plugin = module.ZibllPlugin(object(), config())
        plugin.client = self.client
        plugin.settings = self.settings
        module.ScopedCommandFilter.settings = self.settings
        plugin.connected = True
        event = StubEvent()
        replies = [reply async for reply in plugin.website_command(event)]
        self.assertTrue(event.stopped)
        self.assertFalse(event.llm)
        self.assertEqual(replies[0].components[0], image)
        self.assertIsNone(replies[0].use_markdown_)
        text = replies[0].components[1].text
        for field in ("昵称：Member", "UID：123", "天数：7", "Lv.2", "积分：50", "4 篇", "5 条"):
            self.assertIn(field, text)
        duplicate = [reply async for reply in plugin.website_command(StubEvent())]
        self.assertIn("频繁", duplicate[0].text)
        self.assertIs(duplicate[0].use_markdown_, False)
        await plugin.terminate()


class BusinessValidationTests(unittest.TestCase):
    def test_safe_text_and_links(self):
        self.assertEqual(safe_text("<b>A</b>&amp;[CQ:at,qq=all]@全体成员\x00"), "A&［CQ:at,qq=all］＠全体成员")
        for url in ("https://evil.test", "https://example.com\\@evil.test", "https://example.com/[CQ:x]"):
            with self.assertRaises(BridgeError):
                safe_link(url, "https://example.com")

    def test_event_identity_is_stable_and_missing_identity_is_rejected(self):
        actor = Actor("p", "b", "g", "s")
        event = types.SimpleNamespace(message_obj=types.SimpleNamespace(message_id="001", timestamp=int(time.time())))
        first = event_identity(event, actor)
        event.message_obj.timestamp += 1
        self.assertEqual(event_identity(event, actor)["request_id"], first["request_id"])
        event.message_obj.message_id = None
        with self.assertRaises(BridgeError):
            event_identity(event, actor)

    def test_avatar_network_and_format_restrictions(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "0.0.0.0"):
            self.assertFalse(allowed_address(address, False))
        self.assertTrue(allowed_address("8.8.8.8", False))
        self.assertTrue(allowed_address("127.0.0.1", True))
        with self.assertRaises(Exception):
            validate_image(b"<svg></svg>")


class Plain:
    def __init__(self, text):
        self.text = text


class At:
    def __init__(self, qq):
        self.qq = qq


class Reply:
    def __init__(self, id):
        self.id = id


class StubResult:
    def __init__(self, text):
        self.text = text
        self.chain = [Plain(text)] if text else []
        self.stopped = False
        self.use_markdown_ = None

    def use_markdown(self, use=True):
        self.use_markdown_ = use
        return self

    def stop_event(self):
        self.stopped = True
        return self


class StubEvent:
    def __init__(self, text="/网站资料"):
        self.messages = [Plain(text)]
        self.platform = "onebot-instance"
        self.platform_name = "aiocqhttp"
        self.result = None
        self.bot = "001"
        self.group = "group-1"
        self.sender = "user-1"
        self.kind = "group"
        self.is_at_or_wake_command = True
        self.message_obj = None
        self.admin = False
        self.stopped = False
        self.llm = None

    def get_platform_id(self):
        return self.platform

    def get_platform_name(self):
        return self.platform_name

    def get_result(self):
        return self.result

    def get_self_id(self):
        return self.bot

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.sender

    def get_messages(self):
        return self.messages

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        return StubResult(text)

    def chain_result(self, components):
        result = StubResult("")
        result.components = components
        result.chain = components
        return result

    def stop_event(self):
        self.stopped = True

    def should_call_llm(self, enabled):
        self.llm = enabled


def load_main_with_framework_stubs():
    class CustomFilter:
        def __init__(self, raise_error=True):
            self.raise_error = raise_error

    class EventMessageTypeFilter:
        def __init__(self, kind):
            self.kind = kind

        def filter(self, event, cfg):
            return event.kind == self.kind

    class Star:
        def __init__(self, context):
            self.context = context

    def custom_filter(cls, **kwargs):
        if not issubclass(cls, CustomFilter):
            raise TypeError("A filter class is required")
        cls(raise_error=True)
        return lambda handler: handler

    decorating_handlers = []

    def on_decorating_result():
        def register(handler):
            decorating_handlers.append(handler)
            return handler
        return register

    modules = {
        name: types.ModuleType(name) for name in (
            "astrbot", "astrbot.api", "astrbot.api.event",
            "astrbot.api.message_components", "astrbot.api.star",
        )
    }
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = logging.getLogger("astrbot-foundation-tests")
    modules["astrbot.api.event"].AstrMessageEvent = StubEvent
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        CustomFilter=CustomFilter, custom_filter=custom_filter,
        on_decorating_result=on_decorating_result,
        EventMessageTypeFilter=EventMessageTypeFilter,
        EventMessageType=types.SimpleNamespace(GROUP_MESSAGE="group"),
    )
    modules["astrbot.api.message_components"].At = At
    modules["astrbot.api.message_components"].Plain = Plain
    modules["astrbot.api.message_components"].Reply = Reply
    modules["astrbot.api.message_components"].Image = types.SimpleNamespace(fromBytes=lambda data: data)
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = Star
    path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("astrbot_plugin_zibll._test_main", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    module.decorating_handlers = decorating_handlers
    return module


class ReplyCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_main_with_framework_stubs()
        self.plugin = self.module.ZibllPlugin(object(), config())

    async def decorate(self, event):
        for handler in self.module.decorating_handlers:
            await handler(self.plugin, event)

    async def test_qq_chat_and_builtin_text_replies_disable_markdown(self):
        for prompt in ("ordinary chat", "/sid", "/help"):
            for markdown in (None, True, False):
                with self.subTest(prompt=prompt, markdown=markdown):
                    event = StubEvent(prompt)
                    event.platform_name = "qq_official"
                    event.result = StubResult("Bot reply")
                    event.result.chain[:0] = [Reply("message-1"), At(event.sender)]
                    event.result.use_markdown_ = markdown
                    chain = list(event.result.chain)
                    await self.decorate(event)
                    self.assertIs(event.result.use_markdown_, False)
                    self.assertEqual(event.result.chain, chain)
                    self.assertFalse(event.stopped)
                    self.assertFalse(event.result.stopped)
                    self.assertIsNone(event.llm)

    async def test_other_platforms_keep_markdown_choice(self):
        for platform in ("aiocqhttp", "telegram"):
            for markdown in (None, True, False):
                with self.subTest(platform=platform, markdown=markdown):
                    event = StubEvent()
                    event.platform_name = platform
                    event.result = StubResult("Bot reply")
                    event.result.use_markdown_ = markdown
                    await self.decorate(event)
                    self.assertIs(event.result.use_markdown_, markdown)

    async def test_media_and_empty_results_are_unchanged(self):
        event = StubEvent()
        event.platform_name = "qq_official"
        await self.decorate(event)
        self.assertIsNone(event.result)
        for components in ([], [b"image"], [Plain("Caption"), b"image"], [object()]):
            with self.subTest(components=components):
                event.result = event.chain_result(components)
                event.result.use_markdown_ = True
                await self.decorate(event)
                self.assertIs(event.result.use_markdown_, True)
                self.assertEqual(event.result.chain, components)

    async def test_compatibility_does_not_require_website_connection_or_scope(self):
        self.plugin = self.module.ZibllPlugin(object(), {})
        event = StubEvent("ordinary chat")
        event.platform_name = "qq_official"
        event.platform = "qq-instance"
        event.group = "another-group"
        event.result = StubResult("Bot reply")
        await self.decorate(event)
        self.assertIs(event.result.use_markdown_, False)
        self.assertIsNone(self.plugin.client)
        self.assertFalse(event.stopped)

    async def test_terminated_plugin_does_not_change_replies(self):
        await self.plugin.terminate()
        event = StubEvent()
        event.platform_name = "qq_official"
        event.result = StubResult("Bot reply")
        await self.decorate(event)
        self.assertIsNone(event.result.use_markdown_)


class FilterContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # These are contract doubles, not an installed AstrBot or adapter.
        self.module = load_main_with_framework_stubs()
        self.settings = Settings.from_config(config())
        self.module.ScopedCommandFilter.settings = self.settings

    def match(self, event):
        return self.module.ScopedCommandFilter.match(event)

    def test_exact_commands_do_not_wake_normal_chat(self):
        event = StubEvent()
        self.assertEqual(self.match(event).action, "profile")
        self.assertIsNone(self.match(StubEvent("ordinary chat")))
        self.assertIsNone(self.match(StubEvent("/other")))
        event.is_at_or_wake_command = False
        self.assertIsNone(self.match(event))
        self.assertFalse(event.is_at_or_wake_command)
        self.assertFalse(event.stopped)

    def test_scopes_and_self_messages(self):
        for field, value in (
            ("kind", "private"), ("platform", "other"), ("bot", "other"),
            ("group", "other"), ("sender", "001"), ("sender", None),
        ):
            with self.subTest(field=field):
                event = StubEvent()
                setattr(event, field, value)
                self.assertIsNone(self.match(event))
        self.module.ScopedCommandFilter.settings = dataclasses.replace(
            self.settings, scopes=frozenset()
        )
        self.assertIsNone(self.match(StubEvent()))

    def test_reject_nontext_and_foreign_mentions(self):
        event = StubEvent()
        event.messages = [At("001"), Plain("/网站资料")]
        self.assertIsNotNone(self.match(event))
        for messages in (
            [At("other"), Plain("/网站资料")],
            [object(), Plain("/网站资料")], [Plain(123)], None, [],
        ):
            with self.subTest(messages=messages):
                event.messages = messages
                self.assertIsNone(self.match(event))

    def test_adapter_probe_matches_only_explicit_qq_official_bot_mention(self):
        scopes = frozenset({("qq-instance", "qq_official", "group-1")})
        self.module.ScopedCommandFilter.settings = dataclasses.replace(
            self.settings, scopes=scopes
        )
        event = StubEvent("/适配器核查")
        event.platform_name = "qq_official"
        event.platform = "qq-instance"
        event.bot = "qq_official"
        event.messages = [At("qq_official"), Plain("/适配器核查 "), At("target-id")]
        self.assertEqual(self.match(event).action, "adapter_probe")
        event.messages = [Plain("/适配器核查")]
        self.assertIsNone(self.match(event))
        event.messages = [At("qq_official"), Plain("/适配器核查")]
        self.assertEqual(self.match(event).argument, "0")
        event.messages = [At("qq_official"), Plain("/适配器核查 "), At("target-id"), At("second-id")]
        self.assertIsNone(self.match(event))
        event.platform_name = "aiocqhttp"
        self.assertIsNone(self.match(event))

    async def test_adapter_probe_logs_hashed_ids_without_site_request(self):
        target_id = "target-openid"
        target_hash = hashlib.sha256(target_id.encode()).hexdigest()[:12]
        settings = {
            **config(),
            "allowed_groups": ["qq-instance|qq_official|group-1"],
        }
        plugin = self.module.ZibllPlugin(object(), settings)
        plugin.client.request = AsyncMock()
        event = StubEvent("/适配器核查")
        event.platform_name = "qq_official"
        event.platform = "qq-instance"
        event.bot = "qq_official"
        event.admin = True
        event.messages = [At("qq_official"), Plain("/适配器核查 "), At(target_id)]
        event.message_obj = types.SimpleNamespace(
            type="group",
            raw_message=types.SimpleNamespace(
                raw_data={
                    "content": "<@target-openid>",
                    "mentions": [{"id": target_id, "is_you": False, "username": "Target Name"}],
                    "author": {"member_openid": "admin-openid"},
                    "msg_elements": [{"type": "at", "user_id": target_id}],
                    "access_token": "do-not-log",
                },
                content="<@target-openid>",
                mentions=[types.SimpleNamespace(
                    id=target_id, is_you=False, username="Target Name",
                )],
                msg_elements=[{"type": "at", "user_id": target_id}],
                message_type=100,
                author=types.SimpleNamespace(member_openid="admin-openid"),
            ),
        )
        with self.assertLogs("astrbot-foundation-tests", level="INFO") as captured:
            replies = [result async for result in plugin.website_command(event)]
        logged = "\n".join(captured.output)
        self.assertEqual(len(replies), 1)
        self.assertIn("适配器核查信息已记录", replies[0].text)
        self.assertIn(target_hash, logged)
        self.assertNotIn(target_id, logged)
        self.assertNotIn("Target Name", logged)
        self.assertNotIn("do-not-log", logged)
        plugin.client.request.assert_not_awaited()

        event.sender = target_id
        event.messages = [At("qq_official"), Plain("/适配器核查")]
        event.message_obj = types.SimpleNamespace(type="group", raw_message=None)
        with self.assertLogs("astrbot-foundation-tests", level="INFO") as captured:
            [result async for result in plugin.website_command(event)]
        self.assertIn(target_hash, "\n".join(captured.output))
        plugin.client.request.assert_not_awaited()
        await plugin.terminate()

    def test_disabled_or_invalid_config_does_not_register_business(self):
        for value in ({}, {**config(), "shared_secret": ""}, {**config(), "allowed_groups": []}):
            with self.subTest(config=value):
                with self.assertLogs("astrbot-foundation-tests") if value.get("enabled") else patch.dict({}, {}):
                    plugin = self.module.ZibllPlugin(object(), value)
                self.assertIsNone(plugin.client)
                self.assertIsNone(self.match(StubEvent()))

    async def test_handler_stops_only_matching_commands(self):
        plugin = self.module.ZibllPlugin(object(), config())
        event = StubEvent()
        results = [result async for result in plugin.website_command(event)]
        self.assertTrue(event.stopped)
        self.assertFalse(event.llm)
        self.assertTrue(results[0].stopped)
        self.assertIn("尚未通过验证", results[0].text)
        self.assertIs(results[0].use_markdown_, False)
        event = StubEvent("ordinary chat")
        self.assertEqual([result async for result in plugin.website_command(event)], [])
        self.assertFalse(event.stopped)
        self.assertIsNone(event.llm)
        await plugin.terminate()

    async def test_checkin_without_event_identity_does_not_write(self):
        plugin = self.module.ZibllPlugin(object(), config())
        plugin.client = AsyncMock()
        plugin.client.settings = plugin.settings
        plugin.client.pending_bindings = {}
        await plugin.initialize()
        self.assertTrue(plugin.connected)
        results = [result async for result in plugin.website_command(StubEvent("/网站签到"))]
        self.assertIn("消息 ID", results[0].text)
        self.assertIs(results[0].use_markdown_, False)
        self.assertEqual(plugin.client.mock_calls[0][0], "capabilities")
        self.assertEqual(len(plugin.client.mock_calls), 1)
        await plugin.terminate()
        plugin.client.close.assert_awaited_once()
        self.assertIsNone(self.match(StubEvent()))

    async def test_terminate_during_connection_does_not_reenable_plugin(self):
        plugin = self.module.ZibllPlugin(object(), config())
        gate = asyncio.Event()
        plugin.client = AsyncMock()
        plugin.client.capabilities.side_effect = gate.wait
        task = asyncio.create_task(plugin.initialize())
        await asyncio.sleep(0)
        await plugin.terminate()
        gate.set()
        await task
        self.assertFalse(plugin.connected)
        self.assertIsNone(self.match(StubEvent()))

    async def test_old_instance_termination_does_not_clear_reloaded_settings(self):
        old = self.module.ZibllPlugin(object(), config())
        current_config = config()
        current_config["commands"]["profile"] = "/me"
        current = self.module.ZibllPlugin(object(), current_config)
        event = StubEvent("/me")
        self.assertEqual([result async for result in old.website_command(event)], [])
        self.assertFalse(event.stopped)
        await old.terminate()
        self.assertIsNone(self.match(StubEvent("/网站资料")))
        self.assertEqual(self.match(StubEvent("/me")).action, "profile")
        await current.terminate()


if __name__ == "__main__":
    unittest.main()
