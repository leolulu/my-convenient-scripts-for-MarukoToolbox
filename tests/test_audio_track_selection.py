from __future__ import annotations

import unittest
from pathlib import Path

import burn_subtitles as burn


class AudioTrackSelectionTests(unittest.TestCase):
    def test_single_audio_track_is_selected_directly(self) -> None:
        streams = [burn.AudioStream(index=1, language="jpn", default=True)]

        selected, decision = burn.select_audio_stream(streams, "jpn")

        self.assertEqual(selected, streams[0])
        self.assertIn("只有一条音轨", decision)

    def test_multiple_audio_tracks_follow_requested_language(self) -> None:
        streams = [
            burn.AudioStream(index=1, language="jpn", default=True),
            burn.AudioStream(index=2, language="eng", default=False),
        ]

        selected_japanese, _decision = burn.select_audio_stream(streams, "jpn")
        selected_english, _decision = burn.select_audio_stream(streams, "eng")

        self.assertEqual(selected_japanese, streams[0])
        self.assertEqual(selected_english, streams[1])

    def test_multiple_english_tracks_prefer_default_then_lowest_index(self) -> None:
        streams = [
            burn.AudioStream(index=1, language="eng", default=False),
            burn.AudioStream(index=2, language="en-US", default=True),
            burn.AudioStream(index=3, language="eng", default=True),
        ]

        selected, _decision = burn.select_audio_stream(streams, "eng")

        self.assertEqual(selected, streams[1])

    def test_multiple_audio_tracks_without_english_keep_ffmpeg_fallback(self) -> None:
        streams = [
            burn.AudioStream(index=1, language="jpn", default=True),
            burn.AudioStream(index=2, language="chi", default=False),
        ]

        selected, decision = burn.select_audio_stream(streams, "eng")

        self.assertIsNone(selected)
        self.assertIn("没有识别到英语音轨", decision)
        self.assertIn("ffmpeg 原有自动选择规则", decision)

    def test_supported_language_aliases_are_recognized(self) -> None:
        cases = {
            "jpn": ("jpn", "ja", "ja-JP", "japanese"),
            "eng": ("eng", "en", "en-US", "english"),
        }

        for preferred_language, aliases in cases.items():
            for alias in aliases:
                with self.subTest(
                    preferred_language=preferred_language,
                    alias=alias,
                ):
                    self.assertTrue(
                        burn.matches_audio_language(alias, preferred_language)
                    )

    def test_command_line_defaults_to_japanese_and_accepts_english(self) -> None:
        parser = burn.build_parser()

        default_args = parser.parse_args(["sample.mkv", "sample.ass"])
        english_args = parser.parse_args(
            ["sample.mkv", "sample.ass", "--audio-language", "eng"]
        )

        self.assertEqual(default_args.audio_language, "jpn")
        self.assertEqual(english_args.audio_language, "eng")

    def test_probe_output_parses_language_index_and_default_flag(self) -> None:
        probe_text = """
          Stream #0:0: Video: h264, yuv420p, 1920x1080, 23.98 fps
          Stream #0:1[0x2](jpn): Audio: aac, 48000 Hz, stereo
          Stream #0:2(eng): Audio: aac, 48000 Hz, stereo (default)
        """

        streams = burn.parse_audio_streams(probe_text)

        self.assertEqual(
            streams,
            [
                burn.AudioStream(index=1, language="jpn", default=False),
                burn.AudioStream(index=2, language="eng", default=True),
            ],
        )

    def test_selected_stream_is_mapped_into_audio_decode_command(self) -> None:
        stream = burn.AudioStream(index=2, language="eng", default=False)

        command = burn.build_audio_decode_command(Path("sample.mkv"), stream)

        map_position = command.index("-map")
        self.assertEqual(command[map_position + 1], "0:2")

    def test_ffmpeg_fallback_command_does_not_force_a_stream(self) -> None:
        command = burn.build_audio_decode_command(Path("sample.mkv"), None)

        self.assertNotIn("-map", command)


if __name__ == "__main__":
    unittest.main()
