#!/usr/bin/env python3
"""ytm - Stream YouTube audio from your terminal."""

import click

from ytm_cli import __version__
from ytm_cli.app import YtmApp


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="ytm")
@click.pass_context
def cli(ctx: click.Context) -> None:
    """ytm - Stream YouTube audio from your terminal."""
    if ctx.invoked_subcommand is None:
        YtmApp().run()


@cli.command(name="play")
@click.argument("url")
def play_url(url: str) -> None:
    """Play a YouTube URL directly."""
    YtmApp(play_url=url).run()


@cli.command()
@click.option("--port", "-p", default=7685, show_default=True, help="Port to listen on.")
def host(port: int) -> None:
    """Host a shared jukebox queue on the LAN."""
    from ytm_cli.net import JukeboxServer

    server = JukeboxServer(port=port)
    YtmApp(mode="host", server=server).run()


@cli.command()
@click.argument("address")
@click.option("--port", "-p", default=7685, show_default=True, help="Port to connect to.")
def join(address: str, port: int) -> None:
    """Join a shared jukebox queue on the LAN."""
    from ytm_cli.net import JukeboxClient

    client = JukeboxClient(host=address, port=port)
    YtmApp(mode="client", client=client).run()


def main() -> None:
    cli()
