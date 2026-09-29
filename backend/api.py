"""分别构造内部和外部 ASGI 应用，不挂载彼此的路由。"""

import hmac
import logging
import os
import uuid

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .service import Service, ServiceError


class ProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=200)


class ImageRequest(ProjectRequest):
    image_path: str = Field(min_length=1, max_length=4096)
    dem_path: str | None = Field(default=None, max_length=4096)


class DraftReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_id: uuid.UUID
    draft_id: uuid.UUID


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    drafts: list[DraftReference] = Field(min_length=1)
    base_version: uuid.UUID | None
    idempotency_key: uuid.UUID
    confirmed: bool


def create_app(internal=False, settings=None, service=None):
    """内部应用由网络隔离保护；公共应用在路由执行前验证 API Key。"""
    settings = settings or Settings.from_env()
    settings.validate()
    service = service or Service(settings)
    expected_key = settings.public_key

    def authenticate(x_api_key: str = Header(default="")):
        if not hmac.compare_digest(x_api_key.encode(), expected_key.encode()):
            raise ServiceError(401, "API Key 无效。")

    app = FastAPI(title="LCC Internal" if internal else "LCC Public",
                  dependencies=[] if internal else [Depends(authenticate)], docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.service = service

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        logging.getLogger("lcc").exception("服务请求失败")
        return JSONResponse(status_code=500, content={"detail": "服务处理失败，旧版本保持可用，请联系管理员。"})

    if internal:
        @app.get("/internal/projects")
        def projects():
            return service.projects(internal=True)

        @app.post("/internal/projects", status_code=201)
        def create_project(payload: ProjectRequest):
            return service.create_project(payload.name)

        @app.get("/internal/projects/{project_id}/images")
        def images(project_id: uuid.UUID):
            return service.images(project_id)

        @app.post("/internal/projects/{project_id}/images", status_code=201)
        def register_image(project_id: uuid.UUID, payload: ImageRequest):
            try:
                return service.register_image(project_id, payload.name,
                                              payload.image_path, payload.dem_path)
            except (OSError, ValueError):
                raise ServiceError(422, "影像或 DEM 文件不存在、不可读或不是普通文件。")

        @app.put("/internal/projects/{project_id}/images/{image_id}/draft")
        async def save_draft(project_id: uuid.UUID, image_id: uuid.UUID, request: Request,
                             base_draft_id: uuid.UUID | None = None):
            staging = service.root / "uploads"
            staging.mkdir(parents=True, exist_ok=True)
            path = staging / (str(uuid.uuid4()) + ".gpkg")
            size = 0
            try:
                with path.open("xb") as stream:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > settings.max_upload_bytes:
                            raise ServiceError(413, "草稿超过上传大小限制。")
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    return await run_in_threadpool(
                        service.save_draft, project_id, image_id, path, base_draft_id)
                except (ValueError, RuntimeError) as exc:
                    logging.getLogger("lcc").warning("草稿校验失败: %s", exc)
                    raise ServiceError(422, "草稿几何、字段或 GeoPackage 格式不符合契约。")
            finally:
                path.unlink(missing_ok=True)

        @app.get("/internal/projects/{project_id}/images/{image_id}/draft")
        def draft(project_id: uuid.UUID, image_id: uuid.UUID):
            row = service.draft(project_id, image_id)
            return FileResponse(service.file_path(row["file_path"]),
                                media_type="application/geopackage+sqlite3",
                                headers={"X-Draft-ID": str(row["id"]), "ETag": row["sha256"]})

        @app.post("/internal/projects/{project_id}/publish")
        def publish(project_id: uuid.UUID, payload: PublishRequest):
            if not payload.confirmed:
                raise ServiceError(422, "发布前必须人工确认最终几何。")
            return service.publish(project_id,
                                   [(item.image_id, item.draft_id) for item in payload.drafts],
                                   payload.base_version, payload.idempotency_key)
    else:
        @app.get("/projects")
        def projects():
            return service.projects()

        @app.get("/projects/{project_id}")
        def detail(project_id: uuid.UUID, version_id: uuid.UUID | None = None):
            return service.detail(project_id, version_id)

        @app.get("/projects/{project_id}/versions")
        def versions(project_id: uuid.UUID):
            return service.versions(project_id)

        @app.get("/projects/{project_id}/features")
        def features(project_id: uuid.UUID, version_id: uuid.UUID | None = None,
                     offset: int = Query(default=0, ge=0),
                     limit: int = Query(default=100, ge=1, le=1000)):
            return service.features(project_id, version_id, offset, limit)

        @app.get("/projects/{project_id}/download")
        def download(project_id: uuid.UUID, version_id: uuid.UUID | None = None):
            row = service.download(project_id, version_id)
            return FileResponse(service.file_path(row["file_path"]),
                                filename="project_" + str(row["id"]) + ".gpkg",
                                media_type="application/geopackage+sqlite3",
                                headers={"X-Version-ID": str(row["id"]), "ETag": row["sha256"]})
    return app


def internal_app():
    """仅供内部网络进程使用。"""
    return create_app(internal=True)


def public_app():
    """仅供对外只读进程使用。"""
    return create_app()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--init-db", action="store_true", required=True)
    parser.parse_args()
    Service(Settings.from_env()).initialize()
