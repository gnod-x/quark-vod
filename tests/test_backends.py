"""Offline tests for the narrow remote storage contract."""
import atexit
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
_TEST_BASE = tempfile.TemporaryDirectory(prefix="vod-backends-")
atexit.register(_TEST_BASE.cleanup)
for key in tuple(os.environ):
    if key.startswith(("VOD_", "QUARK_", "OPENLIST_", "JELLYFIN_", "AI_PARSE_", "SELFHEAL_")):
        del os.environ[key]
os.environ.update(
    VOD_BASE_DIR=_TEST_BASE.name,
    VOD_STARTUP_SCAN="0",
    VOD_STARTUP_WRITE_PROBE="0",
    SELFHEAL_ENABLED="0",
    AI_PARSE_ENABLED="0",
)
sys.path.insert(0, str(ROOT / "app"))

from quark_vod import config
from quark_vod.backends import OpenListStore
from quark_vod.transfer import QuarkTransfer, TransferNotReady


class FakeStore:
    def __init__(self):
        self.calls = []

    def mkdir(self, path):
        self.calls.append(("mkdir", path))
        return True

    def direct_url(self, path):
        self.calls.append(("direct_url", path))
        return "https://media.example/episode.mp4"

    def refresh_path_tree(self, path):
        self.calls.append(("refresh", path))
        return True

    def remove(self, path):
        self.calls.append(("remove", path))
        return True


class StorageContractTests(unittest.TestCase):
    def test_openlist_adapter_forwards_narrow_contract(self):
        class Client:
            def mkdir(self, path):
                return path

            def direct_url(self, path):
                return "url:" + path

            def refresh_path_tree(self, path):
                return path

            def remove(self, path):
                return path

        store = OpenListStore(Client())
        self.assertEqual(store.mkdir("/x"), True)
        self.assertEqual(store.direct_url("/x"), "url:/x")
        self.assertEqual(store.refresh_path_tree("/x"), True)
        self.assertEqual(store.remove("/x"), True)

    def test_transfer_uses_injected_store_for_lifecycle_operations(self):
        store = FakeStore()
        transfer = QuarkTransfer(storage=store)
        with patch.object(transfer, "_get_quark_fid", return_value="fid"):
            self.assertEqual(transfer.ensure_vod_dir(), "fid")
        self.assertIn(("mkdir", "/夸克网盘/点播"), store.calls)
        self.assertTrue(transfer.delete_remote("/夸克网盘/点播/episode.mp4"))
        self.assertIn(("remove", "/夸克网盘/点播/episode.mp4"), store.calls)

    def test_missing_backend_fails_closed_for_mutations(self):
        transfer = QuarkTransfer(None)
        with self.assertRaises(TransferNotReady):
            transfer.ensure_vod_dir()
        with self.assertRaises(TransferNotReady):
            transfer.delete_remote("/夸克网盘/点播/episode.mp4")

    def test_selected_transfer_uses_injected_storage_for_create_and_refresh(self):
        store = FakeStore()
        transfer = QuarkTransfer(storage=store)
        transfer.ensure_ready = Mock()
        transfer._get_quark_fid = Mock(return_value="target-fid")
        transfer.resolve_share = Mock(return_value=("token", "share-id", []))
        transfer.share_files = Mock(return_value=[])
        transfer.select_share_video = Mock(return_value={
            "size": 400 * 1024**2,
            "share": {"fid": "source-fid", "share_fid_token": "synthetic",
                      "file_name": "Fixture.mkv", "size": 400 * 1024**2,
                      "parent_fid": "0"},
        })
        transfer._quark_list_fid = Mock(side_effect=[[], [{
            "name": "Fixture.mkv", "size": 400 * 1024**2,
            "is_dir": False, "fid": "saved-fid",
        }]])
        transfer._check_capacity = Mock(return_value=(True, 10**12))
        transfer._request = Mock(return_value={"code": 0, "data": {"task_id": "task"}})
        transfer._wait_saved_task = Mock()

        transfer_id, remote = transfer._do_transfer(
            "电影", "Fixture (2026)", 0, 0, "https://pan.quark.cn/s/fixture")

        self.assertTrue(transfer_id)
        self.assertTrue(remote.endswith("/Fixture.mkv"))
        self.assertIn(("mkdir", config.QUARK_VOD_ROOT), store.calls)
        slot_path = remote.rsplit("/", 1)[0]
        self.assertIn(("mkdir", slot_path), store.calls)
        self.assertIn(("refresh", config.play_path(remote)), store.calls)

    def test_format_sniff_uses_injected_direct_and_refresh(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return b"\x1a\x45\xdf\xa3" + b"0" * (size - 4)

        store = FakeStore()
        transfer = QuarkTransfer(storage=store)
        item = {"fid": "file-id", "name": "disguised.mp3", "size": 60 * 1024**2}
        with patch("urllib.request.urlopen", return_value=Response()), \
                patch.object(transfer, "rename_quark_file", return_value=True):
            fixed = transfer.sniff_and_fix_file(item, config.QUARK_VOD_ROOT + "/slot")
        self.assertTrue(fixed["path"].endswith("disguised.mkv"))
        self.assertTrue(any(call[0] == "direct_url" for call in store.calls))
        self.assertTrue(any(call[0] == "refresh" for call in store.calls))

    def test_existing_openlist_constructor_keeps_default_adapter(self):
        from unittest.mock import Mock
        client = Mock()
        transfer = QuarkTransfer(client)
        self.assertIs(transfer.ol, client)
        self.assertIsInstance(transfer.storage, OpenListStore)
        self.assertIs(transfer.storage.client, client)


if __name__ == "__main__":
    unittest.main()
