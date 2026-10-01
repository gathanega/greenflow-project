"""
GreenFlow VPS server (FastAPI).

Two ingestion modes, both feeding the same pipeline (detect -> classify ->
compute light program -> store -> serve):

  MODE A ("esp32"): the ESP32-S3-VROOM-1 camera node POSTs JPEG frames to
      POST /api/ingest/esp32/{device_id}?location=...

  MODE B ("cctv"): this server itself periodically pulls a snapshot from an
      existing online CCTV API/stream URL, for locations configured with
      mode: cctv in config.yaml. No ESP32-S3 needed for those locations.
      Three CCTV source types are supported (set with cctv_stream_type):
        - "snapshot" (default): the URL returns a plain JPEG directly.
        - "hls": the URL is a live HLS (.m3u8) stream.
        - "mjpeg": the URL is a live MJPEG/multipart stream (common for
          city-run CCTV proxies, e.g. Diskominfo-style "proxy.php" feeds).
      Both "hls" and "mjpeg" grab a single frame using ffmpeg, since
      requests.get() alone can't reliably pull one still frame out of a
      live multi-frame stream.

  LANE FILTERING (ROI): a wide CCTV frame often covers more than one lane
      or direction of traffic, but the light program should usually react
      to just the lane feeding that light. Each location in config.yaml
      can optionally set a `roi` — a polygon (list of [x, y] points, each
      0.0-1.0, normalized to frame width/height) marking the lane to
      count. See vision.detect_vehicles() for the matching logic, and
      GET /api/debug/roi-preview to visually calibrate the polygon against
      a live frame before committing to config.yaml.

Either mode ends up calling process_frame(), which writes one row to the
Supabase `greenflow_logs` table (used by the web dashboard) and also keeps
the latest program in memory per location, served to the ESP32-C6 traffic
light controllers via:

  GET /api/light/latest?location=...
"""

from __future__ import annotations

import asyncio
import io
import logging
import shutil
import subprocess
import time
from typing import Optional

# systemd services can run with a more limited PATH than an interactive
# shell, so "ffmpeg" alone may not resolve even if `apt install ffmpeg`
# succeeded. Resolve the absolute path once at import time, with a
# fallback to the common Debian/Ubuntu install location.
FFMPEG_BIN = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"

import requests
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageDraw
from supabase import Client, create_client

from vision import compute_light_program, detect_vehicles

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("greenflow")

app = FastAPI(title="GreenFlow VPS Server")

with open("config.yaml", "r") as f:
    CONFIG = yaml.safe_load(f)

supabase: Client = create_client(CONFIG["supabase"]["url"], CONFIG["supabase"]["service_key"])

# in-memory cache of the most recent light program per location, so the
# ESP32-C6 poll endpoint is instant and doesn't hit Supabase every request
LATEST_PROGRAMS: dict[str, dict] = {}


def location_calibration(location: str) -> dict:
    """Per-location thresholds from config.yaml, with sane fallbacks."""
    locations = CONFIG.get("locations", {})
    return locations.get(location, {}) or {}


def process_frame(jpeg_bytes: bytes, location: str, device_id: str, battery_level: Optional[int] = None):
    calib = location_calibration(location)
    calibration_max_count = calib.get("calibration_max_count", 20)
    density_low = calib.get("density_low", 5)
    density_high = calib.get("density_high", 15)
    roi_polygon = calib.get("roi")  # optional: [[x,y], ...] normalized 0-1, one lane

    detection = detect_vehicles(jpeg_bytes, roi_polygon=roi_polygon)
    program = compute_light_program(
        vehicle_count=detection.vehicle_count,
        calibration_max_count=calibration_max_count,
        density_low=density_low,
        density_high=density_high,
    )

    # counts_by_class is jsonb, so we can pack the lane-vs-whole-frame
    # comparison in there without a schema migration. Only added when this
    # location actually has a `roi` configured, to keep old rows/locations
    # unchanged.
    counts_payload = dict(detection.counts_by_class)
    if roi_polygon:
        counts_payload["_lane_label"] = calib.get("roi_label", "jalur kanan")
        counts_payload["_all_lanes_total"] = detection.total_vehicle_count
        counts_payload["_all_lanes_by_class"] = detection.total_counts_by_class

    row = {
        "device_id": device_id,
        "location": location,
        "vehicle_count": detection.vehicle_count,   # = lane count when roi is set, else whole-frame count
        "counts_by_class": counts_payload,
        "light_status": "HIJAU",  # status at the moment of capture; dashboard shows history, not live phase
        "green_duration": program.green_seconds,
        "battery_level": battery_level if battery_level is not None else 100,
        "traffic_status": program.traffic_status,
    }

    try:
        supabase.table("greenflow_logs").insert(row).execute()
    except Exception as e:
        log.error("failed to write to Supabase: %s", e)

    LATEST_PROGRAMS[location] = {
        "red_seconds": program.red_seconds,
        "yellow_seconds": program.yellow_seconds,
        "green_seconds": program.green_seconds,
        "traffic_status": program.traffic_status,
        "vehicle_count": detection.vehicle_count,
        "updated_at": time.time(),
    }

    log.info(
        "[%s] %s vehicles=%d status=%s -> R%d/Y%d/G%d",
        location, device_id, detection.vehicle_count, program.traffic_status,
        program.red_seconds, program.yellow_seconds, program.green_seconds,
    )
    return row, program


# ------------------------- MODE A: ESP32-S3 push ---------------------------

@app.post("/api/ingest/esp32/{device_id}")
async def ingest_esp32(device_id: str, request: Request, location: str = "default"):
    jpeg_bytes = await request.body()
    if not jpeg_bytes:
        raise HTTPException(400, "empty body, expected raw JPEG bytes")

    row, program = process_frame(jpeg_bytes, location=location, device_id=device_id)
    return JSONResponse({
        "ok": True,
        "vehicle_count": row["vehicle_count"],
        "traffic_status": program.traffic_status,
        "red_seconds": program.red_seconds,
        "yellow_seconds": program.yellow_seconds,
        "green_seconds": program.green_seconds,
    })


# ------------------------- MODE B: online CCTV pull -------------------------

def grab_frame_ffmpeg(stream_url: str, timeout: int = 20) -> bytes:
    """Extract a single JPEG frame from a live stream (HLS .m3u8, RTSP, etc.)
    using ffmpeg. Needed for CCTV providers that only expose a live stream
    rather than a plain still-image snapshot endpoint."""
    if not shutil.which(FFMPEG_BIN) and not (FFMPEG_BIN.startswith("/") and __import__("os").path.exists(FFMPEG_BIN)):
        raise RuntimeError(
            f"ffmpeg binary not found at '{FFMPEG_BIN}'. "
            "Install it with: apt install -y ffmpeg"
        )

    cmd = [
        FFMPEG_BIN,
        "-y",
        "-loglevel", "error",
        "-i", stream_url,
        "-frames:v", "1",
        "-q:v", "2",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if result.returncode != 0 or not result.stdout:
        stderr = result.stderr.decode(errors="ignore")[:300]
        raise RuntimeError(f"ffmpeg failed to grab frame: {stderr}")
    return result.stdout


def fetch_cctv_frame(cfg: dict) -> bytes:
    """Fetch one frame for a mode: cctv location, using whichever method
    matches cctv_stream_type:
      - "hls" or "mjpeg": grabbed via ffmpeg (handles live multi-frame
        streams, including MJPEG/multipart proxies like many city
        Diskominfo CCTV feeds).
      - anything else (default "snapshot"): plain GET, URL returns one
        JPEG directly."""
    url = cfg["cctv_snapshot_url"]
    if cfg.get("cctv_stream_type") in ("hls", "mjpeg"):
        return grab_frame_ffmpeg(url, timeout=cfg.get("cctv_ffmpeg_timeout", 20))

    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.content


async def cctv_poll_loop():
    """Background task: for every location configured with mode: cctv,
    fetch a fresh snapshot on its own interval and run it through the same
    pipeline as the ESP32-S3 push path."""
    while True:
        for location, cfg in CONFIG.get("locations", {}).items():
            if cfg.get("mode") != "cctv":
                continue
            try:
                jpeg_bytes = await asyncio.to_thread(fetch_cctv_frame, cfg)
                process_frame(jpeg_bytes, location=location, device_id=cfg.get("device_id", location))
            except Exception as e:
                log.error("[%s] CCTV pull failed: %s", location, e)
        await asyncio.sleep(CONFIG.get("cctv_poll_interval_seconds", 10))


@app.on_event("startup")
async def start_background_tasks():
    if any(c.get("mode") == "cctv" for c in CONFIG.get("locations", {}).values()):
        asyncio.create_task(cctv_poll_loop())


# ------------------------- served to the ESP32-C6 ---------------------------

@app.get("/api/light/latest")
async def light_latest(location: str = "default"):
    program = LATEST_PROGRAMS.get(location)
    if program is None:
        # Fall back to the baseline [CONTOHFORMATLALULINTAS] program until
        # the first frame for this location has been processed.
        return {
            "red_seconds": 180,
            "yellow_seconds": 30,
            "green_seconds": 420,
            "traffic_status": "LANCAR",
            "vehicle_count": 0,
            "note": "default baseline, no data processed yet for this location",
        }
    return program


@app.get("/api/health")
async def health():
    return {"ok": True, "locations_tracked": list(LATEST_PROGRAMS.keys())}


# ------------------------- served to the dashboard --------------------------

@app.get("/api/cameras")
async def list_cameras():
    """List CCTV-mode locations from config.yaml, so the web dashboard can
    render a live-view panel without needing access to config.yaml itself.
    Only the stream URL is exposed (already a public camera feed, not a
    secret) — never the Supabase keys or other server-only config."""
    cameras = []
    for location, cfg in CONFIG.get("locations", {}).items():
        if cfg.get("mode") == "cctv":
            cameras.append({
                "location": location,
                "stream_url": cfg.get("cctv_snapshot_url"),
                "stream_type": cfg.get("cctv_stream_type", "snapshot"),
                "has_roi": bool(cfg.get("roi")),
            })
    return {"cameras": cameras}


# ------------------------- ROI (lane) calibration ---------------------------

@app.get("/api/debug/roi-preview")
async def roi_preview(location: str):
    """Grab one live frame for `location` (must be mode: cctv) and draw the
    configured `roi` polygon on top of it, so the normalized [x, y]
    coordinates in config.yaml can be tuned by eye instead of guesswork.

    Open this URL directly in a browser:
        http://<server>/api/debug/roi-preview?location=Nama%20Lokasi

    If `roi` isn't set yet for this location, the raw frame is returned
    unmodified so you can still see what you're calibrating against.
    """
    cfg = location_calibration(location)
    if not cfg or cfg.get("mode") != "cctv":
        raise HTTPException(400, f"location '{location}' tidak ditemukan atau bukan mode: cctv")

    try:
        jpeg_bytes = await asyncio.to_thread(fetch_cctv_frame, cfg)
    except Exception as e:
        raise HTTPException(502, f"gagal ambil frame CCTV: {e}")

    roi = cfg.get("roi")
    if not roi:
        return Response(content=jpeg_bytes, media_type="image/jpeg")

    image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    width, height = image.size

    draw = ImageDraw.Draw(image, "RGBA")
    pixel_points = [(x * width, y * height) for x, y in roi]
    draw.polygon(pixel_points, outline=(255, 0, 0, 255), width=4, fill=(255, 0, 0, 60))
    label = cfg.get("roi_label", "ROI")
    label_x, label_y = pixel_points[0]
    draw.text((label_x + 8, label_y + 8), label, fill=(255, 255, 255, 255))

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85)
    return Response(content=buf.getvalue(), media_type="image/jpeg")
