"""
时间戳平移器 — 将运动记录的时间戳平移到目标日期。

保持原始的小时/分钟/秒/毫秒不变，只修改日期部分。
同时更新 start_date, end_date 字符串格式。
"""

from datetime import datetime, timedelta
from typing import Optional

from .models import SportRecord


def shift_to_date(
    record: SportRecord,
    target_date: str = "today",
) -> SportRecord:
    """
    将运动记录的所有时间戳平移到目标日期。

    策略：
    - 计算原始日期与目标日期的天数差（毫秒偏移量）
    - 统一平移 start_date_long, end_date_long, 每个 TrackPoint.t
    - 重新格式化 start_date, end_date 字符串
    - 保持 dllc, avg_speed, avg_pace, cal, step_count, step_minute 不变
      （这些是运动本身的属性，不受日期影响）

    Args:
        record: 原始运动记录
        target_date: 目标日期 "original" | "today" | "YYYY-MM-DD"

    Returns:
        平移后的 SportRecord（浅拷贝，points 深拷贝）

    Raises:
        ValueError: 原始记录没有有效的时间戳
    """
    if not record.start_date_long or not record.end_date_long:
        raise ValueError("原始记录缺少有效的时间戳")

    # 原始日期（从 start_date_long 提取）
    orig_start_ms = record.start_date_long
    orig_start_dt = datetime.fromtimestamp(orig_start_ms / 1000.0)

    target_dt = (
        orig_start_dt
        if target_date == "original"
        else _parse_target_date(target_date)
    )

    # 计算偏移：目标日期的 00:00:00 - 原始日期的 00:00:00
    orig_midnight = orig_start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    target_midnight = target_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    offset_ms = int((target_midnight - orig_midnight).total_seconds() * 1000)

    # 平移时间戳
    new_start_ms = record.start_date_long + offset_ms
    new_end_ms = record.end_date_long + offset_ms

    # 平移所有轨迹点时间戳
    new_points = []
    for pt in record.points:
        new_pt = type(pt)(
            a=pt.a,
            o=pt.o,
            v=pt.v,
            s=pt.s,
            t=pt.t + offset_ms if pt.t > 0 else 0,
            ac=pt.ac,
            da=pt.da,
            d=pt.d,
            st=pt.st,
            sta=pt.sta,
            sportbean_id=pt.sportbean_id,
        )
        new_points.append(new_pt)

    # 格式化新的日期字符串
    new_start_str = datetime.fromtimestamp(
        new_start_ms / 1000.0
    ).strftime("%Y-%m-%d %H:%M:%S")
    new_end_str = datetime.fromtimestamp(
        new_end_ms / 1000.0
    ).strftime("%Y-%m-%d %H:%M:%S")

    # 构造新记录
    shifted = SportRecord(
        sport_id=record.sport_id,
        xh=record.xh,
        name=record.name,
        route_id=record.route_id,
        route_name=record.route_name,
        sport_type=record.sport_type,
        start_date=new_start_str,
        end_date=new_end_str,
        start_date_long=new_start_ms,
        end_date_long=new_end_ms,
        # activeTime 是计时器有效时长，可能与首尾墙钟差不同。
        # 日期平移不能用 end-start 覆盖它。
        dl_time=record.dl_time,
        last_odometer_time=record.last_odometer_time,
        dllc=record.dllc,
        total_step=record.total_step,
        avg_speed=record.avg_speed,
        avg_pace=record.avg_pace,
        cal=record.cal,
        step_minute=record.step_minute,
        max_speed_per_hour=record.max_speed_per_hour,
        min_speed_per_hour=record.min_speed_per_hour,
        indoor=record.indoor,
        route_polyline_bh=record.route_polyline_bh,
        already_pass_point=record.already_pass_point,
        activity_status=record.activity_status,
        status=record.status,
        result=record.result,
        gyro_upload_status=record.gyro_upload_status,
        has_photo=record.has_photo,
        points=new_points,
        pace=record.pace,
        minute_speed=record.minute_speed,
    )

    return shifted


def _parse_target_date(target_date: str) -> datetime:
    """解析目标日期字符串"""
    if target_date == "today":
        return datetime.now()
    try:
        return datetime.strptime(target_date, "%Y-%m-%d")
    except ValueError:
        raise ValueError(
            f"无效的日期格式: {target_date!r}，"
            f"请使用 'today' 或 'YYYY-MM-DD'"
        )


def compute_date_offset_ms(orig_date_long: int, target_date: str = "today") -> int:
    """
    计算时间偏移量（毫秒）— 不修改记录，只返回偏移值。

    用于检查平移是否合理。

    Args:
        orig_date_long: 原始毫秒时间戳
        target_date: 目标日期

    Returns:
        毫秒偏移量
    """
    orig_dt = datetime.fromtimestamp(orig_date_long / 1000.0)
    target_dt = orig_dt if target_date == "original" else _parse_target_date(target_date)
    orig_midnight = orig_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    target_midnight = target_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((target_midnight - orig_midnight).total_seconds() * 1000)


def validate_time_window(record: SportRecord) -> bool:
    """
    验证运动时间是否在允许时段内（06:00-23:00）。

    基于逆向文档：客户端时间校验按天比对，但服务端可能检查时段。
    """
    if not record.start_date_long or not record.end_date_long:
        return False

    start_dt = datetime.fromtimestamp(record.start_date_long / 1000.0)
    end_dt = datetime.fromtimestamp(record.end_date_long / 1000.0)

    # 检查是否在 06:00-23:00 之间
    start_hour = start_dt.hour + start_dt.minute / 60.0
    end_hour = end_dt.hour + end_dt.minute / 60.0

    return 6.0 <= start_hour <= 23.0 and 6.0 <= end_hour <= 23.0
