"""上传端点回归：回溯记录必须走 v6.1/savesports。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hustrun.http_client as http_client
from hustrun.models import AuthConfig


AUTH = AuthConfig(
    token="t" * 40,
    secret="0123456789abcdef" * 4,
    sign="",
    timestamp="",
    web_view_time="",
    xh="U000000000",
    name="测试",
    imei="00000000-0000-0000-0000-000000000000",
    user_agent="hzkjapp/1.2.1",
)


class DummySigner:
    def sign(self, _secret, _target):
        return "1700000000000", "s" * 69


class SavesportsEndpointTest(unittest.TestCase):
    def test_posts_to_v61_savesports(self):
        # v7 会拒绝 beginTime 早于 sportId 创建时刻的回溯记录（24 数据异常）。
        response = mock.Mock(status_code=200, text="{}")
        response.json.return_value = {
            "code": 200,
            "data": {"sportId": "abc", "isValid": "1"},
        }
        with mock.patch.object(
            http_client.requests, "post", return_value=response
        ) as post:
            result = http_client.upload_savesports(
                AUTH, "ciphertext", signer=DummySigner()
            )

        self.assertTrue(post.called)
        url = post.call_args.args[0]
        self.assertTrue(url.endswith("/api/l/v6.1/savesports"), url)
        self.assertEqual(
            post.call_args.kwargs["data"], {"jsonsports": "ciphertext"}
        )
        self.assertTrue(result.success)
        self.assertEqual(result.is_valid, "1")


if __name__ == "__main__":
    unittest.main()
