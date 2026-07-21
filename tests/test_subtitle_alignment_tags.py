from __future__ import annotations

import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import extract_english_subtitle as extract
import mkv_to_mp4_with_english_subtitle as workflow


ASS_TRACK = {
    "id": 2,
    "type": "subtitles",
    "codec": "SubStationAlpha",
    "properties": {
        "language": "eng",
        "codec_id": "S_TEXT/ASS",
    },
}
SRT_TRACK = {
    "id": 3,
    "type": "subtitles",
    "codec": "SubRip/SRT",
    "properties": {
        "language": "eng",
        "codec_id": "S_TEXT/UTF8",
    },
}


class CommandLineTests(unittest.TestCase):
    def test_no_export_subtitle_is_supported(self) -> None:
        args = workflow.build_parser().parse_args(
            ["sample.mkv", "--no-export-subtitle"]
        )

        self.assertTrue(args.no_export_subtitle)

    def test_temporary_subtitle_is_no_longer_supported(self) -> None:
        with mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                workflow.build_parser().parse_args(
                    ["sample.mkv", "--temporary-subtitle"]
                )


class CleanExportedAlignmentTagsTests(unittest.TestCase):
    def test_srt_removes_alignment_tags_and_preserves_other_override_tags(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            subtitle = Path(temp_dir) / "sample.srt"
            subtitle.write_bytes(
                b"1\r\n00:00:01,000 --> 00:00:02,000\r\n"
                b"{\\an8}Top line\r\n\r\n"
                b"2\r\n00:00:03,000 --> 00:00:04,000\r\n"
                b"{\\an7\\i1}Styled line{\\i0}\r\n"
            )

            removed_count = extract.clean_exported_alignment_tags(subtitle)

            self.assertEqual(removed_count, 2)
            self.assertEqual(
                subtitle.read_bytes(),
                b"1\r\n00:00:01,000 --> 00:00:02,000\r\n"
                b"Top line\r\n\r\n"
                b"2\r\n00:00:03,000 --> 00:00:04,000\r\n"
                b"{\\i1}Styled line{\\i0}\r\n",
            )

    def test_ass_removes_tags_from_dialogue_and_comment_events(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            subtitle = Path(temp_dir) / "sample.ass"
            subtitle.write_bytes(
                b"[Script Info]\n"
                b"Title: {\\an8} metadata\n"
                b"[Events]\n"
                b"Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,"
                b"{\\an8}Top line\n"
                b"Comment: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,"
                b"{\\an7}comment\n"
            )

            removed_count = extract.clean_exported_alignment_tags(subtitle)

            self.assertEqual(removed_count, 2)
            self.assertEqual(
                subtitle.read_bytes(),
                b"[Script Info]\n"
                b"Title: {\\an8} metadata\n"
                b"[Events]\n"
                b"Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Top line\n"
                b"Comment: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,comment\n",
            )

    def test_atomic_clean_failure_preserves_original_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            subtitle = root / "sample.srt"
            original = b"{\\an8}Original\r\n"
            subtitle.write_bytes(original)

            with mock.patch.object(extract.os, "replace", side_effect=OSError("locked")):
                with self.assertRaises(OSError):
                    extract.clean_exported_alignment_tags(subtitle)

            self.assertEqual(subtitle.read_bytes(), original)
            self.assertEqual(list(root.glob(".sample.clean_*.srt")), [])

    def test_export_copy_keeps_source_complete_and_atomically_replaces_output(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            source = root / "raw.ass"
            output = root / "sample.ass"
            source.write_bytes(
                b"[Events]\n"
                b"Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,"
                b"{\\an8}Top line\n"
                b"Comment: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,"
                b"{\\an7}comment\n"
            )
            output.write_bytes(b"old subtitle\n")

            removed_count = extract.export_subtitle_copy(source, output, ASS_TRACK)

            self.assertEqual(removed_count, 2)
            self.assertIn(b"{\\an8}Top line", source.read_bytes())
            self.assertNotIn(b"\\an", output.read_bytes())
            self.assertEqual(list(root.glob(".sample.export_*.ass")), [])


class WorkflowAlignmentTagTests(unittest.TestCase):
    def run_retained_workflow(
        self,
        root: Path,
        track: dict,
        raw_content: bytes,
    ) -> tuple[int, Path, Path, list[str], bytes]:
        suffix = extract.get_output_path(root / "sample.mkv", track).suffix
        mkv = root / "sample.mkv"
        raw_subtitle = root / f"raw{suffix}"
        exported_subtitle = root / f"sample{suffix}"
        output = root / "sample.mp4"
        mkv.touch()
        burned_arguments: list[str] = []
        burned_content = b""

        def write_extracted_subtitle(*_args: object) -> None:
            raw_subtitle.write_bytes(raw_content)

        def burn_main(arguments: list[str]) -> int:
            nonlocal burned_arguments, burned_content
            burned_arguments = arguments
            burned_content = Path(arguments[1]).read_bytes()
            return 0

        args = Namespace(
            mkv=mkv,
            output=output,
            crf=24.0,
            audio_bitrate=128,
            keyint=None,
            fallback_ffmpeg=None,
            overwrite=False,
            no_export_subtitle=False,
            keep_media_temp=False,
        )

        with (
            mock.patch.object(workflow, "build_parser") as build_parser,
            mock.patch.object(extract, "validate_binaries"),
            mock.patch.object(extract, "resolve_input", return_value=mkv),
            mock.patch.object(workflow, "select_english_subtitle", return_value=track),
            mock.patch.object(workflow.burn, "validate_binaries"),
            mock.patch.object(workflow.burn, "resolve_output", return_value=output),
            mock.patch.object(
                workflow,
                "prepare_subtitle_outputs",
                return_value=(raw_subtitle, exported_subtitle, [raw_subtitle]),
            ),
            mock.patch.object(
                extract,
                "extract_subtitle",
                side_effect=write_extracted_subtitle,
            ),
            mock.patch.object(workflow.burn, "main", side_effect=burn_main),
        ):
            build_parser.return_value.parse_args.return_value = args
            exit_code = workflow.main([])

        return (
            exit_code,
            raw_subtitle,
            exported_subtitle,
            burned_arguments,
            burned_content,
        )

    def test_retained_ass_burns_raw_file_and_exports_clean_copy(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            raw_content = (
                b"[Events]\n"
                b"Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,"
                b"{\\an8}Top line\n"
            )

            exit_code, raw, exported, burned_arguments, burned_content = (
                self.run_retained_workflow(Path(temp_dir), ASS_TRACK, raw_content)
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(Path(burned_arguments[1]), raw)
            self.assertIn(b"{\\an8}Top line", burned_content)
            self.assertNotIn(b"\\an8", exported.read_bytes())
            self.assertFalse(raw.exists())

    def test_retained_srt_burns_raw_file_and_exports_clean_copy(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            raw_content = (
                b"1\r\n00:00:01,000 --> 00:00:02,000\r\n"
                b"{\\an8}Top line\r\n"
            )

            exit_code, raw, exported, burned_arguments, burned_content = (
                self.run_retained_workflow(Path(temp_dir), SRT_TRACK, raw_content)
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(Path(burned_arguments[1]), raw)
            self.assertIn(b"{\\an8}Top line", burned_content)
            self.assertNotIn(b"\\an8", exported.read_bytes())
            self.assertFalse(raw.exists())


class AtomicExtractionTests(unittest.TestCase):
    def test_overwrite_failure_preserves_existing_subtitle(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            mkv = root / "sample.mkv"
            output = root / "sample.srt"
            mkv.touch()
            output.write_bytes(b"existing subtitle\n")

            def fail_extraction(_mkv: Path, _track: dict, staging: Path) -> None:
                staging.write_bytes(b"partial subtitle\n")
                raise extract.ExtractSubtitleError("simulated failure")

            args = Namespace(mkv=mkv, overwrite=True)
            with (
                mock.patch.object(extract, "build_parser") as build_parser,
                mock.patch.object(extract, "validate_binaries"),
                mock.patch.object(extract, "resolve_input", return_value=mkv),
                mock.patch.object(extract, "identify_tracks", return_value=[SRT_TRACK]),
                mock.patch.object(
                    extract,
                    "extract_subtitle",
                    side_effect=fail_extraction,
                ),
            ):
                build_parser.return_value.parse_args.return_value = args
                exit_code = extract.main([])

            self.assertEqual(exit_code, 1)
            self.assertEqual(output.read_bytes(), b"existing subtitle\n")
            self.assertEqual(list(root.glob(".sample.extract_*.srt")), [])


if __name__ == "__main__":
    unittest.main()
