# CLAUDE.md — video-workflow

## Project Overview

**Memora: video-workflow** — a CLI tool that turns photo folders into polished slideshow videos in 16x9 and 9x16 formats. Single script: `videophotoslide.py`. No web server, no database, no framework.

Key output characteristics:
- Deterministic, descriptive filenames: `<timestamp>_<folder>_fmt<format>_q<quality>_transition-<transition>_n<count>.mp4`
- Both aspect ratios rendered in one pass by default
- All visual effects (filmic grade, vignette, grain, Ken Burns, parallax) composed in FFmpeg filter graphs
- Hardware-accelerated encoding via `h264_videotoolbox` with `libx264` fallback

---

## Architecture

**Single file**: `videophotoslide.py` (~1,600 lines). No separate modules.

**Processing pipeline** (in execution order):
1. Scan input dir for supported files → `IMG_EXTS`
2. Convert to normalized PNG → `convert_to_pngs()` (parallel via `multiprocessing.Pool`)
3. Extract metadata (EXIF, GPS, camera) → `get_image_metadata()`
4. Optional subject detection → `detect_subject_focus()` (MediaPipe)
5. Sort photos → `sort_images_and_infos()`
6. Optional geocoding → `geocode_photos()`
7. Build FFmpeg filter graph → `build_filter_for_still()` + `build_xfade_chain()`
8. Execute ffmpeg → `run_ffmpeg_with_progress()`
9. Post-render: YouTube upload and/or macOS Photos import

**Key data structure**: `PhotoInfo` dataclass (path, dimensions, aspect ratio, EXIF, GPS, camera, focal point). The `images` list (Paths to work PNGs) and `infos` list (PhotoInfo) must stay aligned through all sorts and splits. Any reorder of one must reorder the other in parallel.

---

## Key Constants

```python
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif"}

QUALITY_PRESETS = {
    "draft":    {"fps": 24, "blur_strength": 12, "bitrate": "8M"},
    "standard": {"fps": 30, "blur_strength": 18, "bitrate": "15M"},
    "high":     {"fps": 30, "blur_strength": 22, "bitrate": "25M"},
}

MOTION_PRESETS = {
    "none":     {"ken": 0.0,    "parallax_ratio": 0.0},
    "kenburns": {"ken": 0.0015, "parallax_ratio": 0.0},
    "parallax": {"ken": 0.0,    "parallax_ratio": 0.0028},
    "both":     {"ken": 0.0015, "parallax_ratio": 0.0028},
}

PRO_TRANSITIONS = ["fade", "smoothleft", "smoothright"]  # auto mode rotates through these
```

---

## Running the Script

```bash
source .venv/bin/activate
python videophotoslide.py ./input_photos [OPTIONS]
```

Common invocations:
```bash
# Default: both formats, standard quality, auto transition
python videophotoslide.py ./input_photos

# Dry run to inspect FFmpeg command without rendering
python videophotoslide.py ./input_photos --dry-run

# Vertical only, with Ken Burns and smart subject framing
python videophotoslide.py ./input_photos --format 9x16 --motion-style kenburns --smart-focus

# Render and upload to YouTube
python videophotoslide.py ./input_photos --format 16x9 --youtube-upload --youtube-privacy private

# Upload existing render without re-rendering
python videophotoslide.py --youtube-upload-file ./Renders/some.mp4 --youtube-title "{filename}"

# Check what ffmpeg command would be built for a split render
python videophotoslide.py ./input_photos --split-secs 60 --dry-run
```

---

## Testing

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

Test file: `tests/test_videophotoslide.py`. Uses `unittest` with mocking. Tests cover: metadata alignment after sorting, filename collision safety, transition validation, motion/parallax combinations, location overlay generation, rhythm calculations.

When adding new features:
- Add unit tests for any new pure functions
- Sorting/reordering functions must test that `infos` stays aligned with `images`
- Filename format changes must update the output name validation tests

---

## Code Conventions

- **No external config files** — all settings are CLI args with defaults, no YAML/TOML/JSON config
- **Path handling** — always use `pathlib.Path`, never string concatenation for paths
- **Temp work directory** — normalized PNGs go to `.work_pngs/` inside a `tempfile.mkdtemp()` subtree; always cleaned up in `finally`
- **FFmpeg invocation** — build command as a list of strings, execute via `subprocess.run()`, never `shell=True`
- **Encoder selection** — always check `ffmpeg_has_encoder("h264_videotoolbox")` at runtime, fall back to `libx264`
- **Parallelism** — image conversion uses `multiprocessing.Pool`; everything else is single-threaded
- **Print style** — plain `print()` for user output; `progress_print(args.progress, msg)` for phase progress (only shown with `--progress`)

---

## FFmpeg Filter Graph Structure

Each photo gets a filter chain built by `build_filter_for_still()`:
```
scale → pad (blurred BG fill) → overlay (foreground) → grade → vignette → grain → [optional: kenburns/parallax zoompan]
```

Transitions are chained with `build_xfade_chain()` using ffmpeg's `xfade` filter. The chain uses offsets calculated from per-photo durations minus the crossfade overlap.

Location label overlays are pre-rendered to transparent PNGs and composited via ffmpeg `overlay` filter.

---

## YouTube Upload

Credentials live in `client_secrets.json` (Google OAuth Desktop app). Token cached to `.youtube_token.json`. On first run, opens a browser for consent.

- Upload uses chunked resumable protocol with retry
- If render succeeds but upload fails, use `--youtube-upload-file` to retry upload only
- Both aspect ratios are uploaded if both are rendered

---

## macOS Photos Import

`--add-to-photos` uses AppleScript via `osascript`. macOS may prompt for permission on first use.

---

## What NOT to Do

- **Don't use `shell=True`** in subprocess calls — all ffmpeg commands are built as lists
- **Don't modify `images` without also modifying `infos`** — they must stay index-aligned
- **Don't add new CLI flags without updating README.md** — the README table is the canonical option reference
- **Don't use absolute paths** in output filenames — outputs always land in `outdir` (default `./Renders`)
- **Don't skip the `finally` cleanup** — temp work PNGs can be several hundred MB
- **Don't break deterministic output naming** — filename format is the primary way users track render versions

---

## Dependencies

| Dependency | Required | Purpose |
|---|---|---|
| ffmpeg | Yes | All video rendering |
| pillow | Yes | Image processing, EXIF |
| numpy | Yes | Motion calculations |
| google-api-python-client | No | YouTube upload |
| google-auth-oauthlib | No | YouTube OAuth |
| mediapipe | No | Smart focus subject detection |

Install: `pip install pillow numpy` (core); see README for optional deps.

---

## Files to Know

| File | Purpose |
|---|---|
| `videophotoslide.py` | Entire implementation |
| `tests/test_videophotoslide.py` | Unit test suite |
| `README.md` | User-facing docs and option table |
| `client_secrets.json` | Google OAuth credentials (not committed) |
| `.youtube_token.json` | Cached OAuth token (not committed) |
| `Photos/` | Organized source photo collections by project |
| `Renders/` | All rendered video outputs |
