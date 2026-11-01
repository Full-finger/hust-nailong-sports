"""生成器与 payload 构造的离线测试。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hustrun.builder import build_json_string, build_prejudgment_json
from hustrun.consistency import validate_record_consistency
from hustrun.generator import generate_route_record, load_route, make_sensor_trace
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

ROUTES = ROOT / "config" / "routes"


class GeneratorTest(unittest.TestCase):
    def test_all_routes_are_consistent(self):
        for path in sorted(ROUTES.glob("*.json")):
            with self.subTest(route=path.name):
                record = generate_route_record(
                    load_route(path),
                    start_ms=1_784_961_322_000,
                    sport_id="00000000-0000-4000-8000-000000000001",
                    xh=AUTH.xh,
                    target_distance_m=2_150,
                    seed=11,
                )
                self.assertEqual(validate_record_consistency(record), [])
                self.assertGreaterEqual(record.points[-1].da, 2_000)
                self.assertEqual(record.activity_status, 1)
                self.assertEqual(record.result, 1)

    def test_speed_statistics_match_geometry(self):
        record = generate_route_record(
            load_route(ROUTES / "route_track.json"),
            start_ms=1_784_961_322_000,
            sport_id="00000000-0000-4000-8000-000000000002",
            xh=AUTH.xh,
            target_distance_m=2_150,
            seed=3,
        )
        weighted = sum(
            point.s * (point.t - previous.t)
            for previous, point in zip(record.points[1:], record.points[2:])
        ) / (record.points[-1].t - record.points[1].t)
        self.assertAlmostEqual(weighted, float(record.avg_speed), delta=0.6)

    def test_speed_fields_match_patched_app(self):
        """v51 的 build() 会把 max 覆盖成 avg、把 min 统一成 "6.0"。"""
        record = generate_route_record(
            load_route(ROUTES / "route_track.json"),
            start_ms=1_784_961_322_000,
            sport_id="00000000-0000-4000-8000-000000000004",
            xh=AUTH.xh,
            target_distance_m=2_150,
            seed=13,
        )
        self.assertEqual(record.max_speed_per_hour, record.avg_speed)
        self.assertEqual(record.min_speed_per_hour, "6.0")

    def test_payload_shape(self):
        record = generate_route_record(
            load_route(ROUTES / "route_track.json"),
            start_ms=1_784_961_322_000,
            sport_id="00000000-0000-4000-8000-000000000003",
            xh=AUTH.xh,
            target_distance_m=2_150,
            seed=5,
        )
        payload = json.loads(build_json_string(record, AUTH))
        self.assertEqual(payload["sportId"], record.sport_id)
        self.assertEqual(payload["isValid"], 1)
        self.assertEqual(len(payload["coordinate"]), len(record.points))
        self.assertEqual(payload["coordinate"][0]["t"], 0)
        self.assertNotIn("baseObjId", payload["coordinate"][0])
        prejudgment = json.loads(build_prejudgment_json(record, AUTH))
        self.assertNotIn("coordinate", prejudgment)

    def test_sensor_trace_length(self):
        trace = json.loads(make_sensor_trace(500, seed=1))
        self.assertEqual(len(trace), 5_000)
        self.assertTrue(all(0 < value < 40 for value in trace))


if __name__ == "__main__":
    unittest.main()
