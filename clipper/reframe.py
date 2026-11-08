"""Face-aware 16:9 -> 9:16 reframing plan.

Frames are sampled at a few fps; a colour-histogram jump marks a camera cut. Within each
shot the crop window is fixed on the median position of the dominant (largest) face, which
looks like a deliberate edit rather than a jittery tracker. Shots without faces get a
centre crop. The result is a list of shots that render.py turns into an ffmpeg crop
expression, so the actual pixels are processed in a single ffmpeg pass.
"""

from __future__ import annotations

import statistics
from bisect import bisect_left, bisect_right
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
ANALYSIS_VERSION = 3   # bump when the analysis gains fields or changes, so old cached analyses are redone
DETECT_CONFIDENCE = 0.3   # low, so a far-away second person in a wide shot still counts
SPLIT_RATIO = 0.6      # share of the frames in a window that must show two faces for a split screen
SPLIT_WINDOW = 3.0     # seconds the share is measured over
SPLIT_STEP = 0.5       # how often the split is re-checked, in seconds
PANEL_ZOOM = 1.6       # how close each half of a split screen is to its person


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
                base_options=BaseOptions(model_asset_path=str(model)), min_detection_confidence=DETECT_CONFIDENCE
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
    # Frames with two people: time and the x of the left and the right face (the two largest faces).
    two = [[round(s["t"], 3), *sorted(f[0] for f in sorted(s["faces"], key=lambda f: f[1], reverse=True)[:2])]
           for s in samples if len(s["faces"]) >= 2]
    return {
        "width": width, "height": height, "fps": fps, "duration": round(duration, 3),
        "version": ANALYSIS_VERSION,
        "detector": detector.kind,
        "face_coverage": round(with_faces / len(samples), 3) if samples else 0.0,
        "shots": shots,
        "two": two,
        "sample_period": round(step / fps, 4),
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


def split_plan(analysis: dict, trim_start: float, length: float) -> tuple[list[tuple[float, float]], tuple[float, float] | None]:
    """Where two people are on screen together, in clip seconds, and the median x of the left and the right person.
    A moment counts when at least SPLIT_RATIO of the frames within SPLIT_WINDOW seconds show two faces."""
    two = analysis.get("two") or []
    if not two:
        return [], None
    times = [row[0] for row in two]
    per_window = SPLIT_WINDOW / analysis["sample_period"]
    spans, lefts, rights, start, u = [], [], [], None, 0.0
    while u < length:
        t = trim_start + u
        lo = bisect_left(times, t - SPLIT_WINDOW / 2)
        hi = bisect_right(times, t + SPLIT_WINDOW / 2)
        if (hi - lo) / per_window >= SPLIT_RATIO:
            lefts += [two[k][1] for k in range(lo, hi)]
            rights += [two[k][2] for k in range(lo, hi)]
            start = u if start is None else start
        elif start is not None:
            spans.append((round(start, 3), round(u, 3)))
            start = None
        u += SPLIT_STEP
    if start is not None:
        spans.append((round(start, 3), round(length, 3)))
    spans = [(a, b) for a, b in spans if b - a >= 1.0]   # a second or less is flicker, not a conversation
    if not spans or not lefts:
        return [], None
    return spans, (statistics.median(lefts), statistics.median(rights))


def panel_crop(width: int, height: int, out_w: int, out_h: int) -> tuple[int, int]:
    """Source crop for one half of a split screen: a close-up of one person, in the half's shape.
    A full-height crop is too wide and picks up the other person's arm, so the crop is zoomed in."""
    crop_h = round(height / PANEL_ZOOM / 2) * 2
    crop_w = round(crop_h * out_w / (out_h // 2) / 2) * 2
    return min(crop_w, width), crop_h


def panel_x_expr(two: list, trim_start: float, spans: list, width: int, panel_w: int, col: int) -> str:
    """ffmpeg expression for one split panel's x over clip time `t`: it follows that person second by second.
    col 1 is the left person, col 2 the right one. Empty when no one was seen."""
    pts = []
    for a, b in spans:
        u = a
        while u < b:
            seen = [row[col] for row in two if trim_start + u <= row[0] < trim_start + u + 1.0]
            if seen:
                pts.append((u, float(window_left(statistics.median(seen), width, panel_w))))
            u += 1.0
    if not pts:
        return ""
    # A 5-second average stops the small jitter of face detection, and the move between points is gradual.
    xs = [x for _, x in pts]
    smooth = [statistics.fmean(xs[max(0, i - 2): i + 3]) for i in range(len(xs))]
    expr = f"{smooth[-1]:.1f}"
    for i in range(len(pts) - 2, -1, -1):
        (u0, _), (u1, _) = pts[i], pts[i + 1]
        step = f"{smooth[i]:.1f}+({smooth[i + 1] - smooth[i]:.2f})*(t-{u0:.3f})/{u1 - u0:.3f}"
        expr = f"if(lt(t,{u1:.3f}),{step},{expr})"
    return expr


def crop_geometry(width: int, height: int, out_w: int, out_h: int) -> tuple[int, int, int]:
    """Largest crop with the output aspect ratio: (crop_w, crop_h, y)."""
    ratio = out_w / out_h
    if width / height > ratio:
        crop_w, crop_h = int(height * ratio) // 2 * 2, height // 2 * 2
    else:
        crop_w, crop_h = width // 2 * 2, int(width / ratio) // 2 * 2
    return crop_w, crop_h, (height - crop_h) // 2


def window_left(center: float | None, width: int, crop_w: int) -> int:
    """Left edge of the crop window for a face centre (share of the width); centred without a face."""
    c = 0.5 if center is None else center
    return int(min(max(c * width - crop_w / 2, 0), width - crop_w))


def crop_x_expr(shots: list[dict], width: int, crop_w: int, t_offset: float = 0.0) -> str:
    """Piecewise-constant ffmpeg expression for the crop's x position over time `t`.

    `t_offset` is subtracted from shot times (the render trims the padded download).
    """
    def x_for(center):
        return window_left(center, width, crop_w)

    xs = [(s["end"] - t_offset, x_for(s["center_x"])) for s in shots]
    if not xs:
        return str(x_for(None))
    expr = str(xs[-1][1])
    for end, x in reversed(xs[:-1]):
        expr = f"if(lt(t,{end:.3f}),{x},{expr})"
    return expr
