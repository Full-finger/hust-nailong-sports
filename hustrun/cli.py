"""命令行入口：login / check / generate / upload。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import click

from .config import get_data_paths, load_auth_config
from .pipeline import (
    UploadOptions,
    check_session,
    parse_end_time,
    upload_route,
)


def _configure_console() -> None:
    """Windows 旧代码页无法显示符号时用替换字符，避免 CLI 崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


_configure_console()


def _prompt_captcha(image: bytes, suffix: str) -> str:
    path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix="hustrun_captcha_", suffix=suffix, delete=False
        ) as file:
            file.write(image)
            path = file.name
        click.echo(f"\n[登录] 图形验证码: {path}")
        startfile = getattr(os, "startfile", None)
        if startfile is not None:
            try:
                startfile(path)
            except OSError:
                pass
        return click.prompt("验证码", type=str).strip()
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def _prompt_dynamic_code(message: str) -> str:
    click.echo(f"\n[登录] {message}")
    return click.prompt("企业微信动态验证码", type=str).strip()


_SP_XML_HEADER = '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<map>\n</map>\n'


def _ensure_sp_xml(path: Path) -> None:
    """首次运行时创建空的 SharedPreferences XML，权限尽量收成 600。"""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_SP_XML_HEADER, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _write_userinfo(path: Path, xh: str, name: str) -> None:
    """写入 USERINFO.xml，保留已有的 USER_ROLES。"""
    roles = "1"
    if path.exists():
        try:
            for elem in ET.parse(path).getroot().findall("string"):
                if elem.get("name") == "USER_ROLES" and elem.text:
                    roles = elem.text
        except ET.ParseError:
            pass
    root = ET.Element("map")
    ET.SubElement(root, "string", {"name": "USERXH"}).text = xh
    ET.SubElement(root, "string", {"name": "USERNAME"}).text = name
    ET.SubElement(root, "string", {"name": "USER_ROLES"}).text = roles
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _emit_result(result) -> int:
    """打印上传结论并给出退出码：0 有效 / 2 被判无效 / 1 请求失败。"""
    if result.success and result.is_valid == "1":
        click.echo(
            f"✓ 提交成功 sportId={result.sport_id} "
            f"checkStatus={result.check_status} tip={result.tip}"
        )
        return 0
    if result.success:
        click.echo(
            f"✗ 服务端判定无效 isValid={result.is_valid} "
            f"reason={result.in_valid_reason} tip={result.tip}"
        )
        return 2
    click.echo(f"✗ 上传失败: {result.raw_response}", err=True)
    return 1


@click.group()
@click.version_option("1.0.0", prog_name="hustrun")
def main() -> None:
    """HUST 校园跑 · 桌面一键上传（无需手机/模拟器）。"""


@main.command()
@click.option("--config", "-c", default="config.yaml")
@click.option("--username", "-u", default=None, help="学号（默认读 USERINFO.xml）")
@click.option("--password", "-p", default=None, help="密码（建议省略，交互输入）")
@click.option("--debug", is_flag=True, help="显示 CAS 重定向链")
def login(config: str, username: str | None, password: str | None, debug: bool) -> None:
    """CAS 登录并更新本地凭证文件。"""
    from getpass import getpass

    from .cas_login import cas_login, update_header_xml

    auth = load_auth_config(config)
    paths = get_data_paths(config)
    header_path = Path(paths["header_xml"])
    userinfo_path = Path(paths["userinfo_xml"])

    # 首次运行时本地还没有凭证文件：先放一个空模板，登录成功后由结果填写。
    had_credentials = header_path.exists()
    _ensure_sp_xml(header_path)

    username = username or auth.xh or click.prompt("用户名/学号", type=str)
    password = password or getpass("密码: ")
    if not username or not password:
        raise click.ClickException("用户名和密码不能为空")

    click.echo(f"[登录] 服务端: {auth.server_url}")
    result = cas_login(
        server_url=auth.server_url,
        username=username,
        password=password,
        captcha_callback=_prompt_captcha,
        dynamic_code_callback=_prompt_dynamic_code,
    )
    if not result.success:
        click.echo(f"✗ CAS 登录失败: {result.error}", err=True)
        if "403" in result.error:
            click.echo(
                "  说明: 学校 WAF 拦截了本次请求（常见于短时间重复登录）。"
                "请等待 10–30 分钟，或换一个网络（如手机热点）后重试；"
                "不要连续重试，否则封禁时间会延长。",
                err=True,
            )
        if debug:
            for item in result.trace:
                click.echo(f"  {item}", err=True)
        raise SystemExit(1)

    click.echo("✓ 登录成功（TOKEN/SECRET 不在终端显示）")
    update_header_xml(str(header_path), result, backup=had_credentials)
    click.echo(f"✓ 凭证已更新: {header_path}")

    name = result.name
    if not name and sys.stdin.isatty():
        name = click.prompt(
            "姓名（显示在运动记录里，可留空）", default="", show_default=False
        ).strip()
    _write_userinfo(userinfo_path, result.xh or username, name)
    click.echo(f"✓ 用户信息已更新: {userinfo_path}")


@main.command()
@click.option("--config", "-c", default="config.yaml")
def check(config: str) -> None:
    """签名器 + 会话自检（只读，不会改动服务端）。"""
    auth = load_auth_config(config)
    click.echo(f"[配置] 学号: {auth.xh}  服务端: {auth.server_url}")
    click.echo(
        f"[配置] TOKEN {'已配置' if auth.token else '缺失'} "
        f"({len(auth.token)} 字符), "
        f"SECRET {'已配置' if auth.secret else '缺失'}, "
        f"过期={auth.is_token_expired}"
    )
    try:
        report = check_session(config, progress=click.echo)
    except Exception as exc:  # noqa: BLE001 - CLI 边界
        click.echo(f"✗ 自检失败: {exc}", err=True)
        raise SystemExit(1)
    click.echo(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    raise SystemExit(0 if report.get("ok") else 1)


@main.command()
@click.option("--config", "-c", default="config.yaml")
@click.option("--route", "-r", "route_path", default="config/routes/route_track.json", show_default=True, help="路径 JSON")
@click.option("--distance", "-d", type=float, default=2150.0, help="目标距离（米）")
@click.option("--seed", type=int, default=None)
@click.option("--out", "output", default=None, help="把明文 payload 写到文件")
def generate(
    config: str,
    route_path: str,
    distance: float,
    seed: int | None,
    output: str | None,
) -> None:
    """只生成 payload（不联网、不签名）。"""
    options = UploadOptions(
        route_path=route_path,
        target_distance_m=distance,
        seed=seed,
        dry_run=True,
        output_payload=output,
    )
    report = upload_route(config, options, progress=click.echo)
    if report.record is None:
        click.echo(f"✗ {report.result.raw_response}", err=True)
        raise SystemExit(1)
    click.echo("✓ 生成完成（dry-run）")


@main.command()
@click.option("--config", "-c", default="config.yaml")
@click.option("--route", "-r", "route_path", default="config/routes/route_track.json", show_default=True, help="路径 JSON")
@click.option("--distance", "-d", type=float, default=2150.0, help="目标距离（米）")
@click.option("--seed", type=int, default=None)
@click.option(
    "--end-time",
    default=None,
    help='记录结束时刻（北京时间 HH:MM 或 "YYYY-MM-DD HH:MM:SS"，默认现在）',
)
@click.option("--skip-sensor", is_flag=True, help="不上传传感器数据")
@click.option("--out", "output", default=None, help="把明文 payload 写到文件")
def upload(
    config: str,
    route_path: str,
    distance: float,
    seed: int | None,
    end_time: str | None,
    skip_sensor: bool,
    output: str | None,
) -> None:
    """申请 sportId → 生成记录 → 立即提交（可用 --end-time 补传历史记录）。"""
    try:
        end_ms = parse_end_time(end_time) if end_time else None
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    options = UploadOptions(
        route_path=route_path,
        target_distance_m=distance,
        seed=seed,
        end_ms=end_ms,
        skip_sensor=skip_sensor,
        output_payload=output,
    )
    report = upload_route(config, options, progress=click.echo)
    result = report.result
    if report.record is not None:
        click.echo(
            f"[记录] {report.record.start_date} → {report.record.end_date} "
            f"{report.record.dllc} km"
        )
    raise SystemExit(_emit_result(result))


@main.command()
@click.option("--config", "-c", default="config.yaml")
@click.option("--route", "-r", "route_path", default="config/routes/route_track.json", show_default=True, help="路径 JSON")
@click.option("--distance", "-d", type=float, default=2150.0, help="目标距离（米）")
@click.option("--seed", type=int, default=None)
@click.option(
    "--end-time",
    default=None,
    help='记录结束时刻（北京时间 HH:MM 或 "YYYY-MM-DD HH:MM:SS"，默认现在）',
)
@click.option("--out", "output", default=None, help="把明文 payload 写到文件")
def prepare(
    config: str,
    route_path: str,
    distance: float,
    seed: int | None,
    end_time: str | None,
    output: str | None,
) -> None:
    """第一步：领取 sportId 并生成记录落盘（不提交）。"""
    from .pipeline import prepare_route

    try:
        end_ms = parse_end_time(end_time) if end_time else None
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    options = UploadOptions(
        route_path=route_path,
        target_distance_m=distance,
        seed=seed,
        end_ms=end_ms,
        output_payload=output,
    )
    report = prepare_route(config, options, progress=click.echo)
    if not report.session_path:
        click.echo(f"✗ 准备失败: {report.result.raw_response}", err=True)
        raise SystemExit(1)
    click.echo(f"✓ 已准备 session={report.session_path}")
    raise SystemExit(0)


@main.command()
@click.option("--config", "-c", default="config.yaml")
@click.option("--session", "session_path", default=None, help="要提交的会话文件（默认取最新）")
@click.option("--skip-sensor", is_flag=True, help="不上传传感器数据")
@click.option("--out", "output", default=None, help="把明文 payload 写到文件")
def submit(
    config: str,
    session_path: str | None,
    skip_sensor: bool,
    output: str | None,
) -> None:
    """第二步：提交 prepare 落盘的 session。"""
    from .pipeline import submit_session

    report = submit_session(
        config,
        session_path,
        skip_sensor=skip_sensor,
        output_payload=output,
        progress=click.echo,
    )
    if report.record is not None:
        click.echo(
            f"[记录] {report.record.start_date} → {report.record.end_date} "
            f"{report.record.dllc} km"
        )
    raise SystemExit(_emit_result(report.result))


@main.command(name="routes")
@click.option("--config", "-c", default="config.yaml")
def list_routes(config: str) -> None:
    """列出 config/routes 下的路径及长度。"""
    from .generator import load_route

    root = Path(__file__).resolve().parent.parent / "config" / "routes"
    for path in sorted(root.glob("*.json")):
        try:
            geometry = load_route(path)
            click.echo(f"{path.name:<24} {geometry.length_m:>8.0f} m")
        except Exception as exc:  # noqa: BLE001
            click.echo(f"{path.name:<24} 读取失败: {exc}")


@main.command()
@click.option("--config", "-c", default="config.yaml")
@click.option("--valid", "valid_filter", default=None, help="按有效性过滤: 1 有效，-1 无效")
@click.option("--limit", type=int, default=None, help="只显示最近 N 条")
def records(config: str, valid_filter: str | None, limit: int | None) -> None:
    """查看服务端已有的运动记录（只读）。"""
    from .pipeline import fetch_sport_records

    report = fetch_sport_records(config)
    if report["message"]:
        click.echo(f"✗ 查询失败: {report['message']}", err=True)
        raise SystemExit(1)

    recs = sorted(report["records"], key=lambda r: r.get("date") or "", reverse=True)
    if valid_filter:
        recs = [r for r in recs if str(r.get("isValid")) == valid_filter]

    def fmt_duration(rec: dict) -> str:
        value = rec.get("duration")
        if value not in (None, ""):
            try:
                sec = int(float(value))
                return f"{sec // 60}:{sec % 60:02d}"
            except ValueError:
                return str(value)
        try:
            sec = int((int(rec["endTime"]) - int(rec["beginTime"])) / 1000)
            return f"{sec // 60}:{sec % 60:02d}"
        except (KeyError, TypeError, ValueError):
            return "-"

    def fmt_pace(rec: dict) -> str:
        parts = rec.get("paceStr") or []
        return " ".join(p.get("t", "?") for p in parts) if parts else "-"

    valid_n = sum(1 for r in recs if str(r.get("isValid")) == "1")
    total_km = sum(
        float(r.get("odometer") or 0) for r in recs if str(r.get("isValid")) == "1"
    )
    click.echo(f"共 {len(recs)} 条记录（有效 {valid_n} 条 / 有效里程 {total_km:.2f} km）\n")
    header = (
        f"{'日期':<20}{'距离km':>6}{'时长':>8}{'配速':>16}"
        f"  {'状态':<10}{'审核':<6}{'sportId':<13}备注"
    )
    click.echo(header)
    click.echo("-" * 84)
    shown = recs if limit is None else recs[:limit]
    valid_text = {"1": "有效", "-1": "无效", "0": "未判"}
    check_text = {"1": "过审", "-2": "待审", "0": "未审"}
    for rec in shown:
        status = valid_text.get(str(rec.get("isValid")), str(rec.get("isValid")))
        if rec.get("isValidReason"):
            status += f"({rec['isValidReason']})"
        check = check_text.get(str(rec.get("checkStatus")), str(rec.get("checkStatus")))
        if rec.get("checkResult"):
            check += f"({rec['checkResult']})"
        click.echo(
            f"{rec.get('date', ''):<20}{rec.get('odometer', '-'):>6}"
            f"{fmt_duration(rec):>8}{fmt_pace(rec):>16}  {status:<10}{check:<6}"
            f"{str(rec.get('sportId', ''))[:12]:<13}{rec.get('tip') or ''}"
        )


@main.command()
@click.option("--config", "-c", default="config.yaml")
@click.option("--sport-id", "sport_id", default=None, help="要渲染的记录 sportId（默认取最新一条）")
@click.option("--out", "-o", "output", default=None, help="输出 PNG 路径（默认 output/card_<日期>.png）")
def card(config: str, sport_id: str | None, output: str | None) -> None:
    """把一条运动记录渲染成分享卡片（PNG）。"""
    from .card import render_card
    from .pipeline import fetch_sport_records

    report = fetch_sport_records(config)
    if report["message"]:
        click.echo(f"✗ 查询失败: {report['message']}", err=True)
        raise SystemExit(1)

    recs = sorted(report["records"], key=lambda r: r.get("date") or "", reverse=True)
    if not recs:
        click.echo("✗ 服务端没有运动记录", err=True)
        raise SystemExit(1)

    record = None
    if sport_id:
        for rec in recs:
            if str(rec.get("sportId", "")).startswith(sport_id):
                record = rec
                break
        if record is None:
            click.echo(f"✗ 找不到 sportId 前缀为 {sport_id} 的记录", err=True)
            raise SystemExit(1)
    else:
        record = recs[0]

    total_km = sum(
        float(r.get("odometer") or 0) for r in recs if str(r.get("isValid")) == "1"
    )
    default_name = f"card_{(record.get('date') or 'record')[:10].replace('-', '')}.png"
    out_path = Path("output") / default_name if output is None else Path(output)
    saved = render_card(record, total_km, out_path)
    click.echo(f"✓ 已生成 {saved}  ({record.get('date', '')} {record.get('odometer', '-')}km)")


if __name__ == "__main__":
    main()
