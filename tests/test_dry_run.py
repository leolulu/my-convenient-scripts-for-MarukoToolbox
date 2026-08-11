import argparse
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
            subtitle_line, audio_line, read_failed = workflow.dry_run_one(
                self.args,
                self.mkv,
            )

        self.assertEqual(subtitle_line, "ID 2 eng(英语)")
        self.assertEqual(audio_line, "0:1 jpn(日语)")
        self.assertFalse(read_failed)

    def test_subtitle_miss_lists_existing_languages(self) -> None:
        tracks = [_subtitle_track(1, "jpn"), _subtitle_track(3, "chs")]
        with (
            mock.patch.object(workflow.extract, "identify_tracks", return_value=tracks),
            mock.patch.object(workflow.burn, "probe_video", return_value=(None, True, True, None, [_audio_stream(1, "jpn")])),
        ):
            subtitle_line, _, read_failed = workflow.dry_run_one(
                self.args,
                self.mkv,
            )

        self.assertEqual(subtitle_line, "无 enm/eng(现有: chs, jpn)")
        self.assertFalse(read_failed)

    def test_subtitle_no_tracks_reports_unknown(self) -> None:
        with (
            mock.patch.object(workflow.extract, "identify_tracks", return_value=[]),
            mock.patch.object(workflow.burn, "probe_video", return_value=(None, True, True, None, [_audio_stream(1, "jpn")])),
        ):
            subtitle_line, _, read_failed = workflow.dry_run_one(
                self.args,
                self.mkv,
            )

        self.assertEqual(subtitle_line, "无 enm/eng(现有: 未知)")
        self.assertFalse(read_failed)

    def test_audio_miss_reports_ffmpeg_fallback(self) -> None:
        tracks = [_subtitle_track(2, "eng")]
        with (
            mock.patch.object(workflow.extract, "identify_tracks", return_value=tracks),
            mock.patch.object(workflow.burn, "probe_video", return_value=(None, True, True, None, [_audio_stream(1, "eng"), _audio_stream(2, "chi")])),
        ):
            _, audio_line, read_failed = workflow.dry_run_one(
                self.args,
                self.mkv,
            )

        self.assertEqual(audio_line, "无 jpn(现有: eng, chi) → ffmpeg 自动选")
        self.assertFalse(read_failed)

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
            _, audio_line, read_failed = workflow.dry_run_one(
                eng_args,
                self.mkv,
            )

        self.assertEqual(audio_line, "0:2 eng(英语)")
        self.assertFalse(read_failed)

    def test_missing_ffmpeg_reports_undetected(self) -> None:
        tracks = [_subtitle_track(2, "eng")]
        with (
            mock.patch.object(workflow.extract, "identify_tracks", return_value=tracks),
            mock.patch.object(workflow.burn, "FFMPEG", Path("nonexistent.exe")),
        ):
            _, audio_line, read_failed = workflow.dry_run_one(
                self.args,
                self.mkv,
            )

        self.assertEqual(audio_line, "未探测(缺 ffmpeg)")
        self.assertFalse(read_failed)

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
            _, audio_line, read_failed = workflow.dry_run_one(
                self.args,
                self.mkv,
            )

        self.assertEqual(audio_line, "无音频流")
        self.assertFalse(read_failed)


class DryRunMainTests(unittest.TestCase):
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
                return_value=("ID 2 eng(英语)", "0:1 jpn(日语)", False),
            ) as dry_run_one,
            mock.patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            exit_code = workflow.main(["input", "--dry-run"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(dry_run_one.call_count, 2)
        inspected = [call.args[1] for call in dry_run_one.call_args_list]
        self.assertEqual(inspected, mkvs)
        output = stdout.getvalue()
        self.assertIn("[1] already.mkv [已有处理结果]", output)
        self.assertIn("[2] pending.mkv\t字幕:", output)
        self.assertNotIn("已处理过，跳过", output)
        self.assertIn("字幕选中 2/2", output)
        self.assertIn("音轨命中 2/2", output)
        self.assertIn("读取失败 0", output)

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
        self.assertIn("字幕: ID 2 eng(英语)", output)
        self.assertIn("音轨: 探测失败(ffmpeg 未检测到视频流)", output)
        self.assertNotIn("字幕: 读取失败", output)
        self.assertIn("字幕选中 1/1", output)
        self.assertIn("音轨命中 0/1", output)
        self.assertIn("读取失败 1", output)

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
                return_value=("ID 2 eng(英语)", "0:1 jpn(日语)", False),
            ) as dry_run_one,
            mock.patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            exit_code = workflow.main(["input", "--dry-run"])

        self.assertEqual(exit_code, 1)
        dry_run_one.assert_called_once()
        output = stdout.getvalue()
        self.assertIn("处理结果状态检查失败(系统错误：无法读取输出目录)", output)
        self.assertIn("字幕: ID 2 eng(英语)", output)
        self.assertIn("音轨: 0:1 jpn(日语)", output)
        self.assertIn("字幕选中 1/1", output)
        self.assertIn("音轨命中 1/1", output)
        self.assertIn("读取失败 1", output)

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
        self.assertIn("音轨: 无音频流", output)
        self.assertNotIn("ffmpeg 自动选", output)
        self.assertIn("字幕选中 1/1", output)
        self.assertIn("音轨命中 0/1", output)
        self.assertIn("读取失败 0", output)


if __name__ == "__main__":
    unittest.main()
