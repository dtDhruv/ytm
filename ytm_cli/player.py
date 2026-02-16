"""mpv IPC controller for ytm."""

import json
import os
import socket
import subprocess
import tempfile
import threading
import time


class MpvPlayer:
    """Controls mpv playback via JSON IPC socket."""

    def __init__(self) -> None:
        self._socket_path = os.path.join(
            tempfile.gettempdir(), f"ytm-mpv-{os.getpid()}"
        )
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def play(self, stream_url: str, title: str = "") -> bool:
        """Start playing a stream. Returns True on success."""
        self.stop()
        try:
            self._proc = subprocess.Popen(
                [
                    "mpv",
                    "--no-video",
                    "--no-terminal",
                    f"--input-ipc-server={self._socket_path}",
                    f"--title={title}",
                    stream_url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return False

        # Wait for IPC socket
        for _ in range(30):
            if os.path.exists(self._socket_path):
                break
            time.sleep(0.1)
        else:
            return False

        return self._connect()

    def _connect(self) -> bool:
        try:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._sock.connect(self._socket_path)
            self._sock.settimeout(0.2)
            return True
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            self._sock = None
            return False

    def _command(self, *args: str | float) -> dict | None:
        with self._lock:
            if not self._sock:
                return None
            try:
                msg = json.dumps({"command": list(args)}) + "\n"
                self._sock.sendall(msg.encode())
                buf = b""
                while True:
                    try:
                        chunk = self._sock.recv(4096)
                        if not chunk:
                            return None
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            data = json.loads(line.decode())
                            if "event" not in data:
                                return data
                    except socket.timeout:
                        return None
            except (BrokenPipeError, ConnectionResetError, OSError):
                self._sock = None
                return None

    def get_property(self, name: str) -> float | bool | str | None:
        result = self._command("get_property", name)
        if result and result.get("error") == "success":
            return result.get("data")
        return None

    @property
    def position(self) -> float:
        return self.get_property("playback-time") or 0.0

    @property
    def duration(self) -> float:
        return self.get_property("duration") or 0.0

    @property
    def paused(self) -> bool:
        return self.get_property("pause") or False

    @property
    def volume(self) -> float:
        return self.get_property("volume") or 100.0

    def toggle_pause(self) -> None:
        self._command("cycle", "pause")

    def seek(self, seconds: float) -> None:
        self._command("seek", seconds, "relative")

    def set_volume(self, vol: float) -> None:
        self._command("set_property", "volume", max(0.0, min(150.0, vol)))

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

        try:
            if os.path.exists(self._socket_path):
                os.unlink(self._socket_path)
        except OSError:
            pass

    def __del__(self) -> None:
        self.stop()
