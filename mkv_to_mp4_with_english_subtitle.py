#!/usr/bin/env python3
"""从 MKV 选择并提取英文字幕，再将其烧录为 MP4。"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Sequence

import burn_subtitles as burn
import extract_english_subtitle as extract


class MkvToMp4Error(RuntimeError):
    """MKV 英文字幕烧录流程失败。"""


class SkipCurrentFile(Exception):
    """用户在交互选轨时主动跳过当前文件。"""


class ExplicitAudioLanguage(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        namespace.audio_language_explicit = True


@dataclass(frozen=True)
class DryRunResult:
    """单个 MKV 的字幕与音轨预检结果。"""

    subtitle_line: str
    audio_line: str
    subtitle_status: str
    audio_status: str
    failure_reasons: tuple[str, ...] = ()

    @property
    def subtitle_selected(self) -> bool:
        return self.subtitle_status == "selected"

    @property
    def audio_selected(self) -> bool:
        return self.audio_status == "selected"

    @property
    def failed(self) -> bool:
        return bool(self.failure_reasons)


@dataclass(frozen=True)
class DryRunRecord:
    """用于生成文件级汇总的完整预检记录。"""

    result: DryRunResult
    status_failure: str | None = None

    @property
    def failed(self) -> bool:
        return self.status_failure is not None or self.result.failed


@dataclass(frozen=True)
class InteractiveSelection:
    """交互阶段为单个文件确认的字幕与音轨。"""

    subtitle: dict
    audio: burn.AudioStream


def format_processing_duration(elapsed_seconds: float) -> str:
    """根据耗时长度按分钟、小时或天显示。"""

    total_seconds = int(elapsed_seconds)
    days, remaining_seconds = divmod(total_seconds, 24 * 60 * 60)
    hours, remaining_seconds = divmod(remaining_seconds, 60 * 60)
    minutes, seconds = divmod(remaining_seconds, 60)
    if days:
        return f"{days}天{hours:02d}小时{minutes:02d}分{seconds:02d}秒"
    if hours:
        return f"{hours}小时{minutes:02d}分{seconds:02d}秒"
    return f"{minutes}分{seconds:02d}秒"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "默认从 MKV 中按 enm→eng 选择一条英文字幕并提取；"
            "使用 -i/--interactive 可手选字幕与音轨，然后烧录为 MP4。"
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
        action=ExplicitAudioLanguage,
        metavar="{jpn,eng}",
        help="多音轨时优先选择的语言：jpn=日语，eng=英语；默认 jpn",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="先为所有视频手动选择字幕轨和音轨，全部确认后再依次提取与压制",
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


def read_interactive_input(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError as error:
        raise KeyboardInterrupt from error


def prompt_track_number(label: str, count: int, selectable: set[int]) -> int:
    while True:
        answer = read_interactive_input(f"请选择{label}编号（q 跳过当前文件）：")
        if answer == "q":
            raise SkipCurrentFile
        if answer.isdecimal():
            number = int(answer)
            if 1 <= number <= count and number in selectable:
                return number - 1
        print("输入无效或该轨道不可选，请输入可选编号。")


def select_interactive_tracks(
    mkv: Path, output: Path
) -> tuple[dict, burn.AudioStream]:
    tracks = extract.identify_tracks(mkv)
    subtitle_tracks = [track for track in tracks if track.get("type") == "subtitles"]
    if not subtitle_tracks:
        fail("MKV 中没有字幕轨，流程提前结束")
    selectable_subtitles = {
        index
        for index, track in enumerate(subtitle_tracks, start=1)
        if str(track.get("properties", {}).get("codec_id", ""))
        in extract.SUBTITLE_EXTENSIONS
    }
    if not selectable_subtitles:
        fail("MKV 中没有当前脚本可提取的字幕轨，流程提前结束")

    audio_details: dict[int, burn.AudioStreamDetails] = {}
    _, has_audio, _, _, audio_streams = burn.probe_video(
        mkv, audio_details=audio_details
    )
    if not has_audio:
        fail("输入视频不包含音频流，当前脚本无法执行“压制音频”流程")
    mkv_audio_tracks = [track for track in tracks if track.get("type") == "audio"]
    if (
        not audio_streams
        or len(audio_streams) != len(mkv_audio_tracks)
        or len({stream.index for stream in audio_streams}) != len(audio_streams)
        or len(audio_details) != len(audio_streams)
    ):
        fail("无法完整识别音轨，交互模式无法安全地手动选择")

    named_audio_tracks: dict[int, dict] = {}
    ambiguous_track_numbers: set[int] = set()
    for track in mkv_audio_tracks:
        number = track.get("properties", {}).get("number")
        try:
            number = int(number)
        except (TypeError, ValueError):
            continue
        if number in named_audio_tracks:
            ambiguous_track_numbers.add(number)
        else:
            named_audio_tracks[number] = track
    for number in ambiguous_track_numbers:
        named_audio_tracks.pop(number)

    while True:
        print(f"\n内嵌字幕轨：共 {len(subtitle_tracks)} 条")
        for index, track in enumerate(subtitle_tracks, start=1):
            properties = track.get("properties", {})
            language = str(properties.get("language") or "und").lower()
            codec_id = str(properties.get("codec_id") or "未知")
            if index not in selectable_subtitles:
                availability = " | 不可选：当前脚本不支持提取"
            elif codec_id not in {
                "S_TEXT/UTF8",
                "S_TEXT/ASCII",
                "S_TEXT/ASS",
                "S_TEXT/SSA",
                "S_HDMV/PGS",
            }:
                availability = " | 提示：烧录兼容性未验证"
            else:
                availability = ""
            print(
                f"  [{index}] ID={track.get('id')} | "
                f"语言={language}（{extract.describe_language(language)}） | "
                f"名称={properties.get('track_name') or '未知'} | "
                f"格式={track.get('codec') or '未知'}（{codec_id}） | "
                f"默认={bool(properties.get('default_track', False))} | "
                f"强制={bool(properties.get('forced_track', False))}"
                f"{availability}"
            )
        subtitle_index = prompt_track_number(
            "字幕轨", len(subtitle_tracks), selectable_subtitles
        )
        subtitle = subtitle_tracks[subtitle_index]

        print(f"\n音轨：共 {len(audio_streams)} 条")
        for index, stream in enumerate(audio_streams, start=1):
            details = audio_details[stream.index]
            named_track = named_audio_tracks.get(details.track_number, {})
            name = (
                details.name
                or named_track.get("properties", {}).get("track_name")
                or "未知"
            )
            print(
                f"  [{index}] 流 0:{stream.index} | 语言={stream.language} | "
                f"名称={name} | 编码={details.codec} | 声道={details.channels} | "
                f"默认={stream.default}"
            )
        audio_index = prompt_track_number(
            "音轨", len(audio_streams), set(range(1, len(audio_streams) + 1))
        )
        audio = audio_streams[audio_index]

        print("\n选轨确认：")
        print(
            f"  字幕：ID {subtitle.get('id')}，"
            f"语言 {subtitle.get('properties', {}).get('language') or 'und'}，"
            f"名称 {subtitle.get('properties', {}).get('track_name') or '未知'}"
        )
        print(f"  音轨：流 0:{audio.index}，语言 {audio.language}")
        print(f"  输出：{output}")
        while True:
            answer = read_interactive_input("确认选择（y 开始 / r 重选 / q 跳过）：")
            if answer == "y":
                return subtitle, audio
            if answer == "r":
                break
            if answer == "q":
                raise SkipCurrentFile
            print("输入无效，请输入 y、r 或 q。")


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


def dry_run_one(args: argparse.Namespace, mkv: Path) -> DryRunResult:
    """只读预检单个 MKV，返回结构化的字幕、音轨与失败状态。"""
    failure_reasons: list[str] = []
    try:
        subtitle_tracks = [
            track
            for track in extract.identify_tracks(mkv)
            if track.get("type") == "subtitles"
        ]
        track, _ = extract.select_subtitle(subtitle_tracks)
        if not subtitle_tracks:
            subtitle_line = "无字幕轨"
            subtitle_status = "no_track"
        elif track is None:
            available = sorted(
                {
                    str(item.get("properties", {}).get("language", "und"))
                    for item in subtitle_tracks
                }
            )
            subtitle_line = f"无 enm/eng（现有：{'、'.join(available)}）"
            subtitle_status = "no_match"
        else:
            props = track.get("properties", {})
            language = str(props.get("language", "und"))
            subtitle_line = (
                f"ID {track.get('id')} {language}"
                f"（{extract.describe_language(language)}）"
            )
            subtitle_status = "selected"
    except (extract.ExtractSubtitleError, OSError) as error:
        reason = describe_processing_error(error)
        subtitle_line = f"读取失败（{reason}）"
        subtitle_status = "failed"
        failure_reasons.append(f"字幕读取失败：{reason}")

    if not burn.FFMPEG.is_file():
        audio_line = "未探测（缺 ffmpeg）"
        audio_status = "unprobed"
    else:
        try:
            _, has_audio, _, _, audio_streams = burn.probe_video(mkv)
            if not has_audio:
                audio_line = "无音频流"
                audio_status = "no_track"
            else:
                audio, _ = burn.select_audio_stream(
                    audio_streams,
                    args.audio_language,
                )
                if audio is None:
                    existing = "、".join(
                        stream.language for stream in audio_streams
                    )
                    audio_line = (
                        f"无 {args.audio_language}（现有：{existing}）→ "
                        "ffmpeg 自动选"
                        if existing
                        else f"无 {args.audio_language}（现有：未知）→ "
                        "ffmpeg 自动选"
                    )
                    audio_status = "fallback"
                else:
                    audio_line = (
                        f"0:{audio.index} {audio.language}"
                        f"（{_audio_language_label(audio.language)}）"
                    )
                    audio_status = "selected"
        except (burn.BurnSubtitlesError, OSError) as error:
            reason = describe_processing_error(error)
            audio_line = f"探测失败（{reason}）"
            audio_status = "failed"
            failure_reasons.append(f"音轨探测失败：{reason}")
    return DryRunResult(
        subtitle_line=subtitle_line,
        audio_line=audio_line,
        subtitle_status=subtitle_status,
        audio_status=audio_status,
        failure_reasons=tuple(failure_reasons),
    )


def dry_run_group(record: DryRunRecord) -> str:
    """返回预检记录所属的互斥汇总分组。"""
    if record.failed:
        return "failed"
    if record.result.subtitle_selected and record.result.audio_selected:
        return "both"
    if record.result.subtitle_selected:
        return "subtitle_only"
    if record.result.audio_selected:
        return "audio_only"
    return "neither"


def print_dry_run_summary(records: list[DryRunRecord]) -> None:
    """按文件的字幕/音轨组合打印互斥分组计数。"""
    groups = (
        ("both", "字幕和音轨均选中"),
        ("subtitle_only", "仅字幕选中"),
        ("audio_only", "仅音轨选中"),
        ("neither", "字幕和音轨均未选中"),
        ("failed", "处理失败"),
    )
    print(f"\n预检汇总：共 {len(records)} 个文件")
    for key, label in groups:
        count = sum(dry_run_group(record) == key for record in records)
        print(f"  {label}：{count}")


def prepare_interactive_selection(
    args: argparse.Namespace, mkv: Path
) -> InteractiveSelection:
    """校验单个输入并完成选轨，不执行字幕提取或视频压制。"""
    print("========== 手动选择字幕与音轨 ==========")
    print(f"输入文件：{mkv}")
    extract.validate_binaries()
    burn.validate_binaries()
    output = burn.resolve_output(mkv, args.output)
    if output.exists() and not args.overwrite:
        fail(f"输出 MP4 已经存在；如需覆盖，请添加 --overwrite：{output}")
    subtitle, audio = select_interactive_tracks(mkv, output)
    return InteractiveSelection(subtitle, audio)


def process_one(
    args: argparse.Namespace,
    mkv: Path,
    interactive_selection: InteractiveSelection | None = None,
) -> dict[str, object]:
    """对单个 MKV 执行完整的识别→提取→烧录流程；失败抛 MkvToMp4Error/ExtractSubtitleError。

    成功返回记录：{"mkv", "audio": burn.AudioStream|None, "subtitle": track dict}。
    """
    burn_subtitle: Path | None = None
    exported_subtitle: Path | None = None
    burn_subtitle_outputs: list[Path] = []
    fonts_dir: Path | None = None
    interactive = getattr(args, "interactive", False)
    selected_audio_stream: burn.AudioStream | None = None
    try:
        extract.validate_binaries()

        if interactive:
            if interactive_selection is None:
                interactive_selection = prepare_interactive_selection(args, mkv)
            else:
                burn.validate_binaries()
                output = burn.resolve_output(mkv, args.output)
                if output.exists() and not args.overwrite:
                    fail(f"输出 MP4 已经存在；如需覆盖，请添加 --overwrite：{output}")
            print("========== 第 1 步：使用已确认的字幕与音轨 ==========")
            print(f"输入文件：{mkv}")
            track = interactive_selection.subtitle
            selected_audio_stream = interactive_selection.audio
        else:
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
        if interactive:
            burn_exit_code = burn.main(
                burn_arguments,
                result=result,
                selected_audio_stream=selected_audio_stream,
            )
        else:
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
    if output.exists():
        return True
    if args.output is None:
        return mkv.with_suffix(".mp4").exists()
    return False


def describe_processing_error(error: BaseException) -> str:
    """为批处理失败明细保留原有异常分类。"""
    if isinstance(error, extract.ExtractSubtitleError):
        return f"字幕提取错误：{error}"
    if isinstance(error, OSError):
        return f"系统错误：{error}"
    return str(error)


def print_interrupted_batch_summary(states: dict[Path, str]) -> None:
    """打印多文件批次在 Ctrl+C 时的最终状态。"""
    groups = [
        ("succeeded", "本次已完成"),
        ("skipped", "已有结果，已跳过"),
        ("failed", "处理失败"),
        ("interrupted", "当前被中断"),
        ("not_started", "尚未开始"),
    ]
    if "user_skipped" in states.values():
        groups.insert(2, ("user_skipped", "用户主动跳过"))
    grouped = {
        status: [path for path, current in states.items() if current == status]
        for status, _label in groups
    }
    counts = "，".join(
        f"{label} {len(grouped[status])} 个" for status, label in groups
    )
    print(
        f"\n批次中断汇总：共 {len(states)} 个文件；{counts}。",
        file=sys.stderr,
    )
    for status, label in groups:
        paths = grouped[status]
        if not paths:
            continue
        print(f"{label}：", file=sys.stderr)
        for path in paths:
            print(f"  - {path}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    batch_states: dict[Path, str] = {}
    try:
        interactive = getattr(args, "interactive", False)
        if interactive and args.dry_run:
            fail("--interactive 不能与 --dry-run 同时使用")
        if interactive and getattr(args, "audio_language_explicit", False):
            fail("--interactive 不能与显式指定的 --audio-language 同时使用")
        if interactive and not sys.stdin.isatty():
            fail("--interactive 需要可交互的标准输入终端")
        mkvs = expand_inputs(args.inputs)
        batch_states = {mkv: "not_started" for mkv in mkvs}
        if args.output is not None and len(mkvs) != 1:
            fail("--output 仅支持单个 MKV 输入；批量或目录模式使用默认输出命名")
        processed_skipped = 0
        user_skipped: list[Path] = []
        succeeded = 0
        failures: list[tuple[Path, str]] = []
        succeeded_records: list[dict[str, object]] = []
        dry_run_records: list[DryRunRecord] = []
        interactive_selections: dict[Path, InteractiveSelection] = {}
        processing_started_at = time.monotonic() if not args.dry_run else None
        if args.dry_run:
            audio_pref = burn.AUDIO_LANGUAGE_NAMES[args.audio_language]
            print(
                f"Dry-run 预检：{len(mkvs)} 个输入"
                f"（字幕按内建 enm→eng，音轨偏好={audio_pref} {args.audio_language}）"
            )
        if interactive:
            print(f"\n========== 选轨阶段：共 {len(mkvs)} 个输入 ==========")
            for index, mkv in enumerate(mkvs, start=1):
                batch_states[mkv] = "interrupted"
                print(f"\n========== 选择 {index}/{len(mkvs)}：{mkv} ==========")
                try:
                    already_processed = has_processed_output(mkv, args)
                    if already_processed and not args.overwrite:
                        print(f"已处理过，跳过：{mkv}")
                        processed_skipped += 1
                        batch_states[mkv] = "skipped"
                        continue
                    interactive_selections[mkv] = prepare_interactive_selection(args, mkv)
                    batch_states[mkv] = "not_started"
                except SkipCurrentFile:
                    user_skipped.append(mkv)
                    batch_states[mkv] = "user_skipped"
                    print(f"用户主动跳过：{mkv}")
                except (MkvToMp4Error, extract.ExtractSubtitleError, OSError) as error:
                    reason = describe_processing_error(error)
                    failures.append((mkv, reason))
                    batch_states[mkv] = "failed"
                    print(f"选轨失败：{mkv}", file=sys.stderr)
                    print(f"原因：{reason}", file=sys.stderr)
            print(
                f"\n选轨阶段完成：已确认 {len(interactive_selections)} 个文件。"
                "现在开始依次转换，无需继续操作。"
            )
        conversion_index = 0
        for index, mkv in enumerate(mkvs, start=1):
            if interactive and mkv not in interactive_selections:
                continue
            if interactive:
                conversion_index += 1
                display_index = conversion_index
                display_total = len(interactive_selections)
            else:
                display_index = index
                display_total = len(mkvs)
            if not args.dry_run:
                batch_states[mkv] = "interrupted"
                file_started_at = time.monotonic()
            if not args.dry_run:
                action = "转换" if interactive else "处理"
                print(
                    f"\n========== {action} {display_index}/{display_total}："
                    f"{mkv} =========="
                )
            try:
                if args.dry_run:
                    status_failure = None
                    try:
                        already_processed = has_processed_output(mkv, args)
                    except (burn.BurnSubtitlesError, OSError) as error:
                        reason = describe_processing_error(error)
                        processed_label = f" [处理结果状态检查失败（{reason}）]"
                        already_processed = False
                        status_failure = f"处理结果状态检查失败：{reason}"
                    else:
                        processed_label = (
                            " [已有处理结果]" if already_processed else ""
                        )
                    result = dry_run_one(args, mkv)
                    dry_run_records.append(
                        DryRunRecord(
                            result=result,
                            status_failure=status_failure,
                        )
                    )
                    print(f"\n[{index}]{processed_label} {mkv.name}")
                    print(f"    字幕：{result.subtitle_line}")
                    print(f"    音轨：{result.audio_line}")
                    continue
                if interactive:
                    record = process_one(args, mkv, interactive_selections[mkv])
                else:
                    already_processed = has_processed_output(mkv, args)
                    if already_processed and not args.overwrite:
                        print(f"已处理过，跳过：{mkv}")
                        processed_skipped += 1
                        batch_states[mkv] = "skipped"
                        continue
                    record = process_one(args, mkv)
            except SkipCurrentFile:
                user_skipped.append(mkv)
                batch_states[mkv] = "user_skipped"
                print(f"用户主动跳过：{mkv}")
                continue
            except (MkvToMp4Error, extract.ExtractSubtitleError, OSError) as error:
                reason = describe_processing_error(error)
                failures.append((mkv, reason))
                batch_states[mkv] = "failed"
                print(f"处理失败：{mkv}", file=sys.stderr)
                print(f"原因：{reason}", file=sys.stderr)
                continue
            record["processing_duration"] = format_processing_duration(
                time.monotonic() - file_started_at
            )
            succeeded += 1
            succeeded_records.append(record)
            batch_states[mkv] = "succeeded"
        if args.dry_run:
            print_dry_run_summary(dry_run_records)
            return 1 if any(record.failed for record in dry_run_records) else 0
        assert processing_started_at is not None
        processing_duration = format_processing_duration(
            time.monotonic() - processing_started_at
        )
        if interactive:
            print(
                f"\n全部处理结束：共 {len(mkvs)} 个输入，"
                f"跳过 {processed_skipped} 个已处理，"
                f"用户主动跳过 {len(user_skipped)} 个，"
                f"成功 {succeeded} 个，失败 {len(failures)} 个。"
            )
        else:
            print(
                f"\n全部处理结束：共 {len(mkvs)} 个输入，"
                f"跳过 {processed_skipped} 个已处理，成功 {succeeded} 个，"
                f"失败 {len(failures)} 个。"
            )
        print(f"总处理时间：{processing_duration}")
        if user_skipped:
            print("\n用户主动跳过明细：")
            for mkv in user_skipped:
                print(f"  - {mkv}")
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
                print(f"  处理时间：{record['processing_duration']}")
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
        if not args.dry_run and len(batch_states) > 1:
            print_interrupted_batch_summary(batch_states)
        return 130
    except OSError as error:
        print(f"系统错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
