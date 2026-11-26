"""Built-in public Telegram and PanSou providers; no account credentials."""
from concurrent.futures import wait, FIRST_COMPLETED
from html.parser import HTMLParser
import html
import json
import re
import time
import urllib.parse

# Examples, not a guarantee that a channel exists or carries a given work.
DEFAULT_TG_CHANNELS = (
    "Quark_Movies", "XiangxiuNB", "Aliyun_4K_Movies", "alyp_1",
    "yunpanshare", "PanjClub", "Quark_4K", "Aliyun_4K", "shareAliyun",
    "YunPanPan", "tianyirigeng", "NewAliPan", "ucpanpan", "zaihuayun",
    "yunpanziyuan",
)


class _Messages(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.depth = 0
        self.text_depth = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if "tgme_widget_message_wrap" in classes:
            if self.row is not None:
                self.rows.append(self.row)
            self.row = {"texts": [], "links": [], "evidence": [], "published_at": ""}
            self.depth = 0
            self.text_depth = None
        if self.row is None:
            return
        if tag not in ("br", "img", "hr", "meta", "input", "link", "source", "wbr"):
            self.depth += 1
        if "tgme_widget_message_text" in classes:
            self.text_depth = self.depth
        if tag == "a" and attrs.get("href"):
            self.row["links"].append(attrs["href"])
            if self.text_depth is not None:self.row["evidence"].append(attrs["href"]+" ")
        if tag == "time":
            self.row["published_at"] = attrs.get("datetime", "")[:40]
        if tag == "br" and self.text_depth is not None:
            self.row["texts"].append("\n")

    def handle_endtag(self, tag):
        if self.row is not None:
            if tag == "a" and self.text_depth is not None and self.row["links"]:
                self.row["evidence"].append(" " + self.row["links"][-1] + " ")
            if self.depth == self.text_depth:
                self.text_depth = None
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.row is not None and self.text_depth is not None:
            self.row["texts"].append(data)
            self.row["evidence"].append(data)

    def finish(self):
        if self.row is not None:
            self.rows.append(self.row)
            self.row = None
        return self.rows


def telegram_rows(text, keyword, source):
    from .panshare_matrix import extract_quark_links_and_pwd
    parser = _Messages()
    parser.feed(text)
    result = []
    for message in parser.finish():
        name = " ".join("".join(message["texts"]).split())
        if not name:
            continue
        combined = "".join(message["evidence"]) + "\n" + "\n".join(message["links"])
        for link in extract_quark_links_and_pwd(combined):
            result.append({"source": source, "name": name[:1000],
                           "share_url": link, "published_at": message["published_at"]})
    return result


def fetch_tg_web(node, keyword, limit, max_pages):
    from .panshare_matrix import _fetch_html, _start_task, _error_status
    channels = node.get("channels") or DEFAULT_TG_CHANNELS
    deadline = min(time.monotonic() + min(22.0, max(.1, node.get("timeout", 4)) * 3),
                   node.get("_request_deadline", float("inf")))
    pending = {}
    remaining = iter(channels)
    items = []
    report = {"errors": 0, "pages": 0, "details": 0, "truncated": False,
              "http_status_counts": {}}
    def fetch(channel):
        url = node["base"].rstrip("/") + "/s/" + channel + "?" + urllib.parse.urlencode({"q": keyword})
        text = _fetch_html(url, min(node.get("timeout", 4), max(.1, deadline-time.monotonic())))
        return telegram_rows(text, keyword, node["key"])
    exhausted = False
    while not exhausted or pending:
        while not exhausted and len(pending) < 4 and time.monotonic() < deadline:
            channel = next(remaining, None)
            if channel is None:
                exhausted = True
                break
            future = _start_task(fetch, channel)
            if future is None:
                report["errors"] += 1
            else:
                pending[future] = channel
        if not pending or time.monotonic() >= deadline:
            break
        done, _ = wait(pending, timeout=max(0, deadline-time.monotonic()), return_when=FIRST_COMPLETED)
        if not done:
            break
        for future in done:
            pending.pop(future)
            try:
                items.extend(future.result())
                report["pages"] += 1
                report["http_status_counts"]["200"] = report["http_status_counts"].get("200", 0) + 1
            except Exception as exc:
                report["errors"] += 1
                status = _error_status(exc)
                if status:
                    report["http_status_counts"][str(status)] = report["http_status_counts"].get(str(status), 0) + 1
                    report["http_status"] = status
                if getattr(exc, "redirect", False):
                    report["redirect"] = True
    if pending or not exhausted:
        report["errors"] += len(pending) or 1
        report["truncated"] = True
    # A few failed channels do not invalidate an otherwise useful broad search.
    if 0 < report["errors"] <= 2 and report["pages"] and len(items) >= 8:
        report["degraded"] = True
    return items[:1200], report


def pansou_rows(body, keyword, source, limit):
    """Accept merged_by_type, results[].links and compatible items shapes."""
    from .panshare_matrix import canonical_share, extract_quark_links_and_pwd
    if not isinstance(body, dict):
        raise ValueError("invalid provider response")
    if body.get("code") not in (None, 0, 200, "0", "200"):
        raise ValueError("provider response failure")
    root = body.get("data", body)
    if not isinstance(root, (dict, list)):
        raise ValueError("invalid provider response")
    result = []
    def visit(value, inherited="", depth=0):
        if depth > 8 or len(result) >= limit:
            return
        if isinstance(value, list):
            for row in value[:1200]:
                visit(row, inherited, depth+1)
        elif isinstance(value, dict):
            name = next((value[k] for k in ("name", "title", "note", "content", "text")
                         if isinstance(value.get(k), str) and value[k].strip()), inherited or keyword)
            raw = next((value[k] for k in ("share_url", "url", "link")
                        if isinstance(value.get(k), str)), "")
            if canonical_share(raw):
                links = extract_quark_links_and_pwd(raw)
                if links:
                    url = links[0]
                    password = value.get("password", value.get("pwd", ""))
                    if isinstance(password, str) and re.fullmatch(r"[A-Za-z0-9]{4}", password):
                        url = canonical_share(url) + "#pwd=" + password
                    result.append({"name": name[:1000], "share_url": url, "source": source})
            for key in ("merged_by_type", "results", "items", "links", "quark"):
                if key in value:
                    visit(value[key], name, depth+1)
    visit(root)
    return result


def fetch_pansou(node, keyword, limit, max_pages):
    from .panshare_matrix import _http_request
    payload = {"kw": keyword, "cloud_types": ["quark"], "res": "merge"}
    body = json.loads(_http_request(node["base"].rstrip("/") + node.get("path", "/api/search"),
                                   node.get("timeout", 4), method="POST", json_body=payload))
    rows = pansou_rows(body, keyword, node["key"], limit)
    return rows, {"errors": 0, "pages": 1, "details": 0,
                  "truncated": len(rows) >= limit, "partial": bool(body.get("partial"))}
