"""Small dependency boundaries for QuarkTransfer storage operations.

The public contract deliberately covers only the storage operations currently
used by the transfer lifecycle.  Quark share inspection and save-task handling
remain a separate concern in :mod:`quark_vod.transfer`.
"""

from __future__ import annotations

from typing import Any, Protocol


class RemoteStore(Protocol):
    """Storage operations currently required by QuarkTransfer.

    Path namespaces intentionally mirror the current integration:

    * ``mkdir`` and ``remove`` receive write-mount paths (normally
      ``/夸克网盘/...``).
    * ``direct_url`` and ``refresh_path_tree`` receive playback-mount paths
      after ``config.play_path`` (normally ``/夸克/...``).

    ``mkdir`` and ``remove`` return true only after their operation is accepted.
    ``refresh_path_tree`` is best-effort: its return value is advisory and must
    never be used as proof that an object exists. Implementations may raise
    provider exceptions; callers must not treat read or refresh failures as a
    missing file.
    """

    def mkdir(self, write_path: str) -> bool:
        ...

    def direct_url(self, playback_path: str) -> str:
        ...

    def refresh_path_tree(self, playback_path: str) -> bool:
        ...

    def remove(self, write_path: str) -> bool:
        ...


class OpenListStore:
    """Adapter exposing the narrow ``RemoteStore`` contract over OpenList.

    Keeping the client behind this adapter makes fake storage usable in tests
    and prevents a future provider from being mistaken for a Quark transfer
    implementation.  The wrapped client is intentionally available through
    ``client`` for legacy diagnostics that still need OpenList-specific APIs.
    """

    def __init__(self, client: Any):
        self.client = client

    def mkdir(self, write_path: str) -> bool:
        return bool(self.client.mkdir(write_path))

    def direct_url(self, playback_path: str) -> str:
        return self.client.direct_url(playback_path)

    def refresh_path_tree(self, playback_path: str) -> bool:
        return bool(self.client.refresh_path_tree(playback_path))

    def remove(self, write_path: str) -> bool:
        return bool(self.client.remove(write_path))


def openlist_store(client: Any) -> RemoteStore:
    """Return the default storage adapter for an already-created client."""

    return OpenListStore(client)
