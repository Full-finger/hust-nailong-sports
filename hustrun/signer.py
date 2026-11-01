"""桌面动态签名器 — 纯 Python 复刻官方 ChingoEncrypt 的 cgapiEnrypt。

SECRET 解出的 32 字节密钥 A、``SHA256(A | target | ms | reverse(A))`` 的
插字规则，以及 native 用 ``system_clock::now`` 生成的 64 位 timestamp
（``hustrun/nonce.py``）都已逆向完毕，因此签名完全在本进程里算：
不启动子进程、不加载 .so、不需要 APK 或 JVM。

SECRET 与签名结果不写日志、不进入异常文本。
"""

from __future__ import annotations

import base64
import hashlib
import time
from typing import Protocol

from Crypto.Cipher import AES

from .nonce import nonce as _native_nonce

# SECRET 里那段密文的 AES-256 密钥（从 native 常量池逆向）。
# 只用于解开 SECRET 得到签名密钥 A；它本身不是登录凭证。
_SECRET_BLOB_KEY = bytes.fromhex(
    "ad8dad2869c5e017303e5f48858db3129bf4d07a47d85beaaaa9d26f436d232a"
)


class RequestSigner(Protocol):
    """业务请求动态签名接口。"""

    def sign(self, secret: str, signing_target: str) -> tuple[str, str]:
        """对官方规范化签名串返回 ``(timestamp, sign)``。"""


class SignerError(RuntimeError):
    """签名器不可用或返回格式无效。"""


def derive_signing_key(secret: str) -> bytes:
    """把 SECRET 解成 native 使用的 32 字节签名密钥 A。

    SECRET 是 ``base64(4-byte BE total_len | 16-byte IV | 32-byte CT)``，
    ``A = AES-256-CBC-decrypt(KEY, IV, CT)``，A 是 32 个 ASCII hex 字符。
    """
    try:
        blob = base64.b64decode(secret, validate=True)
    except (ValueError, TypeError) as exc:
        raise SignerError("SECRET 不是合法的 base64") from exc
    if len(blob) < 52:
        raise SignerError("SECRET 长度不符合 native 格式")
    declared = int.from_bytes(blob[:4], "big")
    end = declared if 20 < declared <= len(blob) else len(blob)
    a = AES.new(_SECRET_BLOB_KEY, AES.MODE_CBC, blob[4:20]).decrypt(blob[20:end])
    if len(a) != 32:
        raise SignerError("SECRET 解出的签名密钥长度异常")
    return a


_HEX_VALUE = {
    **{c: c - 0x30 for c in range(0x30, 0x3A)},
    **{c: c - 0x61 + 10 for c in range(0x61, 0x67)},
    **{c: c - 0x41 + 10 for c in range(0x41, 0x47)},
}


def _percent_decode(data: bytes) -> bytes:
    """form 解码：%XX（大小写均可）解成字节，'+' 变空格，非法转义原样保留。"""
    out = bytearray()
    i = 0
    while i < len(data):
        c = data[i]
        if c == 0x2B:  # '+'
            out.append(0x20)
            i += 1
        elif c == 0x25 and i + 2 < len(data):  # '%XX'
            high = _HEX_VALUE.get(data[i + 1])
            low = _HEX_VALUE.get(data[i + 2])
            if high is None or low is None:
                out.append(c)
                i += 1
            else:
                out.append((high << 4) | low)
                i += 3
        else:
            out.append(c)
            i += 1
    return bytes(out)


def canonicalize_signing_target(signing_target: str | bytes) -> bytes:
    """复刻 native 对签名串的规范化。

    native 只处理第一个 '?' 之后的内容，前半段（path）原样保留。后半段按
    '&' 切段，每段按第一个 '=' 拆成 key/value：末尾不含 '=' 的段丢弃，
    key 为空的段跳过；value 做一次 form 解码，key 不解码。最后按
    ``(key, value)`` 的字节序排序，拼成 ``path + Σ(key + value)``。

    中间夹着不含 '=' 的段时，native 会把它折进下一段的 key 且丢掉该段
    value（实测 ``a&b=1`` → ``a&b``、``a=1&&b=2`` → ``&ba1``），后续
    还存在错位复制的行为。业务签名串永远由 key=value 组成，这里直接
    拒绝这种形态，避免静默算出错误签名。
    """
    raw = (
        signing_target.encode("utf-8")
        if isinstance(signing_target, str)
        else signing_target
    )
    q = raw.find(b"?")
    if q < 0:
        return raw
    segments = raw[q + 1:].split(b"&") if raw[q + 1:] else []
    while segments and b"=" not in segments[-1]:
        segments.pop()  # 末尾没有 '=' 的段 native 直接丢弃
    pairs = []
    for segment in segments:
        eq = segment.find(b"=")
        if eq < 0:
            raise SignerError(
                "签名串查询段混入了不含 '=' 的片段，无法用纯 Python 精确复刻"
            )
        if eq == 0:
            continue  # key 为空的段 native 会跳过
        pairs.append((segment[:eq], _percent_decode(segment[eq + 1:])))
    pairs.sort()
    return raw[:q] + b"".join(key + value for key, value in pairs)


def build_signature(a: bytes, signing_target: str | bytes, ms: int) -> str:
    """复刻 native 的 sign：先规范化签名串，再按固定位置插入 1/9/9/s/t。"""
    digest = hashlib.sha256(
        a
        + canonicalize_signing_target(signing_target)
        + int(ms).to_bytes(8, "big", signed=False)
        + a[::-1]
    ).hexdigest()
    return (
        digest[:12] + "1" + digest[12:27] + "9" + digest[27:34] + "9"
        + digest[34:35] + "s" + digest[35:54] + "t" + digest[54:]
    )


class PythonSigner:
    """纯 Python 签名器：与 native 的 (timestamp, sign) 逐字节一致。"""

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> "PythonSigner":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        pass

    def sign(self, secret: str, signing_target: str) -> tuple[str, str]:
        if not secret:
            raise SignerError("SECRET 为空，无法签名")
        a = derive_signing_key(secret)
        # native 用 system_clock::now() 的微秒值除以 1000 取整。
        ms = time.time_ns() // 1_000_000
        return str(_native_nonce(ms)), build_signature(a, signing_target, ms)
