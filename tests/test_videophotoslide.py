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

    def test_time_sort_keeps_all_images_and_orders_dated_first(self):
        images = [Path("a.png"), Path("b.png"), Path("c.png"), Path("d.png")]
        infos = [
            self._info("a.png", dt=datetime(2024, 1, 3, 10, 0, 0)),
            None,
            self._info("c.png", dt=datetime(2024, 1, 1, 10, 0, 0)),
            self._info("d.png", dt=None),
        ]

        out = vps.sort_photos(images, infos, sort_by="time")

        self.assertEqual(len(out), len(images))
        self.assertEqual(out, [Path("c.png"), Path("a.png"), Path("b.png"), Path("d.png")])

    def test_location_sort_keeps_all_images_and_orders_geotagged_first(self):
        images = [Path("a.png"), Path("b.png"), Path("c.png"), Path("d.png")]
        infos = [
            self._info("a.png", gps=(37.0, -122.0)),
            None,
            self._info("c.png", gps=(35.0, -120.0)),
            self._info("d.png", gps=None),
        ]

        out = vps.sort_photos(images, infos, sort_by="location")

        self.assertEqual(len(out), len(images))
        self.assertEqual(out, [Path("c.png"), Path("a.png"), Path("b.png"), Path("d.png")])

    def test_random_sort_does_not_mutate_input_list(self):
        images = [Path("1.png"), Path("2.png"), Path("3.png"), Path("4.png")]
        before = images.copy()

        _ = vps.sort_photos(images, [None] * len(images), sort_by="random", seed=42)

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

        sorted_images, sorted_infos = vps.sort_images_and_infos(images, infos, sort_by="time")

        self.assertEqual(sorted_images, [Path("b.png"), Path("c.png"), Path("a.png")])
        self.assertEqual([info.focal_point for info in sorted_infos], [(0.3, 0.4), (0.5, 0.6), (0.1, 0.2)])

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

    def test_build_targets_uses_required_filename_fields(self):
        targets = vps.build_targets(
            fmt="both",
            stamp="20260315-214501",
            input_dir_name="Input Photos",
            quality="standard",
            transition="fade",
            photo_count=12,
        )

        names = [name for name, _w, _h in targets]
        self.assertEqual(
            names,
            [
                "20260315-214501_input-photos_fmt16x9_qstandard_transition-fade_n12.mp4",
                "20260315-214501_input-photos_fmt9x16_qstandard_transition-fade_n12.mp4",
            ],
        )

    def test_validate_transition_rejects_unknown_value(self):
        with self.assertRaises(SystemExit) as ctx:
            vps.validate_transition("not-a-real-transition")
        self.assertIn("--transition must be one of:", str(ctx.exception))

    def test_parse_args_rejects_invalid_transition(self):
        with patch("sys.argv", ["videophotoslide.py", "./input_photos", "--transition", "bad-transition"]):
            with self.assertRaises(SystemExit):
                vps.parse_args()

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

    def test_import_media_to_photos_uses_osascript(self):
        with patch("videophotoslide.subprocess.run") as mock_run:
            vps.import_media_to_photos([Path("Renders/render.mp4")])

        cmd = mock_run.call_args.args[0]
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[1], "-e")
        self.assertIn('tell application "Photos"', cmd[2])
        self.assertEqual(cmd[3], str((Path.cwd() / "Renders/render.mp4").resolve()))

    def test_build_filter_for_still_uses_focus_aware_crop_expression(self):
        filt = vps.build_filter_for_still(
            0,
            1920,
            1080,
            30,
            2.8,
            ken_strength=0.0015,
            focal_point=(0.25, 0.4),
        )

        self.assertIn("crop=1920:1080", filt)
        self.assertIn("force_original_aspect_ratio=increase", filt)
        self.assertIn("3*pow(min(max((t/2.8),0),1),2)-2*pow(min(max((t/2.8),0),1),3)", filt)
        self.assertIn("*iw)-(ow/2)", filt)
        self.assertIn("*ih)-(oh/2)", filt)

    def test_build_filter_for_still_without_focus_keeps_overlay_path(self):
        filt = vps.build_filter_for_still(
            0,
            1920,
            1080,
            30,
            2.8,
            ken_strength=0.0015,
            focal_point=None,
        )

        self.assertIn("force_original_aspect_ratio=increase", filt)
        self.assertIn("crop=1920:1080", filt)
        self.assertNotIn("[bg0][fg0]overlay=(W-w)/2:(H-h)/2", filt)

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

        with patch("videophotoslide.run_ffmpeg_with_progress") as mock_run:
            mock_run.side_effect = [
                RuntimeError("ffmpeg render failed with exit code 187.\nError while opening encoder"),
                None,
            ]
            with patch("builtins.print") as mock_print:
                vps.render(
                    images,
                    Path("Renders/render.mp4"),
                    1920,
                    1080,
                    encoder="h264_videotoolbox",
                )

        self.assertEqual(mock_run.call_count, 2)
        first_cmd = mock_run.call_args_list[0].args[0]
        second_cmd = mock_run.call_args_list[1].args[0]
        self.assertIn("h264_videotoolbox", first_cmd)
        self.assertIn("libx264", second_cmd)
        mock_print.assert_any_call("VideoToolbox encoder failed; retrying with libx264")

    def test_main_smoke_prints_render_settings_and_outputs(self):
        args = SimpleNamespace(
            source_dir="input_photos",
            outdir="./Renders",
            workdir="./.work_pngs",
            quality="standard",
            format="16x9",
            sort_by="natural",
            max_workers=0,
            camera_stats=False,
            motion_style="kenburns",
            ken_burns_strength=None,
            parallax_px=None,
            smart_focus=False,
            progress=False,
            sec=2.8,
            xfade=0.7,
            transition="fade",
            seed=7,
            rhythm_strength=0.12,
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
        images = [Path("work/000000_a.png"), Path("work/000001_b.png")]
        infos = [self._info("a.png"), self._info("b.png")]
        fake_output = Path("/tmp/render.mp4")
        fake_output_stat = SimpleNamespace(st_size=5 * 1024 * 1024)

        with TemporaryDirectory() as tmp:
            temp_work = Path(tmp)
            stream = StringIO()
            with patch("videophotoslide.parse_args", return_value=args), \
                 patch("videophotoslide.convert_to_pngs", return_value=(images, infos)), \
                 patch("videophotoslide.sort_images_and_infos", return_value=(images, infos)), \
                 patch("videophotoslide.ffmpeg_has_encoder", return_value=False), \
                 patch("videophotoslide.tempfile.mkdtemp", return_value=str(temp_work)), \
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
        render_args = mock_render.call_args
        self.assertEqual(render_args.args[0], images)
        self.assertEqual(render_args.args[1], Path("Renders") / "render.mp4")
        self.assertEqual(render_args.kwargs["transition"], "fade")
        mock_import.assert_not_called()
        mock_rmtree.assert_called_once_with(temp_work, ignore_errors=True)

    def test_main_can_upload_to_youtube_after_render(self):
        args = SimpleNamespace(
            source_dir="input_photos",
            outdir="./Renders",
            workdir="./.work_pngs",
            quality="standard",
            format="16x9",
            sort_by="natural",
            max_workers=0,
            camera_stats=False,
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
            youtube_upload=True,
            youtube_upload_file=None,
            add_to_photos=False,
            youtube_client_secrets="./client_secrets.json",
            youtube_token_file="./.youtube_token.json",
            youtube_title="{input_dir} {format}",
            youtube_description="Description",
            youtube_tags="travel, arizona",
            youtube_category="22",
            youtube_privacy="private",
        )
        images = [Path("work/000000_a.png")]
        infos = [self._info("a.png")]
        fake_output_stat = SimpleNamespace(st_size=2 * 1024 * 1024)

        with TemporaryDirectory() as tmp:
            temp_work = Path(tmp)
            stream = StringIO()
            with patch("videophotoslide.parse_args", return_value=args), \
                 patch("videophotoslide.convert_to_pngs", return_value=(images, infos)), \
                 patch("videophotoslide.sort_images_and_infos", return_value=(images, infos)), \
                 patch("videophotoslide.ffmpeg_has_encoder", return_value=False), \
                 patch("videophotoslide.tempfile.mkdtemp", return_value=str(temp_work)), \
                 patch("videophotoslide.datetime") as mock_datetime, \
                 patch("videophotoslide.ensure_dir"), \
                 patch("videophotoslide.import_media_to_photos"), \
                 patch("videophotoslide.render") as mock_render, \
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
        mock_render.assert_called_once()
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

    def test_main_can_upload_existing_render_without_rendering(self):
        args = SimpleNamespace(
            source_dir=None,
            outdir="./Renders",
            workdir="./.work_pngs",
            quality="standard",
            format="16x9",
            sort_by="natural",
            max_workers=0,
            camera_stats=False,
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
            youtube_upload=False,
            youtube_upload_file="./Renders/20260322-194059_lorena_fmt16x9.mp4",
            add_to_photos=False,
            youtube_client_secrets="./client_secrets.json",
            youtube_token_file="./.youtube_token.json",
            youtube_title="{filename} {format}",
            youtube_description="Description",
            youtube_tags="travel, arizona",
            youtube_category="22",
            youtube_privacy="private",
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

    def test_main_can_import_render_to_photos(self):
        args = SimpleNamespace(
            source_dir="input_photos",
            outdir="./Renders",
            workdir="./.work_pngs",
            quality="standard",
            format="16x9",
            sort_by="natural",
            max_workers=0,
            camera_stats=False,
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
            youtube_upload=False,
            youtube_upload_file=None,
            add_to_photos=True,
            youtube_client_secrets="./client_secrets.json",
            youtube_token_file="./.youtube_token.json",
            youtube_title=None,
            youtube_description="",
            youtube_tags="",
            youtube_category="22",
            youtube_privacy="private",
        )
        images = [Path("work/000000_a.png")]
        infos = [self._info("a.png")]
        fake_output_stat = SimpleNamespace(st_size=2 * 1024 * 1024)

        with TemporaryDirectory() as tmp:
            temp_work = Path(tmp)
            stream = StringIO()
            with patch("videophotoslide.parse_args", return_value=args), \
                 patch("videophotoslide.convert_to_pngs", return_value=(images, infos)), \
                 patch("videophotoslide.sort_images_and_infos", return_value=(images, infos)), \
                 patch("videophotoslide.ffmpeg_has_encoder", return_value=False), \
                 patch("videophotoslide.tempfile.mkdtemp", return_value=str(temp_work)), \
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

    def test_main_passes_smart_focus_flag_to_conversion_and_render(self):
        args = SimpleNamespace(
            source_dir="input_photos",
            outdir="./Renders",
            workdir="./.work_pngs",
            quality="standard",
            format="16x9",
            sort_by="natural",
            max_workers=0,
            camera_stats=False,
            motion_style="kenburns",
            ken_burns_strength=None,
            parallax_px=None,
            smart_focus=True,
            progress=False,
            sec=2.8,
            xfade=0.7,
            transition="fade",
            seed=0,
            rhythm_strength=0.12,
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
        images = [Path("work/000000_a.png")]
        infos = [self._info("a.png")]
        infos[0].focal_point = (0.3, 0.4)
        fake_output_stat = SimpleNamespace(st_size=2 * 1024 * 1024)

        with TemporaryDirectory() as tmp:
            temp_work = Path(tmp)
            stream = StringIO()
            with patch("videophotoslide.parse_args", return_value=args), \
                 patch("videophotoslide.convert_to_pngs", return_value=(images, infos)) as mock_convert, \
                 patch("videophotoslide.sort_images_and_infos", return_value=(images, infos)), \
                 patch("videophotoslide.ffmpeg_has_encoder", return_value=False), \
                 patch("videophotoslide.tempfile.mkdtemp", return_value=str(temp_work)), \
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

        self.assertTrue(mock_convert.call_args.kwargs["detect_focus"])
        self.assertEqual(mock_render.call_args.kwargs["focal_points"], [(0.3, 0.4)])

    def test_main_prints_prep_progress_when_enabled(self):
        args = SimpleNamespace(
            source_dir="input_photos",
            outdir="./Renders",
            workdir="./.work_pngs",
            quality="standard",
            format="16x9",
            sort_by="natural",
            max_workers=0,
            camera_stats=False,
            motion_style="none",
            ken_burns_strength=None,
            parallax_px=None,
            smart_focus=False,
            progress=True,
            sec=2.8,
            xfade=0.7,
            transition="fade",
            seed=0,
            rhythm_strength=0.12,
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
        images = [Path("work/000000_a.png")]
        infos = [self._info("a.png")]
        fake_output_stat = SimpleNamespace(st_size=2 * 1024 * 1024)

        with TemporaryDirectory() as tmp:
            temp_work = Path(tmp)
            stream = StringIO()
            with patch("videophotoslide.parse_args", return_value=args), \
                 patch("videophotoslide.convert_to_pngs", return_value=(images, infos)), \
                 patch("videophotoslide.sort_images_and_infos", return_value=(images, infos)), \
                 patch("videophotoslide.ffmpeg_has_encoder", return_value=False), \
                 patch("videophotoslide.tempfile.mkdtemp", return_value=str(temp_work)), \
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
        self.assertIn("[phase prep] ordering 1 prepared images with sort=natural", output)


if __name__ == "__main__":
    unittest.main()
