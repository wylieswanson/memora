# Memora: video-workflow (Photos to Professional Slideshows)

A practical workflow for turning photo folders into modern slideshow videos in 16x9 and 9x16.

The current version is slideshow-only and focuses on high-ROI visual polish:
- subtle filmic grade
- gentle vignette
- fine temporal grain
- curated transition rotation
- slight rhythm modulation for more editorial pacing (single arc: slow start, faster middle, slow end)

---

## What this produces

From a directory of photos and/or video clips, the script renders one or both aspect ratios at the selected resolution:

| Resolution | 16x9 output | 9x16 output | Notes |
|---|---:|---:|---|
| `1080p` | 1920x1080 | 1080x1920 | Default; fast and broadly compatible |
| `1440p` | 2560x1440 | 1440x2560 | Sharper YouTube upload without huge files |
| `4k` / `2160p` | 3840x2160 | 2160x3840 | Recommended practical maximum for YouTube |
| `8k` / `4320p` | 7680x4320 | 4320x7680 | Experimental; slow renders and very large files |

Output naming is deterministic and includes run identity fields:
- Renders/<timestamp>_<input-folder>_fmt16x9_res<resolution>_q<quality>_transition-<transition>_n<photos>.mp4
- Renders/<timestamp>_<input-folder>_fmt9x16_res<resolution>_q<quality>_transition-<transition>_n<photos>.mp4

When video clips are present the count suffix includes both: `_n10c2` means 10 photos and 2 clips.

---

## Repository layout

```text
video-workflow/
  input_photos/          # source photos
  Renders/               # output videos
  .work_pngs/            # parent for session temp dirs (--workdir); each render creates and removes a subdir
  videophotoslide.py     # slideshow generator
```

---

## Requirements

- macOS (Apple Silicon recommended)
- ffmpeg on PATH
- Python 3.11+

Install ffmpeg with Homebrew:

```bash
brew install ffmpeg
ffmpeg -version
```

Python dependency:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -r requirements.txt
```

Optional YouTube upload dependencies:

```bash
pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
```

Optional smart-focus dependency for subject-aware Ken Burns framing:

```bash
pip install mediapipe
```

`--smart-focus` uses MediaPipe Tasks. On first use, the script downloads the default Face Detector and Pose Landmarker model assets into `./.mediapipe_models`. Use the model override flags below if you want to pin or supply those assets manually.

---

## Usage

Place photos into a folder such as input_photos. Supported extensions:
- .jpg .jpeg .png .webp .tif .tiff .heic .heif

Run:

```bash
python videophotoslide.py ./input_photos
```

---

## Key options

| Flag | Default | Description |
|---|---|---|
| --outdir | ./Renders | Output directory |
| --workdir | ./.work_pngs | Parent directory for the session temp folder; only the session subdir is deleted after render |
| --format | both | 16x9, 9x16, or both |
| --resolution | 1080p | 1080p, 1440p, 4k/2160p, or 8k/4320p |
| --quality | standard | draft, standard, high, youtube, max |
| --fps | quality preset | Override frames per second |
| --bitrate | quality preset | Override output bitrate, e.g. 45M |
| --sec | 2.8 | Base seconds per photo |
| --xfade | 0.7 | Crossfade duration |
| --transition | auto | Transition mode or explicit ffmpeg xfade transition |
| --rhythm-strength | 0.12 | Pacing variation strength (0.0 to 0.25) |
| --motion-style | none | none, kenburns, parallax, both |
| --ken-burns-strength | auto | Override Ken Burns strength (0.0 to 0.03) |
| --parallax-px | auto | Override parallax amplitude in pixels |
| --smart-focus | off | Use MediaPipe Tasks face detection with pose fallback to bias Ken Burns framing |
| --smart-focus-model-dir | ./.mediapipe_models | Cache directory for MediaPipe Tasks model assets |
| --smart-focus-face-model | auto | Path to a MediaPipe Face Detector model asset |
| --smart-focus-pose-model | auto | Path to a MediaPipe Pose Landmarker model asset |
| --sort-by | natural | natural, time, location, random |
| --seed | 0 | Seed for sort and pacing variation |
| --clip-grade | full | Visual treatment for video clips: none, grade (color only), full (grade+vignette+grain) |
| --clip-audio | mute | Clip audio handling: mute (silence), keep (mix with background), duck (lower background during clips) |
| --audio | none | Path to background audio file to mix into the slideshow |
| --audio-offset | 0.0 | Skip N seconds into the background audio before mixing (e.g. skip to the drop) |
| --audio-fade | off | Fade out the background audio over N seconds at the end |
| --clip-max-sec | off | Trim video clips to at most N seconds |
| --youtube-upload | off | Upload each rendered output to YouTube after rendering |
| --youtube-upload-file | off | Upload an existing rendered `.mp4` to YouTube without re-rendering |
| --add-to-photos | off | Import rendered `.mp4` files into the macOS Photos app |
| --youtube-client-secrets | ./client_secrets.json | OAuth client JSON from Google Cloud |
| --youtube-token-file | ./.youtube_token.json | Cached OAuth token file |
| --youtube-title | auto | Optional template with {stem}, {filename}, {format}, {input_dir} |
| --youtube-description | empty | YouTube description text |
| --youtube-tags | empty | Comma-separated YouTube tags |
| --youtube-category | 22 | YouTube category ID |
| --youtube-privacy | private | private, public, unlisted |

## Output targets and quality

`--format` controls aspect ratio. `--resolution` controls pixel dimensions. `--quality` controls fps, background blur strength, and bitrate.

For routine YouTube uploads, use:

```bash
python videophotoslide.py ./input_photos --format both --resolution 4k --quality youtube
```

Use `8k --quality max` only when you really want the largest supported render. It is useful as an archive or stress-test target, but it is usually overkill for normal slideshow uploads.

Quality presets:

| Quality | FPS | Bitrate behavior | Best use |
|---|---:|---|---|
| `draft` | 24 | `8M` | Fast preview; bicubic scaling |
| `standard` | 30 | `15M` | Default balanced render; bicubic scaling |
| `high` | 30 | `25M` | Better local/archive render; Lanczos scaling |
| `youtube` | 30 | Resolution-aware YouTube SDR bitrate; high-frame-rate value when `--fps > 30` | Recommended YouTube upload; Lanczos scaling |
| `max` | 30 | Highest SDR bitrate in the table below | Largest built-in upload preset; Lanczos scaling |

The `youtube` and `max` presets use the SDR H.264 bitrate guidance from YouTube's recommended upload settings. The script keeps the output as MP4/H.264 with `+faststart`, AAC audio when audio is present, progressive frames, and 4:2:0 chroma via `yuv420p`.

| Resolution | `youtube` at 24/25/30 fps | `youtube` at 48/50/60 fps | `max` |
|---|---:|---:|---:|
| `1080p` | `8M` | `12M` | `12M` |
| `1440p` | `16M` | `24M` | `24M` |
| `4k` | `45M` | `68M` | `68M` |
| `8k` | `160M` | `240M` | `240M` |

YouTube currently lists maximum upload size as 256 GB or 12 hours, whichever is less, for verified accounts. Check the official YouTube Help pages before very large batch renders because upload limits and encoding recommendations can change:
- [Recommended upload encoding settings](https://support.google.com/youtube/answer/1722171)
- [Upload videos longer than 15 minutes](https://support.google.com/youtube/answer/71673)

Examples:

```bash
# Default two-format render
python videophotoslide.py ./input_photos

# Longer photos and softer transitions
python videophotoslide.py ./input_photos --sec 3.5 --xfade 0.9

# Stronger editorial pacing and auto transition rotation
python videophotoslide.py ./input_photos --transition auto --rhythm-strength 0.18

# Vertical-only output
python videophotoslide.py ./input_photos --format 9x16

# Practical maximum-quality YouTube render
python videophotoslide.py ./input_photos --format both --resolution 4k --quality youtube

# Absolute max preset; expect long renders and very large files
python videophotoslide.py ./input_photos --format 16x9 --resolution 8k --quality max

# Ken Burns with subject-aware framing
python videophotoslide.py ./input_photos --motion-style kenburns --smart-focus

# Render and upload to YouTube as private
python videophotoslide.py ./input_photos \
  --format 16x9 \
  --youtube-upload \
  --youtube-title "{input_dir} | {format}" \
  --youtube-description "Fresh slideshow render" \
  --youtube-tags "travel,arizona,slideshow"

# Upload an existing render without re-rendering
python videophotoslide.py \
  --youtube-upload-file ./Renders/20260322-194059_lorena-climbing-prescott_fmt16x9_res1080p_qstandard_transition-auto_n12.mp4 \
  --youtube-title "{filename}" \
  --youtube-description "Fresh slideshow render" \
  --youtube-tags "travel,arizona,slideshow"

# Render and import finished videos into macOS Photos
python videophotoslide.py ./input_photos --add-to-photos
```

---

## YouTube Upload Setup

To publish directly after rendering:

1. Create a Google Cloud project.
2. Enable `YouTube Data API v3`.
3. Create an OAuth client for a Desktop app.
4. Download the OAuth client JSON to `client_secrets.json` in the repo root, or point `--youtube-client-secrets` at it.
5. Run the script with `--youtube-upload`.

On first upload, the script opens a browser for Google consent and stores a reusable token in `.youtube_token.json`.

If `--youtube-upload` is enabled during a render, the script validates or refreshes the OAuth token before rendering starts so expired or revoked credentials fail fast instead of after a long render.

If a render succeeds but upload fails, rerun with `--youtube-upload-file` pointed at the existing `.mp4` to retry the upload without rendering again.

`--add-to-photos` is macOS-only and uses AppleScript via `osascript` to import each finished video into the Photos app. On first use, macOS may ask you to allow Terminal access to control Photos.

Notes:
- The uploader uses the YouTube Data API `videos.insert` flow.
- Uploads default to `private`.
- If you render both aspect ratios, the script uploads both outputs.

---

## Visual style notes

The compositor keeps full-photo visibility by layering:
- blurred, graded background fill
- fit-into-frame foreground
- optional subtle motion

Then each shot receives:
- mild filmic grade
- low-intensity vignette
- fine temporal grain

Transitions:
- auto mode rotates through a restrained set for a modern professional flow.

Ken Burns motion:
- Sub-pixel overlay positioning gives smooth continuous motion with no stairstepping.
- The zoom uses smooth ease-in/out (quintic Hermite curve) over each shot.
- `--ken-burns-strength` controls the total zoom range per shot (0.0–0.03); default is auto-scaled to shot duration.

Smart focus:
- `--smart-focus` is a clean v1 subject-targeting mode for Ken Burns.
- It uses MediaPipe Tasks face detection first, pose landmark fallback second, and otherwise falls back to center framing.
- It currently activates when `--motion-style` is `kenburns` or `both`.
- Default model assets are cached in `./.mediapipe_models`; use `--smart-focus-face-model` and `--smart-focus-pose-model` for manually downloaded or alternate models.

Sort modes:
- `natural` (default): filename order.
- `time`: EXIF datetime, with undated items appended at the end.
- `random`: seeded shuffle (use `--seed` for reproducibility).
- `location`: greedy nearest-neighbor sort by GPS proximity (haversine). Keeps photos from the same area together. Photos without GPS are appended at the end.

---

## Troubleshooting

No supported images found:
- verify you passed a photo folder, not a video-only folder
- verify extensions are one of the supported image types

ffmpeg not found:
- install with Homebrew and confirm ffmpeg -version works in your shell

---

## Notes

- The script always cleans temporary work files after completion.
- Output filenames are intentionally descriptive for easier version tracking.
