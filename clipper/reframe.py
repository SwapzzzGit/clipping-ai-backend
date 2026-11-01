"""Face-aware 16:9 -> 9:16 reframing plan.

Frames are sampled at a few fps; a colour-histogram jump marks a camera cut. Within each
shot the crop window is fixed on the median position of the dominant (largest) face, which
looks like a deliberate edit rather than a jittery tracker. Shots without faces get a
centre crop. The result is a list of shots that render.py turns into an ffmpeg crop
expression, so the actual pixels are processed in a single ffmpeg pass.
"""

from __future__ import annotations

import statistics
import urllib.request
from pathlib import Path

import cv2
import numpy as np

from clipper.util import log

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_detector/"
    "blaze_face_short_range/float16/latest/blaze_face_short_range.tflite"
)
DETECT_WIDTH = 640
MIN_SHOT_SEC = 0.6


class FaceDetector:
    """MediaPipe BlazeFace (Tasks API), falling back to OpenCV's bundled Haar cascade."""

    def __init__(self, model_dir: Path):
        self.kind = "haar"
        self._mp = None
        try:
            import mediapipe as mp
            from mediapipe.tasks.python import BaseOptions, vision

            model = model_dir / "blaze_face_short_range.tflite"
            if not model.is_file():
                model_dir.mkdir(parents=True, exist_ok=True)
                log.info("downloading MediaPipe face model")
                urllib.request.urlretrieve(MODEL_URL, model)
            opts = vision.FaceDetectorOptions(
                base_options=BaseOptions(model_asset_path=str(model)), min_detection_confidence=0.5
            )
            self._mp = (mp, vision.FaceDetector.create_from_options(opts))
            self.kind = "mediapipe"
        except Exception as e:
            log.warning("MediaPipe face detector unavailable (%s); using OpenCV Haar cascade", e)
            self._haar = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    def detect(self, bgr: np.ndarray) -> list[tuple[float, float]]:
        """Return [(center_x, width)] as fractions of the frame width."""
        h, w = bgr.shape[:2]
        if self._mp:
            mp, det = self._mp
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            res = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
            return [((d.bounding_box.origin_x + d.bounding_box.width / 2) / w, d.bounding_box.width / w)
                    for d in res.detections]
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        faces = self._haar.detectMultiScale(gray, 1.1, 5, minSize=(max(24, w // 30), max(24, w // 30)))
        return [((x + fw / 2) / w, fw / w) for x, y, fw, fh in faces]

    def close(self):
        if self._mp:
            self._mp[1].close()


def _hist(frame: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
    return cv2.normalize(hist, hist).flatten()


def analyze(path: Path, detector: FaceDetector, sample_fps: float, cut_threshold: float) -> dict:
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width, height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    step = max(1, round(fps / sample_fps))
    samples, prev_hist, cuts, idx = [], None, [0.0], 0
    while True:
        if not cap.grab():
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            t = idx / fps
            small = cv2.resize(frame, (DETECT_WIDTH, round(DETECT_WIDTH * height / width)))
            hist = _hist(small)
            if prev_hist is not None and cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA) > cut_threshold:
                cuts.append(t)
            prev_hist = hist
            samples.append({"t": t, "faces": detector.detect(small)})
        idx += 1
    cap.release()
    duration = idx / fps

    shots = []
    for i, start in enumerate(cuts):
        end = cuts[i + 1] if i + 1 < len(cuts) else duration
        in_shot = [s for s in samples if start <= s["t"] < end]
        # Dominant face per sample = the largest one (closest to camera / main speaker).
        centers = [max(s["faces"], key=lambda f: f[1])[0] for s in in_shot if s["faces"]]
        shots.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "center_x": round(statistics.median(centers), 4) if centers else None,
            "face_ratio": round(len(centers) / len(in_shot), 3) if in_shot else 0.0,
        })
    shots = _merge_short_shots(shots)
    with_faces = sum(1 for s in samples if s["faces"])
    return {
        "width": width, "height": height, "fps": fps, "duration": round(duration, 3),
        "detector": detector.kind,
        "face_coverage": round(with_faces / len(samples), 3) if samples else 0.0,
        "shots": shots,
    }


def _merge_short_shots(shots: list[dict]) -> list[dict]:
    """Fold flash cuts (shorter than MIN_SHOT_SEC) into the previous shot."""
    out: list[dict] = []
    for s in shots:
        if out and (s["end"] - s["start"] < MIN_SHOT_SEC or s["center_x"] == out[-1]["center_x"]):
            out[-1]["end"] = s["end"]
            if out[-1]["center_x"] is None:
                out[-1]["center_x"] = s["center_x"]
        else:
            out.append(dict(s))
    return out


def crop_geometry(width: int, height: int, out_w: int, out_h: int) -> tuple[int, int, int]:
    """Largest crop with the output aspect ratio: (crop_w, crop_h, y)."""
    ratio = out_w / out_h
    if width / height > ratio:
        crop_w, crop_h = int(height * ratio) // 2 * 2, height // 2 * 2
    else:
        crop_w, crop_h = width // 2 * 2, int(width / ratio) // 2 * 2
    return crop_w, crop_h, (height - crop_h) // 2


def crop_x_expr(shots: list[dict], width: int, crop_w: int, t_offset: float = 0.0) -> str:
    """Piecewise-constant ffmpeg expression for the crop's x position over time `t`.

    `t_offset` is subtracted from shot times (the render trims the padded download).
    """
    def x_for(center):
        c = 0.5 if center is None else center
        return int(min(max(c * width - crop_w / 2, 0), width - crop_w))

    xs = [(s["end"] - t_offset, x_for(s["center_x"])) for s in shots]
    if not xs:
        return str(x_for(None))
    expr = str(xs[-1][1])
    for end, x in reversed(xs[:-1]):
        expr = f"if(lt(t,{end:.3f}),{x},{expr})"
    return expr
