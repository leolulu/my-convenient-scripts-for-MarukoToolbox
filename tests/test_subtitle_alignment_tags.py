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
        self.assertEqual(args.audio_language, "jpn")

    def test_audio_language_accepts_english(self) -> None:
        args = workflow.build_parser().parse_args(
            ["sample.mkv", "--audio-language", "eng"]
        )

        self.assertEqual(args.audio_language, "eng")

    def test_temporary_subtitle_is_no_longer_supported(self) -> None:
        with mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                workflow.build_parser().parse_args(
                    ["sample.mkv", "--temporary-subtitle"]
                )


class BatchCliTests(unittest.TestCase):
    def test_expand_inputs_expands_top_level_mkv_files_only(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            (root / "a.mkv").touch()
            (root / "b.MKV").touch()
            (root / "c.txt").touch()
            nested = root / "nested"
            nested.mkdir()
            (nested / "d.mkv").touch()

            expanded = workflow.expand_inputs([root])

            names = [path.name for path in expanded]
            self.assertEqual(names, ["a.mkv", "b.MKV"])
            self.assertNotIn("d.mkv", names)

    def test_expand_inputs_deduplicates_preserving_order(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            mkv = root / "a.mkv"
            mkv.touch()
            other = root / "other.mkv"
            other.touch()

            expanded = workflow.expand_inputs([mkv, root, mkv])

            self.assertEqual(
                [path.name for path in expanded],
                ["a.mkv", "other.mkv"],
            )

    def test_expand_inputs_warns_and_skips_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            empty = root / "empty"
            empty.mkdir()

            with mock.patch("sys.stderr") as stderr:
                expanded = workflow.expand_inputs([empty])

            self.assertEqual(expanded, [])
            stderr.write.assert_called()
            self.assertIn(".mkv", "".join(c[0][0] for c in stderr.write.call_args_list))

    def test_expand_inputs_fails_on_nonexistent_path(self) -> None:
        with self.assertRaises(workflow.MkvToMp4Error):
            workflow.expand_inputs([Path("no_such_file.mkv")])

    def test_expand_inputs_fails_on_non_mkv_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            video = root / "video.mp4"
            video.touch()

            with self.assertRaises(extract.ExtractSubtitleError):
                workflow.expand_inputs([video])

    def test_has_processed_output_checks_default_output(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            mkv = root / "a.mkv"
            mkv.touch()
            output = root / "a_x264.mp4"

            args = Namespace(output=None)

            self.assertFalse(workflow.has_processed_output(mkv, args))

            output.touch()
            self.assertTrue(workflow.has_processed_output(mkv, args))

    def test_has_processed_output_accepts_same_stem_mp4(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            mkv = root / "a.mkv"
            same_stem_output = root / "a.mp4"
            mkv.touch()

            args = Namespace(output=None)

            self.assertFalse(workflow.has_processed_output(mkv, args))

            same_stem_output.touch()
            self.assertTrue(workflow.has_processed_output(mkv, args))

    def test_has_processed_output_honors_custom_output_path(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            mkv = root / "a.mkv"
            mkv.touch()
            default_output = root / "a_x264.mp4"
            same_stem_output = root / "a.mp4"
            custom_output = root / "custom.mp4"

            default_output.touch()
            same_stem_output.touch()

            args = Namespace(output=custom_output)
            self.assertFalse(workflow.has_processed_output(mkv, args))

            custom_output.touch()
            self.assertTrue(workflow.has_processed_output(mkv, args))

    def test_output_flag_rejected_for_multiple_inputs(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            (root / "a.mkv").touch()
            (root / "b.mkv").touch()

            with mock.patch.object(workflow, "process_one"):
                with mock.patch("sys.stderr"):
                    exit_code = workflow.main([str(root), "-o", str(root / "out.mp4")])

            self.assertEqual(exit_code, 1)

    def test_main_skips_already_processed_and_counts(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            (root / "a.mkv").touch()
            (root / "b.mkv").touch()
            (root / "a.mp4").touch()

            processed: list[Path] = []

            def process_one(_args: Namespace, mkv: Path) -> dict[str, object]:
                processed.append(mkv)
                return {"mkv": mkv, "audio": None, "subtitle": {}}

            with mock.patch.object(
                workflow,
                "process_one",
                side_effect=process_one,
            ):
                with mock.patch("sys.stdout") as stdout:
                    exit_code = workflow.main([str(root)])

            self.assertEqual(exit_code, 0)
            self.assertEqual([path.name for path in processed], ["b.mkv"])
            output = "".join(c[0][0] for c in stdout.write.call_args_list)
            self.assertIn("跳过 1 个已处理", output)
            self.assertIn("成功 1 个", output)
            self.assertIn("失败 0 个", output)

    def test_main_overwrite_processes_same_stem_output(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            mkv = root / "a.mkv"
            mkv.touch()
            mkv.with_suffix(".mp4").touch()
            processed: list[Path] = []

            def process_one(_args: Namespace, input_mkv: Path) -> dict[str, object]:
                processed.append(input_mkv)
                return {"mkv": input_mkv, "audio": None, "subtitle": {}}

            with (
                mock.patch.object(
                    workflow,
                    "process_one",
                    side_effect=process_one,
                ),
                mock.patch("sys.stdout"),
            ):
                exit_code = workflow.main([str(mkv), "--overwrite"])

            self.assertEqual(exit_code, 0)
            self.assertEqual(processed, [mkv.resolve()])

    def test_main_continues_after_each_supported_processing_error(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            names = ["a.mkv", "b.mkv", "c.mkv", "d.mkv"]
            for name in names:
                (root / name).touch()

            processed: list[str] = []

            def process(_args: Namespace, mkv: Path) -> dict[str, object]:
                processed.append(mkv.name)
                if mkv.name == "a.mkv":
                    raise workflow.MkvToMp4Error("没有字幕轨")
                if mkv.name == "b.mkv":
                    raise extract.ExtractSubtitleError("字幕无法提取")
                if mkv.name == "c.mkv":
                    raise OSError("网络文件不可读")
                return {"mkv": mkv, "audio": None, "subtitle": {}}

            with mock.patch.object(workflow, "process_one", side_effect=process):
                with mock.patch("sys.stdout") as stdout, mock.patch("sys.stderr") as stderr:
                    exit_code = workflow.main([str(root)])

            self.assertEqual(exit_code, 1)
            self.assertEqual(processed, names)
            output = "".join(c[0][0] for c in stdout.write.call_args_list)
            errors = "".join(c[0][0] for c in stderr.write.call_args_list)
            self.assertIn("成功 1 个", output)
            self.assertIn("失败 3 个", output)
            self.assertIn("a.mkv", errors)
            self.assertIn("没有字幕轨", errors)
            self.assertIn("字幕提取错误：字幕无法提取", errors)
            self.assertIn("系统错误：网络文件不可读", errors)

    def test_main_keyboard_interrupt_stops_the_batch(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            (root / "a.mkv").touch()
            (root / "b.mkv").touch()

            processed: list[str] = []

            def process(_args: Namespace, mkv: Path) -> None:
                processed.append(mkv.name)
                raise KeyboardInterrupt

            with mock.patch.object(workflow, "process_one", side_effect=process):
                with mock.patch("sys.stderr") as stderr:
                    exit_code = workflow.main([str(root)])

            self.assertEqual(exit_code, 130)
            self.assertEqual(processed, ["a.mkv"])
            summary = "".join(c[0][0] for c in stderr.write.call_args_list)
            self.assertIn("批次中断汇总：共 2 个文件", summary)
            self.assertIn("当前被中断 1 个", summary)
            self.assertIn("尚未开始 1 个", summary)
            self.assertIn(str((root / "a.mkv").resolve()), summary)
            self.assertIn(str((root / "b.mkv").resolve()), summary)

    def test_interrupted_batch_summary_covers_every_file_status(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            for name in ("a.mkv", "b.mkv", "c.mkv", "d.mkv", "e.mkv"):
                (root / name).touch()
            (root / "b.mp4").touch()

            def process(_args: Namespace, mkv: Path) -> dict[str, object]:
                if mkv.name == "c.mkv":
                    raise workflow.MkvToMp4Error("simulated failure")
                if mkv.name == "d.mkv":
                    raise KeyboardInterrupt
                return {"mkv": mkv, "audio": None, "subtitle": {}}

            with (
                mock.patch.object(workflow, "process_one", side_effect=process),
                mock.patch("sys.stdout"),
                mock.patch("sys.stderr") as stderr,
            ):
                exit_code = workflow.main([str(root)])

            self.assertEqual(exit_code, 130)
            summary = "".join(c[0][0] for c in stderr.write.call_args_list)
            self.assertIn("本次已完成 1 个", summary)
            self.assertIn("已有结果，已跳过 1 个", summary)
            self.assertIn("处理失败 1 个", summary)
            self.assertIn("当前被中断 1 个", summary)
            self.assertIn("尚未开始 1 个", summary)
            summary = summary[summary.index("批次中断汇总：") :]
            for name in ("a.mkv", "b.mkv", "c.mkv", "d.mkv", "e.mkv"):
                self.assertEqual(summary.count(str((root / name).resolve())), 1)

    def test_single_file_interrupt_does_not_print_batch_summary(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            mkv = Path(temp_dir) / "single.mkv"
            mkv.touch()

            with (
                mock.patch.object(workflow, "process_one", side_effect=KeyboardInterrupt),
                mock.patch("sys.stdout"),
                mock.patch("sys.stderr") as stderr,
            ):
                exit_code = workflow.main([str(mkv)])

            self.assertEqual(exit_code, 130)
            output = "".join(c[0][0] for c in stderr.write.call_args_list)
            self.assertIn("已取消", output)
            self.assertNotIn("批次中断汇总", output)


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

        def burn_main(arguments: list[str], *, result: list | None = None) -> int:
            nonlocal burned_arguments, burned_content
            burned_arguments = arguments
            burned_content = Path(arguments[1]).read_bytes()
            if result is not None:
                result.append(None)
            return 0

        args = Namespace(
            inputs=[mkv],
            output=output,
            crf=24.0,
            audio_bitrate=128,
            audio_language="jpn",
            keyint=None,
            fallback_ffmpeg=None,
            overwrite=False,
            no_export_subtitle=False,
            keep_media_temp=False,
            dry_run=False,
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
            mock.patch.object(
                workflow,
                "prepare_font_directory",
                return_value=None,
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
            audio_language_index = burned_arguments.index("--audio-language")
            self.assertEqual(
                burned_arguments[audio_language_index + 1],
                "jpn",
            )
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
