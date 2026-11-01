"""
数据模型 — 对应逆向分析中的核心数据结构。
"""

from dataclasses import dataclass, field


@dataclass
class TrackPoint:
    """轨迹点 — 对应 SportLatLngBean / coordinate[] 元素"""
    a: float        # 纬度
    o: float        # 经度
    v: int          # 速度标记（通常为 1）
    s: float        # 速度 (km/h，与 minuteSpeed.v 相同量纲)
    t: int          # 时间戳（毫秒）
    ac: float       # 定位精度
    da: float       # 累计距离 (m)
    d: float        # 段距离 (m)
    st: int = 0     # 状态
    sta: int = 0    # 状态
    sportbean_id: str = ""  # 外键，序列化到 coordinate 时会被清洗

    def to_coordinate_dict(self) -> dict:
        """转为 UploadJsonSports.coordinate[] 元素格式"""
        return {
            "a": self.a,
            "ac": self.ac,
            "d": self.d,
            "da": self.da,
            "o": self.o,
            "s": self.s,
            "st": self.st,
            "sta": self.sta,
            "t": self.t,
            "v": self.v,
        }


@dataclass
class PaceEntry:
    """每公里配速 — 对应 Cpacestr 表 / pace[] 元素"""
    km: str     # 公里序号 "1", "2", ...
    t: str      # 配速 "05'30''"
    km_total_length: float = 0.0  # 该段结束时累计公里数
    km_dlsj: int = 0              # 该段结束时累计锻炼秒数


@dataclass
class MinuteSpeedEntry:
    """每分钟速度 — 对应 CminuteSpeedstr 表 / minuteSpeed[] 元素"""
    min: str    # 分钟序号 "1", "2", ...
    v: str      # 速度 "8.50" (km/h)


@dataclass
class SportRecord:
    """运动记录 — 对应 SportBean / sportbean 表 + 关联子表"""
    # 身份
    sport_id: str          # UUID sportid
    xh: str                # 学号
    name: str = ""         # 姓名

    # 路线
    route_id: str = ""     # 路线 ID
    route_name: str = ""   # 路线名称
    sport_type: str = ""   # 运动模式: "1"校园 "2"范围 "3"自主 "4"固定线 "5"打卡

    # 时间
    start_date: str = ""          # "2026-06-20 09:50:48"
    end_date: str = ""            # "2026-06-20 10:04:17"
    start_date_long: int = 0      # 毫秒时间戳
    end_date_long: int = 0        # 毫秒时间戳
    dl_time: str = ""             # 运动时长 "00:13:24"
    last_odometer_time: str = ""  # 最近里程时间 "00:00:56"

    # 运动数据
    dllc: str = ""              # 里程 km "3.23"
    total_step: int = 0         # 总步数
    avg_speed: str = ""         # 平均速度 km/h "14.4"
    avg_pace: str = ""          # 平均配速 "04'10''"
    cal: float = 0.0            # 热量
    step_minute: str = ""       # 平均步频 "25.3"
    max_speed_per_hour: str = ""  # 最大时速 "27.04"
    min_speed_per_hour: str = ""  # 最小时速 "6.91"

    # 标志
    indoor: int = 0             # 是否室内
    route_polyline_bh: str = "" # 路线 polyline
    already_pass_point: str = "" # 已途经点
    activity_status: int = 0    # 0 运动未结束，1 运动已结束
    status: int = 0             # 0 主记录未上传，1 主记录已上传
    result: int = 0             # 服务端有效性结果
    gyro_upload_status: int = 0 # 传感器是否已上传
    has_photo: int = 0          # 1 表示还需要图片上传链

    # 子数据
    points: list[TrackPoint] = field(default_factory=list)
    pace: list[PaceEntry] = field(default_factory=list)
    minute_speed: list[MinuteSpeedEntry] = field(default_factory=list)

    @property
    def distance_km(self) -> float:
        """总里程（公里）"""
        try:
            return float(self.dllc)
        except (ValueError, TypeError):
            if self.points:
                return self.points[-1].da / 1000.0
            return 0.0

    @property
    def duration_seconds(self) -> int:
        """运动时长（秒）"""
        if self.start_date_long and self.end_date_long:
            return (self.end_date_long - self.start_date_long) // 1000
        return 0

    @property
    def is_valid_record(self) -> bool:
        """是否满足基本有效性条件"""
        return (
            self.distance_km >= 3.0
            and len(self.points) > 10
            and self.sport_id != ""
            and self.xh != ""
        )

    def summary(self) -> str:
        """单行摘要"""
        return (
            f"[{self.sport_id[:8]}...] {self.start_date} → {self.end_date} | "
            f"{self.dllc}km | {self.total_step}步 | "
            f"{self.avg_speed}km/h | {self.avg_pace} | "
            f"{len(self.points)}轨迹点"
        )


@dataclass
class AuthConfig:
    """鉴权配置 — 从 HEADER.xml + config.yaml 提取"""
    # 从 HEADER.xml
    token: str          # JWT
    secret: str         # Base64 编码, 取 [:16] 作 AES key
    sign: str           # 旧静态值，仅兼容读取，不用于业务请求
    timestamp: str      # 旧静态值，仅兼容读取，不用于业务请求
    web_view_time: str  # 旧静态值，仅兼容读取

    # 从 USERINFO.xml
    xh: str             # 学号
    name: str           # 姓名

    # 从 SCHOOL.xml
    server_url: str = "https://petzjk.hust.edu.cn/cgapp-server"
    login_out_time: str = ""

    # 从 config.yaml
    aes_encoding: str = "base64"    # "hex" | "base64"
    token_header: str = "cgAuthorization"
    imei: str = ""
    oaid: str = ""
    user_agent: str = ""
    app_key: str = ""
    v1_seed: str = ""
    is_root: bool = False
    phone_version: str = "Android 14"
    skip_sensor: bool = False

    @property
    def aes_key(self) -> str:
        """AES 密钥 = SECRET 前 16 字符"""
        return self.secret[:16]

    @property
    def is_token_expired(self) -> bool:
        """检查 JWT 是否过期"""
        import base64
        import json
        import time
        try:
            payload_b64 = self.token.split(".")[1]
            # 补齐 padding
            payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            exp = payload.get("exp", 0)
            return time.time() > exp
        except Exception:
            return True  # 解析失败视为过期

    @property
    def missing_session_fields(self) -> list[str]:
        fields = {
            "TOKEN": self.token,
            "SECRET": self.secret,
        }
        return [name for name, value in fields.items() if not value]

    @property
    def missing_device_fields(self) -> list[str]:
        fields = {
            "imei": self.imei,
            "User-Agent": self.user_agent,
        }
        return [name for name, value in fields.items() if not value]


@dataclass
class UploadResult:
    """上传结果"""
    success: bool
    sport_id: str = ""
    is_valid: str = "0"         # "1" = 有效
    in_valid_reason: str = ""   # 无效原因
    check_status: str = ""
    tip: str = ""
    raw_response: str = ""
    sensor_zip_ok: bool = False
    sensor_file_ok: bool = False
    http_status: int = 0
    api_code: str = ""
    api_message: str = ""
