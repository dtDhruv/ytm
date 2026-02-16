"""LAN shared queue networking for ytm."""

import asyncio
import json
import logging
from typing import Callable

DEFAULT_PORT = 7685

log = logging.getLogger(__name__)


# ── Framing ─────────────────────────────────────────────────────────


def encode_msg(msg: dict) -> bytes:
    """Encode a message as newline-delimited JSON."""
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode()


async def read_msg(reader: asyncio.StreamReader) -> dict | None:
    """Read one newline-delimited JSON message. Returns None on EOF/error."""
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=30.0)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        return None
    if not line:
        return None
    try:
        return json.loads(line.decode())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


# ── Server ──────────────────────────────────────────────────────────


class JukeboxServer:
    """TCP server that broadcasts host state to connected clients."""

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self._clients: dict[asyncio.Task, asyncio.StreamWriter] = {}
        self._server: asyncio.Server | None = None
        self.on_add: Callable[[dict], None] | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, "0.0.0.0", self.port
        )
        log.info("Jukebox server listening on port %d", self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for task, writer in list(self._clients.items()):
            task.cancel()
            writer.close()
        self._clients.clear()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        addr = writer.get_extra_info("peername")
        log.info("Client connected: %s", addr)
        task = asyncio.current_task()
        self._clients[task] = writer
        try:
            while True:
                msg = await read_msg(reader)
                if msg is None:
                    break
                self._process(msg, writer)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            log.info("Client disconnected: %s", addr)
            self._clients.pop(task, None)
            try:
                writer.close()
            except OSError:
                pass

    def _process(self, msg: dict, writer: asyncio.StreamWriter) -> None:
        mtype = msg.get("type")
        if mtype == "add" and isinstance(msg.get("entry"), dict):
            entry = msg["entry"]
            # Validate required keys
            if "id" in entry and "title" in entry:
                if self.on_add:
                    self.on_add(entry)
                try:
                    writer.write(
                        encode_msg({"type": "ack", "title": entry.get("title", "?")})
                    )
                except (ConnectionError, OSError):
                    pass

    def broadcast(self, msg: dict) -> None:
        """Send a message to all connected clients. Fire-and-forget."""
        if not self._clients:
            return
        data = encode_msg(msg)
        dead: list[asyncio.Task] = []
        for task, writer in self._clients.items():
            try:
                writer.write(data)
            except (ConnectionError, OSError):
                dead.append(task)
        for task in dead:
            self._clients.pop(task, None)

    @property
    def client_count(self) -> int:
        return len(self._clients)


# ── Client ──────────────────────────────────────────────────────────


class JukeboxClient:
    """TCP client that connects to a host and receives state updates."""

    def __init__(self, host: str, port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self.on_sync: Callable[[dict], None] | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=5.0,
            )
            self._connected = True
            self._writer.write(encode_msg({"type": "hello"}))
            await self._writer.drain()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    async def listen(self) -> None:
        """Read messages from host until disconnected."""
        while self._reader and self._connected:
            msg = await read_msg(self._reader)
            if msg is None:
                self._connected = False
                break
            if msg.get("type") == "sync" and self.on_sync:
                self.on_sync(msg)

    async def send_add(self, entry: dict) -> None:
        if self._writer and self._connected:
            try:
                self._writer.write(encode_msg({"type": "add", "entry": entry}))
                await self._writer.drain()
            except (ConnectionError, OSError):
                self._connected = False

    async def close(self) -> None:
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
            except OSError:
                pass
