"""用真人生成特征做回归（2026-11 对齐真人样本后更新）。

真人特征：均速 9~13 km/h、点速长尾噪声、2 秒采样、精度长尾。
防止生成器退回到「竞技配速/速度精度恒定」的旧形态。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hustrun.builder import build_json_string
from hustrun.generator import generate_route_record, load_route
from hustrun.models import AuthConfig

AUTH = AuthConfig(
    token="x" * 32,
    secret="0123456789abcdef" * 4,
    sign="",
    timestamp="",
    web_view_time="",
    xh="U000000000",
    name="测试",
    imei="00000000-0000-0000-0000-000000000000",
    user_agent="hzkjapp/1.0.9",
    phone_version="PJE110,35,15|1.0.9",
)


class V50ProfileTest(unittest.TestCase):
    def setUp(self):
        record = generate_route_record(
            load_route(ROOT / "config" / "routes" / "route_track.json"),
            start_ms=1_784_961_322_000,
            sport_id="00000000-0000-4000-8000-000000000009",
            xh=AUTH.xh,
            target_distance_m=2_150,
            seed=101,
        )
        self.payload = json.loads(build_json_string(record, AUTH))
        self.points = self.payload["coordinate"]
        self.speeds = [p["s"] for p in self.points if p["s"] > 0]
        self.accuracies = [p["ac"] for p in self.points if p["t"] > 0]

    def test_speed_is_not_constant(self):
        # 真人样本（2026-09-01）：均速 9.97 km/h，点速 1.3~17.7，sd 2.6。
        self.assertGreaterEqual(len(set(self.speeds)), 50)
        self.assertGreaterEqual(min(self.speeds), 1.0)
        self.assertLessEqual(max(self.speeds), 20.0)
        mean = sum(self.speeds) / len(self.speeds)
        self.assertGreaterEqual(mean, 8.0)
        self.assertLessEqual(mean, 13.5)

    def test_accuracy_is_not_constant(self):
        # 真人样本：均值约 4m，长尾漂到 50~100m。
        self.assertGreaterEqual(len(set(self.accuracies)), 20)
        self.assertGreaterEqual(min(self.accuracies), 3.7)
        self.assertLessEqual(max(self.accuracies), 100.0)
        self.assertGreaterEqual(max(self.accuracies), 15.0)

    def test_sampling_is_about_two_seconds(self):
        deltas = [
            (b["t"] - a["t"]) / 1000.0
            for a, b in zip(self.points[1:], self.points[2:])
        ]
        self.assertTrue(all(1.9 <= delta <= 2.1 for delta in deltas))

    def test_minute_speed_changes_every_minute(self):
        values = [float(item["v"]) for item in self.payload["minuteSpeed"]]
        self.assertGreaterEqual(len(values), 5)
        self.assertGreater(len(set(values)), len(values) // 2)

    def test_distance_has_margin(self):
        self.assertGreaterEqual(float(self.payload["odometer"]), 2.0)
        self.assertGreaterEqual(self.points[-1]["da"], 2_000)


if __name__ == "__main__":
    unittest.main()
