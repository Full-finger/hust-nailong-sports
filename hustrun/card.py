"""把一条运动记录渲染成分享卡片（PNG）。

布局仿 App 详情页：顶部距离 + 状态徽章，中部四项指标与轨迹，
底部每公里配速条。只做只读渲染，不改任何服务端状态。
"""

from __future__ import annotations

import io
import math
from datetime import datetime
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(r"C:/Windows/Fonts")
FONT_REG = FONT_DIR / "msyh.ttc"
FONT_BOLD = FONT_DIR / "msyhbd.ttc"

# 参考图配色
COLOR_TEXT_MAIN = (58, 70, 101)
COLOR_TEXT_SUB = (152, 158, 170)
COLOR_TEXT_LIGHT = (190, 195, 205)
COLOR_BLUE = (74, 144, 217)
COLOR_TRACK = (92, 191, 138)
COLOR_TRACK_EDGE = (230, 243, 236)
COLOR_START = (52, 168, 83)
COLOR_END = (232, 115, 74)
COLOR_MAP_BG = (240, 243, 241)
COLOR_MAP_GRID = (226, 232, 228)
COLOR_BADGE = (201, 204, 212)
COLOR_BADGE_OK = (76, 175, 125)
COLOR_BADGE_INVALID = (238, 146, 100)
BAR_START = (127, 179, 247)
BAR_END = (74, 125, 224)
GRAD_LEFT = (77, 184, 168)
GRAD_RIGHT = (232, 201, 92)

CARD_W = 720
TRACK_H = 480
PACE_ROW_H = 52
PACE_BLOCK_H = 100
SPEED_H = 260

TILE_SIZE = 256
TILE_URL = (
    "https://webrd0{host}.is.autonavi.com/appmaptile"
    "?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}"
)
TILE_TIMEOUT = 4


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REG
    return ImageFont.truetype(str(path), size)


def _text(draw: ImageDraw.ImageDraw, xy, text, size, fill, bold=False, anchor=None):
    draw.text(xy, text, font=_font(size, bold), fill=fill, anchor=anchor)


def _duration_seconds(rec: dict) -> int | None:
    value = rec.get("duration")
    if value not in (None, ""):
        try:
            return int(float(value))
        except ValueError:
            pass
    try:
        start = datetime.strptime(rec["beginTime"], "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(rec["endTime"], "%Y-%m-%d %H:%M:%S")
        return int((end - start).total_seconds())
    except (KeyError, TypeError, ValueError):
        return None


def _fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _pace_seconds(text: str) -> int | None:
    """把 04'36'' 解析成秒。"""
    try:
        parts = text.replace("''", "'").split("'")
        return int(parts[0]) * 60 + int(parts[1])
    except (IndexError, ValueError):
        return None


def _avg_pace_seconds(rec: dict) -> int | None:
    value = rec.get("avgPace")
    if isinstance(value, str) and "'" in value:
        return _pace_seconds(value)
    return None


def _badge(rec: dict) -> tuple[str, tuple[int, int, int]]:
    if str(rec.get("isValid")) == "-1":
        return "无效", COLOR_BADGE_INVALID
    if str(rec.get("checkStatus")) == "1":
        return "已过审", COLOR_BADGE_OK
    return "待审", COLOR_BADGE


def _draw_gradient(draw: ImageDraw.ImageDraw, x0, x1, y, height, c0, c1):
    for x in range(int(x0), int(x1)):
        ratio = (x - x0) / max(1, (x1 - x0))
        color = tuple(int(c0[i] + (c1[i] - c0[i]) * ratio) for i in range(3))
        draw.line([(x, y), (x, y + height)], fill=color)


def _draw_bar(draw: ImageDraw.ImageDraw, x0, x1, y, half_h, c0, c1):
    for x in range(int(x0), int(x1)):
        t = (x - x0) / max(1, (x1 - x0))
        color = tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))
        draw.line([(x, y - half_h), (x, y + half_h)], fill=color)


def _merc_y(lat_deg: float, n: float) -> float:
    phi = math.radians(lat_deg)
    return (1 - math.asinh(math.tan(phi)) / math.pi) / 2 * n * TILE_SIZE


def _world_pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    n = float(2**zoom)
    x = (lon + 180) / 360 * n * TILE_SIZE
    return x, _merc_y(lat, n)


def _pick_zoom(
    lat_min: float, lat_max: float, lon_min: float, lon_max: float,
    win_w: int, win_h: int,
) -> int:
    """选最大 zoom 使得轨迹 bbox 约占窗口 2/3。高德路网瓦片到 z18。"""
    lat_mid = (lat_min + lat_max) / 2
    for z in range(18, 12, -1):
        n = float(2**z)
        w = (lon_max - lon_min) / 360 * n * TILE_SIZE * math.cos(math.radians(lat_mid))
        h = abs(_merc_y(lat_min, n) - _merc_y(lat_max, n))
        if w <= win_w * 0.68 and h <= win_h * 0.68:
            return z
    return 13


def _fetch_base_map(
    box,
    lats, lons,
    zoom: int,
    win_left: float, win_top: float,
) -> Image.Image | None:
    """拉高德 Web 瓦片拼出 box 大小的底图；失败返回 None。"""
    x0, y0, x1, y1 = box
    win_w, win_h = int(x1 - x0), int(y1 - y0)
    win_right = win_left + win_w
    win_bottom = win_top + win_h

    tx0, ty0 = int(win_left // TILE_SIZE), int(win_top // TILE_SIZE)
    tx1, ty1 = int(win_right // TILE_SIZE), int(win_bottom // TILE_SIZE)
    if tx1 - tx0 + 1 > 8 or ty1 - ty0 + 1 > 8:
        return None

    tiled = Image.new("RGB", ((tx1 - tx0 + 1) * TILE_SIZE, (ty1 - ty0 + 1) * TILE_SIZE))
    n = int(2**zoom)
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            if ty < 0 or ty >= n:
                continue
            url = TILE_URL.format(host=(abs(tx) + abs(ty)) % 4 + 1, x=tx % n, y=ty, z=zoom)
            try:
                resp = requests.get(url, timeout=TILE_TIMEOUT,
                                    headers={"User-Agent": "hustrun/1.0"})
                if resp.status_code != 200 or len(resp.content) < 200:
                    return None  # 空白占位瓦片视为不可用
                tile = Image.open(io.BytesIO(resp.content))
                if tile.mode in ("P", "LA"):
                    tile = tile.convert("RGBA")
                tile = tile.convert("RGB")
            except Exception:
                return None
            tiled.paste(tile, ((tx - tx0) * TILE_SIZE, (ty - ty0) * TILE_SIZE))

    crop_left = int(win_left - tx0 * TILE_SIZE)
    crop_top = int(win_top - ty0 * TILE_SIZE)
    return tiled.crop((crop_left, crop_top, crop_left + win_w, crop_top + win_h))


def _draw_track(base: Image.Image, draw: ImageDraw.ImageDraw, box, points):
    """把轨迹画进 box=(x0, y0, x1, y1)，带高德底图（失败退回网格）。"""
    x0, y0, x1, y1 = box
    win_w, win_h = int(x1 - x0), int(y1 - y0)
    pts = [p for p in points if p.get("a") and p.get("o")]

    if len(pts) < 2:
        draw.rectangle(box, fill=COLOR_MAP_BG)
        for gx in range(int(x0), int(x1), 60):
            draw.line([(gx, y0), (gx, y1)], fill=COLOR_MAP_GRID, width=1)
        for gy in range(int(y0), int(y1), 60):
            draw.line([(x0, gy), (x1, gy)], fill=COLOR_MAP_GRID, width=1)
        _text(draw, ((x0 + x1) / 2, (y0 + y1) / 2), "无轨迹数据", 24,
              COLOR_TEXT_LIGHT, anchor="mm")
        return

    lats = [p["a"] for p in pts]
    lons = [p["o"] for p in pts]
    zoom = _pick_zoom(min(lats), max(lats), min(lons), max(lons), win_w, win_h)
    center_x, center_y = _world_pixel(
        (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2, zoom
    )
    win_left = center_x - win_w / 2
    win_top = center_y - win_h / 2

    base_map = _fetch_base_map(box, lats, lons, zoom, win_left, win_top)
    if base_map is not None:
        base.paste(base_map, (int(x0), int(y0)))
    else:
        draw.rectangle(box, fill=COLOR_MAP_BG)
        for gx in range(int(x0), int(x1), 60):
            draw.line([(gx, y0), (gx, y1)], fill=COLOR_MAP_GRID, width=1)
        for gy in range(int(y0), int(y1), 60):
            draw.line([(x0, gy), (x1, gy)], fill=COLOR_MAP_GRID, width=1)

    def project(p):
        px, py = _world_pixel(p["o"], p["a"], zoom)
        # 墨卡托 y 向下增长，与画布坐标一致
        return x0 + px - win_left, y0 + py - win_top

    coords = [project(p) for p in pts]
    draw.line(coords, fill=COLOR_TRACK_EDGE, width=10, joint="curve")
    draw.line(coords, fill=COLOR_TRACK, width=6, joint="curve")

    start, end = coords[0], coords[-1]
    for pos, color, label in ((start, COLOR_START, "起"), (end, COLOR_END, "终")):
        r = 16
        draw.ellipse(
            (pos[0] - r, pos[1] - r, pos[0] + r, pos[1] + r),
            fill=color, outline="white", width=3,
        )
        _text(draw, pos, label, 18, "white", bold=True, anchor="mm")


def _extract_speed_series(rec: dict) -> list[tuple[float, float]]:
    """从轨迹点提取 (秒偏移, 速度 km/h)。

    点的 t 混用 0 占位与 epoch 毫秒：首个点 t=0，之后是毫秒时间戳。
    以首个非零 t 为基准换算秒偏移；有效时间戳过少时退回按序号铺开。
    """
    pts = [
        p for p in (rec.get("coordinatestr") or [])
        if p.get("s") is not None
        and not (not p.get("t") and float(p["s"]) == 0)  # 首点占位：t=0 且 s=0
    ]
    if len(pts) < 2:
        return []
    ts = [p.get("t") or 0 for p in pts]
    usable = sum(1 for t in ts if t > 0)
    if usable >= len(pts) // 2:
        t0 = min(t for t in ts if t > 0)
        return [
            (0.0 if t <= 0 else (t - t0) / 1000.0 + 1.0, float(p["s"]))
            for t, p in zip(ts, pts)
        ]
    return [(float(i), float(p["s"])) for i, p in enumerate(pts)]


def _draw_speed_chart(
    draw: ImageDraw.ImageDraw, box, series: list[tuple[float, float]],
):
    """速度折线图：逐轨迹点 km/h，叠加均速参考虚线。

    均速取轨迹点自身均值，与折线自洽；record 的 avgSpeed 可能按
    不同时长口径计算，直接用会和曲线对不上。
    """
    x0, y0, x1, y1 = box
    _text(draw, (40, y0 + 22), "速度 (km/h)", 30, COLOR_TEXT_MAIN,
          bold=True, anchor="lm")
    sub = f"逐点采样 · {len(series)}点"
    _text(draw, (CARD_W - 40, y0 + 22), sub, 20, COLOR_TEXT_SUB, anchor="rm")

    plot_x0, plot_x1 = x0 + 64, x1 - 40
    plot_y0, plot_y1 = y0 + 56, y1 - 72
    plot_w = max(1.0, plot_x1 - plot_x0)
    plot_h = max(1.0, plot_y1 - plot_y0)

    xs = [s for s, _ in series]
    ys = [v for _, v in series]
    t_max = max(xs) or 1.0
    moving = [v for v in ys if v > 0]
    avg_speed = sum(moving) / len(moving) if moving else None
    top = max(max(ys), avg_speed or 0.0)
    hi = top * 1.15 + 0.5

    def sx(t: float) -> float:
        return plot_x0 + t / t_max * plot_w

    def sy(v: float) -> float:
        return plot_y1 - v / hi * plot_h

    # 横向网格 + 整数刻度
    raw_step = hi / 3
    step = 5 if raw_step > 3.5 else (2 if raw_step > 1.5 else 1)
    v = step
    while v < hi:
        gy = sy(v)
        draw.line([(plot_x0, gy), (plot_x1, gy)], fill=COLOR_MAP_GRID, width=1)
        _text(draw, (plot_x0 - 10, gy), f"{v:.0f}", 16, COLOR_TEXT_LIGHT,
              anchor="rm")
        v += step

    # 均速参考虚线
    if avg_speed and avg_speed > 0:
        gy = sy(avg_speed)
        seg = 16
        for gx in range(int(plot_x0), int(plot_x1), seg * 2):
            draw.line([(gx, gy), (gx + seg, gy)], fill=COLOR_TRACK, width=2)
        _text(draw, (plot_x1, gy - 14), f"均速 {avg_speed:.1f}", 16,
              COLOR_TRACK, anchor="rm")

    coords = [(sx(t), sy(s)) for t, s in series]
    draw.line(coords, fill=COLOR_BLUE, width=2, joint="curve")

    # x 轴时长标注
    total = int(t_max)
    _text(draw, (plot_x0, y1 - 50), "0:00", 16, COLOR_TEXT_LIGHT, anchor="lm")
    _text(draw, (plot_x1, y1 - 50),
          f"全程 {total // 60:02d}:{total % 60:02d}", 16,
          COLOR_TEXT_LIGHT, anchor="rm")


def render_card(record: dict, total_valid_km: float, out_path: str | Path) -> Path:
    """把一条记录渲染成 PNG 卡片。total_valid_km 为累计有效里程。

    轨迹点带速度时，在配速条下方追加速度折线图。
    """
    rec = record
    pace_list = rec.get("paceStr") or []

    speed_series = _extract_speed_series(rec)
    chart_block = SPEED_H if speed_series else 0
    card_h = 370 + TRACK_H + PACE_BLOCK_H + PACE_ROW_H * len(pace_list) + chart_block
    image = Image.new("RGB", (CARD_W, card_h), "white")
    draw = ImageDraw.Draw(image)

    # ---- 顶部：距离 + 徽章 ----
    badge_text, badge_color = _badge(rec)
    odo = f"{float(rec.get('odometer') or 0):g}"
    _text(draw, (40, 60), odo, 84, COLOR_TEXT_MAIN, bold=True, anchor="lm")
    odo_w = draw.textlength(odo, font=_font(84, bold=True))
    _text(draw, (40 + odo_w + 16, 88), "公里", 28, COLOR_TEXT_SUB, anchor="lm")

    badge_w = 104
    draw.rounded_rectangle(
        (CARD_W - 40 - badge_w, 34, CARD_W - 40, 78),
        radius=22, fill=badge_color,
    )
    _text(draw, (CARD_W - 40 - badge_w / 2, 56), badge_text, 24, "white",
          bold=True, anchor="mm")
    _text(draw, (CARD_W - 40, 104), rec.get("tip") or "", 24, COLOR_TEXT_SUB,
          anchor="rm")

    # ---- 日期 + 类型 ----
    date_text = (rec.get("beginTime") or rec.get("date") or "")[:16]
    _text(draw, (40, 150), date_text, 30, COLOR_BLUE, anchor="lm")
    _text(draw, (CARD_W - 40, 150), "校园跑", 30, COLOR_TEXT_MAIN, anchor="rm")

    # ---- 渐变分隔条 ----
    _draw_gradient(draw, 40, CARD_W - 40, 186, 6, GRAD_LEFT, GRAD_RIGHT)

    # ---- 本次 / 累计 ----
    valid_km = float(rec.get("odometer") or 0) if str(rec.get("isValid")) == "1" else 0.0
    _text(draw, (40, 224), "本次有效", 26, COLOR_TEXT_SUB, anchor="lm")
    _text(draw, (150, 224), f"{valid_km:.2f}km", 30, COLOR_TEXT_MAIN, bold=True, anchor="lm")
    _text(draw, (CARD_W - 190, 224), "累计有效里程", 26, COLOR_TEXT_SUB, anchor="rm")
    _text(draw, (CARD_W - 40, 224), f"{total_valid_km:.2f}km", 30, COLOR_TEXT_MAIN,
          bold=True, anchor="rm")

    # ---- 四项指标 ----
    seconds = _duration_seconds(rec)
    avg_pace = _avg_pace_seconds(rec)
    pace_text = f"{avg_pace // 60}'{avg_pace % 60:02d}''" if avg_pace is not None else "-"
    metrics = [
        (_fmt_duration(seconds), "时长"),
        (f"{float(rec.get('avgSpeed') or 0):.2f}", "均速(km/h)"),
        (pace_text, "平均配速"),
        (f"{float(rec.get('calorie') or 0):.1f}", "热量(kcal)"),
    ]
    col_w = (CARD_W - 80) / 4
    for i, (value, label) in enumerate(metrics):
        cx = 40 + col_w * i + col_w / 2
        _text(draw, (cx, 290), value, 34, COLOR_TEXT_MAIN, bold=True, anchor="mm")
        _text(draw, (cx, 334), label, 24, COLOR_TEXT_SUB, anchor="mm")

    # ---- 轨迹 ----
    _draw_track(image, draw, (0, 370, CARD_W, 370 + TRACK_H), rec.get("coordinatestr") or [])

    # ---- 配速 ----
    y = 370 + TRACK_H + 30
    _text(draw, (40, y), "配速", 32, COLOR_TEXT_MAIN, bold=True, anchor="lm")
    parsed = []
    for i, row in enumerate(pace_list):
        sec = _pace_seconds(row.get("t", ""))
        parsed.append((row.get("km", i + 1), row.get("t", "-"), sec))
        if sec is not None:
            pass
    secs = [s for _, _, s in parsed if s is not None]
    sec_max = max(secs) if secs else 0
    bar_x0 = 150
    bar_w_max = CARD_W - 190 - bar_x0
    for i, (km, label, sec) in enumerate(parsed):
        ry = y + 58 + i * PACE_ROW_H
        _text(draw, (40, ry), f"{km} km", 26, COLOR_TEXT_MAIN, anchor="lm")
        if sec is None or sec_max == 0:
            width = bar_w_max * 0.8
        else:
            # 条长 ∝ 该公里耗时（单纯按时间长短，最慢的满条）
            width = bar_w_max * (sec / sec_max)
        _draw_bar(draw, bar_x0, bar_x0 + width, ry, 12, BAR_START, BAR_END)
        _text(draw, (CARD_W - 40, ry), label, 28, COLOR_TEXT_MAIN, anchor="rm")

    # ---- 速度 ----
    if speed_series:
        y_chart = 370 + TRACK_H + PACE_BLOCK_H + PACE_ROW_H * len(pace_list)
        _draw_speed_chart(
            draw, (0, y_chart, CARD_W, y_chart + SPEED_H),
            speed_series,
        )

    # ---- 页脚 ----
    footer_y = card_h - 26
    _text(draw, (40, footer_y), "hustrun", 20, COLOR_TEXT_LIGHT, anchor="lm")
    sport_id = str(rec.get("sportId", ""))
    _text(draw, (CARD_W - 40, footer_y), sport_id[:13], 20, COLOR_TEXT_LIGHT, anchor="rm")

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, "PNG")
    return out
