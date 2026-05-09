from __future__ import annotations

import unittest

from pal.llm.codex_app_server import (
    CodexAppServerAuthMessages,
    CodexAppServerClientInfo,
    is_chatgpt_auth_tokens_refresh_request,
    redact_codex_auth_message,
)


class OpenAICodexAppServerAuthProtocolTests(unittest.TestCase):
    def test_initialize_can_advertise_experimental_api_for_external_chatgpt_tokens(self) -> None:
        messages = CodexAppServerAuthMessages(
            client_info=CodexAppServerClientInfo(name="pal-test", title="Pal Test", version="1.2.3"),
            experimental_api=True,
        )

        self.assertEqual(
            messages.initialize_request(request_id=1),
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {"name": "pal-test", "title": "Pal Test", "version": "1.2.3"},
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        self.assertEqual(messages.initialized_notification(), {"method": "initialized", "params": {}})

    def test_chatgpt_login_start_requests_match_official_auth_types(self) -> None:
        messages = CodexAppServerAuthMessages()

        self.assertEqual(
            messages.login_chatgpt_browser_request(request_id=2),
            {
                "method": "account/login/start",
                "id": 2,
                "params": {"type": "chatgpt"},
            },
        )
        self.assertEqual(
            messages.login_chatgpt_device_code_request(request_id=3),
            {
                "method": "account/login/start",
                "id": 3,
                "params": {"type": "chatgptDeviceCode"},
            },
        )
        self.assertEqual(
            messages.login_chatgpt_auth_tokens_request(
                request_id=4,
                access_token="token-a",
                chatgpt_account_id="acct_1",
                chatgpt_plan_type="pro",
            ),
            {
                "method": "account/login/start",
                "id": 4,
                "params": {
                    "type": "chatgptAuthTokens",
                    "accessToken": "token-a",
                    "chatgptAccountId": "acct_1",
                    "chatgptPlanType": "pro",
                },
            },
        )

    def test_account_read_logout_rate_limits_and_external_token_refresh_shape(self) -> None:
        messages = CodexAppServerAuthMessages()

        self.assertEqual(
            messages.account_read_request(request_id=5, refresh_token=True),
            {"method": "account/read", "id": 5, "params": {"refreshToken": True}},
        )
        self.assertEqual(messages.logout_request(request_id=6), {"method": "account/logout", "id": 6})
        self.assertEqual(messages.rate_limits_read_request(request_id=7), {"method": "account/rateLimits/read", "id": 7})

        refresh_request = {"method": "account/chatgptAuthTokens/refresh", "id": 8, "params": {}}
        self.assertTrue(is_chatgpt_auth_tokens_refresh_request(refresh_request))
        self.assertEqual(
            messages.chatgpt_auth_tokens_refresh_response(
                request_id=8,
                access_token="token-b",
                chatgpt_account_id="acct_1",
                chatgpt_plan_type="pro",
            ),
            {
                "id": 8,
                "result": {
                    "accessToken": "token-b",
                    "chatgptAccountId": "acct_1",
                    "chatgptPlanType": "pro",
                },
            },
        )

    def test_codex_auth_message_redaction_hides_tokens(self) -> None:
        raw = {
            "method": "account/login/start",
            "id": 9,
            "params": {
                "type": "chatgptAuthTokens",
                "accessToken": "token-a",
                "chatgptAccountId": "acct_1",
            },
        }

        self.assertEqual(
            redact_codex_auth_message(raw),
            {
                "method": "account/login/start",
                "id": 9,
                "params": {
                    "type": "chatgptAuthTokens",
                    "accessToken": "<redacted>",
                    "chatgptAccountId": "acct_1",
                },
            },
        )
        self.assertEqual(raw["params"]["accessToken"], "token-a")


if __name__ == "__main__":
    unittest.main()
