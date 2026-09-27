"""
GreenFlow VPS server (FastAPI).

Two ingestion modes, both feeding the same pipeline (detect -> classify ->
compute light program -> store -> serve):

  MODE A ("esp32"): the ESP32-S3-VROOM-1 camera node POSTs JPEG frames to
      POST /api/ingest/esp32/{device_id}?location=...

  MODE B ("cctv"): this server itself periodically pulls a snapshot from an
      existing online CCTV API/stream URL, for locations configured with
      mode: cctv in config.yaml. No ESP32-S3 needed for those locations.
      Two CCTV source types are supported (set with cctv_stream_type):
        - "snapshot" (default): the URL returns a plain JPEG directly.
        - "hls": the URL is a live HLS (.m3u8) stream, and a single frame
          is grabbed from it using ffmpeg (needed for camera providers
          that only expose a live stream, no still-image endpoint).

Either mode ends up calling process_frame(), which writes one row to the
Supabase `greenflow_logs` table (used by the web dashboard) and also keeps
the latest program in memory per location, served to the ESP32-C6 traffic
light controllers via:

  GET /api/light/latest?location=...
"""

from __future__ import annotations

import asyncio
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
from fastapi.responses import JSONResponse
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

    detection = detect_vehicles(jpeg_bytes)
    program = compute_light_program(
        vehicle_count=detection.vehicle_count,
        calibration_max_count=calibration_max_count,
        density_low=density_low,
        density_high=density_high,
    )

    row = {
        "device_id": device_id,
        "location": location,
        "vehicle_count": detection.vehicle_count,
        "counts_by_class": detection.counts_by_class,
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
    matches cctv_stream_type ("hls" -> ffmpeg, otherwise -> plain GET)."""
    url = cfg["cctv_snapshot_url"]
    if cfg.get("cctv_stream_type") == "hls":
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
