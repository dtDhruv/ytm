"""ytm TUI application built with Textual."""

from __future__ import annotations

import asyncio
import json
import subprocess
from shutil import which
from typing import TYPE_CHECKING

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.widgets import DataTable, Footer, Header, Input, Label, Static
from textual.worker import get_current_worker
from rich.text import Text

from ytm_cli.player import MpvPlayer

if TYPE_CHECKING:
    from ytm_cli.net import JukeboxClient, JukeboxServer

# ── Constants ───────────────────────────────────────────────────────

WELCOME = """\
[bold magenta]
 ╦ ╦╔╦╗╔╦╗
 ╚╦╝ ║ ║║║
  ╩  ╩ ╩ ╩
[/]
[dim]stream youtube audio[/]

[dim italic]Type a query above and press Enter[/]
"""

BLOCKS = " ▏▎▍▌▋▊▉█"


# ── Helpers ─────────────────────────────────────────────────────────


def fmt_dur(sec: int | float | None) -> str:
    if not sec:
        return "--:--"
    s = int(sec)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def progress_bar(ratio: float, width: int) -> str:
    """Smooth Unicode progress bar using 1/8-block characters."""
    ratio = max(0.0, min(1.0, ratio))
    eighths = int(ratio * width * 8)
    full, partial = divmod(eighths, 8)
    empty = width - full - (1 if partial else 0)
    return "█" * full + (BLOCKS[partial] if partial else "") + "─" * max(0, empty)


def yt_search(query: str, count: int = 10) -> list[dict]:
    """Search YouTube via yt-dlp. Blocks."""
    try:
        r = subprocess.run(
            [
                "yt-dlp",
                f"ytsearch{count}:{query}",
                "--dump-json",
                "--flat-playlist",
                "--no-warnings",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    entries = []
    for line in r.stdout.strip().splitlines():
        try:
            entries.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            pass
    return entries


def get_stream(url: str) -> str | None:
    """Extract best audio URL via yt-dlp. Blocks."""
    try:
        r = subprocess.run(
            ["yt-dlp", "-f", "bestaudio", "-g", "--no-warnings", "--quiet", url],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return r.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


# ── App ─────────────────────────────────────────────────────────────

APP_CSS = """
Screen {
    background: $surface;
}

#search {
    dock: top;
    height: 3;
}

#main {
    height: 1fr;
}

#results-panel {
    width: 1fr;
}

#welcome {
    width: 100%;
    height: 100%;
    content-align: center middle;
    text-align: center;
}

#results {
    height: 1fr;
}

.hidden {
    display: none;
}

#queue-panel {
    width: 38;
    display: none;
    border-left: tall $accent;
}

#queue-panel.visible {
    display: block;
}

#queue-title {
    text-style: bold;
    color: $warning;
    padding: 1 0 0 1;
    height: 3;
}

#queue-list {
    height: 1fr;
}

#np {
    height: auto;
    max-height: 4;
    background: $boost;
    border-top: tall magenta 50%;
    padding: 0 2;
    display: none;
}

#np.active {
    display: block;
}
"""


class YtmApp(App):
    TITLE = "ytm"
    SUB_TITLE = "stream youtube audio"
    CSS = APP_CSS

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "focus_search", "Search"),
        Binding("tab", "toggle_queue", "Queue", priority=True),
        Binding("space", "play_pause", "Play/Pause"),
        Binding("a", "queue_add", "+Queue"),
        Binding("n", "next_track", "Next"),
        Binding("d", "queue_remove", show=False),
        Binding("escape", "focus_results", show=False),
        Binding("left", "seek_back", show=False),
        Binding("right", "seek_fwd", show=False),
    ]

    def __init__(
        self,
        play_url: str | None = None,
        mode: str = "local",
        server: JukeboxServer | None = None,
        client: JukeboxClient | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._mode = mode  # "local" | "host" | "client"
        self._server = server
        self._client = client
        self.player = MpvPlayer() if mode != "client" else None
        self._results: list[dict] = []
        self._queue: list[dict] = []
        self._track: dict | None = None
        self._loading = False
        self._play_url = play_url

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Search YouTube…", id="search")
        with Horizontal(id="main"):
            with Container(id="results-panel"):
                yield Static(WELCOME, id="welcome")
                yield DataTable(id="results", classes="hidden")
            with Container(id="queue-panel"):
                yield Label("Queue", id="queue-title")
                yield DataTable(id="queue-list")
        yield Static(id="np")
        yield Footer()

    def on_mount(self) -> None:
        # Results table
        t = self.query_one("#results", DataTable)
        t.add_columns("#", "Title", "Channel", "Duration")
        t.cursor_type = "row"

        # Queue table
        q = self.query_one("#queue-list", DataTable)
        q.add_columns("#", "Title", "Duration")
        q.cursor_type = "row"

        # Dependency check
        deps = ["yt-dlp"] if self._mode == "client" else ["yt-dlp", "mpv"]
        missing = [c for c in deps if not which(c)]
        if missing:
            self.notify(f"Missing: {', '.join(missing)}", severity="error", timeout=10)

        # Progress timer
        self.set_interval(0.5, self._tick)

        # Network setup
        if self._mode == "host" and self._server:
            self._server.on_add = self._on_remote_add
            asyncio.get_event_loop().create_task(self._server.start())
            self.sub_title = f"hosting on :{self._server.port}"

        if self._mode == "client" and self._client:
            self._start_client_listener()

        # Auto-play URL if provided
        if self._play_url and self._mode != "client":
            self._play_from_url(self._play_url)

    # ── Search ──────────────────────────────────────────────────

    @on(Input.Submitted, "#search")
    def _on_search(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if not query:
            return
        self._do_search(query)

    @work(thread=True, exclusive=True, group="search")
    def _do_search(self, query: str) -> None:
        self.call_from_thread(setattr, self, "sub_title", "searching…")
        entries = yt_search(query)
        if not get_current_worker().is_cancelled:
            self.call_from_thread(self._populate_results, entries)
            self.call_from_thread(setattr, self, "sub_title", "stream youtube audio")

    def _populate_results(self, entries: list[dict]) -> None:
        self._results = entries
        self.query_one("#welcome").add_class("hidden")
        table = self.query_one("#results", DataTable)
        table.remove_class("hidden")
        table.clear()

        for i, e in enumerate(entries, 1):
            table.add_row(
                Text(str(i), style="bold cyan"),
                Text(e.get("title", "Unknown")),
                Text(e.get("channel", e.get("uploader", "?")), style="green"),
                Text(fmt_dur(e.get("duration")), style="dim"),
                key=str(i),
            )

        if entries:
            table.focus()
            table.move_cursor(row=0)
        else:
            self.notify("No results found", severity="warning")

    # ── Playback ────────────────────────────────────────────────

    @on(DataTable.RowSelected, "#results")
    def _on_result_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if 0 <= idx < len(self._results):
            entry = self._results[idx]
            if self._mode == "client" and self._client:
                asyncio.get_event_loop().create_task(self._client.send_add(entry))
                self.notify(f"Sent: {entry.get('title', '?')}", timeout=2)
            else:
                self._play_entry(entry)

    @on(DataTable.RowSelected, "#queue-list")
    def _on_queue_selected(self, event: DataTable.RowSelected) -> None:
        if self._mode == "client":
            return
        idx = event.cursor_row
        if 0 <= idx < len(self._queue):
            track = self._queue.pop(idx)
            self._refresh_queue()
            self._play_entry(track)

    @work(thread=True, exclusive=True, group="play")
    def _play_entry(self, entry: dict) -> None:
        """Fetch stream URL and start mpv."""
        self._loading = True
        self._track = entry
        vid = entry.get("id", entry.get("url", ""))
        title = entry.get("title", "Unknown")
        url = f"https://www.youtube.com/watch?v={vid}"

        self.call_from_thread(self._show_np_loading, title)

        stream = get_stream(url)
        if get_current_worker().is_cancelled:
            self._loading = False
            return

        if not stream:
            self._track = None
            self._loading = False
            self.call_from_thread(self._hide_np)
            self.call_from_thread(
                self.notify, "Failed to get audio stream", severity="error"
            )
            return

        self.player.play(stream, title)
        self._loading = False

    @work(thread=True, exclusive=True, group="play")
    def _play_from_url(self, url: str) -> None:
        """Fetch info for a URL then play it."""
        self._loading = True
        entry: dict = {"id": url, "title": url, "channel": "—", "duration": None}
        try:
            r = subprocess.run(
                ["yt-dlp", "--dump-json", "--no-warnings", "--quiet", url],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if r.stdout.strip():
                entry = json.loads(r.stdout.strip())
        except Exception:
            pass

        self._track = entry
        title = entry.get("title", "Unknown")
        vid = entry.get("id", url)
        yt_url = f"https://www.youtube.com/watch?v={vid}"

        self.call_from_thread(self._show_np_loading, title)

        stream = get_stream(yt_url)
        if get_current_worker().is_cancelled:
            self._loading = False
            return

        if not stream:
            self._track = None
            self._loading = False
            self.call_from_thread(self._hide_np)
            self.call_from_thread(
                self.notify, "Failed to get audio stream", severity="error"
            )
            return

        self.player.play(stream, title)
        self._loading = False

    def _show_np_loading(self, title: str) -> None:
        np = self.query_one("#np", Static)
        np.add_class("active")
        np.update(f"  [dim]Loading:[/] [bold]{title}[/]")

    def _hide_np(self) -> None:
        np = self.query_one("#np", Static)
        np.remove_class("active")

    # ── Progress tick ───────────────────────────────────────────

    def _tick(self) -> None:
        """Called every 0.5s to update now-playing bar and detect track end."""
        # Client gets state from network, not local player
        if self._mode == "client":
            return

        pos = 0.0
        dur = 0.0
        paused = False

        if self._track and self.player:
            # Track ended? (but not if we're still fetching the stream)
            if not self.player.is_running and not self._loading:
                self._track = None
                if self._queue:
                    nxt = self._queue.pop(0)
                    self._refresh_queue()
                    self._play_entry(nxt)
                else:
                    self._hide_np()
            else:
                # Update progress
                pos = self.player.position
                dur = self.player.duration
                paused = self.player.paused
                self._render_np(self._track, pos, dur, paused)

        # Broadcast state to clients (host mode)
        if self._mode == "host" and self._server:
            n = self._server.client_count
            self.sub_title = (
                f"hosting on :{self._server.port} · {n} client{'s' * (n != 1)}"
                if n
                else f"hosting on :{self._server.port}"
            )
            self._server.broadcast(
                {
                    "type": "sync",
                    "queue": self._queue,
                    "track": self._track,
                    "position": pos,
                    "duration": dur,
                    "paused": paused,
                }
            )

    def _render_np(
        self, track: dict, pos: float, dur: float, paused: bool
    ) -> None:
        """Render the now-playing bar from given state."""
        title = track.get("title", "Unknown")
        artist = track.get("channel", track.get("uploader", "?"))

        w = max(self.size.width - 6, 20)
        ratio = pos / dur if dur > 0 else 0
        pbar = progress_bar(ratio, w)
        icon = "⏸" if paused else "♫"
        ts = f"{fmt_dur(pos)} / {fmt_dur(dur)}"
        qi = f"  [dim]Queue: {len(self._queue)}[/]" if self._queue else ""

        np_widget = self.query_one("#np", Static)
        np_widget.add_class("active")
        np_widget.update(
            f"  {icon} [bold]{title}[/] · [green]{artist}[/]{qi}\n"
            f"  [magenta]{pbar}[/]  [dim]{ts}[/]"
        )

    # ── Queue ───────────────────────────────────────────────────

    def _refresh_queue(self) -> None:
        qt = self.query_one("#queue-list", DataTable)
        qt.clear()
        for i, e in enumerate(self._queue, 1):
            qt.add_row(
                Text(str(i), style="bold cyan"),
                Text(e.get("title", "Unknown")),
                Text(fmt_dur(e.get("duration")), style="dim"),
            )
        lbl = self.query_one("#queue-title", Label)
        n = len(self._queue)
        lbl.update(f"Queue · {n}" if n else "Queue")

    # ── Actions ─────────────────────────────────────────────────

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_focus_results(self) -> None:
        t = self.query_one("#results", DataTable)
        if not t.has_class("hidden"):
            t.focus()

    def action_toggle_queue(self) -> None:
        panel = self.query_one("#queue-panel", Container)
        panel.toggle_class("visible")
        if panel.has_class("visible"):
            self.query_one("#queue-list", DataTable).focus()
        else:
            self.action_focus_results()

    def action_play_pause(self) -> None:
        if self._mode == "client":
            return
        if self.player and self.player.is_running:
            self.player.toggle_pause()
        else:
            # Play highlighted result
            t = self.query_one("#results", DataTable)
            if not t.has_class("hidden") and self._results:
                idx = t.cursor_row
                if 0 <= idx < len(self._results):
                    self._play_entry(self._results[idx])

    def action_queue_add(self) -> None:
        t = self.query_one("#results", DataTable)
        if t.has_class("hidden") or not self._results:
            return
        idx = t.cursor_row
        if 0 <= idx < len(self._results):
            entry = self._results[idx]
            if self._mode == "client" and self._client:
                asyncio.get_event_loop().create_task(self._client.send_add(entry))
                self.notify(f"Sent: {entry.get('title', '?')}", timeout=2)
            else:
                self._queue.append(entry)
                self.notify(f"+ {entry.get('title', '?')}", timeout=2)
                self._refresh_queue()

    def action_queue_remove(self) -> None:
        if self._mode == "client":
            return
        qt = self.query_one("#queue-list", DataTable)
        if not qt.has_focus or not self._queue:
            return
        idx = qt.cursor_row
        if 0 <= idx < len(self._queue):
            removed = self._queue.pop(idx)
            self.notify(f"− {removed.get('title', '?')}", timeout=2)
            self._refresh_queue()

    def action_next_track(self) -> None:
        if self._mode == "client":
            return
        if self._queue:
            track = self._queue.pop(0)
            self._refresh_queue()
            self._play_entry(track)
        else:
            self.notify("Queue is empty", timeout=2)

    def action_seek_fwd(self) -> None:
        if self._mode == "client":
            return
        if self.player and self.player.is_running:
            self.player.seek(10)

    def action_seek_back(self) -> None:
        if self._mode == "client":
            return
        if self.player and self.player.is_running:
            self.player.seek(-10)

    def action_quit(self) -> None:
        if self.player:
            self.player.stop()
        if self._server:
            asyncio.get_event_loop().create_task(self._server.stop())
        if self._client:
            asyncio.get_event_loop().create_task(self._client.close())
        self.exit()

    # ── Network (host mode) ────────────────────────────────────

    def _on_remote_add(self, entry: dict) -> None:
        """Called by JukeboxServer when a client adds a track."""
        self._queue.append(entry)
        self._refresh_queue()
        self.notify(f"+ {entry.get('title', '?')} [dim](remote)[/]", timeout=2)

    # ── Network (client mode) ──────────────────────────────────

    @work(exclusive=True, group="net")
    async def _start_client_listener(self) -> None:
        """Async worker: connect to host and listen for state updates."""
        assert self._client is not None
        self._client.on_sync = self._apply_sync
        if not await self._client.connect():
            self.notify("Failed to connect to host", severity="error", timeout=10)
            return
        self.sub_title = f"connected to {self._client.host}:{self._client.port}"
        self.notify("Connected to host", timeout=3)
        await self._client.listen()
        # Disconnected
        self.sub_title = "disconnected"
        self.notify("Disconnected from host", severity="error", timeout=10)

    def _apply_sync(self, state: dict) -> None:
        """Apply a state snapshot received from the host.

        Called from the async event loop (not a thread), so we can
        touch widgets directly.
        """
        self._queue = state.get("queue", [])
        self._track = state.get("track")
        self._refresh_queue()

        if self._track:
            pos = state.get("position", 0)
            dur = state.get("duration", 0)
            paused = state.get("paused", False)
            self._render_np(self._track, pos, dur, paused)
        else:
            self._hide_np()
