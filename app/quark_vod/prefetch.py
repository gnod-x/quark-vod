"""One bounded, low-priority queue; prefetch only after verified viewing progress."""
import json
import os
import threading
import time
import urllib.request
import uuid
from collections import OrderedDict
from pathlib import Path
from . import config, db, strm_manager
from .identity import Work

class SmartPrefetcher:
    def __init__(self, check=None, submit=None, delay=30):
        self.check=check or self._watching
        self.submit=submit or self._submit
        self.delay=delay
        self.pending=OrderedDict()
        self.lock=threading.Lock()
        self.wakeup=threading.Event()
        self.stop_event=threading.Event()
        self.thread=None

    def schedule(self, work):
        if work.episode<=0 or work.season<0:
            return False
        key=(work.cat,work.title,work.season,work.episode,getattr(work,"variant",""))
        with self.lock:
            if key not in self.pending:
                self.pending[key]=(time.monotonic()+self.delay,time.monotonic()+4*3600,work)
            while len(self.pending)>32:
                self.pending.popitem(last=False)
        self.wakeup.set()
        return True

    def _watching(self, work):
        if not config.VOD_JELLYFIN_ENABLED:
            return False
        key=config.get_jellyfin2_key()
        if not key:
            return False
        req=urllib.request.Request(config.JELLYFIN2_API+"/Sessions",headers={"Authorization":'MediaBrowser Token="'+key+'"'})
        with urllib.request.urlopen(req,timeout=5) as response:
            raw=response.read(2*1024*1024+1)
            if len(raw)>2*1024*1024: return False
            sessions=json.loads(raw)
        if not isinstance(sessions,list):return False
        mapped=db.get_strm_path(*work.args,**work.options)
        if not mapped:return False
        try:
            if mapped not in strm_manager.activation_paths(*work.args,**work.options):return False
        except (OSError,ValueError):
            return False
        library=Path(config.CINEMA2_DIR).resolve()
        expected=Path(mapped).resolve()
        if not expected.is_relative_to(library):return False
        relative=expected.relative_to(library)
        allowed={str(expected),str(Path('/cinema2')/relative)}
        for session in sessions:
            item=session.get("NowPlayingItem") or {}
            state=session.get("PlayState") or {}
            if item.get("ParentIndexNumber")!=work.season or item.get("IndexNumber")!=work.episode:
                continue
            # SeriesName normally omits the year. The mapped STRM, including
            # its version suffix, is stronger identity evidence than that label.
            sources=item.get("MediaSources") or []
            source_id=state.get("MediaSourceId") or session.get("MediaSourceId")
            if source_id:
                matches=[source for source in sources if source.get("Id")==source_id]
                if len(matches)>1 or (sources and not matches):continue
                actual=matches[0].get("Path","") if matches else ""
                if not actual or actual.startswith(("http://","https://")):
                    # Real Sessions DTOs omit MediaSources. For STRM playback,
                    # PlaybackInfo may instead expose its remote media URL.
                    # Only the selected source's exact item id permits using
                    # the library item's local STRM path; another version or
                    # an unknown/non-item source remains unconfirmed.
                    try:
                        same_item=uuid.UUID(str(source_id))==uuid.UUID(str(item.get("Id","")))
                    except (ValueError,TypeError,AttributeError):
                        same_item=False
                    if not same_item:continue
                    actual=item.get("Path") or ""
            elif len(sources)>1:
                continue  # Multiple versions without a selected source are unknown.
            else:
                actual=item.get("Path") or (sources[0].get("Path","") if sources else "")
            if os.path.normpath(actual) not in allowed:continue
            runtime=item.get("RunTimeTicks") or 0
            position=state.get("PositionTicks") or 0
            if runtime>0 and position/runtime>=.45 and not state.get("IsPaused",False):
                return True
        return False

    def _submit(self, work):
        from . import server
        options={"variant":work.variant} if getattr(work,"variant","") else {}
        path=db.get_strm_path(work.cat,work.title,work.season,work.episode+1,**options)
        if not path:
            return False
        server.trigger_preheat_task(work.cat,work.title,work.season,work.episode+1,**options)
        return True

    def tick(self):
        now=time.monotonic()
        with self.lock:
            due=[(key,value) for key,value in self.pending.items() if value[0]<=now]
        for key,(_,expires,work) in due:
            if expires<=now:
                with self.lock: self.pending.pop(key,None)
                continue
            try:
                watching=self.check(work)
                complete=watching and self.submit(work)
            except Exception:
                complete=False
            with self.lock:
                if complete: self.pending.pop(key,None)
                elif key in self.pending: self.pending[key]=(now+30,expires,work)

    def _loop(self):
        try:
            while not self.stop_event.is_set():
                self.wakeup.wait(30); self.wakeup.clear()
                if not self.stop_event.is_set(): self.tick()
        finally:
            db.close()

    def start(self):
        if self.thread is None:
            self.thread=threading.Thread(target=self._loop,name="smart-prefetch",daemon=True)
            self.thread.start()

    def close(self):
        self.stop_event.set(); self.wakeup.set()
        if self.thread: self.thread.join(timeout=6)

prefetcher=SmartPrefetcher()
