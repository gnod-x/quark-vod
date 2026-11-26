"""Bounded read-only account rotation and API admission; secrets never enter diagnostics."""
from collections import OrderedDict
from http.cookies import SimpleCookie
import json
import os
import stat
import tempfile
import threading
import time
from . import config

class TokenBucket:
    def __init__(self, rate=3, burst=5):
        self.rate,self.burst=rate,burst
        self.tokens=float(burst)
        self.updated=time.monotonic()
        self.lock=threading.Lock()
    def acquire(self, timeout=60):
        deadline=time.monotonic()+timeout
        while True:
            with self.lock:
                now=time.monotonic()
                self.tokens=min(self.burst,self.tokens+(now-self.updated)*self.rate)
                self.updated=now
                if self.tokens>=1:
                    self.tokens-=1
                    return True
                delay=(1-self.tokens)/self.rate
            if time.monotonic()+delay>deadline: return False
            time.sleep(delay)

bucket=TokenBucket(config.QUARK_REQUEST_RATE,config.QUARK_REQUEST_BURST)

class AccountPool:
    def __init__(self):
        self.lock=threading.RLock()
        self.cursor=0
        self.cookies={}
        self.tokens=OrderedDict()
        filename=config.QUARK_COOKIE_STATE_FILE
        if filename:
            try:
                info=os.stat(filename,follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size>1024*1024: raise ValueError()
                fd=os.open(filename,os.O_RDONLY|os.O_NOFOLLOW)
                with os.fdopen(fd,encoding='utf8') as stream:
                    actual=os.fstat(stream.fileno())
                    if (actual.st_dev,actual.st_ino)!=(info.st_dev,info.st_ino):raise ValueError()
                    data=json.loads(stream.read(1024*1024+1))
                if isinstance(data,dict) and len(data)<=33 and all(isinstance(k,str) and isinstance(v,str) and len(v)<65536 for k,v in data.items()):
                    self.cookies=data
            except FileNotFoundError: pass
            except Exception: raise ValueError('QUARK_COOKIE_STATE_FILE is not a private valid cookie state') from None
    def scout(self, main):
        scouts=config.QUARK_SCOUT_COOKIES
        with self.lock:
            if not scouts: return self.cookies.get('main',main)
            key='scout:'+str(self.cursor%len(scouts))
            self.cursor+=1
            return self.cookies.get(key,scouts[(self.cursor-1)%len(scouts)])
    def main(self, original):
        with self.lock: return self.cookies.get('main',original)
    def remember_token(self, token, cookie):
        with self.lock:
            self.tokens[token]=(time.monotonic()+600,cookie)
            self.tokens.move_to_end(token)
            while len(self.tokens)>128:self.tokens.popitem(last=False)
    def token_cookie(self, token, default):
        with self.lock:
            row=self.tokens.get(token)
            return row[1] if row and row[0]>time.monotonic() else default
    def rolling(self, original, set_cookie_headers, main):
        if not set_cookie_headers:return original
        jar=SimpleCookie()
        try:
            jar.load(original)
            for header in set_cookie_headers:
                update=SimpleCookie();update.load(header)
                for key,value in update.items():
                    # Only cookies from Quark's own response; no metadata attributes.
                    jar[key]=value.value
            updated='; '.join(key+'='+value.value for key,value in jar.items())
        except Exception:return original
        if not updated or updated==original:return original
        with self.lock:
            key='main' if original==main or original==self.cookies.get('main') else None
            if key is None:
                for index,cookie in enumerate(config.QUARK_SCOUT_COOKIES):
                    if original in (cookie,self.cookies.get('scout:'+str(index))):
                        key='scout:'+str(index);break
            if key is None:return updated
            self.cookies[key]=updated
            for token,(expires,bound) in list(self.tokens.items()):
                if bound==original:self.tokens[token]=(expires,updated)
            path=config.QUARK_COOKIE_STATE_FILE
            if path:
                directory=os.path.dirname(path)
                fd,tmp=tempfile.mkstemp(prefix='.quark-cookie-',dir=directory)
                try:
                    os.fchmod(fd,0o600)
                    with os.fdopen(fd,'w',encoding='utf8') as out:
                        json.dump(self.cookies,out);out.flush();os.fsync(out.fileno())
                    os.replace(tmp,path)
                finally:
                    if os.path.exists(tmp):os.unlink(tmp)
        return updated

pool=AccountPool()
