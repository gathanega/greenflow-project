"""
GreenFlow vision + timing logic.

- detect_vehicles(): runs YOLOv8n over a JPEG frame, returns per-class
  vehicle counts (car/motorcycle/bus/truck).
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


@dataclass
class DetectionResult:
    vehicle_count: int
    counts_by_class: dict = field(default_factory=dict)


def detect_vehicles(jpeg_bytes: bytes, conf: float = 0.35) -> DetectionResult:
    image = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    frame = np.array(image)

    model = get_model()
    results = model.predict(frame, conf=conf, verbose=False)

    counts: dict[str, int] = {name: 0 for name in VEHICLE_CLASSES.values()}
    for box in results[0].boxes:
        cls_id = int(box.cls[0])
        if cls_id in VEHICLE_CLASSES:
            counts[VEHICLE_CLASSES[cls_id]] += 1

    total = sum(counts.values())
    return DetectionResult(vehicle_count=total, counts_by_class=counts)


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
