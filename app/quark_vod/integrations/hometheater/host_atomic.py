"""Atomic metadata writes, preserving existing ownership, mode and xattrs."""

from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config
import os
import stat
import tempfile
from quark_vod.coordination import playback_lease, file_lock


def atomic_write_text(path, text):
    path = os.fspath(path)
    with playback_lease(), file_lock("host-metadata:" + os.path.abspath(path), timeout=2):
        if os.path.lexists(path):
            return _replace_existing(path, text)
        descriptor, temporary = tempfile.mkstemp(prefix=".metadata-", dir=os.path.dirname(path))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o644)
            # Hard-link publication fails if another writer created the destination.
            # No existing STRM/NFO can be overwritten by a racing new-file writer.
            os.link(temporary, path)
        finally:
            os.unlink(temporary)


def _replace_existing(path, text):
    """Commit local text only if file identity, permissions and ACL stay intact."""
    original = os.lstat(path)
    if not stat.S_ISREG(original.st_mode):
        raise RuntimeError('refusing to replace a nonregular file')
    def attributes(target):
        return {name: os.getxattr(target, name, follow_symlinks=False)
                for name in os.listxattr(target, follow_symlinks=False)}
    original_attrs = attributes(path)
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_uid, info.st_gid, info.st_mode)
    fd, temporary = tempfile.mkstemp(prefix='.night-write-', dir=os.path.dirname(path))
    try:
        fresh = os.fstat(fd)
        if (fresh.st_uid, fresh.st_gid) != (original.st_uid, original.st_gid):
            os.fchown(fd, original.st_uid, original.st_gid)
        os.fchmod(fd, stat.S_IMODE(original.st_mode))
        # Includes POSIX ACL and any other existing extended attributes.
        # Unsupported or forbidden metadata operations abort before replacement.
        for name in os.listxattr(temporary, follow_symlinks=False):
            if name not in original_attrs:
                os.removexattr(temporary, name, follow_symlinks=False)
        for name, value in original_attrs.items():
            os.setxattr(temporary, name, value, follow_symlinks=False)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            fd = None
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        saved = os.lstat(temporary)
        if ((saved.st_uid, saved.st_gid, stat.S_IMODE(saved.st_mode)) !=
                (original.st_uid, original.st_gid, stat.S_IMODE(original.st_mode))
                or attributes(temporary) != original_attrs):
            raise RuntimeError('replacement metadata could not be preserved')
        if identity(os.lstat(path)) != identity(original) or attributes(path) != original_attrs:
            raise RuntimeError('source file changed before replacement')
        os.replace(temporary, path)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.exists(temporary):
            os.unlink(temporary)

