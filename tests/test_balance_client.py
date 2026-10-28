import asyncio
import dataclasses
import json
import threading
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

import aiohttp

from astrbot_plugin_zibll.balance import (
    MAX_RESPONSE_BYTES,
    BalanceClient,
    BalanceError,
    BalanceSettings,
)

DEEPSEEK_KEY = "test-deepseek-key"
ACCOUNT_TOKEN = "test-account-token"


def balance_config():
    return {
        "balance_enabled": True,
        "allowed_groups": ["platform|bot|group"],
        "deepseek_api_key": DEEPSEEK_KEY,
        "compatible_balance": {
            "enabled": True,
            "name": "测试账户",
            "url": "https://account.example/api/user/self",
            "credential": ACCOUNT_TOKEN,
            "method": "GET",
            "balance_path": "data.quota",
            "unit": "USD",
            "divisor": "500000",
            "user_id": "17",
        },
    }


def deepseek_payload(available=True):
    return {
        "is_available": available,
        "balance_infos": [{
            "currency": "CNY",
            "total_balance": "100.01",
            "granted_balance": "10.00",
            "topped_up_balance": "90.01",
        }, {
            "currency": "USD",
            "total_balance": "0.00",
            "granted_balance": "0.00",
            "topped_up_balance": "0.00",
        }],
    }


class BalanceSettingsTests(unittest.TestCase):
    def test_defaults_ignore_website_configuration_and_hide_credentials(self):
        settings = BalanceSettings.from_config({})
        self.assertEqual(settings.command, "/模型余额")
        self.assertFalse(settings.scopes)
        self.assertIsNone(settings.compatible)
        settings = BalanceSettings.from_config({
            **balance_config(), "api_base_url": "bad", "shared_secret": "bad",
        })
        self.assertEqual(settings.compatible.divisor, Decimal("500000"))
        self.assertNotIn(DEEPSEEK_KEY, repr(settings))
        self.assertNotIn(ACCOUNT_TOKEN, repr(settings))

    def test_commands_and_timeout_fail_closed(self):
        for command in (
            "", "/bad command", "000001", "/模型余额\n", "/模型余额\u200b",
            "x" * 33, "/网站绑定", "/适配器核查", "/custom", 3,
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                BalanceSettings.from_config({
                    "balance_command": command, "commands": {"profile": "/custom"},
                })
        for timeout in (True, 2, 31, "15"):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                BalanceSettings.from_config({"request_timeout": timeout})
        self.assertEqual(
            BalanceSettings.from_config({"balance_command": "/余额"}).command, "/余额",
        )

    def test_invalid_source_does_not_disable_the_other_source(self):
        config = balance_config()
        config["deepseek_api_key"] = "secret\ninjection"
        settings = BalanceSettings.from_config(config)
        self.assertEqual(settings.deepseek_error, "invalid_config")
        self.assertEqual(settings.deepseek_api_key, "")
        self.assertIsNotNone(settings.compatible)
        config = balance_config()
        config["compatible_balance"]["url"] = "http://account.example/balance"
        settings = BalanceSettings.from_config(config)
        self.assertEqual(settings.compatible_error, "invalid_config")
        self.assertEqual(settings.deepseek_api_key, DEEPSEEK_KEY)

    def test_custom_endpoint_field_and_units_require_explicit_safe_contract(self):
        cases = (
            ("url", "http://account.example/balance"),
            ("url", "https://user:secret@account.example/balance"),
            ("url", "https://account.example/balance?key=secret"),
            ("url", "https://account.example/balance#fragment"),
            ("url", "https://account.example/%2e/balance"),
            ("url", "https://account.example/../balance"),
            ("url", "https://account.example/api/usage/token"),
            ("url", "https://account.example/prefix/api/usage/token/"),
            ("url", "https://account.example:99999/balance"),
            ("method", "DELETE"),
            ("method", []),
            ("balance_path", "data.quota / 500000"),
            ("balance_path", "data[0].balance"),
            ("unit", "USD\nsecret"),
            ("divisor", "0"),
            ("divisor", "-1"),
            ("divisor", "NaN"),
            ("divisor", "1e99999"),
            ("user_id", "17\r\nAuthorization: secret"),
            ("credential", "Bearer account-key"),
            ("name", "DeepSeek"),
            ("name", ACCOUNT_TOKEN),
            ("name", DEEPSEEK_KEY),
            ("name", "account\u2028label"),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                config = balance_config()
                config["compatible_balance"][key] = value
                settings = BalanceSettings.from_config(config)
                self.assertIsNone(settings.compatible)
                self.assertEqual(settings.compatible_error, "invalid_config")
                self.assertNotIn(ACCOUNT_TOKEN, settings.compatible_name)
                self.assertNotIn(DEEPSEEK_KEY, settings.compatible_name)

    def test_base_url_and_api_key_derive_only_confirmed_balance_contracts(self):
        config = balance_config()
        config["compatible_balance"] = {
            "enabled": True,
            "name": "DeepSeek 自动",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "auto-deepseek-key",
        }
        settings = BalanceSettings.from_config(config)
        self.assertIsNone(settings.compatible_error)
        self.assertEqual(settings.compatible.url, "https://api.deepseek.com/user/balance")
        self.assertEqual(settings.compatible.credential, "auto-deepseek-key")
        self.assertEqual(settings.compatible.result_kind, "deepseek")
        self.assertNotIn("auto-deepseek-key", repr(settings))

        for base_url in (
            "https://new-api.example/v1/responses",
            "https://relay.example/one-api/v1",
        ):
            with self.subTest(base_url=base_url):
                config = balance_config()
                config["compatible_balance"] = {
                    "enabled": True,
                    "name": "中转 API",
                    "base_url": base_url,
                    "api_key": "auto-api-key",
                }
                settings = BalanceSettings.from_config(config)
                self.assertIsNone(settings.compatible_error)
                self.assertTrue(settings.compatible.url.endswith("/api/usage/token"))
                self.assertEqual(settings.compatible.balance_path, ("data", "total_available"))
                self.assertEqual(settings.compatible.divisor, Decimal("500000"))
                self.assertEqual(settings.compatible.result_kind, "api_key_quota")
                self.assertNotIn("auto-api-key", repr(settings))

    def test_base_url_derivation_fails_closed_for_unknown_or_account_only_endpoint(self):
        config = balance_config()
        config["compatible_balance"] = {
            "enabled": True,
            "name": "未知服务",
            "base_url": "https://unknown.example/v1",
            "api_key": "unknown-key",
        }
        settings = BalanceSettings.from_config(config)
        self.assertIsNone(settings.compatible)
        self.assertEqual(settings.compatible_error, "not_configured")

        config = balance_config()
        config["compatible_balance"] = {
            "enabled": True,
            "name": "New API",
            "base_url": "https://new-api.example/api/user/self",
            "api_key": "model-key",
        }
        settings = BalanceSettings.from_config(config)
        self.assertIsNone(settings.compatible)
        self.assertEqual(settings.compatible_error, "invalid_config")

    def test_magpie_known_hosts_derive_fixed_balance_contracts(self):
        cases = {
            "https://api.moonshot.cn/v1": (
                "https://api.moonshot.cn/v1/users/me/balance", "CNY", "known",
            ),
            "https://api.moonshot.ai/v1": (
                "https://api.moonshot.ai/v1/users/me/balance", "USD", "known",
            ),
            "https://openrouter.ai/api/v1": (
                "https://openrouter.ai/api/v1/credits", "USD", "openrouter",
            ),
            "https://api.stepfun.com/v1": (
                "https://api.stepfun.com/v1/accounts", "CNY", "known",
            ),
            "https://api.stepfun.ai/v1": (
                "https://api.stepfun.ai/v1/accounts", "USD", "known",
            ),
            "https://api.commandcode.ai/v1": (
                "https://api.commandcode.ai/alpha/billing/credits", "USD", "commandcode",
            ),
            "https://ollama.com/v1": (
                "https://ollama.com/api/balance", "USD", "ollama",
            ),
            "https://aihubmix.com/v1": (
                "https://aihubmix.com/dashboard/billing/remain", "USD", "aihubmix_key",
            ),
        }
        for base_url, (endpoint, unit, result_kind) in cases.items():
            with self.subTest(base_url=base_url):
                config = balance_config()
                config["compatible_balance"] = {
                    "enabled": True,
                    "name": "自动服务商",
                    "base_url": base_url,
                    "api_key": "auto-api-key",
                }
                settings = BalanceSettings.from_config(config)
                self.assertIsNone(settings.compatible_error)
                self.assertEqual(settings.compatible.url, endpoint)
                self.assertEqual(settings.compatible.unit, unit)
                self.assertEqual(settings.compatible.result_kind, result_kind)


class BalanceHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.status = 200
        self.content_type = "application/json"
        self.location = None
        self.chunked = False
        self.body = json.dumps(deepseek_payload()).encode()
        test = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle(self):
                try:
                    super().handle()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def do_GET(self):
                length = int(self.headers.get("Content-Length", "0"))
                test.requests.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": self.rfile.read(length),
                })
                self.send_response(test.status)
                self.send_header("Content-Type", test.content_type)
                if test.location:
                    self.send_header("Location", test.location)
                if test.chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(len(test.body)))
                self.end_headers()
                try:
                    if test.chunked:
                        for index in range(0, len(test.body), 8192):
                            chunk = test.body[index:index + 8192]
                            self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                        self.wfile.write(b"0\r\n\r\n")
                    else:
                        self.wfile.write(test.body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            do_POST = do_GET

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.url_patch = patch("astrbot_plugin_zibll.balance.DEEPSEEK_BALANCE_URL", self.base + "/balance")
        self.url_patch.start()
        settings = BalanceSettings.from_config(balance_config())
        settings = dataclasses.replace(
            settings, compatible=dataclasses.replace(settings.compatible, url=self.base + "/account"),
        )
        self.client = BalanceClient(settings)

    async def asyncTearDown(self):
        await self.client.close()
        self.url_patch.stop()
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join(timeout=1)
        self.assertFalse(self.thread.is_alive())

    async def test_deepseek_exact_amounts_multiple_currencies_and_session_isolation(self):
        text = await self.client.query("deepseek")
        self.assertIn("总余额：CNY 100.01", text)
        self.assertIn("赠送余额：CNY 10.00", text)
        self.assertIn("充值余额：CNY 90.01", text)
        self.assertIn("总余额：USD 0.00", text)
        self.assertNotIn("200.02", text)
        self.assertIn("查询时间：", text)
        request = self.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["headers"]["Authorization"], "Bearer " + DEEPSEEK_KEY)
        self.assertNotIn("New-Api-User", request["headers"])
        self.assertEqual(request["body"], b"")
        self.assertFalse(self.client._session.trust_env)
        self.assertEqual(self.client._session.timeout.total, 15)
        self.assertEqual(self.client._session.timeout.connect, 3)
        self.assertTrue(self.client._session.connector._ssl)
        session = self.client._session
        await self.client.query("DeepSeek")
        self.assertIs(self.client._session, session)
        self.assertNotIn(DEEPSEEK_KEY, text)

    async def test_zero_and_unavailable_are_successful_balance_results(self):
        payload = deepseek_payload(False)
        payload["balance_infos"] = payload["balance_infos"][1:]
        self.body = json.dumps(payload).encode()
        text = await self.client.query("DeepSeek")
        self.assertIn("可用：否", text)
        self.assertIn("总余额：USD 0.00", text)
        self.assertNotIn("失败", text)

    async def test_account_query_uses_explicit_token_user_id_and_decimal_conversion(self):
        self.body = b'{"success":true,"data":{"quota":12345678.125}}'
        text = await self.client.query("测试账户")
        self.assertIn("余额：USD 24.69135625", text)
        self.assertEqual(self.requests[0]["headers"]["Authorization"], "Bearer " + ACCOUNT_TOKEN)
        self.assertEqual(self.requests[0]["headers"]["New-Api-User"], "17")
        self.assertNotIn(DEEPSEEK_KEY, str(self.requests[0]))
        self.client.settings = dataclasses.replace(
            self.client.settings,
            compatible=dataclasses.replace(self.client.settings.compatible, method="POST"),
        )
        await self.client.query("兼容")
        self.assertEqual(self.requests[-1]["method"], "POST")
        self.assertEqual(self.requests[-1]["body"], b"")

    async def test_derived_new_api_key_quota_is_not_reported_as_account_balance(self):
        config = balance_config()
        config["compatible_balance"] = {
            "enabled": True,
            "name": "中转 API",
            "base_url": "https://new-api.example/v1/responses",
            "api_key": "derived-api-key",
        }
        settings = BalanceSettings.from_config(config)
        settings = dataclasses.replace(
            settings,
            compatible=dataclasses.replace(
                settings.compatible,
                url=self.base + "/api/usage/token",
            ),
        )
        self.client.settings = settings
        self.body = b'{"success":true,"data":{"total_available":1000000}}'
        text = await self.client.query("兼容")
        self.assertIn("API Key 剩余额度", text)
        self.assertIn("额度：USD 2.00", text)
        self.assertNotIn("当前账户余额", text)
        self.assertEqual(self.requests[-1]["path"], "/api/usage/token")
        self.assertEqual(
            self.requests[-1]["headers"]["Authorization"], "Bearer derived-api-key",
        )

    async def test_derived_deepseek_endpoint_uses_official_balance_parser(self):
        config = balance_config()
        config["compatible_balance"] = {
            "enabled": True,
            "name": "DeepSeek 自动",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "derived-deepseek-key",
        }
        settings = BalanceSettings.from_config(config)
        settings = dataclasses.replace(
            settings,
            compatible=dataclasses.replace(
                settings.compatible,
                url=self.base + "/user/balance",
            ),
        )
        self.client.settings = settings
        self.body = json.dumps(deepseek_payload()).encode()
        text = await self.client.query("兼容")
        self.assertIn("DeepSeek 自动 当前余额", text)
        self.assertIn("总余额：CNY 100.01", text)
        self.assertEqual(self.requests[-1]["path"], "/user/balance")
        self.assertEqual(
            self.requests[-1]["headers"]["Authorization"], "Bearer derived-deepseek-key",
        )

    async def test_derived_openrouter_and_command_code_amounts(self):
        cases = (
            (
                "https://openrouter.ai/api/v1",
                '{"data":{"total_credits":20,"total_usage":3.5}}',
                "OpenRouter API Key",
                "USD 16.50",
                "/api/v1/credits",
            ),
            (
                "https://api.commandcode.ai/v1",
                '{"credits":{"monthlyCredits":41.2,"purchasedCredits":5,"freeCredits":0}}',
                "Command Code API Key",
                "USD 46.20",
                "/alpha/billing/credits",
            ),
        )
        for base_url, body, name, amount, path in cases:
            with self.subTest(base_url=base_url):
                config = balance_config()
                config["compatible_balance"] = {
                    "enabled": True,
                    "name": name,
                    "base_url": base_url,
                    "api_key": "derived-api-key",
                }
                settings = BalanceSettings.from_config(config)
                settings = dataclasses.replace(
                    settings,
                    compatible=dataclasses.replace(
                        settings.compatible, url=self.base + path,
                    ),
                )
                self.client.settings = settings
                self.body = body.encode()
                text = await self.client.query("兼容")
                self.assertIn(amount, text)
                self.assertEqual(self.requests[-1]["path"], path)
                self.assertEqual(
                    self.requests[-1]["headers"]["Authorization"],
                    "Bearer derived-api-key",
                )

    async def test_derived_ollama_and_aihubmix_amounts(self):
        cases = (
            (
                "https://ollama.com/v1",
                '{"purchased":{"balance_usd":25.0}}',
                "Ollama",
                "USD 25.00",
                "/api/balance",
            ),
            (
                "https://aihubmix.com/v1",
                '{"object":"list","total_usage":12.5}',
                "AiHubMix",
                "USD 12.50",
                "/dashboard/billing/remain",
            ),
        )
        for base_url, body, name, amount, path in cases:
            with self.subTest(base_url=base_url):
                config = balance_config()
                config["compatible_balance"] = {
                    "enabled": True,
                    "name": name,
                    "base_url": base_url,
                    "api_key": "derived-api-key",
                }
                settings = BalanceSettings.from_config(config)
                settings = dataclasses.replace(
                    settings,
                    compatible=dataclasses.replace(
                        settings.compatible, url=self.base + path,
                    ),
                )
                self.client.settings = settings
                self.body = body.encode()
                text = await self.client.query("兼容")
                self.assertIn(amount, text)
                self.assertEqual(self.requests[-1]["path"], path)

    async def test_missing_configuration_and_invalid_arguments_never_request(self):
        client = BalanceClient(BalanceSettings.from_config({}))
        try:
            text = await client.query("")
            self.assertEqual(text.count("未配置"), 2)
            self.assertIn("用法", await client.query("https://attacker.example"))
            self.assertIn("用法", await client.query("DeepSeek token-value"))
            self.assertIsNone(client._session)
            self.assertEqual(self.requests, [])
        finally:
            await client.close()

    async def test_http_failures_and_redirects_do_not_disclose_or_forward_credentials(self):
        for status, phrase in (
            (401, "认证失败"), (403, "认证失败"), (429, "过于频繁"),
            (500, "暂不可用"), (404, "请求失败"), (302, "重定向"),
        ):
            with self.subTest(status=status):
                self.status = status
                self.body = (DEEPSEEK_KEY + ACCOUNT_TOKEN).encode()
                self.location = self.base + "/stolen"
                before = len(self.requests)
                text = await self.client.query("DeepSeek")
                self.assertIn(phrase, text)
                self.assertNotIn(DEEPSEEK_KEY, text)
                self.assertNotIn(ACCOUNT_TOKEN, text)
                self.assertNotIn("/stolen", text)
                self.assertEqual(len(self.requests), before + 1)

    async def test_business_failure_and_invalid_json_are_not_reported_as_zero(self):
        for body, phrase in (
            (b'{"success":false,"data":{"quota":0},"message":"private"}', "返回查询失败"),
            (b'{"error":{"message":"private"},"data":{"quota":0}}', "返回查询失败"),
            (b'{"success":"true","data":{"quota":0}}', "返回查询失败"),
            (b'{"data":{"quota":NaN}}', "无法识别"),
            (b'{"data":{"quota":Infinity}}', "无法识别"),
            (b'{"data":{"quota":1e1000000}}', "无法识别"),
            (b'{"data":{"quota":true}}', "无法识别"),
            (b'{"data":{"other":0}}', "无法识别"),
            (b'{"data":[]}', "无法识别"),
            (b"[]", "无法识别"),
            (b"{", "无法识别"),
        ):
            with self.subTest(body=body):
                self.body = body
                text = await self.client.query("兼容")
                self.assertIn(phrase, text)
                self.assertNotIn("余额：USD 0", text)
                self.assertNotIn("private", text)

    async def test_deepseek_validates_every_currency_and_amount(self):
        invalid_values = (None, True, "NaN", "Infinity", "1e999999", "1e-999999", "1.0junk")
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                payload = deepseek_payload()
                payload["balance_infos"][1]["total_balance"] = invalid
                self.body = json.dumps(payload).encode()
                self.assertIn("无法识别", await self.client.query("DeepSeek"))
        for payload in (
            {"is_available": 1, "balance_infos": deepseek_payload()["balance_infos"]},
            {"is_available": True, "balance_infos": []},
            {"is_available": True, "balance_infos": [None]},
            {"is_available": True, "balance_infos": [deepseek_payload()["balance_infos"][0]] * 2},
            {"is_available": True, "balance_infos": [{
                **deepseek_payload()["balance_infos"][0], "currency": "CNY\nsecret",
            }]},
        ):
            self.body = json.dumps(payload).encode()
            self.assertIn("无法识别", await self.client.query("DeepSeek"))

    async def test_response_size_limit_applies_to_length_and_chunked_bodies(self):
        self.body = b" " * (MAX_RESPONSE_BYTES + 1)
        for chunked in (False, True):
            with self.subTest(chunked=chunked):
                self.chunked = chunked
                self.assertIn("响应过大", await self.client.query("DeepSeek"))

    async def test_source_failure_does_not_prevent_second_balance_and_errors_are_sanitized(self):
        self.client._request = AsyncMock(side_effect=[
            BalanceError("authentication_failed"), {"data": {"quota": Decimal("500000")}},
        ])
        text = await self.client.query("")
        self.assertIn("DeepSeek：余额查询认证失败", text)
        self.assertIn("余额：USD 1.00", text)
        self.assertEqual(self.client._request.await_count, 2)

    async def test_content_type_and_network_errors_are_handled_without_raw_details(self):
        self.content_type = "application/problem+json"
        self.assertIn("总余额：CNY 100.01", await self.client.query("DeepSeek"))
        self.content_type = "text/html"
        self.body = DEEPSEEK_KEY.encode()
        self.assertIn("无法识别", await self.client.query("DeepSeek"))
        with patch(
            "aiohttp.ClientSession.request", side_effect=aiohttp.ClientError(DEEPSEEK_KEY),
        ):
            text = await self.client.query("DeepSeek")
        self.assertIn("连接失败", text)
        self.assertNotIn(DEEPSEEK_KEY, text)

    async def test_closed_client_and_timeout_do_not_reopen_or_leak(self):
        await self.client.close()
        self.assertIn("已停止", await self.client.query("DeepSeek"))
        self.assertIsNone(self.client._session)
        self.assertEqual(self.requests, [])
        self.client = BalanceClient(self.client.settings)
        with patch(
            "aiohttp.ClientSession.request", side_effect=TimeoutError(DEEPSEEK_KEY),
        ):
            text = await self.client.query("DeepSeek")
        self.assertIn("超时", text)
        self.assertNotIn(DEEPSEEK_KEY, text)
