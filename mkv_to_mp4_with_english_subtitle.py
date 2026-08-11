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
    parser.add_argument(
        "inputs",
        type=Path,
        nargs="+",
        help="输入 MKV 文件或目录；目录会处理其中顶层所有 .mkv，已输出过的自动跳过",
    )
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
        "--audio-language",
        choices=burn.AUDIO_LANGUAGE_CHOICES,
        default="jpn",
        metavar="{jpn,eng}",
        help="多音轨时优先选择的语言：jpn=日语，eng=英语；默认 jpn",
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
        "--no-export-subtitle",
        action="store_true",
        help="只烧录字幕，不在输出 MP4 旁边生成清洁后的字幕文件",
    )
    parser.add_argument(
        "--keep-media-temp",
        action="store_true",
        help="保留压制流程在小丸 temp 目录中生成的中间音视频",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只做预检：识别每个文件的音轨与字幕选择结果并打印，不执行提取/烧录",
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


def prepare_subtitle_outputs(
    mkv: Path,
    track: dict,
    no_export_subtitle: bool,
    overwrite: bool,
) -> tuple[Path, Path | None, list[Path]]:
    exported_output = (
        None if no_export_subtitle else extract.get_output_path(mkv, track)
    )
    exported_outputs = (
        []
        if exported_output is None
        else extract.get_related_outputs(exported_output, track)
    )
    existing = [path for path in exported_outputs if path.exists()]
    if existing and not overwrite:
        formatted = "\n".join(f"  - {path}" for path in existing)
        fail(f"字幕输出已经存在；如需覆盖，请添加 --overwrite：\n{formatted}")

    source_output = extract.get_output_path(mkv, track)
    burn.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    temp_id = f"mkv_english_subtitle_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    burn_subtitle = burn.TEMP_DIR / f"{temp_id}{source_output.suffix}"
    temporary_outputs = extract.get_related_outputs(burn_subtitle, track)
    return burn_subtitle, exported_output, temporary_outputs


def prepare_font_directory(mkv: Path, track: dict) -> Path | None:
    codec_id = str(track.get("properties", {}).get("codec_id", ""))
    if codec_id != "S_TEXT/ASS":
        return None

    attachments = extract.identify_attachments(mkv)
    font_attachments = extract.get_font_attachments(attachments)
    if not font_attachments:
        print("MKV 中没有字体附件；ASS 字幕继续使用现有烧录路径。")
        return None

    burn.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    temp_id = f"mkv_fonts_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    fonts_dir = burn.TEMP_DIR / temp_id
    try:
        outputs = extract.extract_font_attachments(
            mkv,
            font_attachments,
            fonts_dir,
        )
    except BaseException:
        extract.remove_directory(fonts_dir, warn_on_error=True)
        raise

    print(f"已提取 {len(outputs)} 个临时字体：{fonts_dir}")
    return fonts_dir


def build_burn_arguments(
    args: argparse.Namespace,
    mkv: Path,
    subtitle: Path,
    output: Path,
    fonts_dir: Path | None = None,
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
        "--audio-language",
        args.audio_language,
    ]
    if args.keyint is not None:
        arguments.extend(("--keyint", str(args.keyint)))
    if args.fallback_ffmpeg is not None:
        arguments.extend(("--fallback-ffmpeg", str(args.fallback_ffmpeg)))
    if fonts_dir is not None:
        arguments.extend(("--fonts-dir", str(fonts_dir)))
    if args.overwrite:
        arguments.append("--overwrite")
    if args.keep_media_temp:
        arguments.append("--keep-temp")
    return arguments


def _audio_language_label(language: str) -> str:
    """将音轨语言码映射为可读中文名；优先按偏好的 aliases 反查，其次 extract 表，最后原码。"""
    for pref_code, pref_name in burn.AUDIO_LANGUAGE_NAMES.items():
        if burn.matches_audio_language(language, pref_code):
            return pref_name
    name = extract.describe_language(language)
    if name != "未知语言代码":
        return name
    return language


def dry_run_one(args: argparse.Namespace, mkv: Path) -> tuple[str, str, bool]:
    """只读预检单个 MKV，返回字幕列、音轨列和是否发生读取失败。"""
    read_failed = False
    try:
        subtitle_tracks = [
            track
            for track in extract.identify_tracks(mkv)
            if track.get("type") == "subtitles"
        ]
        track, _ = extract.select_subtitle(subtitle_tracks)
        if track is None:
            available = sorted(
                {
                    str(item.get("properties", {}).get("language", "und"))
                    for item in subtitle_tracks
                }
            )
            subtitle_line = (
                f"无 enm/eng(现有: {', '.join(available)})"
                if available
                else "无 enm/eng(现有: 未知)"
            )
        else:
            props = track.get("properties", {})
            language = str(props.get("language", "und"))
            subtitle_line = (
                f"ID {track.get('id')} {language}"
                f"({extract.describe_language(language)})"
            )
    except (extract.ExtractSubtitleError, OSError) as error:
        subtitle_line = f"读取失败({describe_processing_error(error)})"
        read_failed = True

    if not burn.FFMPEG.is_file():
        audio_line = "未探测(缺 ffmpeg)"
    else:
        try:
            _, has_audio, _, _, audio_streams = burn.probe_video(mkv)
            if not has_audio:
                audio_line = "无音频流"
            else:
                audio, _ = burn.select_audio_stream(
                    audio_streams,
                    args.audio_language,
                )
                if audio is None:
                    existing = ", ".join(
                        stream.language for stream in audio_streams
                    )
                    audio_line = (
                        f"无 {args.audio_language}(现有: {existing}) → "
                        "ffmpeg 自动选"
                        if existing
                        else f"无 {args.audio_language}(现有: 未知) → "
                        "ffmpeg 自动选"
                    )
                else:
                    audio_line = (
                        f"0:{audio.index} {audio.language}"
                        f"({_audio_language_label(audio.language)})"
                    )
        except (burn.BurnSubtitlesError, OSError) as error:
            audio_line = f"探测失败({describe_processing_error(error)})"
            read_failed = True
    return subtitle_line, audio_line, read_failed


def process_one(args: argparse.Namespace, mkv: Path) -> dict[str, object]:
    """对单个 MKV 执行完整的识别→提取→烧录流程；失败抛 MkvToMp4Error/ExtractSubtitleError。

    成功返回记录：{"mkv", "audio": burn.AudioStream|None, "subtitle": track dict}。
    """
    burn_subtitle: Path | None = None
    exported_subtitle: Path | None = None
    burn_subtitle_outputs: list[Path] = []
    fonts_dir: Path | None = None
    try:
        extract.validate_binaries()

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
        burn_subtitle, exported_subtitle, burn_subtitle_outputs = (
            prepare_subtitle_outputs(
                mkv,
                track,
                args.no_export_subtitle,
                args.overwrite,
            )
        )
        subtitle_usage = (
            "仅用于烧录" if args.no_export_subtitle else "烧录并保留清洁副本"
        )
        print(f"字幕用途：{subtitle_usage}")
        print(f"原始烧录字幕（临时）：{burn_subtitle}")
        extract.extract_subtitle(mkv, track, burn_subtitle)
        fonts_dir = prepare_font_directory(mkv, track)

        if exported_subtitle is not None:
            removed_count = extract.export_subtitle_copy(
                burn_subtitle,
                exported_subtitle,
                track,
            )
            print(f"保留字幕路径：{exported_subtitle}")
            if removed_count:
                print(f"已从保留字幕中移除 {removed_count} 个定位标记。")

        print("\n========== 第 3 步：烧录字幕并生成 MP4 ==========")
        burn_arguments = build_burn_arguments(
            args,
            mkv,
            burn_subtitle,
            output,
            fonts_dir,
        )
        result: list = []
        burn_exit_code = burn.main(burn_arguments, result=result)
        if burn_exit_code == 130:
            raise KeyboardInterrupt
        if burn_exit_code != 0:
            fail(f"字幕烧录流程失败，退出码：{burn_exit_code}")
        audio_stream = result[0] if result else None

        print("\n========== 全部完成 ==========")
        print(f"输出 MP4：{output}")
        if exported_subtitle is not None:
            print(f"保留字幕：{exported_subtitle}")
        return {
            "mkv": mkv,
            "audio": audio_stream,
            "subtitle": track,
        }
    finally:
        if fonts_dir is not None:
            extract.remove_directory(fonts_dir, warn_on_error=True)
        if burn_subtitle is not None:
            extract.remove_outputs(
                burn_subtitle_outputs,
                warn_on_error=True,
            )


def expand_inputs(inputs: Sequence[Path]) -> list[Path]:
    """展开用户传入的位置参数：目录→其中顶层所有 .mkv 文件，文件→原样；去重保序。"""
    expanded: list[Path] = []
    for item in inputs:
        p = item.expanduser().resolve()
        if p.is_dir():
            mkv_files = [
                child
                for child in p.iterdir()
                if child.is_file() and child.suffix.lower() == ".mkv"
            ]
            if not mkv_files:
                print(f"目录中没有 .mkv 文件，跳过：{p}", file=sys.stderr)
            expanded.extend(mkv_files)
        elif p.is_file():
            expanded.append(extract.resolve_input(p))
        else:
            fail(f"输入路径不存在或既不是文件也不是目录：{p}")
    return list(dict.fromkeys(expanded))


def has_processed_output(mkv: Path, args: argparse.Namespace) -> bool:
    """返回输入 MKV 对应的输出 MP4 是否已经存在。"""
    output = burn.resolve_output(mkv, args.output)
    return output.exists()


def describe_processing_error(error: BaseException) -> str:
    """为批处理失败明细保留原有异常分类。"""
    if isinstance(error, extract.ExtractSubtitleError):
        return f"字幕提取错误：{error}"
    if isinstance(error, OSError):
        return f"系统错误：{error}"
    return str(error)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        mkvs = expand_inputs(args.inputs)
        if args.output is not None and len(mkvs) != 1:
            fail("--output 仅支持单个 MKV 输入；批量或目录模式使用默认输出命名")
        processed_skipped = 0
        succeeded = 0
        failures: list[tuple[Path, str]] = []
        succeeded_records: list[dict[str, object]] = []
        dry_run_total = 0
        dry_run_subtitle_hit = 0
        dry_run_audio_hit = 0
        dry_run_failures = 0
        if args.dry_run:
            audio_pref = burn.AUDIO_LANGUAGE_NAMES[args.audio_language]
            print(
                f"Dry-run 预检：{len(mkvs)} 个输入"
                f"（字幕按内建 enm→eng，音轨偏好={audio_pref} {args.audio_language}）"
            )
        for index, mkv in enumerate(mkvs, start=1):
            if not args.dry_run:
                print(f"\n========== 处理 {index}/{len(mkvs)}：{mkv} ==========")
            try:
                if args.dry_run:
                    status_failed = False
                    try:
                        already_processed = has_processed_output(mkv, args)
                    except (burn.BurnSubtitlesError, OSError) as error:
                        reason = describe_processing_error(error)
                        processed_label = f" [处理结果状态检查失败({reason})]"
                        status_failed = True
                    else:
                        processed_label = (
                            " [已有处理结果]" if already_processed else ""
                        )
                    subtitle_line, audio_line, read_failed = dry_run_one(args, mkv)
                    dry_run_total += 1
                    if status_failed or read_failed:
                        dry_run_failures += 1
                    if subtitle_line.startswith("ID "):
                        dry_run_subtitle_hit += 1
                    if audio_line.startswith("0:"):
                        dry_run_audio_hit += 1
                    print(
                        f"  [{index}] {mkv.name}{processed_label}"
                        f"\t字幕: {subtitle_line}\t音轨: {audio_line}"
                    )
                    continue
                already_processed = has_processed_output(mkv, args)
                if already_processed and not args.overwrite:
                    print(f"已处理过，跳过：{mkv}")
                    processed_skipped += 1
                    continue
                record = process_one(args, mkv)
            except (MkvToMp4Error, extract.ExtractSubtitleError, OSError) as error:
                reason = describe_processing_error(error)
                failures.append((mkv, reason))
                print(f"处理失败：{mkv}", file=sys.stderr)
                print(f"原因：{reason}", file=sys.stderr)
                continue
            succeeded += 1
            succeeded_records.append(record)
        if args.dry_run:
            print(
                f"\n命中汇总：字幕选中 {dry_run_subtitle_hit}/{dry_run_total} · "
                f"音轨命中 {dry_run_audio_hit}/{dry_run_total} · "
                f"读取失败 {dry_run_failures}"
            )
            return 1 if dry_run_failures else 0
        print(
            f"\n全部处理结束：共 {len(mkvs)} 个输入，"
            f"跳过 {processed_skipped} 个已处理，成功 {succeeded} 个，"
            f"失败 {len(failures)} 个。"
        )
        if succeeded_records:
            print("\n成功明细：")
            for record in succeeded_records:
                audio = record["audio"]
                if audio is None:
                    audio_line = "ffmpeg 自动选择（无偏好音轨）"
                else:
                    audio_line = f"{audio.language} (流 0:{audio.index})"
                sub = record["subtitle"]
                sub_props = sub.get("properties", {})
                sub_line = (
                    f"ID {sub.get('id')} · {sub_props.get('language', 'und')}"
                    f" · {sub_props.get('track_name') or '无名称'}"
                    f" · {sub.get('codec', '未知')}"
                )
                print(f"[{record['mkv']}]")
                print(f"  音轨：{audio_line}")
                print(f"  字幕：{sub_line}")
        if failures:
            print("\n失败明细：", file=sys.stderr)
            for mkv, reason in failures:
                print(f"  - {mkv}：{reason}", file=sys.stderr)
            return 1
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


if __name__ == "__main__":
    raise SystemExit(main())
