#!/usr/bin/env python3
"""Import explicitly configured public Douban wishlists; no cookies or account scraping."""

from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from quark_vod import config, db
from quark_vod.coordination import file_lock


class FeedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_feed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_feed(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'www.douban.com' or parsed.port not in (None, 443):
        raise ValueError('only official public Douban RSS URLs are allowed')
    if parsed.username or parsed.password or not re.fullmatch(r'/feed/people/[A-Za-z0-9_-]{1,100}/interests/?', parsed.path):
        raise ValueError('invalid public Douban wishlist RSS path')
    return url


def parse_wishlist(content):
    root = ET.fromstring(content)
    results = []
    for item in root.findall('./channel/item')[:200]:
        title = (item.findtext('title') or '').strip()
        # The feed mixes "watched", "watching" and "wish"; only wish entries are subscriptions.
        if not title.startswith(('想看', 'Wish', 'wish')):
            continue
        title = re.sub(r'^(?:想看|Wish|wish)\s*[:：]?\s*', '', title).strip(' 《》')
        if not title or len(title) > 200:
            continue
        identifier = item.findtext('guid') or item.findtext('link') or title
        results.append({'key': hashlib.sha256(identifier.encode()).hexdigest(), 'title': title})
    return results


def run_sync():
    raw = host_config._value('DOUBAN_WATCHLIST_RSS', '').strip()
    if not raw:
        return {'configured': False, 'imported': 0, 'errors': 0}
    urls = json.loads(raw) if raw.startswith('[') else [s.strip() for s in raw.split(',') if s.strip()]
    if not isinstance(urls, list) or len(urls) > 10:
        raise ValueError('DOUBAN_WATCHLIST_RSS must contain at most ten public feeds')
    path = Path(config.DATA_DIR) / 'douban_wishlist_state.json'
    with file_lock('douban-watchlist'):
        seen = set(json.loads(path.read_text()).get('seen', [])) if path.exists() else set()
        imported = errors = 0
        opener = urllib.request.build_opener(FeedRedirects())
        from quark_vod.integrations.hometheater.wishlist_bot import ingest_message
        for url in urls:
            validate_feed(url)
            try:
                with opener.open(urllib.request.Request(url, headers={'User-Agent': 'Quark-VOD Wishlist/1.0'}), timeout=12) as response:
                    body = response.read(2 * 1024**2 + 1)
                if len(body) > 2 * 1024**2:
                    raise ValueError('RSS too large')
                for item in parse_wishlist(body):
                    if item['key'] in seen:
                        continue
                    result = ingest_message(item['title'], source='douban')
                    if result.get('status') not in {'ambiguous', 'not_found', 'failed'}:
                        seen.add(item['key'])
                        imported += 1
            except Exception:
                errors += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'seen': sorted(seen)[-5000:]}))
        temporary.replace(path)
    return {'configured': True, 'imported': imported, 'errors': errors}


if __name__ == '__main__':
    try:
        result = run_sync()
        print(json.dumps(result, ensure_ascii=False))
        raise SystemExit(int(bool(result['errors'])))
    finally:
        db.close()
