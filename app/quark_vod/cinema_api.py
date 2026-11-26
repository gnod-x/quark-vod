"""Authenticated cinema adapter for the optional packaged hometheater integration."""
import importlib
import json
import re
import urllib.parse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import config, db
from .coordination import Busy

_OPERATIONS = {
    'catalogue': ('catalogue', {'id', 'media_type', 'season', 'episode', 'variant'}),
    'sources': ('source_search', {'id', 'media_type', 'season', 'episode', 'variant'}),
    'files': ('list_source_files', {'token'}),
    'add': ('add_to_library', {'token'}),
    'replace': ('replace_to_library', {'token', 'reason'}),
    'subscribe': ('subscribe_season', {'id', 'media_type', 'season', 'episode', 'variant'}),
}


def _integration():
    return importlib.import_module('quark_vod.integrations.hometheater.vod_search_and_add')


def _link_result(result, public_prefix=''):
    if not isinstance(result, dict):
        raise TypeError('invalid integration result')
    result = dict(result)
    play_url = result.get('play_url')
    if play_url:
        parsed = urllib.parse.urlsplit(play_url)
        if parsed.path in ('/play', '/services/vod/play'):
            result['play_url'] = public_prefix + '/play' + ('?' + parsed.query if parsed.query else '')
        else:
            result['play_url'] = ''
    item_id = result.get('jellyfin_item_id')
    public_jellyfin = config._value('VOD_JELLYFIN_PUBLIC_URL').rstrip('/')
    parsed_jellyfin = urllib.parse.urlsplit(public_jellyfin)
    if item_id and parsed_jellyfin.scheme in ('http', 'https') and parsed_jellyfin.netloc and not parsed_jellyfin.username and not parsed_jellyfin.password:
        result['jellyfin_url'] = public_jellyfin + '/web/index.html#!/details?id=' + urllib.parse.quote(str(item_id), safe='')
    return result


def _call(name, *args, public_prefix=''):
    try:
        result = getattr(_integration(), name)(*args)
        return _link_result(result, public_prefix)
    except (ImportError, ModuleNotFoundError):
        return JSONResponse({'error': 'cinema_integration_unavailable'}, status_code=503)
    except Busy:
        return JSONResponse({'error': 'cinema_busy'}, status_code=409)
    except (ValueError, TypeError, KeyError):
        return JSONResponse({'error': 'cinema_invalid_request'}, status_code=400)
    except Exception:
        return JSONResponse({'error': 'cinema_temporarily_unavailable'}, status_code=503)
    finally:
        db.close()


def _valid_payload(operation, payload):
    if not isinstance(payload, dict) or set(payload) - _OPERATIONS[operation][1]:
        return False
    if operation in ('files', 'add', 'replace'):
        token = payload.get('token')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', token):
            return False
        if operation == 'replace' and payload.get('reason', 'corrupted') not in ('dv_color_issue', 'wrong_audio', 'wrong_episode', 'hardcoded_ad', 'corrupted'):
            return False
        return True
    if type(payload.get('id')) is not int or not 0 < payload['id'] < 2**53:
        return False
    if payload.get('media_type') not in ('movie', 'tv'):
        return False
    for key in ('season', 'episode'):
        if key in payload and (type(payload[key]) is not int or not 0 <= payload[key] <= 10000):
            return False
    if 'variant' in payload and (not isinstance(payload['variant'], str) or len(payload['variant']) > 32):
        return False
    return True


def create_router(require_admin):
    router = APIRouter(prefix='/api/admin/cinema', tags=['Cinema'], dependencies=[Depends(require_admin)])

    @router.get('/discovery')
    def discovery(request: Request):
        return _call('discovery', public_prefix=request.scope.get('root_path', '').rstrip('/'))

    @router.get('/search')
    def search(kw: str = ''):
        if not kw.strip() or len(kw) > 100 or any(ord(c) < 32 for c in kw):
            return JSONResponse({'error': 'cinema_invalid_request'}, status_code=400)
        try:
            results = _integration().search_all(kw.strip())
            return {'code': 0, 'data': results}
        except (ImportError, ModuleNotFoundError):
            return JSONResponse({'error': 'cinema_integration_unavailable'}, status_code=503)
        except Exception:
            return JSONResponse({'error': 'cinema_search_unavailable'}, status_code=503)
        finally:
            db.close()

    @router.get('/job')
    def job(request: Request, id: str = ''):
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', id):
            return JSONResponse({'error': 'cinema_invalid_request'}, status_code=400)
        return _call('job_status', id, public_prefix=request.scope.get('root_path', '').rstrip('/'))

    def endpoint(operation):
        async def post(request: Request):
            if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/json':
                return JSONResponse({'error': 'cinema_invalid_request'}, status_code=415)
            try:
                length = int(request.headers.get('content-length', '0'))
                if length < 0 or length > 16384:
                    return JSONResponse({'error': 'cinema_request_too_large'}, status_code=413)
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 16384:
                        return JSONResponse({'error': 'cinema_request_too_large'}, status_code=413)
                payload = json.loads(raw)
            except (ValueError, UnicodeDecodeError, RecursionError):
                return JSONResponse({'error': 'cinema_invalid_request'}, status_code=400)
            if not _valid_payload(operation, payload):
                return JSONResponse({'error': 'cinema_invalid_request'}, status_code=400)
            return await run_in_threadpool(_call, _OPERATIONS[operation][0], payload, public_prefix=request.scope.get('root_path', '').rstrip('/'))
        post.__name__ = 'cinema_' + operation
        return post

    for operation in _OPERATIONS:
        router.add_api_route('/' + operation, endpoint(operation), methods=['POST'])
    return router
