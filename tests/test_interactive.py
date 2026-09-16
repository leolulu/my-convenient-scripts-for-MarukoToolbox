from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import burn_subtitles as burn
import extract_english_subtitle as extract
import mkv_to_mp4_with_english_subtitle as workflow


SUBTITLES = [
    {
        "id": 2,
        "type": "subtitles",
        "codec": "SubRip/SRT",
        "properties": {
            "language": "jpn",
            "track_name": "Japanese Full",
            "codec_id": "S_TEXT/UTF8",
            "default_track": False,
            "forced_track": False,
        },
    },
    {
        "id": 3,
        "type": "subtitles",
        "codec": "PGS",
        "properties": {
            "language": "eng",
            "track_name": "English Signs",
            "codec_id": "S_HDMV/PGS",
            "default_track": True,
            "forced_track": True,
        },
    },
    {
        "id": 4,
        "type": "subtitles",
        "codec": "Unknown",
        "properties": {"language": "und", "codec_id": "S_UNKNOWN"},
    },
]
AUDIO = [
    burn.AudioStream(1, "jpn", True),
    burn.AudioStream(2, "eng", False),
]
AUDIO_TRACKS = [
    {"id": 0, "type": "audio", "properties": {"number": 2, "track_name": "Main"}},
    {"id": 1, "type": "audio", "properties": {"number": 3, "track_name": "Commentary"}},
]


def probe_with_details(_mkv: Path, *, audio_details: dict) -> tuple:
    audio_details.update(
        {
            1: burn.AudioStreamDetails(2, "aac", "stereo"),
            2: burn.AudioStreamDetails(3, "ac3", "5.1(side)"),
        }
    )
    return (24.0, True, True, None, AUDIO)


class InteractiveSelectionTests(unittest.TestCase):
    def select(self, answers: list[str], tracks: list[dict] | None = None):
        output = io.StringIO()
        with (
            mock.patch.object(extract, "identify_tracks", return_value=tracks or SUBTITLES + AUDIO_TRACKS),
            mock.patch.object(burn, "probe_video", side_effect=probe_with_details),
            mock.patch("builtins.input", side_effect=answers),
            redirect_stdout(output),
        ):
            selected = workflow.select_interactive_tracks(Path("sample.mkv"), Path("sample_x264.mp4"))
        return selected, output.getvalue()

    def test_menu_allows_non_english_subtitle_and_specific_audio(self) -> None:
        (subtitle, audio), output = self.select(["1", "2", "y"])

        self.assertEqual(subtitle["id"], 2)
        self.assertEqual(audio, AUDIO[1])
        self.assertIn("Japanese Full", output)
        self.assertIn("不可选：当前脚本不支持提取", output)
        self.assertNotIn("烧录兼容性未验证", output)
        self.assertIn("Commentary", output)
        self.assertIn("编码=ac3 | 声道=5.1(side)", output)
        self.assertIn("输出：sample_x264.mp4", output)

    def test_menu_allows_pgs_without_compatibility_warning(self) -> None:
        (subtitle, audio), output = self.select(["2", "1", "y"])

        self.assertEqual(subtitle["id"], 3)
        self.assertEqual(audio, AUDIO[0])
        self.assertNotIn("烧录兼容性未验证", output)

    def test_invalid_or_disabled_selection_reprompts_then_can_reselect(self) -> None:
        (subtitle, audio), output = self.select(["3", "", "2", "1", "r", "1", "2", "y"])

        self.assertEqual(subtitle["id"], 2)
        self.assertEqual(audio, AUDIO[1])
        self.assertEqual(output.count("输入无效或该轨道不可选"), 2)
        self.assertEqual(output.count("内嵌字幕轨：共 3 条"), 2)

    def test_q_skips_at_selection_or_confirmation(self) -> None:
        for answers in (["q"], ["1", "q"], ["1", "1", "q"]):
            with self.subTest(answers=answers):
                with self.assertRaises(workflow.SkipCurrentFile):
                    self.select(answers)

    def test_missing_or_unparseable_tracks_fail_before_prompt(self) -> None:
        with (
            mock.patch.object(extract, "identify_tracks", return_value=AUDIO_TRACKS),
            mock.patch("builtins.input") as prompt,
        ):
            with self.assertRaisesRegex(workflow.MkvToMp4Error, "没有字幕轨"):
                workflow.select_interactive_tracks(Path("sample.mkv"), Path("out.mp4"))
            prompt.assert_not_called()

        with (
            mock.patch.object(extract, "identify_tracks", return_value=SUBTITLES + AUDIO_TRACKS),
            mock.patch.object(burn, "probe_video", return_value=(24.0, False, True, None, [])),
            mock.patch("builtins.input") as prompt,
        ):
            with self.assertRaisesRegex(workflow.MkvToMp4Error, "不包含音频流"):
                workflow.select_interactive_tracks(Path("sample.mkv"), Path("out.mp4"))
            prompt.assert_not_called()

        with (
            mock.patch.object(extract, "identify_tracks", return_value=SUBTITLES + AUDIO_TRACKS),
            mock.patch.object(burn, "probe_video", return_value=(24.0, True, True, None, [])),
            mock.patch("builtins.input") as prompt,
        ):
            with self.assertRaisesRegex(workflow.MkvToMp4Error, "无法完整识别音轨"):
                workflow.select_interactive_tracks(Path("sample.mkv"), Path("out.mp4"))
            prompt.assert_not_called()

    def test_audio_name_is_unknown_without_reliable_track_number(self) -> None:
        def probe(_mkv: Path, *, audio_details: dict) -> tuple:
            audio_details[1] = burn.AudioStreamDetails(None, "aac", "stereo")
            return (24.0, True, True, None, [AUDIO[0]])

        output = io.StringIO()
        with (
            mock.patch.object(extract, "identify_tracks", return_value=SUBTITLES + AUDIO_TRACKS[:1]),
            mock.patch.object(burn, "probe_video", side_effect=probe),
            mock.patch("builtins.input", side_effect=["1", "1", "y"]),
            redirect_stdout(output),
        ):
            workflow.select_interactive_tracks(Path("sample.mkv"), Path("out.mp4"))
        self.assertIn("流 0:1 | 语言=jpn | 名称=未知", output.getvalue())

    def test_audio_name_uses_its_own_ffmpeg_metadata(self) -> None:
        def probe(_mkv: Path, *, audio_details: dict) -> tuple:
            audio_details[1] = burn.AudioStreamDetails(None, "aac", "stereo", "Director")
            return (24.0, True, True, None, [AUDIO[0]])

        output = io.StringIO()
        with (
            mock.patch.object(extract, "identify_tracks", return_value=SUBTITLES + AUDIO_TRACKS[:1]),
            mock.patch.object(burn, "probe_video", side_effect=probe),
            mock.patch("builtins.input", side_effect=["1", "1", "y"]),
            redirect_stdout(output),
        ):
            workflow.select_interactive_tracks(Path("sample.mkv"), Path("out.mp4"))
        self.assertIn("流 0:1 | 语言=jpn | 名称=Director", output.getvalue())


class InteractiveCommandTests(unittest.TestCase):
    def test_parser_tracks_explicit_audio_language_only(self) -> None:
        parser = workflow.build_parser()
        implicit = parser.parse_args(["sample.mkv", "--interactive"])
        short = parser.parse_args(["sample.mkv", "-i"])
        explicit = parser.parse_args(["sample.mkv", "--interactive", "--audio-language=eng"])

        self.assertEqual(implicit.audio_language, "jpn")
        self.assertTrue(short.interactive)
        self.assertEqual(short, implicit)
        self.assertFalse(getattr(implicit, "audio_language_explicit", False))
        self.assertEqual(explicit.audio_language, "eng")
        self.assertTrue(explicit.audio_language_explicit)

    def test_conflicts_and_noninteractive_stdin_fail_before_processing(self) -> None:
        cases = [
            (["sample.mkv", "--interactive", "--dry-run"], "--dry-run"),
            (["sample.mkv", "--interactive", "--audio-language", "jpn"], "--audio-language"),
            (["sample.mkv", "--interactive"], "可交互"),
        ]
        for argv, message in cases:
            with self.subTest(argv=argv):
                error_output = io.StringIO()
                with (
                    mock.patch.object(workflow, "expand_inputs") as expand,
                    mock.patch.object(workflow.sys, "stdin", mock.Mock(isatty=lambda: False)),
                    redirect_stderr(error_output),
                ):
                    self.assertEqual(workflow.main(argv), 1)
                self.assertIn(message, error_output.getvalue())
                expand.assert_not_called()

    def test_manual_skip_is_separate_and_successful_batch_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mkvs = [Path(directory) / "a.mkv", Path(directory) / "b.mkv"]
            for mkv in mkvs:
                mkv.touch()
            output = io.StringIO()
            selection = workflow.InteractiveSelection(SUBTITLES[0], AUDIO[0])
            with (
                mock.patch.object(workflow, "expand_inputs", return_value=mkvs),
                mock.patch.object(workflow, "has_processed_output", return_value=False),
                mock.patch.object(workflow, "prepare_interactive_selection", side_effect=[
                    workflow.SkipCurrentFile,
                    selection,
                ]),
                mock.patch.object(workflow, "process_one", side_effect=[
                    {"mkv": mkvs[1], "audio": AUDIO[0], "subtitle": SUBTITLES[0]},
                ]),
                mock.patch.object(workflow.sys, "stdin", mock.Mock(isatty=lambda: True)),
                redirect_stdout(output),
            ):
                exit_code = workflow.main([directory, "--interactive"])

        self.assertEqual(exit_code, 0)
        self.assertIn("用户主动跳过 1 个", output.getvalue())
        self.assertIn("成功 1 个，失败 0 个", output.getvalue())
        self.assertIn("用户主动跳过明细", output.getvalue())

    def test_existing_output_skips_without_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mkv = Path(directory) / "sample.mkv"
            mkv.touch()
            output = io.StringIO()
            with (
                mock.patch.object(workflow, "has_processed_output", return_value=True),
                mock.patch.object(workflow, "prepare_interactive_selection") as select,
                mock.patch.object(workflow, "process_one") as process,
                mock.patch.object(workflow.sys, "stdin", mock.Mock(isatty=lambda: True)),
                redirect_stdout(output),
            ):
                self.assertEqual(workflow.main([str(mkv), "--interactive"]), 0)
            select.assert_not_called()
            process.assert_not_called()
            self.assertIn("跳过 1 个已处理", output.getvalue())

    def test_eof_aborts_batch_with_interrupted_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mkvs = [Path(directory) / "a.mkv", Path(directory) / "b.mkv"]
            error_output = io.StringIO()
            with (
                mock.patch.object(workflow, "expand_inputs", return_value=mkvs),
                mock.patch.object(workflow, "has_processed_output", return_value=False),
                mock.patch.object(
                    workflow,
                    "prepare_interactive_selection",
                    side_effect=lambda *_: workflow.read_interactive_input("选轨："),
                ),
                mock.patch.object(workflow, "process_one") as process,
                mock.patch("builtins.input", side_effect=EOFError),
                mock.patch.object(workflow.sys, "stdin", mock.Mock(isatty=lambda: True)),
                redirect_stdout(io.StringIO()),
                redirect_stderr(error_output),
            ):
                exit_code = workflow.main([directory, "--interactive"])
        self.assertEqual(exit_code, 130)
        process.assert_not_called()
        self.assertIn("当前被中断 1 个", error_output.getvalue())
        self.assertIn("尚未开始 1 个", error_output.getvalue())

    def test_all_files_are_selected_before_first_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mkvs = [Path(directory) / "a.mkv", Path(directory) / "b.mkv"]
            for mkv in mkvs:
                mkv.touch()
            events: list[str] = []

            def select(_args, mkv):
                events.append(f"select:{mkv.name}")
                return workflow.InteractiveSelection(SUBTITLES[0], AUDIO[0])

            def process(_args, mkv, selection):
                self.assertEqual(selection.audio, AUDIO[0])
                events.append(f"convert:{mkv.name}")
                return {"mkv": mkv, "audio": AUDIO[0], "subtitle": SUBTITLES[0]}

            with (
                mock.patch.object(workflow, "expand_inputs", return_value=mkvs),
                mock.patch.object(workflow, "has_processed_output", return_value=False),
                mock.patch.object(workflow, "prepare_interactive_selection", side_effect=select),
                mock.patch.object(workflow, "process_one", side_effect=process),
                mock.patch.object(workflow.sys, "stdin", mock.Mock(isatty=lambda: True)),
                redirect_stdout(io.StringIO()),
            ):
                exit_code = workflow.main([directory, "--interactive"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            events,
            ["select:a.mkv", "select:b.mkv", "convert:a.mkv", "convert:b.mkv"],
        )

    def test_process_one_passes_exact_manual_audio_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mkv = Path(directory) / "sample.mkv"
            output = Path(directory) / "sample_x264.mp4"
            temp_subtitle = Path(directory) / "temp.srt"
            args = argparse.Namespace(
                interactive=True, output=None, overwrite=False,
                no_export_subtitle=True, crf=24.0, audio_bitrate=128,
                audio_language="jpn", keyint=None, fallback_ffmpeg=None,
                keep_media_temp=False,
            )

            def burn_main(_argv, *, result, selected_audio_stream):
                self.assertEqual(selected_audio_stream, AUDIO[1])
                result.append(selected_audio_stream)
                return 0

            with (
                mock.patch.object(extract, "validate_binaries"),
                mock.patch.object(burn, "validate_binaries"),
                mock.patch.object(burn, "resolve_output", return_value=output),
                mock.patch.object(workflow, "prepare_subtitle_outputs", return_value=(temp_subtitle, None, [temp_subtitle])),
                mock.patch.object(extract, "extract_subtitle"),
                mock.patch.object(workflow, "prepare_font_directory", return_value=None),
                mock.patch.object(burn, "main", side_effect=burn_main),
                mock.patch.object(extract, "remove_outputs"),
                redirect_stdout(io.StringIO()),
            ):
                record = workflow.process_one(
                    args,
                    mkv,
                    workflow.InteractiveSelection(SUBTITLES[0], AUDIO[1]),
                )

        self.assertEqual(record["audio"], AUDIO[1])


class BurnManualAudioTests(unittest.TestCase):
    def test_probe_details_and_manual_stream_validation(self) -> None:
        probe_text = (
            "Stream #0:0: Video: h264, yuv420p, 24 fps\n"
            "Stream #0:1[0x2](jpn): Audio: aac (LC), 48000 Hz, stereo (default)\n"
            "    Metadata:\n"
            "      title           : Main\n"
            "Stream #0:2[0x3](eng): Audio: ac3, 48000 Hz, 5.1(side)\n"
            "    Metadata:\n"
            "      title           : Commentary\n"
        )
        details = burn.parse_audio_stream_details(probe_text)
        self.assertEqual(details[1], burn.AudioStreamDetails(2, "aac (LC)", "stereo (default)", "Main"))
        self.assertEqual(details[2], burn.AudioStreamDetails(3, "ac3", "5.1(side)", "Commentary"))

        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "video.mkv"
            subtitle = Path(directory) / "sub.srt"
            video.touch()
            subtitle.touch()
            with (
                mock.patch.object(burn, "validate_binaries"),
                mock.patch.object(burn, "probe_video", return_value=(24.0, True, True, None, AUDIO)),
                mock.patch.object(burn, "prepare_ffmpeg_fonts_dir", return_value=(None, None)),
                redirect_stderr(io.StringIO()) as errors,
            ):
                exit_code = burn.main(
                    [str(video), str(subtitle)],
                    selected_audio_stream=burn.AudioStream(7, "eng", False),
                )
        self.assertEqual(exit_code, 1)
        self.assertIn("压制前复检", errors.getvalue())

    def test_manual_stream_reaches_audio_encoder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "video.mkv"
            subtitle = root / "sub.srt"
            video.touch()
            subtitle.touch()
            result = []
            output = io.StringIO()
            with (
                mock.patch.object(burn, "validate_binaries"),
                mock.patch.object(burn, "probe_video", return_value=(
                    24.0, True, True, burn.VideoColorInfo(None, None, None, None), AUDIO,
                )),
                mock.patch.object(burn, "prepare_ffmpeg_fonts_dir", return_value=(None, None)),
                mock.patch.object(burn, "encode_audio") as encode_audio,
                mock.patch.object(burn, "encode_video"),
                mock.patch.object(burn, "mux_mp4"),
                mock.patch.object(burn, "TEMP_DIR", root / "temp"),
                redirect_stdout(output),
            ):
                exit_code = burn.main(
                    [str(video), str(subtitle)],
                    result=result,
                    selected_audio_stream=AUDIO[1],
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(result, [AUDIO[1]])
        self.assertEqual(encode_audio.call_args.args[-1], AUDIO[1])
        self.assertIn("手动选择流 0:2", output.getvalue())
        self.assertNotIn("目标音轨语言", output.getvalue())


if __name__ == "__main__":
    unittest.main()
