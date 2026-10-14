from __future__ import annotations

import json
import os
import secrets
import base64
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer
from fastapi import Depends
from pydantic import BaseModel, Field, ValidationError
from .config import RUNS, ROOT, Settings, PROVIDERS
from .ingest import MAX_BYTES, ALLOWED
from .jobs import JobManager, create_run
from .llm import KimiClient, WorkflowError
from .models import Meta
from .usage import read_records, summarize, write_json

STATIC = Path(__file__).parent / "static"
ARTIFACTS = {"report.html", "report.json", "quality.json", "transcript.json", "evidence.json", "facts.json", "manifest.json", "usage.json", "usage-summary.json", "analysis.json"}


class KeyBody(BaseModel):
    api_key: str = Field(min_length=8, max_length=500)
    provider: str | None = None


class ModelOptions(BaseModel):
    model_config = {'extra': 'forbid', 'allow_inf_nan': False}
    provider: str = 'kimi'
    api_key: str | None = Field(default=None, max_length=500, repr=False)
    model: str
    effort: str
    max_concurrency: int = Field(ge=1, le=8)
    max_tokens: int = Field(ge=1024, le=393216)
    timeout: float = Field(ge=30, le=1800)
    base_url: str


class SourceBody(BaseModel):
    filename: str = Field(max_length=1000)
    meta: Meta


class TaskModelOptions(BaseModel):
    model_config = {'extra': 'forbid', 'allow_inf_nan': False}
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=8)
    max_tokens: int | None = Field(default=None, ge=1024, le=393216)
    timeout: float | None = Field(default=None, ge=30, le=1800)


class TextJobBody(BaseModel):
    model_config = {'extra': 'forbid'}
    filename: str = Field(default='transcript.txt', min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=MAX_BYTES)
    meta: Meta = Field(default_factory=Meta)
    options: TaskModelOptions | None = None


def create_app(root: Path = RUNS, settings: Settings | None = None, allow_remote: bool = False,
               api_token: str | None = None):
    access_token = os.environ.get('VISIT_API_TOKEN', '') if api_token is None else api_token
    config_path = root / '.model-settings.json'
    current = (settings or Settings.load()).with_saved(config_path)
    saved = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    profiles = {key: {k:v for k,v in options.items() if k in current.options()}
                for key, options in saved.get('profiles', {}).items() if key in PROVIDERS}
    profiles[current.provider] = current.options()
    config_lock = threading.RLock()
    manager = JobManager(root)

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            manager.close()

    app = FastAPI(title="接访洞察 API", version="1.0.0", docs_url='/docs', redoc_url=None, lifespan=lifespan)
    app.state.manager = manager
    api_security = [Depends(HTTPBearer(auto_error=False))]

    @app.middleware("http")
    async def request_guard(request: Request, call_next):
        is_mcp = request.url.path == '/mcp' or request.url.path.startswith('/mcp/')
        if is_mcp and not access_token:
            return JSONResponse({'detail': 'MCP requires VISIT_API_TOKEN'}, status_code=503)
        host = request.url.hostname
        if not allow_remote and host not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"detail": "仅限本机访问"}, status_code=403)
        authorized = False
        if access_token and request.url.path not in {'/healthz', '/docs', '/openapi.json', '/docs/oauth2-redirect'}:
            scheme, _, credential = request.headers.get('authorization', '').partition(' ')
            if scheme.lower() == 'bearer':
                authorized = secrets.compare_digest(credential.encode(), access_token.encode())
            elif scheme.lower() == 'basic' and not is_mcp:
                try:
                    password = base64.b64decode(credential, validate=True).decode().partition(':')[2]
                    authorized = secrets.compare_digest(password.encode(), access_token.encode())
                except (ValueError, UnicodeError):
                    pass
            if not authorized:
                return JSONResponse({'detail': '需要有效的接口访问密钥'}, status_code=401,
                                    headers={'WWW-Authenticate': 'Basic realm="Visit Insight"' if not is_mcp and not request.url.path.startswith('/api/') else 'Bearer'})
        if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "不接受跨站请求"}, status_code=403)
            bearer_authorized = authorized and request.headers.get('authorization', '').lower().startswith('bearer ')
            if request.headers.get("x-visit-workflow") != "1" and not bearer_authorized:
                return JSONResponse({"detail": "请求来源校验失败"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
        if request.url.path == '/docs':
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
        return response

    @app.exception_handler(ValueError)
    async def invalid_value(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def not_found(request, exc):
        return JSONResponse({"detail": "任务或文件不存在"}, status_code=404)

    @app.get("/", response_class=HTMLResponse)
    def home():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/api/config")
    def config():
        with config_lock:
            providers = []
            for key, spec in PROVIDERS.items():
                defaults = {'provider':key, 'model':spec['default_model'], 'base_url':spec['urls'][0],
                            'effort':'high', 'max_tokens':32000, 'timeout':600, 'max_concurrency':3}
                providers.append({'id':key, **spec, 'models':[{'id':m,'name':n} for m,n in spec['models'].items()],
                                  'settings':profiles.get(key,defaults), 'configured':bool(current.credentials.get(key))})
            return {**current.public(), 'providers':providers,
                    'models':[{'id':key,'name':value} for key,value in PROVIDERS[current.provider]['models'].items()],
                    'config_source':'网页保存参数优先；各厂商密钥分别配置', 'max_file_mb':2}

    def draft(body):
        return current.select(body.model_dump(exclude={'api_key'}), body.api_key) if body else replace(current)

    @app.post('/api/config')
    def save_config(body: ModelOptions):
        nonlocal current
        with config_lock:
            updated = draft(body)
            updated_profiles = {**profiles, updated.provider:updated.options()}
            write_json(config_path, {**updated.options(), 'profiles':updated_profiles})
            profiles.update(updated_profiles)
            current = updated
            return {'ok':True, **current.public(), 'message':'已保存，重启后保留；新建或继续任务时生效，已排队及运行中的任务保持原配置'}

    @app.post("/api/config/key")
    def set_key(body: KeyBody):
        nonlocal current
        with config_lock:
            provider = body.provider or current.provider
            if provider not in PROVIDERS:
                raise ValueError('请选择支持的 API 厂商')
            keys = {**current.credentials, provider:body.api_key.strip()}
            current = replace(current, credentials=keys, api_key=keys.get(current.provider,''))
        return {"ok": True, "message": "密钥仅保存在本次服务内存中", **current.public()}

    @app.post("/api/config/test")
    def test_key(body: ModelOptions | None = None):
        try:
            with config_lock:
                selected = draft(body)
            text, usage = KimiClient(selected).complete([{"role": "user", "content": 'Reply only with {"ok":true}.'}], max_tokens=min(selected.max_tokens,2048))
            return {"ok": True, 'provider':selected.provider, "model": selected.model, "message": f"{PROVIDERS[selected.provider]['models'][selected.model]} 连接成功", "usage": usage}
        except WorkflowError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

    @app.get("/api/jobs")
    def jobs():
        return manager.list()

    def task_view(run_id: str):
        state = get_job(run_id)
        return {**state, 'links': {
            'status': f'/api/v1/jobs/{run_id}',
            'usage': f'/api/v1/jobs/{run_id}/usage',
            'report_html': f'/reports/{run_id}/report.html',
            'report_json': f'/reports/{run_id}/report.json',
        }}

    @app.post('/api/v1/jobs', status_code=202, dependencies=api_security, tags=['Reports'])
    def submit_text(body: TextJobBody):
        """提交转写文本，立即返回任务 ID；轮询 links.status 获取结果。"""
        with config_lock:
            options = body.options.model_dump(exclude_none=True) if body.options else {}
            provider = options.get('provider', current.provider)
            if provider not in PROVIDERS:
                raise ValueError('请选择支持的 API 厂商')
            if provider != current.provider:
                options = {**current.options(), **profiles.get(provider, {
                    'provider': provider, 'model': PROVIDERS[provider]['default_model'],
                    'base_url': PROVIDERS[provider]['urls'][0], 'effort': 'high',
                    'max_tokens': 32000}), **options}
            selected = current.select(options)
        if not selected.api_key:
            raise HTTPException(400, detail=f'服务端未配置 {selected.provider_name} API Key')
        folder = create_run(body.text.encode('utf-8'), body.filename, body.meta, root)
        manager.start(folder.name, selected)
        return task_view(folder.name)

    @app.get('/api/v1/jobs/{run_id}', dependencies=api_security, tags=['Reports'])
    def task_status(run_id: str):
        return task_view(run_id)

    @app.get("/api/sources")
    def sources():
        directory = (ROOT / "录音文本").resolve()
        if not directory.exists():
            return []
        return [{"name": p.relative_to(directory).as_posix(), "size": p.stat().st_size}
                for p in sorted(directory.rglob("*")) if valid_source(p, directory)]

    def valid_source(path: Path, directory: Path) -> bool:
        if not path.resolve().is_relative_to(directory) or not path.is_file() or path.suffix.lower() not in ALLOWED:
            return False
        return not any(part.is_symlink() for part in [path, *path.parents] if part != directory and part.is_relative_to(directory))

    @app.post("/api/jobs/from-source")
    def from_source(body: SourceBody):
        if not current.api_key:
            raise HTTPException(400, detail=f"请先配置 {current.provider_name} API Key")
        directory = (ROOT / "录音文本").resolve()
        path = directory / body.filename
        if not valid_source(path, directory):
            raise HTTPException(400, detail="请选择录音文本目录中的有效文件")
        if path.stat().st_size > MAX_BYTES:
            raise HTTPException(400, detail="文件超过 2 MB")
        folder = create_run(path.read_bytes(), path.name, body.meta, root)
        manager.start(folder.name, replace(current))
        return manager.get(folder.name)

    @app.post("/api/jobs")
    @app.post('/api/v1/jobs/upload', status_code=202, dependencies=api_security, tags=['Reports'])
    async def new_job(file: UploadFile = File(...), client_alias: str = Form("本次接访客户"), project: str = Form(""), advisor: str = Form(""), visit_date: str = Form("")):
        if not current.api_key:
            raise HTTPException(400, detail=f"请先配置 {current.provider_name} API Key")
        data = await file.read(MAX_BYTES+1)
        await file.close()
        try:
            meta = Meta(client_alias=client_alias or "本次接访客户", project=project, advisor=advisor, visit_date=visit_date)
        except ValidationError as exc:
            raise HTTPException(400, detail="接访信息格式不正确，请核对接访日期") from exc
        folder = create_run(data, file.filename or "transcript.txt", meta, root)
        manager.start(folder.name, replace(current))
        return manager.get(folder.name)

    @app.get("/api/jobs/{run_id}")
    def get_job(run_id: str):
        return {**manager.get(run_id), 'usage': summarize(read_records(manager.folder(run_id) / 'usage.json'))}

    @app.get('/api/jobs/{run_id}/usage')
    @app.get('/api/v1/jobs/{run_id}/usage', dependencies=api_security, tags=['Reports'])
    def job_usage(run_id: str):
        records = read_records(manager.folder(run_id) / 'usage.json')
        return {'summary': summarize(records), 'calls': records}

    @app.post("/api/jobs/{run_id}/resume")
    @app.post('/api/v1/jobs/{run_id}/resume', dependencies=api_security, tags=['Reports'])
    def resume(run_id: str):
        if not current.api_key:
            raise HTTPException(400, detail="请先配置密钥")
        state = manager.get(run_id)
        if state["status"] in {"complete", "needs_review"}:
            raise HTTPException(400, detail="任务已有完整报告，请查看报告；重新分析请新建任务")
        manager.start(run_id, replace(current))
        return manager.get(run_id)

    @app.post("/api/jobs/{run_id}/stop")
    @app.post('/api/v1/jobs/{run_id}/stop', dependencies=api_security, tags=['Reports'])
    def stop(run_id: str):
        manager.stop(run_id)
        return manager.get(run_id)

    @app.get("/reports/{run_id}/{artifact}", dependencies=api_security, tags=['Reports'])
    def artifact(run_id: str, artifact: str, download: bool = False):
        if artifact not in ARTIFACTS:
            raise HTTPException(404, detail="文件不存在")
        folder = manager.folder(run_id)
        path = folder / artifact
        if not path.is_file():
            raise HTTPException(404, detail="文件尚未生成")
        return FileResponse(path, media_type="text/html; charset=utf-8" if artifact.endswith(".html") else "application/json",
                            filename=artifact if download else None)

    @app.get("/demo", response_class=HTMLResponse)
    def demo():
        path = ROOT / "output" / "demo" / "report.html"
        if not path.exists():
            raise HTTPException(404, detail="演示文件尚未生成")
        return HTMLResponse(path.read_text(encoding="utf-8"))

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    from .mcp_server import build_mcp
    mcp = build_mcp('http://localhost', access_token, app=app)
    app.state.mcp = mcp
    app.mount('/', mcp.streamable_http_app())
    return app
