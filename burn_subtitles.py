#!/usr/bin/env python3
"""使用小丸工具箱自带的二进制文件将外挂字幕烧录到视频中。"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import NoReturn, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
TOOLS_DIR = ROOT_DIR / "tools"
TEMP_DIR = ROOT_DIR / "temp"

FFMPEG = TOOLS_DIR / "ffmpeg.exe"
NERO_AAC = TOOLS_DIR / "neroAacEnc.exe"
X264 = TOOLS_DIR / "x264_64-8bit.exe"
MP4BOX = TOOLS_DIR / "MP4Box.exe"

VIDEO_STREAM_RE = re.compile(r"^\s*Stream .+Video:", re.MULTILINE)
AUDIO_STREAM_RE = re.compile(r"^\s*Stream .+Audio:", re.MULTILINE)
FPS_RE = re.compile(r"(?:,|\s)(\d+(?:\.\d+)?)\s*fps(?:,|\s)", re.IGNORECASE)
TBR_RE = re.compile(r"(?:,|\s)(\d+(?:\.\d+)?)\s*tbr(?:,|\s)", re.IGNORECASE)


class BurnSubtitlesError(RuntimeError):
    """字幕压制流程失败。"""


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return number


def crf_value(value: str) -> float:
    number = float(value)
    if not 0 <= number <= 51:
        raise argparse.ArgumentTypeError("CRF 必须在 0 到 51 之间")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "调用小丸工具箱自带的 ffmpeg、Nero AAC、x264 和 MP4Box，"
            "将 ASS/SSA/SRT 等外挂字幕烧录到 MP4 视频中。"
        )
    )
    parser.add_argument("video", type=Path, help="输入视频文件")
    parser.add_argument("subtitle", type=Path, help="外挂字幕文件")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="输出 MP4；默认是输入视频同目录下的 <原名>_x264.mp4",
    )
    parser.add_argument(
        "--crf",
        type=crf_value,
        default=24.0,
        help="x264 CRF，默认 24.0",
    )
    parser.add_argument(
        "--audio-bitrate",
        type=positive_int,
        default=128,
        metavar="KBPS",
        help="Nero AAC-LC 音频码率，默认 128 kbps",
    )
    parser.add_argument(
        "--keyint",
        type=positive_int,
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
        help="允许覆盖已经存在的输出文件",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="完成或失败后保留 temp 目录中的中间音视频文件",
    )
    return parser


def fail(message: str) -> NoReturn:
    raise BurnSubtitlesError(message)


def resolve_input(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        fail(f"{label}不存在或不是文件：{resolved}")
    return resolved


def resolve_output(video: Path, output: Path | None) -> Path:
    if output is None:
        resolved = video.with_name(f"{video.stem}_x264.mp4")
    else:
        resolved = output.expanduser().resolve()

    if resolved.suffix.lower() != ".mp4":
        fail(f"输出文件必须使用 .mp4 扩展名：{resolved}")
    if not resolved.parent.is_dir():
        fail(f"输出目录不存在：{resolved.parent}")
    if resolved.exists() and not resolved.is_file():
        fail(f"输出路径已经存在且不是文件：{resolved}")
    if resolved == video:
        fail("输出文件不能与输入视频相同")
    return resolved


def validate_binaries() -> None:
    missing = [path for path in (FFMPEG, NERO_AAC, X264, MP4BOX) if not path.is_file()]
    if missing:
        formatted = "\n".join(f"  - {path}" for path in missing)
        fail(f"缺少小丸工具箱二进制文件：\n{formatted}")


def display_command(command: Sequence[os.PathLike[str] | str]) -> str:
    return subprocess.list2cmdline([os.fspath(argument) for argument in command])


def normalize_y4m_header(header: bytes) -> bytes:
    if not header.startswith(b"YUV4MPEG2 "):
        fail("外部 ffmpeg 未输出有效的 Y4M 视频头")

    fields = header.rstrip(b"\r\n").split(b" ")
    fields = [
        field
        for field in fields
        if not field.startswith((b"XYSCSS=", b"XCOLORRANGE="))
    ]
    return b" ".join(fields) + b"\n"


def run_command(command: Sequence[os.PathLike[str] | str], stage: str) -> None:
    print(f"\n[{stage}]\n{display_command(command)}", flush=True)
    result = subprocess.run(command, cwd=ROOT_DIR, check=False)
    if result.returncode != 0:
        fail(f"{stage}失败，退出码：{result.returncode}")


def probe_video(video: Path) -> tuple[float | None, bool, bool]:
    command = [FFMPEG, "-i", video]
    result = subprocess.run(
        command,
        cwd=ROOT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    probe_text = result.stderr.decode("utf-8", errors="replace")

    video_line = next(
        (line for line in probe_text.splitlines() if VIDEO_STREAM_RE.search(line)),
        None,
    )
    if video_line is None:
        fail("ffmpeg 未检测到视频流")

    match = FPS_RE.search(video_line) or TBR_RE.search(video_line)
    if match is None:
        frame_rate = None
    else:
        frame_rate = float(match.group(1))
        if frame_rate <= 0:
            fail(f"ffmpeg 返回了无效帧率：{frame_rate}")

    bundled_decoder_supported = not re.search(
        r"Video:\s*(?:none|unknown)\b", video_line, re.IGNORECASE
    )
    return (
        frame_rate,
        bool(AUDIO_STREAM_RE.search(probe_text)),
        bundled_decoder_supported,
    )


def resolve_fallback_ffmpeg(path: Path | None) -> Path:
    if path is not None:
        fallback_ffmpeg = path.expanduser().resolve()
    else:
        discovered = shutil.which("ffmpeg")
        if discovered is None:
            fail(
                "小丸内置解码器不支持输入视频，且 PATH 中未找到外部 ffmpeg；"
                "请通过 --fallback-ffmpeg 指定"
            )
        fallback_ffmpeg = Path(discovered).resolve()

    if not fallback_ffmpeg.is_file():
        fail(f"外部 ffmpeg 不存在或不是文件：{fallback_ffmpeg}")
    if fallback_ffmpeg == FFMPEG.resolve():
        fail("兜底解码需要外部新版 ffmpeg，不能继续使用小丸内置的旧版本")
    return fallback_ffmpeg


def validate_fallback_decoder(fallback_ffmpeg: Path, video: Path) -> None:
    command = [
        fallback_ffmpeg,
        "-v",
        "error",
        "-nostdin",
        "-i",
        video,
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(
        command,
        cwd=ROOT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        details = result.stderr.decode("utf-8", errors="replace").strip()
        if details:
            details = f"\n{details}"
        fail(f"外部 ffmpeg 无法解码输入视频，退出码：{result.returncode}{details}")


def encode_audio(video: Path, audio_temp: Path, bitrate_kbps: int) -> None:
    ffmpeg_command = [
        FFMPEG,
        "-i",
        video,
        "-vn",
        "-sn",
        "-v",
        "0",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        "pipe:",
    ]
    nero_command = [
        NERO_AAC,
        "-ignorelength",
        "-lc",
        "-br",
        str(bitrate_kbps * 1000),
        "-if",
        "-",
        "-of",
        audio_temp,
    ]

    print(
        f"\n[压制音频]\n{display_command(ffmpeg_command)}"
        f" | {display_command(nero_command)}",
        flush=True,
    )

    ffmpeg_process = subprocess.Popen(
        ffmpeg_command,
        cwd=ROOT_DIR,
        stdout=subprocess.PIPE,
    )
    assert ffmpeg_process.stdout is not None
    nero_process: subprocess.Popen[bytes] | None = None

    try:
        nero_process = subprocess.Popen(
            nero_command,
            cwd=ROOT_DIR,
            stdin=ffmpeg_process.stdout,
        )
        ffmpeg_process.stdout.close()
        nero_returncode = nero_process.wait()
        ffmpeg_returncode = ffmpeg_process.wait()
    except BaseException:
        if nero_process is not None and nero_process.poll() is None:
            nero_process.terminate()
        if ffmpeg_process.poll() is None:
            ffmpeg_process.terminate()
        if nero_process is not None:
            nero_process.wait()
        ffmpeg_process.wait()
        raise

    if ffmpeg_returncode != 0 or nero_returncode != 0:
        fail(
            "音频压制失败，"
            f"ffmpeg 退出码：{ffmpeg_returncode}，Nero AAC 退出码：{nero_returncode}"
        )


def encode_video(
    video: Path,
    subtitle: Path,
    video_temp: Path,
    crf: float,
    keyint: int,
    fallback_ffmpeg: Path | None,
) -> None:
    x264_command = [
        X264,
        "--crf",
        str(crf),
        "--preset",
        "8",
        "-I",
        str(keyint),
        "-r",
        "4",
        "-b",
        "3",
        "--me",
        "umh",
        "-i",
        "1",
        "--scenecut",
        "60",
        "-f",
        "1:1",
        "--qcomp",
        "0.5",
        "--psy-rd",
        "0.3:0",
        "--aq-mode",
        "2",
        "--aq-strength",
        "0.8",
        "--vf",
        "subtitles",
        "--sub",
        subtitle,
        "-o",
        video_temp,
    ]

    if fallback_ffmpeg is None:
        x264_command.append(video)
        run_command(x264_command, "烧录字幕并压制视频")
        return

    decoder_command = [
        fallback_ffmpeg,
        "-v",
        "error",
        "-nostdin",
        "-i",
        video,
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "yuv4mpegpipe",
        "-",
    ]
    x264_command.extend(("--demuxer", "y4m", "-"))

    print(
        f"\n[外部解码、烧录字幕并压制视频]\n{display_command(decoder_command)}"
        f" | {display_command(x264_command)}",
        flush=True,
    )

    decoder_process = subprocess.Popen(
        decoder_command,
        cwd=ROOT_DIR,
        stdout=subprocess.PIPE,
    )
    assert decoder_process.stdout is not None
    x264_process: subprocess.Popen[bytes] | None = None

    try:
        x264_process = subprocess.Popen(
            x264_command,
            cwd=ROOT_DIR,
            stdin=subprocess.PIPE,
        )
        assert x264_process.stdin is not None

        y4m_header = decoder_process.stdout.readline()
        x264_process.stdin.write(normalize_y4m_header(y4m_header))
        try:
            shutil.copyfileobj(decoder_process.stdout, x264_process.stdin)
        except BrokenPipeError:
            pass
        finally:
            try:
                x264_process.stdin.close()
            except BrokenPipeError:
                pass
            decoder_process.stdout.close()

        x264_returncode = x264_process.wait()
        decoder_returncode = decoder_process.wait()
    except BaseException:
        if x264_process is not None and x264_process.poll() is None:
            x264_process.terminate()
        if decoder_process.poll() is None:
            decoder_process.terminate()
        if x264_process is not None:
            x264_process.wait()
        decoder_process.wait()
        raise

    if decoder_returncode != 0 or x264_returncode != 0:
        fail(
            "视频压制失败，"
            f"外部 ffmpeg 退出码：{decoder_returncode}，x264 退出码：{x264_returncode}"
        )


def mux_mp4(video_temp: Path, audio_temp: Path, output: Path) -> None:
    command = [
        MP4BOX,
        "-add",
        f"{video_temp}#trackID=1:name=",
        "-add",
        f"{audio_temp}#trackID=1:name=",
        "-new",
        output,
    ]
    run_command(command, "封装 MP4")


def remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        print(f"警告：无法删除中间文件 {path}：{error}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        validate_binaries()
        video = resolve_input(args.video, "输入视频")
        subtitle = resolve_input(args.subtitle, "字幕文件")
        output = resolve_output(video, args.output)

        if output.exists() and not args.overwrite:
            fail(f"输出文件已经存在；如需覆盖，请添加 --overwrite：{output}")

        frame_rate, has_audio, bundled_decoder_supported = probe_video(video)
        if not has_audio:
            fail("输入视频不包含音频流，当前脚本无法执行“压制音频”流程")

        if args.keyint is None and frame_rate is None:
            fail("无法从 ffmpeg 输出中识别视频帧率，请使用 --keyint 手动指定关键帧间隔")

        keyint = args.keyint or max(1, round(frame_rate * 10))
        print(f"输入视频：{video}")
        print(f"字幕文件：{subtitle}")
        print(f"输出文件：{output}")
        if frame_rate is None:
            print(f"检测帧率：无法识别；使用指定的关键帧间隔：{keyint}")
        else:
            print(f"检测帧率：{frame_rate:g} fps；关键帧间隔：{keyint}")

        fallback_ffmpeg = None
        if not bundled_decoder_supported:
            fallback_ffmpeg = resolve_fallback_ffmpeg(args.fallback_ffmpeg)
            validate_fallback_decoder(fallback_ffmpeg, video)
            print(f"小丸内置解码器不支持此视频；启用外部兜底：{fallback_ffmpeg}")

        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        temp_id = f"burn_subtitles_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        audio_temp = TEMP_DIR / f"{temp_id}_atemp.mp4"
        video_temp = TEMP_DIR / f"{temp_id}_vtemp.mp4"

        completed = False
        mux_started = False
        try:
            encode_audio(video, audio_temp, args.audio_bitrate)
            encode_video(
                video,
                subtitle,
                video_temp,
                args.crf,
                keyint,
                fallback_ffmpeg,
            )

            if output.exists():
                output.unlink()
            mux_started = True
            mux_mp4(video_temp, audio_temp, output)
            completed = True
        finally:
            if not args.keep_temp:
                remove_file(video_temp)
                remove_file(audio_temp)
            elif audio_temp.exists() or video_temp.exists():
                print(f"\n已保留中间文件：{TEMP_DIR}")

            if not completed and mux_started and output.exists():
                remove_file(output)

        print(f"\n压制完成：{output}")
        return 0
    except BurnSubtitlesError as error:
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
