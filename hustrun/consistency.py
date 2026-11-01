"""运动记录内部一致性检查。

这里只检查可由 payload 自身相互验证的关系，不猜测服务端风控阈值，也不把
校验通过解释为服务端一定判定有效。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .models import SportRecord


_PACE_RE = re.compile(r"^(\d+)'([0-5]\d)''$")
_DURATION_RE = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)$")


@dataclass(frozen=True)
class ConsistencyIssue:
    """一项可定位的数据一致性问题。"""

    code: str
    message: str


class RecordConsistencyError(ValueError):
    """记录未通过本地一致性检查。"""

    def __init__(self, issues: list[ConsistencyIssue]):
        self.issues = issues
        detail = "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
        super().__init__(f"运动记录内部不一致: {detail}")


def parse_pace_seconds(value: str) -> int | None:
    """解析 ``04'08''``，格式无效时返回 None。"""
    match = _PACE_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def format_pace(seconds: float) -> str:
    """把秒/公里格式化为官方使用的配速字符串。"""
    rounded = max(1, int(round(seconds)))
    minutes, remainder = divmod(rounded, 60)
    return f"{minutes:02d}'{remainder:02d}''"


def parse_duration_seconds(value: str) -> int | None:
    """解析 ``HH:MM:SS`` 形式的实际运动时长。"""
    match = _DURATION_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    hours, minutes, seconds = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def haversine_distance_m(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    """计算两个 WGS84 经纬度点之间的球面距离。"""
    earth_radius_m = 6_371_000.0
    lat_a = math.radians(latitude_a)
    lat_b = math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = math.radians(longitude_b - longitude_a)
    haversine = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * earth_radius_m * math.asin(min(1.0, math.sqrt(haversine)))


def coordinate_distance_m(record: SportRecord) -> float:
    """计算 coordinate 折线总长度。"""
    return sum(
        haversine_distance_m(previous.a, previous.o, current.a, current.o)
        for previous, current in zip(record.points, record.points[1:])
    )


def validate_record_consistency(record: SportRecord) -> list[ConsistencyIssue]:
    """返回 payload 内部可验证的一致性问题；空列表表示通过。"""
    issues: list[ConsistencyIssue] = []
    wall_duration = record.duration_seconds
    active_duration = parse_duration_seconds(record.dl_time)
    duration = active_duration if active_duration and active_duration > 0 else wall_duration
    distance_km = record.distance_km
    statistics_distance_km = distance_km
    if record.points and record.points[-1].da > 0:
        statistics_distance_km = record.points[-1].da / 1000.0

    if duration <= 0:
        issues.append(ConsistencyIssue("time.duration", "结束时间必须晚于开始时间"))
    if distance_km <= 0:
        issues.append(ConsistencyIssue("distance.total", "总里程必须大于 0"))
    if len(record.points) < 2:
        issues.append(ConsistencyIssue("track.count", "轨迹点至少需要 2 个"))

    if duration > 0 and statistics_distance_km > 0:
        expected_speed = statistics_distance_km / (duration / 3600.0)
        try:
            actual_speed = float(record.avg_speed)
        except (TypeError, ValueError):
            actual_speed = math.nan
        speed_tolerance = max(0.2, expected_speed * 0.02)
        if not math.isfinite(actual_speed):
            issues.append(ConsistencyIssue("speed.average", "平均速度不是有效数值"))
        elif abs(actual_speed - expected_speed) > speed_tolerance:
            issues.append(
                ConsistencyIssue(
                    "speed.average",
                    f"平均速度 {actual_speed:.2f} 与累计距离/时长推导值 "
                    f"{expected_speed:.2f} km/h 不符",
                )
            )
        elif actual_speed > 50.0:
            issues.append(
                ConsistencyIssue(
                    "speed.plausibility",
                    f"平均速度 {actual_speed:.2f} km/h 超出步行/跑步合理范围",
                )
            )

        actual_pace = parse_pace_seconds(record.avg_pace)
        expected_pace = duration / statistics_distance_km
        if actual_pace is None:
            issues.append(ConsistencyIssue("pace.average", "平均配速格式无效"))
        elif abs(actual_pace - expected_pace) > 3.0:
            issues.append(
                ConsistencyIssue(
                    "pace.average",
                    f"平均配速 {actual_pace} 秒/公里与累计距离/时长推导值 "
                    f"{expected_pace:.1f} 秒/公里不符",
                )
            )

        expected_cadence = record.total_step / (duration / 60.0)
        try:
            actual_cadence = float(record.step_minute)
        except (TypeError, ValueError):
            actual_cadence = math.nan
        if not math.isfinite(actual_cadence):
            issues.append(ConsistencyIssue("step.cadence", "步频不是有效数值"))
        elif abs(actual_cadence - expected_cadence) > 0.2:
            issues.append(
                ConsistencyIssue(
                    "step.cadence",
                    f"步频 {actual_cadence:.1f} 与步数/时长推导值 "
                    f"{expected_cadence:.1f} 不符",
                )
            )

    if record.points:
        previous_time = None
        previous_da = None
        previous_point = None
        segment_sum = 0.0
        valid_coordinates = True
        for index, point in enumerate(record.points):
            if not (
                math.isfinite(point.a)
                and math.isfinite(point.o)
                and -90.0 <= point.a <= 90.0
                and -180.0 <= point.o <= 180.0
            ):
                valid_coordinates = False
                issues.append(
                    ConsistencyIssue(
                        "track.coordinate",
                        f"第 {index + 1} 个轨迹点经纬度无效",
                    )
                )
                break
            if (
                abs(point.a - round(point.a, 6)) > 1e-9
                or abs(point.o - round(point.o, 6)) > 1e-9
            ):
                issues.append(
                    ConsistencyIssue(
                        "track.coordinate_precision",
                        f"第 {index + 1} 个轨迹点超过 App 使用的 6 位小数精度",
                    )
                )
                break
            if point.d < 0 or point.da < 0:
                issues.append(
                    ConsistencyIssue(
                        "track.distance",
                        f"第 {index + 1} 个轨迹点距离为负数",
                    )
                )
                break
            try:
                point_speed = float(point.s)
            except (TypeError, ValueError):
                point_speed = math.nan
            if not math.isfinite(point_speed) or point_speed < 0:
                issues.append(
                    ConsistencyIssue(
                        "track.speed",
                        f"第 {index + 1} 个轨迹点速度不是有效的非负数",
                    )
                )
                break
            if point_speed > 50.0:
                issues.append(
                    ConsistencyIssue(
                        "track.segment_plausibility",
                        f"第 {index + 1} 个轨迹点瞬时速度 "
                        f"{point_speed:.2f} km/h 超出步行/跑步合理范围",
                    )
                )
                break
            if previous_time is not None and point.t < previous_time:
                issues.append(
                    ConsistencyIssue("track.time", "轨迹点时间戳不是单调递增")
                )
                break
            if previous_da is not None and point.da < previous_da:
                issues.append(
                    ConsistencyIssue("track.cumulative", "累计距离 da 出现倒退")
                )
                break

            if previous_point is not None:
                delta_ms = point.t - previous_point.t
                distance_delta = point.da - previous_point.da
                if abs(point.d - distance_delta) > max(3.0, point.d * 0.05):
                    issues.append(
                        ConsistencyIssue(
                            "track.segment_cumulative",
                            f"第 {index + 1} 个轨迹点 d={point.d:.1f} m "
                            f"与 da 增量 {distance_delta:.1f} m 不符",
                        )
                    )
                    break
                if delta_ms <= 0 and point.d > 0:
                    issues.append(
                        ConsistencyIssue(
                            "track.segment_time",
                            f"第 {index + 1} 个轨迹段有距离但没有正时间间隔",
                        )
                    )
                    break
            previous_time = point.t
            previous_da = point.da
            previous_point = point
            segment_sum += point.d

        if duration > 0:
            timed_points = [point.t for point in record.points if point.t > 0]
            tolerance_ms = 5_000
            if timed_points and (
                min(timed_points) < record.start_date_long - tolerance_ms
                or max(timed_points) > record.end_date_long + tolerance_ms
            ):
                issues.append(
                    ConsistencyIssue(
                        "track.time_window",
                        "轨迹点时间戳超出运动起止时间",
                    )
                )

        last_da = record.points[-1].da
        if last_da > 0:
            distance_tolerance = max(50.0, last_da * 0.05)
            if abs(segment_sum - last_da) > distance_tolerance:
                issues.append(
                    ConsistencyIssue(
                        "track.d_da",
                        f"分段距离之和 {segment_sum:.1f} m 与最终 da "
                        f"{last_da:.1f} m 不符",
                    )
                )
            if abs(last_da / 1000.0 - distance_km) > max(
                0.1, distance_km * 0.05
            ):
                issues.append(
                    ConsistencyIssue(
                        "track.odometer",
                        f"最终 da {last_da / 1000.0:.3f} km 与 odometer "
                        f"{distance_km:.3f} km 不符",
                    )
                )
            if valid_coordinates and len(record.points) >= 2:
                geometry = coordinate_distance_m(record)
                if abs(geometry - last_da) > max(100.0, last_da * 0.20):
                    issues.append(
                        ConsistencyIssue(
                            "track.geometry",
                            f"坐标折线 {geometry / 1000.0:.3f} km 与最终 da "
                            f"{last_da / 1000.0:.3f} km 差异过大",
                        )
                    )

    previous_cumulative = 0
    for index, pace in enumerate(record.pace):
        if pace.km_dlsj <= 0:
            continue
        if pace.km_dlsj <= previous_cumulative:
            issues.append(
                ConsistencyIssue("pace.cumulative", "分公里累计时间不是严格递增")
            )
            break
        segment_seconds = pace.km_dlsj - previous_cumulative
        pace_seconds = parse_pace_seconds(pace.t)
        if pace_seconds is None:
            issues.append(
                ConsistencyIssue(
                    "pace.segment_format",
                    f"第 {index + 1} 段配速格式无效",
                )
            )
            break
        if abs(pace_seconds - segment_seconds) > 2:
            issues.append(
                ConsistencyIssue(
                    "pace.segment",
                    f"第 {index + 1} 段配速 {pace_seconds} 秒与累计时间差 "
                    f"{segment_seconds} 秒不符",
                )
            )
            break
        previous_cumulative = pace.km_dlsj

    numeric_speeds: list[tuple[str, float]] = []
    for name, value in (
        ("min", record.min_speed_per_hour),
        ("avg", record.avg_speed),
        ("max", record.max_speed_per_hour),
    ):
        try:
            numeric_speeds.append((name, float(value)))
        except (TypeError, ValueError):
            pass
    speed_map = dict(numeric_speeds)
    if {"min", "avg", "max"} <= speed_map.keys() and not (
        speed_map["min"] <= speed_map["avg"] <= speed_map["max"]
    ):
        issues.append(
            ConsistencyIssue(
                "speed.bounds",
                "最小速度、平均速度、最大速度的大小关系无效",
            )
        )

    return issues


def assert_record_consistent(record: SportRecord) -> None:
    """记录有任何一致性问题时抛出带明细的异常。"""
    issues = validate_record_consistency(record)
    if issues:
        raise RecordConsistencyError(issues)


def validate_record_server_time(
    record: SportRecord,
    server_time_ms: int,
    allowed_future_ms: int = 120_000,
) -> list[ConsistencyIssue]:
    """检查已结束记录是否位于服务端未来。

    使用 epoch 毫秒直接比较，不依赖客户端和服务端的时区显示。允许两分钟时钟
    偏差，但不会自动篡改运动时间来绕过检查。
    """
    if server_time_ms <= 0:
        return [
            ConsistencyIssue(
                "time.server",
                "服务器没有返回有效的毫秒时间戳",
            )
        ]
    if record.end_date_long <= 0:
        return [
            ConsistencyIssue(
                "time.end",
                "记录缺少有效的结束时间戳",
            )
        ]

    future_ms = record.end_date_long - server_time_ms
    if future_ms > allowed_future_ms:
        future_minutes = future_ms / 60_000.0
        return [
            ConsistencyIssue(
                "time.future",
                f"运动结束时间位于服务器未来 {future_minutes:.1f} 分钟",
            )
        ]
    return []


def assert_record_not_in_server_future(
    record: SportRecord,
    server_time_ms: int,
    allowed_future_ms: int = 120_000,
) -> None:
    """记录结束时间明显晚于服务器时间时抛出异常。"""
    issues = validate_record_server_time(
        record,
        server_time_ms,
        allowed_future_ms=allowed_future_ms,
    )
    if issues:
        raise RecordConsistencyError(issues)
