from __future__ import annotations

import io
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import burn_subtitles as burn
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
BT709_LIMITED = burn.VideoColorInfo(
    color_range="tv",
    color_primaries="bt709",
    transfer_characteristics="bt709",
    matrix_coefficients="bt709",
)


class FakeProcess:
    def __init__(
        self,
        *,
        stdout: io.BytesIO | None = None,
        stdin: io.BytesIO | None = None,
        returncode: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stdin = stdin
        self.returncode = returncode
        self.terminated = False

    def wait(self) -> int:
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True


class FontAttachmentTests(unittest.TestCase):
    def test_font_detection_uses_extension_and_content_type(self) -> None:
        attachments = [
            {"id": 1, "file_name": "font.ttf", "content_type": "application/octet-stream"},
            {"id": 2, "file_name": "font.bin", "content_type": "font/otf"},
            {"id": 3, "file_name": "cover.jpg", "content_type": "image/jpeg"},
        ]

        selected = extract.get_font_attachments(attachments)

        self.assertEqual([attachment["id"] for attachment in selected], [1, 2])

    def test_extract_fonts_uses_safe_unique_output_names(self) -> None:
        attachments = [
            {"id": 7, "file_name": "../Bolton.ttf", "content_type": "font/ttf"},
            {"id": 8, "file_name": "Bolton.ttf", "content_type": "font/ttf"},
            {"id": 9, "file_name": "cover.jpg", "content_type": "image/jpeg"},
        ]

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            mkv = root / "sample.mkv"
            output_dir = root / "fonts"
            mkv.touch()

            def run_command(command: list[object], **_kwargs: object) -> SimpleNamespace:
                for specification in command[3:]:
                    output = Path(str(specification).split(":", 1)[1])
                    output.write_bytes(b"font")
                return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

            with mock.patch.object(
                extract.subprocess,
                "run",
                side_effect=run_command,
            ) as run:
                outputs = extract.extract_font_attachments(
                    mkv,
                    attachments,
                    output_dir,
                )

            self.assertEqual(
                [output.name for output in outputs],
                ["7_Bolton.ttf", "8_Bolton.ttf"],
            )
            self.assertTrue(all(output.is_file() for output in outputs))
            command = run.call_args.args[0]
            self.assertEqual(command[:3], [extract.MKVEXTRACT, "attachments", mkv])


class BurnFontsPipelineTests(unittest.TestCase):
    def test_mediainfo_values_are_mapped_to_x264_values(self) -> None:
        self.assertEqual(
            burn.map_mediainfo_color_value(
                "Limited",
                burn.COLOR_RANGE_MAP,
                "色彩范围",
            ),
            "tv",
        )
        self.assertEqual(
            burn.map_mediainfo_color_value(
                "BT.2020 non-constant",
                burn.MATRIX_COEFFICIENTS_MAP,
                "矩阵系数",
            ),
            "bt2020nc",
        )
        self.assertEqual(
            burn.map_mediainfo_color_value(
                "SMPTE ST 2084",
                burn.TRANSFER_CHARACTERISTICS_MAP,
                "传递特性",
            ),
            "smpte2084",
        )

    def test_unknown_mediainfo_value_stays_unspecified(self) -> None:
        with mock.patch("sys.stderr"):
            mapped = burn.map_mediainfo_color_value(
                "Unsupported value",
                burn.COLOR_PRIMARIES_MAP,
                "色彩原色",
            )

        self.assertIsNone(mapped)

    def test_x264_color_options_include_only_known_source_values(self) -> None:
        color_info = burn.VideoColorInfo(
            color_range="pc",
            color_primaries=None,
            transfer_characteristics="iec61966-2-1",
            matrix_coefficients="bt709",
        )

        self.assertEqual(
            burn.build_x264_color_options(color_info),
            [
                "--range",
                "pc",
                "--transfer",
                "iec61966-2-1",
                "--colormatrix",
                "bt709",
            ],
        )

    def test_mediainfo_load_failure_is_reported(self) -> None:
        with mock.patch.object(
            burn.ctypes,
            "WinDLL",
            side_effect=OSError("missing"),
        ):
            with self.assertRaisesRegex(
                burn.BurnSubtitlesError,
                "无法加载 MediaInfo",
            ):
                burn.probe_video_color(Path("sample.mkv"))

    def test_non_ascii_fonts_dir_is_staged_with_ascii_names(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "字体"
            source_dir.mkdir()
            (source_dir / "字体文件.ttf").write_bytes(b"font")
            temp_output = root / "temp"

            with mock.patch.object(burn, "TEMP_DIR", temp_output):
                fonts_dir, staged_dir = burn.prepare_ffmpeg_fonts_dir(source_dir)

            self.assertIsNotNone(staged_dir)
            assert fonts_dir is not None
            assert staged_dir is not None
            self.assertTrue(str(fonts_dir).isascii())
            self.assertEqual(
                [path.name for path in fonts_dir.iterdir()],
                ["0001.ttf"],
            )
            self.assertEqual((fonts_dir / "0001.ttf").read_bytes(), b"font")
            burn.remove_directory(staged_dir)
            self.assertFalse(staged_dir.exists())

    def test_ass_with_fonts_uses_bundled_ffmpeg_and_x264_y4m(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            video = root / "sample.mkv"
            subtitle = root / "sample.ass"
            fonts_dir = root / "fonts"
            output = root / "video.mp4"
            video.touch()
            subtitle.touch()
            fonts_dir.mkdir()
            (fonts_dir / "font.ttf").write_bytes(b"font")

            decoder_process = FakeProcess(
                stdout=io.BytesIO(
                    b"YUV4MPEG2 W16 H16 F24:1 Ip A1:1 C420jpeg\nFRAME\n"
                )
            )
            x264_process = FakeProcess(stdin=io.BytesIO())

            with mock.patch.object(
                burn.subprocess,
                "Popen",
                side_effect=[decoder_process, x264_process],
            ) as popen:
                burn.encode_video(
                    video,
                    subtitle,
                    output,
                    24.0,
                    240,
                    None,
                    fonts_dir,
                    BT709_LIMITED,
                )

            decoder_command = popen.call_args_list[0].args[0]
            x264_command = popen.call_args_list[1].args[0]
            self.assertEqual(decoder_command[0], burn.FFMPEG)
            self.assertIn("-copyts", decoder_command)
            self.assertIn("-start_at_zero", decoder_command)
            self.assertIn("-vf", decoder_command)
            subtitle_filter = decoder_command[decoder_command.index("-vf") + 1]
            self.assertIn("subtitles=", subtitle_filter)
            self.assertIn("fontsdir=", subtitle_filter)
            self.assertIn("C\\:/", subtitle_filter)
            self.assertNotIn("C\\\\:/", subtitle_filter)
            self.assertNotIn("--vf", x264_command)
            self.assertNotIn("--sub", x264_command)
            self.assertIn("--range", x264_command)
            self.assertEqual(
                x264_command[x264_command.index("--range") + 1],
                "tv",
            )
            self.assertIn("--colorprim", x264_command)
            self.assertIn("--transfer", x264_command)
            self.assertIn("--colormatrix", x264_command)
            self.assertEqual(x264_command[-3:], ["--demuxer", "y4m", "-"])

    def test_srt_without_fonts_keeps_existing_x264_path(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            video = root / "sample.mkv"
            subtitle = root / "sample.srt"
            output = root / "video.mp4"

            with mock.patch.object(burn, "run_command") as run:
                burn.encode_video(
                    video,
                    subtitle,
                    output,
                    24.0,
                    240,
                    None,
                )

            command = run.call_args.args[0]
            self.assertIn("--vf", command)
            self.assertIn("--sub", command)
            self.assertNotIn("--demuxer", command)
            self.assertNotIn("--range", command)
            self.assertNotIn("--colorprim", command)
            self.assertEqual(command[-1], video)


class WorkflowFontCleanupTests(unittest.TestCase):
    def test_font_directory_is_removed_when_burn_fails(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            mkv = root / "sample.mkv"
            subtitle = root / "raw.ass"
            output = root / "sample.mp4"
            fonts_dir = root / "fonts"
            mkv.touch()
            fonts_dir.mkdir()
            (fonts_dir / "font.ttf").write_bytes(b"font")

            args = Namespace(
                inputs=[mkv],
                output=output,
                crf=24.0,
                audio_bitrate=128,
                keyint=None,
                fallback_ffmpeg=None,
                overwrite=False,
                no_export_subtitle=True,
                keep_media_temp=False,
            )

            def extract_subtitle(
                _mkv: Path,
                _track: dict,
                target: Path,
            ) -> None:
                target.write_bytes(b"[Events]\n")

            with (
                mock.patch.object(workflow, "build_parser") as build_parser,
                mock.patch.object(extract, "validate_binaries"),
                mock.patch.object(extract, "resolve_input", return_value=mkv),
                mock.patch.object(
                    workflow,
                    "select_english_subtitle",
                    return_value=ASS_TRACK,
                ),
                mock.patch.object(burn, "validate_binaries"),
                mock.patch.object(burn, "resolve_output", return_value=output),
                mock.patch.object(
                    workflow,
                    "prepare_subtitle_outputs",
                    return_value=(subtitle, None, [subtitle]),
                ),
                mock.patch.object(
                    extract,
                    "extract_subtitle",
                    side_effect=extract_subtitle,
                ),
                mock.patch.object(
                    workflow,
                    "prepare_font_directory",
                    return_value=fonts_dir,
                ),
                mock.patch.object(burn, "main", return_value=1),
                mock.patch("sys.stderr"),
            ):
                build_parser.return_value.parse_args.return_value = args
                exit_code = workflow.main([])

            self.assertEqual(exit_code, 1)
            self.assertFalse(fonts_dir.exists())
            self.assertFalse(subtitle.exists())

    def test_build_burn_arguments_passes_fonts_directory(self) -> None:
        args = Namespace(
            crf=24.0,
            audio_bitrate=128,
            keyint=None,
            fallback_ffmpeg=None,
            overwrite=False,
            keep_media_temp=False,
        )

        arguments = workflow.build_burn_arguments(
            args,
            Path("sample.mkv"),
            Path("sample.ass"),
            Path("sample.mp4"),
            Path("fonts"),
        )

        self.assertIn("--fonts-dir", arguments)
        self.assertEqual(arguments[arguments.index("--fonts-dir") + 1], "fonts")


if __name__ == "__main__":
    unittest.main()
