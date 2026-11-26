"""Optional notification transports. Unconfigured delivery is a quiet no-op."""
import hashlib
import json
import urllib.request
from . import config, db, notification_settings

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def _post(url, payload):
    req=urllib.request.Request(url,data=json.dumps(payload,ensure_ascii=False).encode(),method="POST",
                               headers={"Content-Type":"application/json"})
    with urllib.request.build_opener(_NoRedirect).open(req,timeout=6) as response:
        raw=response.read(65537)
        if len(raw)>65536: raise ValueError("notification response too large")
        data=json.loads(raw) if raw else {}
        if url.startswith("https://api.telegram.org/bot") and data.get("ok") is not True:
            raise ValueError("notification transport rejected message")
        if data.get("ok") is False or data.get("errcode",0)!=0 or data.get("code",200) not in (0,200):
            raise ValueError("notification transport rejected message")

def notify(event, title, message, level="P1", fingerprint="", dedupe_seconds=21600):
    if level not in ("P0","P1","P2"):
        raise ValueError("invalid notification level")
    try:
        settings = notification_settings.runtime_settings()
    except notification_settings.SettingsError:
        return {"configured":False,"delivered":0,"error":"notification_settings_unavailable"}
    if level=="P2" and not settings['viewing_enabled']:
        return {"configured":False,"delivered":0,"skipped":"viewing_disabled"}
    transports=[]
    telegram=settings['telegram']
    token=telegram['bot_token']
    chat=telegram['chat_id']
    if telegram['enabled'] and token and chat:
        transports.append(("telegram","https://api.telegram.org/bot"+token+"/sendMessage",{"chat_id":chat,"text":f"[{level}] {title}\n{message}"}))
    bark=config._secret("NOTIFY_BARK_URL")
    if bark:
        transports.append(("bark",bark,{"title":f"[{level}] {title}","body":message,"group":"家庭影院"}))
    wecom=config._secret("NOTIFY_WECOM_WEBHOOK")
    if wecom:
        transports.append(("wecom",wecom,{"msgtype":"text","text":{"content":f"[{level}] {title}\n{message}"}}))
    webhook=config._secret("NOTIFY_WEBHOOK_URL")
    if webhook:
        transports.append(("webhook",webhook,{"event":event,"level":level,"title":title,"message":message}))
    if not transports:
        return {"configured":False,"delivered":0}
    fp=fingerprint or hashlib.sha256((event+"|"+title+"|"+message).encode()).hexdigest()
    try:
        if not db.notification_claim(fp,event,level,int(dedupe_seconds)):
            return {"configured":True,"delivered":0,"deduplicated":True}
    except Exception:
        return {"configured":True,"delivered":0,"error":"notification_state_unavailable"}
    delivered=0; failures=[]
    for name,url,payload in transports:
        try:
            _post(url,payload); delivered+=1
        except Exception:
            failures.append(name)
    with db._conn() as con:
        if delivered:
            con.execute("UPDATE notification_events SET delivered_at=? WHERE fingerprint=?",(db.now(),fp))
        else:
            con.execute("DELETE FROM notification_events WHERE fingerprint=?",(fp,))
    return {"configured":True,"delivered":delivered,"failed_channels":failures}
