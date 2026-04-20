# CLAUDE.md — Memora Motion

## Project Overview

**Memora Motion** — an installable Python CLI tool that turns folders of photos and video clips into polished slideshow videos in 16x9 and 9x16 formats. Main executable: `memoramotion`; implementation module: `memoramotion.py`. No web server, no database, no framework.

Key output characteristics:
- Deterministic, descriptive filenames: `<timestamp>_<folder>_<format>_<resolution>_q<quality>_t-<transition>_n<photos>[c<clips>]_m-<motion>[_kb-<engine>][_fps<n>][_sort-<mode>]_dur<duration>.mp4` — `c<clips>` suffix only when clips present; `_kb-`, `_fps`, `_sort-` only when non-default
- Both aspect ratios rendered in one pass by default
- Resolution presets cover 1080p, 1440p, 4K/2160p, and experimental 8K/4320p
- All visual effects (filmic grade, vignette, grain, Ken Burns, parallax) composed in FFmpeg filter graphs
- Hardware-accelerated encoding via `h264_videotoolbox` with `libx264` fallback

---

## Architecture

**Single runtime module**: `memoramotion.py`. Packaging metadata lives in `pyproject.toml` and exposes the `memoramotion` console script.

**Processing pipeline** (in execution order):
0. Preflight `ffmpeg`/`ffprobe`; if `--youtube-upload` is set, validate/refresh YouTube OAuth credentials before rendering so auth failures happen early
1. Detect encoder (`h264_videotoolbox` or `libx264`); print resolved settings via `build_effective_settings()` / `print_effective_settings()` unless `--settings off`
2. Scan input dir for supported files → `IMG_EXTS` + `VID_EXTS` — orchestrated by `_phase_prep()`
3. Images: convert to normalized PNG in parallel → `convert_to_pngs()` / `_convert_single_image()`
   Video clips: probe via ffprobe → `probe_video_clip()`
   Merge in natural sort order → `collect_media()`
4. Extract photo metadata (EXIF, GPS, camera) → `get_image_metadata()`; clips get creation time from container
5. Optional subject detection on photos → `detect_subject_focus()` (MediaPipe)
6. Sort → `sort_images_and_infos()`
7. Optional geocoding → `geocode_photos()`
8. Build per-item FFmpeg filters → `build_filter_for_still()` (dispatches to `_filter_still_no_motion`, `_filter_still_fit_overlay`, `_filter_still_fixed_viewport`, `_filter_still_preserve_stage`) or `build_filter_for_clip()` — orchestrated by `_phase_render()`
9. Chain transitions → `build_xfade_chain()`
10. Execute ffmpeg → `run_ffmpeg_with_progress()`
11. Post-render: YouTube upload and/or macOS Photos import

**Key data structure**: `PhotoInfo` dataclass (path, dimensions, aspect ratio, EXIF, GPS, camera, focal point, `is_video`, `video_duration`).

The `images` list and `infos` list must stay index-aligned through all sorts and splits. Images contains PNG paths for photos and original file paths for video clips. Always check `info.is_video` before assuming an item is a still.

---

## Key Constants

```python
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif"}
VID_EXTS = {".mp4", ".mov"}  # matched case-insensitively via .suffix.lower()

QUALITY_PRESETS = {
    "draft":    {"fps": 24, "blur_strength": 12, "bitrate": "8M"},
    "standard": {"fps": 30, "blur_strength": 18, "bitrate": "15M"},
    "high":     {"fps": 30, "blur_strength": 22, "bitrate": "25M"},
    "youtube":  {"fps": 30, "blur_strength": 22, "bitrate": "youtube"},
    "max":      {"fps": 30, "blur_strength": 22, "bitrate": "max"},
}

RESOLUTION_PRESETS = {
    "1080p": {"16x9": (1920, 1080), "9x16": (1080, 1920)},
    "1440p": {"16x9": (2560, 1440), "9x16": (1440, 2560)},
    "4k": {"16x9": (3840, 2160), "9x16": (2160, 3840)},
    "8k": {"16x9": (7680, 4320), "9x16": (4320, 7680)},
}

YOUTUBE_SDR_BITRATES = {
    "1080p": {"standard_fps": "8M", "high_fps": "12M", "max": "12M"},
    "1440p": {"standard_fps": "16M", "high_fps": "24M", "max": "24M"},
    "4k": {"standard_fps": "45M", "high_fps": "68M", "max": "68M"},
    "8k": {"standard_fps": "160M", "high_fps": "240M", "max": "240M"},
}

MOTION_PRESETS = {
    "none":     {"ken": 0.0,    "parallax_ratio": 0.0},
    "kenburns": {"ken": 0.0015, "parallax_ratio": 0.0},
    "parallax": {"ken": 0.0,    "parallax_ratio": 0.0028},
    "both":     {"ken": 0.0015, "parallax_ratio": 0.0028},
}

MOTION_STYLE_CHOICES = ["auto", "none", "kenburns", "parallax", "both"]
KEN_BURNS_ENGINE_CHOICES = ["fit-overlay", "preserve-stage", "fixed-viewport"]
PRO_TRANSITIONS = ["fade", "smoothleft", "smoothright"]  # auto mode rotates through these
```

---

## Resolution and Quality

`--format` chooses aspect ratio (`16x9`, `9x16`, or `both`). `--resolution` chooses the pixel dimensions for each aspect ratio. `build_targets()` combines them and includes `_res<resolution>` in filenames so different resolution renders do not collide.

`--quality youtube` and `--quality max` do not use fixed bitrates from `QUALITY_PRESETS`. `main()` calls `resolve_output_bitrate()` after resolving `fps` and normalized `resolution`:
- `youtube`: uses `standard_fps` for `fps <= 30`, `high_fps` for `fps > 30`
- `max`: uses the table's `max` value regardless of fps
- `--bitrate` always wins over the preset

The YouTube bitrate table mirrors the SDR H.264 recommendations in YouTube Help's recommended upload encoding settings. Re-check that source before changing these values. YouTube also currently documents a 256 GB or 12 hour maximum upload size for verified accounts, whichever is less.

Recommended practical YouTube command:

```bash
memoramotion ./input_photos --format both --resolution 4k --quality youtube
```

Absolute largest built-in target:

```bash
memoramotion ./input_photos --format 16x9 --resolution 8k --quality max
```

---

## Smart Focus and MediaPipe

`--smart-focus` is optional and lazy-loads MediaPipe only when subject-aware Ken Burns framing is requested. The current implementation uses MediaPipe Tasks:
- `mediapipe.tasks.python.vision.FaceDetector`
- `mediapipe.tasks.python.vision.PoseLandmarker`

Keep MediaPipe imports dynamic through `importlib.import_module()` so importing the CLI stays light for users who never enable smart focus. The Tasks API also needs model assets: by default the app downloads the Face Detector and Pose Landmarker models into `./.mediapipe_models`, while `--smart-focus-face-model` and `--smart-focus-pose-model` let callers pin local assets.

When changing smart focus behavior, update `_get_mediapipe_detectors()`, `detect_subject_focus()`, model resolution flags, README install notes, and the MediaPipe tests together. Multiprocessing conversion relies on resolved model paths being passed through `_init_mediapipe_worker()` so spawned workers can initialize detectors cleanly.

---

## Mixed Media (Photos + Video Clips)

Photos (`.jpg`, `.heic`, etc.) and video clips (`.mp4`, `.mov`) can be mixed freely in the same input directory. They are interleaved in natural filename sort order.

**Clip behaviors vs. photos:**
- Clips play at their **natural duration** — `--sec` does not apply
- `--motion-style` is **not applied** to clips (they already have motion)
- `--sort-by time` uses the MP4/MOV container `creation_time` tag, falling back to file mtime
- Clips stay at their original file path; photos are converted to normalized PNGs in the temp work dir

**Clip-specific flags:**

| Flag | Default | Values | Description |
|---|---|---|---|
| `--clip-grade` | `full` | `none`, `grade`, `full` | `none` = passthrough; `grade` = filmic color only; `full` = grade + vignette + grain |
| `--clip-audio` | `mute` | `mute`, `keep`, `duck` | `mute` = silence clip audio; `keep` = mix at full volume; `duck` = lower background to 20% during clips |

**Audio routing:**
- `mute`: clip audio discarded; background `--audio` plays at full volume via the simple `-map + apad` path
- `keep`/`duck`: routed through `filter_complex`. Each clip's audio stream is delayed to its timeline position via `adelay`. `duck` applies a `volume='if(...between(t,...),0.2,1.0)'` expression to the background track.

**Key functions:**

| Function | Purpose |
|---|---|
| `probe_video_clip(path)` | ffprobe a clip → `PhotoInfo(is_video=True, video_duration=...)` |
| `collect_media(input_dir, ...)` | Entry point for main; merges images + clips in natural sort order |
| `build_media_durations(infos, ...)` | Per-item durations: clips → natural duration; photos → rhythm-varied `--sec` |
| `_media_start_times(durations, xfade)` | Timeline start offsets per item — used for both `adelay` in audio filters and `offset` in `build_xfade_chain()` |
| `build_filter_for_clip(i, ...)` | ffmpeg filter chain for a clip: bg+fg composite, optional grade/vignette/grain |
| `_build_clip_audio_filters(...)` | Builds audio `filter_complex` fragment for `keep`/`duck` → `[aout]` |

---

## FFmpeg Filter Graph Structure

**Photos** — `build_filter_for_still()`:
- Input via `-loop 1 -t <dur>` (still image looped for its duration)
- **No motion** (`ken_strength == 0`): `scale (bg fill, increase) → boxblur` + `scale (fg fit, decrease)` → `overlay=(W-w)/2:(H-h)/2` → grade → vignette → grain. Optional parallax animates the overlay x position.
- **Ken Burns / `fit-overlay` engine** (`ken_strength > 0`, default unless smart focus auto-selects `fixed-viewport`): same bg path as no-motion; fg uses `scale (decrease, fit) → fps/trim/setpts → scale (zoom, eval=frame)`. The zoom uses duration-based strength, smootherstep easing, and multiplicative interpolation so higher FPS only improves smoothness instead of increasing zoom distance. Smart-focus points are blended toward center by `KEN_BURNS_FOCUS_BIAS` so they gently bias the zoom instead of dragging the photo.
- **Ken Burns / `preserve-stage` engine** (`--ken-burns-engine preserve-stage`): full-photo-preserving zoompan path. It fits the foreground into a smaller transparent full-frame stage, uses `zoompan` to animate that fixed-size stage with a small preservation margin, then overlays the full-frame result at `0:0`. This keeps full-photo visibility, but the visible foreground footprint may subtly resize. `fixed-frame` remains accepted as an alias for this engine.
- **Ken Burns / `fixed-viewport` engine** (`--ken-burns-engine fixed-viewport`): stable-footprint path. It computes the fitted photo viewport from `PhotoInfo` dimensions, keeps that viewport centered and fixed, then uses `zoompan` to zoom/pan content inside it. This is the cleanest smart-focus motion, but it can crop during the zoom.
- `--motion-style` defaults to auto: `none` normally, `kenburns` when `--smart-focus` is enabled.
- `--ken-burns-engine` defaults to auto: `fit-overlay` normally, `fixed-viewport` when `--smart-focus` resolves to `kenburns` or explicit `both`. Explicit engine flags override auto selection.
- Both functions accept `use_lanczos: bool` (set by `build_render_command` when `quality_name` is `high`, `youtube`, or `max`). When true, `:flags=lanczos` is appended to all fg scale filters; the blurred bg scale is unaffected.

**Clips** — `build_filter_for_clip()`:
- Input via plain `-i` (no loop)
- Filter chain: `scale (bg fill) → boxblur → overlay (fg) → [optional grade/vignette/grain per --clip-grade]`
- No motion applied
- Also accepts `use_lanczos` (same logic as stills)

**Transitions** — `build_xfade_chain()`:
- ffmpeg `xfade` filter chained between all items
- Transition offset per item = `_media_start_times(durations, xfade)[i]` — the shared timeline start of each item. This keeps xfade offset computation consistent with audio delay offsets.
- `auto` mode rotates through `PRO_TRANSITIONS`

**Rhythm pacing** — `build_photo_durations()`:
- Per-shot durations vary via a sine wave over a **half-cycle** (0 → π) across the slideshow, creating one slow→fast→slow arc rather than a full oscillation. Jitter is layered on top for natural variation.

**Location overlays** — pre-rendered to transparent PNGs, composited via `overlay=0:0` after the per-item filter.

---

## Running the CLI

```bash
source .venv/bin/activate
memoramotion <source_dir> [OPTIONS]
```

Common invocations:
```bash
# Default: both formats, standard quality, auto transition
memoramotion ./input_photos

# Dry run — prints ffmpeg command without rendering
memoramotion ./input_photos --dry-run --progress

# Inspect all resolved settings before any media work (default: on)
memoramotion ./input_photos --quality youtube --resolution 4k --settings on

# Machine-readable settings for scripting
memoramotion ./input_photos --settings json

# Suppress the settings block in automated pipelines
memoramotion ./input_photos --settings off

# Video clips only, muted, full grade treatment
memoramotion "Photos/Videotest" --format 9x16

# Video clips with duck audio (background lowers during clips)
memoramotion "Photos/Videotest" --format 9x16 --clip-audio duck --audio ./music.mp3

# Vertical only, with smart subject framing on photos
memoramotion ./input_photos --format 9x16 --smart-focus

# Practical maximum-quality YouTube render
memoramotion ./input_photos --format both --resolution 4k --quality youtube

# Render and upload to YouTube
memoramotion ./input_photos --format 16x9 --youtube-upload --youtube-privacy private

# Upload existing render without re-rendering
memoramotion --youtube-upload-file ./Renders/some.mp4 --youtube-title "{filename}"

# Split long render into parts
memoramotion ./input_photos --split-secs 60 --dry-run
```

---

## Testing

```bash
source .venv/bin/activate
python -m unittest tests/test_memoramotion.py
```

Test file: `tests/test_memoramotion.py`. Uses `unittest` with mocking. Tests cover: metadata alignment after sorting, filename collision safety, resolution target generation, YouTube bitrate selection, transition validation, motion/parallax combinations, location overlay generation, and rhythm calculations.

When adding new features:
- Add unit tests for any new pure functions
- Sorting/reordering functions must test that `infos` stays aligned with `images`
- Filename or output target changes must update the output name and resolution validation tests

---

## Code Conventions

- **No external config files** — all settings are CLI args with defaults, no YAML/TOML/JSON config
- **Path handling** — always use `pathlib.Path`, never string concatenation for paths
- **Temp work directory** — `tempfile.mkdtemp(prefix="memoramotion_", dir=args.workdir)` creates a session-specific subdir inside `--workdir` (default `./.work_pngs`). Only that subdir is deleted in `finally` — the workdir parent is never removed. Video clips are never copied; they're referenced at their original path.
- **FFmpeg/ffprobe invocation** — build command as a list of strings, execute via `subprocess.run()`, never `shell=True`
- **Encoder selection** — always check `ffmpeg_has_encoder("h264_videotoolbox")` at runtime, fall back to `libx264`
- **Parallelism** — image conversion uses `multiprocessing.Pool`; clip probing and everything else is single-threaded
- **Print style** — plain `print()` for user output; `progress_print(args.progress, msg)` for phase progress (only shown with `--progress`)

---

## What NOT to Do

- **Don't use `shell=True`** in subprocess calls — all ffmpeg/ffprobe commands are built as lists
- **Don't modify `images` without also modifying `infos`** — they must stay index-aligned
- **Don't assume every item in `images` is a PNG** — clips stay at their original `.mp4`/`.mov` path; check `info.is_video`
- **Don't apply motion style to clips** — `build_filter_for_clip()` never calls Ken Burns/parallax logic
- **Don't add new CLI flags without updating README.md** — the README table is the canonical user-facing option reference
- **Don't use absolute paths** in output filenames — outputs always land in `outdir` (default `./Renders`)
- **Don't skip the `finally` cleanup** — temp work PNGs can be several hundred MB
- **Don't break deterministic output naming** — filename format is the primary way users track render versions

---

## Dependencies

| Dependency | Default install | Purpose |
|---|---|---|
| ffmpeg + ffprobe | Yes | All video rendering and clip probing |
| pillow | Yes | Image processing, EXIF extraction |
| pillow-heif | Yes | HEIC/HEIF image input support |
| numpy | Yes | MediaPipe image conversion and numeric helpers |
| google-api-python-client | Yes | YouTube upload |
| google-auth-oauthlib | Yes | YouTube OAuth |
| mediapipe | Yes | Smart focus subject detection |

Install for development with `pip install -r requirements.txt`; the project dependencies are declared in `pyproject.toml`. `requirements-core.txt` is a lightweight source-checkout path for normal rendering without MediaPipe or Google API libraries.

---

## Files to Know

| File | Purpose |
|---|---|
| `memoramotion.py` | Entire implementation |
| `pyproject.toml` | Package metadata and `memoramotion` console-script entry point |
| `requirements-core.txt` | Lightweight source-checkout dependencies |
| `tests/test_memoramotion.py` | Unit test suite |
| `README.md` | User-facing docs and option table |
| `client_secrets.json` | Google OAuth credentials (not committed) |
| `.youtube_token.json` | Cached OAuth token (not committed) |
| `Photos/` | Organized source media collections by project |
| `Renders/` | All rendered video outputs |
