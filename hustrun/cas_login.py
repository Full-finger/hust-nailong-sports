"""
HUST CAS 登录。

当前统一身份认证页面使用以下流程：
1. GET cgapp-server/cas/appLogin，并跟随重定向到 pass.hust.edu.cn。
2. POST /cas/rsa 获取临时 RSA 公钥。
3. 使用 RSA/PKCS#1 v1.5 加密账号和密码，分别提交为 ul/pl。
4. 保持同一 Cookie 会话提交 lt/execution/_eventId 等表单字段。
5. 从 cgapp-server 返回页的 getUserName(uid, tokenBundle) JS 回调取凭证。

不要在这里猜测 CAS REST/AJAX 端点。学校当前页面没有使用这些流程，盲目尝试
既会掩盖真实错误，也可能触发验证码或账号风控。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA


@dataclass
class LoginResult:
    success: bool
    token: str = ""
    secret: str = ""
    login_out_time: str = ""
    sign: str = ""
    timestamp: str = ""
    web_view_time: str = ""
    xh: str = ""
    name: str = ""
    error: str = ""
    trace: list[str] = field(default_factory=list)

    @property
    def extra(self) -> str:
        """兼容旧调用方；第五段实际是 LOGIN_OUT_TIME。"""
        return self.login_out_time


class _CASFormParser(HTMLParser):
    """解析 loginForm、隐藏字段和页面上真正可见的错误文本。"""

    def __init__(self):
        super().__init__()
        self.inputs: dict[str, str] = {}
        self.form_action = ""
        self.error_text = ""
        self.captcha_url = ""
        self._in_login_form = False
        self._in_error = False
        self._error_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attrs_dict = {k: v or "" for k, v in attrs}
        if tag == "form":
            is_login = attrs_dict.get("id") == "loginForm"
            if is_login or not self.form_action:
                self.form_action = attrs_dict.get("action", "")
                self._in_login_form = True
            return

        if tag == "input" and self._in_login_form and attrs_dict.get("name"):
            self.inputs[attrs_dict["name"]] = attrs_dict.get("value", "")

        if tag == "img" and attrs_dict.get("id") == "codeImage":
            self.captcha_url = attrs_dict.get("src", "")

        classes = set(attrs_dict.get("class", "").split())
        if (
            attrs_dict.get("id") in {"errormsg", "errormsghide"}
            or "error-msg-box" in classes
        ):
            self._in_error = True

    def handle_endtag(self, tag: str):
        if tag == "form":
            self._in_login_form = False
        if self._in_error and tag in {"div", "span", "p"}:
            self._in_error = False
            self.error_text = " ".join(self._error_parts).strip()

    def handle_data(self, data: str):
        if self._in_error and data.strip():
            self._error_parts.append(data.strip())


def cas_login(
    server_url: str,
    username: str,
    password: str,
    captcha_callback: Optional[Callable[[bytes, str], str]] = None,
    dynamic_code_callback: Optional[Callable[[str], str]] = None,
) -> LoginResult:
    """按 HUST 当前网页登录流程认证并获取 App token bundle。"""
    session = requests.Session()
    _set_session_headers(session)
    server_url = server_url.rstrip("/")
    trace: list[str] = []

    entry_url = f"{server_url}/cas/appLogin"
    try:
        page = session.get(entry_url, allow_redirects=True, timeout=30)
    except requests.RequestException as exc:
        return LoginResult(False, error=f"无法打开 CAS 登录页: {exc}", trace=trace)

    trace.extend(_redirect_trace(page))
    if page.status_code != 200:
        return LoginResult(
            False,
            error=f"CAS 登录页返回 HTTP {page.status_code}",
            trace=trace,
        )

    final_host = urlparse(page.url).hostname or ""
    if final_host.lower() != "pass.hust.edu.cn":
        return LoginResult(
            False,
            error=f"CAS 重定向目标异常: {page.url}",
            trace=trace,
        )

    parser = _CASFormParser()
    parser.feed(page.text)
    if not parser.form_action:
        return LoginResult(
            False,
            error="CAS 页面中未找到 loginForm；页面结构可能已经更新",
            trace=trace,
        )

    try:
        public_key = _fetch_rsa_public_key(session, page.url)
        encrypted_user = _rsa_encrypt(username.strip(), public_key)
        encrypted_password = _rsa_encrypt(password.strip(), public_key)
    except (requests.RequestException, ValueError, KeyError) as exc:
        return LoginResult(False, error=f"CAS RSA 初始化失败: {exc}", trace=trace)

    captcha_code = ""
    if parser.captcha_url:
        if captcha_callback is None:
            return LoginResult(
                False,
                error="CAS 当前要求图形验证码，但调用方没有提供验证码输入",
                trace=trace,
            )
        try:
            image, suffix = _fetch_captcha(
                session,
                page.url,
                parser.captcha_url,
            )
            captcha_code = captcha_callback(image, suffix).strip()
        except requests.RequestException as exc:
            return LoginResult(
                False,
                error=f"CAS 验证码下载失败: {exc}",
                trace=trace,
            )
        if not captcha_code:
            return LoginResult(False, error="未输入验证码", trace=trace)

    # 浏览器提交前会禁用可见的 un/pd，只提交隐藏的 ul/pl。
    post_data = dict(parser.inputs)
    for key in ("un", "pd", "username", "password", "rsa"):
        post_data.pop(key, None)
    post_data.update(
        {
            "ul": encrypted_user,
            "pl": encrypted_password,
            "ua": _browser_ua_json(session.headers["User-Agent"]),
            "visitorId": _visitor_id(session.headers["User-Agent"]),
            "code": captcha_code,
            "_eventId": post_data.get("_eventId") or "submit",
        }
    )

    form_url = urljoin(page.url, parser.form_action)
    try:
        response = session.post(
            form_url,
            data=post_data,
            headers={
                "Origin": f"{urlparse(page.url).scheme}://{urlparse(page.url).netloc}",
                "Referer": page.url,
            },
            allow_redirects=True,
            timeout=30,
        )
    except requests.RequestException as exc:
        return LoginResult(False, error=f"CAS 表单提交失败: {exc}", trace=trace)

    trace.extend(_redirect_trace(response))

    second_parser = _CASFormParser()
    second_parser.feed(response.text)
    if _is_dynamic_code_challenge(second_parser.error_text):
        if dynamic_code_callback is None:
            return LoginResult(
                False,
                error=f"CAS: {second_parser.error_text}",
                trace=trace,
            )
        dynamic_code = dynamic_code_callback(second_parser.error_text).strip()
        if not dynamic_code:
            return LoginResult(False, error="未输入动态验证码", trace=trace)
        if not second_parser.form_action:
            return LoginResult(
                False,
                error="CAS 动态验证码页面缺少 loginForm",
                trace=trace,
            )

        try:
            second_key = _fetch_rsa_public_key(session, response.url)
            second_data = _build_post_data(
                second_parser,
                username,
                password,
                second_key,
                session.headers["User-Agent"],
                captcha_code="",
                dynamic_code=dynamic_code,
            )
            response = session.post(
                urljoin(response.url, second_parser.form_action),
                data=second_data,
                headers={
                    "Origin": (
                        f"{urlparse(response.url).scheme}://"
                        f"{urlparse(response.url).netloc}"
                    ),
                    "Referer": response.url,
                },
                allow_redirects=True,
                timeout=30,
            )
        except (requests.RequestException, ValueError, KeyError) as exc:
            return LoginResult(
                False,
                error=f"CAS 动态验证码提交失败: {exc}",
                trace=trace,
            )
        trace.extend(_redirect_trace(response))

    parsed = _parse_login_response(response)
    parsed.trace = trace
    return parsed


def _set_session_headers(session: requests.Session):
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 14; Mobile) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Mobile Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )


def _build_post_data(
    parser: _CASFormParser,
    username: str,
    password: str,
    public_key: str,
    user_agent: str,
    captcha_code: str = "",
    dynamic_code: str = "",
) -> dict[str, str]:
    data = dict(parser.inputs)
    for key in ("un", "pd", "username", "password", "rsa"):
        data.pop(key, None)
    data.update(
        {
            "ul": _rsa_encrypt(username.strip(), public_key),
            "pl": _rsa_encrypt(password.strip(), public_key),
            "ua": _browser_ua_json(user_agent),
            "visitorId": _visitor_id(user_agent),
            "code": captcha_code,
            "phoneCode": dynamic_code,
            "_eventId": data.get("_eventId") or "submit",
        }
    )
    return data


def _is_dynamic_code_challenge(message: str) -> bool:
    # 首次触发通常返回“验证码已发送到企业微信”；若服务端已经记住该终端
    # 处于二次认证状态，空 phoneCode 会直接返回“动态验证码错误”。两者都
    # 应进入同一个 e1s2 输入流程，而不是提前终止。
    return bool(message and "动态验证码" in message)


def _fetch_rsa_public_key(session: requests.Session, login_url: str) -> str:
    rsa_url = urljoin(login_url, "rsa")
    response = session.post(
        rsa_url,
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": login_url,
            "X-Requested-With": "XMLHttpRequest",
        },
        timeout=15,
    )
    response.raise_for_status()
    body = response.json()
    key = body.get("publicKey") if isinstance(body, dict) else None
    if not key:
        raise ValueError("响应中缺少 publicKey")
    return str(key)


def _fetch_captcha(
    session: requests.Session,
    login_url: str,
    captcha_path: str,
) -> tuple[bytes, str]:
    url = urljoin(login_url, captcha_path)
    response = session.get(
        url,
        headers={
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": login_url,
        },
        timeout=15,
    )
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "").lower()
    if not content_type.startswith("image/") or not response.content:
        raise requests.RequestException(
            f"验证码响应格式异常: {content_type or 'unknown'}"
        )
    suffix = {
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(content_type.split(";", 1)[0], ".jpg")
    if response.content.startswith((b"GIF87a", b"GIF89a")):
        suffix = ".gif"
    elif response.content.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    return response.content, suffix


def _rsa_encrypt(value: str, public_key: str) -> str:
    if not value:
        raise ValueError("用户名和密码不能为空")
    try:
        key_material: str | bytes = public_key
        if "BEGIN" not in public_key:
            key_material = base64.b64decode(public_key)
        key = RSA.import_key(key_material)
    except (ValueError, IndexError, TypeError) as exc:
        raise ValueError("CAS 返回了无法解析的 RSA 公钥") from exc
    encrypted = PKCS1_v1_5.new(key).encrypt(value.encode("utf-8"))
    return base64.b64encode(encrypted).decode("ascii")


def _browser_ua_json(user_agent: str) -> str:
    """生成与 UAParser 输出结构相近的最小 JSON；服务端可接受附加字段缺失。"""
    return json.dumps(
        {
            "ua": user_agent,
            "browser": {"name": "Chrome", "major": "120"},
            "device": {"type": "mobile"},
            "os": {"name": "Android", "version": "14"},
        },
        separators=(",", ":"),
    )


def _visitor_id(user_agent: str) -> str:
    # FingerprintJS 的真实 visitorId 与浏览器特征有关。CLI 没有浏览器环境，
    # 使用本次客户端特征的稳定摘要，避免空字段；若服务端强制校验应改走 WebView。
    return hashlib.sha256(user_agent.encode("utf-8")).hexdigest()[:32]


def _redirect_trace(response: requests.Response) -> list[str]:
    trace = []
    for item in response.history:
        trace.append(
            f"{item.status_code} {item.url} -> {item.headers.get('Location', '')}"
        )
    trace.append(f"{response.status_code} {response.url}")
    return trace


def _parse_login_response(response: requests.Response) -> LoginResult:
    content_type = response.headers.get("Content-Type", "").lower()
    if "json" in content_type:
        return _parse_json_login_response(response)

    callback = _extract_login_callback(response.text)
    if callback:
        uid, token_bundle = callback
        result = _parse_token_string(token_bundle)
        result.xh = uid
        return result

    parser = _CASFormParser()
    parser.feed(response.text)
    if parser.error_text:
        return LoginResult(False, error=f"CAS: {parser.error_text}")

    if urlparse(response.url).hostname == "pass.hust.edu.cn":
        return LoginResult(
            False,
            error="CAS 未返回登录凭证；可能需要验证码、二次认证，或账号信息不正确",
        )

    return LoginResult(
        False,
        error=f"App 登录回调格式未知: HTTP {response.status_code}, URL={response.url}",
    )


def _parse_json_login_response(response: requests.Response) -> LoginResult:
    try:
        body = response.json()
    except (requests.JSONDecodeError, json.JSONDecodeError, ValueError):
        return LoginResult(False, error="登录接口返回了无效 JSON")

    if not isinstance(body, dict):
        return LoginResult(False, error=f"登录响应类型异常: {type(body).__name__}")

    code = body.get("code")
    if code not in (None, 0, 200, "0", "200"):
        message = body.get("message") or body.get("msg") or "登录失败"
        return LoginResult(False, error=f"登录接口 code={code}: {message}")

    data = body.get("data", body)
    if not isinstance(data, dict):
        return LoginResult(False, error="登录响应缺少 data 对象")

    token_bundle = data.get("token") or body.get("token") or ""
    result = _parse_token_string(str(token_bundle))
    info = data.get("info") or {}
    if isinstance(info, dict):
        result.xh = str(info.get("xh") or info.get("uid") or "")
        result.name = str(info.get("xm") or info.get("name") or "")
    return result


def _parse_token_string(token_bundle: str) -> LoginResult:
    """解析 APK getUserName 回调中的 JWT.SECRET.LOGIN_OUT_TIME 五段值。"""
    parts = token_bundle.split(".")
    if len(parts) < 5:
        return LoginResult(
            False,
            error=f"登录凭证格式异常：收到 {len(parts)} 段，App 需要至少 5 段",
        )
    return LoginResult(
        True,
        token=".".join(parts[:3]),
        secret=parts[3],
        login_out_time=parts[4],
    )


def _extract_login_callback(html: str) -> tuple[str, str] | None:
    match = re.search(
        r'''getUserName\s*\(\s*["']([^"']*)["']\s*,\s*["']([^"']+)["']''',
        html,
    )
    if not match:
        return None
    return match.group(1), match.group(2)


def _extract_token_from_html(html: str) -> str:
    """兼容旧测试/调用方。"""
    callback = _extract_login_callback(html)
    return callback[1] if callback else ""


def update_header_xml(
    header_xml_path: str,
    login_result: LoginResult,
    backup: bool = True,
) -> bool:
    """更新 CAS 回调能够确定的字段，并清除与旧 SECRET 绑定的派生状态。"""
    if not login_result.success:
        raise ValueError("不能用失败的登录结果更新 HEADER.xml")

    if backup:
        shutil.copy2(header_xml_path, header_xml_path + ".bak")

    tree = ET.parse(header_xml_path)
    root = tree.getroot()
    updates = {
        "TOKEN": login_result.token,
        "SECRET": login_result.secret,
        "LOGIN_OUT_TIME": login_result.login_out_time,
        # SIGN/WEB_VIEW_TIME/CGAPP_TIME_KEY 与旧 SECRET 绑定，不能静默复用。
        "SIGN": login_result.sign,
        "WEB_VIEW_TIME": login_result.web_view_time,
        "CGAPP_TIME_KEY": login_result.timestamp,
    }

    existing = {elem.get("name"): elem for elem in root.findall("string")}
    for name, value in updates.items():
        elem = existing.get(name)
        if elem is None:
            elem = ET.SubElement(root, "string", {"name": name})
        elem.text = value

    tree.write(header_xml_path, encoding="utf-8", xml_declaration=True)
    return True
