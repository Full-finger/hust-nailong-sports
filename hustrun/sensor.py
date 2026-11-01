"""
传感器数据处理器 — 校验真实传感器文件并准备上传。

真实格式（从真机 /sdcard/sensor/<sportId>.txt 提取）：
    纯 JSON 数组，每个元素为加速度幅值（float），格式如下：
    [val1, val1, val2, val2, val3, val3, ...]
    数值是设备采集结果，不能由 GPS 轨迹可靠反推。

    加速度幅值 ≈ sqrt(ax² + ay² + az²)，包含重力加速度（基线 ~9.8 m/s²）。
    跑步时足部着地产生冲击峰值（可达 15-20 m/s²）。

从 2026-06-20 记录 (431 轨迹点, 3.23km, 13:24):
    传感器文件 74KB, 7941 个值, 约 18.4 值/轨迹点。
"""

import json
import hashlib
import math
from pathlib import Path
from typing import Optional

# 参考数据文件的相对路径
_REFERENCE_SENSOR_FILE = Path(__file__).resolve().parent.parent / "data" / "sensor_a80da7e9.txt"


def _load_reference_data() -> list[float]:
    """加载真实传感器参考数据，用于统计分析。"""
    if _REFERENCE_SENSOR_FILE.exists():
        with open(_REFERENCE_SENSOR_FILE, "r") as f:
            return json.load(f)
    return []


def load_reference_sensor(
    sport_id: Optional[str] = None,
    sensor_path: Optional[str] = None,
    expected_sha256: Optional[str] = None,
) -> Optional[str]:
    """
    加载参考传感器文件。

    如果有与 sport_id 匹配的参考文件则用它，
    否则使用 data/sensor_a80da7e9.txt（2026-06-20 记录）。

    Args:
        sport_id: 运动记录 ID（可选）

    Returns:
        JSON 字符串或 None
    """
    candidates: list[Path] = []
    if sensor_path:
        explicit = Path(sensor_path)
        name_matches = (
            not sport_id
            or sport_id in explicit.stem
            or sport_id[:8] in explicit.stem
        )
        # config.yaml 中显式指定的 sensor_path 即为用户选定的快照；
        # 变异记录会在上传参数中改用新的 sportId，不能再按文件名筛掉它。
        if name_matches or expected_sha256 or sensor_path:
            candidates.append(explicit)
    if sport_id:
        candidates.extend(
            [
                _REFERENCE_SENSOR_FILE.parent / f"sensor_{sport_id}.txt",
                _REFERENCE_SENSOR_FILE.parent / f"sensor_{sport_id[:8]}.txt",
            ]
        )
    elif not sensor_path:
        candidates.append(_REFERENCE_SENSOR_FILE)

    for candidate in candidates:
        if candidate.exists():
            raw = candidate.read_bytes()
            if expected_sha256:
                actual_sha256 = hashlib.sha256(raw).hexdigest()
                if actual_sha256.lower() != expected_sha256.strip().lower():
                    continue
            text = raw.decode("utf-8").strip()
            try:
                values = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(values, list) and values:
                return text

    return None


def prepare_sensor_zip_upload(sensor_json: str, sport_id: str) -> tuple[str, bytes]:
    """
    准备传感器 ZIP 上传。

    模拟 SportUploadModel.侂薶挂():
        1. 补齐末尾 "]"
        2. gzip 压缩
        3. Base64 编码

    Args:
        sensor_json: 传感器 JSON 字符串
        sport_id: 运动记录 ID

    Returns:
        (文件名, Base64 编码的 gzip 数据)
    """
    import base64
    import gzip
    import io

    # 确保 JSON 格式完整
    text = sensor_json.strip()
    if not text.endswith("]"):
        text = text + "]"

    # gzip 压缩
    # Android GZIPOutputStream 使用 level 6、mtime=0、OS=255。
    # Android Base64.DEFAULT 每 76 字符换行并保留末尾换行。
    output = io.BytesIO()
    with gzip.GzipFile(
        fileobj=output,
        mode="wb",
        compresslevel=6,
        mtime=0,
    ) as compressed:
        compressed.write(text.encode("utf-8"))
    b64_data = base64.encodebytes(output.getvalue())

    filename = f"{sport_id}.txt"

    return filename, b64_data


def prepare_sensor_file_upload(sensor_json: str) -> str:
    """
    准备传感器文件上传的原始文本。

    对应 sensorFile 端点：直接读取 sensor txt 内容。

    Args:
        sensor_json: 传感器 JSON 字符串

    Returns:
        补齐后的传感器文本
    """
    text = sensor_json.strip()
    if not text.endswith("]"):
        text = text + "]"
    return text


def analyze_reference_data() -> dict:
    """
    分析参考传感器数据的统计特征。

    Returns:
        {count, mean, std, min, max, median, values_per_track_point}
    """
    ref = _load_reference_data()
    if not ref:
        return {"error": "无参考数据文件"}

    sorted_ref = sorted(ref)
    n = len(sorted_ref)
    median = sorted_ref[n // 2]

    return {
        "count": n,
        "mean": round(sum(ref) / n, 4),
        "std": round(math.sqrt(sum((v - sum(ref) / n) ** 2 for v in ref) / n), 4),
        "min": round(min(ref), 4),
        "max": round(max(ref), 4),
        "median": round(median, 4),
    }
