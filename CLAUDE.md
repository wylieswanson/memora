# CLAUDE.md — video-workflow

## Project Overview

**Memora: video-workflow** — a CLI tool that turns folders of photos and video clips into polished slideshow videos in 16x9 and 9x16 formats. Single script: `videophotoslide.py`. No web server, no database, no framework.

Key output characteristics:
- Deterministic, descriptive filenames: `<timestamp>_<folder>_fmt<format>_q<quality>_transition-<transition>_n<count>.mp4`
- Both aspect ratios rendered in one pass by default
- All visual effects (filmic grade, vignette, grain, Ken Burns, parallax) composed in FFmpeg filter graphs
- Hardware-accelerated encoding via `h264_videotoolbox` with `libx264` fallback

---

## Architecture

**Single file**: `videophotoslide.py` (~1,900 lines). No separate modules.

**Processing pipeline** (in execution order):
0. If `--youtube-upload` is set, validate/refresh YouTube OAuth credentials before rendering so auth failures happen early
1. Scan input dir for supported files → `IMG_EXTS` + `VID_EXTS`
2. Images: convert to normalized PNG in parallel → `convert_to_pngs()` / `_convert_single_image()`
   Video clips: probe via ffprobe → `probe_video_clip()`
   Merge in natural sort order → `collect_media()`
3. Extract photo metadata (EXIF, GPS, camera) → `get_image_metadata()`; clips get creation time from container
4. Optional subject detection on photos → `detect_subject_focus()` (MediaPipe)
5. Sort → `sort_images_and_infos()`
6. Optional geocoding → `geocode_photos()`
7. Build per-item FFmpeg filters → `build_filter_for_still()` or `build_filter_for_clip()`
8. Chain transitions → `build_xfade_chain()`
9. Execute ffmpeg → `run_ffmpeg_with_progress()`
10. Post-render: YouTube upload and/or macOS Photos import

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
| `_media_start_times(durations, xfade)` | Timeline start offsets per item (used for `adelay` in audio filters) |
| `build_filter_for_clip(i, ...)` | ffmpeg filter chain for a clip: bg+fg composite, optional grade/vignette/grain |
| `_build_clip_audio_filters(...)` | Builds audio `filter_complex` fragment for `keep`/`duck` → `[aout]` |

---

## FFmpeg Filter Graph Structure

**Photos** — `build_filter_for_still()`:
- Input via `-loop 1 -t <dur>` (still image looped for its duration)
- Filter chain: `scale (bg fill) → boxblur → overlay (fg) → grade → vignette → grain → [optional kenburns/parallax zoompan]`

**Clips** — `build_filter_for_clip()`:
- Input via plain `-i` (no loop)
- Filter chain: `scale (bg fill) → boxblur → overlay (fg) → [optional grade/vignette/grain per --clip-grade]`
- No motion applied

**Transitions** — `build_xfade_chain()`:
- ffmpeg `xfade` filter chained between all items
- Offset per transition = cumulative duration minus crossfade overlap
- `auto` mode rotates through `PRO_TRANSITIONS`

**Location overlays** — pre-rendered to transparent PNGs, composited via `overlay=0:0` after the per-item filter.

---

## Running the Script

```bash
source .venv/bin/activate
python videophotoslide.py <source_dir> [OPTIONS]
```

Common invocations:
```bash
# Default: both formats, standard quality, auto transition
python videophotoslide.py ./input_photos

# Dry run — prints ffmpeg command without rendering
python videophotoslide.py ./input_photos --dry-run --progress

# Video clips only, muted, full grade treatment
python videophotoslide.py "Photos/Videotest" --format 9x16

# Video clips with duck audio (background lowers during clips)
python videophotoslide.py "Photos/Videotest" --format 9x16 --clip-audio duck --audio ./music.mp3

# Vertical only, with Ken Burns and smart subject framing on photos
python videophotoslide.py ./input_photos --format 9x16 --motion-style kenburns --smart-focus

# Render and upload to YouTube
python videophotoslide.py ./input_photos --format 16x9 --youtube-upload --youtube-privacy private

# Upload existing render without re-rendering
python videophotoslide.py --youtube-upload-file ./Renders/some.mp4 --youtube-title "{filename}"

# Split long render into parts
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
- **Temp work directory** — normalized PNGs go inside a `tempfile.mkdtemp()` subtree; always cleaned up in `finally`. Video clips are never copied — they're referenced at their original path.
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

| Dependency | Required | Purpose |
|---|---|---|
| ffmpeg + ffprobe | Yes | All video rendering and clip probing |
| pillow | Yes | Image processing, EXIF extraction |
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
| `Photos/` | Organized source media collections by project |
| `Renders/` | All rendered video outputs |
