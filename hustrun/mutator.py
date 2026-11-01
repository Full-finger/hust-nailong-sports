"""记录变异器。

所有可相互验证的字段都从同一条变异后的轨迹和时间线派生：

- 目标日期由显式参考时刻（上传时为服务器时间）决定；
- 时间安排使用服务器参考时刻裁剪已结束窗口，并受 seed 控制；
- 对轨迹做小幅平滑形变，而不是重放原坐标折线；
- 分别重排真实采样间隔、过滤距离和瞬时速度序列，并重建 ``da``；
- 从新轨迹重建分公里配速、分钟速度和速度边界；
- 返回前执行本地一致性检查。

本模块不模拟或猜测服务端风控规则。
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from typing import Optional

from .consistency import (
    ConsistencyIssue,
    RecordConsistencyError,
    assert_record_consistent,
    coordinate_distance_m,
    format_pace,
    haversine_distance_m,
    parse_duration_seconds,
)
from .models import MinuteSpeedEntry, PaceEntry, SportRecord, TrackPoint
from .transformer import shift_to_date


_EARTH_RADIUS_M = 6_371_000.0


def _format_decimal(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _format_duration(seconds: int) -> str:
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _generate_sport_id(rng: random.Random) -> str:
    """生成受局部随机源控制的 UUID v4 兼容 sport_id。"""
    random_bytes = bytearray(rng.getrandbits(8) for _ in range(16))
    random_bytes[6] = (random_bytes[6] & 0x0F) | 0x40
    random_bytes[8] = (random_bytes[8] & 0x3F) | 0x80
    hex_str = bytes(random_bytes).hex()
    return (
        f"{hex_str[:8]}-{hex_str[8:12]}-{hex_str[12:16]}-"
        f"{hex_str[16:20]}-{hex_str[20:]}"
    )


def _copy_with_time_shift(record: SportRecord, shift_ms: int) -> SportRecord:
    """对记录的所有绝对时间戳统一加上 ``shift_ms``。"""
    if shift_ms == 0:
        return record

    new_start_ms = record.start_date_long + shift_ms
    new_end_ms = record.end_date_long + shift_ms
    new_points = [
        TrackPoint(
            a=point.a,
            o=point.o,
            v=point.v,
            s=point.s,
            t=point.t + shift_ms if point.t > 0 else 0,
            ac=point.ac,
            da=point.da,
            d=point.d,
            st=point.st,
            sta=point.sta,
            sportbean_id=point.sportbean_id,
        )
        for point in record.points
    ]

    return SportRecord(
        sport_id=record.sport_id,
        xh=record.xh,
        name=record.name,
        route_id=record.route_id,
        route_name=record.route_name,
        sport_type=record.sport_type,
        start_date=datetime.fromtimestamp(new_start_ms / 1000.0).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        end_date=datetime.fromtimestamp(new_end_ms / 1000.0).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        start_date_long=new_start_ms,
        end_date_long=new_end_ms,
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
        pace=list(record.pace),
        minute_speed=list(record.minute_speed),
    )


def _reference_datetime(reference_time_ms: int | None) -> datetime:
    if reference_time_ms is None:
        return datetime.now()
    if reference_time_ms <= 0:
        raise ValueError("reference_time_ms 必须是正的 epoch 毫秒时间戳")
    return datetime.fromtimestamp(reference_time_ms / 1000.0)


def _adjust_time_to_safe_window(
    record: SportRecord,
    target_date: str,
    rng: random.Random,
    reference_time_ms: int | None,
    window_start_hour: int = 7,
    window_end_hour: int = 22,
    buffer_minutes: int = 5,
) -> SportRecord:
    """在当天已结束的有效窗口内随机安排记录时间。

    可选起点范围是 ``07:00`` 到
    ``min(22:00, 参考时刻 - buffer - 运动时长)``。上传流程传入服务器时间，
    dry-run 才使用本机时间。相同 seed 和相同参考时间快照会得到相同结果。
    """
    if target_date == "original":
        return record

    duration_ms = record.end_date_long - record.start_date_long
    if duration_ms <= 0:
        raise ValueError("运动结束时间必须晚于开始时间")

    target_dt = datetime.fromtimestamp(record.start_date_long / 1000.0)
    midnight = target_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = midnight.replace(hour=window_start_hour)
    window_end = midnight.replace(hour=window_end_hour)
    earliest_start_ms = int(window_start.timestamp() * 1000)
    reference_dt = _reference_datetime(reference_time_ms)
    cutoff = reference_dt - timedelta(minutes=buffer_minutes)
    effective_end_ms = min(
        int(window_end.timestamp() * 1000),
        int(cutoff.timestamp() * 1000),
    )
    latest_start_ms = effective_end_ms - duration_ms
    if latest_start_ms < earliest_start_ms:
        raise ValueError(
            f"截至{reference_dt.strftime('%H:%M:%S')}尚无足够的已结束时间窗口；"
            f"运动时长为 {duration_ms / 60_000.0:.0f} 分钟，"
            f"最早只能从 {window_start_hour:02d}:00 开始"
        )

    if target_date == "today":
        # 用服务器参考时刻裁剪完整的 07:00–22:00 窗口，再在所有已经结束的
        # 合法起点中随机选择。不会保留源记录下午的时分秒。
        range_ms = latest_start_ms - earliest_start_ms
        offset_ms = rng.randint(0, range_ms) if range_ms > 0 else 0
        candidate = _copy_with_time_shift(
            record,
            earliest_start_ms + offset_ms - record.start_date_long,
        )
    elif (
        record.start_date_long >= earliest_start_ms
        and record.end_date_long <= int(window_end.timestamp() * 1000)
    ):
        candidate = record
    else:
        range_ms = latest_start_ms - earliest_start_ms
        offset_ms = rng.randint(0, range_ms) if range_ms > 0 else 0
        candidate = _copy_with_time_shift(
            record,
            earliest_start_ms + offset_ms - record.start_date_long,
        )

    if candidate.end_date_long > int(cutoff.timestamp() * 1000):
        source = "本机" if reference_time_ms is None else "服务器"
        raise ValueError(
            f"确定性候选结束时间 {candidate.end_date} 晚于{source}参考时刻"
            f"的安全截止值 {cutoff.strftime('%Y-%m-%d %H:%M:%S')}；"
            "请稍后重试或指定已结束的目标日期"
        )
    return candidate


def _validate_source_geometry(record: SportRecord) -> None:
    """拒绝坐标与源里程严重矛盾的输入，避免把坏点缩放成新轨迹。"""
    if len(record.points) < 2:
        raise ValueError("轨迹点至少需要 2 个")
    expected_m = record.distance_km * 1000.0
    geometry_m = coordinate_distance_m(record)
    if expected_m <= 0 or geometry_m <= 0:
        raise ValueError("源记录缺少有效里程或坐标折线")
    if abs(geometry_m - expected_m) > max(100.0, expected_m * 0.20):
        raise RecordConsistencyError(
            [
                ConsistencyIssue(
                    "track.geometry",
                    f"源坐标折线 {geometry_m / 1000.0:.3f} km 与里程 "
                    f"{expected_m / 1000.0:.3f} km 差异过大",
                )
            ]
        )


def _to_local_xy(
    latitude: float,
    longitude: float,
    origin_latitude: float,
    origin_longitude: float,
) -> tuple[float, float]:
    lat0 = math.radians(origin_latitude)
    x = (
        math.radians(longitude - origin_longitude)
        * _EARTH_RADIUS_M
        * math.cos(lat0)
    )
    y = math.radians(latitude - origin_latitude) * _EARTH_RADIUS_M
    return x, y


def _from_local_xy(
    x: float,
    y: float,
    origin_latitude: float,
    origin_longitude: float,
) -> tuple[float, float]:
    lat0 = math.radians(origin_latitude)
    latitude = origin_latitude + math.degrees(y / _EARTH_RADIUS_M)
    longitude = origin_longitude + math.degrees(
        x / (_EARTH_RADIUS_M * math.cos(lat0))
    )
    return latitude, longitude


def _coordinates_from_xy(
    xy: list[tuple[float, float]],
    origin_latitude: float,
    origin_longitude: float,
) -> list[tuple[float, float]]:
    return [
        tuple(
            round(value, 6)
            for value in _from_local_xy(
                x,
                y,
                origin_latitude,
                origin_longitude,
            )
        )
        for x, y in xy
    ]


def _coordinate_length(coordinates: list[tuple[float, float]]) -> float:
    return sum(
        haversine_distance_m(
            previous[0],
            previous[1],
            current[0],
            current[1],
        )
        for previous, current in zip(coordinates, coordinates[1:])
    )


def _deform_coordinates(
    points: list[TrackPoint],
    target_distance_m: float,
    rng: random.Random,
    deformation_m: float,
    gps_jitter_scale: float,
) -> list[tuple[float, float]]:
    """在局部米制平面做平滑形变，并归一化回目标总里程。"""
    origin_latitude = points[0].a
    origin_longitude = points[0].o
    source_xy = [
        _to_local_xy(
            point.a,
            point.o,
            origin_latitude,
            origin_longitude,
        )
        for point in points
    ]
    angle = rng.uniform(-math.radians(4.0), math.radians(4.0))
    cosine = math.cos(angle)
    sine = math.sin(angle)
    phase = rng.uniform(0.0, 2.0 * math.pi)
    amplitude = rng.uniform(0.55, 1.0) * deformation_m
    translation_x = rng.uniform(-deformation_m, deformation_m)
    translation_y = rng.uniform(-deformation_m, deformation_m)

    transformed: list[tuple[float, float]] = []
    denominator = max(1, len(source_xy) - 1)
    for index, (x, y) in enumerate(source_xy):
        progress = index / denominator
        rotated_x = x * cosine - y * sine
        rotated_y = x * sine + y * cosine
        envelope = math.sin(math.pi * progress)
        lateral = (
            amplitude
            * envelope
            * (
                0.70 * math.sin(2.0 * math.pi * progress + phase)
                + 0.30 * math.sin(4.0 * math.pi * progress + phase / 2.0)
            )
        )
        transformed.append(
            (
                rotated_x + translation_x,
                rotated_y + translation_y + lateral,
            )
        )

    # 以首点为中心迭代缩放，使 haversine 折线长度与 odometer 一致。
    anchor_x, anchor_y = transformed[0]
    scale = 1.0
    scaled = transformed
    for _ in range(4):
        scaled = [
            (
                anchor_x + (x - anchor_x) * scale,
                anchor_y + (y - anchor_y) * scale,
            )
            for x, y in transformed
        ]
        coordinates = _coordinates_from_xy(
            scaled,
            origin_latitude,
            origin_longitude,
        )
        length_m = _coordinate_length(coordinates)
        if length_m <= 0:
            raise ValueError("轨迹形变后折线长度为 0")
        scale *= target_distance_m / length_m

    coordinates = _coordinates_from_xy(
        scaled,
        origin_latitude,
        origin_longitude,
    )
    if gps_jitter_scale:
        latitude_offset = rng.uniform(-gps_jitter_scale, gps_jitter_scale)
        longitude_offset = rng.uniform(-gps_jitter_scale, gps_jitter_scale)
        coordinates = [
            (
                round(latitude + latitude_offset, 6),
                round(longitude + longitude_offset, 6),
            )
            for latitude, longitude in coordinates
        ]
    return coordinates


def _rigid_coordinates(
    points: list[TrackPoint],
    rng: random.Random,
    gps_jitter_scale: float,
) -> list[tuple[float, float]]:
    latitude_offset = (
        rng.uniform(-gps_jitter_scale, gps_jitter_scale)
        if gps_jitter_scale
        else 0.0
    )
    longitude_offset = (
        rng.uniform(-gps_jitter_scale, gps_jitter_scale)
        if gps_jitter_scale
        else 0.0
    )
    return [
        (
            round(point.a + latitude_offset, 6),
            round(point.o + longitude_offset, 6),
        )
        for point in points
    ]


def _rebuild_timestamps(
    coordinates: list[tuple[float, float]],
    start_ms: int,
    end_ms: int,
    rng: random.Random,
    jitter_pct: float,
    source_intervals_ms: list[int] | None = None,
) -> list[int]:
    """打乱真实采样间隔并施加小幅抖动，避免重放原相对时间序列。"""
    if len(coordinates) < 2:
        return [start_ms] if coordinates else []
    duration_ms = end_ms - start_ms
    if duration_ms < len(coordinates) - 1:
        raise ValueError("运动总时长不足以容纳严格递增的轨迹点")

    if (
        source_intervals_ms
        and len(source_intervals_ms) == len(coordinates) - 1
        and all(interval > 0 for interval in source_intervals_ms)
    ):
        interval_weights = [float(value) for value in source_intervals_ms]
        rng.shuffle(interval_weights)
    else:
        interval_weights = [1.0] * (len(coordinates) - 1)
    weights = [
        interval * (1.0 + rng.uniform(-jitter_pct, jitter_pct))
        for interval in interval_weights
    ]
    total_weight = sum(weights)
    timestamps = [start_ms]
    cumulative_weight = 0.0
    for index, weight in enumerate(weights[:-1], start=1):
        cumulative_weight += weight
        candidate = start_ms + round(duration_ms * cumulative_weight / total_weight)
        minimum = timestamps[-1] + 1
        maximum = end_ms - (len(coordinates) - 1 - index)
        timestamps.append(min(max(candidate, minimum), maximum))
    timestamps.append(end_ms)
    return timestamps


def _rebuild_points(
    source_points: list[TrackPoint],
    coordinates: list[tuple[float, float]],
    timestamps: list[int],
    target_track_distance_m: float,
    rng: random.Random,
) -> list[TrackPoint]:
    # 定位折线长度与 App 的过滤里程不是同一量，成功样本中也存在明显 GPS
    # 跳点。打乱源记录的正分段距离以保留真实分布但不重放原序列，再归一化到
    # 源轨迹最终 da；零距离点模式继续保留。
    positive_distances = [
        float(point.d) for point in source_points[1:] if point.d > 0
    ]
    rng.shuffle(positive_distances)
    distance_iterator = iter(positive_distances)
    filtered_distances = [0.0]
    for source in source_points[1:]:
        filtered_distances.append(
            next(distance_iterator) if source.d > 0 else 0.0
        )
    filtered_total = sum(filtered_distances)
    if filtered_total <= 0 or target_track_distance_m <= 0:
        raise ValueError("无法从轨迹生成有效的过滤里程")
    distance_scale = target_track_distance_m / filtered_total
    segment_distances = [
        distance * distance_scale for distance in filtered_distances
    ]

    # s 是定位回调给出的瞬时 km/h，不是过滤里程 d 除以采样间隔。成功样本
    # 明确存在二者不相等的情况，因此只打乱真实速度序列，不伪造逐点等式。
    timed_speeds = [
        max(0.0, float(point.s))
        for point in source_points
        if point.t > 0
    ]
    rng.shuffle(timed_speeds)
    speed_iterator = iter(timed_speeds)
    speeds = [
        (
            next(speed_iterator)
            if timestamp > 0
            else max(0.0, float(source.s))
        )
        for source, timestamp in zip(source_points, timestamps)
    ]

    rebuilt: list[TrackPoint] = []
    cumulative = 0.0
    for index, source in enumerate(source_points):
        cumulative += segment_distances[index]
        rebuilt.append(
            TrackPoint(
                a=round(coordinates[index][0], 7),
                o=round(coordinates[index][1], 7),
                v=source.v,
                # SportLatLngBean.s 与 minuteSpeed.v 一样使用 km/h。
                s=round(speeds[index], 2),
                t=timestamps[index],
                ac=source.ac,
                da=round(cumulative, 3),
                d=round(segment_distances[index], 3),
                st=source.st,
                sta=source.sta,
                sportbean_id="",
            )
        )
    return rebuilt


def _timed_points(points: list[TrackPoint]) -> list[TrackPoint]:
    return [point for point in points if point.t > 0]


def _elapsed_at_distance(
    points: list[TrackPoint],
    distance_m: float,
    record_start_ms: int,
) -> float:
    timed = _timed_points(points)
    for previous, current in zip(timed, timed[1:]):
        if current.da + 1e-6 < distance_m:
            continue
        segment_m = current.da - previous.da
        fraction = 0.0 if segment_m <= 0 else (
            (distance_m - previous.da) / segment_m
        )
        crossing_ms = previous.t + (current.t - previous.t) * fraction
        return (crossing_ms - record_start_ms) / 1000.0
    return (timed[-1].t - record_start_ms) / 1000.0


def _rebuild_pace(
    points: list[TrackPoint],
    total_distance_m: float,
    record_start_ms: int,
) -> list[PaceEntry]:
    boundaries = [
        float(value)
        for value in range(
            1000,
            int(total_distance_m // 1000) * 1000 + 1,
            1000,
        )
    ]

    rebuilt: list[PaceEntry] = []
    previous_cumulative = 0
    for index, boundary in enumerate(boundaries, start=1):
        cumulative = max(
            previous_cumulative + 1,
            int(
                round(
                    _elapsed_at_distance(
                        points,
                        boundary,
                        record_start_ms,
                    )
                )
            ),
        )
        rebuilt.append(
            PaceEntry(
                km=str(index),
                t=format_pace(cumulative - previous_cumulative),
                km_total_length=round(boundary / 1000.0, 3),
                km_dlsj=cumulative,
            )
        )
        previous_cumulative = cumulative
    return rebuilt


def _rebuild_minute_speed(
    points: list[TrackPoint],
    duration_seconds: int,
    record_start_ms: int,
) -> list[MinuteSpeedEntry]:
    timed = _timed_points(points)
    positive_speed_points = [point for point in timed if point.s > 0]
    rebuilt: list[MinuteSpeedEntry] = []
    for index in range(1, duration_seconds // 60 + 1):
        target_ms = record_start_ms + index * 60_000
        nearest = min(
            positive_speed_points or timed,
            key=lambda point: abs(point.t - target_ms),
        )
        rebuilt.append(
            MinuteSpeedEntry(
                min=str(index),
                # 官方实现是在每个整分钟 tick 记录当时速度，不是该分钟的
                # 距离平均值。
                v=_format_decimal(max(0.0, nearest.s)),
            )
        )
    return rebuilt


def mutate_record(
    record: SportRecord,
    target_date: str = "today",
    gps_jitter_scale: float = 0.0,
    speed_jitter_pct: float = 0.0,
    step_jitter_pct: float = 0.0,
    seed: Optional[int] = None,
    *,
    reference_time_ms: int | None = None,
    deform_track: bool = True,
    track_deformation_m: float = 12.0,
    time_jitter_pct: float = 0.12,
) -> SportRecord:
    """生成内部一致且不直接重放源轨迹的新记录。

    ``reference_time_ms`` 在上传流程中必须使用服务器的 epoch 毫秒时间；未传时
    仅供离线 dry-run 使用本机时刻。相同 seed、目标日期和同一参考时间快照会
    产生相同结果；参考时刻变化时，已结束的可选窗口也会随之变化。

    ``speed_jitter_pct`` 仅为兼容旧调用保留。成功样本表明 ``s`` 是定位瞬时
    km/h，并不等于过滤距离 ``d / Δt``，因此二者分别变异。
    """
    if gps_jitter_scale < 0:
        raise ValueError("gps_jitter_scale 不能为负数")
    if speed_jitter_pct < 0 or step_jitter_pct < 0:
        raise ValueError("扰动比例不能为负数")
    if track_deformation_m < 0 or not 0 <= time_jitter_pct < 1:
        raise ValueError("轨迹形变幅度或时间扰动比例无效")

    _validate_source_geometry(record)
    rng = random.Random(seed)
    source_wall_duration_ms = record.end_date_long - record.start_date_long
    source_timed_points = [point for point in record.points if point.t > 0]
    source_intervals_ms = [
        current.t - previous.t
        for previous, current in zip(
            source_timed_points,
            source_timed_points[1:],
        )
    ]
    first_timed_offset_ms = (
        source_timed_points[0].t - record.start_date_long
        if source_timed_points
        else 0
    )
    last_timed_gap_ms = (
        record.end_date_long - source_timed_points[-1].t
        if source_timed_points
        else 0
    )
    if not 0 <= first_timed_offset_ms < source_wall_duration_ms:
        first_timed_offset_ms = 0
    if not 0 <= last_timed_gap_ms < source_wall_duration_ms:
        last_timed_gap_ms = 0
    reference_dt = _reference_datetime(reference_time_ms)
    resolved_target_date = (
        reference_dt.strftime("%Y-%m-%d")
        if target_date == "today"
        else target_date
    )
    shifted = shift_to_date(record, resolved_target_date)
    shifted = _adjust_time_to_safe_window(
        shifted,
        target_date,
        rng,
        reference_time_ms,
    )

    odometer_distance_km = shifted.distance_km
    target_track_distance_m = (
        shifted.points[-1].da
        if shifted.points and shifted.points[-1].da > 0
        else odometer_distance_km * 1000.0
    )
    target_geometry_m = coordinate_distance_m(shifted)
    if deform_track and track_deformation_m > 0:
        coordinates = _deform_coordinates(
            shifted.points,
            target_geometry_m,
            rng,
            track_deformation_m,
            gps_jitter_scale,
        )
        first_timed_index = next(
            (
                index
                for index, point in enumerate(shifted.points)
                if point.t > 0
            ),
            0,
        )
        timed_start_ms = shifted.start_date_long + first_timed_offset_ms
        timed_end_ms = shifted.end_date_long - last_timed_gap_ms
        if timed_start_ms <= 0 or timed_end_ms <= timed_start_ms:
            first_timed_index = 0
            timed_start_ms = shifted.start_date_long
            timed_end_ms = shifted.end_date_long
        timestamps = [0] * first_timed_index + _rebuild_timestamps(
            coordinates[first_timed_index:],
            timed_start_ms,
            timed_end_ms,
            rng,
            time_jitter_pct,
            source_intervals_ms=source_intervals_ms,
        )
    else:
        coordinates = _rigid_coordinates(
            shifted.points,
            rng,
            gps_jitter_scale,
        )
        timestamps = [point.t for point in shifted.points]

    points = _rebuild_points(
        shifted.points,
        coordinates,
        timestamps,
        target_track_distance_m,
        rng,
    )
    active_seconds = parse_duration_seconds(shifted.dl_time)
    if not active_seconds or active_seconds <= 0:
        active_seconds = shifted.duration_seconds
    if active_seconds <= 0 or odometer_distance_km <= 0:
        raise ValueError("无法从非正数里程或时长生成一致记录")

    # seed 用于复现实验数据，但实际的 today 重试不能复用已经被服务端记录过的
    # sportId。把服务器时间快照纳入 UUID 随机源；同一快照仍完全确定，不同
    # 请求时刻则会得到新的 UUID。
    sport_id_rng = rng
    if target_date == "today" and reference_time_ms is not None:
        sport_id_rng = random.Random(f"{seed}:{reference_time_ms}")

    total_step = shifted.total_step
    if step_jitter_pct:
        total_step = max(
            0,
            int(
                round(
                    shifted.total_step
                    * (1.0 + rng.uniform(-step_jitter_pct, step_jitter_pct))
                )
            ),
        )

    # 官方服务使用内部累计距离计算统计值；成功样本也满足
    # avgSpeed == coordinate[-1].da / activeTime，而不是使用显示里程 dllc。
    # 例如 3091.461 m / 754 s = 14.76 km/h，dllc=3.1 仅是显示/上传里程。
    statistics_distance_km = target_track_distance_m / 1000.0
    if statistics_distance_km <= 0:
        statistics_distance_km = odometer_distance_km
    average_speed = statistics_distance_km / (active_seconds / 3600.0)
    average_pace = active_seconds / statistics_distance_km
    cadence = total_step / (active_seconds / 60.0)
    try:
        old_average_speed = float(shifted.avg_speed)
    except (TypeError, ValueError):
        old_average_speed = average_speed
    speed_ratio = (
        average_speed / old_average_speed
        if old_average_speed > 0
        else 1.0
    )
    try:
        maximum_speed = float(shifted.max_speed_per_hour) * speed_ratio
    except (TypeError, ValueError):
        maximum_speed = max(
            (point.s for point in points if point.s > 0),
            default=average_speed,
        )
    try:
        minimum_speed = float(shifted.min_speed_per_hour) * speed_ratio
    except (TypeError, ValueError):
        minimum_speed = min(
            (point.s for point in points if point.s > 0),
            default=average_speed,
        )

    mutated = SportRecord(
        sport_id=_generate_sport_id(sport_id_rng),
        xh=shifted.xh,
        name=shifted.name,
        route_id=shifted.route_id,
        route_name=shifted.route_name,
        sport_type=shifted.sport_type,
        start_date=shifted.start_date,
        end_date=shifted.end_date,
        start_date_long=shifted.start_date_long,
        end_date_long=shifted.end_date_long,
        dl_time=_format_duration(active_seconds),
        # 该字段的业务语义并非总时长，不能在没有证据时强行派生。
        last_odometer_time=shifted.last_odometer_time,
        dllc=shifted.dllc or _format_decimal(odometer_distance_km, 3),
        total_step=total_step,
        avg_speed=_format_decimal(average_speed),
        avg_pace=format_pace(average_pace),
        cal=shifted.cal,
        step_minute=_format_decimal(cadence, 1),
        max_speed_per_hour=_format_decimal(max(maximum_speed, average_speed)),
        min_speed_per_hour=_format_decimal(min(minimum_speed, average_speed)),
        indoor=shifted.indoor,
        route_polyline_bh=shifted.route_polyline_bh,
        already_pass_point=shifted.already_pass_point,
        activity_status=1,
        status=0,
        # App 在结束运动时先做本地有效性校验，合法完成记录写入 1；
        # UploadJsonSports 会把该值原样作为 isValid。0 不是合法的待判定值。
        result=1,
        gyro_upload_status=0,
        has_photo=0,
        points=points,
        pace=_rebuild_pace(
            points,
            target_track_distance_m,
            shifted.start_date_long,
        ),
        minute_speed=_rebuild_minute_speed(
            points,
            active_seconds,
            shifted.start_date_long,
        ),
    )

    assert_record_consistent(mutated)
    return mutated


def print_diff_summary(original: SportRecord, mutated: SportRecord) -> None:
    """打印原始记录与变异记录的差异摘要。"""
    print("=" * 60)
    print("  变异记录对比")
    print("=" * 60)
    print(f"  {'字段':<20} {'原始':>18} {'变异':>18}")
    print(f"  {'-'*20} {'-'*18} {'-'*18}")
    print(f"  {'sportId':<20} {original.sport_id[:16]:>18} {mutated.sport_id[:16]:>18}")
    print(f"  {'里程 (km)':<20} {original.dllc:>18} {mutated.dllc:>18}")
    print(f"  {'步数':<20} {original.total_step:>18} {mutated.total_step:>18}")
    print(f"  {'平均速度':<20} {original.avg_speed:>18} {mutated.avg_speed:>18}")
    print(f"  {'平均配速':<20} {original.avg_pace:>18} {mutated.avg_pace:>18}")
    print(f"  {'热量':<20} {original.cal:>18.1f} {mutated.cal:>18.1f}")
    print(f"  {'步频':<20} {original.step_minute:>18} {mutated.step_minute:>18}")
    print(f"  {'最大时速':<20} {original.max_speed_per_hour:>18} {mutated.max_speed_per_hour:>18}")
    print(f"  {'最小时速':<20} {original.min_speed_per_hour:>18} {mutated.min_speed_per_hour:>18}")
    print(f"  {'轨迹点数':<20} {len(original.points):>18} {len(mutated.points):>18}")
    print(f"  {'配速条数':<20} {len(original.pace):>18} {len(mutated.pace):>18}")
    print(f"  {'开始时间':<20} {original.start_date:>18} {mutated.start_date:>18}")
    print(f"  {'结束时间':<20} {original.end_date:>18} {mutated.end_date:>18}")
    print("=" * 60)
