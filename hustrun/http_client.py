"""
HTTP 客户端 — 鉴权头构建 & 上传端点调用。

对应 RetrofitFactory 拦截器链 + SchoolService 接口。
"""

import json
import re
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Mapping, Optional
from urllib.parse import urlencode

import requests

from .models import AuthConfig, UploadResult
from .crypto import encrypt_client_header, encrypt_sensor_data
from .signer import RequestSigner, SignerError


@dataclass
class ServerProbe:
    server_time: Optional[int] = None
    http_status: int = 0
    api_code: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.server_time is not None


@dataclass
class InSportTimeResult:
    """服务端对「当前是否处于该运动类型允许时段」的判定。"""

    allowed: Optional[bool] = None
    http_status: int = 0
    api_code: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.allowed is not None


@dataclass
class SportsIdResult:
    sport_id: str = ""
    http_status: int = 0
    api_code: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.sport_id)


@dataclass
class SportListResult:
    """服务端已有运动记录列表（只读查询）。"""

    records: list = field(default_factory=list)
    http_status: int = 0
    api_code: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.api_code == "200"


# ============================================================
# 请求头构建
# ============================================================

def build_headers(
    auth: AuthConfig,
    path: str,
    signer: Optional[RequestSigner],
    *,
    signing_target: Optional[str] = None,
) -> dict[str, str]:
    """
    构建所有 HTTP 请求头。

    模拟 RetrofitFactory 拦截器链注入的 header：
        Content-Type, imei, timestamp, sign,
        User-Agent, client, v1, cgAuthorization

    Args:
        auth: 鉴权配置

    Returns:
        header 字典
    """
    missing = auth.missing_session_fields + auth.missing_device_fields
    if missing:
        raise ValueError(
            "业务请求配置不完整，缺少: " + ", ".join(missing)
        )

    headers = {}

    # Content-Type（Retrofit 默认，form 请求会被覆盖）
    headers["Content-Type"] = "application/json;charset=UTF-8"

    if signer is None:
        raise ValueError("动态 signer 未配置，拒绝业务 API 请求")
    if auth.token_header != "cgAuthorization":
        raise ValueError(
            "token_header 必须为真实协议值 cgAuthorization"
        )

    # 设备指纹
    headers["imei"] = auth.imei

    # 官方拦截器对每个最终签名串即时调用 ChingoEncrypt。GET/JSON 请求的
    # 签名串就是 URL 的 /api... 部分；form 请求还会包含规范化字段。
    try:
        timestamp, sign = signer.sign(
            auth.secret,
            signing_target if signing_target is not None else path,
        )
    except SignerError as exc:
        raise ValueError(str(exc)) from exc
    headers["timestamp"] = timestamp
    headers["sign"] = sign

    # User-Agent
    headers["User-Agent"] = auth.user_agent
    # OkHttp's default wire headers observed in the authorized App HAR.
    # requests otherwise advertises additional compression codecs, which can
    # change the final business request shape behind some gateway layers.
    headers["Accept-Encoding"] = "gzip"
    headers["Connection"] = "Keep-Alive"

    # client 头（反作弊信标，实时生成）
    if len(auth.aes_key.encode("utf-8")) != 16:
        raise ValueError("SECRET 不完整，无法生成 16 字节 client 加密密钥")
    headers["client"] = encrypt_client_header(
        auth.aes_key,
        auth.is_root,
        "base64",
    )

    # v1 = MD5(保存 seed)；seed 为空时使用当前毫秒时间。
    v1_material = auth.v1_seed or str(int(time.time() * 1000))
    headers["v1"] = hashlib.md5(
        v1_material.encode("utf-8")
    ).hexdigest()

    # HUST 1.0.9 的真实 TOKEN 头。
    headers["cgAuthorization"] = auth.token

    return headers


def build_form_headers(
    auth: AuthConfig,
    path: str,
    signer: Optional[RequestSigner],
    form_data: Mapping[str, object],
) -> dict[str, str]:
    """
    构建 form-urlencoded 请求头。

    官方受保护拦截器会读取 FormBody，把 value 做 URLDecode，再放进
    TreeMap，最终签名 ``path?key=value&...``。因此这里必须先拿到最终
    form 字段，不能只签 path。
    """
    headers = build_headers(
        auth,
        path,
        signer,
        signing_target=build_form_signing_target(path, form_data),
    )
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    return headers


def build_form_signing_target(
    path: str,
    form_data: Mapping[str, object],
) -> str:
    """复现官方 ``Common(path, TreeMap)`` 的 form 签名串。"""
    if not form_data:
        return path

    # 当前业务字段名均为 ASCII。以 UTF-16BE code unit 排序可同时复现
    # Java String/TreeMap 对非 ASCII、补充平面字符的比较顺序。
    def java_string_key(value: str) -> bytes:
        return value.encode("utf-16-be", errors="surrogatepass")

    normalized = {
        str(name): "null" if value is None else str(value)
        for name, value in form_data.items()
    }
    fields = (
        f"{name}={normalized[name]}"
        for name in sorted(normalized, key=java_string_key)
    )
    return path + "?" + "&".join(fields)


def build_url_signing_target(
    path: str,
    query: Mapping[str, object],
) -> str:
    """生成与 requests 实际 URL 一致的 `/api...?...` 签名串。"""
    encoded = urlencode(query)
    return path if not encoded else f"{path}?{encoded}"


# ============================================================
# 服务器状态检查
# ============================================================

def probe_server_time(
    auth: AuthConfig,
    signer: Optional[RequestSigner] = None,
) -> ServerProbe:
    """
    查询服务器当前时间。

    对应: GET api/l/v6/currentTimeMillis

    Returns:
        服务器毫秒时间戳，失败返回 None
    """
    path = "/api/l/v6/currentTimeMillis"
    url = f"{auth.server_url}{path}"
    try:
        headers = build_headers(auth, path, signer)
    except ValueError as exc:
        return ServerProbe(message=str(exc))

    try:
        resp = requests.get(url, headers=headers, timeout=10)
    except requests.RequestException as exc:
        return ServerProbe(message=f"网络请求失败: {exc}")

    probe = ServerProbe(http_status=resp.status_code)
    if resp.status_code != 200:
        probe.message = _http_error_message(resp)
        return probe

    try:
        body = resp.json()
    except (requests.JSONDecodeError, json.JSONDecodeError, ValueError):
        probe.message = "服务器返回 HTTP 200，但响应不是 JSON"
        return probe

    ok, code, message = _api_status(body)
    probe.api_code = code
    probe.message = message
    if not ok or not isinstance(body, dict):
        return probe

    time_value = body.get("data")
    if isinstance(time_value, dict):
        time_value = time_value.get("currentTimeMillis") or time_value.get("time")
    try:
        probe.server_time = int(time_value)
    except (ValueError, TypeError):
        probe.message = "currentTimeMillis 响应缺少有效毫秒时间戳"
    return probe


def check_server_time(
    auth: AuthConfig,
    signer: Optional[RequestSigner] = None,
) -> Optional[int]:
    """兼容旧调用方；详细失败原因请使用 probe_server_time。"""
    return probe_server_time(auth, signer).server_time


def check_token_valid(
    auth: AuthConfig,
    signer: Optional[RequestSigner] = None,
) -> bool:
    """
    检查 TOKEN 是否有效（不抛出异常即为有效）。

    向 /currentTimeMillis 发请求，看是否返回 200。
    """
    return not auth.is_token_expired and probe_server_time(auth, signer).ok


def request_in_sport_time(
    auth: AuthConfig,
    sport_type_id: str = "7",
    signer: Optional[RequestSigner] = None,
) -> InSportTimeResult:
    """查询服务端的运动时段判定。

    对应 ``GET api/l/v6.1/sporttype/inSportTime?sportTypeId=7``。官方
    选择运动类型时会先问服务端，返回 ``"1"`` 才继续；返回 ``"0"`` 时
    App 只是弹一个「不在运动时段」的提示（仍可继续）。本地 6–23 点的
    判断终究是猜测，服务端才是准的。
    """
    path = "/api/l/v6.1/sporttype/inSportTime"
    query = {"sportTypeId": str(sport_type_id)}
    url = f"{auth.server_url}{path}"
    try:
        headers = build_headers(
            auth,
            build_url_signing_target(path, query),
            signer,
        )
    except ValueError as exc:
        return InSportTimeResult(message=str(exc))

    try:
        response = requests.get(url, params=query, headers=headers, timeout=10)
    except requests.RequestException as exc:
        return InSportTimeResult(message=f"网络请求失败: {exc}")

    result = InSportTimeResult(http_status=response.status_code)
    if response.status_code != 200:
        result.message = _http_error_message(response)
        return result
    try:
        body = response.json()
    except (requests.JSONDecodeError, json.JSONDecodeError, ValueError):
        result.message = "inSportTime 返回 HTTP 200，但响应不是 JSON"
        return result

    ok, code, message = _api_status(body)
    result.api_code = code
    result.message = message
    if not ok or not isinstance(body, dict):
        return result
    value = body.get("data")
    text = "" if value is None else str(value).strip()
    if text not in {"0", "1"}:
        result.message = f"inSportTime 返回了无法识别的值: {text[:20]}"
        return result
    result.allowed = text == "1"
    return result


def request_sports_id(
    auth: AuthConfig,
    signer: Optional[RequestSigner] = None,
) -> SportsIdResult:
    """向服务端申请官方运动 ID。

    对应运动开始链的 ``GET api/l/v7/sportsId``。自主运动不能用客户端随机
    UUID 替代该值，否则缺少服务端创建的运动链上下文。
    """
    path = "/api/l/v7/sportsId"
    url = f"{auth.server_url}{path}"
    try:
        headers = build_headers(auth, path, signer)
    except ValueError as exc:
        return SportsIdResult(message=str(exc))

    try:
        response = requests.get(url, headers=headers, timeout=10)
    except requests.RequestException as exc:
        return SportsIdResult(message=f"网络请求失败: {exc}")

    result = SportsIdResult(http_status=response.status_code)
    if response.status_code != 200:
        result.message = _http_error_message(response)
        return result
    try:
        body = response.json()
    except (requests.JSONDecodeError, json.JSONDecodeError, ValueError):
        result.message = "sportsId 返回 HTTP 200，但响应不是 JSON"
        return result

    ok, code, message = _api_status(body)
    result.api_code = code
    result.message = message
    if not ok or not isinstance(body, dict):
        return result
    value = body.get("data")
    if not isinstance(value, str) or not value.strip():
        result.message = "sportsId 响应缺少有效 ID"
        return result
    result.sport_id = value.strip()
    return result


def request_sport_list(
    auth: AuthConfig,
    signer: Optional[RequestSigner] = None,
) -> SportListResult:
    """查询服务端已有的运动记录列表（只读）。

    对应运动记录页的 ``GET api/l/v6/sportlist?xh=<学号>``。路径取自 App
    脱壳 dex（ExerciseRecordActivity 调用链），参数名与 ExerciseRecord-
    Presenter 反汇编一致。
    """
    path = "/api/l/v6/sportlist"
    query = {"xh": auth.xh}
    url = f"{auth.server_url}{path}"
    try:
        headers = build_headers(
            auth,
            build_url_signing_target(path, query),
            signer,
        )
    except ValueError as exc:
        return SportListResult(message=str(exc))

    try:
        response = requests.get(url, params=query, headers=headers, timeout=15)
    except requests.RequestException as exc:
        return SportListResult(message=f"网络请求失败: {exc}")

    result = SportListResult(http_status=response.status_code)
    if response.status_code != 200:
        result.message = _http_error_message(response)
        return result
    try:
        body = response.json()
    except (requests.JSONDecodeError, json.JSONDecodeError, ValueError):
        result.message = "sportlist 返回 HTTP 200，但响应不是 JSON"
        return result

    ok, code, message = _api_status(body)
    result.api_code = code
    result.message = message
    if not ok or not isinstance(body, dict):
        return result
    value = body.get("data")
    if not isinstance(value, list):
        result.message = "sportlist 响应缺少记录列表"
        return result
    result.records = value
    return result


# ============================================================
# 上传端点
# ============================================================

def upload_savesports(
    auth: AuthConfig,
    encrypted_payload: str,
    signer: Optional[RequestSigner] = None,
) -> UploadResult:
    """
    上传主运动记录。

    对应: POST api/l/v6.1/savesports (@FormUrlEncoded)
         field: jsonsports = <AES密文>

    v6.1 不校验 ``beginTime`` 是否早于 sportId 的服务端创建时刻，因此记录
    可以整体回溯到当前时间之前再提交；v7 会以 ``24``（数据异常）拒绝这种
    记录。提交后的 ``isValid``/``checkStatus`` 与 v7 成功记录一致。

    Args:
        auth: 鉴权配置
        encrypted_payload: AES 加密后的 JSON 密文字符串

    Returns:
        UploadResult
    """
    path = "/api/l/v6.1/savesports"
    url = f"{auth.server_url}{path}"
    data = {"jsonsports": encrypted_payload}
    missing = auth.missing_session_fields + auth.missing_device_fields
    if missing:
        return UploadResult(
            success=False,
            raw_response=f"上传配置不完整，缺少: {', '.join(missing)}",
        )

    try:
        headers = build_form_headers(auth, path, signer, data)
    except ValueError as exc:
        return UploadResult(success=False, raw_response=str(exc))

    try:
        resp = requests.post(url, headers=headers, data=data, timeout=30)
        raw_text = resp.text

        result = UploadResult(
            success=False,
            raw_response=raw_text,
            http_status=resp.status_code,
        )

        if resp.status_code != 200:
            result.raw_response = _http_error_message(resp)
            return result

        try:
            body = resp.json()
        except (requests.JSONDecodeError, json.JSONDecodeError, ValueError):
            result.raw_response = "服务器返回 HTTP 200，但响应不是 JSON"
            return result

        ok, code, message = _api_status(body)
        result.api_code = code
        result.api_message = message
        if not ok:
            result.raw_response = f"业务请求失败 code={code}: {message}"
            return result

        api_data = body.get("data", body) if isinstance(body, dict) else None
        if not isinstance(api_data, dict):
            result.raw_response = "savesports 响应缺少 data 对象"
            return result
        if "sportId" not in api_data or "isValid" not in api_data:
            result.raw_response = "savesports 响应缺少 sportId/isValid，不能判定上传成功"
            return result

        result.is_valid = str(api_data.get("isValid", "0"))
        result.in_valid_reason = str(
            api_data.get("inValidReason")
            or api_data.get("isValidReason")
            or ""
        )
        result.check_status = str(api_data.get("checkStatus", ""))
        result.tip = str(api_data.get("tip", ""))
        result.sport_id = str(api_data.get("sportId", ""))
        result.success = True

        return result

    except requests.RequestException as e:
        return UploadResult(
            success=False,
            raw_response=str(e),
        )


def upload_sensor_zip(
    auth: AuthConfig,
    sport_id: str,
    zip_data: bytes,
    filename: Optional[str] = None,
    signer: Optional[RequestSigner] = None,
) -> bool:
    """
    上传传感器数据（gzip + Base64 版）。

    对应: POST api/l/v7/upload/sensorZipFile (multipart)
         @Query sportId + multipart file

    Args:
        auth: 鉴权配置
        sport_id: 运动记录 ID
        zip_data: gzip 压缩后 Base64 编码的数据

    Returns:
        是否上传成功
    """
    path = "/api/l/v7/upload/sensorZipFile"
    url = f"{auth.server_url}{path}"
    filename = filename or f"{sport_id}.txt"
    if any(char in filename for char in ('\r', '\n', '"')):
        return False
    params = {"sportId": sport_id, "sensorZipFile": filename}
    try:
        headers = build_headers(
            auth,
            path,
            signer,
            signing_target=build_url_signing_target(path, params),
        )
    except ValueError:
        return False
    try:
        boundary = str(uuid.uuid4())
        part_headers = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{filename}"; '
            f'filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n"
            f"Content-Length: {len(zip_data)}\r\n"
            "\r\n"
        ).encode("utf-8")
        body = (
            part_headers
            + zip_data
            + f"\r\n--{boundary}--\r\n".encode("ascii")
        )
        headers["Content-Type"] = (
            f"multipart/form-data; boundary={boundary}"
        )
        resp = requests.post(
            url,
            headers=headers,
            params=params,
            data=body,
            timeout=30,
        )
        return _response_succeeded(resp)
    except (requests.RequestException, ValueError):
        return False


def upload_sensor_file(
    auth: AuthConfig,
    sport_id: str,
    sensor_text: str,
    signer: Optional[RequestSigner] = None,
) -> bool:
    """
    上传传感器数据（AES 加密版）。

    对应: POST api/l/v7/upload/sensorFile (form)
         @Field sportId + sensorFile

    Args:
        auth: 鉴权配置
        sport_id: 运动记录 ID
        sensor_text: 传感器原始文本

    Returns:
        是否上传成功
    """
    path = "/api/l/v7/upload/sensorFile"
    url = f"{auth.server_url}{path}"

    encrypted = encrypt_sensor_data(
        sensor_text, auth.aes_key, auth.aes_encoding
    )

    data = {
        "sportId": sport_id,
        "sensorFile": encrypted,
    }

    try:
        headers = build_form_headers(auth, path, signer, data)
    except ValueError:
        return False

    try:
        resp = requests.post(url, headers=headers, data=data, timeout=30)
        return _response_succeeded(resp)
    except (requests.RequestException, ValueError):
        return False


def _api_status(body) -> tuple[bool, str, str]:
    if not isinstance(body, dict):
        return False, "", f"响应类型异常: {type(body).__name__}"
    code = body.get("code")
    message = str(body.get("message") or body.get("msg") or "")
    if body.get("success") is False:
        return False, str(code or ""), message or "success=false"
    if code is None and body.get("success") is not True:
        return False, "", message or "响应缺少业务状态 code"
    if code not in (None, 0, 200, "0", "200"):
        return False, str(code), message or "未知业务错误"
    return True, "" if code is None else str(code), message


def _response_succeeded(response: requests.Response) -> bool:
    if response.status_code != 200:
        return False
    try:
        body = response.json()
    except (requests.JSONDecodeError, json.JSONDecodeError, ValueError):
        return False
    return _api_status(body)[0]


def _http_error_message(response: requests.Response) -> str:
    content_type = response.headers.get("Content-Type", "")
    if "html" in content_type.lower() and not response.encoding:
        response.encoding = response.apparent_encoding
    elif "html" in content_type.lower() and response.encoding.lower() in {
        "iso-8859-1",
        "latin-1",
    }:
        response.encoding = response.apparent_encoding
    summary = response.text[:300].replace("\r", " ").replace("\n", " ").strip()
    if "<html" in summary.lower():
        title = ""
        match = re.search(
            r"<title[^>]*>(.*?)</title>",
            response.text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            title = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        summary = title or "HTML 拦截页"
    return f"HTTP {response.status_code} ({content_type}): {summary}"
