#!/usr/bin/env python3
"""
VideoPhotoSlide
A safer, cleaner slideshow generator derived from make_slideshow2.py.
"""

import argparse
import math
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from multiprocessing import Pool, cpu_count
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image, ImageOps

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif"}

QUALITY_PRESETS = {
    "draft": {"fps": 24, "blur_strength": 12, "bitrate": "8M", "description": "Fast preview"},
    "standard": {"fps": 30, "blur_strength": 18, "bitrate": "15M", "description": "Balanced"},
    "high": {"fps": 30, "blur_strength": 22, "bitrate": "25M", "description": "Best quality"},
}

MOTION_PRESETS = {
    "none": {"ken": 0.0, "parallax_ratio": 0.0, "description": "No extra motion"},
    "kenburns": {"ken": 0.0015, "parallax_ratio": 0.0, "description": "Subtle linear zoom"},
    "parallax": {"ken": 0.0, "parallax_ratio": 0.0028, "description": "Very subtle depth drift"},
    "both": {"ken": 0.0015, "parallax_ratio": 0.0028, "description": "Subtle zoom plus depth drift"},
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
TRANSITION_CHOICES = sorted(VALID_TRANSITIONS)


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


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def progress_print(enabled: bool, message: str) -> None:
    if enabled:
        print(message, flush=True)


_MEDIAPIPE_FACE_DETECTOR = None
_MEDIAPIPE_POSE_DETECTOR = None


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _get_mediapipe_detectors():
    global _MEDIAPIPE_FACE_DETECTOR, _MEDIAPIPE_POSE_DETECTOR

    if _MEDIAPIPE_FACE_DETECTOR is not None and _MEDIAPIPE_POSE_DETECTOR is not None:
        return _MEDIAPIPE_FACE_DETECTOR, _MEDIAPIPE_POSE_DETECTOR

    try:
        import mediapipe as mp
    except ImportError as exc:
        raise SystemExit(
            "Smart focus requires MediaPipe. Install: pip install mediapipe"
        ) from exc

    if _MEDIAPIPE_FACE_DETECTOR is None:
        _MEDIAPIPE_FACE_DETECTOR = mp.solutions.face_detection.FaceDetection(
            model_selection=1,
            min_detection_confidence=0.5,
        )
    if _MEDIAPIPE_POSE_DETECTOR is None:
        _MEDIAPIPE_POSE_DETECTOR = mp.solutions.pose.Pose(
            static_image_mode=True,
            model_complexity=1,
            enable_segmentation=False,
            min_detection_confidence=0.5,
        )
    return _MEDIAPIPE_FACE_DETECTOR, _MEDIAPIPE_POSE_DETECTOR


def detect_subject_focus(image: Image.Image) -> Optional[Tuple[float, float]]:
    face_detector, pose_detector = _get_mediapipe_detectors()
    image_rgb = np.asarray(image.convert("RGB"))

    face_result = face_detector.process(image_rgb)
    if face_result.detections:
        best = max(face_result.detections, key=lambda det: det.score[0] if det.score else 0.0)
        bbox = best.location_data.relative_bounding_box
        face_x = bbox.xmin + (bbox.width / 2.0)
        face_y = bbox.ymin + (bbox.height * 0.38)
        return clamp(face_x, 0.0, 1.0), clamp(face_y, 0.0, 1.0)

    pose_result = pose_detector.process(image_rgb)
    if pose_result.pose_landmarks:
        landmarks = pose_result.pose_landmarks.landmark
        visible = [lm for lm in landmarks if getattr(lm, "visibility", 0.0) >= 0.5]
        if visible:
            nose = landmarks[0]
            if getattr(nose, "visibility", 0.0) >= 0.5:
                return clamp(nose.x, 0.0, 1.0), clamp(nose.y, 0.0, 1.0)

            shoulder_ids = (11, 12)
            shoulders = [landmarks[idx] for idx in shoulder_ids if getattr(landmarks[idx], "visibility", 0.0) >= 0.5]
            if shoulders:
                x = sum(lm.x for lm in shoulders) / len(shoulders)
                y = sum(lm.y for lm in shoulders) / len(shoulders)
                return clamp(x, 0.0, 1.0), clamp(y, 0.0, 1.0)

            x = sum(lm.x for lm in visible) / len(visible)
            y = sum(lm.y for lm in visible) / len(visible)
            return clamp(x, 0.0, 1.0), clamp(y, 0.0, 1.0)

    return None


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
    if quality_name == "high":
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
        wave = 0.5 * (1.0 + math.sin((i / max(1, num_photos - 1)) * math.pi * 2.0))
        jitter = (rng.random() - 0.5) * 0.8
        delta = ((wave - 0.5) + jitter) * strength
        sec = base_sec * (1.0 + delta)
        durations.append(max(min_sec, sec))
    return durations


def resolve_transition_name(transition_mode: str, index: int) -> str:
    """Resolve transition name for a segment index (1-based image index)."""
    if transition_mode == "auto":
        return PRO_TRANSITIONS[(index - 1) % len(PRO_TRANSITIONS)]
    return transition_mode


def parse_gps_coord(gps_ref, gps_data):
    try:
        d = gps_data[0][0] / gps_data[0][1]
        m = gps_data[1][0] / gps_data[1][1]
        s = gps_data[2][0] / gps_data[2][1]
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


def extract_exif_gps(image: Image.Image) -> Optional[Tuple[float, float]]:
    try:
        exif_data = image.getexif()
        if not exif_data:
            return None
        gps_ifd = exif_data.get(34853)
        if not gps_ifd:
            return None
        gps_lat_ref = gps_ifd.get(1, b"N").decode() if isinstance(gps_ifd.get(1), bytes) else gps_ifd.get(1, "N")
        gps_lon_ref = gps_ifd.get(3, b"E").decode() if isinstance(gps_ifd.get(3), bytes) else gps_ifd.get(3, "E")
        gps_lat = gps_ifd.get(2)
        gps_lon = gps_ifd.get(4)
        if gps_lat and gps_lon:
            lat = parse_gps_coord(gps_lat_ref, gps_lat)
            lon = parse_gps_coord(gps_lon_ref, gps_lon)
            if lat is not None and lon is not None:
                return lat, lon
    except Exception:
        return None
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
        make, model = extract_exif_camera(image) if extract_exif else ("", "")
        focal_point = detect_subject_focus(image) if detect_focus else None
        return PhotoInfo(
            path=img_path,
            width=width,
            height=height,
            aspect_ratio=aspect,
            is_landscape=(aspect > 1.1),
            orientation="",
            datetime_taken=dt_taken,
            gps_coords=gps,
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


def _init_mediapipe_worker():
    _get_mediapipe_detectors()


def convert_to_pngs(
    input_dir: Path,
    work_dir: Path,
    extract_exif: bool = True,
    detect_focus: bool = False,
    max_workers: int = 0,
    show_progress: bool = False,
) -> Tuple[List[Path], List[Optional[PhotoInfo]]]:
    ensure_dir(work_dir)
    files = sorted(input_dir.iterdir(), key=lambda x: natural_key(x.name))
    image_files = [p for p in files if p.suffix.lower() in IMG_EXTS]
    if not image_files:
        return [], []

    args_list = [(p, i, work_dir, extract_exif, detect_focus) for i, p in enumerate(image_files)]
    progress_every = 1 if len(image_files) <= 10 else 5
    progress_label = "convert+focus" if detect_focus else "convert"

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
        with Pool(workers, initializer=initializer) as pool:
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

    images, infos = [], []
    for _idx, out, info, err in sorted(results, key=lambda item: item[0]):
        if err:
            print(f"Skipping: {err}", file=sys.stderr)
        else:
            images.append(out)
            infos.append(info)
    return images, infos


def build_filter_for_still(
    i,
    width,
    height,
    fps,
    sec,
    blur_strength=18,
    ken_strength=0.0,
    parallax_px=0,
    motion_seed=0,
    focal_point: Optional[Tuple[float, float]] = None,
):
    local_rng = random.Random(motion_seed + i * 101)

    u = f"(t/{sec})"
    smooth_u = f"min(max({u},0),1)"
    ease = f"(3*pow({smooth_u},2)-2*pow({smooth_u},3))"
    if ken_strength > 0:
        focus_x = focal_point[0] if focal_point else 0.5
        focus_y = focal_point[1] if focal_point else 0.5

        has_subject_target = focal_point is not None
        base_zoom = 1.0 + (ken_strength * 0.45)
        end_zoom = 1.0 + (ken_strength * (1.25 if has_subject_target else 0.85))
        drift_norm_x = local_rng.uniform(-0.045, 0.045) if has_subject_target else local_rng.uniform(-0.02, 0.02)
        drift_norm_y = local_rng.uniform(-0.03, 0.03) if has_subject_target else local_rng.uniform(-0.015, 0.015)
        if parallax_px > 0:
            drift_norm_x += local_rng.choice([-1, 1]) * ((parallax_px * 0.35) / max(width, 1))
            drift_norm_y += local_rng.choice([-1, 1]) * ((parallax_px * 0.35) / max(height, 1))
        start_fx = clamp(focus_x - (drift_norm_x * 0.5), 0.18, 0.82)
        end_fx = clamp(focus_x + (drift_norm_x * 0.5), 0.18, 0.82)
        start_fy = clamp(focus_y - (drift_norm_y * 0.5), 0.18, 0.82)
        end_fy = clamp(focus_y + (drift_norm_y * 0.5), 0.18, 0.82)

        fg = (
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,setsar=1,"
            f"scale=w='iw*({base_zoom}+(({end_zoom}-{base_zoom})*{ease}))':"
            f"h='ih*({base_zoom}+(({end_zoom}-{base_zoom})*{ease}))':eval=frame,"
            f"fps={fps},trim=duration={sec},setpts=PTS-STARTPTS[fg{i}]"
        )
        start_x = f"min(max(({start_fx}*iw)-(ow/2),0),max(iw-ow,0))"
        end_x = f"min(max(({end_fx}*iw)-(ow/2),0),max(iw-ow,0))"
        start_y = f"min(max(({start_fy}*ih)-(oh/2),0),max(ih-oh,0))"
        end_y = f"min(max(({end_fy}*ih)-(oh/2),0),max(ih-oh,0))"
        crop_x = f"({start_x})+(({end_x})-({start_x}))*{ease}"
        crop_y = f"({start_y})+(({end_y})-({start_y}))*{ease}"
        comp = (
            f"[fg{i}]crop={width}:{height}:x='{crop_x}':y='{crop_y}',"
            "eq=contrast=1.05:brightness=0.01:saturation=1.04:gamma=0.98,"
            "vignette=PI/10,"
            "noise=alls=3:allf=t+u,"
            f"format=yuv420p[v{i}]"
        )
        return ";\n".join([fg, comp])

    bg = (
        f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
        f"boxblur={blur_strength}:1,eq=brightness=-0.04:saturation=1.08,"
        f"fps={fps},trim=duration={sec},setpts=PTS-STARTPTS[bg{i}]"
    )
    fg = f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,setsar=1,fps={fps},trim=duration={sec},setpts=PTS-STARTPTS[fg{i}]"
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


def build_xfade_chain(photo_durations: List[float], xfade, transition="auto"):
    n = len(photo_durations)
    if n == 1:
        return "[v0]format=yuv420p[vout]"
    parts = []
    cur = "[v0]"
    cumulative = photo_durations[0]
    for i in range(1, n):
        offset = cumulative - (i * xfade)
        nxt = f"[v{i}]"
        out = f"[x{i}]"
        resolved_transition = resolve_transition_name(transition, i)
        parts.append(
            f"{cur}{nxt}xfade=transition={resolved_transition}:duration={xfade}:offset={offset}{out}"
        )
        cur = out
        cumulative += photo_durations[i]
    total = estimate_duration_variable(photo_durations, xfade)
    parts.append(f"{cur}trim=duration={total},format=yuv420p[vout]")
    return ";\n".join(parts)


def run_ffmpeg_with_progress(cmd: List[str], total_duration: float):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert proc.stdout is not None
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
    images,
    out_path: Path,
    width,
    height,
    fps=30,
    sec=2.8,
    xfade=0.7,
    transition="auto",
    blur_strength=18,
    bitrate="15M",
    quality_name="standard",
    encoder="h264_videotoolbox",
    motion_style="none",
    ken_override=None,
    parallax_override=None,
    motion_seed=0,
    rhythm_strength=0.12,
    focal_points: Optional[List[Optional[Tuple[float, float]]]] = None,
    audio_path: Optional[Path] = None,
    audio_offset: float = 0.0,
):
    photo_durations = build_photo_durations(len(images), sec, xfade, rhythm_strength, motion_seed)
    duration = estimate_duration_variable(photo_durations, xfade)
    ken, para = resolve_motion_values(motion_style, ken_override, parallax_override, min(width, height), sec)

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats"]
    if encoder == "h264_videotoolbox":
        cmd += ["-hwaccel", "videotoolbox"]
    for img, still_sec in zip(images, photo_durations):
        cmd += ["-loop", "1", "-t", str(still_sec), "-i", str(img)]
    if audio_path is not None:
        if audio_offset > 0:
            cmd += ["-ss", str(audio_offset)]
        cmd += ["-i", str(audio_path)]

    filters = [
        build_filter_for_still(
            i,
            width,
            height,
            fps,
            still_sec,
            blur_strength,
            ken,
            para,
            motion_seed,
            focal_points[i] if focal_points else None,
        )
        for i, still_sec in enumerate(photo_durations)
    ]
    filter_complex = ";\n".join(filters) + ";\n" + build_xfade_chain(photo_durations, xfade, transition)

    cmd += ["-filter_complex", filter_complex, "-map", "[vout]", "-r", str(fps), "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    if audio_path is not None:
        audio_index = len(images)
        cmd += ["-map", f"{audio_index}:a", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    if encoder == "h264_videotoolbox":
        cmd += ["-c:v", "h264_videotoolbox", "-b:v", bitrate]
    else:
        crf, preset = x264_tuning_for_quality(quality_name)
        cmd += ["-c:v", "libx264", "-crf", crf, "-preset", preset]

    cmd += [str(out_path)]
    return cmd, duration


def render(images, out_path: Path, width, height, fps=30, sec=2.8, xfade=0.7, transition="auto", blur_strength=18,
           bitrate="15M", quality_name="standard", encoder="h264_videotoolbox",
           motion_style="none", ken_override=None, parallax_override=None, motion_seed=0,
           rhythm_strength=0.12, focal_points: Optional[List[Optional[Tuple[float, float]]]] = None,
           audio_path: Optional[Path] = None,
           audio_offset: float = 0.0):
    render_kwargs = dict(
        images=images, out_path=out_path, width=width, height=height,
        fps=fps, sec=sec, xfade=xfade, transition=transition,
        blur_strength=blur_strength, bitrate=bitrate, quality_name=quality_name,
        encoder=encoder, motion_style=motion_style, ken_override=ken_override,
        parallax_override=parallax_override, motion_seed=motion_seed,
        rhythm_strength=rhythm_strength, focal_points=focal_points,
        audio_path=audio_path, audio_offset=audio_offset,
    )
    cmd, duration = build_render_command(**render_kwargs)  # type: ignore[arg-type]
    try:
        run_ffmpeg_with_progress(cmd, duration)
    except RuntimeError as exc:
        message = str(exc)
        videotoolbox_failed = (
            encoder == "h264_videotoolbox"
            and ("Error while opening encoder" in message or "Could not open encoder" in message)
        )
        if not videotoolbox_failed:
            raise
        print("VideoToolbox encoder failed; retrying with libx264")
        render_kwargs["encoder"] = "libx264"
        fallback_cmd, fallback_duration = build_render_command(**render_kwargs)  # type: ignore[arg-type]
        run_ffmpeg_with_progress(fallback_cmd, fallback_duration)


def sort_images_and_infos(
    images: List[Path],
    infos: List[Optional[PhotoInfo]],
    sort_by="natural",
    seed=0,
) -> Tuple[List[Path], List[Optional[PhotoInfo]]]:
    paired = list(zip(images, infos))
    if sort_by == "natural":
        return images, infos
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
        with_gps.sort(key=lambda item: item[1].gps_coords or (float("inf"), float("inf")))
        ordered = with_gps + no_gps
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
) -> List[Tuple[List[Path], List[Optional[PhotoInfo]], List[Optional[Tuple[float, float]]]]]:
    """Greedily group photos into parts whose estimated duration does not exceed max_sec."""
    parts = []
    cur_imgs: List[Path] = []
    cur_infos: List[Optional[PhotoInfo]] = []
    cur_focal: List[Optional[Tuple[float, float]]] = []

    for img, info, fp in zip(images, infos, focal_points):
        trial = cur_imgs + [img]
        trial_dur = build_photo_durations(len(trial), base_sec, xfade, rhythm_strength, seed)
        trial_total = estimate_duration_variable(trial_dur, xfade)
        if trial_total > max_sec and cur_imgs:
            parts.append((cur_imgs, cur_infos, cur_focal))
            cur_imgs, cur_infos, cur_focal = [img], [info], [fp]
        else:
            cur_imgs, cur_infos, cur_focal = trial, cur_infos + [info], cur_focal + [fp]

    if cur_imgs:
        parts.append((cur_imgs, cur_infos, cur_focal))

    return parts


def parse_args():
    ap = argparse.ArgumentParser(description="Create slideshow videos from photos.")
    ap.add_argument("source_dir", nargs="?")
    ap.add_argument("--outdir", default="./Renders")
    ap.add_argument("--workdir", default="./.work_pngs")
    ap.add_argument("--quality", default="standard", choices=["draft", "standard", "high"])
    ap.add_argument("--format", default="both", choices=["16x9", "9x16", "both"])
    ap.add_argument("--sort-by", default="natural", choices=["natural", "time", "location", "random"])
    ap.add_argument("--max-workers", type=int, default=0)
    ap.add_argument("--camera-stats", action="store_true")
    ap.add_argument("--motion-style", default="none", choices=["none", "kenburns", "parallax", "both"])
    ap.add_argument("--ken-burns-strength", type=float, default=None)
    ap.add_argument("--parallax-px", type=int, default=None)
    ap.add_argument("--smart-focus", action="store_true",
                    help="Use MediaPipe face detection with pose fallback to bias Ken Burns framing")
    ap.add_argument("--progress", action="store_true",
                    help="Show progress updates during preparation and rendering")
    ap.add_argument("--sec", type=float, default=2.8)
    ap.add_argument("--xfade", type=float, default=0.7)
    ap.add_argument("--transition", default="auto", type=validate_transition, choices=TRANSITION_CHOICES)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--rhythm-strength", type=float, default=0.12,
                    help="Per-photo timing variation strength (0.0 to 0.25)")
    ap.add_argument("--audio", default=None,
                    help="Path to an audio file to mix into the slideshow. Trimmed to video length.")
    ap.add_argument("--split-secs", type=float, default=None,
                    help="Also render split parts, each no longer than this many seconds")
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


def build_targets(fmt: str, stamp: str, input_dir_name: str, quality: str, transition: str, photo_count: int):
    """Build render targets using the agreed deterministic filename schema."""
    base = (
        f"{stamp}_{_slug(input_dir_name)}"
        f"_fmt{{fmt}}"
        f"_q{_slug(quality)}"
        f"_transition-{_slug(transition)}"
        f"_n{photo_count}"
    )
    targets = []
    if fmt in ("16x9", "both"):
        targets.append((base.format(fmt="16x9") + ".mp4", 1920, 1080))
    if fmt in ("9x16", "both"):
        targets.append((base.format(fmt="9x16") + ".mp4", 1080, 1920))
    return targets


def validate_transition(transition: str) -> str:
    normalized = transition.strip().lower()
    if normalized not in VALID_TRANSITIONS:
        supported = ", ".join(sorted(VALID_TRANSITIONS))
        raise SystemExit(f"--transition must be one of: {supported}")
    return normalized


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


def build_youtube_title(output_path: Path, input_dir: Path, fmt: str, custom_title: Optional[str]) -> str:
    if custom_title:
        return custom_title.format(
            stem=output_path.stem,
            filename=output_path.name,
            format=fmt,
            input_dir=input_dir.name,
        )
    return f"{input_dir.name} slideshow ({fmt})"


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
        creds.refresh(Request())
    elif not creds or not creds.valid:
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
        media_body=MediaFileUpload(str(video_path), chunksize=-1, resumable=True),
    )

    response = None
    retries = 0
    while response is None:
        try:
            _, response = request.next_chunk()
        except HttpError as exc:
            if exc.resp.status not in (500, 502, 503, 504) or retries >= 5:
                raise
            retries += 1
            print(f"Upload chunk failed (HTTP {exc.resp.status}), retrying ({retries}/5)...")
            time.sleep(2 ** retries)

    return response["id"]


def main():
    t0 = time.time()
    args = parse_args()
    upload_only_path = Path(args.youtube_upload_file) if args.youtube_upload_file else None
    should_upload = args.youtube_upload or upload_only_path is not None

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
    if not (0.0 <= args.rhythm_strength <= 0.25):
        raise SystemExit("--rhythm-strength must be between 0.0 and 0.25")
    if should_upload and not args.youtube_category.isdigit():
        raise SystemExit("--youtube-category must be a numeric YouTube category ID")
    if args.split_secs is not None and args.split_secs <= 0:
        raise SystemExit("--split-secs must be > 0")
    audio_path = Path(args.audio) if args.audio else None
    if audio_path is not None and not audio_path.is_file():
        raise SystemExit(f"--audio file not found: {audio_path}")
    outdir = Path(args.outdir)
    ensure_dir(outdir)

    preset = QUALITY_PRESETS[args.quality]
    fps = preset["fps"]
    youtube_client_secrets = Path(args.youtube_client_secrets)
    youtube_token_file = Path(args.youtube_token_file)
    youtube_tags = parse_youtube_tags(args.youtube_tags)

    if upload_only_path is not None:
        if not upload_only_path.exists() or not upload_only_path.is_file():
            raise SystemExit(f"Missing YouTube upload file: {upload_only_path}")

        input_dir = Path(args.source_dir) if args.source_dir else upload_only_path.parent
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
        return

    if not args.source_dir:
        raise SystemExit("source_dir is required unless --youtube-upload-file is used")

    src = Path(args.source_dir)
    if not src.exists() or not src.is_dir():
        raise SystemExit(f"Missing source directory: {src}")

    parent = Path(args.workdir).parent if Path(args.workdir).parent != Path("") else Path(".")
    ensure_dir(parent)
    temp_work = Path(tempfile.mkdtemp(prefix="videophotoslide_", dir=str(parent)))

    try:
        blur = preset["blur_strength"]
        bitrate = preset["bitrate"]

        extract_exif = args.sort_by in ("time", "location") or args.camera_stats
        detect_focus = args.smart_focus and args.motion_style in ("kenburns", "both")
        progress_print(args.progress, f"[phase prep] scanning {src}")
        if detect_focus:
            progress_print(args.progress, "[phase prep] initializing smart focus detectors")
        progress_print(args.progress, f"[phase prep] found image candidates and starting {'conversion + smart focus' if detect_focus else 'conversion'}")
        images, infos = convert_to_pngs(
            src,
            temp_work,
            extract_exif=extract_exif,
            detect_focus=detect_focus,
            max_workers=args.max_workers,
            show_progress=args.progress,
        )
        if not images:
            raise SystemExit("No supported images found.")

        progress_print(args.progress, f"[phase prep] ordering {len(images)} prepared images with sort={args.sort_by}")
        images, infos = sort_images_and_infos(images, infos, sort_by=args.sort_by, seed=args.seed)
        if args.camera_stats:
            print_camera_stats(infos)
        focal_points = [info.focal_point if info else None for info in infos]
        if detect_focus:
            focused_count = sum(1 for point in focal_points if point is not None)
            print(f"Smart focus: detected subjects in {focused_count}/{len(focal_points)} images")

        encoder = "h264_videotoolbox" if ffmpeg_has_encoder("h264_videotoolbox") else "libx264"
        estimated_total = estimate_duration_variable(
            build_photo_durations(len(images), args.sec, args.xfade, args.rhythm_strength, args.seed),
            args.xfade,
        )
        print(
            "Render settings: "
            f"encoder={encoder}, transition={args.transition}, motion={args.motion_style}, "
            f"estimated_duration={estimated_total:.1f}s"
        )
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        targets = build_targets(
            args.format,
            stamp,
            src.name,
            args.quality,
            args.transition,
            len(images),
        )

        if args.split_secs is not None:
            part_groups = split_photos_into_parts(
                images, infos, focal_points,
                args.sec, args.xfade, args.rhythm_strength, args.seed,
                args.split_secs,
            )
            if len(part_groups) <= 1:
                progress_print(args.progress, "[phase split] skipped: content fits in a single part")
                part_groups = []
        else:
            part_groups = []

        part_audio_offsets: List[float] = []
        cumulative = 0.0
        for part_imgs, _, _ in part_groups:
            part_audio_offsets.append(cumulative)
            cumulative += estimate_duration_variable(
                build_photo_durations(len(part_imgs), args.sec, args.xfade, args.rhythm_strength, args.seed),
                args.xfade,
            )

        outputs = []
        for name, width, height in targets:
            out = outdir / name
            progress_print(args.progress, f"[phase render] starting {width}x{height} -> {out.name}")
            render(
                images,
                out,
                width,
                height,
                fps=fps,
                sec=args.sec,
                xfade=args.xfade,
                transition=args.transition,
                blur_strength=blur,
                bitrate=bitrate,
                quality_name=args.quality,
                encoder=encoder,
                motion_style=args.motion_style,
                ken_override=args.ken_burns_strength,
                parallax_override=args.parallax_px,
                motion_seed=args.seed,
                rhythm_strength=args.rhythm_strength,
                focal_points=focal_points,
                audio_path=audio_path,
            )
            outputs.append(out)
            progress_print(args.progress, f"[phase render] completed {out.name}")

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

            if part_groups:
                progress_print(args.progress, f"[phase split] rendering {len(part_groups)} parts (<={args.split_secs}s each)")
                for part_idx, (part_imgs, _, part_focal) in enumerate(part_groups, start=1):
                    part_out = outdir / f"{out.stem}_part{part_idx:03d}.mp4"
                    progress_print(args.progress, f"[phase split] part {part_idx}/{len(part_groups)}: {len(part_imgs)} images -> {part_out.name}")
                    render(
                        part_imgs, part_out, width, height,
                        fps=fps, sec=args.sec, xfade=args.xfade,
                        transition=args.transition, blur_strength=blur,
                        bitrate=bitrate, quality_name=args.quality,
                        encoder=encoder, motion_style=args.motion_style,
                        ken_override=args.ken_burns_strength,
                        parallax_override=args.parallax_px,
                        motion_seed=args.seed, rhythm_strength=args.rhythm_strength,
                        focal_points=part_focal, audio_path=audio_path,
                        audio_offset=part_audio_offsets[part_idx - 1],
                    )
                    outputs.append(part_out)
                    progress_print(args.progress, f"[phase split] completed {part_out.name}")

                    if args.add_to_photos:
                        progress_print(args.progress, f"[phase photos] importing {part_out.name}")
                        print(f"Importing into Photos: {part_out.name}")
                        import_media_to_photos([part_out])

                    if args.youtube_upload:
                        part_title = build_youtube_title(part_out, src, f"{width}x{height}", args.youtube_title)
                        progress_print(args.progress, f"[phase youtube] uploading {part_out.name}")
                        print(f"Uploading to YouTube: {part_title}")
                        part_id = upload_video_to_youtube(
                            video_path=part_out,
                            title=part_title,
                            description=args.youtube_description,
                            tags=youtube_tags,
                            category=args.youtube_category,
                            privacy=args.youtube_privacy,
                            client_secrets=youtube_client_secrets,
                            token_file=youtube_token_file,
                        )
                        print(f"YouTube upload complete: https://www.youtube.com/watch?v={part_id}")
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
