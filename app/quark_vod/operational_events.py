"""Bounded, redacted operational notifications; no upstream text or URLs."""
from concurrent.futures import ThreadPoolExecutor
import threading
from . import db,notifier

_EVENTS={
    'main_cookie_invalid':('夸克主号认证失败','请在管理台核对主号授权。','P0'),
    'quark_risk_control':('夸克触发风控','已保留转存状态，请核对账号风控。','P0'),
    'quark_quota_exhausted':('夸克空间不足','空间不足以保存目标视频，未切换片源重复保存。','P0'),
    'quota_cleanup_blocked':('容量超限且无法安全清理','现有保护策略保留云端对象，请人工核对容量。','P0'),
    'playback_backend_failed':('播放存储后端不可用','后端连接或限时探针未通过，已保留现有媒体。','P1'),
}
_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='operational-notify')
_slots=threading.BoundedSemaphore(8)

def emit(event, error=None):
    if event not in _EVENTS:raise ValueError('unknown operational event')
    if not _slots.acquire(blocking=False):return False
    title,message,level=_EVENTS[event]
    category=type(error).__name__ if isinstance(error,BaseException) else 'OperationalFailure'
    def send():
        try:
            return notifier.notify(event,title,message+' 异常类别：'+category,level,
                                   fingerprint='operational:'+event+':'+category)
        except Exception:
            return None
        finally:
            db.close()
            _slots.release()
    try:_executor.submit(send)
    except Exception:
        _slots.release()
        return False
    return True
