"""Bounded Range cache for MP4 files with widely separated audio/video chunks.

The cache never downloads a complete movie. It retains at most 24 MiB for five
minutes, coalesces identical block reads and allows four upstream reads at once.
The HTTP adapter accepts a saved transfer ID and version, never an arbitrary URL.
"""
from collections import OrderedDict
import hashlib
import re
import threading
import time
from . import cleaner, config

BLOCK_SIZE = 512 * 1024
MAX_BYTES = 24 * 1024**2
TTL = 300


def version(row):
    return hashlib.sha256((str(row['id'])+'\0'+row['remote_path']+'\0'+str(row['file_size'])).encode()).hexdigest()[:24]


def playback_url(row, skip=0):
    return f"{config.PENDING_BASE_URL}/media/{row['id']}?v={version(row)}&skip={skip}"


class RangeCache:
    def __init__(self, block_size=BLOCK_SIZE, max_bytes=MAX_BYTES, ttl=TTL):
        self.block_size=block_size;self.max_bytes=max_bytes;self.ttl=ttl
        self._entries=OrderedDict();self._bytes=0;self._lock=threading.Lock()
        self._stripes=[threading.Lock() for _ in range(64)]
        self._fetch_slots=threading.BoundedSemaphore(4)

    def _lookup(self,key):
        with self._lock:
            entry=self._entries.get(key)
            if not entry:return None
            if entry[0]<=time.monotonic():
                self._bytes-=len(entry[1]);del self._entries[key];return None
            self._entries.move_to_end(key)
            return entry[1:]

    def block(self,identity,url,skip,index):
        key=(identity,skip,index)
        cached=self._lookup(key)
        if cached is not None:return cached
        with self._stripes[hash(key)%len(self._stripes)]:
            cached=self._lookup(key)
            if cached is not None:return cached
            start=index*self.block_size
            with self._fetch_slots:
                stream=cleaner.open_clean_stream(url,skip,f'bytes={start}-{start+self.block_size-1}')
                try:
                    match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',stream.headers.get('Content-Range',''))
                    if stream.status!=206 or not match or int(match[1])!=start or stream.discard>8:
                        raise cleaner.CleanStreamError(502,'media source must support bounded byte ranges')
                    data=b''.join(stream.chunks())
                    if not data or len(data)>self.block_size or len(data)!=int(match[2])-start+1:
                        raise cleaner.CleanStreamError(502,'invalid media block length')
                    total=int(match[3])
                finally:stream.close()
            with self._lock:
                now=time.monotonic()
                for old in list(self._entries):
                    if self._entries[old][0]<=now:
                        self._bytes-=len(self._entries.pop(old)[1])
                while self._entries and self._bytes+len(data)>self.max_bytes:
                    self._bytes-=len(self._entries.popitem(last=False)[1][1])
                if len(data)<=self.max_bytes:
                    self._entries[key]=(now+self.ttl,data,total);self._bytes+=len(data)
            return data,total

    def open(self,identity,url,skip,range_header=''):
        selected=cleaner.parse_range_header(range_header)
        # Fetch the requested block first when possible. A suffix/whole request
        # needs the total size; block zero supplies it without a separate HEAD.
        index=selected[0]//self.block_size if selected and selected[0] is not None else 0
        first,total=self.block(identity,url,skip,index)
        if selected is None:start,end=0,total-1
        elif selected[0] is None:start,end=max(0,total-selected[1]),total-1
        else:start,end=selected[0],min(selected[1],total-1) if selected[1] is not None else total-1
        if start>=total:
            raise cleaner.CleanStreamError(416,'range not satisfiable',{'Content-Range':f'bytes */{total}'})
        headers={'Accept-Ranges':'bytes','Content-Type':'video/mp4','Content-Length':str(end-start+1),'Cache-Control':'private, no-store'}
        if selected is not None:headers['Content-Range']=f'bytes {start}-{end}/{total}'
        def chunks():
            position=start
            while position<=end:
                current=position//self.block_size
                data,size=(first,total) if current==index else self.block(identity,url,skip,current)
                if size!=total:raise OSError('media size changed during playback')
                offset=position%self.block_size
                count=min(len(data)-offset,end-position+1,128*1024)
                if count<=0:raise OSError('incomplete media block')
                yield data[offset:offset+count]
                position+=count
        return 206 if selected is not None else 200,headers,chunks()


cache=RangeCache()
# Jellyfin opens parallel video/audio ranges while building a seek index.  Queue
# a bounded number briefly instead of rejecting those bursts with an immediate
# 503; the cache and upstream fetch semaphore still cap memory and requests.
stream_slots=threading.BoundedSemaphore(32)
