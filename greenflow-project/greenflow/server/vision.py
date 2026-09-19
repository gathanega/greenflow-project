"""
GreenFlow vision + timing logic.

- detect_vehicles(): runs YOLOv8n over a JPEG frame, returns per-class
  vehicle counts (car/motorcycle/bus/truck).
- classify_density(): turns a vehicle count into LANCAR / PADAT / MACET.
- compute_light_program(): turns a density score into red/yellow/green
  durations, following the [CONTOHFORMATLALULINTAS] baseline given in the
  spec (MERAH 03:00 / KUNING 00:30 / HIJAU 07:00 at maximum congestion).
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


def compute_light_program(
    vehicle_count: int,
    calibration_max_count: int,
    total_cycle_seconds: int = 630,   # 180 + 30 + 420, matches the example
    yellow_seconds: int = 30,         # fixed, matches [CONTOHFORMATLALULINTAS]
    green_min_seconds: int = 60,
    green_max_seconds: int = 420,     # matches HIJAU 07:00 at full congestion
    density_low: int = 5,
    density_high: int = 15,
) -> LightProgram:
    """
    Maps a vehicle count into a red/yellow/green program.

    - density score = vehicle_count / calibration_max_count, clamped to [0,1]
    - green scales linearly between green_min and green_max with the score
    - yellow is fixed
    - red = total_cycle - green - yellow (so at max congestion, with
      green=420 and yellow=30, red=180 -> exactly the given example)
    """
    score = max(0.0, min(1.0, vehicle_count / max(1, calibration_max_count)))
    green = round(green_min_seconds + score * (green_max_seconds - green_min_seconds))
    red = max(30, total_cycle_seconds - green - yellow_seconds)
    status = classify_density(vehicle_count, density_low, density_high)

    return LightProgram(
        red_seconds=red,
        yellow_seconds=yellow_seconds,
        green_seconds=green,
        traffic_status=status,
    )
