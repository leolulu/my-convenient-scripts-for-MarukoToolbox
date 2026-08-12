from __future__ import annotations

import subprocess
import unittest
from unittest import mock

import graceful_stop


class FakeProcess:
    def __init__(self, *, wait_effect: BaseException | None = None) -> None:
        self.returncode: int | None = None
        self.wait_effect = wait_effect
        self.signals: list[int] = []
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, signal_number: int) -> None:
        self.signals.append(signal_number)

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.wait_effect is not None:
            effect = self.wait_effect
            self.wait_effect = None
            raise effect
        self.returncode = 0
        return self.returncode


class CommunicateInterruptProcess(FakeProcess):
    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        raise KeyboardInterrupt


class GracefulStopTests(unittest.TestCase):
    def test_windows_requests_ctrl_break_and_waits(self) -> None:
        process = FakeProcess()

        with mock.patch.object(graceful_stop.os, "name", "nt"):
            graceful_stop.stop_processes([process])

        self.assertEqual(process.signals, [graceful_stop.signal.CTRL_BREAK_EVENT])
        self.assertFalse(process.killed)
        self.assertEqual(process.returncode, 0)

    def test_timeout_force_kills_process(self) -> None:
        process = FakeProcess()

        with mock.patch.object(graceful_stop.os, "name", "nt"):
            graceful_stop.stop_processes([process], timeout=0.0)

        self.assertTrue(process.killed)

    def test_second_keyboard_interrupt_force_kills_process(self) -> None:
        process = FakeProcess(wait_effect=KeyboardInterrupt())

        with mock.patch.object(graceful_stop.os, "name", "nt"):
            graceful_stop.stop_processes([process])

        self.assertTrue(process.killed)

    def test_run_process_stops_child_and_reraises_keyboard_interrupt(self) -> None:
        process = CommunicateInterruptProcess()

        with (
            mock.patch.object(graceful_stop, "start_process", return_value=process),
            mock.patch.object(graceful_stop, "stop_processes") as stop_processes,
        ):
            with self.assertRaises(KeyboardInterrupt):
                graceful_stop.run_process(
                    ["converter"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )

        stop_processes.assert_called_once_with([process])

    def test_run_process_returns_captured_output(self) -> None:
        process = mock.Mock()
        process.communicate.return_value = (b"out", b"err")
        process.returncode = 0

        with mock.patch.object(graceful_stop, "start_process", return_value=process):
            result = graceful_stop.run_process(
                ["converter"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"out")
        self.assertEqual(result.stderr, b"err")


if __name__ == "__main__":
    unittest.main()
