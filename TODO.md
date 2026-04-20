# TODO

## Effective Settings Summary

Add a default-on resolved settings summary so running `memoramotion` makes the full expanded configuration visible even when the user only passes a few options.

Recommended CLI:

```bash
memoramotion ./input_photos --quality youtube --resolution 4k
memoramotion ./input_photos --settings off
memoramotion ./input_photos --settings json
```

Add `--settings {on,off,json}` with default `on`.

Implementation outline:

- Extend `parse_args()` with `--settings`, choices `on`, `off`, `json`, default `on`.
- Add `build_effective_settings(...) -> dict` that receives parsed args and resolved values rather than recomputing them.
- Add `print_effective_settings(settings: dict, mode: str) -> None`.
- Call the settings printer in `main()` after validation and preset expansion, but before expensive media work.
- The summary should show actual values in use, including defaults and derived values.
- For upload-only mode, show only upload-related settings plus any inferred title/format values.

Values to include:

- App: `app_name`, `app_version`.
- Inputs/outputs: `source_dir`, `outdir`, `workdir`, `format`, `resolution`, target dimensions for selected formats.
- Quality/render: `quality`, resolved `fps`, resolved `bitrate`, selected `encoder`, `dry_run`.
- Timing/editing: `sec`, `xfade`, `transition`, `rhythm_strength`, `sort_by`, `seed`, `split_secs`.
- Motion: `motion_style`, resolved Ken Burns strength from `resolve_motion_values()`, resolved parallax px, `smart_focus`, smart-focus model dir and explicit model paths if supplied.
- Media handling: `camera_stats`, `geocode`, `location_stats`, `location_overlay`, `clip_grade`, `clip_audio`, `clip_max_sec`.
- Audio: `audio`, `audio_offset`, `audio_fade`.
- Actions: `youtube_upload`, `youtube_upload_file`, `youtube_privacy`, `add_to_photos`.

Notes:

- `build_targets()` needs media counts for final filenames, so the early settings block should show target dimensions rather than final filenames.
- Keep the existing later render summary or replace it with a shorter "starting render" line once the settings block exists.
- JSON mode should write stable key names and simple scalar/list/dict values so it can be used by scripts.

Tests to add:

- `parse_args()` accepts all settings modes and defaults to `on`.
- `build_effective_settings()` includes derived `fps`, `bitrate`, target dimensions, resolved motion values, and action flags.
- `main()` prints the human-readable block before collection/render work.
- `--settings off` suppresses the block.
- `--settings json` prints parseable JSON.

Docs to update:

- README usage section with a short example settings block.
- Key options table with `--settings`.
- CLAUDE.md running/architecture notes.
