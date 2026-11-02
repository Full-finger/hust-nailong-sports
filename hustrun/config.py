"""
配置管理 — 从 XML 文件和 YAML 配置文件加载鉴权和数据源配置。

数据来源：
- HEADER.xml: TOKEN、SECRET、LOGIN_OUT_TIME；旧静态签名字段仅兼容读取
- USERINFO.xml: xh (学号), name (姓名)
- SCHOOL.xml: SERVERURL
- config.yaml: 上述的覆盖 + 设备指纹 + 上传参数
"""

import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import yaml

from .models import AuthConfig


# 默认基础路径：hustRun/ 目录
_BASE_DIR = Path(__file__).resolve().parent.parent


def _read_xml_strings(xml_path: str) -> dict[str, str]:
    """
    读取 Android SharedPreferences XML 文件中的 <string> 条目。

    Android SP XML 格式:
        <map>
            <string name="KEY">VALUE</string>
        </map>
    """
    result = {}
    if not os.path.exists(xml_path):
        return result

    tree = ET.parse(xml_path)
    root = tree.getroot()
    for elem in root.findall("string"):
        name = elem.get("name", "")
        value = elem.text or ""
        result[name] = value
    return result


def _resolve_path(path_str: str) -> str:
    """将相对路径解析为绝对路径（相对于 hustRun/ 目录）"""
    p = Path(path_str)
    if p.is_absolute():
        return str(p)
    return str(_BASE_DIR / p)


def load_auth_config(config_path: str = "config.yaml") -> AuthConfig:
    """
    加载完整的鉴权配置。

    1. 读取 config.yaml 获取路径配置
    2. 读取 HEADER.xml, USERINFO.xml, SCHOOL.xml
    3. 合并 YAML 中的覆盖值

    Args:
        config_path: config.yaml 路径（相对于 hustRun/）

    Returns:
        AuthConfig 对象
    """
    config_path = _resolve_path(config_path)

    # --- 加载 YAML 配置 ---
    yaml_config = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            yaml_config = yaml.safe_load(f) or {}

    paths = yaml_config.get("paths", {})
    data_cfg = yaml_config.get("data", {})
    upload_cfg = yaml_config.get("upload", {})
    device_cfg = yaml_config.get("device", {})
    auth_cfg = yaml_config.get("auth", {})

    # --- 读取 XML 文件 ---
    header = _read_xml_strings(_resolve_path(paths.get(
        "header_xml", ".credentials/HEADER.xml")))
    userinfo = _read_xml_strings(_resolve_path(paths.get(
        "userinfo_xml", ".credentials/USERINFO.xml")))
    school = _read_xml_strings(_resolve_path(paths.get(
        "school_xml", ".credentials/SCHOOL.xml")))

    # --- 组装 AuthConfig ---

    # 服务端 URL
    server_url = auth_cfg.get(
        "server_url",
        school.get("SERVERURL", "https://petzjk.hust.edu.cn/cgapp-server"),
    )

    # 鉴权凭证
    token = auth_cfg.get("token", header.get("TOKEN", ""))
    secret = auth_cfg.get("secret", header.get("SECRET", ""))
    sign_val = auth_cfg.get("sign", header.get("SIGN", ""))
    timestamp_val = auth_cfg.get("timestamp", header.get("CGAPP_TIME_KEY", ""))
    web_view_time = auth_cfg.get(
        "web_view_time", header.get("WEB_VIEW_TIME", "")
    )
    login_out_time = auth_cfg.get(
        "login_out_time", header.get("LOGIN_OUT_TIME", "")
    )

    # 用户信息
    xh = userinfo.get("USERXH", "")
    name = userinfo.get("USERNAME", "")

    return AuthConfig(
        token=token,
        secret=secret,
        sign=sign_val,
        timestamp=timestamp_val,
        web_view_time=web_view_time,
        xh=xh,
        name=name,
        server_url=server_url.rstrip("/"),
        login_out_time=login_out_time,
        aes_encoding=upload_cfg.get("aes_encoding", "base64"),
        token_header=upload_cfg.get(
            "token_header", "cgAuthorization"
        ),
        imei=device_cfg.get("imei", ""),
        oaid=device_cfg.get("oaid", ""),
        user_agent=device_cfg.get("user_agent", ""),
        app_key=device_cfg.get("app_key", ""),
        v1_seed=device_cfg.get("v1_seed", ""),
        is_root=device_cfg.get("is_root", False),
        phone_version=device_cfg.get("phone_version", "Android 14"),
        skip_sensor=upload_cfg.get("skip_sensor", False),
    )


def load_yaml_config(config_path: str = "config.yaml") -> dict:
    """加载原始 YAML 配置（供其他模块读取非鉴权参数）。"""
    config_path = _resolve_path(config_path)
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def get_data_paths(config_path: str = "config.yaml") -> dict:
    """获取数据源路径配置。"""
    cfg = load_yaml_config(config_path)
    paths = cfg.get("paths", {})
    return {
        "header_xml": _resolve_path(paths.get(
            "header_xml", ".credentials/HEADER.xml")),
        "userinfo_xml": _resolve_path(paths.get(
            "userinfo_xml", ".credentials/USERINFO.xml")),
        "school_xml": _resolve_path(paths.get(
            "school_xml", ".credentials/SCHOOL.xml")),
    }
