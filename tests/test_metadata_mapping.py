"""Metadata reference mappings never infer a host layout or drop unknown paths."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
TMP=tempfile.TemporaryDirectory(prefix='vod-metadata-')
os.environ.update(VOD_BASE_DIR=TMP.name,VOD_DATA_DIR=str(Path(TMP.name)/'data'),VOD_JELLYFIN_ENABLED='0',AI_PARSE_ENABLED='0',SELFHEAL_ENABLED='0')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from quark_vod import cleanup,config,coordination,db

class MetadataMappingTests(unittest.TestCase):
    def references(self,paths):
        con=sqlite3.connect(':memory:')
        con.execute('CREATE TABLE BaseItemImageInfos(Path TEXT)')
        con.executemany('INSERT INTO BaseItemImageInfos VALUES (?)',[(p,) for p in paths])
        try:
            with patch.object(config,'JELLYFIN_METADATA_LIBRARY_PREFIX','/custom/jf/library'):
                return cleanup._metadata_references(con,'/mounted/library')
        finally:con.close()

    def test_explicit_custom_mapping_and_unrelated_remote_url(self):
        self.assertEqual(self.references(['/custom/jf/library/aa/poster.jpg','https://images.example/a.jpg']),{'/mounted/library/aa/poster.jpg'})

    def test_unknown_local_prefix_is_not_silently_filtered(self):
        with self.assertRaises(cleanup.MetadataCleanupError):
            self.references(['/custom/jf/library/aa/poster.jpg','/unknown/images/a.jpg'])

    def test_parent_reference_cannot_escape_local_root(self):
        with self.assertRaises(cleanup.MetadataCleanupError):
            self.references(['/custom/jf/library/../../other.jpg'])

    def test_prune_unconfigured_never_uses_default_host_tree(self):
        with patch.object(config,'JELLYFIN_METADATA_LIBRARY_DIR',''),patch.object(config,'JELLYFIN_METADATA_LIBRARY_PREFIX',''),patch.object(coordination,'ensure_idle'),patch.object(cleanup,'_plan_orphan_metadata') as plan:
            self.assertEqual(cleanup.prune_jellyfin2_orphan_metadata(),(0,0))
            plan.assert_not_called()

def tearDownModule():
    if hasattr(db._local,'conn'):
        db._local.conn.close();del db._local.conn
    TMP.cleanup()
if __name__=='__main__':unittest.main(verbosity=2)
