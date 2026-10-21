#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
from pathlib import Path

import click

from bdfr import __version__, logs
from bdfr.configuration import Configuration
from bdfr.downloader import Downloader
from bdfr.renamer import Renamer, rename_directory
from bdfr.server import DEFAULT_PORT, run_server

logger = logging.getLogger()

_common_options = [
    click.argument("directory", type=str),
    click.option("--config", type=str, default=None),
    click.option("--disable-module", multiple=True, default=None, type=str),
    click.option("--exclude-id", default=None, multiple=True),
    click.option("--exclude-id-file", default=None, multiple=True),
    click.option("--file-scheme", default=None, type=str),
    click.option("--filename-restriction-scheme", type=click.Choice(("linux", "windows")), default=None),
    click.option(
        "--filename-character-set", type=click.Choice(("ascii", "unicode", "unicode_no_emojis")), default=None
    ),
    click.option("--folder-scheme", default=None, type=str),
    click.option("--ignore-user", type=str, multiple=True, default=None),
    click.option("--include-id-file", multiple=True, default=None),
    click.option("--log", type=str, default=None),
    click.option("--opts", type=str, default=None),
    click.option("--submitted", is_flag=True, default=None),
    click.option("--time-format", type=str, default=None),
    click.option("--fail-fast", is_flag=True, default=None),
    click.option("-L", "--limit", default=None, type=int),
    click.option("-l", "--link", multiple=True, default=None, type=str),
    click.option("-s", "--subreddit", multiple=True, default=None, type=str),
    click.option("-t", "--time", type=click.Choice(("all", "hour", "day", "week", "month", "year")), default=None),
    click.option("-u", "--user", type=str, multiple=True, default=None),
    click.option("-v", "--verbose", default=None, count=True),
]

_downloader_options = [
    click.option("--make-hard-links", is_flag=True, default=None),
    click.option("--max-wait-time", type=int, default=None),
    click.option("--no-dupes", is_flag=True, default=None),
    click.option("--search-existing", is_flag=True, default=None),
    click.option("--keep-hashes", is_flag=True, default=None),
    click.option("--keep-hashes-db", is_flag=True, default=None),
    click.option("--save-hashes-interval", type=click.IntRange(10, 1000), default=None),
    click.option("--stop-at-existing", type=click.IntRange(1), default=None),
    click.option(
        "--parallel",
        type=click.IntRange(1, 16),
        default=None,
        help="Download this many posts at once (default 1). Duplicate checks and file writes stay one at a time.",
    ),
    click.option(
        "--media-only",
        is_flag=True,
        default=None,
        help="Skip self posts and crossposts.",
    ),
    click.option(
        "--rename-existing",
        is_flag=True,
        default=None,
        help="Rename already downloaded files of a post to the current --file-scheme when they are found again.",
    ),
    click.option("--post-json", multiple=True, default=None, type=str),
    click.option("--skip", default=None, multiple=True),
    click.option("--skip-domain", default=None, multiple=True),
    click.option("--skip-subreddit", default=None, multiple=True),
    click.option("--min-score", type=int, default=None),
    click.option("--max-score", type=int, default=None),
    click.option("--min-score-ratio", type=float, default=None),
    click.option("--max-score-ratio", type=float, default=None),
    click.option("--imgur-originals", is_flag=True, default=None),
    click.option("--imgur-fix404", is_flag=True, default=None),
]


def _add_options(opts: list):
    def wrap(func):
        # Decorators apply bottom-up, so reverse to keep --help in the listed order
        for opt in reversed(opts):
            func = opt(func)
        return func

    return wrap


def _check_version(context, param, value):
    if not value or context.resilient_parsing:
        return
    # This fork is not published, so PyPI's bdfr version says nothing about it
    print(f"bdfr (server fork) v{__version__}")
    context.exit()


@click.group()
@click.help_option("-h", "--help")
@click.option(
    "--version",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_check_version,
    help="Show version and exit.",
)
def cli():
    """BDFR downloads the media of Reddit posts, read from the Arctic Shift archive, into folders kept free of
    duplicates. `bdfr serve` runs downloads for the Tampermonkey script and keeps watched users' folders complete."""
    pass


@cli.command("download")
@_add_options(_common_options)
@_add_options(_downloader_options)
@click.help_option("-h", "--help")
@click.pass_context
def cli_download(context: click.Context, **_):
    """Downloads the media of Reddit posts (users' and subreddits' listings, links, --post-json files)."""
    config = Configuration()
    config.process_click_arguments(context)
    silence_module_loggers()
    stream = make_console_logging_handler(config.verbose)
    try:
        downloader = Downloader(config, [stream])
        downloader.download()
    except Exception:
        logger.exception("Downloader exited unexpectedly")
        raise
    else:
        logger.info("Program complete")


# What `bdfr serve` uses unless told otherwise: one folder per user (the folder the userscript names), files named by
# post ID first, every file's hash kept in that folder's hashes.sqlite, and duplicates skipped
SERVE_DEFAULTS = {
    "no_dupes": True,
    "search_existing": True,
    "keep_hashes_db": True,
    "folder_scheme": "",
    "file_scheme": "{POSTID}_{TITLE}",
    "parallel": 8,
    "media_only": True,
}


@cli.command("serve")
@_add_options(_common_options)
@_add_options(_downloader_options)
@click.option(
    "--no-defaults",
    is_flag=True,
    default=False,
    help="Don't apply serve's defaults ("
    + " ".join(
        f"--{k.replace('_', '-')}" + ("" if v is True else f" {v}" if isinstance(v, int) else f" '{v}'")
        for k, v in SERVE_DEFAULTS.items()
    )
    + "); use the download command's.",
)
@click.option(
    "--port",
    type=click.IntRange(1024, 65535),
    default=DEFAULT_PORT,
    help=f"Port to listen on (default {DEFAULT_PORT}).",
)
@click.option(
    "--host",
    default=None,
    help="Address to listen on (default 127.0.0.1, this computer only). 0.0.0.0 lets other machines reach it, "
    "e.g. from Docker; the token then travels the network unencrypted.",
)
@click.option(
    "--watchlist",
    type=click.Path(dir_okay=False),
    default=None,
    help="YAML file of users whose folders are kept complete from Arctic Shift, checked weekly "
    "(default: watchlist.yaml next to BDFR's configuration).",
)
@click.help_option("-h", "--help")
@click.pass_context
def cli_serve(context: click.Context, **_):
    """Runs downloads requested by the Tampermonkey script, each into a folder under DIRECTORY.

    Download options given here apply to every request. Unless --no-defaults is given, serve uses --no-dupes
    --search-existing --keep-hashes-db --folder-scheme '' --file-scheme '{POSTID}_{TITLE}' --parallel 8
    --media-only; options
    given here or in --opts override them (in --opts, e.g. no_dupes: false)."""
    port = context.params.pop("port")
    no_defaults = context.params.pop("no_defaults")
    config = Configuration()
    if not no_defaults:
        # Applied first, so --opts files and command line options still win
        for key, value in SERVE_DEFAULTS.items():
            setattr(config, key, value)
    config.process_click_arguments(context)
    silence_module_loggers()
    logging.getLogger().addHandler(make_console_logging_handler(config.verbose))
    run_server(config, port)


@cli.command("rename")
@click.argument("directory", type=str)
@click.option("--file-scheme", required=True, help="The new file name scheme, e.g. '{POSTID}_{TITLE}'.")
@click.option(
    "--from-scheme",
    default="{REDDITOR}_{TITLE}_{POSTID}",
    show_default=True,
    help="The scheme the existing names were made with.",
)
@click.option(
    "--time-format", default="ISO", help="Format of {DATE} in the new names (dates come from modified times)."
)
@click.option("--filename-restriction-scheme", type=click.Choice(("linux", "windows")), default=None)
@click.option("--filename-character-set", type=click.Choice(("ascii", "unicode", "unicode_no_emojis")), default=None)
@click.option(
    "--build-hash-db",
    is_flag=True,
    help="Create hashes.sqlite if missing and add the content hash of every file not in it.",
)
@click.option("--no-subfolders", is_flag=True, help="Only rename files directly in DIRECTORY.")
@click.option(
    "--folder-is-user",
    is_flag=True,
    help="The folder is named after the user, so only files starting with its name are renamed.",
)
@click.option("--dry-run", is_flag=True, help="Only show what would be renamed.")
@click.option("-v", "--verbose", count=True)
@click.help_option("-h", "--help")
def cli_rename(
    directory: str,
    file_scheme: str,
    from_scheme: str,
    time_format: str,
    filename_restriction_scheme,
    filename_character_set,
    build_hash_db: bool,
    no_subfolders: bool,
    folder_is_user: bool,
    dry_run: bool,
    verbose: int,
):
    """Renames downloaded files in DIRECTORY to a new file scheme, offline.

    Uses only the existing file names, modified times and hash store; makes no network requests."""
    logging.getLogger().addHandler(make_console_logging_handler(verbose))
    renamer = Renamer(
        from_scheme, file_scheme, time_format, filename_restriction_scheme, filename_character_set, folder_is_user
    )
    result = rename_directory(Path(directory), renamer, dry_run, build_hash_db, recursive=not no_subfolders)
    summary = f"{result['planned']} to rename, {result['renamed']} renamed, {result['skipped']} skipped"
    if build_hash_db:
        summary += f", {result['hashed']} {'to hash' if dry_run else 'hashed'}"
    logger.info(summary)


def make_console_logging_handler(verbosity: int) -> logging.Handler:
    return logs.console_handler(verbosity)


def silence_module_loggers():
    logging.getLogger("urllib3").setLevel(logging.CRITICAL)


if __name__ == "__main__":
    cli()
