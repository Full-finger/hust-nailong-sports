"""由路径生成一条可直接提交的运动记录。

成功样本说明服务端判的是**轨迹是否像真人跑的**：
速度要在 4.0–5.0 m/s 之间缓慢起伏、定位精度要随机、配速/分钟速度必须能由
轨迹和时长推导出来。本模块按同一统计特征生成 1 Hz 轨迹：

- 沿给定折线前进，速度 = 基准 + 慢正弦 + 小噪声，钳在 4.0–5.0 m/s；
- 每点 ``s`` 是定位瞬时 km/h（与 ``d / Δt`` 不必相等，官方样本也是如此）；
- ``d`` 由相邻坐标直接算 haversine，``da`` 为 ``d`` 的累计；
- 时间戳从服务端下发的运动开始时刻起算，采样间隔 1 s 附近轻微抖动；
- 配速、分钟速度、平均速度、步数、步频全部由同一条轨迹派生，
  并通过 ``consistency`` 的内部一致性检查。
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .consistency import (
    assert_record_consistent,
    format_pace,
    haversine_distance_m,
)
from .models import MinuteSpeedEntry, PaceEntry, SportRecord, TrackPoint

_EARTH_RADIUS_M = 6_371_000.0
_M_PER_DEG_LAT = 111_320.0
# beginTime/endTime 是手机本地时间；服务在境内，按 UTC+8 写死可避免本机时区
# 不是中国时区时字符串和 startdatelong 对不上。
_CHINA_TZ = timezone(timedelta(hours=8))

DEFAULT_SPORT_TYPE = "3"
DEFAULT_ROUTE_ID = "7"
DEFAULT_ROUTE_NAME = "跑步-男生"


@dataclass(frozen=True)
class RouteGeometry:
    """闭合/开放折线及其累计长度。"""

    points: tuple[tuple[float, float], ...]
    cumulative: tuple[float, ...]
    length_m: float

    def point_at(self, distance_m: float) -> tuple[float, float, float]:
        """返回沿折线 ``distance_m`` 处的 ``(lat, lon, bearing_deg)``。"""
        if self.length_m <= 0 or len(self.points) < 2:
            lat, lon = self.points[0] if self.points else (0.0, 0.0)
            return lat, lon, 0.0
        distance_m %= self.length_m
        lo, hi = 0, len(self.cumulative) - 1
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if self.cumulative[mid] <= distance_m:
                lo = mid
            else:
                hi = mid
        start = self.points[lo]
        end = self.points[hi]
        segment = self.cumulative[hi] - self.cumulative[lo]
        fraction = 0.0 if segment <= 0 else (distance_m - self.cumulative[lo]) / segment
        lat = start[0] + (end[0] - start[0]) * fraction
        lon = start[1] + (end[1] - start[1]) * fraction
        return lat, lon, _bearing(start[0], start[1], end[0], end[1])


def load_route(path: str | Path) -> RouteGeometry:
    """读取 ``[[lat, lon], ...]`` 形式的路径文件。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValueError(f"路径文件格式无效: {path}")
    points = [(float(item[0]), float(item[1])) for item in raw]
    return build_geometry(points)


def _chaikin(points: Sequence[tuple[float, float]], iterations: int) -> list[tuple[float, float]]:
    """Chaikin 圆角：去掉描点留下的 1 m 级锯齿，让 1 Hz 采样不再切掉拐角。"""
    pts = list(points)
    closed = pts[0] == pts[-1]
    for _ in range(max(0, iterations)):
        smoothed: list[tuple[float, float]] = []
        if not closed:
            smoothed.append(pts[0])
        for start, end in zip(pts, pts[1:]):
            smoothed.append(
                (0.75 * start[0] + 0.25 * end[0], 0.75 * start[1] + 0.25 * end[1])
            )
            smoothed.append(
                (0.25 * start[0] + 0.75 * end[0], 0.25 * start[1] + 0.75 * end[1])
            )
        if closed:
            smoothed.append(smoothed[0])
        else:
            smoothed.append(pts[-1])
        pts = smoothed
    return pts


def build_geometry(
    points: Sequence[tuple[float, float]],
    smooth_iterations: int = 3,
) -> RouteGeometry:
    """构造几何；首尾距离小于 20 m 时自动按闭合路径处理。"""
    pts = list(points)
    if haversine_distance_m(pts[0][0], pts[0][1], pts[-1][0], pts[-1][1]) < 20.0:
        if pts[0] != pts[-1]:
            pts.append(pts[0])
    if smooth_iterations:
        pts = _chaikin(pts, smooth_iterations)
    cumulative = [0.0]
    total = 0.0
    for start, end in zip(pts, pts[1:]):
        total += haversine_distance_m(start[0], start[1], end[0], end[1])
        cumulative.append(total)
    if total <= 0:
        raise ValueError("路径总长度为 0")
    return RouteGeometry(tuple(pts), tuple(cumulative), total)


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _offset_meters(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    lat2 = lat + north_m / _M_PER_DEG_LAT
    lon2 = lon + east_m / (_M_PER_DEG_LAT * math.cos(math.radians(lat)))
    return lat2, lon2


def _round_coord(value: float) -> float:
    return round(value, 6)


def _format_number(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _format_duration(seconds: int) -> str:
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_datetime(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=_CHINA_TZ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _build_pace(points: Sequence[TrackPoint], start_ms: int) -> list[PaceEntry]:
    """按每公里切分，生成 ``cpacestr`` 等价条目。"""
    entries: list[PaceEntry] = []
    if not points:
        return entries
    previous_seconds = 0
    next_km = 1
    for point in points:
        if point.t <= 0 or point.da < next_km * 1000.0:
            continue
        elapsed = int(round((point.t - start_ms) / 1000.0))
        segment = max(1, elapsed - previous_seconds)
        entries.append(
            PaceEntry(
                km=str(next_km),
                t=format_pace(segment),
                km_total_length=float(next_km),
                km_dlsj=elapsed,
            )
        )
        previous_seconds = elapsed
        next_km += 1
    return entries


def _build_minute_speed(points: Sequence[TrackPoint], start_ms: int) -> list[MinuteSpeedEntry]:
    """按自然分钟聚合轨迹点的瞬时速度。"""
    buckets: dict[int, list[float]] = {}
    for point in points:
        if point.t <= 0 or point.s <= 0:
            continue
        minute = int((point.t - start_ms) // 60_000) + 1
        buckets.setdefault(minute, []).append(point.s)
    entries: list[MinuteSpeedEntry] = []
    for minute in sorted(buckets):
        values = buckets[minute]
        average = sum(values) / len(values)
        entries.append(MinuteSpeedEntry(min=str(minute), v=_format_number(average, 2)))
    return entries


def generate_route_record(
    route: RouteGeometry | Sequence[tuple[float, float]],
    *,
    start_ms: int,
    sport_id: str,
    xh: str,
    name: str = "",
    target_distance_m: float = 2_100.0,
    seed: Optional[int] = None,
    sport_type: str = DEFAULT_SPORT_TYPE,
    route_id: str = DEFAULT_ROUTE_ID,
    route_name: str = DEFAULT_ROUTE_NAME,
    min_speed_mps: float = 2.6,
    max_speed_mps: float = 3.4,
    min_accuracy_m: float = 3.7,
    max_accuracy_m: float = 100.0,
    first_fix_delay_s: float = 2.0,
    sample_interval_s: float = 2.0,
) -> SportRecord:
    """沿路径生成一条内部一致的跑步记录。

    ``start_ms`` 必须是服务端时间；记录会从该时刻起按真实时间前进，调用方
    应等到 ``end_date_long`` 之后再做实际上传。
    """
    if not isinstance(route, RouteGeometry):
        route = build_geometry(route)
    if target_distance_m < 100.0:
        raise ValueError("目标距离过短")
    if not (1.0 <= min_speed_mps < max_speed_mps <= 12.0):
        raise ValueError("速度范围无效")
    if not (0.0 <= min_accuracy_m < max_accuracy_m <= 120.0):
        raise ValueError("精度范围无效")

    rng = random.Random(seed)
    # 巡航速度与真实样本对齐：普通学生 9.4~12.2 km/h（5'~6'25''）。
    # 之前默认 4.0~5.0 m/s 是竞技配速，卡片上一眼假。
    base_speed = rng.uniform(min_speed_mps + 0.1, max_speed_mps - 0.1)
    speed_amplitude = rng.uniform(0.18, 0.35)
    speed_period = rng.uniform(55.0, 95.0)
    speed_phase = rng.uniform(0.0, 2.0 * math.pi)
    drift_period = rng.uniform(30.0, 70.0)
    drift_phase = rng.uniform(0.0, 2.0 * math.pi)
    # 逐次生成的路线形变：模拟每次跑不同车道/漂移模式。以弧长为自变量，
    # 同一跑道位置本次生成内偏移一致（环仍是环），不同生成之间形状不同。
    route_morph_amp = rng.uniform(1.2, 2.6)
    route_morph_period = rng.uniform(120.0, 320.0)
    route_morph_phase = rng.uniform(0.0, 2.0 * math.pi)
    # 起步热身与最后减速的长度随机化
    ramp_up_s = rng.uniform(35.0, 60.0)
    cool_down_m = rng.uniform(70.0, 110.0)
    start_speed = rng.uniform(1.3, 1.9)
    # GPS 误差 AR(1) 相关噪声：误差缓慢游走而非逐点独立乱跳（真人毛刺中位约 0.6m）
    gps_lat_noise = 0.0
    gps_lon_noise = 0.0
    # 起点相位随机：每次沿路线的不同位置起跑
    start_offset_m = rng.uniform(0.0, route.length_m)

    points: list[TrackPoint] = []
    # 官方样本的第一个点是一个占位点：t=0、速度/精度/里程全为 0。
    # 占位点和实际起点用同一相位，否则两点会被一条横跨全场的直线连起来。
    start_lat, start_lon, _ = route.point_at(start_offset_m)
    points.append(
        TrackPoint(
            a=_round_coord(start_lat),
            o=_round_coord(start_lon),
            v=1,
            s=0.0,
            t=0,
            ac=0.0,
            da=0.0,
            d=0.0,
        )
    )

    elapsed_s = first_fix_delay_s
    travelled = 0.0
    cumulative = 0.0
    previous_lat: Optional[float] = None
    previous_lon: Optional[float] = None
    previous_ms = start_ms

    while cumulative < target_distance_m:
        remaining = target_distance_m - cumulative
        cruise = (
            base_speed
            + speed_amplitude * math.sin(2.0 * math.pi * elapsed_s / speed_period + speed_phase)
            + rng.gauss(0.0, 0.05)
        )
        if elapsed_s - first_fix_delay_s < ramp_up_s:
            # 起步：慢走热身后线性升到巡航（真实样本起步 3~4 km/h）
            progress = (elapsed_s - first_fix_delay_s) / ramp_up_s
            speed = start_speed + (cruise - start_speed) * progress
            speed = min(speed, max_speed_mps)
        elif remaining < cool_down_m:
            # 最后一段逐渐减速停下
            speed = max(0.9, cruise * max(0.35, remaining / cool_down_m))
        else:
            speed = min(max(cruise, min_speed_mps), max_speed_mps)
        interval = sample_interval_s + rng.uniform(-0.04, 0.04)
        step = speed * interval
        travelled += step

        lat, lon, bearing = route.point_at(travelled + start_offset_m)
        # 横向缓慢漂移：真人不会严格贴着一条数学折线跑。时间驱动的慢摆
        # 之上，再叠加以弧长为自变量的逐次路线形变。
        lateral = 0.7 * math.sin(2.0 * math.pi * elapsed_s / drift_period + drift_phase)
        lateral += route_morph_amp * math.sin(
            2.0 * math.pi * travelled / route_morph_period + route_morph_phase
        )
        radial = math.radians(bearing)
        north = -math.sin(radial) * lateral
        east = math.cos(radial) * lateral
        lat, lon = _offset_meters(lat, lon, north, east)
        # GPS 散布：AR(1) 相关噪声，稳态约 0.7m，相邻点强相关，
        # 带长程缓慢游走（多径/星历误差的真实行为）
        gps_lat_noise = 0.9 * gps_lat_noise + rng.gauss(0.0, 0.3)
        gps_lon_noise = 0.9 * gps_lon_noise + rng.gauss(0.0, 0.3)
        lat += gps_lat_noise / _M_PER_DEG_LAT
        lon += gps_lon_noise / (_M_PER_DEG_LAT * math.cos(math.radians(lat)))
        lat, lon = _round_coord(lat), _round_coord(lon)

        point_ms = start_ms + int(round(elapsed_s * 1000.0))
        if previous_lat is None:
            segment = 0.0
            reported = speed
        else:
            segment = haversine_distance_m(previous_lat, previous_lon, lat, lon)
            # 定位速度以真实坐标位移为准。GPS 瞬时速度本身噪声很大
            # （真样本点速 sd 约 3 km/h），叠加 12% 高斯噪声并截尾。
            measured = segment / max(0.2, (point_ms - previous_ms) / 1000.0)
            reported = max(0.3, measured * rng.gauss(1.0, 0.12))
        cumulative += segment
        # 定位精度呈长尾分布（真样本均值约 4m，偶尔漂到 50~100m）
        accuracy = round(
            min(max_accuracy_m, max(min_accuracy_m, math.exp(rng.gauss(1.44, 0.5)))),
            1,
        )
        points.append(
            TrackPoint(
                a=lat,
                o=lon,
                v=1,
                s=round(reported * 3.6, 2),
                t=point_ms,
                ac=accuracy,
                da=round(cumulative, 6),
                d=round(segment, 6),
            )
        )
        previous_lat, previous_lon = lat, lon
        previous_ms = point_ms
        elapsed_s += interval

    # 结束时间向上取整到秒，和官方样本一致（enddatelong 是整秒）。
    end_ms = int(math.ceil(previous_ms / 1000.0) * 1000)
    active_seconds = max(1, int(round((end_ms - start_ms) / 1000.0)))
    distance_km = cumulative / 1000.0

    average_speed = distance_km / (active_seconds / 3600.0)
    average_pace_seconds = active_seconds / distance_km
    cadence = rng.uniform(155.0, 172.0)
    total_step = max(1, int(round(cadence * active_seconds / 60.0)))
    step_minute = total_step / (active_seconds / 60.0)

    # 与改包 App（v51）的 UploadJsonSports.build() 保持一致：
    #   maxSpeedPerHour 最终被覆盖为 avgSpeed；
    #   minSpeedPerHour 经 clampMin：原值 >= 6.0 时统一写成 "6.0"。
    # 这是 2026-10-24 唯一被判 isValid=1 的 v50 payload 的实际形状。
    point_speeds = [point.s for point in points if point.s > 0]
    maximum_speed = average_speed
    minimum_speed = min(point_speeds, default=average_speed)
    if minimum_speed >= 6.0:
        # clampMin() 的产物是字符串 "6.0"，保留一位小数以免和官方 payload 不同。
        minimum_speed_text = "6.0"
    else:
        minimum_speed_text = _format_number(minimum_speed, 2)

    first_movement = next(
        ((point.t - start_ms) // 1000 for point in points if point.t > 0 and point.d > 0),
        active_seconds,
    )

    record = SportRecord(
        sport_id=sport_id,
        xh=xh,
        name=name,
        route_id=route_id,
        route_name=route_name,
        sport_type=sport_type,
        start_date=_format_datetime(start_ms),
        end_date=_format_datetime(end_ms),
        start_date_long=start_ms,
        end_date_long=end_ms,
        dl_time=_format_duration(active_seconds),
        last_odometer_time=_format_duration(int(first_movement)),
        dllc=_format_number(distance_km, 2),
        total_step=total_step,
        avg_speed=_format_number(average_speed, 2),
        avg_pace=format_pace(average_pace_seconds),
        cal=round(distance_km * rng.uniform(53.0, 60.0), 1),
        step_minute=_format_number(step_minute, 1),
        max_speed_per_hour=_format_number(maximum_speed, 2),
        min_speed_per_hour=minimum_speed_text,
        indoor=0,
        activity_status=1,
        status=0,
        result=1,
        gyro_upload_status=0,
        has_photo=0,
        points=points,
        pace=_build_pace(points, start_ms),
        minute_speed=_build_minute_speed(points, start_ms),
    )
    assert_record_consistent(record)
    return record


SENSOR_SAMPLE_HZ = 20


def make_sensor_trace(duration_seconds: int, seed: Optional[int] = None) -> str:
    """生成与记录时长匹配的加速度幅值序列（20 Hz）。

    真机样本约每个 1 Hz 轨迹点对应 18.4 个采样值（≈20 Hz），1.2.1 客户端
    也固定按 ``20 * activeTime`` 生成，这里保持一致。
    """
    rng = random.Random(seed)
    samples: list[float] = []
    total = max(1, int(duration_seconds * SENSOR_SAMPLE_HZ))
    phase = rng.uniform(0.0, 2.0 * math.pi)
    for index in range(total):
        t = index / SENSOR_SAMPLE_HZ
        cadence_hz = rng.uniform(2.6, 2.9)
        stride = 5.2 * math.sin(2.0 * math.pi * cadence_hz * t + phase)
        sway = 0.8 * math.sin(2.0 * math.pi * 0.35 * t)
        noise = rng.gauss(0.0, 0.45)
        samples.append(round(9.8 + stride + sway + noise, 6))
    return json.dumps(samples, separators=(",", ":"))


def route_summary(record: SportRecord) -> str:
    return (
        f"{record.start_date} → {record.end_date} | "
        f"{record.dllc} km | {record.dl_time} | "
        f"{record.avg_speed} km/h | {len(record.points)} 点"
    )


def iter_route_points(points: Iterable[TrackPoint]) -> Iterable[tuple[float, float, float]]:
    for point in points:
        yield point.a, point.o, point.s
