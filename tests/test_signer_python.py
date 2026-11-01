"""纯 Python 签名器回归测试：native nonce 向量与签名串规范化。

向量里的数值只由毫秒时间戳决定，不含任何凭证；签名串样本全部是合成
的路径/查询，也不含真实 SECRET。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hustrun.nonce import nonce
from hustrun.signer import (
    SignerError,
    build_signature,
    canonicalize_signing_target,
)


class NonceTest(unittest.TestCase):
    """``T = F(ms)``：向量取自 native 的定时钟采样。"""

    VECTORS = (
        (1792980000000, 9700844518142346010),
        (1792980234567, 10510588983020170643),
        (1792980358546, 15914751984504218043),
        (1792979000000, 7829393877569493284),
    )

    def test_native_vectors(self) -> None:
        for ms, expected in self.VECTORS:
            self.assertEqual(nonce(ms), expected, ms)

    def test_masks_to_64_bits(self) -> None:
        ms = self.VECTORS[0][0]
        self.assertEqual(nonce(ms + (1 << 64)), nonce(ms))


class CanonicalizeTest(unittest.TestCase):
    """规范化结果取自 native 内存里实际的 SHA256 输入。"""

    CASES = (
        # 没有查询串：原样返回
        ("/api/l/v6/currentTimeMillis", b"/api/l/v6/currentTimeMillis"),
        # 去掉 '?'、'='、'&'，按 (key, value) 排序后拼接
        ("/wq?a=z&ab=1", b"/wqazab1"),
        ("/wt?a=9&a0=1", b"/wta9a01"),
        ("/wr?x=z&x=a", b"/wrxaxz"),
        ("/wf?x=2&x=1&x=3", b"/wfx1x2x3"),
        ("/wz?aB=1&aa=2", b"/wzaB1aa2"),
        # value 做一次 form 解码，key 与 path 不解码
        ("/w0?x=a+b", b"/w0xa b"),
        ("/w1?x=a%2Bb", b"/w1xa+b"),
        ("/w2?x=%2520", b"/w2x%20"),
        ("/w3?x=%zz", b"/w3x%zz"),
        ("/wb?x=%E4%B8%AD", b"/wbx\xe4\xb8\xad"),
        ("/u4?a=b%26c", b"/u4ab&c"),
        ("/u0?a%20b=1", b"/u0a%20b1"),
        ("/va%61?x=1", b"/va%61x1"),
        # 空 key 跳过；末尾不是 key=value 的段丢弃
        ("/w6?a=1&=2&b=3", b"/w6a1b3"),
        ("/w7?=1&a=2", b"/w7a2"),
        ("/wh?x=1&y", b"/whx1"),
        ("/p3?a=1&", b"/p3a1"),
        ("/p5?a", b"/p5"),
        # 非分隔符字符原样保留
        ("/ve?x=1;y=2", b"/vex1;y=2"),
    )

    def test_cases(self) -> None:
        for target, expected in self.CASES:
            self.assertEqual(
                canonicalize_signing_target(target), expected, target
            )

    def test_malformed_segments_rejected(self) -> None:
        # native 会把不含 '=' 的段折进下一段 key，纯 Python 复刻不了，
        # 宁可报错也不要静默签出错误结果。
        for target in (
            "/api?&&k=v",
            "/api?k=v&&x=1",
            "/t1?x&y=1",
            "/tf?&x=1",
            "/ta?x&&a=1",
        ):
            with self.assertRaises(SignerError, msg=target):
                canonicalize_signing_target(target)


class SignatureTest(unittest.TestCase):
    """摘要插字规则（12/28/36/38/58 位置的 1/9/9/s/t）。"""

    # 合成密钥，只用于验证插字位置。
    KEY = b"0123456789abcdef0123456789abcdef"
    MS = 1792980518965

    def test_plain_path(self) -> None:
        self.assertEqual(
            build_signature(self.KEY, "/api/l/v6/currentTimeMillis", self.MS),
            "16a848f1f11013f702adb5b95c579dc36ba39c"
            "s39209ab3ef1e83c6dd4t50cc0a794a",
        )

    def test_sorted_target(self) -> None:
        self.assertEqual(
            build_signature(self.KEY, "/wq?a=z&ab=1", self.MS),
            "dcf081d013ce1f37fa883596a048969f7ab09a"
            "s79a3b669c61178cfe50t3906c13155",
        )

    def test_insertion_positions(self) -> None:
        sign = build_signature(self.KEY, "/api/l/v6/currentTimeMillis", self.MS)
        self.assertEqual(len(sign), 69)
        for index, char in ((12, "1"), (28, "9"), (36, "9"), (38, "s"), (58, "t")):
            self.assertEqual(sign[index], char, index)


if __name__ == "__main__":
    unittest.main()
