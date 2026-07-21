#!/usr/bin/env python3
"""从 MKV 中提取一条英文字幕，优先 enm，其次 eng。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, NoReturn, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
TOOLS_DIR = ROOT_DIR / "tools"

MKVMERGE = TOOLS_DIR / "mkvmerge.exe"
MKVEXTRACT = TOOLS_DIR / "mkvextract.exe"

LANGUAGE_PRIORITY = ("enm", "eng")
LANGUAGE_NAMES = {
    "ara": "阿拉伯语",
    "chi": "中文",
    "cmn": "普通话",
    "deu": "德语",
    "eng": "英语",
    "enm": "中古英语代码（按需求作为英文候选）",
    "fre": "法语",
    "fra": "法语",
    "ger": "德语",
    "ind": "印度尼西亚语",
    "ita": "意大利语",
    "jpn": "日语",
    "kor": "韩语",
    "may": "马来语",
    "msa": "马来语",
    "pol": "波兰语",
    "por": "葡萄牙语",
    "rus": "俄语",
    "spa": "西班牙语",
    "tha": "泰语",
    "tur": "土耳其语",
    "und": "未确定语言",
    "vie": "越南语",
    "zho": "中文",
}
SUBTITLE_EXTENSIONS = {
    "S_TEXT/UTF8": ".srt",
    "S_TEXT/ASCII": ".srt",
    "S_TEXT/ASS": ".ass",
    "S_TEXT/SSA": ".ssa",
    "S_TEXT/WEBVTT": ".vtt",
    "S_TEXT/USF": ".usf",
    "S_HDMV/PGS": ".sup",
    "S_VOBSUB": ".idx",
    "S_DVBSUB": ".sub",
}
ALIGNMENT_TAG_EXTENSIONS = {".srt", ".ass", ".ssa"}
OVERRIDE_BLOCK_RE = re.compile(rb"\{[^{}\r\n]*\}")
ALIGNMENT_TAG_RE = re.compile(rb"\\an[1-9](?!\d)", re.IGNORECASE)
ASS_EVENT_PREFIXES = (b"dialogue:", b"comment:")


class ExtractSubtitleError(RuntimeError):
    """字幕识别或提取失败。"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "使用小丸工具箱内置的 mkvmerge/mkvextract，从 MKV 中提取一条英文字幕。"
            "优先选择 enm，没有 enm 时选择 eng。"
        )
    )
    parser.add_argument("mkv", type=Path, help="输入 MKV 文件")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖已经存在的同名字幕文件",
    )
    return parser


def fail(message: str) -> NoReturn:
    raise ExtractSubtitleError(message)


def resolve_input(path: Path) -> Path:
    mkv = path.expanduser().resolve()
    if not mkv.is_file():
        fail(f"输入文件不存在或不是文件：{mkv}")
    if mkv.suffix.lower() != ".mkv":
        fail(f"输入文件必须使用 .mkv 扩展名：{mkv}")
    return mkv


def validate_binaries() -> None:
    missing = [path for path in (MKVMERGE, MKVEXTRACT) if not path.is_file()]
    if missing:
        formatted = "\n".join(f"  - {path}" for path in missing)
        fail(f"缺少小丸工具箱二进制文件：\n{formatted}")


def identify_tracks(mkv: Path) -> list[dict[str, Any]]:
    command = [MKVMERGE, "-J", mkv]
    result = subprocess.run(
        command,
        cwd=ROOT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode not in (0, 1):
        details = result.stderr.decode("utf-8", errors="replace").strip()
        if details:
            details = f"\n{details}"
        fail(f"mkvmerge 读取轨道失败，退出码：{result.returncode}{details}")

    try:
        identification = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"无法解析 mkvmerge 返回的轨道 JSON：{error}")

    errors = identification.get("errors") or []
    if errors:
        fail("mkvmerge 报告错误：\n" + "\n".join(str(error) for error in errors))

    tracks = identification.get("tracks")
    if not isinstance(tracks, list):
        fail("mkvmerge 返回的数据中缺少轨道列表")
    return tracks


def select_subtitle(
    subtitle_tracks: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    decisions = ["选择优先级固定为：enm → eng。"]
    for language in LANGUAGE_PRIORITY:
        candidates = [
            track
            for track in subtitle_tracks
            if str(track.get("properties", {}).get("language", "")).lower() == language
        ]
        if not candidates:
            decisions.append(f"查找 {language}：没有匹配轨道，继续下一优先级。")
            continue

        candidate_ids = ", ".join(str(track.get("id")) for track in candidates)
        decisions.append(
            f"查找 {language}：找到 {len(candidates)} 条，轨道 ID 为 {candidate_ids}。"
        )
        ranked = sorted(
            candidates,
            key=lambda track: (
                not bool(track.get("properties", {}).get("default_track", False)),
                int(track.get("id", 0)),
            ),
        )
        selected = ranked[0]
        if len(ranked) > 1:
            decisions.append(
                "同语言存在多条轨道，先选择 default_track=true 的轨道，"
                "仍有并列时选择 ID 最小的轨道。"
            )
        if language == "enm":
            decisions.append("enm 已命中最高优先级，因此不再使用 eng 轨道。")
        else:
            decisions.append("enm 不存在，按规则回退到 eng。")
        decisions.append(f"本次选择轨道 ID {selected.get('id')}。")
        return selected, decisions

    decisions.append("所有优先级均未命中，无法自动选择字幕。")
    return None, decisions


def describe_language(code: str) -> str:
    return LANGUAGE_NAMES.get(code.lower(), "未知语言代码")


def describe_track_function(track: dict[str, Any]) -> str:
    properties = track.get("properties", {})
    name = str(properties.get("track_name") or "")
    normalized_name = name.lower()
    features: list[str] = []

    if properties.get("default_track", False):
        features.append("默认轨")
    if properties.get("forced_track", False):
        features.append("强制字幕")
    if "honorific" in normalized_name:
        features.append("保留敬称的字幕版本")
    if "song" in normalized_name and "sign" in normalized_name:
        features.append("歌曲与画面文字字幕")
    elif "sign" in normalized_name:
        features.append("画面文字字幕")
    elif "song" in normalized_name:
        features.append("歌曲字幕")
    if "sdh" in normalized_name:
        features.append("听障辅助字幕（SDH）")
    if normalized_name == "cc" or "closed caption" in normalized_name:
        features.append("隐藏式字幕（CC）")
    if "forced" in normalized_name and not properties.get("forced_track", False):
        features.append("名称标示为强制字幕")

    if not features:
        return "未注明具体功能"
    return "；".join(features)


def print_subtitle_tracks(subtitle_tracks: list[dict[str, Any]]) -> None:
    print(f"\n字幕轨扫描结果：共 {len(subtitle_tracks)} 条")
    for index, track in enumerate(subtitle_tracks, start=1):
        properties = track.get("properties", {})
        language = str(properties.get("language") or "und").lower()
        name = properties.get("track_name") or "（无名称）"
        codec = track.get("codec", "未知")
        codec_id = properties.get("codec_id", "未知")
        print(
            f"  [{index}] ID={track.get('id')} | "
            f"语言={language}（{describe_language(language)}） | "
            f"名称={name} | 格式={codec}（{codec_id}） | "
            f"默认={bool(properties.get('default_track', False))} | "
            f"强制={bool(properties.get('forced_track', False))} | "
            f"功能/特征={describe_track_function(track)}"
        )


def get_output_path(mkv: Path, track: dict[str, Any]) -> Path:
    properties = track.get("properties", {})
    codec_id = str(properties.get("codec_id", ""))
    extension = SUBTITLE_EXTENSIONS.get(codec_id)
    if extension is None:
        codec = track.get("codec", "未知")
        fail(f"暂不支持提取该字幕格式：{codec}（{codec_id or '无 codec_id'}）")
    return mkv.with_suffix(extension)


def get_related_outputs(output: Path, track: dict[str, Any]) -> list[Path]:
    outputs = [output]
    if track.get("properties", {}).get("codec_id") == "S_VOBSUB":
        outputs.append(output.with_suffix(".sub"))
    return outputs


def extract_subtitle(mkv: Path, track: dict[str, Any], output: Path) -> None:
    track_id = int(track["id"])
    command = [MKVEXTRACT, "tracks", mkv, f"{track_id}:{output}"]
    print("执行命令：")
    print(subprocess.list2cmdline([str(argument) for argument in command]), flush=True)

    result = subprocess.run(
        command,
        cwd=ROOT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode not in (0, 1):
        details = (result.stdout + result.stderr).decode("utf-8", errors="replace").strip()
        if details:
            details = f"\n{details}"
        fail(f"mkvextract 提取字幕失败，退出码：{result.returncode}{details}")
    if result.returncode == 1:
        print("警告：mkvextract 提取完成，但报告了警告。", file=sys.stderr)
    if not output.is_file():
        fail(f"mkvextract 未生成预期的字幕文件：{output}")


def make_staging_output(output: Path, label: str) -> Path:
    token = f"{os.getpid()}_{uuid.uuid4().hex[:8]}"
    return output.with_name(f".{output.stem}.{label}_{token}{output.suffix}")


def remove_outputs(outputs: Sequence[Path], *, warn_on_error: bool = False) -> None:
    for output in outputs:
        try:
            output.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            if not warn_on_error:
                raise
            print(f"警告：无法删除临时字幕文件 {output}：{error}", file=sys.stderr)


def write_bytes_atomically(output: Path, content: bytes) -> None:
    staging_output = make_staging_output(output, "clean")
    try:
        staging_output.write_bytes(content)
        os.replace(staging_output, output)
    finally:
        remove_outputs([staging_output], warn_on_error=True)


def remove_alignment_tags_from_content(content: bytes) -> tuple[bytes, int]:
    removed_count = 0

    def clean_override_block(match: re.Match[bytes]) -> bytes:
        nonlocal removed_count
        inner = match.group(0)[1:-1]
        cleaned_inner, count = ALIGNMENT_TAG_RE.subn(b"", inner)
        removed_count += count
        if not cleaned_inner.strip():
            return b""
        return b"{" + cleaned_inner + b"}"

    return OVERRIDE_BLOCK_RE.sub(clean_override_block, content), removed_count


def clean_exported_alignment_tags(output: Path) -> int:
    extension = output.suffix.lower()
    if extension not in ALIGNMENT_TAG_EXTENSIONS:
        return 0

    original = output.read_bytes()
    if extension == ".srt":
        cleaned, removed_count = remove_alignment_tags_from_content(original)
    else:
        cleaned_lines: list[bytes] = []
        removed_count = 0
        for line in original.splitlines(keepends=True):
            if line.lstrip().lower().startswith(ASS_EVENT_PREFIXES):
                line, count = remove_alignment_tags_from_content(line)
                removed_count += count
            cleaned_lines.append(line)
        cleaned = b"".join(cleaned_lines)

    if cleaned != original:
        write_bytes_atomically(output, cleaned)
    return removed_count


def publish_staged_subtitle_outputs(
    staged_output: Path,
    output: Path,
    track: dict[str, Any],
) -> None:
    staged_outputs = get_related_outputs(staged_output, track)
    final_outputs = get_related_outputs(output, track)
    missing = [path for path in staged_outputs if not path.is_file()]
    if missing:
        formatted = "\n".join(f"  - {path}" for path in missing)
        fail(f"缺少待发布的字幕文件：\n{formatted}")

    for staged_path, final_path in zip(staged_outputs, final_outputs, strict=True):
        os.replace(staged_path, final_path)


def export_subtitle_copy(
    source_output: Path,
    output: Path,
    track: dict[str, Any],
) -> int:
    staging_output = make_staging_output(output, "export")
    source_outputs = get_related_outputs(source_output, track)
    staging_outputs = get_related_outputs(staging_output, track)
    try:
        for source_path, staging_path in zip(
            source_outputs,
            staging_outputs,
            strict=True,
        ):
            shutil.copyfile(source_path, staging_path)
        removed_count = clean_exported_alignment_tags(staging_output)
        publish_staged_subtitle_outputs(staging_output, output, track)
        return removed_count
    finally:
        remove_outputs(staging_outputs, warn_on_error=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        validate_binaries()
        mkv = resolve_input(args.mkv)
        tracks = identify_tracks(mkv)
        subtitle_tracks = [track for track in tracks if track.get("type") == "subtitles"]
        if not subtitle_tracks:
            fail("MKV 中没有字幕轨")

        print(f"输入文件：{mkv}")
        print_subtitle_tracks(subtitle_tracks)
        track, decisions = select_subtitle(subtitle_tracks)
        print("\n判断流程：")
        for index, decision in enumerate(decisions, start=1):
            print(f"  {index}. {decision}")

        if track is None:
            available = sorted(
                {
                    str(item.get("properties", {}).get("language", "und"))
                    for item in subtitle_tracks
                }
            )
            fail(
                "MKV 中没有 enm 或 eng 字幕轨；"
                f"现有字幕语言：{', '.join(available) if available else '未知'}"
            )

        output = get_output_path(mkv, track)
        related_outputs = get_related_outputs(output, track)

        existing = [path for path in related_outputs if path.exists()]
        if existing and not args.overwrite:
            formatted = "\n".join(f"  - {path}" for path in existing)
            fail(f"输出文件已经存在；如需覆盖，请添加 --overwrite：\n{formatted}")
        properties = track.get("properties", {})
        language = properties.get("language", "und")
        track_name = properties.get("track_name") or "（无名称）"
        codec = track.get("codec", "未知")
        print(
            f"\n最终选择：ID {track['id']}，语言 {language}，"
            f"名称 {track_name}，格式 {codec}"
        )
        print(f"选择原因：{decisions[-2] if len(decisions) >= 2 else decisions[-1]}")
        print(f"输出文件：{output}")

        staging_output = make_staging_output(output, "extract")
        staging_outputs = get_related_outputs(staging_output, track)
        try:
            extract_subtitle(mkv, track, staging_output)
            removed_count = clean_exported_alignment_tags(staging_output)
            publish_staged_subtitle_outputs(staging_output, output, track)
        finally:
            remove_outputs(staging_outputs, warn_on_error=True)

        if removed_count:
            print(f"已移除 {removed_count} 个字幕定位标记。")
        print(f"\n字幕提取完成：{output}")
        return 0
    except ExtractSubtitleError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except OSError as error:
        print(f"系统错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
