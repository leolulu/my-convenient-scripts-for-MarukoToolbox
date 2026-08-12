import argparse
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

import mkv_to_mp4_with_english_subtitle as workflow


def _subtitle_track(track_id: int, language: str) -> dict:
    return {
        "id": track_id,
        "type": "subtitles",
        "codec": "SubRip/SRT",
        "properties": {"language": language},
    }


def _audio_stream(index: int, language: str, default: bool = False):
    return workflow.burn.AudioStream(index=index, language=language, default=default)


def _result(
    subtitle_line: str = "ID 2 eng（英语）",
    audio_line: str = "0:1 jpn（日语）",
    subtitle_status: str = "selected",
    audio_status: str = "selected",
    failure_reasons: tuple[str, ...] = (),
) -> workflow.DryRunResult:
    return workflow.DryRunResult(
        subtitle_line=subtitle_line,
        audio_line=audio_line,
        subtitle_status=subtitle_status,
        audio_status=audio_status,
        failure_reasons=failure_reasons,
    )


class DryRunOneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.args = argparse.Namespace(audio_language="jpn")
        self.mkv = Path("sample.mkv")

    def test_subtitle_hit_and_audio_hit(self) -> None:
        tracks = [
            _subtitle_track(2, "eng"),
            _subtitle_track(1, "jpn"),
        ]
        with (
            mock.patch.object(workflow.extract, "identify_tracks", return_value=tracks),
            mock.patch.object(workflow.burn, "probe_video", return_value=(None, True, True, None, [_audio_stream(1, "jpn")])),
        ):
            result = workflow.dry_run_one(
                self.args,
                self.mkv,
            )

        self.assertEqual(result.subtitle_line, "ID 2 eng（英语）")
        self.assertEqual(result.audio_line, "0:1 jpn（日语）")
        self.assertEqual(result.subtitle_status, "selected")
        self.assertEqual(result.audio_status, "selected")
        self.assertFalse(result.failed)

    def test_subtitle_miss_lists_existing_languages(self) -> None:
        tracks = [_subtitle_track(1, "jpn"), _subtitle_track(3, "chs")]
        with (
            mock.patch.object(workflow.extract, "identify_tracks", return_value=tracks),
            mock.patch.object(workflow.burn, "probe_video", return_value=(None, True, True, None, [_audio_stream(1, "jpn")])),
        ):
            result = workflow.dry_run_one(
                self.args,
                self.mkv,
            )

        self.assertEqual(result.subtitle_line, "无 enm/eng（现有：chs、jpn）")
        self.assertEqual(result.subtitle_status, "no_match")
        self.assertFalse(result.failed)

    def test_subtitle_no_tracks_reports_no_subtitle_stream(self) -> None:
        with (
            mock.patch.object(workflow.extract, "identify_tracks", return_value=[]),
            mock.patch.object(workflow.burn, "probe_video", return_value=(None, True, True, None, [_audio_stream(1, "jpn")])),
        ):
            result = workflow.dry_run_one(
                self.args,
                self.mkv,
            )

        self.assertEqual(result.subtitle_line, "无字幕轨")
        self.assertEqual(result.subtitle_status, "no_track")
        self.assertFalse(result.failed)

    def test_audio_miss_reports_ffmpeg_fallback(self) -> None:
        tracks = [_subtitle_track(2, "eng")]
        with (
            mock.patch.object(workflow.extract, "identify_tracks", return_value=tracks),
            mock.patch.object(workflow.burn, "probe_video", return_value=(None, True, True, None, [_audio_stream(1, "eng"), _audio_stream(2, "chi")])),
        ):
            result = workflow.dry_run_one(
                self.args,
                self.mkv,
            )

        self.assertEqual(
            result.audio_line,
            "无 jpn（现有：eng、chi）→ ffmpeg 自动选",
        )
        self.assertEqual(result.audio_status, "fallback")
        self.assertFalse(result.failed)

    def test_audio_language_preference_is_respected(self) -> None:
        tracks = [_subtitle_track(2, "eng")]
        eng_args = argparse.Namespace(audio_language="eng")
        with (
            mock.patch.object(workflow.extract, "identify_tracks", return_value=tracks),
            mock.patch.object(
                workflow.burn,
                "probe_video",
                return_value=(None, True, True, None, [_audio_stream(1, "jpn"), _audio_stream(2, "eng")]),
            ),
        ):
            result = workflow.dry_run_one(
                eng_args,
                self.mkv,
            )

        self.assertEqual(result.audio_line, "0:2 eng（英语）")
        self.assertEqual(result.audio_status, "selected")
        self.assertFalse(result.failed)

    def test_missing_ffmpeg_reports_undetected(self) -> None:
        tracks = [_subtitle_track(2, "eng")]
        with (
            mock.patch.object(workflow.extract, "identify_tracks", return_value=tracks),
            mock.patch.object(workflow.burn, "FFMPEG", Path("nonexistent.exe")),
        ):
            result = workflow.dry_run_one(
                self.args,
                self.mkv,
            )

        self.assertEqual(result.audio_line, "未探测（缺 ffmpeg）")
        self.assertEqual(result.audio_status, "unprobed")
        self.assertFalse(result.failed)

    def test_no_audio_stream_reports_truthfully(self) -> None:
        tracks = [_subtitle_track(2, "eng")]
        with (
            mock.patch.object(workflow.extract, "identify_tracks", return_value=tracks),
            mock.patch.object(
                workflow.burn,
                "probe_video",
                return_value=(None, False, True, None, []),
            ),
        ):
            result = workflow.dry_run_one(
                self.args,
                self.mkv,
            )

        self.assertEqual(result.audio_line, "无音频流")
        self.assertEqual(result.audio_status, "no_track")
        self.assertFalse(result.failed)


class DryRunMainTests(unittest.TestCase):
    def test_main_marks_same_stem_mp4_as_existing_output(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            mkv = root / "already.mkv"
            mkv.touch()
            mkv.with_suffix(".mp4").touch()

            with (
                mock.patch.object(
                    workflow,
                    "dry_run_one",
                    return_value=_result(),
                ),
                mock.patch("sys.stdout", new_callable=StringIO) as stdout,
            ):
                exit_code = workflow.main([str(mkv), "--dry-run"])

            self.assertEqual(exit_code, 0)
            self.assertIn(
                "\n[1] [已有处理结果] already.mkv\n",
                stdout.getvalue(),
            )

    def test_main_inspects_processed_file_and_marks_existing_output(self) -> None:
        mkvs = [Path("already.mkv"), Path("pending.mkv")]
        with (
            mock.patch.object(workflow, "expand_inputs", return_value=mkvs),
            mock.patch.object(
                workflow,
                "has_processed_output",
                side_effect=[True, False],
            ),
            mock.patch.object(
                workflow,
                "dry_run_one",
                return_value=_result(),
            ) as dry_run_one,
            mock.patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            exit_code = workflow.main(["input", "--dry-run"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(dry_run_one.call_count, 2)
        inspected = [call.args[1] for call in dry_run_one.call_args_list]
        self.assertEqual(inspected, mkvs)
        output = stdout.getvalue()
        self.assertIn("\n[1] [已有处理结果] already.mkv\n", output)
        self.assertIn("\n[2] pending.mkv\n", output)
        self.assertIn("    字幕：ID 2 eng（英语）\n", output)
        self.assertIn("    音轨：0:1 jpn（日语）\n", output)
        self.assertNotIn("\t", output)
        self.assertNotIn("已处理过，跳过", output)
        self.assertIn("预检汇总：共 2 个文件", output)
        self.assertIn("字幕和音轨均选中：2", output)
        self.assertIn("处理失败：0", output)
        self.assertEqual(output.count("already.mkv"), 1)
        self.assertEqual(output.count("pending.mkv"), 1)

    def test_main_attributes_audio_probe_failure_to_audio_column(self) -> None:
        tracks = [_subtitle_track(2, "eng")]
        with (
            mock.patch.object(
                workflow,
                "expand_inputs",
                return_value=[Path("broken.mkv")],
            ),
            mock.patch.object(
                workflow,
                "has_processed_output",
                return_value=False,
            ),
            mock.patch.object(workflow.extract, "identify_tracks", return_value=tracks),
            mock.patch.object(workflow.burn, "FFMPEG") as ffmpeg,
            mock.patch.object(
                workflow.burn,
                "probe_video",
                side_effect=workflow.burn.BurnSubtitlesError(
                    "ffmpeg 未检测到视频流"
                ),
            ),
            mock.patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            ffmpeg.is_file.return_value = True
            exit_code = workflow.main(["input", "--dry-run"])

        self.assertEqual(exit_code, 1)
        output = stdout.getvalue()
        self.assertIn("字幕：ID 2 eng（英语）", output)
        self.assertIn("音轨：探测失败（ffmpeg 未检测到视频流）", output)
        self.assertNotIn("字幕：读取失败", output)
        self.assertIn("字幕和音轨均选中：0", output)
        self.assertIn("仅字幕选中：0", output)
        self.assertIn("处理失败：1", output)
        self.assertEqual(output.count("broken.mkv"), 1)

    def test_main_counts_output_status_failure_and_still_inspects(self) -> None:
        with (
            mock.patch.object(
                workflow,
                "expand_inputs",
                return_value=[Path("status-error.mkv")],
            ),
            mock.patch.object(
                workflow,
                "has_processed_output",
                side_effect=OSError("无法读取输出目录"),
            ),
            mock.patch.object(
                workflow,
                "dry_run_one",
                return_value=_result(),
            ) as dry_run_one,
            mock.patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            exit_code = workflow.main(["input", "--dry-run"])

        self.assertEqual(exit_code, 1)
        dry_run_one.assert_called_once()
        output = stdout.getvalue()
        self.assertIn(
            "\n[1] [处理结果状态检查失败（系统错误：无法读取输出目录）] "
            "status-error.mkv\n",
            output,
        )
        self.assertIn("字幕：ID 2 eng（英语）", output)
        self.assertIn("音轨：0:1 jpn（日语）", output)
        self.assertIn("字幕和音轨均选中：0", output)
        self.assertIn("处理失败：1", output)
        self.assertEqual(output.count("status-error.mkv"), 1)

    def test_main_groups_files_by_combined_selection_result(self) -> None:
        mkvs = [
            Path("both.mkv"),
            Path("subtitle-only.mkv"),
            Path("audio-only.mkv"),
            Path("neither.mkv"),
            Path("failed.mkv"),
        ]
        results = [
            _result(),
            _result(
                audio_line="无 jpn（现有：eng、chi）→ ffmpeg 自动选",
                audio_status="fallback",
            ),
            _result(
                subtitle_line="无字幕轨",
                subtitle_status="no_track",
            ),
            _result(
                subtitle_line="无 enm/eng（现有：jpn）",
                audio_line="无音频流",
                subtitle_status="no_match",
                audio_status="no_track",
            ),
            _result(
                subtitle_line="读取失败（字幕提取错误：损坏）",
                subtitle_status="failed",
                failure_reasons=("字幕读取失败：字幕提取错误：损坏",),
            ),
        ]
        with (
            mock.patch.object(workflow, "expand_inputs", return_value=mkvs),
            mock.patch.object(
                workflow,
                "has_processed_output",
                return_value=False,
            ),
            mock.patch.object(
                workflow,
                "dry_run_one",
                side_effect=results,
            ),
            mock.patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            exit_code = workflow.main(["input", "--dry-run"])

        self.assertEqual(exit_code, 1)
        output = stdout.getvalue()
        self.assertIn("预检汇总：共 5 个文件", output)
        self.assertIn("字幕和音轨均选中：1", output)
        self.assertIn("仅字幕选中：1", output)
        self.assertIn("仅音轨选中：1", output)
        self.assertIn("字幕和音轨均未选中：1", output)
        self.assertIn("处理失败：1", output)
        for mkv in mkvs:
            self.assertEqual(output.count(mkv.name), 1)
        self.assertIn("音轨：无 jpn（现有：eng、chi）→ ffmpeg 自动选", output)
        self.assertIn("字幕：无字幕轨", output)
        self.assertIn("字幕：无 enm/eng（现有：jpn）", output)
        self.assertIn("音轨：无音频流", output)
        self.assertIn("字幕：读取失败（字幕提取错误：损坏）", output)

    def test_main_reports_no_audio_without_ffmpeg_fallback(self) -> None:
        tracks = [_subtitle_track(2, "eng")]
        with (
            mock.patch.object(
                workflow,
                "expand_inputs",
                return_value=[Path("silent.mkv")],
            ),
            mock.patch.object(
                workflow,
                "has_processed_output",
                return_value=False,
            ),
            mock.patch.object(workflow.extract, "identify_tracks", return_value=tracks),
            mock.patch.object(workflow.burn, "FFMPEG") as ffmpeg,
            mock.patch.object(
                workflow.burn,
                "probe_video",
                return_value=(None, False, True, None, []),
            ),
            mock.patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            ffmpeg.is_file.return_value = True
            exit_code = workflow.main(["input", "--dry-run"])

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("音轨：无音频流", output)
        self.assertNotIn("ffmpeg 自动选", output)
        self.assertIn("仅字幕选中：1", output)
        self.assertIn("处理失败：0", output)
        self.assertEqual(output.count("silent.mkv"), 1)


if __name__ == "__main__":
    unittest.main()
