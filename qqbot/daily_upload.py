#!/usr/bin/env python3
"""每日 10:00 串行上传：读取 users.json，为每个 daily=true 的用户跑一次上传。

systemd 用 `flock -n $HUSTRUN_LOG_DIR/upload.lock` 包住整个脚本，保证与
机器人里的手动上传、上一次定时任务互斥。每个用户用自己的 config.yaml 和
路线文件，日志追加到 `$HUSTRUN_LOG_DIR/users/<openid>.log`。

退出码只反映真正的失败：还没登录（没有可用 TOKEN）的用户算“跳过”，不算
失败，避免一个绑定了但没登录的账号让定时任务天天报 failed。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# 默认目录布局就是这个仓库本身：ROOT=仓库根，用户目录在 ROOT/users，
# 日志在 ROOT/logs。换成服务器布局时用 HUSTRUN_ROOT / HUSTRUN_APP_DIR /
# HUSTRUN_USERS_FILE / HUSTRUN_LOG_DIR / HUSTRUN_PYTHON 覆盖。
QQBOT_DIR = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("HUSTRUN_ROOT", QQBOT_DIR.parent))
APP_DIR = Path(os.environ.get("HUSTRUN_APP_DIR", ROOT))
USERS_FILE = Path(os.environ.get("HUSTRUN_USERS_FILE", QQBOT_DIR / "users.json"))
LOG_ROOT = Path(os.environ.get("HUSTRUN_LOG_DIR", ROOT / "logs"))
LOG_DIR = LOG_ROOT / "users"
PYTHON = os.environ.get("HUSTRUN_PYTHON", sys.executable)
DISTANCE_M = "2050"
PER_USER_TIMEOUT = 1800

OK, SKIPPED, FAILED = "ok", "skip", "fail"

sys.path.insert(0, str(APP_DIR))


def load_users() -> dict:
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        print(f"[错误] users.json 不是合法 JSON: {exc}", flush=True)
        raise SystemExit(2) from exc


def append_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} daily =====\n")
        file.write(text)
        if not text.endswith("\n"):
            file.write("\n")
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass


def check_ok(config_path: str) -> tuple[bool, str]:
    from hustrun.pipeline import check_session

    report = check_session(config_path)
    if report.get("ok"):
        return True, "ok"
    return False, str(report.get("message") or report.get("api_code") or report)


def run_user(openid: str, user: dict) -> str:
    label = user.get("label") or openid[:8]
    config_path = user.get("config") or str(APP_DIR / "config.yaml")
    route_path = user.get("route") or str(APP_DIR / "config/routes/route_track.json")
    log_path = Path(user.get("log") or (LOG_DIR / f"{openid}.log"))

    if not Path(config_path).is_file():
        print(f"[跳过] {label}: 缺少配置 {config_path}", flush=True)
        return SKIPPED
    if not Path(route_path).is_file():
        print(f"[跳过] {label}: 缺少路线 {route_path}", flush=True)
        return SKIPPED

    try:
        ok, detail = check_ok(config_path)
    except Exception as exc:  # noqa: BLE001 - 单个用户失败不影响其他人
        message = f"[异常] {label}: 自检失败 {type(exc).__name__}: {exc}"
        print(message, flush=True)
        append_log(log_path, message + "\n")
        return FAILED
    if not ok:
        message = f"[跳过] {label}: TOKEN 不可用（{detail}），请私聊机器人“登录”"
        print(message, flush=True)
        append_log(log_path, message + "\n")
        return SKIPPED

    print(f"[开始] {label} ({openid})", flush=True)
    cmd = [
        PYTHON,
        "-m",
        "hustrun",
        "upload",
        "-c",
        config_path,
        "-r",
        route_path,
        "-d",
        DISTANCE_M,
    ]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(APP_DIR),
            capture_output=True,
            timeout=PER_USER_TIMEOUT,
            check=False,
        )
        text = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode(
            "utf-8", "replace"
        )
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        text = f"上传超时（{PER_USER_TIMEOUT}s），已终止。\n"
        if exc.stdout:
            text += exc.stdout.decode("utf-8", "replace")
        code = -1
    except Exception as exc:  # noqa: BLE001
        text = f"启动上传失败：{type(exc).__name__}: {exc}\n"
        code = -1

    elapsed = int(time.monotonic() - started)
    append_log(log_path, text)
    status = "完成" if code == 0 else f"失败 exit={code}"
    print(f"[{status}] {label}，用时 {elapsed}s", flush=True)
    print(text.rstrip()[-2000:], flush=True)
    return OK if code == 0 else FAILED


def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]
    data = load_users()
    users = data.get("users", {})
    targets = [
        (openid, user)
        for openid, user in users.items()
        if bool(user.get("daily", True))
    ]
    if not targets:
        print("没有开启每日上传的用户。", flush=True)
        return 0

    if dry_run:
        for openid, user in targets:
            print(
                f"[dry-run] {user.get('label') or openid[:8]} | {openid} | "
                f"{user.get('config')} | {user.get('route')}",
                flush=True,
            )
        return 0

    failed = 0
    skipped = 0
    for openid, user in targets:
        status = run_user(openid, user)
        if status == FAILED:
            failed += 1
        elif status == SKIPPED:
            skipped += 1
        time.sleep(2)

    summary = f"结束：{len(targets) - failed - skipped}/{len(targets)} 成功"
    if skipped:
        summary += f"，{skipped} 个跳过（未登录或缺配置）"
    if failed:
        summary += f"，{failed} 个失败"
    print(summary + "。", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
