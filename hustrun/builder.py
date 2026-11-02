"""
Payload 构造器 — 将 SportRecord 转换为 UploadJsonSports 兼容的 JSON 字典。

对应 UploadJsonSports.build(SportBean, true) 的完全模拟。
字段映射参考逆向文档 §4。
"""

import json
from typing import Any

from .models import SportRecord, AuthConfig


def build_upload_json(record: SportRecord, auth: AuthConfig) -> dict[str, Any]:
    """
    构造 savesports 的 jsonsports 明文字段。

    模拟常规运动 UploadJsonSports.build(SportBean, true)：
    - coordinate 清洗掉 LitePal 内部字段 (baseObjId, sportbean_id)
    - activeTime 中 ' 替换为 :
    - odometer 若为 0 则用 coordinate 最后一个点 da/1000 兜底

    Args:
        record: 运动记录
        auth: 鉴权配置

    Returns:
        与 UploadJsonSports 字段一致的字典
    """
    # coordinate 轨迹点数组（清洗）
    coordinate = []
    for pt in record.points:
        coord = pt.to_coordinate_dict()
        # 清洗 LitePal 内部字段
        coord.pop("sportbean_id", None)
        coord.pop("baseObjId", None)
        coordinate.append(coord)

    # beganPoint / endPoint
    began_point = ""
    end_point = ""
    if record.points:
        began_point = f"{record.points[0].a}|{record.points[0].o}"
        end_point = f"{record.points[-1].a}|{record.points[-1].o}"

    # odometer 兜底
    odometer = record.dllc
    if not odometer or odometer in ("0", "0.0"):
        if record.points:
            odometer = str(record.points[-1].da / 1000.0)

    # activeTime: ' → :（Java 端做了这个替换）
    active_time = record.dl_time.replace("'", ":").replace('"', "").strip()

    # pace 数组
    pace = []
    for p in record.pace:
        pace.append({
            "km": p.km,
            "kmTotallength": p.km_total_length,
            "kmdlsj": p.km_dlsj,
            "t": p.t,
        })

    # minuteSpeed 数组
    minute_speed = []
    for ms in record.minute_speed:
        minute_speed.append({"min": ms.min, "v": ms.v})

    # 字段顺序与官方 Gson 产物保持一致，便于逐字节协议回归。
    payload = {
        "activeTime": active_time,
        "alreadyPassPoint": record.already_pass_point,
        "alreadyPassPointResult": "",
        "avgPace": record.avg_pace,
        "avgSpeed": record.avg_speed,
        "beganPoint": began_point,
        "beginTime": record.start_date,
        "calorie": record.cal,
        "coordinate": coordinate,
        "endPoint": end_point,
        "endTime": record.end_date,
        "indoor": record.indoor,
        "isValid": record.result,
        "isValidReason": "",
        "lastOdometerTime": record.last_odometer_time,
        "maxSpeedPerHour": record.max_speed_per_hour,
        "minSpeedPerHour": record.min_speed_per_hour,
        "minuteSpeed": minute_speed,
        "modementMode": record.sport_type,
        "name": record.name or auth.name,
        "needPassPointCount": "0",
        "odometer": odometer,
        "pace": pace,
        "phoneVersion": auth.phone_version,
        "planRouteName": record.route_name,
        "routeId": record.route_id,
        "sportId": record.sport_id,
        "stepCount": record.total_step,
        "stepMinute": record.step_minute,
        "xh": record.xh or auth.xh,
    }

    if record.route_polyline_bh:
        payload["routePolylineBh"] = record.route_polyline_bh

    # Gson 默认不序列化 null。随机打卡点只属于 sporttype=5，不能把普通运动
    # 错误地构造成空打卡运动。
    if record.sport_type == "5":
        payload["randomPointStr"] = []

    return payload


def build_json_string(record: SportRecord, auth: AuthConfig) -> str:
    """
    构造并序列化为 JSON 字符串。

    使用紧凑格式（无空格），模仿 Gson().toJson(uploadJsonSports)。
    排除值为 None 的字段，排除 LitePal 内部字段。
    """
    payload = build_upload_json(record, auth)
    return _serialize_gson(payload)


def _serialize_gson(payload: dict[str, Any]) -> str:
    """按 App 的 Gson 默认规则序列化 payload。"""
    # 紧凑 JSON（Gson 默认无空格），确保 ASCII 编码
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    # Gson 默认启用 HTML-safe escaping。运动配速中的单引号会写成
    # \u0027；保持这一行为可让明文和 AES 密文与官方 App 逐字节一致。
    for char, escaped in (
        ("'", "\\u0027"),
        ("<", "\\u003c"),
        (">", "\\u003e"),
        ("&", "\\u0026"),
        ("=", "\\u003d"),
        ("\u2028", "\\u2028"),
        ("\u2029", "\\u2029"),
    ):
        encoded = encoded.replace(char, escaped)
    return encoded


def build_sensor_zip_data(sensor_text: str) -> bytes:
    """
    准备传感器 ZIP 上传数据：gzip 压缩 → Base64 编码。

    对应 SportUploadModel.侂薶挂():
        gzip(sensorTxt) → Base64 → 写文件

    Args:
        sensor_text: 传感器原始 JSON 文本

    Returns:
        Base64 编码的 gzip 压缩数据
    """
    import base64
    import gzip
    import io

    output = io.BytesIO()
    with gzip.GzipFile(
        fileobj=output,
        mode="wb",
        compresslevel=6,
        mtime=0,
    ) as compressed:
        compressed.write(sensor_text.encode("utf-8"))
    return base64.encodebytes(output.getvalue())
