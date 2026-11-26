"""Command line entry points for the API and isolated automation workers."""
import argparse
import os
import sys

from . import __version__


def _paths(args):
    if args.env:
        os.environ["VOD_ENV_FILE"] = os.path.abspath(args.env)
    if args.data_dir:
        os.environ["VOD_BASE_DIR"] = os.path.abspath(args.data_dir)


def main(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    # Worker parsing stays in its module and does not import the web application.
    if values and values[0] == "jobs":
        parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        parser.add_argument("--env")
        parser.add_argument("--data-dir")
        args, remaining = parser.parse_known_args(values[1:])
        _paths(args)
        from .automation_runner import cli_main
        return cli_main(remaining)

    parser = argparse.ArgumentParser(
        prog="quark-vod", allow_abbrev=False,
        description="Quark VOD API. Use 'quark-vod jobs --help' for automation.")
    parser.add_argument("command", nargs="?", choices=["serve", "jobs"], default="serve")
    parser.add_argument("--host", help="Bind address (default: VOD_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Listen port (default: VOD_PORT or 8099)")
    parser.add_argument("--data-dir", help="Directory for state, logs and the media library")
    parser.add_argument("--env", help="Explicit operator environment file")
    parser.add_argument("--version", action="version", version=f"Quark VOD {__version__}")
    args, remaining = parser.parse_known_args(values)
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.command != "jobs" and remaining:
        parser.error("unrecognized arguments: " + " ".join(remaining))
    _paths(args)
    if args.command == "jobs":
        from .automation_runner import cli_main
        return cli_main(remaining)
    if args.host:
        os.environ["VOD_HOST"] = args.host
    if args.port:
        os.environ["VOD_PORT"] = str(args.port)
    import uvicorn
    from . import config
    uvicorn.run("quark_vod.api:create_app", factory=True, host=config.HOST,
                port=config.PORT, workers=1, log_config=None, access_log=False)
