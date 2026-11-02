"""一键上传编排：申请 sportId → 沿路径生成记录 → 等到记录结束 → 提交。

官方时间线（HAR 已确认）：

    运动开始前  GET  /api/l/v7/sportsId
    运动中      客户端逐秒记录轨迹
    点“结束”    POST /api/l/v6.1/prejudgment
    倒计时内    POST /api/l/v7/savesports
    随后        POST /api/l/v7/upload/sensorZipFile

其中 sportId 由服务端在运动**开始前**分配。因此默认流程先申请 sportId，
再把记录的开始时间设为申请成功之后，并等到记录的结束时间真正过去才提交。
``fast=True`` 会立即提交一条“回溯生成”的记录，仅用于对照实验。

这条时间线可以拆成两段，跨进程执行：

    python -m hustrun prepare -r ... -d 2150   # 领取 sportId 并生成记录，落盘
    python -m hustrun submit --session ...     # 到点后只做提交

拆开的好处是等待期间可以关掉终端，WAF/登录问题也不会让已领取的 sportId 作废。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from .builder import build_json_string, build_prejudgment_json, build_upload_json
from .config import load_auth_config
from .consistency import (
    RecordConsistencyError,
    assert_record_consistent,
    assert_record_not_in_server_future,
)
from .crypto import encrypt_for_upload
from .generator import (
    generate_route_record,
    load_route,
    make_sensor_trace,
    route_summary,
)
from .http_client import (
    probe_server_time,
    request_in_sport_time,
    request_prejudgment,
    request_sports_id,
    upload_savesports,
    upload_sensor_zip,
)
from .models import (
    AuthConfig,
    MinuteSpeedEntry,
    PaceEntry,
    SportRecord,
    TrackPoint,
    UploadResult,
)
from .signer import PythonSigner, RequestSigner, SignerError

# 官方服务允许的运动时段（客户端直接丢弃时段外的数据）。
ALLOWED_HOUR_START = 6
ALLOWED_HOUR_END = 23
# 服务端时间按北京时间解释，避免本机时区不是 UTC+8 时判断错位。
SERVER_TZ = timezone(timedelta(hours=8))
# prepare/submit 之间传递已领取 sportId 的落地目录（含个人轨迹，已在 .gitignore）。
SESSION_DIR = Path(__file__).resolve().parent.parent / ".sessions"
SESSION_VERSION = 1

Progress = Callable[[str], None]


@dataclass
class UploadOptions:
    route_path: str
    target_distance_m: float = 2_150.0
    seed: Optional[int] = None
    fast: bool = False
    skip_sensor: bool = False
    dry_run: bool = False
    output_payload: Optional[str] = None
    wait_extra_seconds: float = 3.0
    sport_type: str = "3"
    route_id: str = "7"
    route_name: str = "跑步-男生"


@dataclass
class FlowReport:
    result: UploadResult
    record: Optional[SportRecord] = None
    prejudgment_ok: bool = False
    server_sport_id: str = ""
    sport_id_issued_at_ms: Optional[int] = None
    waited_seconds: float = 0.0
    session_path: str = ""
    notes: list[str] = field(default_factory=list)


def _noop(_message: str) -> None:
    pass


def open_signer(config_path: str = "config.yaml") -> RequestSigner:
    """返回签名器上下文（纯 Python，不启动子进程、不读 APK/.so）。"""
    return PythonSigner()


class PrepareAbort(Exception):
    """准备阶段失败，`report` 里已带好给用户看的原因。"""

    def __init__(self, report: "FlowReport"):
        super().__init__(report.result.raw_response)
        self.report = report


def _require_auth(config_path: str) -> AuthConfig:
    """读凭证；缺失或本地 JWT 已过期时直接失败，不启动签名器也不碰网络。"""
    auth = load_auth_config(config_path)
    if auth.missing_session_fields:
        raise PrepareAbort(
            FlowReport(
                UploadResult(
                    success=False,
                    raw_response=(
                        "登录凭证缺失或已过期，请先运行 `python -m hustrun login`"
                    ),
                ),
                notes=list(auth.missing_session_fields),
            )
        )
    if auth.is_token_expired:
        raise PrepareAbort(
            FlowReport(
                UploadResult(
                    success=False,
                    raw_response=(
                        "TOKEN 已过期，请先运行 `python -m hustrun login` 刷新凭证"
                    ),
                ),
                notes=["token_expired"],
            )
        )
    return auth


def _shift_record(record: SportRecord, delta_ms: int) -> None:
    """整体平移一条已生成记录的时间线（用于 fast 对照模式）。"""
    record.start_date_long += delta_ms
    record.end_date_long += delta_ms
    for point in record.points:
        if point.t > 0:
            point.t += delta_ms
    record.start_date = datetime.fromtimestamp(
        record.start_date_long / 1000.0, tz=SERVER_TZ
    ).strftime("%Y-%m-%d %H:%M:%S")
    record.end_date = datetime.fromtimestamp(
        record.end_date_long / 1000.0, tz=SERVER_TZ
    ).strftime("%Y-%m-%d %H:%M:%S")
    assert_record_consistent(record)


def _check_window(record: SportRecord, progress: Progress) -> bool:
    start_hour = datetime.fromtimestamp(
        record.start_date_long / 1000.0, tz=SERVER_TZ
    ).hour
    end_hour = datetime.fromtimestamp(
        record.end_date_long / 1000.0, tz=SERVER_TZ
    ).hour
    if (
        ALLOWED_HOUR_START <= start_hour < ALLOWED_HOUR_END
        and ALLOWED_HOUR_START <= end_hour < ALLOWED_HOUR_END
    ):
        return True
    progress(
        f"[时间] 记录 {start_hour:02d}:xx–{end_hour:02d}:xx，官方允许时段为 "
        f"{ALLOWED_HOUR_START:02d}:00–{ALLOWED_HOUR_END:02d}:00（含结束时刻）"
    )
    return False


def upload_route(
    config_path: str = "config.yaml",
    options: UploadOptions | None = None,
    progress: Optional[Progress] = None,
) -> FlowReport:
    """生成并上传一条路径记录。"""
    if options is None:
        raise ValueError("缺少上传参数")
    progress = progress or _noop

    if options.dry_run:
        # 离线生成只要学号/姓名，不需要 token 还有效。
        auth = load_auth_config(config_path)
        if auth.missing_session_fields:
            return FlowReport(
                UploadResult(
                    success=False,
                    raw_response="登录凭证缺失，请先运行 `python -m hustrun login`",
                ),
                notes=list(auth.missing_session_fields),
            )
    else:
        try:
            auth = _require_auth(config_path)
        except PrepareAbort as abort:
            return abort.report

    geometry = load_route(options.route_path)
    progress(f"[路径] {options.route_path}: {geometry.length_m:.0f} m")

    if options.dry_run:
        start_ms = int(time.time() * 1000)
        record = generate_route_record(
            geometry,
            start_ms=start_ms,
            sport_id="00000000-0000-4000-8000-000000000000",
            xh=auth.xh,
            name=auth.name,
            target_distance_m=options.target_distance_m,
            seed=options.seed,
            sport_type=options.sport_type,
            route_id=options.route_id,
            route_name=options.route_name,
        )
        progress(f"[生成] {route_summary(record)}")
        _dump_payload(record, auth, options, progress)
        return FlowReport(
            UploadResult(success=False, raw_response="dry-run"),
            record=record,
            notes=["dry-run：未访问网络"],
        )

    try:
        with open_signer(config_path) as signer:
            return _run_network_flow(auth, geometry, options, signer, progress)
    except SignerError as exc:
        return FlowReport(UploadResult(success=False, raw_response=str(exc)))


def _dump_payload(
    record: SportRecord,
    auth: AuthConfig,
    options: UploadOptions,
    progress: Progress,
) -> None:
    if not options.output_payload:
        return
    Path(options.output_payload).write_text(
        json.dumps(
            build_upload_json(record, auth),
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    progress(f"[输出] 明文 payload 已写入 {options.output_payload}")


def _claim_and_generate(
    auth: AuthConfig,
    geometry,
    options: UploadOptions,
    signer: RequestSigner,
    progress: Progress,
    notes: list[str],
) -> tuple[str, int, SportRecord, int]:
    """服务端时间 → 时段判定 → 领取 sportId → 生成记录（还不提交）。

    Returns:
        (sportId, 领取时刻 ms, 记录, 服务端时间 ms, 服务端减本机的时差 ms)
    """
    probe = probe_server_time(auth, signer)
    if not probe.ok or not probe.server_time:
        raise PrepareAbort(
            FlowReport(
                UploadResult(
                    success=False,
                    raw_response=f"服务器时间检查失败: {probe.message}",
                    http_status=probe.http_status,
                    api_code=probe.api_code,
                ),
                notes=[probe.message],
            )
        )
    offset_ms = probe.server_time - int(time.time() * 1000)
    progress(
        "[服务器] 时间 "
        + datetime.fromtimestamp(
            probe.server_time / 1000.0, tz=SERVER_TZ
        ).strftime("%Y-%m-%d %H:%M:%S")
    )

    # 服务端时段判定（官方选择运动类型时也会查这个接口）
    window = request_in_sport_time(auth, options.route_id, signer)
    if window.ok:
        progress(
            "[时段] 服务端判定: "
            + ("在允许时段内" if window.allowed else "不在允许时段")
        )
        if not window.allowed:
            raise PrepareAbort(
                FlowReport(
                    UploadResult(
                        success=False,
                        raw_response="服务端判定当前不在该运动的允许时段，提交只会得到 inValidReason=4",
                        http_status=window.http_status,
                        api_code=window.api_code,
                    ),
                    notes=[window.message],
                )
            )
    else:
        notes.append(f"服务端时段查询失败，改用本地 6–23 点判断: {window.message}")

    progress("[服务器] GET /api/l/v7/sportsId ...")
    sports = request_sports_id(auth, signer)
    if not sports.ok:
        raise PrepareAbort(
            FlowReport(
                UploadResult(
                    success=False,
                    raw_response=f"申请 sportId 失败: {sports.message}",
                    http_status=sports.http_status,
                    api_code=sports.api_code,
                ),
                notes=[sports.message],
            )
        )
    sport_id = sports.sport_id
    issued_at_ms = int(time.time() * 1000) + offset_ms
    progress(
        f"[服务器] sportId={sport_id}（领取于 "
        f"{datetime.fromtimestamp(issued_at_ms / 1000.0, tz=SERVER_TZ).strftime('%H:%M:%S')}）"
    )

    # 再取一次服务端时间，确保记录的起点确实晚于 sportId 的分配时刻。
    anchor = probe_server_time(auth, signer)
    anchor_ms = probe.server_time
    if anchor.ok and anchor.server_time:
        anchor_ms = anchor.server_time

    # 记录从 sportId 分配之后开始
    record = generate_route_record(
        geometry,
        start_ms=anchor_ms + 3_000,
        sport_id=sport_id,
        xh=auth.xh,
        name=auth.name,
        target_distance_m=options.target_distance_m,
        seed=options.seed,
        sport_type=options.sport_type,
        route_id=options.route_id,
        route_name=options.route_name,
    )
    progress(f"[生成] {route_summary(record)}")
    if not _check_window(record, progress):
        raise PrepareAbort(
            FlowReport(
                UploadResult(
                    success=False,
                    raw_response="当前不在官方允许的运动时段内",
                ),
                record=record,
                server_sport_id=sport_id,
                sport_id_issued_at_ms=issued_at_ms,
            )
        )
    return sport_id, issued_at_ms, record, anchor_ms, offset_ms


def _wait_for_end(
    record: SportRecord,
    auth: AuthConfig,
    signer: RequestSigner,
    progress: Progress,
    extra_seconds: float,
) -> float:
    """阻塞到记录的结束时间（按服务端时钟）之后，返回实际等待秒数。"""
    probe = probe_server_time(auth, signer)
    offset_ms = 0
    if probe.ok and probe.server_time:
        offset_ms = probe.server_time - int(time.time() * 1000)
    else:
        progress(f"[等待] 取服务端时间失败，按本机时钟估算: {probe.message}")
    target_local_ms = record.end_date_long - offset_ms + int(extra_seconds * 1000)
    started = time.monotonic()
    while True:
        remaining = (target_local_ms - int(time.time() * 1000)) / 1000.0
        if remaining <= 0:
            break
        if remaining > 60:
            progress(f"[等待] 记录还需 {remaining / 60:.1f} 分钟结束 ...")
            time.sleep(min(60.0, remaining - 55.0))
        else:
            time.sleep(max(0.2, remaining))
    progress("[等待] 记录时间已结束，开始提交")
    return time.monotonic() - started


def _submit_record(
    auth: AuthConfig,
    record: SportRecord,
    options: UploadOptions,
    signer: RequestSigner,
    progress: Progress,
    notes: list[str],
    *,
    sport_id: str,
    issued_at_ms: Optional[int],
    waited: float,
) -> FlowReport:
    """提交前校验 → payload → prejudgment → savesports → sensorZipFile。"""
    try:
        assert_record_consistent(record)
    except RecordConsistencyError as exc:
        return FlowReport(
            UploadResult(success=False, raw_response=f"本地一致性校验失败: {exc}"),
            record=record,
            server_sport_id=sport_id,
            sport_id_issued_at_ms=issued_at_ms,
            waited_seconds=waited,
            notes=notes,
        )

    # 提交前再用服务端时间确认记录已经结束
    confirm = probe_server_time(auth, signer)
    if confirm.ok and confirm.server_time:
        try:
            assert_record_not_in_server_future(record, confirm.server_time)
        except RecordConsistencyError as exc:
            return FlowReport(
                UploadResult(success=False, raw_response=f"上传前时间校验失败: {exc}"),
                record=record,
                server_sport_id=sport_id,
                sport_id_issued_at_ms=issued_at_ms,
                waited_seconds=waited,
                notes=notes,
            )

    # 构造并加密 payload
    payload_json = build_json_string(record, auth)
    encrypted = encrypt_for_upload(payload_json, auth.aes_key, auth.aes_encoding)
    progress(f"[构造] payload {len(payload_json)} 字符，密文 {len(encrypted)} 字符")

    # prejudgment
    prejudgment_json = build_prejudgment_json(record, auth)
    encrypted_prejudgment = encrypt_for_upload(
        prejudgment_json, auth.aes_key, auth.aes_encoding
    )
    progress("[预判] POST /api/l/v6.1/prejudgment ...")
    prejudgment = request_prejudgment(auth, encrypted_prejudgment, signer)
    if not prejudgment.ok:
        detail = "；".join(check.content for check in prejudgment.failures) or prejudgment.message
        return FlowReport(
            UploadResult(
                success=False,
                raw_response=f"prejudgment 未通过: {detail}",
                http_status=prejudgment.http_status,
                api_code=prejudgment.api_code,
            ),
            record=record,
            server_sport_id=sport_id,
            sport_id_issued_at_ms=issued_at_ms,
            waited_seconds=waited,
            notes=notes,
        )
    for check in prejudgment.checks:
        progress(
            f"[预判] {'通过' if check.is_valid == '1' else '失败'}: {check.content}"
        )

    # savesports
    progress("[上传] POST /api/l/v7/savesports ...")
    result = upload_savesports(auth, encrypted, signer)
    result.sport_id = result.sport_id or sport_id
    progress(
        f"[上传] isValid={result.is_valid} reason='{result.in_valid_reason}' "
        f"checkStatus={result.check_status} tip='{result.tip}'"
    )

    # sensorZipFile
    if result.success and result.is_valid == "1" and not options.skip_sensor:
        from .sensor import prepare_sensor_zip_upload

        sensor_text = make_sensor_trace(record.duration_seconds, seed=options.seed)
        filename, zip_b64 = prepare_sensor_zip_upload(sensor_text, result.sport_id)
        progress("[传感器] POST /api/l/v7/upload/sensorZipFile ...")
        result.sensor_zip_ok = upload_sensor_zip(
            auth,
            result.sport_id,
            zip_b64,
            filename=filename,
            signer=signer,
        )
        progress(f"[传感器] {'OK' if result.sensor_zip_ok else 'FAIL'}")
        if not result.sensor_zip_ok:
            notes.append("savesports 成功但 sensorZipFile 上传失败")

    return FlowReport(
        result=result,
        record=record,
        prejudgment_ok=True,
        server_sport_id=sport_id,
        sport_id_issued_at_ms=issued_at_ms,
        waited_seconds=waited,
        notes=notes,
    )


def _run_network_flow(
    auth: AuthConfig,
    geometry,
    options: UploadOptions,
    signer: RequestSigner,
    progress: Progress,
) -> FlowReport:
    """一次性流程：领取 sportId → 生成 → 等结束 → 提交。"""
    notes: list[str] = []
    try:
        sport_id, issued_at_ms, record, server_time_ms, _offset_ms = _claim_and_generate(
            auth, geometry, options, signer, progress, notes
        )
    except PrepareAbort as abort:
        return abort.report

    _dump_payload(record, auth, options, progress)

    # 等到记录结束（fast 模式直接把时间线整体前移）
    waited = 0.0
    if options.fast:
        latest_end = server_time_ms - 2_000
        if record.end_date_long > latest_end:
            delta = latest_end - record.end_date_long
            _shift_record(record, delta)
        progress("[时间] fast 模式：记录时间线已整体前移，立即提交")
    else:
        waited = _wait_for_end(
            record, auth, signer, progress, options.wait_extra_seconds
        )

    # 这条差值就是「sportId 分配时刻 vs 轨迹最早时间戳」的实验变量。
    shift_seconds = (record.start_date_long - issued_at_ms) / 1000.0
    progress(
        f"[时间] 记录起点比 sportId 领取时刻"
        f"{'晚' if shift_seconds >= 0 else '早'} {abs(shift_seconds):.1f} 秒"
    )

    return _submit_record(
        auth,
        record,
        options,
        signer,
        progress,
        notes,
        sport_id=sport_id,
        issued_at_ms=issued_at_ms,
        waited=waited,
    )


def _capture_fields(cls) -> set[str]:
    return {item.name for item in fields(cls)}


def save_session(
    session_path: Path,
    *,
    sport_id: str,
    issued_at_ms: int,
    record: SportRecord,
    options: UploadOptions,
) -> Path:
    """把已领取的 sportId 和生成的记录落盘，供稍后 `submit` 使用。"""
    data = {
        "version": SESSION_VERSION,
        "sport_id": sport_id,
        "issued_at_ms": issued_at_ms,
        "created_at": datetime.now(SERVER_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "route_path": options.route_path,
        "target_distance_m": options.target_distance_m,
        "seed": options.seed,
        "sport_type": options.sport_type,
        "route_id": options.route_id,
        "route_name": options.route_name,
        "record": asdict(record),
    }
    path = Path(session_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def load_session(session_path: Path) -> tuple[SportRecord, dict]:
    """读回 `save_session` 写出的记录。"""
    data = json.loads(Path(session_path).read_text(encoding="utf-8"))
    version = data.get("version")
    if version != SESSION_VERSION:
        raise ValueError(f"会话文件版本不匹配: {version} != {SESSION_VERSION}")
    raw = data.get("record") or {}
    top_fields = _capture_fields(SportRecord) - {"points", "pace", "minute_speed"}
    point_fields = _capture_fields(TrackPoint)
    pace_fields = _capture_fields(PaceEntry)
    minute_fields = _capture_fields(MinuteSpeedEntry)
    record = SportRecord(
        **{k: v for k, v in raw.items() if k in top_fields},
        points=[
            TrackPoint(**{k: v for k, v in item.items() if k in point_fields})
            for item in raw.get("points", [])
        ],
        pace=[
            PaceEntry(**{k: v for k, v in item.items() if k in pace_fields})
            for item in raw.get("pace", [])
        ],
        minute_speed=[
            MinuteSpeedEntry(**{k: v for k, v in item.items() if k in minute_fields})
            for item in raw.get("minute_speed", [])
        ],
    )
    if not record.sport_id:
        record.sport_id = data.get("sport_id", "")
    return record, data


def latest_session() -> Optional[Path]:
    """最近一次 prepare 写出的会话文件。"""
    if not SESSION_DIR.is_dir():
        return None
    files = sorted(
        SESSION_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return files[0] if files else None


def prepare_route(
    config_path: str = "config.yaml",
    options: UploadOptions | None = None,
    progress: Optional[Progress] = None,
) -> FlowReport:
    """领取 sportId、生成记录并落盘，不提交（第一阶段）。

    与 `upload_route` 的区别只有「不等、不交」：时间线仍然是 sportId 之后开始，
    结束时间交给 `submit_session` 去等。
    """
    if options is None:
        raise ValueError("缺少上传参数")
    progress = progress or _noop

    try:
        auth = _require_auth(config_path)
    except PrepareAbort as abort:
        return abort.report

    geometry = load_route(options.route_path)
    progress(f"[路径] {options.route_path}: {geometry.length_m:.0f} m")

    notes: list[str] = []
    try:
        with open_signer(config_path) as signer:
            sport_id, issued_at_ms, record, _server_ms, offset_ms = _claim_and_generate(
                auth, geometry, options, signer, progress, notes
            )
    except PrepareAbort as abort:
        return abort.report
    except SignerError as exc:
        return FlowReport(UploadResult(success=False, raw_response=str(exc)))

    _dump_payload(record, auth, options, progress)

    path = save_session(
        SESSION_DIR / f"{record.start_date_long}_{sport_id}.json",
        sport_id=sport_id,
        issued_at_ms=issued_at_ms,
        record=record,
        options=options,
    )
    # 用服务端时钟算剩余时间：本机时钟可能和服务器差几分钟，
    # 直接用 time.time() 会把「还要等 8 分钟」显示成「2 分钟」。
    server_now_ms = int(time.time() * 1000) + offset_ms
    remaining = (record.end_date_long - server_now_ms) / 1000.0
    progress(f"[会话] 已写入 {path}")
    progress(
        f"[会话] 记录 {record.start_date} → {record.end_date}，"
        f"约 {max(0.0, remaining) / 60:.1f} 分钟后可提交："
    )
    progress(f"[会话] python -m hustrun submit --session {path}")
    return FlowReport(
        UploadResult(success=False, raw_response="prepared"),
        record=record,
        server_sport_id=sport_id,
        sport_id_issued_at_ms=issued_at_ms,
        session_path=str(path),
        notes=notes,
    )


def submit_session(
    config_path: str = "config.yaml",
    session_path: Optional[str] = None,
    *,
    skip_sensor: bool = False,
    no_wait: bool = False,
    output_payload: Optional[str] = None,
    progress: Optional[Progress] = None,
) -> FlowReport:
    """第二阶段：等记录结束，然后提交已落盘的记录。"""
    progress = progress or _noop
    path = Path(session_path) if session_path else latest_session()
    if path is None:
        return FlowReport(
            UploadResult(
                success=False,
                raw_response=f"没有可提交的会话文件（{SESSION_DIR} 为空），先运行 `python -m hustrun prepare`",
            )
        )
    if not path.is_file():
        return FlowReport(
            UploadResult(success=False, raw_response=f"会话文件不存在: {path}")
        )
    try:
        record, data = load_session(path)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        return FlowReport(
            UploadResult(success=False, raw_response=f"会话文件无法解析: {exc}")
        )

    try:
        auth = _require_auth(config_path)
    except PrepareAbort as abort:
        return abort.report

    # 学号/姓名以当前登录态为准（prepare 之后可能重新登录过）。
    record.xh = auth.xh
    record.name = auth.name or record.name
    sport_id = data.get("sport_id") or record.sport_id
    issued_at_ms = data.get("issued_at_ms")

    options = UploadOptions(
        route_path=data.get("route_path", ""),
        target_distance_m=float(data.get("target_distance_m") or 0.0),
        seed=data.get("seed"),
        skip_sensor=skip_sensor,
        output_payload=output_payload,
        sport_type=data.get("sport_type") or "3",
        route_id=data.get("route_id") or "7",
        route_name=data.get("route_name") or "跑步-男生",
    )

    notes: list[str] = []
    try:
        with open_signer(config_path) as signer:
            probe = probe_server_time(auth, signer)
            if probe.ok and probe.server_time:
                progress(
                    "[服务器] 时间 "
                    + datetime.fromtimestamp(
                        probe.server_time / 1000.0, tz=SERVER_TZ
                    ).strftime("%Y-%m-%d %H:%M:%S")
                )
            if no_wait:
                if (
                    probe.ok
                    and probe.server_time
                    and record.end_date_long > probe.server_time
                ):
                    remaining = (record.end_date_long - probe.server_time) / 1000.0
                    return FlowReport(
                        UploadResult(
                            success=False,
                            raw_response=(
                                f"记录还没结束，还需 {remaining / 60:.1f} 分钟；"
                                f"去掉 --no-wait 可原地等待"
                            ),
                        ),
                        record=record,
                        server_sport_id=sport_id,
                        sport_id_issued_at_ms=issued_at_ms,
                        session_path=str(path),
                        notes=["not_finished"],
                    )
                waited = 0.0
            else:
                waited = _wait_for_end(record, auth, signer, progress, 0.0)

            _dump_payload(record, auth, options, progress)
            return _submit_record(
                auth,
                record,
                options,
                signer,
                progress,
                notes,
                sport_id=sport_id,
                issued_at_ms=issued_at_ms,
                waited=waited,
            )
    except SignerError as exc:
        return FlowReport(UploadResult(success=False, raw_response=str(exc)))


def check_session(config_path: str = "config.yaml", progress: Optional[Progress] = None) -> dict:
    """只读自检：签名器可用、服务端时间可读、token 未过期。"""
    progress = progress or _noop
    auth = load_auth_config(config_path)
    out = {
        "token_present": bool(auth.token),
        "token_expired": auth.is_token_expired,
        "secret_present": bool(auth.secret),
        "server_time": None,
        "in_sport_time": None,
        "ok": False,
        "message": "",
    }
    if not auth.token or not auth.secret:
        out["message"] = "缺少 TOKEN/SECRET"
        return out
    with open_signer(config_path) as signer:
        probe = probe_server_time(auth, signer)
        out.update(
            server_time=probe.server_time,
            ok=probe.ok,
            message=probe.message,
            http_status=probe.http_status,
            api_code=probe.api_code,
        )
        progress(f"[自检] 服务端时间: {probe.server_time} ok={probe.ok} {probe.message}")
        if probe.ok:
            window = request_in_sport_time(auth, "7", signer)
            out["in_sport_time"] = window.allowed
            progress(
                "[自检] 运动时段: "
                + (
                    "允许"
                    if window.allowed
                    else "不允许"
                    if window.allowed is False
                    else f"查询失败 ({window.message})"
                )
            )
    return out
