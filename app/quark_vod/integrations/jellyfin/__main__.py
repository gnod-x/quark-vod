"""Export the installed source wrappers for a Jellyfin read-only mount."""
import argparse
import os
from pathlib import Path
from importlib.resources import files
import stat
import tempfile

def export(output, force=False):
    target = Path(output).expanduser().absolute()
    if target.is_symlink():
        raise ValueError("wrapper directory must not be a symlink")
    target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir():
        raise ValueError("wrapper output must be a directory")
    destinations = [target / name for name in ("ffmpeg", "ffprobe")]
    for destination in destinations:
        if destination.is_symlink():
            raise ValueError("refusing a symlink wrapper target")
        if destination.exists():
            if not destination.is_file() or not force:
                raise FileExistsError("wrapper target already exists")
    for destination in destinations:
        data = files(__package__).joinpath("scripts", destination.name).read_bytes()
        descriptor, temporary = tempfile.mkstemp(prefix=".quark-wrapper-", dir=target)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o755)
            if force:
                # Recheck immediately before publication; never follow a link.
                if destination.is_symlink() or (destination.exists() and not destination.is_file()):
                    raise ValueError("wrapper target changed")
                os.replace(temporary, destination)
            else:
                os.link(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return target

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        export(args.output, args.force)
    except (OSError, ValueError) as exc:
        parser.exit(1, "wrapper export failed: " + type(exc).__name__ + "\n")
    print("Exported ffmpeg and ffprobe wrappers. Install upstream binaries and mount this directory read-only.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
