"""编排流程测试：用假服务端验证 sportId 与提交顺序。"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

import hustrun.pipeline as pipeline
from hustrun.generator import generate_route_record, load_route
from hustrun.models import AuthConfig, UploadResult
from hustrun.http_client import (
    InSportTimeResult,
    PrejudgmentCheck,
    PrejudgmentResult,
    ServerProbe,
    SportsIdResult,
)


AUTH = AuthConfig(
    token="t" * 40,
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


class DummySigner:
    def sign(self, secret, target):
        return str(int(time.time() * 1000)), "s" * 69


class FakeSignerContext:
    """替身：prepare/submit 自己负责启动签名器，这里只要上下文协议。"""

    def __init__(self, _cfg=None):
        pass

    def __enter__(self):
        return DummySigner()

    def __exit__(self, *_exc):
        return False


def _decrypt(payload: str) -> dict:
    key = AUTH.aes_key.encode("utf-8")[:16]
    raw = base64.b64decode(payload)
    plain = unpad(AES.new(key, AES.MODE_ECB).decrypt(raw), 16)
    return json.loads(plain.decode("utf-8"))


class PipelineTest(unittest.TestCase):
    def test_fast_flow_order_and_sport_id(self):
        # 固定在北京时间 10:00，避免测试在 06:00–23:00 之外运行时被时段检查拦住。
        from datetime import datetime

        server_time = int(
            datetime(2026, 10, 25, 10, 0, 0, tzinfo=pipeline.SERVER_TZ).timestamp()
            * 1000
        )
        saved = {}
        sensor = {}

        def fake_upload(auth, encrypted, signer=None):
            saved["payload"] = _decrypt(encrypted)
            return UploadResult(
                success=True,
                sport_id=saved["payload"]["sportId"],
                is_valid="1",
                check_status="0",
                tip="提交成功，待审核",
            )

        def fake_sensor(auth, sport_id, zip_data, filename=None, signer=None):
            sensor["sport_id"] = sport_id
            sensor["filename"] = filename
            return True

        options = pipeline.UploadOptions(
            route_path=str(ROOT / "config" / "routes" / "route_track.json"),
            target_distance_m=120,
            fast=True,
            seed=9,
        )

        with mock.patch.object(
            pipeline, "probe_server_time",
            side_effect=lambda *_: ServerProbe(server_time=server_time),
        ), mock.patch.object(
            pipeline,
            "request_in_sport_time",
            return_value=InSportTimeResult(allowed=True),
        ), mock.patch.object(
            pipeline, "request_sports_id",
            return_value=SportsIdResult(
                sport_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
            ),
        ), mock.patch.object(
            pipeline, "request_prejudgment",
            return_value=PrejudgmentResult(
                success=True,
                checks=[PrejudgmentCheck(is_valid="1", content="ok")],
            ),
        ), mock.patch.object(pipeline, "upload_savesports", side_effect=fake_upload), mock.patch.object(
            pipeline, "upload_sensor_zip", side_effect=fake_sensor
        ):
            report = pipeline._run_network_flow(
                AUTH,
                load_route(options.route_path),
                options,
                DummySigner(),
                lambda _m: None,
            )

        self.assertTrue(report.result.success)
        self.assertEqual(report.server_sport_id, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
        self.assertIsNotNone(report.sport_id_issued_at_ms)
        # fast 是对照实验：整条记录被前移，起点必然早于 sportId 的领取时刻。
        self.assertLess(
            report.record.start_date_long, report.sport_id_issued_at_ms
        )
        self.assertEqual(saved["payload"]["sportId"], report.server_sport_id)
        self.assertEqual(sensor["sport_id"], report.server_sport_id)
        # fast 模式必须把记录挪到服务端时间之前。
        self.assertLessEqual(report.record.end_date_long, server_time)
        self.assertEqual(saved["payload"]["odometer"], report.record.dllc)

    def test_session_round_trip_keeps_points(self):
        record = generate_route_record(
            load_route(ROOT / "config" / "routes" / "route_track.json"),
            start_ms=1_784_961_322_000,
            sport_id="00000000-0000-4000-8000-00000000000a",
            xh=AUTH.xh,
            name=AUTH.name,
            target_distance_m=120,
            seed=4,
        )
        options = pipeline.UploadOptions(
            route_path=str(ROOT / "config" / "routes" / "route_track.json"),
            target_distance_m=120,
            seed=4,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = pipeline.save_session(
                Path(tmp) / "s.json",
                sport_id=record.sport_id,
                issued_at_ms=record.start_date_long - 5_000,
                record=record,
                options=options,
            )
            loaded, data = pipeline.load_session(path)

        self.assertEqual(data["sport_id"], record.sport_id)
        self.assertEqual(loaded.sport_id, record.sport_id)
        self.assertEqual(loaded.start_date_long, record.start_date_long)
        self.assertEqual(loaded.end_date_long, record.end_date_long)
        self.assertEqual(loaded.dllc, record.dllc)
        self.assertEqual(len(loaded.points), len(record.points))
        self.assertEqual(loaded.points[0].t, record.points[0].t)
        self.assertEqual(loaded.points[-1].da, record.points[-1].da)
        self.assertEqual([item.v for item in loaded.minute_speed][:3],
                         [item.v for item in record.minute_speed][:3])

    def test_prepare_does_not_submit(self):
        now_ms = int(time.time() * 1000)
        options = pipeline.UploadOptions(
            route_path=str(ROOT / "config" / "routes" / "route_track.json"),
            target_distance_m=120,
            seed=7,
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            pipeline, "SESSION_DIR", Path(tmp)
        ), mock.patch.object(
            pipeline, "_require_auth", return_value=AUTH
        ), mock.patch.object(
            pipeline, "open_signer", return_value=FakeSignerContext()
        ), mock.patch.object(
            pipeline, "probe_server_time",
            side_effect=lambda *_: ServerProbe(server_time=now_ms),
        ), mock.patch.object(
            pipeline,
            "request_in_sport_time",
            return_value=InSportTimeResult(allowed=True),
        ), mock.patch.object(
            pipeline, "request_sports_id",
            return_value=SportsIdResult(
                sport_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
            ),
        ), mock.patch.object(
            pipeline, "upload_savesports", side_effect=AssertionError("prepare 不应提交")
        ):
            report = pipeline.prepare_route("config.yaml", options, lambda _m: None)
            files = list(Path(tmp).glob("*.json"))
            loaded, _ = pipeline.load_session(Path(report.session_path))

        self.assertTrue(report.session_path)
        self.assertEqual(report.server_sport_id, "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
        self.assertEqual(len(files), 1)
        # 记录的起点仍然在 sportId 领取之后。
        self.assertGreater(loaded.start_date_long, report.sport_id_issued_at_ms)

    def test_submit_session_reuses_saved_sport_id(self):
        now_ms = int(time.time() * 1000)
        record = generate_route_record(
            load_route(ROOT / "config" / "routes" / "route_track.json"),
            start_ms=now_ms - 300_000,
            sport_id="aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff",
            xh=AUTH.xh,
            name=AUTH.name,
            target_distance_m=120,
            seed=11,
        )
        options = pipeline.UploadOptions(
            route_path=str(ROOT / "config" / "routes" / "route_track.json"),
            target_distance_m=120,
            seed=11,
        )
        saved = {}

        def fake_upload(auth, encrypted, signer=None):
            saved["payload"] = _decrypt(encrypted)
            return UploadResult(
                success=True, sport_id=saved["payload"]["sportId"], is_valid="1"
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = pipeline.save_session(
                Path(tmp) / "s.json",
                sport_id=record.sport_id,
                issued_at_ms=record.start_date_long - 5_000,
                record=record,
                options=options,
            )
            with mock.patch.object(
                pipeline, "_require_auth", return_value=AUTH
            ), mock.patch.object(
                pipeline, "open_signer", return_value=FakeSignerContext()
            ), mock.patch.object(
                pipeline, "probe_server_time",
                side_effect=lambda *_: ServerProbe(server_time=now_ms),
            ), mock.patch.object(
                pipeline, "request_prejudgment",
                return_value=PrejudgmentResult(
                    success=True,
                    checks=[PrejudgmentCheck(is_valid="1", content="ok")],
                ),
            ), mock.patch.object(
                pipeline, "request_sports_id",
                side_effect=AssertionError("submit 不应重新领取 sportId"),
            ), mock.patch.object(
                pipeline, "upload_savesports", side_effect=fake_upload
            ), mock.patch.object(
                pipeline, "upload_sensor_zip", return_value=True
            ):
                report = pipeline.submit_session(
                    "config.yaml", str(path), skip_sensor=True, progress=lambda _m: None
                )

        self.assertTrue(report.result.success)
        self.assertEqual(report.server_sport_id, record.sport_id)
        self.assertEqual(saved["payload"]["sportId"], record.sport_id)
        self.assertEqual(report.record.xh, AUTH.xh)


if __name__ == "__main__":
    unittest.main()
