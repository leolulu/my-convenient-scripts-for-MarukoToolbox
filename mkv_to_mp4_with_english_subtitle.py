#!/usr/bin/env python3
"""从 MKV 选择并提取英文字幕，再将其烧录为 MP4。"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from typing import NoReturn, Sequence

import burn_subtitles as burn
import extract_english_subtitle as extract


class MkvToMp4Error(RuntimeError):
    """MKV 英文字幕烧录流程失败。"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "从 MKV 中按 enm→eng 选择一条英文字幕并提取，"
            "然后复用小丸压制流程将字幕烧录到 MP4。"
        )
    )
    parser.add_argument("mkv", type=Path, help="输入 MKV 文件")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="输出 MP4；默认是输入 MKV 同目录下的 <原名>_x264.mp4",
    )
    parser.add_argument(
        "--crf",
        type=burn.crf_value,
        default=24.0,
        help="小丸 x264 CRF，默认 24.0",
    )
    parser.add_argument(
        "--audio-bitrate",
        type=burn.positive_int,
        default=128,
        metavar="KBPS",
        help="Nero AAC-LC 音频码率，默认 128 kbps",
    )
    parser.add_argument(
        "--keyint",
        type=burn.positive_int,
        help="x264 最大关键帧间隔；默认按输入帧率的 10 秒自动计算",
    )
    parser.add_argument(
        "--fallback-ffmpeg",
        type=Path,
        help=(
            "小丸内置解码器不支持源视频时使用的外部 ffmpeg；"
            "默认从 PATH 自动查找"
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖已有的输出 MP4；保留字幕时也允许覆盖同名字幕",
    )
    parser.add_argument(
        "--keep-subtitle",
        action="store_true",
        help="将提取的字幕保留在 MKV 同目录；默认仅作为临时文件使用",
    )
    parser.add_argument(
        "--keep-media-temp",
        action="store_true",
        help="保留压制流程在小丸 temp 目录中生成的中间音视频",
    )
    return parser


def fail(message: str) -> NoReturn:
    raise MkvToMp4Error(message)


def print_decisions(decisions: list[str]) -> None:
    print("\n英文字幕判断流程：")
    for index, decision in enumerate(decisions, start=1):
        print(f"  {index}. {decision}")


def select_english_subtitle(mkv: Path) -> dict:
    tracks = extract.identify_tracks(mkv)
    subtitle_tracks = [track for track in tracks if track.get("type") == "subtitles"]
    if not subtitle_tracks:
        fail("MKV 中没有字幕轨，流程提前结束")

    extract.print_subtitle_tracks(subtitle_tracks)
    track, decisions = extract.select_subtitle(subtitle_tracks)
    print_decisions(decisions)
    if track is None:
        available = sorted(
            {
                str(item.get("properties", {}).get("language", "und"))
                for item in subtitle_tracks
            }
        )
        fail(
            "MKV 中没有 enm 或 eng 字幕轨，流程提前结束；"
            f"现有字幕语言：{', '.join(available) if available else '未知'}"
        )
    return track


def prepare_subtitle_output(
    mkv: Path,
    track: dict,
    keep_subtitle: bool,
    overwrite: bool,
) -> tuple[Path, list[Path]]:
    source_output = extract.get_output_path(mkv, track)
    if keep_subtitle:
        subtitle_output = source_output
    else:
        burn.TEMP_DIR.mkdir(parents=True, exist_ok=True)
        temp_id = f"mkv_english_subtitle_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        subtitle_output = burn.TEMP_DIR / f"{temp_id}{source_output.suffix}"

    related_outputs = extract.get_related_outputs(subtitle_output, track)
    existing = [path for path in related_outputs if path.exists()]
    if existing and not overwrite:
        formatted = "\n".join(f"  - {path}" for path in existing)
        fail(f"字幕输出已经存在；如需覆盖，请添加 --overwrite：\n{formatted}")
    if existing:
        extract.remove_outputs(related_outputs)
    return subtitle_output, related_outputs


def build_burn_arguments(
    args: argparse.Namespace,
    mkv: Path,
    subtitle: Path,
    output: Path,
) -> list[str]:
    arguments = [
        str(mkv),
        str(subtitle),
        "--output",
        str(output),
        "--crf",
        str(args.crf),
        "--audio-bitrate",
        str(args.audio_bitrate),
    ]
    if args.keyint is not None:
        arguments.extend(("--keyint", str(args.keyint)))
    if args.fallback_ffmpeg is not None:
        arguments.extend(("--fallback-ffmpeg", str(args.fallback_ffmpeg)))
    if args.overwrite:
        arguments.append("--overwrite")
    if args.keep_media_temp:
        arguments.append("--keep-temp")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    subtitle_output: Path | None = None
    related_subtitle_outputs: list[Path] = []
    try:
        extract.validate_binaries()
        mkv = extract.resolve_input(args.mkv)

        print("========== 第 1 步：识别英文字幕 ==========")
        print(f"输入文件：{mkv}")
        track = select_english_subtitle(mkv)

        burn.validate_binaries()
        output = burn.resolve_output(mkv, args.output)
        if output.exists() and not args.overwrite:
            fail(f"输出 MP4 已经存在；如需覆盖，请添加 --overwrite：{output}")

        properties = track.get("properties", {})
        print(
            f"\n最终字幕选择：ID {track.get('id')}，"
            f"语言 {properties.get('language', 'und')}，"
            f"名称 {properties.get('track_name') or '（无名称）'}，"
            f"格式 {track.get('codec', '未知')}"
        )

        print("\n========== 第 2 步：提取字幕 ==========")
        subtitle_output, related_subtitle_outputs = prepare_subtitle_output(
            mkv,
            track,
            args.keep_subtitle,
            args.overwrite,
        )
        print(f"字幕用途：{'保留输出' if args.keep_subtitle else '临时中间文件'}")
        print(f"字幕路径：{subtitle_output}")
        extract.extract_subtitle(mkv, track, subtitle_output)

        print("\n========== 第 3 步：烧录字幕并生成 MP4 ==========")
        burn_arguments = build_burn_arguments(args, mkv, subtitle_output, output)
        burn_exit_code = burn.main(burn_arguments)
        if burn_exit_code == 130:
            raise KeyboardInterrupt
        if burn_exit_code != 0:
            fail(f"字幕烧录流程失败，退出码：{burn_exit_code}")

        print("\n========== 全部完成 ==========")
        print(f"输出 MP4：{output}")
        if args.keep_subtitle:
            print(f"保留字幕：{subtitle_output}")
        return 0
    except MkvToMp4Error as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except extract.ExtractSubtitleError as error:
        print(f"字幕提取错误：{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except OSError as error:
        print(f"系统错误：{error}", file=sys.stderr)
        return 1
    finally:
        if subtitle_output is not None and not args.keep_subtitle:
            extract.remove_outputs(related_subtitle_outputs)


if __name__ == "__main__":
    raise SystemExit(main())
