"""One immutable AI routing snapshot per task process; no implicit providers."""
import copy
from quark_vod.automation_settings import get_ai_config, ai_models
SNAPSHOT = copy.deepcopy(get_ai_config())
def models():
    return ai_models(SNAPSHOT)
def effort(task):
    return SNAPSHOT.get("reasoning", {}).get(task, "")


def request_chat(messages, *, task, timeout=30, max_tokens=2048):
    """Try the configured model order only; AI disabled performs no I/O."""
    import json
    import urllib.request
    from quark_vod import config
    candidates = models()
    if not candidates or not SNAPSHOT.get("base_url"):
        return None
    token = config.load_cliproxy_token()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    for model in candidates:
        payload = {"model": model, "messages": messages,
                   "max_tokens": max_tokens, "temperature": .1}
        if effort(task):
            payload["reasoning_effort"] = effort(task)
        request = urllib.request.Request(SNAPSHOT["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(2 * 1024**2 + 1)
                if len(raw) > 2 * 1024**2:
                    raise ValueError("oversized AI response")
                body = json.loads(raw)
                content = body["choices"][0]["message"]["content"]
                if isinstance(content, str) and content.strip():
                    return content.strip()
        except Exception:
            continue
    return None
