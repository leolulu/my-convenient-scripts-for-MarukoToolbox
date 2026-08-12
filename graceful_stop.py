"""可中断的子进程执行与温和停止辅助。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any


GRACEFUL_STOP_TIMEOUT = 5.0
INTERRUPT_POLL_INTERVAL = 0.1


def announce_graceful_stop() -> None:
    print(
        "\n收到 Ctrl+C，正在停止当前任务并清理半成品；"
        "再次按 Ctrl+C 将立即强制结束。",
        file=sys.stderr,
        flush=True,
    )


def process_group_creation_flags() -> int:
    """在 Windows 上为子进程创建独立进程组，以便发送 Ctrl+Break。"""
    if os.name == "nt":
        return subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def start_process(command: Sequence[object], **kwargs: Any) -> subprocess.Popen[bytes]:
    """启动一个可独立接收停止信号的子进程。"""
    if os.name == "nt" and "creationflags" not in kwargs:
        kwargs["creationflags"] = process_group_creation_flags()
    return subprocess.Popen(command, **kwargs)


def _is_running(process: subprocess.Popen[bytes]) -> bool:
    return process.poll() is None


def _request_stop(process: subprocess.Popen[bytes]) -> None:
    if not _is_running(process):
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
    except (OSError, ValueError):
        if _is_running(process):
            process.terminate()


def _force_stop(processes: Sequence[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if _is_running(process):
            try:
                process.kill()
            except OSError:
                pass
    for process in processes:
        try:
            process.wait()
        except (OSError, subprocess.SubprocessError):
            pass


def wait_process(process: subprocess.Popen[bytes]) -> int:
    """以短周期等待子进程，使 Windows 主线程能及时处理 Ctrl+C。"""
    while True:
        try:
            return process.wait(timeout=INTERRUPT_POLL_INTERVAL)
        except subprocess.TimeoutExpired:
            continue


def stop_processes(
    processes: Sequence[subprocess.Popen[bytes] | None],
    *,
    timeout: float = GRACEFUL_STOP_TIMEOUT,
) -> None:
    """请求子进程退出；超时或再次 Ctrl+C 时强制结束。"""
    active = [process for process in processes if process is not None]
    for process in active:
        _request_stop(process)

    deadline = time.monotonic() + timeout
    try:
        for process in active:
            while _is_running(process):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _force_stop(active)
                    return
                try:
                    process.wait(timeout=min(INTERRUPT_POLL_INTERVAL, remaining))
                except subprocess.TimeoutExpired:
                    continue
    except KeyboardInterrupt:
        _force_stop(active)


def run_process(command: Sequence[object], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    """以 subprocess.run 风格执行命令，并在 Ctrl+C 后完成 grace stop。"""
    check = bool(kwargs.pop("check", False))
    process = start_process(command, **kwargs)
    try:
        while True:
            try:
                stdout, stderr = process.communicate(
                    timeout=INTERRUPT_POLL_INTERVAL
                )
                break
            except subprocess.TimeoutExpired:
                continue
    except KeyboardInterrupt:
        announce_graceful_stop()
        stop_processes([process])
        raise

    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check:
        completed.check_returncode()
    return completed
