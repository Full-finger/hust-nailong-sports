"""一键上传编排：领取 sportId → 沿路径生成记录 → 回溯到当前时间 → 提交。

官方时间线（1.2.1 客户端实测）：

    运动开始前  GET  /api/l/v7/sportsId
    运动中      客户端逐秒记录轨迹
    点“结束”    POST /api/l/v6.1/savesports
    随后        POST /api/l/v7/upload/sensorZipFile

``v6.1/savesports`` 不校验 ``beginTime`` 是否早于 sportId 的服务端创建
时刻，记录可以整体回溯到提交时刻之前，因此不必按真实时间等满整条记录。
sportId 仍是记录主键（随 payload 和传感器一起上传），但它的领取时刻不再
约束轨迹时间。

这条时间线依然可以拆成两段，跨进程执行：

    python -m hustrun prepare -r ... -d 2150   # 领取 sportId 并生成记录，落盘
    python -m hustrun submit --session ...     # 想提交时再提交，不再等待

拆开的好处是生成和提交的时机可以分开（例如定时任务只负责提交）。

默认记录的结束时刻是“服务端当前时间之前”；``UploadOptions.end_ms`` /
``--end-time`` 可以把结束时刻指到任意已经过去的北京时刻，用来补传之前
某次运动（记录时长仍由路线和距离决定）。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from .builder import build_json_string, build_upload_json
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
# 回溯记录与提交时刻之间保留的余量，避免 endTime 落在服务端未来。
BACKDATE_MARGIN_MS = 2_000
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
    skip_sensor: bool = False
    dry_run: bool = False
    output_payload: Optional[str] = None
    # 记录的结束时刻（服务端 epoch 毫秒，北京时间解释）。None 表示结束于
    # 服务端当前时间之前；指定时用于补传以前某次运动。
    end_ms: Optional[int] = None
    sport_type: str = "3"
    route_id: str = "7"
    route_name: str = "跑步-男生"


@dataclass
class FlowReport:
    result: UploadResult
    record: Optional[SportRecord] = None
    server_sport_id: str = ""
    sport_id_issued_at_ms: Optional[int] = None
    session_path: str = ""
    notes: list[str] = field(default_factory=list)


def parse_end_time(text: str) -> int:
    """把北京时间的结束时刻解析为 epoch 毫秒。

    支持 ``HH:MM`` / ``HH:MM:SS``（取今天）和 ``YYYY-MM-DD HH:MM[:SS]``。
    """
    value = text.strip()
    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%H:%M:%S",
        "%H:%M",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt.startswith("%H"):
            today = datetime.now(SERVER_TZ)
            parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
        return int(parsed.replace(tzinfo=SERVER_TZ).timestamp() * 1000)
    raise ValueError(
        f"无法解析结束时刻 {text!r}，支持 HH:MM[:SS] 或 'YYYY-MM-DD HH:MM[:SS]'"
    )


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
    """整体平移一条已生成记录的时间线。

    记录生成时按“从当前时刻向未来跑”排布，提交前再整体前移，使结束时间
    落在服务端当前时间之前。v6.1/savesports 不做 sportId 创建时刻校验，
    所以整条轨迹可以完全位于过去。
    """
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
) -> tuple[str, int, SportRecord, int, int]:
    """服务端时间 → 时段判定 → 领取 sportId → 生成回溯记录（还不提交）。

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

    # 服务端时段判定（官方选择运动类型时也会查这个接口）。补传历史记录时
    # 该接口只能反映“现在”，记录本身是否在允许时段由下方本地检查负责。
    explicit_end = options.end_ms is not None
    window = request_in_sport_time(auth, options.route_id, signer)
    if window.ok:
        progress(
            "[时段] 服务端当前判定: "
            + ("在允许时段内" if window.allowed else "不在允许时段")
            + ("（历史记录按记录时间校验）" if explicit_end else "")
        )
        if not explicit_end and not window.allowed:
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

    # 先把记录按“从当前时刻开始”生成，再整体平移，使结束时间落在指定
    # 的历史时刻或服务端当前时间之前。v6.1/savesports 不要求 beginTime
    # 晚于 sportId 的领取时刻，所以整条轨迹可以完全位于过去。
    target_end_ms = (
        options.end_ms
        if options.end_ms is not None
        else issued_at_ms - BACKDATE_MARGIN_MS
    )
    if target_end_ms > issued_at_ms:
        raise PrepareAbort(
            FlowReport(
                UploadResult(
                    success=False,
                    raw_response=(
                        "结束时刻 "
                        + datetime.fromtimestamp(
                            target_end_ms / 1000.0, tz=SERVER_TZ
                        ).strftime("%Y-%m-%d %H:%M:%S")
                        + " 晚于服务端当前时间；当前流程只提交已经结束的记录"
                    ),
                ),
                server_sport_id=sport_id,
                sport_id_issued_at_ms=issued_at_ms,
            )
        )

    record = generate_route_record(
        geometry,
        start_ms=issued_at_ms,
        sport_id=sport_id,
        xh=auth.xh,
        name=auth.name,
        target_distance_m=options.target_distance_m,
        seed=options.seed,
        sport_type=options.sport_type,
        route_id=options.route_id,
        route_name=options.route_name,
    )
    _shift_record(record, target_end_ms - record.end_date_long)
    progress(
        f"[生成] {route_summary(record)}"
        f"（{'指定结束时刻' if explicit_end else '已回溯'}）"
    )

    # 服务端时段接口可用且是“当前记录”时以它为准；历史记录和接口不可用
    # 的情况退回本地 6–23 点判断。
    if (explicit_end or not window.ok) and not _check_window(record, progress):
        raise PrepareAbort(
            FlowReport(
                UploadResult(
                    success=False,
                    raw_response="记录不在官方允许的运动时段（06:00–23:00）内",
                ),
                record=record,
                server_sport_id=sport_id,
                sport_id_issued_at_ms=issued_at_ms,
            )
        )
    return sport_id, issued_at_ms, record, issued_at_ms, offset_ms


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
) -> FlowReport:
    """提交前校验 → payload → savesports → sensorZipFile。"""
    try:
        assert_record_consistent(record)
    except RecordConsistencyError as exc:
        return FlowReport(
            UploadResult(success=False, raw_response=f"本地一致性校验失败: {exc}"),
            record=record,
            server_sport_id=sport_id,
            sport_id_issued_at_ms=issued_at_ms,
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
                notes=notes,
            )

    # 构造并加密 payload
    payload_json = build_json_string(record, auth)
    encrypted = encrypt_for_upload(payload_json, auth.aes_key, auth.aes_encoding)
    progress(f"[构造] payload {len(payload_json)} 字符，密文 {len(encrypted)} 字符")

    # savesports（v6.1：不要求 beginTime 晚于 sportId 创建时刻）
    progress("[上传] POST /api/l/v6.1/savesports ...")
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
        server_sport_id=sport_id,
        sport_id_issued_at_ms=issued_at_ms,
        notes=notes,
    )


def _run_network_flow(
    auth: AuthConfig,
    geometry,
    options: UploadOptions,
    signer: RequestSigner,
    progress: Progress,
) -> FlowReport:
    """一次性流程：领取 sportId → 生成回溯记录 → 立即提交。"""
    notes: list[str] = []
    try:
        sport_id, issued_at_ms, record, _server_time_ms, _offset_ms = (
            _claim_and_generate(
                auth, geometry, options, signer, progress, notes
            )
        )
    except PrepareAbort as abort:
        return abort.report

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
    """领取 sportId、生成回溯记录并落盘，不提交（第一阶段）。

    与 `upload_route` 的区别只有「不交」：记录在生成时已整体回溯到当前
    时间之前，`submit_session` 可以在任意时刻发送，不必再等满时长。
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
            sport_id, issued_at_ms, record, _server_ms, _offset_ms = (
                _claim_and_generate(
                    auth, geometry, options, signer, progress, notes
                )
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
    progress(f"[会话] 已写入 {path}")
    progress(
        f"[会话] 记录 {record.start_date} → {record.end_date}，可随时提交："
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
    output_payload: Optional[str] = None,
    progress: Optional[Progress] = None,
) -> FlowReport:
    """第二阶段：提交已落盘的记录（记录已是回溯时间，无需等待）。"""
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
