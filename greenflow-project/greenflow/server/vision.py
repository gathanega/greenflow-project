"""
GreenFlow vision + timing logic.

- detect_vehicles(): runs YOLOv8n over a JPEG frame, returns per-class
  vehicle counts (car/motorcycle/bus/truck). Optionally filtered to a
  region of interest (ROI) polygon representing a single lane, so a wide
  CCTV frame covering multiple directions of traffic can be calibrated to
  count only the lane that actually feeds the traffic light being served
  (e.g. "jalur kanan" / right lane). See app.py's `roi` config field and
  GET /api/debug/roi-preview for calibrating the polygon.

  conf/iou are tuned lower/higher than the ultralytics defaults (0.35/0.7)
  specifically for dense Indonesian motorcycle traffic, where several
  motorcycles riding side-by-side overlap heavily in the frame:
    - conf=0.25 (default was 0.35): catches partially-occluded motorcycles
      that would otherwise fall just below the confidence cutoff.
    - iou=0.85 (default was 0.7): NMS threshold - the ultralytics default
      of 0.7 tends to merge two closely-adjacent (but genuinely separate)
      motorcycles into a single detection. Raising it to 0.85 means only
      near-total overlaps get suppressed as duplicates, so tightly-packed
      but distinct motorcycles are more likely to be counted individually.

- classify_density(): turns a vehicle count into LANCAR / PADAT / MACET.
- compute_light_program(): turns that classification into a fixed
  red/yellow/green program (two-state, not interpolated):

    LANCAR            -> MERAH 05:00 / KUNING 00:30 / HIJAU 01:00 (baseline)
    PADAT atau MACET  -> MERAH 02:00 / KUNING 01:00 / HIJAU 03:00 (kondisi padat)

  Begitu kondisi kembali LANCAR, program otomatis kembali ke baseline.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np
from PIL import Image
from ultralytics import YOLO

# COCO class ids we treat as "vehicles"
VEHICLE_CLASSES = {
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}

_model: YOLO | None = None


def get_model() -> YOLO:
    global _model
    if _model is None:
        # yolov8n.pt auto-downloads on first run (needs outbound internet
        # once). Swap for a local .pt path if the VPS has no internet egress.
        _model = YOLO("yolov8n.pt")
    return _model


def _point_in_polygon(x: float, y: float, polygon: Sequence[tuple[float, float]]) -> bool:
    """Standard ray-casting point-in-polygon test. polygon is a list of
    (x, y) points in the SAME units as the point (here: pixels)."""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


@dataclass
class DetectionResult:
    vehicle_count: int
    counts_by_class: dict = field(default_factory=dict)
    # Always the WHOLE-FRAME totals, regardless of ROI filtering above, so
    # callers can show "jalur kanan: X / semua jalur: Y" style comparisons.
    total_vehicle_count: int = 0
    total_counts_by_class: dict = field(default_factory=dict)


def detect_vehicles(
    jpeg_bytes: bytes,
    conf: float = 0.25,
    iou: float = 0.85,
    roi_polygon: Optional[Sequence[tuple[float, float]]] = None,
) -> DetectionResult:
    """
    roi_polygon: optional list of (x, y) points in NORMALIZED coordinates
    (each 0.0-1.0, fraction of frame width/height) describing the lane to
    count. Using normalized coordinates means the same polygon config works
    regardless of the camera's actual resolution. A vehicle is counted as
    "inside" the lane if its bounding-box CENTER point falls inside the
    polygon. When roi_polygon is None, every detected vehicle is counted
    (original whole-frame behaviour).

    conf/iou: see module docstring for why these differ from ultralytics'
    own defaults (0.35 / 0.7) — tuned for dense, overlapping motorcycle
    traffic rather than generic/sparse traffic scenes.
    """
    image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    frame = np.array(image)
    height, width = frame.shape[:2]

    pixel_polygon = None
    if roi_polygon:
        pixel_polygon = [(px * width, py * height) for (px, py) in roi_polygon]

    model = get_model()
    results = model.predict(frame, conf=conf, iou=iou, verbose=False)

    counts_roi: dict[str, int] = {name: 0 for name in VEHICLE_CLASSES.values()}
    counts_all: dict[str, int] = {name: 0 for name in VEHICLE_CLASSES.values()}

    for box in results[0].boxes:
        cls_id = int(box.cls[0])
        if cls_id not in VEHICLE_CLASSES:
            continue
        cls_name = VEHICLE_CLASSES[cls_id]
        counts_all[cls_name] += 1

        if pixel_polygon is None:
            counts_roi[cls_name] += 1
            continue

        x1, y1, x2, y2 = box.xyxy[0].tolist()
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        if _point_in_polygon(cx, cy, pixel_polygon):
            counts_roi[cls_name] += 1

    total_all = sum(counts_all.values())
    total_roi = sum(counts_roi.values())
    used_roi = pixel_polygon is not None

    return DetectionResult(
        vehicle_count=total_roi if used_roi else total_all,
        counts_by_class=counts_roi if used_roi else counts_all,
        total_vehicle_count=total_all,
        total_counts_by_class=counts_all,
    )


def classify_density(vehicle_count: int, low: int, high: int) -> str:
    """low/high thresholds are per-location calibration (see config.yaml)."""
    if vehicle_count < low:
        return "LANCAR"
    if vehicle_count < high:
        return "PADAT"
    return "MACET"


@dataclass
class LightProgram:
    red_seconds: int
    yellow_seconds: int
    green_seconds: int
    traffic_status: str


# Dua kondisi tetap (bukan interpolasi linear seperti versi sebelumnya).
BASELINE_PROGRAM = {"red": 300, "yellow": 30, "green": 60}    # 05:00 / 00:30 / 01:00
CONGESTED_PROGRAM = {"red": 120, "yellow": 60, "green": 180}  # 02:00 / 01:00 / 03:00


def compute_light_program(
    vehicle_count: int,
    calibration_max_count: int = 20,  # dipertahankan untuk kompatibilitas signature, tidak dipakai di logika baru
    density_low: int = 5,
    density_high: int = 15,
) -> LightProgram:
    """
    Menentukan program lampu berdasarkan status kepadatan saja (dua
    kondisi tetap), bukan skor kontinu:

    - status LANCAR -> program baseline (MERAH 05:00 / KUNING 00:30 / HIJAU 01:00)
    - status PADAT atau MACET -> program padat (MERAH 02:00 / KUNING 01:00 / HIJAU 03:00)

    calibration_max_count tidak lagi memengaruhi durasi lampu di logika
    ini, tapi parameter tetap diterima supaya app.py tidak perlu diubah.
    """
    status = classify_density(vehicle_count, density_low, density_high)

    program = BASELINE_PROGRAM if status == "LANCAR" else CONGESTED_PROGRAM

    return LightProgram(
        red_seconds=program["red"],
        yellow_seconds=program["yellow"],
        green_seconds=program["green"],
        traffic_status=status,
    )
