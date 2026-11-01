"""
JSON 数据源读取器 — 从 sport_*.json 导出文件读取运动记录。

JSON 格式来自数据库导出（sportbean 记录 + sportlatlngbean points 数组），
不含 pace/minute_speed 子表数据。
"""

import json
from pathlib import Path
from typing import Optional

from .models import SportRecord, TrackPoint, PaceEntry, MinuteSpeedEntry


def load_sport_json(json_path: str, sport_id: Optional[str] = None) -> SportRecord:
    """
    从 JSON 导出文件加载运动记录。

    文件格式：
    {
        "record": { ... sportbean 字段 ... },
        "points": [ ... sportlatlngbean 点 ... ],
        "point_count": 431
    }

    Args:
        json_path: JSON 文件路径
        sport_id: 如果指定，验证 sportid 是否匹配

    Returns:
        SportRecord 对象

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: 格式不正确或 sportid 不匹配
    """
    path = Path(json_path)
    if not path.exists():
        raise FileNotFoundError(f"JSON 数据文件不存在: {json_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    record_data = data.get("record", {})
    points_data = data.get("points", [])

    if not record_data:
        raise ValueError("JSON 中缺少 'record' 字段")

    # 检查 sportid
    file_sport_id = record_data.get("sportid", "")
    if sport_id and file_sport_id != sport_id:
        raise ValueError(
            f"JSON 中 sportid ({file_sport_id}) 与请求的 ({sport_id}) 不匹配"
        )

    # 解析轨迹点
    points = []
    for pt in points_data:
        points.append(TrackPoint(
            a=pt.get("a", 0.0),
            o=pt.get("o", 0.0),
            v=pt.get("v", 1),
            s=pt.get("s", 0.0),
            t=pt.get("t", 0),
            ac=pt.get("ac", 0.0),
            da=pt.get("da", 0.0),
            d=pt.get("d", 0.0),
            st=pt.get("st", 0),
            sta=pt.get("sta", 0),
            sportbean_id=file_sport_id,
        ))

    # 解析配速数据（JSON 中可能没有，需要从 DB 补充）
    pace = _parse_pace_data(data.get("pace", []))

    # 解析分钟速度数据
    minute_speed = _parse_minute_speed_data(data.get("minute_speed", []))

    # 构造 SportRecord
    record = SportRecord(
        sport_id=file_sport_id,
        xh=record_data.get("uid", ""),
        name=record_data.get("name") or "",
        route_id=str(record_data.get("routeid", "")),
        route_name=record_data.get("routename", ""),
        sport_type=str(record_data.get("sporttype", "")),
        start_date=record_data.get("startdate", ""),
        end_date=record_data.get("enddate", ""),
        start_date_long=record_data.get("startdatelong", 0),
        end_date_long=record_data.get("enddatelong", 0),
        dl_time=record_data.get("dltime", ""),
        last_odometer_time=record_data.get("lastodometertime", ""),
        dllc=record_data.get("dllc", ""),
        total_step=record_data.get("totalstep", 0),
        avg_speed=str(record_data.get("avgspeed", "")),
        avg_pace=record_data.get("avgpace", ""),
        cal=float(record_data.get("cal", 0)),
        step_minute=str(record_data.get("stepminute", "")),
        max_speed_per_hour=str(record_data.get("maxspeedperhour", "")),
        min_speed_per_hour=str(record_data.get("minspeedperhour", "")),
        indoor=record_data.get("indoor", 0),
        route_polyline_bh=record_data.get("routepolylinebh", ""),
        already_pass_point=record_data.get("alreadypasspoint", ""),
        activity_status=int(record_data.get("activitystatus") or 0),
        status=int(record_data.get("status") or 0),
        result=int(record_data.get("result") or 0),
        gyro_upload_status=int(record_data.get("gyrouploadstatus") or 0),
        has_photo=int(record_data.get("hasphoto") or 0),
        points=points,
        pace=pace,
        minute_speed=minute_speed,
    )

    return record


def _parse_pace_data(raw_pace) -> list[PaceEntry]:
    """解析配速数据（cpacestr 表内容或 JSON 数组）"""
    result = []
    if not raw_pace:
        return result

    if isinstance(raw_pace, list):
        for item in raw_pace:
            if isinstance(item, dict):
                result.append(PaceEntry(
                    km=str(item.get("km", "")),
                    t=str(item.get("t", "")),
                    km_total_length=float(
                        item.get(
                            "kmTotallength",
                            item.get("kmtotallength", 0),
                        )
                        or 0
                    ),
                    km_dlsj=int(item.get("kmdlsj", 0) or 0),
                ))
    return result


def _parse_minute_speed_data(raw_ms) -> list[MinuteSpeedEntry]:
    """解析分钟速度数据（cminutespeedstr 表内容或 JSON 数组）"""
    result = []
    if not raw_ms:
        return result

    if isinstance(raw_ms, list):
        for item in raw_ms:
            if isinstance(item, dict):
                result.append(MinuteSpeedEntry(
                    min=str(item.get("min", "")),
                    v=str(item.get("v", "")),
                ))
    return result


def list_sport_records(json_path: str) -> list[str]:
    """
    列出 JSON 文件中可用的运动记录 sportid。
    当前格式每个文件只有一条记录。
    """
    path = Path(json_path)
    if not path.exists():
        return []

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    record = data.get("record", {})
    sport_id = record.get("sportid", "")
    if sport_id:
        return [sport_id]
    return []
