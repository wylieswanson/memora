import argparse
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

import videophotoslide as vps


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_args(**overrides):
    """Return a SimpleNamespace with all args that main()/_validate_args() reads,
    with safe defaults. Override specific fields for each test."""
    defaults = dict(
        source_dir="input_photos",
        outdir="./Renders",
        workdir="./.work_pngs",
        quality="standard",
        format="16x9",
        sort_by="natural",
        max_workers=0,
        camera_stats=False,
        geocode=False,
        location_stats=False,
        location_overlay=False,
        motion_style="none",
        ken_burns_strength=None,
        parallax_px=None,
        smart_focus=False,
        progress=False,
        sec=2.8,
        xfade=0.7,
        transition="fade",
        seed=0,
        rhythm_strength=0.12,
        audio=None,
        audio_offset=0.0,
        audio_fade=None,
        split_secs=None,
        fps=None,
        bitrate=None,
        dry_run=False,
        clip_grade="full",
        clip_audio="mute",
        clip_max_sec=None,
        youtube_upload=False,
        youtube_upload_file=None,
        add_to_photos=False,
        youtube_client_secrets="./client_secrets.json",
        youtube_token_file="./.youtube_token.json",
        youtube_title=None,
        youtube_description="",
        youtube_tags="",
        youtube_category="22",
        youtube_privacy="private",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class VideoPhotoSlideTests(unittest.TestCase):
    def _info(self, name: str, dt=None, gps=None):
        return vps.PhotoInfo(
            path=Path(name),
            width=1200,
            height=800,
            aspect_ratio=1.5,
            is_landscape=True,
            orientation="",
            datetime_taken=dt,
            gps_coords=gps,
        )

    # -----------------------------------------------------------------------
    # PhotoInfo
    # -----------------------------------------------------------------------

    def test_square_orientation_normalizes_is_landscape(self):
        info = vps.PhotoInfo(
            path=Path("sq.jpg"),
            width=1000,
            height=1000,
            aspect_ratio=1.0,
            is_landscape=True,
            orientation="",
        )
        self.assertEqual(info.orientation, "square")
        self.assertFalse(info.is_landscape)

    # -----------------------------------------------------------------------
    # Sorting
    # -----------------------------------------------------------------------

    def test_time_sort_keeps_all_images_and_orders_dated_first(self):
        images = [Path("a.png"), Path("b.png"), Path("c.png"), Path("d.png")]
        infos = [
            self._info("a.png", dt=datetime(2024, 1, 3, 10, 0, 0)),
            None,
            self._info("c.png", dt=datetime(2024, 1, 1, 10, 0, 0)),
            self._info("d.png", dt=None),
        ]

        out_images, _ = vps.sort_images_and_infos(images, infos, sort_by="time")

        self.assertEqual(len(out_images), len(images))
        self.assertEqual(out_images, [Path("c.png"), Path("a.png"), Path("b.png"), Path("d.png")])

    def test_location_sort_uses_nearest_neighbor_proximity(self):
        # a=(37,-122), c=(35,-120): ~277 km apart
        # Nearest-neighbor starts at a (first GPS item in natural order),
        # then picks c (only other GPS item), then appends no-GPS items.
        images = [Path("a.png"), Path("b.png"), Path("c.png"), Path("d.png")]
        infos = [
            self._info("a.png", gps=(37.0, -122.0)),
            None,
            self._info("c.png", gps=(35.0, -120.0)),
            self._info("d.png", gps=None),
        ]

        out_images, _ = vps.sort_images_and_infos(images, infos, sort_by="location")

        self.assertEqual(len(out_images), len(images))
        self.assertEqual(out_images, [Path("a.png"), Path("c.png"), Path("b.png"), Path("d.png")])

    def test_location_sort_clusters_nearby_photos(self):
        # Two clusters: SF area (37,-122 and 37.5,-122.5) and LA area (34,-118 and 33.9,-118.1)
        # Starting from sf1, nearest-neighbor should visit sf2 before jumping to LA.
        images = [Path("sf1.png"), Path("la1.png"), Path("sf2.png"), Path("la2.png")]
        infos: list[vps.PhotoInfo | None] = [
            self._info("sf1.png", gps=(37.0, -122.0)),
            self._info("la1.png", gps=(34.0, -118.0)),
            self._info("sf2.png", gps=(37.5, -122.5)),
            self._info("la2.png", gps=(33.9, -118.1)),
        ]

        out_images, _ = vps.sort_images_and_infos(images, infos, sort_by="location")

        self.assertEqual(out_images, [Path("sf1.png"), Path("sf2.png"), Path("la1.png"), Path("la2.png")])

    def test_random_sort_does_not_mutate_input_list(self):
        images = [Path("1.png"), Path("2.png"), Path("3.png"), Path("4.png")]
        before = images.copy()

        vps.sort_images_and_infos(images, [None] * len(images), sort_by="random", seed=42)

        self.assertEqual(images, before)

    def test_sort_images_and_infos_keeps_metadata_aligned(self):
        images = [Path("a.png"), Path("b.png"), Path("c.png")]
        infos = [
            self._info("a.png", dt=datetime(2024, 1, 3, 10, 0, 0)),
            self._info("b.png", dt=datetime(2024, 1, 1, 10, 0, 0)),
            self._info("c.png", dt=datetime(2024, 1, 2, 10, 0, 0)),
        ]
        infos[0].focal_point = (0.1, 0.2)
        infos[1].focal_point = (0.3, 0.4)
        infos[2].focal_point = (0.5, 0.6)

        sorted_images, sorted_infos = vps.sort_images_and_infos(images, infos, sort_by="time")  # type: ignore[arg-type]

        self.assertEqual(sorted_images, [Path("b.png"), Path("c.png"), Path("a.png")])
        self.assertEqual([info.focal_point for info in sorted_infos if info], [(0.3, 0.4), (0.5, 0.6), (0.1, 0.2)])

    # -----------------------------------------------------------------------
    # Image conversion
    # -----------------------------------------------------------------------

    def test_convert_single_image_uses_collision_safe_filename(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "IMG_0001.jpg"
            work = tmp_path / "work"
            work.mkdir(parents=True, exist_ok=True)

            Image.new("RGB", (10, 10), color="white").save(src)

            _idx, out, _info, err = vps._convert_single_image((src, 7, work, False, False))

            self.assertIsNone(err)
            if out is None:
                self.fail("Conversion unexpectedly returned no output path")
            self.assertEqual(out, work / "000007_IMG_0001.png")
            self.assertTrue(out.exists())

    # -----------------------------------------------------------------------
    # Output naming
    # -----------------------------------------------------------------------

    def test_build_targets_uses_required_filename_fields(self):
        targets = vps.build_targets(
            fmt="both",
            stamp="20260315-214501",
            input_dir_name="Input Photos",
            quality="standard",
            transition="fade",
            photo_count=12,
        )

        names = [name for name, *_ in targets]
        self.assertEqual(
            names,
            [
                "20260315-214501_input-photos_fmt16x9_qstandard_transition-fade_n12.mp4",
                "20260315-214501_input-photos_fmt9x16_qstandard_transition-fade_n12.mp4",
            ],
        )

    # -----------------------------------------------------------------------
    # Transitions
    # -----------------------------------------------------------------------

    def test_validate_transition_rejects_unknown_value(self):
        with self.assertRaises(argparse.ArgumentTypeError) as ctx:
            vps.validate_transition("not-a-real-transition")
        self.assertIn("must be one of:", str(ctx.exception))

    def test_parse_args_rejects_invalid_transition(self):
        with patch("sys.argv", ["videophotoslide.py", "./input_photos", "--transition", "bad-transition"]):
            with self.assertRaises(SystemExit):
                vps.parse_args()

    # -----------------------------------------------------------------------
    # Motion
    # -----------------------------------------------------------------------

    def test_motion_style_both_enables_parallax_by_default(self):
        ken, parallax = vps.resolve_motion_values(
            motion_style="both",
            ken_override=None,
            parallax_override=None,
            frame_min_dim=1080,
            seconds_per_photo=2.8,
        )
        self.assertGreater(ken, 0.0)
        self.assertGreater(parallax, 0)

    # -----------------------------------------------------------------------
    # Camera stats
    # -----------------------------------------------------------------------

    def test_print_camera_stats_reports_known_and_unknown_cameras(self):
        infos = [
            self._info("a.png"),
            self._info("b.png"),
            self._info("c.png"),
            None,
        ]
        infos[0].camera_make = "Apple"
        infos[0].camera_model = "iPhone 15 Pro"
        infos[1].camera_make = "Apple"
        infos[1].camera_model = "iPhone 15 Pro"
        infos[2].camera_make = "Sony"
        infos[2].camera_model = "A7 IV"

        stream = StringIO()
        with redirect_stdout(stream):
            vps.print_camera_stats(infos)

        output = stream.getvalue()
        self.assertIn("Camera stats", output)
        self.assertIn("- Apple iPhone 15 Pro: 2", output)
        self.assertIn("- Sony A7 IV: 1", output)
        self.assertIn("- Unknown camera: 1", output)

    # -----------------------------------------------------------------------
    # YouTube helpers
    # -----------------------------------------------------------------------

    def test_parse_youtube_tags_trims_and_ignores_empty_values(self):
        tags = vps.parse_youtube_tags(" travel,  hiking ,, arizona ")
        self.assertEqual(tags, ["travel", "hiking", "arizona"])

    def test_build_youtube_title_supports_template_fields(self):
        title = vps.build_youtube_title(
            output_path=Path("Renders/my-video.mp4"),
            input_dir=Path("Input Photos"),
            fmt="1920x1080",
            custom_title="{input_dir} | {format} | {stem}",
        )
        self.assertEqual(title, "Input Photos | 1920x1080 | my-video")

    def test_infer_input_dir_for_upload_uses_render_stem_when_source_dir_missing(self):
        inferred = vps.infer_input_dir_for_upload(
            Path("Renders/20260322-194059_lorena-climbing-prescott_fmt16x9_qstandard_transition-auto_n12.mp4"),
            None,
        )
        self.assertEqual(inferred, Path("lorena-climbing-prescott"))

    # -----------------------------------------------------------------------
    # macOS Photos import
    # -----------------------------------------------------------------------

    def test_import_media_to_photos_uses_osascript(self):
        with patch("videophotoslide.subprocess.run") as mock_run:
            vps.import_media_to_photos([Path("Renders/render.mp4")])

        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[1], "-e")
        self.assertIn('tell application "Photos"', cmd[2])
        self.assertEqual(cmd[3], str((Path.cwd() / "Renders/render.mp4").resolve()))

    # -----------------------------------------------------------------------
    # Filter graph
    # -----------------------------------------------------------------------

    def test_build_filter_for_still_kenburns_uses_bg_fg_overlay(self):
        filt = vps.build_filter_for_still(
            0,
            1920,
            1080,
            30,
            2.8,
            ken_strength=0.0015,
            focal_point=(0.25, 0.4),
        )
        # Background: blurred fill present in Ken Burns path
        self.assertIn("force_original_aspect_ratio=increase", filt)
        self.assertIn("boxblur", filt)
        # Foreground: fit (decrease) with per-frame zoom
        self.assertIn("force_original_aspect_ratio=decrease", filt)
        self.assertIn("eval=frame", filt)
        # Overlay with animated focal-point pan
        self.assertIn("overlay=x=", filt)
        self.assertIn("round(1920/2)", filt)
        self.assertIn("round(1080/2)", filt)
        # Ease function drives both zoom and pan
        self.assertIn("3*pow(min(max((t/2.8),0),1),2)-2*pow(min(max((t/2.8),0),1),3)", filt)
        # No hard crop step on FG
        self.assertNotIn(":x='", filt)

    def test_build_filter_for_still_kenburns_without_focus_still_uses_bg_fg(self):
        filt = vps.build_filter_for_still(
            0,
            1920,
            1080,
            30,
            2.8,
            ken_strength=0.0015,
            focal_point=None,
        )
        # Without a focal point the bg+fg structure is still used
        self.assertIn("force_original_aspect_ratio=increase", filt)
        self.assertIn("boxblur", filt)
        self.assertIn("force_original_aspect_ratio=decrease", filt)
        self.assertIn("overlay=x=", filt)
        # Static center overlay (no Ken Burns) must NOT be present
        self.assertNotIn("overlay=(W-w)/2:(H-h)/2", filt)

    def test_build_filter_for_still_without_kenburns_keeps_overlay_path(self):
        filt = vps.build_filter_for_still(
            0,
            1920,
            1080,
            30,
            2.8,
            ken_strength=0.0,
            focal_point=None,
        )

        self.assertIn("force_original_aspect_ratio=decrease", filt)
        self.assertIn("[bg0][fg0]overlay=(W-w)/2:(H-h)/2", filt)

    # -----------------------------------------------------------------------
    # Clip filter
    # -----------------------------------------------------------------------

    def test_build_filter_for_clip_full_grade_includes_all_effects(self):
        filt = vps.build_filter_for_clip(0, 1080, 1920, 30, 8.0, clip_grade="full")
        self.assertIn("vignette", filt)
        self.assertIn("noise", filt)
        self.assertIn("eq=contrast", filt)
        self.assertIn("trim=duration=8.000", filt)

    def test_build_filter_for_clip_none_grade_skips_effects(self):
        filt = vps.build_filter_for_clip(0, 1080, 1920, 30, 8.0, clip_grade="none")
        self.assertNotIn("vignette", filt)
        self.assertNotIn("noise", filt)
        self.assertNotIn("eq=contrast", filt)

    def test_build_filter_for_clip_grade_only_skips_vignette_and_grain(self):
        filt = vps.build_filter_for_clip(0, 1080, 1920, 30, 8.0, clip_grade="grade")
        self.assertIn("eq=contrast", filt)
        self.assertNotIn("vignette", filt)
        self.assertNotIn("noise", filt)

    def test_build_filter_for_clip_does_not_use_loop_or_motion(self):
        # Clips must not use -loop (that's handled in the ffmpeg input args, not the filter)
        # and must not use zoompan (Ken Burns).
        filt = vps.build_filter_for_clip(1, 1920, 1080, 30, 5.0)
        self.assertNotIn("zoompan", filt)
        # Stream index is correct
        self.assertIn("[1:v]", filt)

    # -----------------------------------------------------------------------
    # build_media_durations
    # -----------------------------------------------------------------------

    def test_build_media_durations_uses_natural_duration_for_clips(self):
        clip_info = vps.PhotoInfo(
            path=Path("clip.mp4"), width=1920, height=1080,
            aspect_ratio=1.78, is_landscape=True, orientation="landscape",
            is_video=True, video_duration=12.5,
        )
        photo_info = self._info("a.png")
        infos: list = [photo_info, clip_info, photo_info]
        durations = vps.build_media_durations(infos, base_sec=2.8, xfade=0.7, rhythm_strength=0.0, seed=0)

        self.assertEqual(len(durations), 3)
        self.assertAlmostEqual(durations[1], 12.5, places=3)
        # Photos should be close to base_sec (rhythm_strength=0 → exact)
        self.assertAlmostEqual(durations[0], 2.8, places=3)
        self.assertAlmostEqual(durations[2], 2.8, places=3)

    def test_build_media_durations_clips_clamped_to_min(self):
        # A very short clip shouldn't produce a duration shorter than xfade + 0.1
        clip_info = vps.PhotoInfo(
            path=Path("clip.mp4"), width=1920, height=1080,
            aspect_ratio=1.78, is_landscape=True, orientation="landscape",
            is_video=True, video_duration=0.1,
        )
        durations = vps.build_media_durations([clip_info], base_sec=2.8, xfade=0.7, rhythm_strength=0.0, seed=0)
        self.assertGreaterEqual(durations[0], 0.7 + 0.1)

    # -----------------------------------------------------------------------
    # probe_video_clip
    # -----------------------------------------------------------------------

    def test_probe_video_clip_returns_none_on_probe_failure(self):
        with patch("videophotoslide.subprocess.run") as mock_run:
            mock_run.return_value.stdout = "not valid json"
            result = vps.probe_video_clip(Path("missing.mp4"))
        self.assertIsNone(result)

    def test_probe_video_clip_returns_none_when_no_video_stream(self):
        payload = json.dumps({"streams": [{"codec_type": "audio"}], "format": {}})
        with patch("videophotoslide.subprocess.run") as mock_run:
            mock_run.return_value.stdout = payload
            result = vps.probe_video_clip(Path("audio_only.mp4"))
        self.assertIsNone(result)

    def test_probe_video_clip_returns_photo_info_with_is_video_true(self):
        payload = json.dumps({
            "streams": [{
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
                "duration": "5.5",
            }],
            "format": {
                "duration": "5.5",
                "tags": {"creation_time": "2026-04-05T12:00:00.000000Z"},
            },
        })
        with patch("videophotoslide.subprocess.run") as mock_run:
            mock_run.return_value.stdout = payload
            result = vps.probe_video_clip(Path("clip.mp4"))

        self.assertIsNotNone(result)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.is_video)
        self.assertIsNotNone(result.video_duration)
        self.assertAlmostEqual(result.video_duration, 5.5)  # type: ignore[arg-type]
        self.assertEqual(result.width, 1920)
        self.assertEqual(result.height, 1080)
        self.assertEqual(result.datetime_taken, datetime(2026, 4, 5, 12, 0, 0))

    # -----------------------------------------------------------------------
    # collect_media
    # -----------------------------------------------------------------------

    def test_collect_media_returns_empty_for_empty_dir(self):
        with TemporaryDirectory() as tmp:
            paths, infos = vps.collect_media(Path(tmp), Path(tmp) / "work")
        self.assertEqual(paths, [])
        self.assertEqual(infos, [])

    def test_collect_media_probes_video_and_converts_images(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            work = tmp_path / "work"

            # Create a real image
            img_path = tmp_path / "001_photo.jpg"
            Image.new("RGB", (10, 10), color="red").save(img_path)

            # Fake a video file (probe will fail gracefully — it's not a real video)
            vid_path = tmp_path / "002_clip.mp4"
            vid_path.write_bytes(b"fake")

            fake_info = vps.PhotoInfo(
                path=vid_path, width=1920, height=1080,
                aspect_ratio=1.78, is_landscape=True, orientation="landscape",
                is_video=True, video_duration=5.0,
            )

            with patch("videophotoslide.probe_video_clip", return_value=fake_info):
                paths, infos = vps.collect_media(tmp_path, work)

        # Image comes first (natural sort), clip second
        self.assertEqual(len(paths), 2)
        info0, info1 = infos[0], infos[1]
        self.assertIsNotNone(info0)
        self.assertIsNotNone(info1)
        assert info0 is not None and info1 is not None
        self.assertFalse(info0.is_video)
        self.assertTrue(info1.is_video)
        self.assertEqual(paths[1], vid_path)

    # -----------------------------------------------------------------------
    # _build_clip_audio_filters
    # -----------------------------------------------------------------------

    def test_build_clip_audio_filters_mute_returns_empty(self):
        clip_info = vps.PhotoInfo(
            path=Path("c.mp4"), width=1920, height=1080,
            aspect_ratio=1.78, is_landscape=True, orientation="landscape",
            is_video=True, video_duration=5.0, has_audio=True,
        )
        # mute is handled upstream — _build_clip_audio_filters is only called for keep/duck
        result = vps._build_clip_audio_filters(
            infos_list=[clip_info],
            media_durations=[5.0],
            xfade=0.7,
            audio_input_idx=None,
            clip_audio="keep",
        )
        self.assertIn("[aout]", result)

    def test_build_clip_audio_filters_duck_includes_volume_expression(self):
        clip_info = vps.PhotoInfo(
            path=Path("c.mp4"), width=1920, height=1080,
            aspect_ratio=1.78, is_landscape=True, orientation="landscape",
            is_video=True, video_duration=5.0,
            has_audio=True,
        )
        result = vps._build_clip_audio_filters(
            infos_list=[clip_info],
            media_durations=[5.0],
            xfade=0.7,
            audio_input_idx=1,
            clip_audio="duck",
        )
        self.assertIn("volume=", result)
        self.assertIn("0.2", result)
        self.assertIn("[aout]", result)

    def test_build_clip_audio_filters_skips_silent_clips(self):
        clip_info = vps.PhotoInfo(
            path=Path("silent.mp4"), width=1920, height=1080,
            aspect_ratio=1.78, is_landscape=True, orientation="landscape",
            is_video=True, video_duration=5.0, has_audio=False,
        )
        result = vps._build_clip_audio_filters(
            infos_list=[clip_info],
            media_durations=[5.0],
            xfade=0.7,
            audio_input_idx=1,
            clip_audio="keep",
        )
        self.assertEqual(result, "[1:a]apad[bg_a];\n[bg_a]anull[aout]")

    # -----------------------------------------------------------------------
    # RenderConfig
    # -----------------------------------------------------------------------

    def test_render_config_defaults(self):
        cfg = vps.RenderConfig()
        self.assertEqual(cfg.fps, 30)
        self.assertEqual(cfg.sec, 2.8)
        self.assertEqual(cfg.clip_grade, "full")
        self.assertEqual(cfg.clip_audio, "mute")
        self.assertEqual(cfg.encoder, "h264_videotoolbox")

    # -----------------------------------------------------------------------
    # ffmpeg progress / retry
    # -----------------------------------------------------------------------

    def test_run_ffmpeg_with_progress_includes_tail_on_failure(self):
        proc = MagicMock()
        proc.stdout = iter(["frame=1\n", "Impossible to convert between formats\n"])
        proc.wait.return_value = 1

        with patch("videophotoslide.subprocess.Popen", return_value=proc):
            with self.assertRaises(RuntimeError) as ctx:
                vps.run_ffmpeg_with_progress(["ffmpeg"], total_duration=3.0)

        message = str(ctx.exception)
        self.assertIn("ffmpeg render failed with exit code 1", message)
        self.assertIn("Impossible to convert between formats", message)

    def test_render_retries_with_libx264_when_videotoolbox_open_fails(self):
        images = [Path("work/000000_a.png")]
        cfg = vps.RenderConfig(encoder="h264_videotoolbox")

        with patch("videophotoslide.run_ffmpeg_with_progress") as mock_run:
            mock_run.side_effect = [
                RuntimeError("ffmpeg render failed with exit code 187.\nError while opening encoder"),
                None,
            ]
            with patch("builtins.print"):
                vps.render(images, Path("Renders/render.mp4"), 1920, 1080, cfg)

        self.assertEqual(mock_run.call_count, 2)
        first_cmd = mock_run.call_args_list[0].args[0]
        second_cmd = mock_run.call_args_list[1].args[0]
        self.assertIn("h264_videotoolbox", first_cmd)
        self.assertIn("libx264", second_cmd)

    # -----------------------------------------------------------------------
    # main() smoke tests
    # -----------------------------------------------------------------------

    def test_main_smoke_prints_render_settings_and_outputs(self):
        with TemporaryDirectory() as tmp:
            session_dir = str(Path(tmp) / "videophotoslide_test")
            args = _make_args(motion_style="kenburns", seed=7, workdir=tmp)
            images = [Path("work/000000_a.png"), Path("work/000001_b.png")]
            infos = [self._info("a.png"), self._info("b.png")]
            fake_output_stat = SimpleNamespace(st_size=5 * 1024 * 1024)

            stream = StringIO()
            with patch("videophotoslide.parse_args", return_value=args), \
                 patch("videophotoslide.collect_media", return_value=(images, infos)), \
                 patch("videophotoslide.sort_images_and_infos", return_value=(images, infos)), \
                 patch("videophotoslide.ffmpeg_has_encoder", return_value=False), \
                 patch("videophotoslide.tempfile.mkdtemp", return_value=session_dir), \
                 patch("videophotoslide.datetime") as mock_datetime, \
                 patch("videophotoslide.ensure_dir"), \
                 patch("videophotoslide.import_media_to_photos") as mock_import, \
                 patch("videophotoslide.render") as mock_render, \
                 patch("videophotoslide.build_targets", return_value=[("render.mp4", 1920, 1080)]), \
                 patch("pathlib.Path.exists", return_value=True), \
                 patch("pathlib.Path.is_dir", return_value=True), \
                 patch("pathlib.Path.stat", return_value=fake_output_stat), \
                 patch("videophotoslide.shutil.rmtree") as mock_rmtree, \
                 redirect_stdout(stream):
                mock_datetime.now.return_value.strftime.return_value = "20260322-120000"
                vps.main()

            output = stream.getvalue()
            self.assertIn("Render settings: encoder=libx264, transition=fade, motion=kenburns", output)
            self.assertIn("DONE", output)
            mock_render.assert_called_once()
            render_call = mock_render.call_args
            self.assertEqual(render_call.args[0], images)
            self.assertEqual(render_call.args[1], Path("Renders") / "render.mp4")
            self.assertEqual(render_call.args[4].transition, "fade")
            mock_import.assert_not_called()
            mock_rmtree.assert_called_once_with(Path(session_dir), ignore_errors=True)

    def test_main_can_upload_to_youtube_after_render(self):
        with TemporaryDirectory() as tmp:
            args = _make_args(
                youtube_upload=True,
                youtube_title="{input_dir} {format}",
                youtube_description="Description",
                youtube_tags="travel, arizona",
                workdir=tmp,
            )
            images = [Path("work/000000_a.png")]
            infos = [self._info("a.png")]
            fake_output_stat = SimpleNamespace(st_size=2 * 1024 * 1024)

            stream = StringIO()
            with patch("videophotoslide.parse_args", return_value=args), \
                 patch("videophotoslide.collect_media", return_value=(images, infos)), \
                 patch("videophotoslide.sort_images_and_infos", return_value=(images, infos)), \
                 patch("videophotoslide._load_youtube_credentials"), \
                 patch("videophotoslide.ffmpeg_has_encoder", return_value=False), \
                 patch("videophotoslide.datetime") as mock_datetime, \
                 patch("videophotoslide.ensure_dir"), \
                 patch("videophotoslide.import_media_to_photos"), \
                 patch("videophotoslide.render"), \
                 patch("videophotoslide.upload_video_to_youtube", return_value="abc123") as mock_upload, \
                 patch("videophotoslide.build_targets", return_value=[("render.mp4", 1920, 1080)]), \
                 patch("pathlib.Path.exists", return_value=True), \
                 patch("pathlib.Path.is_dir", return_value=True), \
                 patch("pathlib.Path.stat", return_value=fake_output_stat), \
                 patch("videophotoslide.shutil.rmtree"), \
                 redirect_stdout(stream):
                mock_datetime.now.return_value.strftime.return_value = "20260322-120000"
                vps.main()

        output = stream.getvalue()
        self.assertIn("Uploading to YouTube: input_photos 1920x1080", output)
        self.assertIn("YouTube upload complete: https://www.youtube.com/watch?v=abc123", output)
        mock_upload.assert_called_once_with(
            video_path=Path("Renders") / "render.mp4",
            title="input_photos 1920x1080",
            description="Description",
            tags=["travel", "arizona"],
            category="22",
            privacy="private",
            client_secrets=Path("./client_secrets.json"),
            token_file=Path("./.youtube_token.json"),
        )

    def test_main_fails_before_render_if_youtube_auth_preflight_fails(self):
        args = _make_args(youtube_upload=True)

        with patch("videophotoslide.parse_args", return_value=args), \
             patch("videophotoslide.ensure_dir"), \
             patch("videophotoslide._load_youtube_credentials", side_effect=SystemExit("auth expired")), \
             patch("videophotoslide.render") as mock_render, \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.is_dir", return_value=True):
            with self.assertRaises(SystemExit) as ctx:
                vps.main()

        self.assertEqual(str(ctx.exception), "auth expired")
        mock_render.assert_not_called()

    def test_main_can_upload_existing_render_without_rendering(self):
        args = _make_args(
            source_dir=None,
            youtube_upload_file="./Renders/20260322-194059_lorena_fmt16x9.mp4",
            youtube_title="{filename} {format}",
            youtube_description="Description",
            youtube_tags="travel, arizona",
        )
        stream = StringIO()

        with patch("videophotoslide.parse_args", return_value=args), \
             patch("videophotoslide.upload_video_to_youtube", return_value="xyz789") as mock_upload, \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.is_file", return_value=True), \
             redirect_stdout(stream):
            vps.main()

        output = stream.getvalue()
        self.assertIn("Uploading existing render to YouTube: 20260322-194059_lorena_fmt16x9.mp4 16x9", output)
        self.assertIn("YouTube upload complete: https://www.youtube.com/watch?v=xyz789", output)
        mock_upload.assert_called_once_with(
            video_path=Path("./Renders/20260322-194059_lorena_fmt16x9.mp4"),
            title="20260322-194059_lorena_fmt16x9.mp4 16x9",
            description="Description",
            tags=["travel", "arizona"],
            category="22",
            privacy="private",
            client_secrets=Path("./client_secrets.json"),
            token_file=Path("./.youtube_token.json"),
        )

    def test_main_upload_only_default_title_uses_inferred_input_dir(self):
        args = _make_args(
            source_dir=None,
            youtube_upload_file="./Renders/20260322-194059_lorena-climbing-prescott_fmt16x9.mp4",
            youtube_title=None,
        )
        stream = StringIO()

        with patch("videophotoslide.parse_args", return_value=args), \
             patch("videophotoslide.upload_video_to_youtube", return_value="xyz789") as mock_upload, \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.is_file", return_value=True), \
             redirect_stdout(stream):
            vps.main()

        output = stream.getvalue()
        self.assertIn("Uploading existing render to YouTube: lorena-climbing-prescott slideshow (16x9)", output)
        self.assertEqual(mock_upload.call_args.kwargs["title"], "lorena-climbing-prescott slideshow (16x9)")

    def test_main_can_import_render_to_photos(self):
        with TemporaryDirectory() as tmp:
            args = _make_args(add_to_photos=True, workdir=tmp)
            images = [Path("work/000000_a.png")]
            infos = [self._info("a.png")]
            fake_output_stat = SimpleNamespace(st_size=2 * 1024 * 1024)

            stream = StringIO()
            with patch("videophotoslide.parse_args", return_value=args), \
                 patch("videophotoslide.collect_media", return_value=(images, infos)), \
                 patch("videophotoslide.sort_images_and_infos", return_value=(images, infos)), \
                 patch("videophotoslide.ffmpeg_has_encoder", return_value=False), \
                 patch("videophotoslide.datetime") as mock_datetime, \
                 patch("videophotoslide.ensure_dir"), \
                 patch("videophotoslide.import_media_to_photos") as mock_import, \
                 patch("videophotoslide.render"), \
                 patch("videophotoslide.build_targets", return_value=[("render.mp4", 1920, 1080)]), \
                 patch("pathlib.Path.exists", return_value=True), \
                 patch("pathlib.Path.is_dir", return_value=True), \
                 patch("pathlib.Path.stat", return_value=fake_output_stat), \
                 patch("videophotoslide.shutil.rmtree"), \
                 redirect_stdout(stream):
                mock_datetime.now.return_value.strftime.return_value = "20260322-120000"
                vps.main()

        output = stream.getvalue()
        self.assertIn("Importing into Photos: render.mp4", output)
        mock_import.assert_called_once_with([Path("Renders") / "render.mp4"])

    def test_main_passes_smart_focus_flag_to_collect_and_render(self):
        with TemporaryDirectory() as tmp:
            args = _make_args(motion_style="kenburns", smart_focus=True, workdir=tmp)
            images = [Path("work/000000_a.png")]
            infos = [self._info("a.png")]
            infos[0].focal_point = (0.3, 0.4)
            fake_output_stat = SimpleNamespace(st_size=2 * 1024 * 1024)

            stream = StringIO()
            with patch("videophotoslide.parse_args", return_value=args), \
                 patch("videophotoslide.collect_media", return_value=(images, infos)) as mock_collect, \
                 patch("videophotoslide.sort_images_and_infos", return_value=(images, infos)), \
                 patch("videophotoslide.ffmpeg_has_encoder", return_value=False), \
                 patch("videophotoslide.datetime") as mock_datetime, \
                 patch("videophotoslide.ensure_dir"), \
                 patch("videophotoslide.import_media_to_photos"), \
                 patch("videophotoslide.render") as mock_render, \
                 patch("videophotoslide.build_targets", return_value=[("render.mp4", 1920, 1080)]), \
                 patch("pathlib.Path.exists", return_value=True), \
                 patch("pathlib.Path.is_dir", return_value=True), \
                 patch("pathlib.Path.stat", return_value=fake_output_stat), \
                 patch("videophotoslide.shutil.rmtree"), \
                 redirect_stdout(stream):
                mock_datetime.now.return_value.strftime.return_value = "20260322-120000"
                vps.main()

        self.assertTrue(mock_collect.call_args.kwargs["detect_focus"])
        self.assertEqual(mock_render.call_args.kwargs["focal_points"], [(0.3, 0.4)])

    def test_main_prints_prep_progress_when_enabled(self):
        with TemporaryDirectory() as tmp:
            args = _make_args(progress=True, workdir=tmp)
            images = [Path("work/000000_a.png")]
            infos = [self._info("a.png")]
            fake_output_stat = SimpleNamespace(st_size=2 * 1024 * 1024)

            stream = StringIO()
            with patch("videophotoslide.parse_args", return_value=args), \
                 patch("videophotoslide.collect_media", return_value=(images, infos)), \
                 patch("videophotoslide.sort_images_and_infos", return_value=(images, infos)), \
                 patch("videophotoslide.ffmpeg_has_encoder", return_value=False), \
                 patch("videophotoslide.datetime") as mock_datetime, \
                 patch("videophotoslide.ensure_dir"), \
                 patch("videophotoslide.import_media_to_photos"), \
                 patch("videophotoslide.render"), \
                 patch("videophotoslide.build_targets", return_value=[("render.mp4", 1920, 1080)]), \
                 patch("pathlib.Path.exists", return_value=True), \
                 patch("pathlib.Path.is_dir", return_value=True), \
                 patch("pathlib.Path.stat", return_value=fake_output_stat), \
                 patch("videophotoslide.shutil.rmtree"), \
                 redirect_stdout(stream):
                mock_datetime.now.return_value.strftime.return_value = "20260322-120000"
                vps.main()

        output = stream.getvalue()
        self.assertIn("[phase prep] scanning input_photos", output)
        self.assertIn("[phase prep] ordering 1 items (0 video clips) with sort=natural", output)

    # -----------------------------------------------------------------------
    # parse_args
    # -----------------------------------------------------------------------

    # -----------------------------------------------------------------------
    # build_targets — clip count suffix
    # -----------------------------------------------------------------------

    def test_build_targets_with_clips_includes_c_suffix(self):
        targets = vps.build_targets(
            fmt="16x9",
            stamp="20260315-214501",
            input_dir_name="Trip Photos",
            quality="standard",
            transition="fade",
            photo_count=10,
            clip_count=2,
        )
        names = [name for name, *_ in targets]
        self.assertEqual(names, ["20260315-214501_trip-photos_fmt16x9_qstandard_transition-fade_n10c2.mp4"])

    def test_build_targets_photo_only_omits_c_suffix(self):
        targets = vps.build_targets(
            fmt="16x9",
            stamp="20260315-214501",
            input_dir_name="Trip Photos",
            quality="standard",
            transition="fade",
            photo_count=12,
        )
        names = [name for name, *_ in targets]
        self.assertEqual(names, ["20260315-214501_trip-photos_fmt16x9_qstandard_transition-fade_n12.mp4"])

    # -----------------------------------------------------------------------
    # build_media_durations — clip_max_sec
    # -----------------------------------------------------------------------

    def test_build_media_durations_respects_clip_max_sec(self):
        clip_info = vps.PhotoInfo(
            path=Path("clip.mp4"), width=1920, height=1080,
            aspect_ratio=1.78, is_landscape=True, orientation="landscape",
            is_video=True, video_duration=30.0,
        )
        durations = vps.build_media_durations(
            [clip_info], base_sec=2.8, xfade=0.7, rhythm_strength=0.0, seed=0,
            clip_max_sec=8.0,
        )
        self.assertAlmostEqual(durations[0], 8.0, places=3)

    def test_build_media_durations_clip_max_sec_not_below_min(self):
        clip_info = vps.PhotoInfo(
            path=Path("clip.mp4"), width=1920, height=1080,
            aspect_ratio=1.78, is_landscape=True, orientation="landscape",
            is_video=True, video_duration=5.0,
        )
        # clip_max_sec below min_clip_dur: result should still be >= min_clip_dur
        durations = vps.build_media_durations(
            [clip_info], base_sec=2.8, xfade=0.7, rhythm_strength=0.0, seed=0,
            clip_max_sec=0.1,
        )
        self.assertGreaterEqual(durations[0], 0.7 + 0.1)

    # -----------------------------------------------------------------------
    # audio fade — simple path
    # -----------------------------------------------------------------------

    def test_audio_fade_applied_in_simple_audio_path(self):
        images = [Path("work/000000_a.png")]
        cfg = vps.RenderConfig(
            encoder="libx264",
            audio_path=Path("music.mp3"),
            audio_fade=2.0,
        )
        cmd, _ = vps.build_render_command(images, Path("out.mp4"), 1920, 1080, cfg)
        af_idx = cmd.index("-af")
        self.assertIn("afade=t=out", cmd[af_idx + 1])
        self.assertIn("d=2.000", cmd[af_idx + 1])

    def test_audio_fade_applied_in_complex_audio_path(self):
        clip_info = vps.PhotoInfo(
            path=Path("c.mp4"), width=1920, height=1080,
            aspect_ratio=1.78, is_landscape=True, orientation="landscape",
            is_video=True, video_duration=5.0,
        )
        result = vps._build_clip_audio_filters(
            infos_list=[clip_info],
            media_durations=[5.0],
            xfade=0.7,
            audio_input_idx=1,
            clip_audio="keep",
            audio_fade=2.0,
            total_duration=5.0,
        )
        self.assertIn("afade=t=out", result)
        self.assertIn("d=2.000", result)
        self.assertIn("[aout]", result)

    def test_build_render_command_complex_audio_without_background_pads_to_video_duration(self):
        clip_info = vps.PhotoInfo(
            path=Path("c.mp4"), width=1920, height=1080,
            aspect_ratio=1.78, is_landscape=True, orientation="landscape",
            is_video=True, video_duration=5.0, has_audio=True,
        )
        photo_info = self._info("p.png")
        cfg = vps.RenderConfig(encoder="libx264", clip_audio="keep")

        cmd, _duration = vps.build_render_command(
            images=[Path("c.mp4"), Path("p.png")],
            out_path=Path("out.mp4"),
            width=1920,
            height=1080,
            cfg=cfg,
            focal_points=[None, None],
            infos=[clip_info, photo_info],
        )

        filter_complex = cmd[cmd.index("-filter_complex") + 1]
        self.assertIn("apad,atrim=duration=", filter_complex)
        self.assertIn("[aout]", filter_complex)
        audio_map_idx = cmd.index("-map", cmd.index("-map") + 1)
        self.assertEqual(cmd[audio_map_idx + 1], "[aout]")

    # -----------------------------------------------------------------------
    # dry-run skips YouTube preflight
    # -----------------------------------------------------------------------

    def test_dry_run_skips_youtube_preflight(self):
        with TemporaryDirectory() as tmp:
            args = _make_args(youtube_upload=True, dry_run=True, source_dir=tmp, workdir=tmp)

            with patch("videophotoslide.parse_args", return_value=args), \
                 patch("videophotoslide.ensure_dir"), \
                 patch("videophotoslide._load_youtube_credentials") as mock_creds:
                with self.assertRaises(SystemExit):
                    # Will exit when no media is found, but preflight must not fire
                    vps.main()

        mock_creds.assert_not_called()

    # -----------------------------------------------------------------------
    # parse_args
    # -----------------------------------------------------------------------

    def test_parse_args_defaults_to_renders_folder(self):
        with patch("sys.argv", ["videophotoslide.py", "./input_photos"]):
            args = vps.parse_args()
        self.assertEqual(args.outdir, "./Renders")
        self.assertFalse(args.progress)

    def test_parse_args_allows_upload_only_mode_without_source_dir(self):
        with patch("sys.argv", ["videophotoslide.py", "--youtube-upload-file", "./Renders/render.mp4"]):
            args = vps.parse_args()
        self.assertIsNone(args.source_dir)
        self.assertEqual(args.youtube_upload_file, "./Renders/render.mp4")

    def test_parse_args_accepts_progress_flag(self):
        with patch("sys.argv", ["videophotoslide.py", "./input_photos", "--progress"]):
            args = vps.parse_args()
        self.assertTrue(args.progress)

    def test_parse_args_accepts_clip_grade_and_clip_audio(self):
        with patch("sys.argv", ["videophotoslide.py", "./input_photos",
                                "--clip-grade", "grade", "--clip-audio", "duck"]):
            args = vps.parse_args()
        self.assertEqual(args.clip_grade, "grade")
        self.assertEqual(args.clip_audio, "duck")


if __name__ == "__main__":
    unittest.main()
