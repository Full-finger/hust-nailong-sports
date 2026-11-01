#!/usr/bin/env python3
"""hustrun QQ 机器人（easybot）：多用户私聊登录、状态、日志与上传。

* 只处理单聊（C2C）消息；每个用户有独立的 openid 白名单。
* 每个用户独立目录 `<USER_ROOT>/<openid>/`：自己的 config.yaml、
  HEADER.xml、USERINFO.xml、cas.json、路线文件。互不共用凭证。
* 邀请码由 owner 用「邀请 [备注]」生成，24 小时有效、单次；朋友私聊
  「绑定 <邀请码>」后自动建目录。
* `登录` 走 CAS：验证码图片通过 QQ 发出，用户回复验证码；首次问学号、
  密码、姓名，成功后只写到自己的 cas.json（600）。
* `上传` 需要二次确认，直接调用 `python -m hustrun upload`，每个用户
  用自己的配置和路线。
* AppSecret / CAS 密码 / TOKEN / SECRET 不写日志。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from easybot import Bot, Model

# 默认目录布局就是这个仓库本身：ROOT=仓库根，用户目录在 ROOT/users，
# 日志在 ROOT/logs。要换成 /opt/hustrun 之类的部署布局时用
# HUSTRUN_ROOT / HUSTRUN_APP_DIR / HUSTRUN_QQBOT_DIR / HUSTRUN_USER_ROOT /
# HUSTRUN_LOG_DIR / HUSTRUN_PYTHON 覆盖。
QQBOT_DIR = Path(
    os.environ.get("HUSTRUN_QQBOT_DIR", Path(__file__).resolve().parent)
)
ROOT = Path(os.environ.get("HUSTRUN_ROOT", QQBOT_DIR.parent))
APP_DIR = Path(os.environ.get("HUSTRUN_APP_DIR", ROOT))
ENV_FILE = QQBOT_DIR / "bot.env"
USERS_FILE = QQBOT_DIR / "users.json"
USER_ROOT = Path(os.environ.get("HUSTRUN_USER_ROOT", ROOT / "users"))
LOG_ROOT = Path(os.environ.get("HUSTRUN_LOG_DIR", ROOT / "logs"))
LOG_DIR = LOG_ROOT / "users"
UPLOAD_LOG = LOG_ROOT / "upload.log"
UPLOAD_LOCK = LOG_ROOT / "upload.lock"
PYTHON = os.environ.get("HUSTRUN_PYTHON", sys.executable)
ASK_TIMEOUT = 180
INVITE_TTL = 24 * 3600
ROUTE_POOL = ("route_track.json", "route_hand.json", "route_stadium.json")
UPLOAD_BASE = [
    "/usr/bin/flock",
    "-n",
    str(UPLOAD_LOCK),
    PYTHON,
    "-m",
    "hustrun",
    "upload",
]

# 允许 `import hustrun`
sys.path.insert(0, str(APP_DIR))


def load_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    """原子写入，权限 600（users.json 里有邀请码，cas.json 里有密码）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)


def write_file(path: Path, text: str, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)


CFG = load_env(ENV_FILE)
APP_ID = CFG.get("EASYBOT_APP_ID", "").strip()
APP_SECRET = CFG.get("EASYBOT_APP_SECRET", "").strip()
BIND_CODE = CFG.get("BIND_CODE", "").strip()
OWNER_QQ = CFG.get("OWNER_QQ", "").strip()
IS_PRIVATE = CFG.get("IS_PRIVATE", "1").strip() in {"1", "true", "yes", "on"}
IS_SANDBOX = CFG.get("IS_SANDBOX", "0").strip() in {"1", "true", "yes", "on"}
SERVER_URL = CFG.get("SERVER_URL", "").strip()

if not APP_ID or not APP_SECRET:
    raise SystemExit(
        f"缺少 EASYBOT_APP_ID / EASYBOT_APP_SECRET，编辑 {ENV_FILE}"
    )

USERS = load_json(USERS_FILE, {"version": 2, "owner": "", "users": {}, "invites": {}})

bot = Bot(
    app_id=APP_ID,
    app_secret=APP_SECRET,
    is_private=IS_PRIVATE,
    is_sandbox=IS_SANDBOX,
)
log = bot.logger.with_module("hustrun")


CONFIG_TEMPLATE = """\
paths:
  header_xml: "{home}/HEADER.xml"
  userinfo_xml: "{home}/USERINFO.xml"

auth:
  server_url: "https://petzjk.hust.edu.cn/cgapp-server"

upload:
  aes_encoding: "base64"
  token_header: "cgAuthorization"
  skip_sensor: false

device:
  imei: "{imei}"
  user_agent: "hzkjapp/1.0.9(Linux;Android 15;OnePlus/TP1A.220905.001)"
  v1_seed: ""
  is_root: false
  phone_version: "PJE110,35,15|1.0.9"
"""

HEADER_TEMPLATE = """\
<?xml version='1.0' encoding='utf-8'?>
<map>
  <string name="TOKEN"></string>
  <string name="SECRET"></string>
  <string name="LOGIN_OUT_TIME"></string>
  <string name="SIGN"></string>
  <string name="WEB_VIEW_TIME"></string>
  <string name="CGAPP_TIME_KEY"></string>
</map>
"""


class LoginCancelled(Exception):
    """用户在 QQ 里取消了当前流程。"""


class Bridge:
    """把同步的 CAS 回调桥接到 asyncio 事件循环（每个用户一个）。"""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.msg: Model.C2CMessage | None = None
        self.openid: str = ""
        self.future: asyncio.Future | None = None
        self.busy = False

    def ask(self, prompt: str, *, image: bytes | None = None) -> str:
        if self.loop is None:
            raise RuntimeError("事件循环未就绪")
        future = asyncio.run_coroutine_threadsafe(self._ask(prompt, image), self.loop)
        return future.result(ASK_TIMEOUT + 30)

    async def _ask(self, prompt: str, image: bytes | None) -> str:
        msg = self.msg
        if msg is None:
            raise RuntimeError("没有可回复的 QQ 消息")
        # 先挂 future 再发消息，避免用户秒回时丢消息。
        self.future = self.loop.create_future()
        try:
            if image is not None:
                info = await bot.api.upload_media(
                    file_type=1, file_data=image, user_openid=self.openid
                )
                await msg.reply(prompt, media_file_info=info.file_info)
            else:
                await msg.reply(prompt)
            return await asyncio.wait_for(self.future, timeout=ASK_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("等待 QQ 回复超时") from exc
        finally:
            self.future = None


bridges: dict[str, Bridge] = {}
cas_lock = asyncio.Lock()
upload_state = {"running": False}
upload_confirms: dict[str, float] = {}


# ---------------------------------------------------------------------------
# 用户/目录
# ---------------------------------------------------------------------------


def normalize_users() -> None:
    USERS.setdefault("version", 2)
    USERS.setdefault("users", {})
    USERS.setdefault("invites", {})


def user_of(openid: str) -> dict | None:
    return USERS.get("users", {}).get(openid)


def is_allowed(openid: str) -> bool:
    return bool(openid) and openid in USERS.get("users", {})


def is_owner(openid: str) -> bool:
    user = user_of(openid)
    return bool(user) and (user.get("role") == "owner" or openid == USERS.get("owner"))


def label_of(openid: str) -> str:
    user = user_of(openid) or {}
    return user.get("label") or openid[:8]


def account_of(msg: Model.C2CMessage) -> str:
    author = msg.author
    value = getattr(author, "union_user_account", None) if author else None
    return (value or "").strip()


def openid_of(msg: Model.C2CMessage) -> str:
    author = msg.author
    if author is None:
        return ""
    return author.user_openid or author.id or ""


def bridge_for(openid: str) -> Bridge:
    bridge = bridges.get(openid)
    if bridge is None:
        bridge = Bridge()
        bridges[openid] = bridge
    return bridge


def pick_route() -> str:
    used = {Path(user.get("route", "")).name for user in USERS["users"].values()}
    for name in ROUTE_POOL:
        if name not in used:
            return name
    return ROUTE_POOL[len(USERS["users"]) % len(ROUTE_POOL)]


def create_profile(openid: str, label: str, daily: bool = True) -> dict:
    """建立独立目录；不写 users.json，由调用方保存。"""
    home = USER_ROOT / openid
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)

    route_dst = home / "route.json"
    if not route_dst.exists():
        # 已有的用户重新授权时保留原来的路线和 IMEI，避免设备身份跳变。
        route_name = pick_route()
        route_src = APP_DIR / "config" / "routes" / route_name
        shutil.copyfile(route_src, route_dst)
        os.chmod(route_dst, 0o640)

    config_path = home / "config.yaml"
    if not config_path.exists():
        write_file(
            config_path,
            CONFIG_TEMPLATE.format(home=str(home), imei=str(uuid.uuid4())),
            mode=0o640,
        )
    header_path = home / "HEADER.xml"
    if not header_path.exists():
        write_file(header_path, HEADER_TEMPLATE, mode=0o600)

    LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o750)
    return {
        "label": label[:40] or "朋友",
        "account": "",
        "role": "user",
        "home": str(home),
        "config": str(config_path),
        "route": str(route_dst),
        "log": str(LOG_DIR / f"{openid}.log"),
        "daily": bool(daily),
        "bound_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def migrate_users() -> None:
    """兼容旧版 users.json：给 owner 补 home/config/route/log，并搬家 cas.json。"""
    normalize_users()
    changed = False
    if USERS.get("version") != 2:
        USERS["version"] = 2
        changed = True

    owner = USERS.get("owner", "")
    if owner:
        record = USERS["users"].setdefault(owner, {})
        home = USER_ROOT / owner
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(home, 0o700)
        desired = {
            "home": str(home),
            "config": str(APP_DIR / "config.yaml"),
            "route": str(APP_DIR / "config" / "routes" / "route_track.json"),
            "log": str(LOG_DIR / f"{owner}.log"),
            "role": "owner",
            "daily": True,
        }
        for key, value in desired.items():
            if record.get(key) in (None, ""):
                record[key] = value
                changed = True
        record.setdefault("label", OWNER_QQ or "owner")
        record.setdefault("account", "")
        record.setdefault("bound_at", time.strftime("%Y-%m-%d %H:%M:%S"))

        old_cas = QQBOT_DIR / "cas.json"
        new_cas = home / "cas.json"
        if old_cas.is_file() and not new_cas.exists():
            shutil.move(str(old_cas), str(new_cas))
            os.chmod(new_cas, 0o600)
            changed = True

    LOG_DIR.mkdir(parents=True, exist_ok=True, mode=0o750)
    os.chmod(LOG_DIR, 0o750)
    if changed:
        save_json(USERS_FILE, USERS)


def resolve_user(arg: str) -> tuple[str | None, list[str]]:
    """按 openid / openid 前缀 / label 查找。返回 (openid, 候选列表)。"""
    users = USERS["users"]
    if arg in users:
        return arg, []
    exact = [oid for oid, user in users.items() if user.get("label") == arg]
    if len(exact) == 1:
        return exact[0], []
    prefix = [oid for oid in users if oid.startswith(arg)]
    if len(prefix) == 1:
        return prefix[0], []
    return None, exact or prefix


def add_or_update_user(openid: str, label: str, daily: bool = True) -> dict:
    existing = user_of(openid)
    if existing:
        existing["label"] = label[:40] or existing.get("label") or "朋友"
        return existing
    user = create_profile(openid, label, daily=daily)
    USERS["users"][openid] = user
    return user


# ---------------------------------------------------------------------------
# 凭证 / 业务
# ---------------------------------------------------------------------------


def cas_path(user: dict) -> Path:
    return Path(user["home"]) / "cas.json"


def log_path(user: dict) -> Path:
    return Path(user.get("log") or (LOG_DIR / "unknown.log"))


def read_userinfo(path: Path) -> dict[str, str]:
    try:
        tree = ET.parse(path)
    except (FileNotFoundError, ET.ParseError):
        return {}
    root = tree.getroot()
    return {
        elem.get("name", ""): elem.text or ""
        for elem in root.findall("string")
        if elem.get("name")
    }


def write_userinfo(path: Path, xh: str, name: str, roles: str | None = None) -> None:
    """写入 USERINFO.xml。项目只读 USERXH/USERNAME，USER_ROLES 保持原值便于排查。"""
    if roles is None:
        roles = read_userinfo(path).get("USER_ROLES") or "1"
    root = ET.Element("map")
    ET.SubElement(root, "string", {"name": "USERXH"}).text = xh
    ET.SubElement(root, "string", {"name": "USERNAME"}).text = name
    ET.SubElement(root, "string", {"name": "USER_ROLES"}).text = roles
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    os.chmod(path, 0o600)


def ensure_header_template(config_path: str) -> None:
    from hustrun.config import get_data_paths

    header = Path(get_data_paths(config_path)["header_xml"])
    header.parent.mkdir(parents=True, exist_ok=True)
    if not header.exists():
        write_file(header, HEADER_TEMPLATE, mode=0o600)


def _login_sync(openid: str, bridge: Bridge):
    """工作线程：用该用户自己的目录做 CAS 登录。"""
    from hustrun.cas_login import cas_login, update_header_xml
    from hustrun.config import get_data_paths, load_auth_config

    user = USERS["users"][openid]
    config_path = user["config"]
    auth = load_auth_config(config_path)
    saved = load_json(cas_path(user), {})
    username = (saved.get("username") or "").strip()
    password = saved.get("password") or ""

    def answer(prompt: str) -> str:
        value = bridge.ask(prompt).strip()
        if value in {"取消", "/取消", "cancel"}:
            raise LoginCancelled("用户取消")
        return value

    def answer_image(prompt: str, image: bytes) -> str:
        value = bridge.ask(prompt, image=image).strip()
        if value in {"取消", "/取消", "cancel"}:
            raise LoginCancelled("用户取消")
        return value

    if not username:
        username = answer("请回复你的学号（用于 CAS 登录，会保存在你自己的目录）")
    if not password:
        password = answer(
            "请回复 CAS 密码（只会保存在你自己的 cas.json，权限 600；回复“取消”终止）"
        )
    if not username or not password:
        raise LoginCancelled("没有提供学号或密码")

    paths = get_data_paths(config_path)
    existing = read_userinfo(Path(paths["userinfo_xml"]))
    name = (existing.get("USERNAME") or "").strip()
    if not name:
        response = answer(
            "请回复姓名（显示在运动记录里；回复“跳过”则留空，之后可用“姓名 <名字>”设置）"
        )
        if response not in {"跳过", "skip", "skip name"}:
            name = response[:40]

    def captcha_callback(image: bytes, _suffix: str) -> str:
        return answer_image(
            "请回复图片中的验证码（3 分钟内有效；回复“取消”终止）", image
        )

    def dynamic_code_callback(message: str) -> str:
        return answer(f"{message}\n请回复动态验证码（回复“取消”终止）")

    result = cas_login(
        server_url=SERVER_URL or auth.server_url,
        username=username,
        password=password,
        captcha_callback=captcha_callback,
        dynamic_code_callback=dynamic_code_callback,
    )
    if result.success:
        ensure_header_template(config_path)
        update_header_xml(paths["header_xml"], result, backup=True)
        write_userinfo(Path(paths["userinfo_xml"]), result.xh or username, name)
        save_json(cas_path(user), {"username": username, "password": password})
    return result


def _check_sync(config_path: str) -> dict:
    from hustrun.pipeline import check_session

    return check_session(config_path)


def _tail(path: Path, n: int = 30) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return "（还没有上传日志）"
    return "\n".join(lines[-n:]) or "（日志为空）"


def _format_check(report: dict) -> str:
    ok = report.get("ok")
    lines = [f"自检：{'✅ 正常' if ok else '❌ 失败'}"]
    message = report.get("message")
    if message:
        lines.append(f"信息：{message}")
    server_time = report.get("server_time")
    if server_time:
        lines.append(f"服务端时间(ms)：{server_time}")
    in_time = report.get("in_sport_time")
    if in_time is not None:
        lines.append(f"时段内：{in_time}")
    return "\n".join(lines)


def append_user_log(user: dict, text: str) -> None:
    path = log_path(user)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} manual =====\n")
        file.write(text)
        if not text.endswith("\n"):
            file.write("\n")
    os.chmod(path, 0o640)


async def _do_upload(msg: Model.C2CMessage, openid: str) -> None:
    if upload_state["running"]:
        await msg.reply("已有上传在进行，请稍后。")
        return
    user = USERS["users"][openid]
    config_path = user["config"]
    route_path = user["route"]

    upload_state["running"] = True
    log.info("用户 %s 开始手动上传", label_of(openid))
    try:
        try:
            report = await asyncio.to_thread(_check_sync, config_path)
        except Exception as exc:  # noqa: BLE001
            log.exception("上传前自检失败")
            await msg.reply(f"自检失败，未开始上传：{type(exc).__name__}: {exc}")
            return
        if not report.get("ok"):
            await msg.reply(
                "当前 TOKEN 不可用，先回复“登录”刷新凭证：\n"
                + _format_check(report)
            )
            return

        await msg.reply(
            "开始上传（约 8–9 分钟）。完成后回复“日志”查看结果；"
            "QQ 主动推送可能受限，所以不保证自动通知。"
        )
        proc = await asyncio.create_subprocess_exec(
            *UPLOAD_BASE,
            "-c",
            config_path,
            "-r",
            route_path,
            "-d",
            "2050",
            cwd=str(APP_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        text = out.decode("utf-8", "replace")
        append_user_log(user, text)
        log.info("用户 %s 手动上传结束 exit=%s", label_of(openid), proc.returncode)
        tail = text[-1500:]
        try:
            if proc.returncode != 0 and not text.strip():
                await msg.reply("上传没有启动：可能已有定时/手动任务在跑，稍后再试。")
            else:
                await msg.reply(f"上传进程结束（exit {proc.returncode}）：\n{tail}")
        except Exception:  # noqa: BLE001
            log.warning("上传结果主动推送失败（可能超出 QQ 被动回复窗口）")
    finally:
        upload_state["running"] = False


HELP_COMMON = (
    "登录 — 用你自己的学号/密码刷新凭证（验证码图片会发到这里）\n"
    "上传 — 立即上传一条 2.05 km 记录（需回复“确认”，约 8–9 分钟）\n"
    "状态 — 只读自检（TOKEN / 服务端时间 / 时段）\n"
    "日志 — 你自己最近一次上传日志\n"
    "我的信息 — 查看绑定信息\n"
    "姓名 <名字> — 设置运动记录里的姓名\n"
    "帮助 — 显示本说明\n"
    "流程中回复“取消”可中止。"
)

HELP_OWNER = (
    "\n管理员命令：\n"
    "邀请 [备注] — 生成 24 小时单次邀请码\n"
    "用户列表 — 查看所有用户\n"
    "添加用户 <openid> [备注] — 手动加人\n"
    "删除用户 <openid|备注> — 移除授权（不删数据）\n"
    "设置自动 <openid|备注> on|off — 开关每日 10:00 上传"
)


def help_text(openid: str) -> str:
    return "可用命令：\n" + HELP_COMMON + (HELP_OWNER if is_owner(openid) else "")


def describe_user(openid: str) -> str:
    user = USERS["users"][openid]
    role = "owner" if is_owner(openid) else "user"
    daily = "开" if user.get("daily", True) else "关"
    return (
        f"openid：{openid}\n"
        f"备注：{user.get('label', '')}\n"
        f"角色：{role}\n"
        f"每日 10:00：{daily}\n"
        f"目录：{user.get('home', '')}"
    )


# ---------------------------------------------------------------------------
# 私聊入口
# ---------------------------------------------------------------------------


@bot.on_c2c_message
async def on_c2c(msg: Model.C2CMessage) -> None:
    openid = openid_of(msg)
    account = account_of(msg)
    text = (msg.treated_msg or msg.content or "").strip()

    if not is_allowed(openid):
        if text.startswith("绑定"):
            parts = text.split(maxsplit=1)
            code = parts[1].strip() if len(parts) > 1 else ""
            invite = USERS["invites"].get(code)
            if (
                invite
                and float(invite.get("expires", 0)) >= time.time()
                and not invite.get("used")
            ):
                try:
                    user = create_profile(
                        openid,
                        invite.get("label") or "朋友",
                        daily=bool(invite.get("daily", True)),
                    )
                except Exception as exc:  # noqa: BLE001
                    log.exception("创建用户目录失败")
                    await msg.reply(f"绑定失败（创建目录出错）：{type(exc).__name__}: {exc}")
                    return
                user["account"] = account
                USERS["users"][openid] = user
                USERS["invites"].pop(code, None)
                save_json(USERS_FILE, USERS)
                log.info("邀请码绑定 openid=%s", openid)
                await msg.reply(
                    "✅ 绑定成功。回复“登录”，用你自己的学号和密码登录；"
                    "登录成功后每天 10:00 会自动上传，也可以手动发送“上传”。"
                )
            else:
                await msg.reply("邀请码无效、已过期或已被使用。请让管理员重新发送“邀请”。")
            return
        await msg.reply(
            f"未授权。openid：{openid}\n请把这段 openid 发给管理员，"
            "或在管理员生成邀请码后回复“绑定 <邀请码>”。"
        )
        return

    user = USERS["users"][openid]
    bridge = bridge_for(openid)
    bridge.loop = asyncio.get_running_loop()
    bridge.msg = msg
    bridge.openid = openid

    # 有流程在等这条回复：验证码 / 动态码 / 学号 / 密码 / 姓名
    if bridge.future is not None and not bridge.future.done():
        if text in {"取消", "/取消", "cancel"}:
            bridge.future.set_exception(LoginCancelled("用户取消"))
        else:
            bridge.future.set_result(text)
        return

    if text.startswith("绑定"):
        await msg.reply("你已经绑定过了。回复“帮助”查看命令。")
        return

    if text in {"帮助", "/help", "help", "菜单"}:
        await msg.reply(help_text(openid))
        return

    if text in {"我的信息", "信息", "/me"}:
        await msg.reply(describe_user(openid))
        return

    if text.startswith("姓名"):
        from hustrun.config import get_data_paths

        userinfo = Path(get_data_paths(user["config"])["userinfo_xml"])
        parts = text.split(maxsplit=1)
        if len(parts) == 1 or not parts[1].strip():
            current = read_userinfo(userinfo).get("USERNAME", "")
            await msg.reply(f"当前姓名：{current or '（空）'}\n发送“姓名 <名字>”修改。")
            return
        name = parts[1].strip()[:40]
        existing = read_userinfo(userinfo)
        xh = existing.get("USERXH", "")
        write_userinfo(userinfo, xh, name)
        await msg.reply(f"✅ 姓名已设置为：{name}")
        return

    if text in {"状态", "/status", "status"}:
        await msg.reply("正在向服务端自检…")
        try:
            report = await asyncio.to_thread(_check_sync, user["config"])
        except Exception as exc:  # noqa: BLE001
            log.exception("自检失败")
            await msg.reply(f"自检失败：{type(exc).__name__}: {exc}")
            return
        await msg.reply(_format_check(report))
        return

    if text in {"日志", "/log", "log"}:
        content = _tail(log_path(user), 30)
        if content == "（还没有上传日志）" and is_owner(openid):
            content = _tail(UPLOAD_LOG, 30)
        await msg.reply("最近上传日志：\n" + content[-1500:])
        return

    if text in {"登录", "/login", "login"}:
        if bridge.busy:
            await msg.reply("你已有登录流程在进行，请先回复验证码或发送“取消”。")
            return
        if cas_lock.locked():
            await msg.reply("另一个用户正在登录，请等对方完成后再试。")
            return
        bridge.busy = True
        log.info("用户 %s 开始 CAS 登录", label_of(openid))
        try:
            await msg.reply(
                "开始 CAS 登录，请按后续提示回复（第一次会问学号、密码、姓名）。"
            )
            async with cas_lock:
                result = await asyncio.to_thread(_login_sync, openid, bridge)
        except LoginCancelled:
            log.info("用户 %s 取消登录", label_of(openid))
            await msg.reply("已取消登录。")
        except Exception as exc:  # noqa: BLE001
            log.exception("CAS 登录异常")
            await msg.reply(f"登录失败：{type(exc).__name__}: {exc}")
        else:
            if result.success:
                log.info("用户 %s CAS 登录成功", label_of(openid))
                await msg.reply("✅ CAS 登录成功，凭证已更新。")
                try:
                    report = await asyncio.to_thread(_check_sync, user["config"])
                    await msg.reply(_format_check(report))
                except Exception:  # noqa: BLE001
                    log.exception("登录后自检失败")
            else:
                log.warning("用户 %s CAS 登录失败: %s", label_of(openid), result.error)
                await msg.reply(f"✗ CAS 登录失败：{result.error}")
        finally:
            bridge.busy = False
        return

    if text in {"上传", "/upload"}:
        upload_confirms[openid] = time.monotonic() + 60
        await msg.reply(
            "将立即上传一条 2.05 km 记录（约 8–9 分钟）。"
            "回复“确认”开始，回复“取消”放弃。"
        )
        return

    if text in {"确认", "确认上传"}:
        if time.monotonic() <= upload_confirms.get(openid, 0.0):
            upload_confirms[openid] = 0.0
            await _do_upload(msg, openid)
        else:
            await msg.reply("确认已过期，请重新发送“上传”。")
        return

    if text in {"取消", "/取消", "cancel"}:
        await msg.reply("当前没有进行中的流程。")
        return

    # ---- owner 管理命令 ----
    if text == "用户列表":
        if not is_owner(openid):
            await msg.reply("只有管理员可以使用该命令。")
            return
        lines = []
        for index, (oid, item) in enumerate(USERS["users"].items(), 1):
            role = "owner" if oid == USERS.get("owner") else "user"
            daily = "✓" if item.get("daily", True) else "✗"
            lines.append(f"{index}. {item.get('label') or '（无备注）'} | {role} | 每日{daily} | {oid}")
        await msg.reply("用户列表：\n" + "\n".join(lines))
        return

    if text.startswith("邀请"):
        if not is_owner(openid):
            await msg.reply("只有管理员可以生成邀请码。")
            return
        parts = text.split(maxsplit=1)
        label = (parts[1].strip() if len(parts) > 1 else "")[:40] or "朋友"
        code = secrets.token_hex(4)
        while code in USERS["invites"]:
            code = secrets.token_hex(4)
        USERS["invites"][code] = {
            "label": label,
            "daily": True,
            "expires": time.time() + INVITE_TTL,
            "created_by": openid,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_json(USERS_FILE, USERS)
        log.info("管理员生成邀请码 label=%s", label)
        await msg.reply(
            f"邀请码：{code}\n24 小时内有效、单次使用。\n"
            f"让朋友私聊我发送：绑定 {code}\n"
            "对方绑定后用自己的学号/密码登录；默认每天 10:00 自动上传。"
        )
        return

    if text.startswith("添加用户"):
        if not is_owner(openid):
            await msg.reply("只有管理员可以添加用户。")
            return
        parts = text.split(maxsplit=2)
        if len(parts) < 2 or len(parts[1].strip()) < 8:
            await msg.reply("用法：添加用户 <openid> [备注]")
            return
        target = parts[1].strip()
        label = (parts[2].strip() if len(parts) > 2 else "")[:40] or "朋友"
        try:
            user = add_or_update_user(target, label, daily=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("添加用户失败")
            await msg.reply(f"添加失败（创建目录出错）：{type(exc).__name__}: {exc}")
            return
        save_json(USERS_FILE, USERS)
        log.info("管理员添加用户 label=%s", label)
        await msg.reply(
            f"✅ 已添加：{user.get('label')}\n"
            f"目录：{user.get('home')}\n"
            "让对方私聊机器人发送“登录”，用自己的学号/密码完成首次登录。"
        )
        return

    if text.startswith("删除用户"):
        if not is_owner(openid):
            await msg.reply("只有管理员可以删除用户。")
            return
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await msg.reply("用法：删除用户 <openid|备注>")
            return
        target, candidates = resolve_user(parts[1].strip())
        if target is None:
            await msg.reply(
                "没有唯一匹配的用户。候选：" + ("、".join(candidates) if candidates else "无")
            )
            return
        if target == USERS.get("owner"):
            await msg.reply("不能删除 owner。")
            return
        user = USERS["users"].pop(target, None)
        save_json(USERS_FILE, USERS)
        log.info("管理员删除用户 label=%s", (user or {}).get("label"))
        await msg.reply(
            f"已移除授权：{(user or {}).get('label') or target}\n"
            "数据目录保留；如需重新授权，再生成邀请码或手动添加。"
        )
        return

    if text.startswith("设置自动"):
        if not is_owner(openid):
            await msg.reply("只有管理员可以修改自动上传。")
            return
        parts = text.split()
        if len(parts) < 3:
            await msg.reply("用法：设置自动 <openid|备注> on|off")
            return
        target_arg = " ".join(parts[1:-1])
        mode = parts[-1].lower()
        if mode not in {"on", "off", "开", "关"}:
            await msg.reply("用法：设置自动 <openid|备注> on|off")
            return
        target, candidates = resolve_user(target_arg)
        if target is None:
            await msg.reply(
                "没有唯一匹配的用户。候选：" + ("、".join(candidates) if candidates else "无")
            )
            return
        USERS["users"][target]["daily"] = mode in {"on", "开"}
        save_json(USERS_FILE, USERS)
        state = "开启" if USERS["users"][target]["daily"] else "关闭"
        await msg.reply(f"已{state} {label_of(target)} 的每日 10:00 上传。")
        return

    await msg.reply("未知命令。回复“帮助”查看可用命令。")


if __name__ == "__main__":
    try:
        migrate_users()
    except Exception as exc:  # noqa: BLE001
        log.exception("users.json 迁移失败")
        raise SystemExit(f"users.json 迁移失败: {exc}") from exc
    log.info("hustrun QQ 机器人启动")
    bot.start()
