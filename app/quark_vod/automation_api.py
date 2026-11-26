"""Authenticated administration routes for opt-in automation."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from .admin_auth import require_admin as _auth
from . import automation_settings as settings, automation_runner as runner


def _error(exc):
    return JSONResponse(status_code=exc.status, content={'error':exc.code}, headers={'Cache-Control':'no-store'})




async def _body(request):
    if request.headers.get('content-type','').split(';',1)[0].strip().lower() != 'application/json':
        settings.invalid()
    length = request.headers.get('content-length')
    if length is not None and (not length.isdecimal() or len(length)>8 or int(length)>settings.MAX_BYTES):
        settings.invalid()
    raw = bytearray()
    async for chunk in request.stream():
        if len(chunk) > settings.MAX_BYTES-len(raw):
            settings.invalid()
        raw.extend(chunk)
    return settings.parse_request(bytes(raw))


def create_router(require_admin=None):
    router = APIRouter(prefix='/api/admin/automation', tags=['Automation'],
                       dependencies=[Depends(require_admin or _auth)])

    @router.get('')
    def get():
        try:
            return settings.get_settings()
        except settings.AutomationError as exc:
            return _error(exc)

    @router.put('')
    async def put(request: Request):
        try:
            return await run_in_threadpool(settings.update_settings,await _body(request))
        except settings.AutomationError as exc:
            return _error(exc)

    @router.get('/jobs')
    def jobs():
        return {'jobs':settings.catalog()}

    @router.get('/runs')
    def runs():
        try:
            return runner.runs()
        except settings.AutomationError as exc:
            return _error(exc)

    @router.post('/tasks/{task}/run', status_code=202)
    async def run(task: str, request: Request):
        try:
            body = await _body(request)
            if set(body)-{'revision','dry_run'} or 'revision' not in body:
                settings.invalid()
            value = await run_in_threadpool(runner.enqueue,task,body['revision'],body.get('dry_run',True))
            return JSONResponse(status_code=202,content=value,headers={'Cache-Control':'no-store'})
        except settings.AutomationError as exc:
            return _error(exc)

    return router


router = create_router()
