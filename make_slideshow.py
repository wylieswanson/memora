import os
import re
import sys
import math
import shutil
import random
import argparse
import subprocess
import time
from pathlib import Path
from PIL import Image, ImageOps

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif"}

def run(cmd):
    print("\n$", " ".join(cmd))
    subprocess.check_call(cmd)

def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def convert_to_pngs(input_dir: Path, work_dir: Path):
    """
    Converts all images to normalized PNGs with EXIF orientation applied.
    Returns list of PNG paths.
    """
    ensure_dir(work_dir)
    images = []
    for p in sorted(input_dir.iterdir(), key=lambda x: natural_key(x.name)):
        if p.suffix.lower() not in IMG_EXTS:
            continue
        try:
            im = Image.open(p)
            im = ImageOps.exif_transpose(im)  # apply EXIF orientation
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            out = work_dir / f"{p.stem}.png"
            im.save(out, format="PNG", optimize=True)
            images.append(out)
        except Exception as e:
            print(f"Skipping {p.name}: {e}", file=sys.stderr)
    return images

def build_filter_for_still(i, W, H, fps, seconds_per_photo, mode="blur",
                           zoom_strength=0.06, blur_strength=22):
    """
    Builds a per-image filter graph and outputs [v{i}].
    Key improvement:
      - Foreground is scaled-to-fit AND padded to WxH BEFORE zoompan.
        This prevents aspect ratio distortions on some images.
    """
    frames = int(round(seconds_per_photo * fps))
    frames = max(frames, 1)

    # Clamp zoom to be subtle and stable
    z_end = 1.0 + float(zoom_strength)

    # Drift in pixels across the whole clip (very small)
    drift = max(0, int(min(W, H) * 0.008))  # ~0.8% of short edge
    # Since we pad to WxH, iw==W and ih==H for zoompan input, so drift math is stable.
    x_expr = f"(iw - iw/zoom)/2 + {drift}*on/{frames}"
    y_expr = f"(ih - ih/zoom)/2 - {drift}*on/{frames}"

    # Common: make a "fit+pad" canvas that is ALWAYS WxH and never stretches
    fit_pad = (
        f"[{i}:v]"
        f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1,format=rgba"
        f"[fit{i}]"
    )

    # Foreground: zoompan on the padded canvas (safe, consistent)
    fg = (
        f"[fit{i}]"
        f"zoompan=z='min({z_end},1+{zoom_strength}*on/{frames})'"
        f":x='{x_expr}':y='{y_expr}'"
        f":d={frames}:s={W}x{H}:fps={fps},"
        f"format=rgba"
        f"[fg{i}]"
    )

    if mode == "cover":
        # Crop-fill without blurred background
        # (Still uses fit_pad + zoom, but we create a cover layer instead)
        cover = (
            f"[{i}:v]"
            f"scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},setsar=1,format=rgba"
            f"[cover{i}]"
        )
        # Optionally zoom the cover too (gives motion without any letterboxing)
        cover_fg = (
            f"[cover{i}]"
            f"zoompan=z='min({z_end},1+{zoom_strength}*on/{frames})'"
            f":x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2'"
            f":d={frames}:s={W}x{H}:fps={fps},"
            f"format=yuv420p"
            f"[v{i}]"
        )
        return ";\n".join([cover, cover_fg])

    if mode == "contain":
        # No blur, no crop. Just fit+pad, then zoompan (subtle).
        out = f"[fg{i}]format=yuv420p[v{i}]"
        return ";\n".join([fit_pad, fg, out])

    # Default: "blur" background + foreground fit
    # Background: scale-to-cover then blur/dim
    bg = (
        f"[{i}:v]"
        f"scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},setsar=1,"
        f"boxblur={blur_strength}:1,"
        f"eq=brightness=-0.08:saturation=0.9,"
        f"format=rgba"
        f"[bg{i}]"
    )

    # Composite fg over bg
    comp = (
        f"[bg{i}][fg{i}]"
        f"overlay=(W-w)/2:(H-h)/2:format=auto,"
        f"format=yuv420p"
        f"[v{i}]"
    )

    return ";\n".join([bg, fit_pad, fg, comp])

def build_xfade_chain(n, seconds_per_photo, xfade_seconds, transition="fade"):
    """
    Chains [v0][v1]...[v{n-1}] using xfade.
    Ensures the last image does not linger unnecessarily.
    """
    if n == 1:
        return "[v0]format=yuv420p[vout]"

    parts = []
    cur = "[v0]"

    for i in range(1, n):
        offset = i * (seconds_per_photo - xfade_seconds)
        nxt = f"[v{i}]"
        out = f"[x{i}]"
        parts.append(
            f"{cur}{nxt}xfade=transition={transition}:duration={xfade_seconds}:offset={offset}{out}"
        )
        cur = out

    # Ensure the last image fades out properly and matches the total duration
    final_offset = (n - 1) * (seconds_per_photo - xfade_seconds) + xfade_seconds
    parts.append(f"{cur}trim=duration={final_offset},format=yuv420p[vout]")
    return ";\n".join(parts)


def render(images, out_path: Path, W, H,
           fps=30, seconds_per_photo=2.8, xfade_seconds=0.7,
           mode="blur", transition="fade",
           zoom_strength=0.04, blur_strength=22):
    """
    Render slideshow using FFmpeg filter_complex.
    """
    if not images:
        raise SystemExit("No images found.")

    cmd = ["ffmpeg", "-y"]

    # Each image becomes a looped input
    for img in images:
        cmd += ["-loop", "1", "-t", str(seconds_per_photo), "-i", str(img)]

    filter_blocks = []
    for i in range(len(images)):
        filter_blocks.append(
            build_filter_for_still(
                i, W, H, fps, seconds_per_photo,
                mode=mode,
                zoom_strength=zoom_strength,
                blur_strength=blur_strength
            )
        )

    xfade_chain = build_xfade_chain(
        len(images),
        seconds_per_photo=seconds_per_photo,
        xfade_seconds=xfade_seconds,
        transition=transition
    )

    filter_complex = ";\n".join(filter_blocks) + ";\n" + xfade_chain

    # Updated to use hardware-accelerated encoding with h264_videotoolbox
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-r", str(fps),

        # Modern encoding defaults
        "-c:v", "h264_videotoolbox",
        "-pix_fmt", "yuv420p",
        "-crf", "20",
        "-preset", "slow",
        "-movflags", "+faststart",

        str(out_path)
    ]

    run(cmd)

def process_with_rife(video_path, model_dir, fps=60, scale=0.5, exp=1, montage=False, fp16=False, UHD=False, png=False, ext="mp4"):
    """
    Process the given video with ECCV2022-RIFE to double the frame rate.
    """
    import math
    from pathlib import Path

    video = Path(video_path)
    if not video.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    # Calculate the original frame count and duration
    original_fps = 30  # Assuming the input video is 30 FPS
    duration = video.stat().st_size / (original_fps * scale)  # Approximation based on size
    original_frame_count = math.ceil(duration * original_fps)

    # Calculate the target frame count based on the desired FPS
    target_frame_count = math.ceil(duration * fps)

    print(f"Processing {video_path} with RIFE:")
    print(f"  Original FPS: {original_fps}")
    print(f"  Target FPS: {fps}")
    print(f"  Original Frame Count: {original_frame_count}")
    print(f"  Target Frame Count: {target_frame_count}")

    venv_python = os.path.join(os.environ.get('VIRTUAL_ENV', '/Volumes/src/video-workflow/.venv'), 'bin', 'python')
    # Replace 'python' with venv_python in the RIFE command
    rife_command = list(map(str, [
        venv_python,
        'ECCV2022-RIFE/inference_video.py',
        '--exp', str(exp),
        '--fps', str(fps),
        '--scale', str(scale),
        '--video', video,
        '--model', model_dir,
        '--ext', ext
    ]))

    if montage:
        rife_command.append("--montage")
    if fp16:
        rife_command.append("--fp16")
    if UHD:
        rife_command.append("--UHD")
    if png:
        rife_command.append("--png")

    # Run the RIFE command
    print("Running RIFE command:", " ".join(rife_command))
    subprocess.run(rife_command, check=True)

def get_file_metadata(file_path):
    """Retrieve file size and other metadata."""
    file = Path(file_path)
    if file.exists():
        size = file.stat().st_size / (1024 * 1024)  # Convert to MB
        return f"{file.name}: {size:.2f} MB"
    return f"{file.name}: File not found"

def main():
    start_time = time.time()

    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="./input_photos", help="Directory of photos")
    ap.add_argument("--outdir", default="./renders", help="Output directory")
    ap.add_argument("--workdir", default="./.work_pngs", help="Working directory (PNGs)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--sec", type=float, default=2.8, help="Seconds per photo")
    ap.add_argument("--xfade", type=float, default=1.5, help="Crossfade/fade transition duration in seconds")
    ap.add_argument("--transition", default="fade", help="xfade transition name (fade, wipeleft, slideright, circleopen, etc.)")
    ap.add_argument("--mode", default="blur", choices=["blur", "cover", "contain"],
                    help="blur = blurred bg + fit fg; cover = crop-fill; contain = fit+pad (no blur)")
    ap.add_argument("--zoom", type=float, default=0.06, help="Ken Burns zoom strength (0.0–0.12 recommended)")
    ap.add_argument("--blur", type=int, default=22, help="Blur strength for mode=blur")
    ap.add_argument("--shuffle", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--without-rife", action="store_true", help="Disable processing videos with ECCV2022-RIFE.")
    ap.add_argument("--rife-exp", type=int, default=1, help="RIFE expansion ratio.")
    ap.add_argument("--rife-fps", type=int, default=60, help="Output FPS for RIFE.")
    ap.add_argument("--rife-scale", type=float, default=0.5, help="Scale factor for RIFE.")
    ap.add_argument("--rife-montage", action="store_true", help="Enable montage mode for RIFE.")
    ap.add_argument("--rife-fp16", action="store_true", help="Enable FP16 mode for RIFE.")
    ap.add_argument("--rife-UHD", action="store_true", help="Enable UHD support for RIFE.")
    ap.add_argument("--rife-png", action="store_true", help="Output PNG frames for RIFE.")
    ap.add_argument("--rife-ext", type=str, default="mp4", help="Output file extension for RIFE.")
    args = ap.parse_args()

    # Display arguments at the start
    print("\nScript Arguments:")
    for arg, value in vars(args).items():
        print(f"  {arg}: {value}")

    input_dir = Path(args.input)
    out_dir = Path(args.outdir)
    work_dir = Path(args.workdir)

    ensure_dir(out_dir)

    if not input_dir.exists():
        raise SystemExit(f"Create {input_dir} and drop your photos in it.")

    # Clean working dir each run
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    images = convert_to_pngs(input_dir, work_dir)
    if not images:
        raise SystemExit(f"No supported images found in {input_dir}")

    if args.shuffle:
        rnd = random.Random(args.seed)
        rnd.shuffle(images)

    # 16:9
    render(
        images,
        out_dir / "slideshow_16x9.mp4",
        W=1920, H=1080,
        fps=args.fps,
        seconds_per_photo=args.sec,
        xfade_seconds=args.xfade,
        mode=args.mode,
        transition=args.transition,
        zoom_strength=args.zoom,
        blur_strength=args.blur
    )

    # 9:16
    render(
        images,
        out_dir / "slideshow_9x16.mp4",
        W=1080, H=1920,
        fps=args.fps,
        seconds_per_photo=args.sec,
        xfade_seconds=args.xfade,
        mode=args.mode,
        transition=args.transition,
        zoom_strength=args.zoom,
        blur_strength=args.blur
    )

    # Process with ECCV2022-RIFE unless --without-rife is specified
    if not args.without_rife:
        process_with_rife(
            str(out_dir / "slideshow_16x9.mp4"),
            "ECCV2022-RIFE/train_log",
            fps=args.rife_fps,
            scale=args.rife_scale,
            exp=args.rife_exp,
            montage=args.rife_montage,
            fp16=args.rife_fp16,
            UHD=args.rife_UHD,
            png=args.rife_png,
            ext=args.rife_ext
        )
        process_with_rife(
            str(out_dir / "slideshow_9x16.mp4"),
            "ECCV2022-RIFE/train_log",
            fps=args.rife_fps,
            scale=args.rife_scale,
            exp=args.rife_exp,
            montage=args.rife_montage,
            fp16=args.rife_fp16,
            UHD=args.rife_UHD,
            png=args.rife_png,
            ext=args.rife_ext
        )

    # Calculate elapsed time
    elapsed_time = time.time() - start_time

    # Display metadata for output files
    print("\nRender Complete:")
    print(f"Elapsed Time: {elapsed_time:.2f} seconds")
    print("Output Files:")
    for file_name in ["slideshow_16x9.mp4", "slideshow_9x16.mp4"]:
        print(get_file_metadata(out_dir / file_name))

    print("\nDone:")
    print(" - renders/slideshow_16x9.mp4")
    print(" - renders/slideshow_9x16.mp4")

if __name__ == "__main__":
    main()
