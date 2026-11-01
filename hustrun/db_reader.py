"""
数据库读取器 — 从 ygty_mysport.db (WCDB 加密) 读取运动记录。

需要 sqlcipher3 库支持（pip install sqlcipher3）。
如果 sqlcipher3 不可用，建议使用 JSON 数据源。
"""

from typing import Optional

from .models import SportRecord, TrackPoint, PaceEntry, MinuteSpeedEntry
from .crypto import open_wcdb


def read_sport_record(
    db_path: str,
    wcdb_key: str,
    sport_id: Optional[str] = None,
) -> SportRecord:
    """
    从 ygty_mysport.db 读取运动记录及关联数据。

    查询顺序：
    1. sportbean — 运动记录主表
    2. sportlatlngbean — 轨迹点 (WHERE sportbean_id = ?)
    3. cpacestr — 每公里配速 (WHERE sportId = ?)
    4. cminutespeedstr — 每分钟速度 (WHERE sportId = ?)

    Args:
        db_path: 数据库路径
        wcdb_key: WCDB 解密密钥
        sport_id: 指定 sportid，不指定则取最新一条

    Returns:
        SportRecord 对象

    Raises:
        RuntimeError: 数据库无法打开或解密失败
        ValueError: 找不到记录
    """
    conn = open_wcdb(db_path, wcdb_key)

    try:
        conn.row_factory = _dict_factory

        # 1. 查询主记录
        if sport_id:
            row = conn.execute(
                "SELECT * FROM sportbean WHERE sportid = ?",
                (sport_id,),
            ).fetchone()
        else:
            # 取最新一条（按 enddatelong 降序）
            row = conn.execute(
                "SELECT * FROM sportbean ORDER BY enddatelong DESC LIMIT 1"
            ).fetchone()

        if not row:
            raise ValueError(
                f"找不到运动记录"
                + (f" (sportid={sport_id})" if sport_id else "")
            )

        record = _parse_sportbean_row(row)

        # 2. 查询轨迹点
        points_rows = conn.execute(
            "SELECT * FROM sportlatlngbean WHERE sportbean_id = ? ORDER BY t ASC",
            (record.sport_id,),
        ).fetchall()

        points = []
        for pt_row in points_rows:
            points.append(TrackPoint(
                a=pt_row.get("a", 0.0),
                o=pt_row.get("o", 0.0),
                v=pt_row.get("v", 1),
                s=pt_row.get("s", 0.0),
                t=pt_row.get("t", 0),
                ac=pt_row.get("ac", 0.0),
                da=pt_row.get("da", 0.0),
                d=pt_row.get("d", 0.0),
                st=pt_row.get("st", 0),
                sta=pt_row.get("sta", 0),
                sportbean_id=record.sport_id,
            ))

        # 3. 查询配速
        pace_rows = conn.execute(
            "SELECT * FROM cpacestr WHERE sportId = ? ORDER BY CAST(km AS INTEGER) ASC",
            (record.sport_id,),
        ).fetchall()
        pace = [
            PaceEntry(
                km=str(r.get("km", "")),
                t=str(r.get("t", "")),
                km_total_length=float(r.get("kmtotallength") or 0),
                km_dlsj=int(r.get("kmdlsj") or 0),
            )
            for r in pace_rows
        ]

        # 4. 查询分钟速度
        ms_rows = conn.execute(
            "SELECT * FROM cminutespeedstr WHERE sportId = ? ORDER BY CAST(min AS INTEGER) ASC",
            (record.sport_id,),
        ).fetchall()
        minute_speed = [
            MinuteSpeedEntry(min=str(r.get("min", "")), v=str(r.get("v", "")))
            for r in ms_rows
        ]

        # 组装
        record.points = points
        record.pace = pace
        record.minute_speed = minute_speed

        return record

    finally:
        conn.close()


def list_sport_ids(db_path: str, wcdb_key: str, limit: int = 20) -> list[dict]:
    """
    列出数据库中的运动记录摘要。

    Args:
        db_path: 数据库路径
        wcdb_key: WCDB 密钥
        limit: 最多返回条数

    Returns:
        [{sportid, startdate, enddate, dllc, totalstep, sporttype, activitystatus}, ...]
    """
    conn = open_wcdb(db_path, wcdb_key)

    try:
        conn.row_factory = _dict_factory
        rows = conn.execute(
            "SELECT sportid, startdate, enddate, dllc, totalstep, "
            "sporttype, activitystatus, result "
            "FROM sportbean ORDER BY enddatelong DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _dict_factory(cursor, row):
    """将查询结果转为字典"""
    columns = [col[0] for col in cursor.description]
    return {col: row[i] for i, col in enumerate(columns)}


def _parse_sportbean_row(row: dict) -> SportRecord:
    """将 sportbean 行转为 SportRecord"""
    return SportRecord(
        sport_id=row.get("sportid", ""),
        xh=row.get("uid", ""),
        name=row.get("name") or "",
        route_id=str(row.get("routeid", "")),
        route_name=row.get("routename", ""),
        sport_type=str(row.get("sporttype", "")),
        start_date=row.get("startdate", ""),
        end_date=row.get("enddate", ""),
        start_date_long=row.get("startdatelong", 0),
        end_date_long=row.get("enddatelong", 0),
        dl_time=row.get("dltime", ""),
        last_odometer_time=row.get("lastodometertime", ""),
        dllc=str(row.get("dllc", "")),
        total_step=row.get("totalstep", 0),
        avg_speed=str(row.get("avgspeed", "")),
        avg_pace=row.get("avgpace", ""),
        cal=float(row.get("cal", 0)),
        step_minute=str(row.get("stepminute", "")),
        max_speed_per_hour=str(row.get("maxspeedperhour", "")),
        min_speed_per_hour=str(row.get("minspeedperhour", "")),
        indoor=row.get("indoor", 0),
        route_polyline_bh=row.get("routepolylinebh", ""),
        already_pass_point=row.get("alreadypasspoint", ""),
        activity_status=int(row.get("activitystatus") or 0),
        status=int(row.get("status") or 0),
        result=int(row.get("result") or 0),
        gyro_upload_status=int(row.get("gyrouploadstatus") or 0),
        has_photo=int(row.get("hasphoto") or 0),
    )
