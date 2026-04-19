#!/usr/bin/env python3
"""
VideoPhotoSlide
A safer, cleaner slideshow generator derived from make_slideshow2.py.
"""

import argparse
import importlib
import json
import math
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass, replace as dataclass_replace
from datetime import datetime
from functools import lru_cache
from multiprocessing import Pool, cpu_count
from pathlib import Path
from typing import Any, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif"}
VID_EXTS = {".mp4", ".mov"}

QUALITY_PRESETS = {
    "draft": {"fps": 24, "blur_strength": 12, "bitrate": "8M", "description": "Fast preview"},
    "standard": {"fps": 30, "blur_strength": 18, "bitrate": "15M", "description": "Balanced"},
    "high": {"fps": 30, "blur_strength": 22, "bitrate": "25M", "description": "Best quality"},
    "youtube": {"fps": 30, "blur_strength": 22, "bitrate": "youtube", "description": "YouTube recommended"},
    "max": {"fps": 30, "blur_strength": 22, "bitrate": "max", "description": "Maximum YouTube bitrate"},
}

RESOLUTION_PRESETS = {
    "1080p": {"16x9": (1920, 1080), "9x16": (1080, 1920), "description": "Full HD"},
    "1440p": {"16x9": (2560, 1440), "9x16": (1440, 2560), "description": "2K / Quad HD"},
    "4k": {"16x9": (3840, 2160), "9x16": (2160, 3840), "description": "Ultra HD"},
    "8k": {"16x9": (7680, 4320), "9x16": (4320, 7680), "description": "Maximum experimental"},
}

RESOLUTION_ALIASES = {
    "2160p": "4k",
    "4320p": "8k",
}

RESOLUTION_CHOICES = sorted(set(RESOLUTION_PRESETS) | set(RESOLUTION_ALIASES))

YOUTUBE_SDR_BITRATES = {
    "1080p": {"standard_fps": "8M", "high_fps": "12M", "max": "12M"},
    "1440p": {"standard_fps": "16M", "high_fps": "24M", "max": "24M"},
    "4k": {"standard_fps": "45M", "high_fps": "68M", "max": "68M"},
    "8k": {"standard_fps": "160M", "high_fps": "240M", "max": "240M"},
}

MOTION_PRESETS = {
    "none": {"ken": 0.0, "parallax_ratio": 0.0, "description": "No extra motion"},
    "kenburns": {"ken": 0.0015, "parallax_ratio": 0.0, "description": "Subtle eased zoom"},
    "parallax": {"ken": 0.0, "parallax_ratio": 0.0028, "description": "Very subtle depth drift"},
    "both": {"ken": 0.0015, "parallax_ratio": 0.0028, "description": "Subtle eased zoom plus depth drift"},
}

# Curated transition set for modern, restrained motion language.
PRO_TRANSITIONS = ["fade", "smoothleft", "smoothright"]
VALID_TRANSITIONS = {
    "auto",
    "fade",
    "fadeblack",
    "fadewhite",
    "distance",
    "wipeleft",
    "wiperight",
    "wipeup",
    "wipedown",
    "slideleft",
    "slideright",
    "slideup",
    "slidedown",
    "smoothleft",
    "smoothright",
    "smoothup",
    "smoothdown",
    "circlecrop",
    "rectcrop",
    "circleclose",
    "circleopen",
    "horzclose",
    "horzopen",
    "vertclose",
    "vertopen",
    "diagbl",
    "diagbr",
    "diagtl",
    "diagtr",
    "hlslice",
    "hrslice",
    "vuslice",
    "vdslice",
    "dissolve",
    "pixelize",
    "radial",
    "hblur",
    "wipetl",
    "wipetr",
    "wipebl",
    "wipebr",
    "squeezeh",
    "squeezev",
    "zoomin",
}


@dataclass
class PhotoInfo:
    path: Path
    width: int
    height: int
    aspect_ratio: float
    is_landscape: bool
    orientation: str
    datetime_taken: Optional[datetime] = None
    gps_coords: Optional[Tuple[float, float]] = None
    camera_make: str = ""
    camera_model: str = ""
    focal_point: Optional[Tuple[float, float]] = None
    altitude_m: Optional[float] = None
    location_name: Optional[str] = None
    is_video: bool = False
    video_duration: Optional[float] = None
    has_audio: bool = False

    def __post_init__(self):
        if self.aspect_ratio > 1.1:
            self.is_landscape = True
            self.orientation = "landscape"
        elif self.aspect_ratio < 0.9:
            self.is_landscape = False
            self.orientation = "portrait"
        else:
            self.is_landscape = False
            self.orientation = "square"


@dataclass
class RenderConfig:
    fps: int = 30
    sec: float = 2.8
    xfade: float = 0.7
    transition: str = "auto"
    blur_strength: int = 18
    bitrate: str = "15M"
    quality_name: str = "standard"
    encoder: str = "h264_videotoolbox"
    motion_style: str = "none"
    ken_override: Optional[float] = None
    parallax_override: Optional[int] = None
    motion_seed: int = 0
    rhythm_strength: float = 0.12
    audio_path: Optional[Path] = None
    audio_offset: float = 0.0
    audio_fade: Optional[float] = None
    clip_grade: str = "full"
    clip_audio: str = "mute"
    clip_max_sec: Optional[float] = None


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def normalize_resolution(resolution: str) -> str:
    normalized = resolution.strip().lower()
    normalized = RESOLUTION_ALIASES.get(normalized, normalized)
    if normalized not in RESOLUTION_PRESETS:
        supported = ", ".join(RESOLUTION_CHOICES)
        raise argparse.ArgumentTypeError(f"must be one of: {supported}")
    return normalized


def dimensions_for_target(fmt: str, resolution: str) -> Tuple[int, int]:
    normalized = normalize_resolution(resolution)
    try:
        return RESOLUTION_PRESETS[normalized][fmt]  # type: ignore[return-value]
    except KeyError as exc:
        raise ValueError(f"Unsupported format/resolution combination: {fmt}/{resolution}") from exc


def resolve_output_bitrate(
    quality_name: str,
    resolution: str,
    fps: int,
    bitrate_override: Optional[str] = None,
) -> str:
    if bitrate_override is not None:
        return bitrate_override

    normalized_resolution = normalize_resolution(resolution)
    preset_bitrate = QUALITY_PRESETS[quality_name]["bitrate"]
    if preset_bitrate == "youtube":
        frame_rate_key = "high_fps" if fps > 30 else "standard_fps"
        return YOUTUBE_SDR_BITRATES[normalized_resolution][frame_rate_key]
    if preset_bitrate == "max":
        return YOUTUBE_SDR_BITRATES[normalized_resolution]["max"]
    return preset_bitrate


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def progress_print(enabled: bool, message: str) -> None:
    if enabled:
        print(message, flush=True)


_MEDIAPIPE_FACE_DETECTOR: Optional[Any] = None
_MEDIAPIPE_POSE_DETECTOR: Optional[Any] = None
_MEDIAPIPE_FACE_MODEL_PATH: Optional[Path] = None
_MEDIAPIPE_POSE_MODEL_PATH: Optional[Path] = None

SMART_FOCUS_FACE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_detector/"
    "blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
)
SMART_FOCUS_POSE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_lite/float16/1/pose_landmarker_lite.task"
)
SMART_FOCUS_FACE_MODEL_NAME = "blaze_face_short_range.tflite"
SMART_FOCUS_POSE_MODEL_NAME = "pose_landmarker_lite.task"


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _ffmpeg_number(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _download_smart_focus_model(url: str, dest: Path, label: str, show_progress: bool = False) -> None:
    ensure_dir(dest.parent)
    tmp = dest.with_suffix(dest.suffix + ".download")
    try:
        progress_print(show_progress, f"[phase prep] downloading {label} model -> {dest}")
        with urllib.request.urlopen(url, timeout=60) as response, tmp.open("wb") as fh:
            shutil.copyfileobj(response, fh)
        tmp.replace(dest)
    except Exception as exc:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise SystemExit(
            f"Smart focus could not download the {label} model from {url}. "
            f"Download it manually and pass its path with the matching --smart-focus-*model flag."
        ) from exc


def _resolve_smart_focus_model(
    explicit_path: Optional[str],
    default_path: Path,
    default_url: str,
    label: str,
    show_progress: bool = False,
) -> Path:
    if explicit_path:
        path = Path(explicit_path).expanduser()
        if not path.is_file():
            raise SystemExit(f"--smart-focus-{label}-model file not found: {path}")
        return path.resolve()

    path = default_path.expanduser()
    if not path.is_file():
        _download_smart_focus_model(default_url, path, label, show_progress=show_progress)
    return path.resolve()


def resolve_smart_focus_model_paths(
    model_dir: Path,
    face_model: Optional[str] = None,
    pose_model: Optional[str] = None,
    show_progress: bool = False,
) -> Tuple[Path, Path]:
    face_path = _resolve_smart_focus_model(
        face_model,
        model_dir / SMART_FOCUS_FACE_MODEL_NAME,
        SMART_FOCUS_FACE_MODEL_URL,
        "face",
        show_progress=show_progress,
    )
    pose_path = _resolve_smart_focus_model(
        pose_model,
        model_dir / SMART_FOCUS_POSE_MODEL_NAME,
        SMART_FOCUS_POSE_MODEL_URL,
        "pose",
        show_progress=show_progress,
    )
    return face_path, pose_path


def configure_smart_focus_models(face_model_path: Path, pose_model_path: Path) -> None:
    global _MEDIAPIPE_FACE_MODEL_PATH, _MEDIAPIPE_POSE_MODEL_PATH
    global _MEDIAPIPE_FACE_DETECTOR, _MEDIAPIPE_POSE_DETECTOR

    face_model_path = face_model_path.expanduser().resolve()
    pose_model_path = pose_model_path.expanduser().resolve()
    if (_MEDIAPIPE_FACE_MODEL_PATH, _MEDIAPIPE_POSE_MODEL_PATH) != (face_model_path, pose_model_path):
        _MEDIAPIPE_FACE_DETECTOR = None
        _MEDIAPIPE_POSE_DETECTOR = None
    _MEDIAPIPE_FACE_MODEL_PATH = face_model_path
    _MEDIAPIPE_POSE_MODEL_PATH = pose_model_path


def _get_configured_smart_focus_models() -> Tuple[Path, Path]:
    if _MEDIAPIPE_FACE_MODEL_PATH is None or _MEDIAPIPE_POSE_MODEL_PATH is None:
        raise SystemExit(
            "Smart focus requires MediaPipe Tasks model assets. "
            "Run through main() so models can be resolved, or call configure_smart_focus_models() first."
        )
    return _MEDIAPIPE_FACE_MODEL_PATH, _MEDIAPIPE_POSE_MODEL_PATH


def _get_mediapipe_task_modules() -> Tuple[Any, Any, Any]:
    try:
        mp = importlib.import_module("mediapipe")
        mp_python = importlib.import_module("mediapipe.tasks.python")
        vision = importlib.import_module("mediapipe.tasks.python.vision")
    except ImportError as exc:
        raise SystemExit(
            "Smart focus requires MediaPipe Tasks. Install: pip install mediapipe"
        ) from exc
    return mp, mp_python, vision


def _get_mediapipe_detectors() -> Tuple[Any, Any]:
    global _MEDIAPIPE_FACE_DETECTOR, _MEDIAPIPE_POSE_DETECTOR

    if _MEDIAPIPE_FACE_DETECTOR is not None and _MEDIAPIPE_POSE_DETECTOR is not None:
        return _MEDIAPIPE_FACE_DETECTOR, _MEDIAPIPE_POSE_DETECTOR

    face_model_path, pose_model_path = _get_configured_smart_focus_models()
    _mp, mp_python, vision = _get_mediapipe_task_modules()
    base_options = mp_python.BaseOptions

    try:
        if _MEDIAPIPE_FACE_DETECTOR is None:
            face_options = vision.FaceDetectorOptions(
                base_options=base_options(model_asset_path=str(face_model_path)),
                running_mode=vision.RunningMode.IMAGE,
                min_detection_confidence=0.5,
            )
            _MEDIAPIPE_FACE_DETECTOR = vision.FaceDetector.create_from_options(face_options)
        if _MEDIAPIPE_POSE_DETECTOR is None:
            pose_options = vision.PoseLandmarkerOptions(
                base_options=base_options(model_asset_path=str(pose_model_path)),
                running_mode=vision.RunningMode.IMAGE,
                num_poses=1,
                min_pose_detection_confidence=0.5,
                min_pose_presence_confidence=0.5,
                min_tracking_confidence=0.5,
                output_segmentation_masks=False,
            )
            _MEDIAPIPE_POSE_DETECTOR = vision.PoseLandmarker.create_from_options(pose_options)
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(f"Smart focus could not initialize MediaPipe Tasks: {exc}") from exc

    return _MEDIAPIPE_FACE_DETECTOR, _MEDIAPIPE_POSE_DETECTOR


def _pil_to_mediapipe_image(image: Image.Image) -> Any:
    mp, _mp_python, _vision = _get_mediapipe_task_modules()
    image_rgb = np.asarray(image.convert("RGB"))
    return mp.Image(image_format=mp.ImageFormat.SRGB, data=image_rgb)


def _focus_from_face_result(face_result: Any, width: int, height: int) -> Optional[Tuple[float, float]]:
    detections = getattr(face_result, "detections", None) or []
    if not detections:
        return None

    def detection_score(detection: Any) -> float:
        categories = getattr(detection, "categories", None) or []
        scores = [getattr(category, "score", 0.0) or 0.0 for category in categories]
        return max(scores) if scores else 0.0

    best = max(detections, key=detection_score)
    keypoints = getattr(best, "keypoints", None) or []
    for keypoint in keypoints:
        label = (getattr(keypoint, "label", "") or "").lower()
        x = getattr(keypoint, "x", None)
        y = getattr(keypoint, "y", None)
        if "nose" in label and x is not None and y is not None:
            return clamp(x, 0.0, 1.0), clamp(y, 0.0, 1.0)

    bbox = getattr(best, "bounding_box", None)
    if bbox is None or width <= 0 or height <= 0:
        return None
    face_x = (bbox.origin_x + (bbox.width / 2.0)) / width
    face_y = (bbox.origin_y + (bbox.height * 0.38)) / height
    return clamp(face_x, 0.0, 1.0), clamp(face_y, 0.0, 1.0)


def _landmark_is_usable(landmark: Any) -> bool:
    if getattr(landmark, "x", None) is None or getattr(landmark, "y", None) is None:
        return False
    visibility = getattr(landmark, "visibility", None)
    presence = getattr(landmark, "presence", None)
    if visibility is not None and visibility < 0.5:
        return False
    if presence is not None and presence < 0.5:
        return False
    return True


def _focus_from_pose_result(pose_result: Any) -> Optional[Tuple[float, float]]:
    poses = getattr(pose_result, "pose_landmarks", None) or []
    if not poses:
        return None

    landmarks = poses[0]
    visible = [lm for lm in landmarks if _landmark_is_usable(lm)]
    if not visible:
        return None

    nose = landmarks[0] if landmarks else None
    if nose is not None and _landmark_is_usable(nose):
        return clamp(nose.x, 0.0, 1.0), clamp(nose.y, 0.0, 1.0)

    shoulder_ids = (11, 12)
    shoulders = [
        landmarks[idx]
        for idx in shoulder_ids
        if idx < len(landmarks) and _landmark_is_usable(landmarks[idx])
    ]
    if shoulders:
        x = sum(lm.x for lm in shoulders) / len(shoulders)
        y = sum(lm.y for lm in shoulders) / len(shoulders)
        return clamp(x, 0.0, 1.0), clamp(y, 0.0, 1.0)

    x = sum(lm.x for lm in visible) / len(visible)
    y = sum(lm.y for lm in visible) / len(visible)
    return clamp(x, 0.0, 1.0), clamp(y, 0.0, 1.0)


def detect_subject_focus(image: Image.Image) -> Optional[Tuple[float, float]]:
    face_detector, pose_detector = _get_mediapipe_detectors()
    mp_image = _pil_to_mediapipe_image(image)

    face_focus = _focus_from_face_result(
        face_detector.detect(mp_image),
        width=image.width,
        height=image.height,
    )
    if face_focus is not None:
        return face_focus

    return _focus_from_pose_result(pose_detector.detect(mp_image))


def probe_video_clip(path: Path) -> Optional["PhotoInfo"]:
    """Use ffprobe to extract video metadata. Returns a PhotoInfo with is_video=True, or None on failure."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()[-1] if result.stderr else "no details"
            print(f"Skipping video (ffprobe exit {result.returncode}): {path.name} — {detail}", file=sys.stderr)
            return None
        data = json.loads(result.stdout)
    except Exception as e:
        print(f"Skipping video (probe failed): {path.name} — {e}", file=sys.stderr)
        return None

    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    audio_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "audio"),
        None,
    )
    if video_stream is None:
        print(f"Skipping video (no video stream found): {path.name}", file=sys.stderr)
        return None

    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))
    if width <= 0 or height <= 0:
        print(f"Skipping video (invalid dimensions {width}x{height}): {path.name}", file=sys.stderr)
        return None

    raw_dur = video_stream.get("duration") or data.get("format", {}).get("duration")
    if not raw_dur:
        print(f"Skipping video (no duration in metadata): {path.name}", file=sys.stderr)
        return None
    try:
        duration = float(raw_dur)
    except ValueError:
        print(f"Skipping video (unreadable duration {raw_dur!r}): {path.name}", file=sys.stderr)
        return None
    if duration <= 0:
        print(f"Skipping video (zero or negative duration {duration}s): {path.name}", file=sys.stderr)
        return None

    ar = width / height

    # creation_time: prefer MP4/MOV container tags, fall back to file mtime.
    datetime_taken: Optional[datetime] = None
    fmt_tags = {k.lower(): v for k, v in data.get("format", {}).get("tags", {}).items()}
    ct = fmt_tags.get("creation_time") or fmt_tags.get("com.apple.quicktime.creationdate")
    if ct:
        for fmt_str in (
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                datetime_taken = datetime.strptime(ct, fmt_str)
                break
            except ValueError:
                pass
    if datetime_taken is None:
        try:
            datetime_taken = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            pass

    return PhotoInfo(
        path=path,
        width=width,
        height=height,
        aspect_ratio=ar,
        is_landscape=ar > 1.1,
        orientation="landscape" if ar > 1.1 else ("portrait" if ar < 0.9 else "square"),
        datetime_taken=datetime_taken,
        is_video=True,
        video_duration=duration,
        has_audio=audio_stream is not None,
    )


def estimate_duration_variable(photo_durations: List[float], xfade_seconds: float) -> float:
    if not photo_durations:
        return 0.0
    return sum(photo_durations) - max(0, len(photo_durations) - 1) * xfade_seconds


@lru_cache(maxsize=None)
def ffmpeg_has_encoder(encoder_name: str) -> bool:
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    out = (probe.stdout or "") + "\n" + (probe.stderr or "")
    return encoder_name in out


def x264_tuning_for_quality(quality_name: str) -> Tuple[str, str]:
    if quality_name == "draft":
        return "28", "veryfast"
    if quality_name == "max":
        return "18", "slow"
    if quality_name == "high":
        return "20", "slow"
    if quality_name == "youtube":
        return "20", "slow"
    return "22", "fast"


def resolve_motion_values(
    motion_style: str,
    ken_override: Optional[float],
    parallax_override: Optional[int],
    frame_min_dim: int,
    seconds_per_photo: float,
) -> Tuple[float, int]:
    preset = MOTION_PRESETS[motion_style]
    ken = preset["ken"] if ken_override is None else ken_override
    default_px = int(round(frame_min_dim * preset["parallax_ratio"]))
    parallax_px = default_px if parallax_override is None else parallax_override

    if seconds_per_photo < 1.4:
        return 0.0, 0

    ken = max(0.0, min(0.03, ken))
    max_px = int(round(frame_min_dim * 0.02))
    parallax_px = max(0, min(max_px, parallax_px))
    return ken, parallax_px


def build_photo_durations(
    num_photos: int,
    base_sec: float,
    xfade: float,
    rhythm_strength: float,
    seed: int,
) -> List[float]:
    """Build slight per-shot duration variation for more editorial pacing."""
    if num_photos <= 0:
        return []

    strength = max(0.0, min(0.25, rhythm_strength))
    min_sec = max(xfade + 0.25, 0.6)
    if strength == 0.0:
        return [max(base_sec, min_sec)] * num_photos

    rng = random.Random(seed + 911)
    durations = []
    for i in range(num_photos):
        # Smoothly varying cadence avoids abrupt timing jumps between adjacent shots.
        wave = 0.5 * (1.0 + math.sin((i / max(1, num_photos - 1)) * math.pi))
        jitter = (rng.random() - 0.5) * 0.8
        delta = ((wave - 0.5) + jitter) * strength
        sec = base_sec * (1.0 + delta)
        durations.append(max(min_sec, sec))
    return durations


def _media_start_times(media_durations: List[float], xfade: float) -> List[float]:
    """Compute the timeline start time of each media item accounting for xfade overlap."""
    if not media_durations:
        return []
    starts = [0.0]
    for i in range(1, len(media_durations)):
        starts.append(starts[-1] + media_durations[i - 1] - xfade)
    return starts


def build_media_durations(
    infos: List[Optional["PhotoInfo"]],
    base_sec: float,
    xfade: float,
    rhythm_strength: float,
    seed: int,
    clip_max_sec: Optional[float] = None,
) -> List[float]:
    """Per-item durations: video clips use natural duration; photos use rhythm-varied base_sec.

    The rhythm wave is calculated across all len(infos) slots regardless of type,
    so photo indices shift when clips are interleaved. This is intentional: the
    wave is used only for photo slots and clip slots are overwritten with their
    natural duration, so the resulting pacing is still musically coherent.
    clip_max_sec caps clip durations when set.
    """
    photo_durations = build_photo_durations(len(infos), base_sec, xfade, rhythm_strength, seed)
    min_clip_dur = max(xfade + 0.1, 0.5)
    result = []
    for dur, info in zip(photo_durations, infos):
        if info and info.is_video and info.video_duration is not None:
            clip_dur = max(info.video_duration, min_clip_dur)
            if clip_max_sec is not None:
                clip_dur = min(clip_dur, max(clip_max_sec, min_clip_dur))
            result.append(clip_dur)
        else:
            result.append(dur)
    return result


def resolve_transition_name(transition_mode: str, index: int) -> str:
    """Resolve transition name for a segment index (1-based image index)."""
    if transition_mode == "auto":
        return PRO_TRANSITIONS[(index - 1) % len(PRO_TRANSITIONS)]
    return transition_mode


def _rational_to_float(val) -> float:
    """Convert an EXIF rational value to float.

    Pillow >= 8 returns IFDRational objects (float-like); older versions or
    some TIFF-based formats return (numerator, denominator) tuples.
    """
    if isinstance(val, tuple):
        return val[0] / val[1] if val[1] != 0 else 0.0
    return float(val)


def parse_gps_coord(gps_ref, gps_data):
    try:
        d = _rational_to_float(gps_data[0])
        m = _rational_to_float(gps_data[1])
        s = _rational_to_float(gps_data[2])
        coord = d + m / 60.0 + s / 3600.0
        if gps_ref in ("S", "W"):
            coord = -coord
        return coord
    except Exception:
        return None


def extract_exif_datetime(image: Image.Image) -> Optional[datetime]:
    try:
        exif_data = image.getexif()
        if not exif_data:
            return None
        dt_str = exif_data.get(306) or exif_data.get(36867)
        if dt_str:
            return datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None
    return None


def _gps_ifd(image: Image.Image) -> Optional[dict]:
    try:
        return image.getexif().get_ifd(34853) or None
    except Exception:
        return None


def _decode_gps_ref(val, default: str) -> str:
    if val is None:
        return default
    return val.decode() if isinstance(val, bytes) else str(val)


def extract_exif_gps(image: Image.Image) -> Optional[Tuple[float, float]]:
    try:
        ifd = _gps_ifd(image)
        if not ifd:
            return None
        lat_ref = _decode_gps_ref(ifd.get(1), "N")
        lon_ref = _decode_gps_ref(ifd.get(3), "E")
        gps_lat = ifd.get(2)
        gps_lon = ifd.get(4)
        if gps_lat and gps_lon:
            lat = parse_gps_coord(lat_ref, gps_lat)
            lon = parse_gps_coord(lon_ref, gps_lon)
            if lat is not None and lon is not None:
                return lat, lon
    except Exception:
        return None
    return None


def extract_exif_altitude(image: Image.Image) -> Optional[float]:
    try:
        ifd = _gps_ifd(image)
        if not ifd:
            return None
        alt = ifd.get(6)  # GPSAltitude
        if alt is None:
            return None
        alt_m = _rational_to_float(alt)
        if ifd.get(5, 0) == 1:  # GPSAltitudeRef: 1 = below sea level
            alt_m = -alt_m
        return alt_m
    except Exception:
        return None


def extract_exif_camera(image: Image.Image) -> Tuple[str, str]:
    try:
        exif_data = image.getexif()
        if not exif_data:
            return "", ""
        make = exif_data.get(271, "")
        model = exif_data.get(272, "")
        if isinstance(make, bytes):
            make = make.decode(errors="ignore")
        if isinstance(model, bytes):
            model = model.decode(errors="ignore")
        return str(make).strip(), str(model).strip()
    except Exception:
        return "", ""


def get_image_metadata(img_path: Path, image: Image.Image, extract_exif: bool, detect_focus: bool) -> Optional[PhotoInfo]:
    try:
        width, height = image.size
        aspect = width / height if height > 0 else 1.0
        dt_taken = extract_exif_datetime(image) if extract_exif else None
        gps = extract_exif_gps(image) if extract_exif else None
        altitude = extract_exif_altitude(image) if extract_exif else None
        make, model = extract_exif_camera(image) if extract_exif else ("", "")
        focal_point = detect_subject_focus(image) if detect_focus else None
        return PhotoInfo(
            path=img_path,
            width=width,
            height=height,
            aspect_ratio=aspect,
            is_landscape=False,
            orientation="",
            datetime_taken=dt_taken,
            gps_coords=gps,
            altitude_m=altitude,
            camera_make=make,
            camera_model=model,
            focal_point=focal_point,
        )
    except Exception:
        return None


def _convert_single_image(args_tuple):
    src, idx, work_dir, extract_exif, detect_focus = args_tuple
    try:
        with Image.open(src) as opened:
            im = ImageOps.exif_transpose(opened)
            info = get_image_metadata(src, im, extract_exif, detect_focus)
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            # Collision-safe normalized names keep deterministic order and uniqueness.
            out = work_dir / f"{idx:06d}_{src.stem}.png"
            im.save(out, format="PNG")
            return idx, out, info, None
    except Exception as e:
        return idx, None, None, f"{src.name}: {e}"


def _init_mediapipe_worker(face_model_path: Optional[str] = None, pose_model_path: Optional[str] = None):
    if face_model_path and pose_model_path:
        configure_smart_focus_models(Path(face_model_path), Path(pose_model_path))
    _get_mediapipe_detectors()


def convert_to_pngs(
    input_dir: Path,
    work_dir: Path,
    extract_exif: bool = True,
    detect_focus: bool = False,
    smart_focus_models: Optional[Tuple[Path, Path]] = None,
    max_workers: int = 0,
    show_progress: bool = False,
) -> Tuple[List[Path], List[Optional[PhotoInfo]], dict]:
    """Convert images to normalized PNGs.

    Returns (png_paths, infos, src_to_png) where src_to_png maps each original
    source Path to its output PNG Path. collect_media uses this mapping directly
    rather than reverse-engineering it from PNG filenames.
    """
    ensure_dir(work_dir)
    files = sorted(input_dir.iterdir(), key=lambda x: natural_key(x.name))
    image_files = [p for p in files if p.suffix.lower() in IMG_EXTS]
    if not image_files:
        return [], [], {}

    args_list = [(p, i, work_dir, extract_exif, detect_focus) for i, p in enumerate(image_files)]
    progress_every = 1 if len(image_files) <= 10 else 5
    progress_label = "convert+focus" if detect_focus else "convert"

    if detect_focus:
        if smart_focus_models is None:
            smart_focus_models = _get_configured_smart_focus_models()
        configure_smart_focus_models(*smart_focus_models)

    def emit_progress(processed_count: int, focus_hits: int) -> None:
        if not show_progress:
            return
        should_print = (
            processed_count == 1
            or processed_count == len(image_files)
            or processed_count % progress_every == 0
        )
        if not should_print:
            return
        pct = (processed_count / len(image_files)) * 100.0
        extra = f", focus hits={focus_hits}" if detect_focus else ""
        print(f"[prep {progress_label} {processed_count}/{len(image_files)} {pct:5.1f}%{extra}]")

    if len(image_files) > 3:
        auto_workers = min(len(image_files), max(2, min(cpu_count(), 6)))
        workers = auto_workers if max_workers <= 0 else max(1, min(len(image_files), max_workers))
        initializer = _init_mediapipe_worker if detect_focus else None
        initargs = tuple(str(p) for p in smart_focus_models) if detect_focus else ()
        with Pool(workers, initializer=initializer, initargs=initargs) as pool:
            processed = 0
            focus_hits = 0
            results = []
            for result in pool.imap_unordered(_convert_single_image, args_list):
                processed += 1
                _idx, _out, info, _err = result
                if info and info.focal_point is not None:
                    focus_hits += 1
                emit_progress(processed, focus_hits)
                results.append(result)
    else:
        results = []
        focus_hits = 0
        for processed, args_item in enumerate(args_list, start=1):
            result = _convert_single_image(args_item)
            _idx, _out, info, _err = result
            if info and info.focal_point is not None:
                focus_hits += 1
            emit_progress(processed, focus_hits)
            results.append(result)

    images, infos, src_to_png = [], [], {}
    for _idx, out, info, err in sorted(results, key=lambda item: item[0]):
        if err:
            print(f"Skipping: {err}", file=sys.stderr)
        else:
            images.append(out)
            infos.append(info)
            src_to_png[image_files[_idx]] = out
    return images, infos, src_to_png


def collect_media(
    input_dir: Path,
    work_dir: Path,
    extract_exif: bool = True,
    detect_focus: bool = False,
    smart_focus_models: Optional[Tuple[Path, Path]] = None,
    max_workers: int = 0,
    show_progress: bool = False,
) -> Tuple[List[Path], List[Optional[PhotoInfo]]]:
    """Collect images + video clips from input_dir in natural sort order.

    Images are converted to normalized PNGs. Video clips are probed via ffprobe
    and kept at their original paths. Returns a unified list maintaining natural
    filename order across both media types.
    """
    ensure_dir(work_dir)
    all_files = sorted(input_dir.iterdir(), key=lambda x: natural_key(x.name))
    img_files = [p for p in all_files if p.suffix.lower() in IMG_EXTS]
    vid_files = [p for p in all_files if p.suffix.lower() in VID_EXTS]
    all_media = [p for p in all_files if p.suffix.lower() in IMG_EXTS | VID_EXTS]

    if not all_media:
        return [], []

    # Process images via existing parallel pipeline.
    # src_to_png maps each original source Path → its PNG output Path directly.
    img_file_result: dict = {}
    if img_files:
        img_paths, img_infos, src_to_png = convert_to_pngs(
            input_dir, work_dir,
            extract_exif=extract_exif,
            detect_focus=detect_focus,
            smart_focus_models=smart_focus_models,
            max_workers=max_workers,
            show_progress=show_progress,
        )
        png_to_info = dict(zip(img_paths, img_infos))
        for src, png_path in src_to_png.items():
            img_file_result[src] = (png_path, png_to_info[png_path])

    # Probe video clips (serial — typically few clips).
    vid_file_result: dict = {}
    for vf in vid_files:
        progress_print(show_progress, f"[prep probe] {vf.name}")
        vinfo = probe_video_clip(vf)
        if vinfo is not None:
            vid_file_result[vf] = (vf, vinfo)

    # Merge in original natural sort order.
    paths: List[Path] = []
    infos: List[Optional[PhotoInfo]] = []
    for p in all_media:
        if p in img_file_result:
            png_path, info = img_file_result[p]
            paths.append(png_path)
            infos.append(info)
        elif p in vid_file_result:
            vid_path, vinfo = vid_file_result[p]
            paths.append(vid_path)
            infos.append(vinfo)

    return paths, infos


def create_label_overlay_png(
    location_lines: List[str],
    altitude_str: Optional[str],
    timestamp_str: Optional[str],
    width: int,
    height: int,
    path: Path,
) -> None:
    """Render location label lines and altitude as a transparent RGBA PNG overlay.

    Layout:
    - Bottom-right: location lines, right-justified, auto-scaled to fit margin.
    - Top-right:    altitude, smaller text, right-justified.
    """
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = max(20, min(width, height) // 40)
    usable_w = width - 2 * margin
    bpad = max(4, margin // 8)  # background box padding around each line

    def load_font(size: int):
        for candidate in (
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/SFNSDisplay.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf",
        ):
            try:
                return ImageFont.truetype(candidate, size, index=0)
            except Exception:
                continue
        return ImageFont.load_default()

    def text_w(txt: str, fnt) -> int:
        bb = draw.textbbox((0, 0), txt, font=fnt)
        return int(bb[2] - bb[0])

    def text_h(txt: str, fnt) -> int:
        bb = draw.textbbox((0, 0), txt, font=fnt)
        return int(bb[3] - bb[1])

    def draw_label_line(txt: str, fnt, x: int, y: int) -> None:
        tw = text_w(txt, fnt)
        th = text_h(txt, fnt)
        draw.rectangle([x - bpad, y - bpad, x + tw + bpad, y + th + bpad], fill=(0, 0, 0, 115))
        draw.text((x, y), txt, font=fnt, fill=(255, 255, 255, 255))  # type: ignore[arg-type]

    # --- Timestamp + altitude: top-right, stacked, smaller text ---
    top_items = [s for s in (timestamp_str, altitude_str) if s]
    if top_items:
        top_size = max(12, height // 90)
        top_font = load_font(top_size)
        top_gap = max(3, top_size // 6)
        top_step = text_h(top_items[0], top_font) + top_gap
        for k, item in enumerate(top_items):
            iw = text_w(item, top_font)
            draw_label_line(item, top_font, width - margin - iw, margin + k * top_step)

    # --- Location lines: bottom-right, auto-scaled, right-justified ---
    if location_lines:
        base_size = max(16, height // 54)
        font = load_font(base_size)
        widest = max(text_w(line, font) for line in location_lines)
        if widest > usable_w:
            scaled_size = max(16, int(base_size * usable_w / widest))
            font = load_font(scaled_size)

        line_gap = max(4, base_size // 8)
        lh = text_h(location_lines[0], font)
        line_step = lh + line_gap
        pad_bottom = max(18, height // 54)
        n = len(location_lines)

        for j, line in enumerate(location_lines):
            lw = text_w(line, font)
            lx = width - margin - lw
            ly = height - pad_bottom - (n - j) * line_step
            draw_label_line(line, font, lx, ly)

    img.save(str(path), "PNG")


def build_filter_for_still(
    i: int,
    width: int,
    height: int,
    fps: int,
    sec: float,
    blur_strength: int = 18,
    ken_strength: float = 0.0,
    parallax_px: int = 0,
    motion_seed: int = 0,
    focal_point: Optional[Tuple[float, float]] = None,
    use_lanczos: bool = False,
) -> str:
    local_rng = random.Random(motion_seed + i * 101)
    scale_flags = ":flags=lanczos" if use_lanczos else ""

    u = f"(t/{sec})"
    smooth_u = f"min(max({u},0),1)"
    ease = f"(6*pow({smooth_u},5)-15*pow({smooth_u},4)+10*pow({smooth_u},3))"
    if ken_strength > 0:
        has_subject_target = focal_point is not None
        focus_x = clamp(focal_point[0], 0.05, 0.95) if focal_point else 0.5
        focus_y = clamp(focal_point[1], 0.05, 0.95) if focal_point else 0.5

        # Convert strength into an across-shot zoom delta, independent of fps.
        # Higher frame rates should feel smoother, not more aggressive.
        zoom_delta = clamp(ken_strength * sec * 18.0, 0.0, 0.18)
        if not has_subject_target:
            zoom_delta *= 0.75
        zoom_in = local_rng.random() >= 0.5
        start_zoom = 1.0 if zoom_in else 1.0 + zoom_delta
        end_zoom = 1.0 + zoom_delta if zoom_in else 1.0
        start_zoom_s = _ffmpeg_number(start_zoom)
        end_zoom_s = _ffmpeg_number(end_zoom)
        focus_x_s = _ffmpeg_number(focus_x)
        focus_y_s = _ffmpeg_number(focus_y)
        zoom = f"({start_zoom_s}*pow(({end_zoom_s}/{start_zoom_s}),{ease}))"

        # Background: blurred fill, same as the no-motion path — ensures portrait photos
        # in landscape output (and any other aspect mismatch) always have a blurred backdrop.
        bg = (
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"boxblur={blur_strength}:1,eq=brightness=-0.04:saturation=1.08,"
            f"fps={fps},trim=duration={sec},setpts=PTS-STARTPTS[bg{i}]"
        )
        # Foreground: first normalize cadence for the still, then animate zoom
        # so ffmpeg evaluates motion on the exact output frame timeline.
        fg = (
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease{scale_flags},setsar=1,"
            f"fps={fps},trim=duration={sec},setpts=PTS-STARTPTS,"
            f"scale=w='iw*{zoom}':h='ih*{zoom}':eval=frame{scale_flags},"
            f"setsar=1[fg{i}]"
        )
        # Overlay: keep the chosen focal point anchored at its neutral fitted-frame
        # position while the foreground zooms. This reads as zooming into/out from
        # the subject instead of sliding the entire photo across the background.
        pan_x = f"(({width}-(w/{zoom}))/2)+({focus_x_s}*(w/{zoom}))-({focus_x_s}*w)"
        pan_y = f"(({height}-(h/{zoom}))/2)+({focus_y_s}*(h/{zoom}))-({focus_y_s}*h)"
        comp = (
            f"[bg{i}][fg{i}]overlay=x='{pan_x}':y='{pan_y}',"
            "eq=contrast=1.05:brightness=0.01:saturation=1.04:gamma=0.98,"
            "vignette=PI/10,"
            "noise=alls=3:allf=t+u,"
            f"format=yuv420p[v{i}]"
        )
        return ";\n".join([bg, fg, comp])

    bg = (
        f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
        f"boxblur={blur_strength}:1,eq=brightness=-0.04:saturation=1.08,"
        f"fps={fps},trim=duration={sec},setpts=PTS-STARTPTS[bg{i}]"
    )
    fg = f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease{scale_flags},setsar=1,fps={fps},trim=duration={sec},setpts=PTS-STARTPTS[fg{i}]"
    if parallax_px > 0:
        parallax_drift = f"{local_rng.choice([-1, 1]) * parallax_px}*({ease}-0.5)"
        overlay = f"overlay=x='(W-w)/2+({parallax_drift})':y='(H-h)/2'"
    else:
        overlay = "overlay=(W-w)/2:(H-h)/2"

    comp = (
        f"[bg{i}][fg{i}]{overlay},"
        "eq=contrast=1.05:brightness=0.01:saturation=1.04:gamma=0.98,"
        "vignette=PI/10,"
        "noise=alls=3:allf=t+u,"
        f"format=yuv420p[v{i}]"
    )
    return ";\n".join([bg, fg, comp])


def build_filter_for_clip(
    i: int,
    width: int,
    height: int,
    fps: int,
    duration: float,
    blur_strength: int = 18,
    clip_grade: str = "full",
    use_lanczos: bool = False,
) -> str:
    """Build ffmpeg filter chain for a video clip. No still-image looping, no motion applied."""
    scale_flags = ":flags=lanczos" if use_lanczos else ""
    bg = (
        f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
        f"boxblur={blur_strength}:1,eq=brightness=-0.04:saturation=1.08,"
        f"fps={fps},trim=duration={duration:.3f},setpts=PTS-STARTPTS[bg{i}]"
    )
    fg = (
        f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease{scale_flags},setsar=1,"
        f"fps={fps},trim=duration={duration:.3f},setpts=PTS-STARTPTS[fg{i}]"
    )
    overlay = "overlay=(W-w)/2:(H-h)/2"
    if clip_grade == "none":
        comp = f"[bg{i}][fg{i}]{overlay},format=yuv420p[v{i}]"
    elif clip_grade == "grade":
        comp = (
            f"[bg{i}][fg{i}]{overlay},"
            "eq=contrast=1.05:brightness=0.01:saturation=1.04:gamma=0.98,"
            f"format=yuv420p[v{i}]"
        )
    else:  # "full"
        comp = (
            f"[bg{i}][fg{i}]{overlay},"
            "eq=contrast=1.05:brightness=0.01:saturation=1.04:gamma=0.98,"
            "vignette=PI/10,"
            "noise=alls=3:allf=t+u,"
            f"format=yuv420p[v{i}]"
        )
    return ";\n".join([bg, fg, comp])


def build_xfade_chain(
    photo_durations: List[float],
    xfade: float,
    transition: str = "auto",
    stream_names: Optional[List[str]] = None,
):
    n = len(photo_durations)
    names = stream_names if stream_names is not None else [f"[v{i}]" for i in range(n)]
    if n == 1:
        return f"{names[0]}format=yuv420p[vout]"
    starts = _media_start_times(photo_durations, xfade)
    parts = []
    cur = names[0]
    for i in range(1, n):
        out = f"[x{i}]"
        resolved_transition = resolve_transition_name(transition, i)
        parts.append(
            f"{cur}{names[i]}xfade=transition={resolved_transition}:duration={xfade}:offset={starts[i]}{out}"
        )
        cur = out
    total = estimate_duration_variable(photo_durations, xfade)
    parts.append(f"{cur}trim=duration={total},format=yuv420p[vout]")
    return ";\n".join(parts)


def _build_clip_audio_filters(
    infos_list: List[Optional[PhotoInfo]],
    media_durations: List[float],
    xfade: float,
    audio_input_idx: Optional[int],
    clip_audio: str,
    audio_fade: Optional[float] = None,
    total_duration: float = 0.0,
) -> str:
    """Build audio filter complex fragment for keep/duck modes. Returns fragment producing [aout].

    audio_input_idx: ffmpeg input index of the background --audio file, or None if not provided.
    clip_audio: "keep" (mix clip audio with background at full volume) or
                "duck" (lower background to 20% during clip playback).
    audio_fade: if set, fade out the final mix over this many seconds at the end.
    """
    start_times = _media_start_times(media_durations, xfade)
    clip_items = [
        (i, start_times[i], start_times[i] + media_durations[i])
        for i, info in enumerate(infos_list)
        if info and info.is_video and info.has_audio
    ]

    parts: List[str] = []
    mix_labels: List[str] = []

    if audio_input_idx is not None:
        if clip_audio == "duck" and clip_items:
            conds = "+".join(f"between(t,{s:.3f},{e:.3f})" for _, s, e in clip_items)
            vol = f"if(gt({conds},0),0.2,1.0)"
            parts.append(f"[{audio_input_idx}:a]volume='{vol}':eval=frame,apad[bg_a]")
        else:
            parts.append(f"[{audio_input_idx}:a]apad[bg_a]")
        mix_labels.append("[bg_a]")

    for seq, (clip_idx, start, end) in enumerate(clip_items):
        delay_ms = int(start * 1000)
        clip_dur = end - start
        parts.append(
            f"[{clip_idx}:a]atrim=duration={clip_dur:.3f},adelay={delay_ms}:all=1[ca{seq}]"
        )
        mix_labels.append(f"[ca{seq}]")

    if not mix_labels:
        return ""

    effective_fade = min(audio_fade, total_duration) if (audio_fade and audio_fade > 0 and total_duration > 0) else None
    final_label: Optional[str] = None
    if len(mix_labels) == 1:
        if effective_fade:
            fade_st = total_duration - effective_fade
            parts.append(f"{mix_labels[0]}afade=t=out:st={fade_st:.3f}:d={effective_fade:.3f}[amix_a]")
            final_label = "[amix_a]"
        else:
            final_label = mix_labels[0]
    else:
        joined = "".join(mix_labels)
        if effective_fade:
            fade_st = total_duration - effective_fade
            parts.append(f"{joined}amix=inputs={len(mix_labels)}:normalize=0[amix_a]")
            parts.append(f"[amix_a]afade=t=out:st={fade_st:.3f}:d={effective_fade:.3f}[amix_b]")
            final_label = "[amix_b]"
        else:
            parts.append(f"{joined}amix=inputs={len(mix_labels)}:normalize=0[amix_a]")
            final_label = "[amix_a]"

    if final_label is None:
        return ""

    if total_duration > 0:
        parts.append(f"{final_label}apad,atrim=duration={total_duration:.6f}[aout]")
    else:
        parts.append(f"{final_label}anull[aout]")

    return ";\n".join(parts)


def run_ffmpeg_with_progress(cmd: List[str], total_duration: float):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    if proc.stdout is None:
        raise RuntimeError("ffmpeg subprocess stdout unavailable")
    last = -1.0
    error_lines = []
    for line in proc.stdout:
        line = line.strip()
        if line:
            error_lines.append(line)
            error_lines = error_lines[-20:]
        if "=" in line:
            key, value = line.split("=", 1)
            if key == "out_time_ms" and total_duration > 0:
                try:
                    sec = int(value) / 1_000_000.0
                    pct = min(100.0, (sec / total_duration) * 100.0)
                    if pct - last >= 2.0 or pct >= 100.0:
                        print(f"[render {pct:5.1f}%]")
                        last = pct
                except ValueError:
                    pass
    ret = proc.wait()
    if ret != 0:
        tail = "\n".join(error_lines) or "ffmpeg exited without additional diagnostics."
        raise RuntimeError(f"ffmpeg render failed with exit code {ret}.\n{tail}")


def build_render_command(
    images: List[Path],
    out_path: Path,
    width: int,
    height: int,
    cfg: RenderConfig,
    focal_points: Optional[List[Optional[Tuple[float, float]]]] = None,
    label_overlay_paths: Optional[List[Optional[Path]]] = None,
    infos: Optional[List[Optional[PhotoInfo]]] = None,
) -> Tuple[List[str], float]:
    infos_list: List[Optional[PhotoInfo]] = infos if infos is not None else [None] * len(images)
    media_durations = build_media_durations(infos_list, cfg.sec, cfg.xfade, cfg.rhythm_strength, cfg.motion_seed, cfg.clip_max_sec)
    duration = estimate_duration_variable(media_durations, cfg.xfade)
    ken, para = resolve_motion_values(cfg.motion_style, cfg.ken_override, cfg.parallax_override, min(width, height), cfg.sec)

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
    if cfg.encoder == "h264_videotoolbox":
        cmd += ["-hwaccel", "videotoolbox"]

    # Inputs: video clips use plain -i; still images use -loop 1 -t <dur>.
    for img, info, item_dur in zip(images, infos_list, media_durations):
        if info and info.is_video:
            cmd += ["-i", str(img)]
        else:
            cmd += ["-loop", "1", "-t", str(item_dur), "-i", str(img)]

    if cfg.audio_path is not None:
        if cfg.audio_offset > 0:
            cmd += ["-ss", str(cfg.audio_offset)]
        cmd += ["-i", str(cfg.audio_path)]

    # Add label overlay PNG inputs (after media + audio).
    audio_input_idx = len(images) if cfg.audio_path is not None else None
    label_input_start = len(images) + (1 if cfg.audio_path is not None else 0)
    label_input_map: dict = {}  # media index -> ffmpeg input index
    if label_overlay_paths:
        next_idx = label_input_start
        for i, lpath in enumerate(label_overlay_paths):
            if lpath is not None:
                cmd += ["-loop", "1", "-t", str(media_durations[i]), "-i", str(lpath)]
                label_input_map[i] = next_idx
                next_idx += 1

    # Per-item video filters: clips skip motion; photos get full still treatment.
    use_lanczos = cfg.quality_name in ("high", "youtube", "max")
    filters = []
    for i, (item_dur, info) in enumerate(zip(media_durations, infos_list)):
        if info and info.is_video:
            filters.append(build_filter_for_clip(i, width, height, cfg.fps, item_dur, cfg.blur_strength, cfg.clip_grade, use_lanczos))
        else:
            filters.append(build_filter_for_still(
                i, width, height, cfg.fps, item_dur,
                cfg.blur_strength, ken, para, cfg.motion_seed,
                focal_points[i] if focal_points else None,
                use_lanczos,
            ))

    # Label overlays: composite label PNG onto each labeled stream.
    overlay_filters = []
    stream_names = []
    for i in range(len(media_durations)):
        if i in label_input_map:
            overlay_filters.append(f"[v{i}][{label_input_map[i]}:v]overlay=0:0[vl{i}]")
            stream_names.append(f"[vl{i}]")
        else:
            stream_names.append(f"[v{i}]")

    filter_parts = [";\n".join(filters)]
    if overlay_filters:
        filter_parts.append(";\n".join(overlay_filters))
    filter_parts.append(build_xfade_chain(media_durations, cfg.xfade, cfg.transition, stream_names))

    # Audio: use filter-complex mixing when clips have audible audio (keep/duck);
    # otherwise fall back to the simple -map + -af apad approach.
    has_video_clips = any(info and info.is_video for info in infos_list)
    use_complex_audio = has_video_clips and cfg.clip_audio != "mute"
    audio_filter = ""
    if use_complex_audio:
        audio_filter = _build_clip_audio_filters(
            infos_list, media_durations, cfg.xfade, audio_input_idx, cfg.clip_audio,
            audio_fade=cfg.audio_fade, total_duration=duration,
        )
        if audio_filter:
            filter_parts.append(audio_filter)

    filter_complex = ";\n".join(filter_parts)
    cmd += ["-filter_complex", filter_complex, "-map", "[vout]", "-r", str(cfg.fps), "-pix_fmt", "yuv420p", "-movflags", "+faststart"]

    if use_complex_audio and audio_filter:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    elif cfg.audio_path is not None:
        af = "apad"
        if cfg.audio_fade and cfg.audio_fade > 0 and duration > 0:
            effective_fade = min(cfg.audio_fade, duration)
            fade_st = duration - effective_fade
            af += f",afade=t=out:st={fade_st:.3f}:d={effective_fade:.3f}"
        cmd += ["-map", f"{audio_input_idx}:a", "-af", af, "-c:a", "aac", "-b:a", "192k", "-shortest"]

    if cfg.encoder == "h264_videotoolbox":
        cmd += ["-c:v", "h264_videotoolbox", "-b:v", cfg.bitrate]
    else:
        crf, preset = x264_tuning_for_quality(cfg.quality_name)
        cmd += ["-c:v", "libx264", "-crf", crf, "-preset", preset]

    cmd += [str(out_path)]
    return cmd, duration


def render(
    images: List[Path],
    out_path: Path,
    width: int,
    height: int,
    cfg: RenderConfig,
    focal_points: Optional[List[Optional[Tuple[float, float]]]] = None,
    label_overlay_paths: Optional[List[Optional[Path]]] = None,
    infos: Optional[List[Optional[PhotoInfo]]] = None,
) -> str:
    """Run the render and return the encoder actually used."""
    cmd, duration = build_render_command(images, out_path, width, height, cfg, focal_points, label_overlay_paths, infos)
    try:
        run_ffmpeg_with_progress(cmd, duration)
        return cfg.encoder
    except RuntimeError as exc:
        message = str(exc)
        videotoolbox_failed = (
            cfg.encoder == "h264_videotoolbox"
            and ("Error while opening encoder" in message or "Could not open encoder" in message)
        )
        if not videotoolbox_failed:
            raise
        print("VideoToolbox encoder failed; retrying with libx264")
        fallback_cfg = dataclass_replace(cfg, encoder="libx264")
        fallback_cmd, fallback_duration = build_render_command(images, out_path, width, height, fallback_cfg, focal_points, label_overlay_paths, infos)
        run_ffmpeg_with_progress(fallback_cmd, fallback_duration)
        return "libx264"


def _haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Great-circle distance in km between two (lat, lon) points."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def _nearest_neighbor_sort(
    pairs: List[Tuple[Path, "PhotoInfo"]],
) -> List[Tuple[Path, "PhotoInfo"]]:
    """Greedy nearest-neighbor sort by GPS proximity (haversine).

    Starts from the first item in the list and always appends the closest
    unvisited item next. O(n²), suitable for slideshow-sized collections.
    """
    remaining = list(pairs)
    ordered = [remaining.pop(0)]
    while remaining:
        last_gps = ordered[-1][1].gps_coords  # type: ignore[union-attr]
        closest_idx = min(
            range(len(remaining)),
            key=lambda i: _haversine_km(last_gps, remaining[i][1].gps_coords),  # type: ignore[arg-type]
        )
        ordered.append(remaining.pop(closest_idx))
    return ordered


def sort_images_and_infos(
    images: List[Path],
    infos: List[Optional[PhotoInfo]],
    sort_by="natural",
    seed=0,
) -> Tuple[List[Path], List[Optional[PhotoInfo]]]:
    if sort_by == "natural":
        return images, infos
    paired = list(zip(images, infos))
    if sort_by == "random":
        rnd = random.Random(seed)
        rnd.shuffle(paired)
        sorted_images = [image for image, _info in paired]
        sorted_infos = [info for _image, info in paired]
        return sorted_images, sorted_infos
    if sort_by == "time":
        with_dt = [(image, info) for image, info in paired if info and info.datetime_taken]
        no_dt = [(image, info) for image, info in paired if not info or not info.datetime_taken]
        if not with_dt:
            return images, infos
        with_dt.sort(key=lambda item: item[1].datetime_taken or datetime.min)
        ordered = with_dt + no_dt
        return [image for image, _info in ordered], [info for _image, info in ordered]
    if sort_by == "location":
        with_gps = [(image, info) for image, info in paired if info and info.gps_coords]
        no_gps = [(image, info) for image, info in paired if not info or not info.gps_coords]
        if not with_gps:
            return images, infos
        ordered = _nearest_neighbor_sort(with_gps) + no_gps
        return [image for image, _info in ordered], [info for _image, info in ordered]
    raise ValueError(f"Unknown sort mode: {sort_by}")


def split_photos_into_parts(
    images: List[Path],
    infos: List[Optional[PhotoInfo]],
    focal_points: List[Optional[Tuple[float, float]]],
    base_sec: float,
    xfade: float,
    rhythm_strength: float,
    seed: int,
    max_sec: float,
    clip_max_sec: Optional[float] = None,
) -> List[Tuple[List[Path], List[Optional[PhotoInfo]], List[Optional[Tuple[float, float]]]]]:
    """Greedily group photos into parts whose estimated duration does not exceed max_sec."""
    parts = []
    cur_imgs: List[Path] = []
    cur_infos: List[Optional[PhotoInfo]] = []
    cur_focal: List[Optional[Tuple[float, float]]] = []

    for img, info, fp in zip(images, infos, focal_points):
        trial = cur_imgs + [img]
        trial_infos = cur_infos + [info]
        trial_dur = build_media_durations(trial_infos, base_sec, xfade, rhythm_strength, seed, clip_max_sec)
        trial_total = estimate_duration_variable(trial_dur, xfade)
        if trial_total > max_sec and cur_imgs:
            parts.append((cur_imgs, cur_infos, cur_focal))
            cur_imgs, cur_infos, cur_focal = [img], [info], [fp]
        else:
            cur_imgs, cur_infos, cur_focal = trial, trial_infos, cur_focal + [fp]

    if cur_imgs:
        parts.append((cur_imgs, cur_infos, cur_focal))

    return parts


def validate_transition(transition: str) -> str:
    normalized = transition.strip().lower()
    if normalized not in VALID_TRANSITIONS:
        supported = ", ".join(sorted(VALID_TRANSITIONS))
        raise argparse.ArgumentTypeError(f"must be one of: {supported}")
    return normalized


def parse_args():
    ap = argparse.ArgumentParser(description="Create slideshow videos from photos.")
    ap.add_argument("source_dir", nargs="?")
    ap.add_argument("--outdir", default="./Renders")
    ap.add_argument("--workdir", default="./.work_pngs")
    ap.add_argument("--quality", default="standard", choices=["draft", "standard", "high", "youtube", "max"])
    ap.add_argument("--format", default="both", choices=["16x9", "9x16", "both"])
    ap.add_argument("--resolution", default="1080p", type=normalize_resolution,
                    choices=RESOLUTION_CHOICES,
                    help="Output resolution: 1080p, 1440p, 4k/2160p, or 8k/4320p")
    ap.add_argument("--sort-by", default="natural", choices=["natural", "time", "location", "random"])
    ap.add_argument("--max-workers", type=int, default=0)
    ap.add_argument("--camera-stats", action="store_true")
    ap.add_argument("--geocode", action="store_true",
                    help="Reverse-geocode GPS coords via OpenStreetMap to populate location labels")
    ap.add_argument("--location-stats", action="store_true",
                    help="Print per-photo location labels (implies --geocode)")
    ap.add_argument("--location-overlay", action="store_true",
                    help="Burn location labels into the video as text (implies --geocode)")
    ap.add_argument("--motion-style", default="none", choices=["none", "kenburns", "parallax", "both"])
    ap.add_argument("--ken-burns-strength", type=float, default=None)
    ap.add_argument("--parallax-px", type=int, default=None)
    ap.add_argument("--smart-focus", action="store_true",
                    help="Use MediaPipe Tasks face detection with pose fallback to bias Ken Burns framing")
    ap.add_argument("--smart-focus-model-dir", default="./.mediapipe_models",
                    help="Directory for cached MediaPipe Tasks model assets")
    ap.add_argument("--smart-focus-face-model", default=None,
                    help="Path to a MediaPipe Face Detector model asset")
    ap.add_argument("--smart-focus-pose-model", default=None,
                    help="Path to a MediaPipe Pose Landmarker model asset")
    ap.add_argument("--progress", action="store_true",
                    help="Show progress updates during preparation and rendering")
    ap.add_argument("--sec", type=float, default=2.8)
    ap.add_argument("--xfade", type=float, default=0.7)
    ap.add_argument("--transition", default="auto", type=validate_transition)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--rhythm-strength", type=float, default=0.12,
                    help="Per-photo timing variation strength (0.0 to 0.25)")
    ap.add_argument("--audio", default=None,
                    help="Path to an audio file to mix into the slideshow. Trimmed to video length.")
    ap.add_argument("--audio-offset", type=float, default=0.0,
                    help="Start the background audio at this offset in seconds (e.g. skip to the drop)")
    ap.add_argument("--audio-fade", type=float, default=None,
                    help="Fade out the background audio over this many seconds at the end of the video")
    ap.add_argument("--clip-max-sec", type=float, default=None,
                    help="Maximum duration in seconds for video clips (longer clips are trimmed to this length)")
    ap.add_argument("--clip-grade", default="full", choices=["none", "grade", "full"],
                    help="Visual treatment for video clips: none (no effect), grade (color only), full (grade+vignette+grain)")
    ap.add_argument("--clip-audio", default="mute", choices=["mute", "keep", "duck"],
                    help="Clip audio handling: mute (silence clips), keep (mix with background), duck (lower background during clips)")
    ap.add_argument("--split-secs", type=float, default=None,
                    help="Also render split parts, each no longer than this many seconds")
    ap.add_argument("--fps", type=int, default=None,
                    help="Override frames per second (overrides quality preset)")
    ap.add_argument("--bitrate", default=None,
                    help="Override output bitrate, e.g. 20M (overrides quality preset)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print ffmpeg command(s) and estimated duration without rendering")
    ap.add_argument("--youtube-upload", action="store_true",
                    help="Upload rendered videos to YouTube after rendering completes")
    ap.add_argument("--youtube-upload-file", default=None,
                    help="Upload an existing rendered video file to YouTube without re-rendering")
    ap.add_argument("--add-to-photos", action="store_true",
                    help="Import generated videos into the macOS Photos app")
    ap.add_argument("--youtube-client-secrets", default="./client_secrets.json",
                    help="Path to Google OAuth client secrets JSON for YouTube uploads")
    ap.add_argument("--youtube-token-file", default="./.youtube_token.json",
                    help="Path to cached OAuth token JSON for YouTube uploads")
    ap.add_argument("--youtube-title", default=None,
                    help="Optional title template. Supports {stem}, {filename}, {format}, {input_dir}")
    ap.add_argument("--youtube-description", default="",
                    help="YouTube video description")
    ap.add_argument("--youtube-tags", default="",
                    help="Comma-separated YouTube tags")
    ap.add_argument("--youtube-category", default="22",
                    help="YouTube category ID (default 22 = People & Blogs)")
    ap.add_argument("--youtube-privacy", default="private", choices=["private", "public", "unlisted"],
                    help="YouTube privacy status")
    return ap.parse_args()


def _slug(value: str) -> str:
    """Convert arbitrary strings to filesystem-safe tokens."""
    token = value.strip().lower()
    token = re.sub(r"[^a-z0-9]+", "-", token)
    token = re.sub(r"-+", "-", token).strip("-")
    return token or "unnamed"


def build_targets(
    fmt: str,
    stamp: str,
    input_dir_name: str,
    quality: str,
    transition: str,
    photo_count: int,
    clip_count: int = 0,
    resolution: str = "1080p",
):
    """Build render targets using the agreed deterministic filename schema.

    Count suffix: n{photo_count} for photo-only renders; n{photo_count}c{clip_count} when clips are present.
    """
    count_str = f"n{photo_count}c{clip_count}" if clip_count > 0 else f"n{photo_count}"
    slug = _slug(input_dir_name)
    res = _slug(normalize_resolution(resolution))
    q = _slug(quality)
    tr = _slug(transition)
    targets = []
    if fmt in ("16x9", "both"):
        width, height = dimensions_for_target("16x9", res)
        targets.append((f"{stamp}_{slug}_fmt16x9_res{res}_q{q}_transition-{tr}_{count_str}.mp4", width, height))
    if fmt in ("9x16", "both"):
        width, height = dimensions_for_target("9x16", res)
        targets.append((f"{stamp}_{slug}_fmt9x16_res{res}_q{q}_transition-{tr}_{count_str}.mp4", width, height))
    return targets


def print_camera_stats(infos: List[Optional[PhotoInfo]]) -> None:
    camera_counts = Counter()
    unknown_count = 0

    for info in infos:
        if not info:
            unknown_count += 1
            continue

        label = " ".join(part for part in (info.camera_make, info.camera_model) if part).strip()
        if label:
            camera_counts[label] += 1
        else:
            unknown_count += 1

    print("\nCamera stats")
    if not camera_counts and unknown_count == 0:
        print("- No images processed.")
        return

    for camera, count in camera_counts.most_common():
        print(f"- {camera}: {count}")
    if unknown_count:
        print(f"- Unknown camera: {unknown_count}")


# ---------------------------------------------------------------------------
# Reverse geocoding via OpenStreetMap Nominatim (free, no API key required).
# Rate-limited to 1 request/second per OSM policy.
# ---------------------------------------------------------------------------

_GEOCODE_CACHE: dict = {}
_GEOCODE_LAST_CALL: float = 0.0


def _nominatim_reverse(lat: float, lon: float) -> Optional[dict]:
    global _GEOCODE_LAST_CALL
    cache_key = (round(lat, 4), round(lon, 4))
    if cache_key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[cache_key]

    elapsed = time.time() - _GEOCODE_LAST_CALL
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)

    url = (
        "https://nominatim.openstreetmap.org/reverse"
        f"?lat={lat:.6f}&lon={lon:.6f}&format=json&zoom=16&addressdetails=1"
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "videophotoslide/1.0 (personal slideshow tool)"},
    )
    try:
        _GEOCODE_LAST_CALL = time.time()
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        _GEOCODE_CACHE[cache_key] = data
        return data
    except Exception:
        _GEOCODE_CACHE[cache_key] = None
        return None


def _build_location_parts(
    lat: float, lon: float, altitude_m: Optional[float]
) -> Tuple[List[str], Optional[str]]:
    """Return (location_lines, altitude_str) for overlay and stats use.

    location_lines contains one entry per visual line (feature name, then
    city/park context). altitude_str is formatted for a separate top-right
    placement, or None if no altitude data.
    """
    data = _nominatim_reverse(lat, lon)
    location_lines: List[str] = []

    if data:
        addr = data.get("address", {})
        feature = data.get("name", "").strip()
        outdoor_context = (
            addr.get("protected_area")
            or addr.get("nature_reserve")
            or addr.get("national_park")
            or addr.get("leisure")
        )
        city = (
            addr.get("city")
            or addr.get("town")
            or addr.get("village")
            or addr.get("hamlet")
            or addr.get("suburb")
        )
        state = addr.get("state")
        country_code = addr.get("country_code", "").upper()

        if feature:
            location_lines.append(feature)
        if outdoor_context and outdoor_context != feature:
            location_lines.append(outdoor_context)
        if city:
            location_lines.append(city)
        if state:
            location_lines.append(state)
        if country_code not in ("US", ""):
            country = addr.get("country", "")
            if country:
                location_lines.append(country)

    altitude_str: Optional[str] = None
    if altitude_m is not None:
        ft = round(altitude_m * 3.28084)
        altitude_str = f"{ft:,} ft"

    return location_lines, altitude_str


def build_location_label(lat: float, lon: float, altitude_m: Optional[float]) -> str:
    """Human-readable single-string label (used for --location-stats output)."""
    lines, alt = _build_location_parts(lat, lon, altitude_m)
    return " · ".join(lines + ([alt] if alt else []))


def geocode_photos(infos: List[Optional[PhotoInfo]], show_progress: bool = False) -> None:
    """Reverse-geocode all photos that have GPS coords, mutating location_name in place."""
    targets = [info for info in infos if info and info.gps_coords]
    if not targets:
        return
    for count, info in enumerate(targets, start=1):
        lat, lon = info.gps_coords  # type: ignore[union-attr]
        label = build_location_label(lat, lon, info.altitude_m)
        info.location_name = label or None
        if show_progress:
            pct = (count / len(targets)) * 100.0
            print(f"[geocode {count}/{len(targets)} {pct:5.1f}%] {label or '(no result)'}")


def print_location_stats(infos: List[Optional[PhotoInfo]]) -> None:
    print("\nLocation stats")
    no_gps = sum(1 for info in infos if not info or not info.gps_coords)
    labeled = [(info.path.name, info.location_name) for info in infos if info and info.location_name]
    no_result = sum(1 for info in infos if info and info.gps_coords and not info.location_name)

    if not labeled and no_gps == len(infos):
        print("- No GPS data found in any image.")
        return

    for filename, label in labeled:
        print(f"- {filename}: {label}")
    if no_result:
        print(f"- {no_result} image(s) with GPS but no geocode result")
    if no_gps:
        print(f"- {no_gps} image(s) without GPS data")


def build_youtube_title(output_path: Path, input_dir: Path, fmt: str, custom_title: Optional[str]) -> str:
    if custom_title:
        return custom_title.format(
            stem=output_path.stem,
            filename=output_path.name,
            format=fmt,
            input_dir=input_dir.name,
        )
    return f"{input_dir.name} slideshow ({fmt})"


def infer_input_dir_for_upload(upload_only_path: Path, source_dir: Optional[str]) -> Path:
    if source_dir:
        return Path(source_dir)

    stem = upload_only_path.stem
    marker = "_fmt"
    if marker in stem:
        prefix = stem.split(marker, 1)[0]
        parts = prefix.split("_", 1)
        if len(parts) == 2 and parts[1]:
            return Path(parts[1])
    return upload_only_path.parent


def infer_render_format(video_path: Path) -> str:
    stem = video_path.stem.lower()
    if "fmt16x9" in stem or "16x9" in stem:
        return "16x9"
    if "fmt9x16" in stem or "9x16" in stem:
        return "9x16"
    return "unknown"


def import_media_to_photos(media_paths: List[Path]) -> None:
    if not media_paths:
        return

    resolved_paths = [str(path.expanduser().resolve()) for path in media_paths]

    script = """
on run argv
    set mediaItems to {}
    repeat with mediaPath in argv
        set end of mediaItems to ((POSIX file mediaPath) as alias)
    end repeat
    tell application "Photos"
        import mediaItems
    end tell
end run
""".strip()

    try:
        subprocess.run(
            ["osascript", "-e", script, *resolved_paths],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit("macOS Photos import requires 'osascript', which was not found on this system") from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        message = "Failed to import media into Photos"
        if details:
            message = f"{message}: {details}"
        raise SystemExit(message) from exc


def parse_youtube_tags(raw_tags: str) -> List[str]:
    return [tag.strip() for tag in raw_tags.split(",") if tag.strip()]


def _load_youtube_credentials(token_file: Path, client_secrets: Path):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise SystemExit(
            "YouTube upload requires Google API libraries. "
            "Install: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        ) from exc

    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    creds = None

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), scopes)

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None
    if not creds or not creds.valid:
        if not client_secrets.exists():
            raise SystemExit(f"Missing YouTube OAuth client secrets file: {client_secrets}")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), scopes)
        creds = flow.run_local_server(port=0)

    token_file.write_text(creds.to_json())
    return creds


def upload_video_to_youtube(
    video_path: Path,
    title: str,
    description: str,
    tags: List[str],
    category: str,
    privacy: str,
    client_secrets: Path,
    token_file: Path,
) -> str:
    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:
        raise SystemExit(
            "YouTube upload requires Google API libraries. "
            "Install: pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        ) from exc

    creds = _load_youtube_credentials(token_file=token_file, client_secrets=client_secrets)
    youtube = build("youtube", "v3", credentials=creds)
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": category,
            },
            "status": {
                "privacyStatus": privacy,
            },
        },
        media_body=MediaFileUpload(str(video_path), chunksize=5 * 1024 * 1024, resumable=True),
    )

    response = None
    retries = 0
    last_pct = -1.0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status is not None:
                pct = (status.resumable_progress / status.total_size) * 100.0
                if pct - last_pct >= 1.0:
                    print(f"\r[upload {pct:5.1f}%]", end="", flush=True)
                    last_pct = pct
        except HttpError as exc:
            if exc.resp.status not in (500, 502, 503, 504) or retries >= 5:
                raise
            retries += 1
            print(f"\nUpload chunk failed (HTTP {exc.resp.status}), retrying ({retries}/5)...")
            time.sleep(2 ** retries)

    print(f"\r[upload 100.0%]")
    return response["id"]


def build_label_overlay_paths_for_infos(
    infos: List[Optional[PhotoInfo]],
    width: int,
    height: int,
    temp_work: Path,
    prefix: str,
) -> Optional[List[Optional[Path]]]:
    """Build per-photo label overlay PNGs. Returns None if no infos have location data."""
    if not any(info.location_name if info else None for info in infos):
        return None
    result: List[Optional[Path]] = []
    for i, info in enumerate(infos):
        if info and info.location_name and info.gps_coords:
            lines, alt = _build_location_parts(
                info.gps_coords[0], info.gps_coords[1], info.altitude_m
            )
            if lines or alt:
                p = temp_work / f"label_{prefix}_{width}x{height}_{i:06d}.png"
                ts = info.datetime_taken.strftime("%-I:%M %p  ·  %b %-d, %Y") if info.datetime_taken else None
                create_label_overlay_png(lines, alt, ts, width, height, p)
                result.append(p)
                continue
        result.append(None)
    return result


def _validate_args(args) -> None:
    should_upload = args.youtube_upload or bool(args.youtube_upload_file)
    try:
        normalize_resolution(getattr(args, "resolution", "1080p"))
    except argparse.ArgumentTypeError as exc:
        raise SystemExit(f"--resolution {exc}") from exc
    if args.sec <= 0:
        raise SystemExit("--sec must be > 0")
    if not (0 <= args.xfade < args.sec):
        raise SystemExit("--xfade must satisfy 0 <= xfade < sec")
    if args.max_workers < 0:
        raise SystemExit("--max-workers must be >= 0")
    if args.ken_burns_strength is not None and not (0.0 <= args.ken_burns_strength <= 0.03):
        raise SystemExit("--ken-burns-strength must be between 0.0 and 0.03")
    if args.parallax_px is not None and args.parallax_px < 0:
        raise SystemExit("--parallax-px must be >= 0")
    if getattr(args, "smart_focus_face_model", None) and not Path(args.smart_focus_face_model).expanduser().is_file():
        raise SystemExit(f"--smart-focus-face-model file not found: {args.smart_focus_face_model}")
    if getattr(args, "smart_focus_pose_model", None) and not Path(args.smart_focus_pose_model).expanduser().is_file():
        raise SystemExit(f"--smart-focus-pose-model file not found: {args.smart_focus_pose_model}")
    if not (0.0 <= args.rhythm_strength <= 0.25):
        raise SystemExit("--rhythm-strength must be between 0.0 and 0.25")
    if should_upload and not args.youtube_category.isdigit():
        raise SystemExit("--youtube-category must be a numeric YouTube category ID")
    if args.split_secs is not None and args.split_secs <= 0:
        raise SystemExit("--split-secs must be > 0")
    if args.split_secs is not None and args.split_secs < args.sec:
        print(f"Warning: --split-secs ({args.split_secs}s) is less than --sec ({args.sec}s); every photo will be its own part", file=sys.stderr)
    if args.audio and not Path(args.audio).is_file():
        raise SystemExit(f"--audio file not found: {args.audio}")
    if args.audio_offset < 0:
        raise SystemExit("--audio-offset must be >= 0")
    if args.audio_fade is not None and args.audio_fade <= 0:
        raise SystemExit("--audio-fade must be > 0")
    if args.clip_max_sec is not None and args.clip_max_sec <= 0:
        raise SystemExit("--clip-max-sec must be > 0")
    if args.fps is not None and args.fps < 12:
        raise SystemExit("--fps must be at least 12")
    if args.bitrate is not None and not re.match(r'^\d+[kKmMgG]?$', args.bitrate):
        raise SystemExit("--bitrate must be a number with optional unit, e.g. 20M or 8000k")


def _preflight_youtube_upload(
    client_secrets: Path,
    token_file: Path,
    show_progress: bool,
) -> None:
    progress_print(show_progress, "[phase youtube] validating upload credentials before render")
    _load_youtube_credentials(token_file=token_file, client_secrets=client_secrets)


def _run_upload_only(
    args,
    upload_only_path: Path,
    youtube_client_secrets: Path,
    youtube_token_file: Path,
    youtube_tags: List[str],
) -> None:
    if not upload_only_path.exists() or not upload_only_path.is_file():
        raise SystemExit(f"Missing YouTube upload file: {upload_only_path}")

    input_dir = infer_input_dir_for_upload(upload_only_path, args.source_dir)
    if args.source_dir and (not input_dir.exists() or not input_dir.is_dir()):
        raise SystemExit(f"Missing source directory: {input_dir}")

    video_fmt = infer_render_format(upload_only_path)
    youtube_title = build_youtube_title(upload_only_path, input_dir, video_fmt, args.youtube_title)
    progress_print(args.progress, f"[phase upload-only] preparing upload for {upload_only_path.name}")
    print(f"Uploading existing render to YouTube: {youtube_title}")
    video_id = upload_video_to_youtube(
        video_path=upload_only_path,
        title=youtube_title,
        description=args.youtube_description,
        tags=youtube_tags,
        category=args.youtube_category,
        privacy=args.youtube_privacy,
        client_secrets=youtube_client_secrets,
        token_file=youtube_token_file,
    )
    print(f"YouTube upload complete: https://www.youtube.com/watch?v={video_id}")
    if args.add_to_photos:
        print(f"Importing into Photos: {upload_only_path.name}")
        import_media_to_photos([upload_only_path])


def _post_render_output(
    out: Path,
    src: Path,
    width: int,
    height: int,
    args,
    youtube_client_secrets: Path,
    youtube_token_file: Path,
    youtube_tags: List[str],
) -> None:
    if args.add_to_photos:
        progress_print(args.progress, f"[phase photos] importing {out.name}")
        print(f"Importing into Photos: {out.name}")
        import_media_to_photos([out])
    if args.youtube_upload:
        youtube_title = build_youtube_title(out, src, f"{width}x{height}", args.youtube_title)
        progress_print(args.progress, f"[phase youtube] uploading {out.name}")
        print(f"Uploading to YouTube: {youtube_title}")
        video_id = upload_video_to_youtube(
            video_path=out,
            title=youtube_title,
            description=args.youtube_description,
            tags=youtube_tags,
            category=args.youtube_category,
            privacy=args.youtube_privacy,
            client_secrets=youtube_client_secrets,
            token_file=youtube_token_file,
        )
        print(f"YouTube upload complete: https://www.youtube.com/watch?v={video_id}")


def main():
    t0 = time.time()
    args = parse_args()
    _validate_args(args)

    upload_only_path = Path(args.youtube_upload_file) if args.youtube_upload_file else None
    audio_path = Path(args.audio) if args.audio else None
    outdir = Path(args.outdir)
    ensure_dir(outdir)

    preset = QUALITY_PRESETS[args.quality]
    fps = args.fps if args.fps is not None else preset["fps"]
    resolution = normalize_resolution(getattr(args, "resolution", "1080p"))
    bitrate = resolve_output_bitrate(args.quality, resolution, fps, args.bitrate)
    youtube_client_secrets = Path(args.youtube_client_secrets)
    youtube_token_file = Path(args.youtube_token_file)
    youtube_tags = parse_youtube_tags(args.youtube_tags)

    if upload_only_path is not None:
        _run_upload_only(args, upload_only_path, youtube_client_secrets, youtube_token_file, youtube_tags)
        return

    if not args.source_dir:
        raise SystemExit("source_dir is required unless --youtube-upload-file is used")

    src = Path(args.source_dir)
    if not src.exists() or not src.is_dir():
        raise SystemExit(f"Missing source directory: {src}")

    if args.youtube_upload and not args.dry_run:
        _preflight_youtube_upload(
            client_secrets=youtube_client_secrets,
            token_file=youtube_token_file,
            show_progress=args.progress,
        )

    if resolution == "8k":
        print(
            "Warning: 8K output is experimental and can be very slow with very large files.",
            file=sys.stderr,
        )

    ensure_dir(Path(args.workdir))
    temp_work = Path(tempfile.mkdtemp(prefix="videophotoslide_", dir=str(Path(args.workdir))))

    try:
        blur = preset["blur_strength"]

        do_geocode = args.geocode or args.location_stats or args.location_overlay
        extract_exif = args.sort_by in ("time", "location") or args.camera_stats or do_geocode
        detect_focus = args.smart_focus and args.motion_style in ("kenburns", "both")
        if args.smart_focus and not detect_focus:
            print(f"Warning: --smart-focus has no effect with --motion-style {args.motion_style}; use kenburns or both", file=sys.stderr)
        smart_focus_models = None
        if detect_focus:
            smart_focus_model_dir = getattr(args, "smart_focus_model_dir", "./.mediapipe_models")
            smart_focus_face_model = getattr(args, "smart_focus_face_model", None)
            smart_focus_pose_model = getattr(args, "smart_focus_pose_model", None)
            smart_focus_models = resolve_smart_focus_model_paths(
                Path(smart_focus_model_dir),
                face_model=smart_focus_face_model,
                pose_model=smart_focus_pose_model,
                show_progress=args.progress,
            )
            configure_smart_focus_models(*smart_focus_models)
        progress_print(args.progress, f"[phase prep] scanning {src}")
        if detect_focus:
            progress_print(args.progress, "[phase prep] initializing smart focus detectors")
        progress_print(args.progress, f"[phase prep] collecting media ({'conversion + smart focus' if detect_focus else 'conversion'} for images, probe for video clips)")
        images, infos = collect_media(
            src,
            temp_work,
            extract_exif=extract_exif,
            detect_focus=detect_focus,
            smart_focus_models=smart_focus_models,
            max_workers=args.max_workers,
            show_progress=args.progress,
        )
        if not images:
            raise SystemExit(
                f"No supported media found in {src}. "
                f"Images: {', '.join(sorted(IMG_EXTS))}  "
                f"Videos: {', '.join(sorted(VID_EXTS))}"
            )

        n_clips = sum(1 for i in infos if i and i.is_video)
        progress_print(args.progress, f"[phase prep] ordering {len(images)} items ({n_clips} video clips) with sort={args.sort_by}")
        images, infos = sort_images_and_infos(images, infos, sort_by=args.sort_by, seed=args.seed)
        if args.camera_stats:
            print_camera_stats(infos)
        if do_geocode:
            progress_print(args.progress, f"[phase geocode] reverse-geocoding {sum(1 for i in infos if i and i.gps_coords)} photos with GPS...")
            geocode_photos(infos, show_progress=args.progress)
        if args.location_stats:
            print_location_stats(infos)
        focal_points = [info.focal_point if info else None for info in infos]
        if detect_focus:
            focused_count = sum(1 for point in focal_points if point is not None)
            print(f"Smart focus: detected subjects in {focused_count}/{len(focal_points) - n_clips} photos")

        encoder = "h264_videotoolbox" if ffmpeg_has_encoder("h264_videotoolbox") else "libx264"
        estimated_total = estimate_duration_variable(
            build_media_durations(infos, args.sec, args.xfade, args.rhythm_strength, args.seed, args.clip_max_sec),
            args.xfade,
        )
        print(
            "Render settings: "
            f"encoder={encoder}, resolution={resolution}, quality={args.quality}, "
            f"fps={fps}, bitrate={bitrate}, transition={args.transition}, motion={args.motion_style}, "
            f"estimated_duration={estimated_total:.1f}s"
        )
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        n_photos = len(images) - n_clips
        targets = build_targets(
            args.format,
            stamp,
            src.name,
            args.quality,
            args.transition,
            n_photos,
            n_clips,
            resolution=resolution,
        )

        if args.split_secs is not None:
            part_groups = split_photos_into_parts(
                images, infos, focal_points,
                args.sec, args.xfade, args.rhythm_strength, args.seed,
                args.split_secs,
                clip_max_sec=args.clip_max_sec,
            )
            if len(part_groups) <= 1:
                progress_print(args.progress, "[phase split] skipped: content fits in a single part")
                part_groups = []
        else:
            part_groups = []

        part_audio_offsets: List[float] = []
        cumulative = 0.0
        for part_imgs, part_infos, _ in part_groups:
            part_audio_offsets.append(cumulative)
            cumulative += estimate_duration_variable(
                build_media_durations(part_infos, args.sec, args.xfade, args.rhythm_strength, args.seed, args.clip_max_sec),
                args.xfade,
            )

        base_cfg = RenderConfig(
            fps=fps, sec=args.sec, xfade=args.xfade, transition=args.transition,
            blur_strength=blur, bitrate=bitrate, quality_name=args.quality,
            encoder=encoder, motion_style=args.motion_style,
            ken_override=args.ken_burns_strength, parallax_override=args.parallax_px,
            motion_seed=args.seed, rhythm_strength=args.rhythm_strength,
            audio_path=audio_path,
            audio_offset=args.audio_offset,
            audio_fade=args.audio_fade,
            clip_grade=args.clip_grade, clip_audio=args.clip_audio,
            clip_max_sec=args.clip_max_sec,
        )

        outputs = []
        for name, width, height in targets:
            out = outdir / name
            progress_print(args.progress, f"[phase render] starting {width}x{height} -> {out.name}")
            label_overlay_paths = (
                build_label_overlay_paths_for_infos(infos, width, height, temp_work, "main")
                if args.location_overlay
                else None
            )
            if args.dry_run:
                cmd, duration = build_render_command(
                    images, out, width, height, base_cfg,
                    focal_points=focal_points, label_overlay_paths=label_overlay_paths,
                    infos=infos,
                )
                print(f"[dry-run] {width}x{height} estimated_duration={duration:.1f}s")
                print(f"[dry-run] {' '.join(cmd)}")
            else:
                actual_encoder = render(
                    images, out, width, height, base_cfg,
                    focal_points=focal_points, label_overlay_paths=label_overlay_paths,
                    infos=infos,
                )
                outputs.append(out)
                note = f" (used {actual_encoder})" if actual_encoder != encoder else ""
                progress_print(args.progress, f"[phase render] completed {out.name}{note}")
                _post_render_output(out, src, width, height, args, youtube_client_secrets, youtube_token_file, youtube_tags)

            if part_groups:
                progress_print(args.progress, f"[phase split] rendering {len(part_groups)} parts (<={args.split_secs}s each)")
                for part_idx, (part_imgs, part_infos, part_focal) in enumerate(part_groups, start=1):
                    part_out = outdir / f"{out.stem}_part{part_idx:03d}.mp4"
                    progress_print(args.progress, f"[phase split] part {part_idx}/{len(part_groups)}: {len(part_imgs)} items -> {part_out.name}")
                    part_label_paths = (
                        build_label_overlay_paths_for_infos(part_infos, width, height, temp_work, f"p{part_idx}")
                        if args.location_overlay
                        else None
                    )
                    part_cfg = dataclass_replace(base_cfg, audio_offset=args.audio_offset + part_audio_offsets[part_idx - 1])
                    if args.dry_run:
                        cmd, duration = build_render_command(
                            part_imgs, part_out, width, height, part_cfg,
                            focal_points=part_focal, label_overlay_paths=part_label_paths,
                            infos=part_infos,
                        )
                        print(f"[dry-run] part {part_idx} {width}x{height} estimated_duration={duration:.1f}s")
                        print(f"[dry-run] {' '.join(cmd)}")
                    else:
                        render(
                            part_imgs, part_out, width, height, part_cfg,
                            focal_points=part_focal, label_overlay_paths=part_label_paths,
                            infos=part_infos,
                        )
                        outputs.append(part_out)
                        progress_print(args.progress, f"[phase split] completed {part_out.name}")
                        _post_render_output(part_out, src, width, height, args, youtube_client_secrets, youtube_token_file, youtube_tags)
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    else:
        elapsed = time.time() - t0
        print("\nDONE")
        print(f"Total time: {elapsed/60:.1f} min")
        for out in outputs:
            if out.exists():
                size_mb = out.stat().st_size / (1024 * 1024)
                print(f"- {out} ({size_mb:.1f} MB)")
            else:
                print(f"- {out} (missing)")
    finally:
        shutil.rmtree(temp_work, ignore_errors=True)


if __name__ == "__main__":
    main()
