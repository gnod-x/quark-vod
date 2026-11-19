"""Byte integrity, memory bounds and failure handling of the media Range cache."""
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from quark_vod import cleaner,media_cache


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.body=bytes(range(255))*3
        self.cache=media_cache.RangeCache(block_size=64,max_bytes=128)
        self.calls=[]
        self.patch=patch.object(cleaner,'open_clean_stream',side_effect=self.upstream)
        self.patch.start();self.addCleanup(self.patch.stop)

    def upstream(self,url,skip,rng):
        self.calls.append(rng)
        start,end=map(int,rng[6:].split('-'));end=min(end,len(self.body)-1)
        if start>=len(self.body):raise cleaner.CleanStreamError(416,'end',{'Content-Range':f'bytes */{len(self.body)}'})
        data=self.body[start:end+1]
        return cleaner.CleanStream(io.BytesIO(data),206,{'Content-Range':f'bytes {start}-{end}/{len(self.body)}'},0,len(data))

    def read(self,rng,identity='one'):
        status,headers,body=self.cache.open(identity,'http://fixture',0,rng)
        return status,headers,b''.join(body)

    def test_random_seeks_share_blocks_and_preserve_bytes(self):
        for start in (5,90,15,100,0):
            self.assertEqual(self.read(f'bytes={start}-{start+9}')[2],self.body[start:start+10])
        self.assertEqual(len(self.calls),2)

    def test_full_suffix_open_and_cross_block_ranges(self):
        for rng,expected in [('',self.body),('bytes=-7',self.body[-7:]),('bytes=700-',self.body[700:]),('bytes=60-70',self.body[60:71]),('bytes=0-99999',self.body)]:
            status,headers,actual=self.read(rng)
            self.assertEqual(actual,expected);self.assertEqual(int(headers['Content-Length']),len(expected))
            self.assertEqual(status,206 if rng else 200)
            self.assertLessEqual(self.cache._bytes,128)

    def test_source_identity_and_expiry_do_not_reuse_old_blocks(self):
        self.read('bytes=0-3','a');self.read('bytes=0-3','b')
        self.assertEqual(len(self.calls),2)
        with patch.object(media_cache.time,'monotonic',return_value=10**12):self.read('bytes=0-3','a')
        self.assertEqual(len(self.calls),3)

    def test_truncated_upstream_is_not_cached(self):
        bad=cleaner.CleanStream(io.BytesIO(b'broken'),206,{'Content-Range':'bytes 0-63/765'},0,64)
        with patch.object(cleaner,'open_clean_stream',return_value=bad):
            with self.assertRaises(OSError):self.read('bytes=0-3')
        self.assertEqual(self.cache._bytes,0)

    def test_ignored_range_cannot_read_entire_prefix(self):
        bad=cleaner.CleanStream(io.BytesIO(b'unused'),206,{'Content-Range':'bytes 64-127/765'},64,64)
        with patch.object(cleaner,'open_clean_stream',return_value=bad):
            with self.assertRaises(cleaner.CleanStreamError):self.read('bytes=64-65')
        self.assertEqual(self.cache._bytes,0)

    def test_beyond_end_keeps_416_size(self):
        with self.assertRaises(cleaner.CleanStreamError) as caught:self.read('bytes=900-')
        self.assertEqual(caught.exception.status,416)
        self.assertEqual(caught.exception.headers['Content-Range'],'bytes */765')


if __name__=='__main__':unittest.main(verbosity=2)
