"""Portable command line entry point."""
import argparse
import os


def main(argv=None):
    parser = argparse.ArgumentParser(prog="quark-vod", description="Standalone Quark VOD service")
    parser.add_argument("command", nargs="?", choices=["serve"], default="serve")
    parser.add_argument("--host", help="Bind address (default: VOD_HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="Listen port (default: VOD_PORT or 8099)")
    parser.add_argument("--data-dir", help="Directory for state, logs and the media library")
    parser.add_argument("--version", action="version", version="Quark VOD 3.0.0")
    args = parser.parse_args(argv)
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.host:
        os.environ["VOD_HOST"] = args.host
    if args.port:
        os.environ["VOD_PORT"] = str(args.port)
    if args.data_dir:
        os.environ["VOD_BASE_DIR"] = os.path.abspath(args.data_dir)
    import uvicorn
    from . import config
    uvicorn.run("quark_vod.api:create_app", factory=True, host=config.HOST,
                port=config.PORT, workers=1, log_config=None)
