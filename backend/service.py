"""项目事务与不可变版本；文件就绪后才允许数据库发布。"""

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .geometry import validate_draft, write_scene


class ServiceError(Exception):
    """携带可安全返回给调用方的业务错误。"""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def digest(path):
    """使用固定块计算文件摘要。"""
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class Service:
    """内部写入与外部读取共用事务实现，但由不同应用暴露。"""

    def __init__(self, settings):
        settings.validate()
        self.settings = settings
        self.root = settings.storage_root.resolve()

    @contextmanager
    def connection(self):
        """每次请求独立事务，异常自动回滚。"""
        with psycopg.connect(self.settings.database_url, row_factory=dict_row) as connection:
            yield connection

    def initialize(self):
        """仅由管理员显式执行建表，启动服务不会自动修改数据库。"""
        with self.connection() as connection:
            connection.execute(Path(__file__).with_name("schema.sql").read_text("utf-8"))

    def input_path(self, value):
        """登记 QGIS 选择的输入，实际打开确认服务账户具有读取权限。"""
        try:
            path = Path(value).resolve(strict=True)
            if not path.is_file():
                raise ValueError("不是普通文件")
            with path.open("rb") as stream:
                stream.read(1)
        except (OSError, ValueError, RuntimeError):
            raise ServiceError(422, "影像或 DEM 文件不存在、不可读或不是普通文件。") from None
        return str(path)

    def file_path(self, value):
        """数据库保存相对路径，防止下载逃逸受控目录。"""
        path = (self.root / value).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ServiceError(503, "成果文件不可用，请联系管理员。")
        return path

    def create_project(self, name):
        with self.connection() as db:
            return db.execute(
                "INSERT INTO lcc_projects(id,name) VALUES (%s,%s) RETURNING *",
                (uuid.uuid4(), name)).fetchone()

    def projects(self, internal=False):
        with self.connection() as db:
            return db.execute(
                """SELECT p.id,p.name,p.current_version,p.created_at,
                   v.created_at AS published_at,v.sha256
                   FROM lcc_projects p LEFT JOIN lcc_versions v ON v.id=p.current_version """
                + ("" if internal else "WHERE p.current_version IS NOT NULL ")
                + "ORDER BY p.created_at,p.id").fetchall()

    def images(self, project_id):
        with self.connection() as db:
            self._project(db, project_id)
            return db.execute(
                "SELECT * FROM lcc_images WHERE project_id=%s ORDER BY name,id",
                (project_id,)).fetchall()

    def register_image(self, project_id, name, image_path, dem_path):
        image_path = self.input_path(image_path)
        dem_path = self.input_path(dem_path) if dem_path else None
        with self.connection() as db:
            self._project(db, project_id, lock=True)
            existing = db.execute(
                "SELECT * FROM lcc_images WHERE project_id=%s AND image_path=%s",
                (project_id, image_path)).fetchone()
            if existing:
                return db.execute(
                    "UPDATE lcc_images SET dem_path=%s WHERE id=%s RETURNING *",
                    (dem_path, existing["id"])).fetchone()
            return db.execute(
                """INSERT INTO lcc_images(id,project_id,name,image_path,dem_path)
                   VALUES (%s,%s,%s,%s,%s) RETURNING *""",
                (uuid.uuid4(), project_id, name, image_path, dem_path)).fetchone()

    def save_draft(self, project_id, image_id, upload, base_draft_id):
        """草稿以内容摘要支持断线重试，以基础草稿标识防止覆盖他人编辑。"""
        validate_draft(upload)
        sha = digest(upload)
        with self.connection() as db:
            # 保存、登记与整项目发布按同一项目锁串行，防止确认快照后出现漏景。
            self._project(db, project_id, lock=True)
            image = db.execute(
                "SELECT * FROM lcc_images WHERE id=%s AND project_id=%s FOR UPDATE",
                (image_id, project_id)).fetchone()
            if not image:
                raise ServiceError(404, "项目影像不存在。")
            previous = None
            if image["draft_id"]:
                previous = db.execute("SELECT * FROM lcc_drafts WHERE id=%s",
                                      (image["draft_id"],)).fetchone()
            if previous and previous["sha256"] == sha:
                return {"draft_id": previous["id"], "sha256": sha}
            if image["draft_id"] != base_draft_id:
                raise ServiceError(409, "草稿已被其他客户端更新，请先另存本地草稿再重新打开。")
            draft_id = uuid.uuid4()
            relative = Path("drafts") / (str(draft_id) + ".gpkg")
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(upload, destination)
            db.execute("INSERT INTO lcc_drafts(id,image_id,file_path,sha256) VALUES(%s,%s,%s,%s)",
                       (draft_id, image_id, relative.as_posix(), sha))
            db.execute("UPDATE lcc_images SET draft_id=%s WHERE id=%s", (draft_id, image_id))
            return {"draft_id": draft_id, "sha256": sha}

    def draft(self, project_id, image_id):
        with self.connection() as db:
            row = db.execute(
                """SELECT d.* FROM lcc_drafts d JOIN lcc_images i ON i.draft_id=d.id
                   WHERE i.id=%s AND i.project_id=%s""", (image_id, project_id)).fetchone()
            if not row:
                raise ServiceError(404, "该影像没有已保存的草稿。")
            return row

    def publish(self, project_id, drafts, base_version, idempotency_key):
        """原子发布人工确认的全部项目草稿；断线重试返回同一个版本。"""
        scenes = dict(drafts)
        if not scenes or len(scenes) != len(drafts):
            raise ServiceError(422, "发布必须包含草稿，且每张影像只能出现一次。")
        request_hash = hashlib.sha256(json.dumps(
            [sorted((str(k), str(v)) for k, v in scenes.items()), str(base_version)]
        ).encode()).hexdigest()
        with self.connection() as db:
            project = self._project(db, project_id, lock=True)
            previous = db.execute(
                "SELECT * FROM lcc_versions WHERE project_id=%s AND idempotency_key=%s",
                (project_id, idempotency_key)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise ServiceError(409, "幂等标识已经用于另一份发布请求。")
                return self._version_summary(db, previous)
            if project["current_version"] != base_version:
                raise ServiceError(409, "项目已有新版本，请刷新项目后重新确认发布。")
            current = {row["id"]: row["draft_id"] for row in db.execute(
                "SELECT id,draft_id FROM lcc_images WHERE project_id=%s AND draft_id IS NOT NULL",
                (project_id,))}
            if scenes != current:
                raise ServiceError(409, "项目草稿已变化，请重新确认全部影像后发布。")
            version_id = uuid.uuid4()
            created = datetime.now(timezone.utc)
            directory = self.root / "versions" / str(version_id)
            directory.mkdir(parents=True, exist_ok=False)
            temporary = directory / "preparing.gpkg"
            final = directory / "project.gpkg"
            # 事务中的占位记录对外不可见；任何异常都回滚当前版本和所有要素。
            db.execute(
                """INSERT INTO lcc_versions
                   (id,project_id,base_version,idempotency_key,request_hash,file_path,sha256,created_at)
                   VALUES(%s,%s,%s,%s,%s,%s,'',%s)""",
                (version_id, project_id, base_version, idempotency_key, request_hash,
                 final.relative_to(self.root).as_posix(), created))
            for scene_id, scene_draft in sorted(scenes.items()):
                record = db.execute("SELECT * FROM lcc_drafts WHERE id=%s",
                                    (scene_draft,)).fetchone()
                path = self.file_path(record["file_path"])
                if digest(path) != record["sha256"]:
                    raise ServiceError(503, "草稿文件校验失败，发布已终止。")
                for feature_id, attributes, wkb in write_scene(
                        temporary, path, project_id, scene_id, version_id, created.isoformat()):
                    db.execute(
                        """INSERT INTO lcc_features(version_id,image_id,feature_id,properties,geom)
                           VALUES(%s,%s,%s,%s,ST_SetSRID(ST_GeomFromWKB(%s),4326))""",
                        (version_id, scene_id, feature_id, Jsonb(attributes), wkb))
                db.execute(
                    "INSERT INTO lcc_version_images(version_id,image_id,draft_id) VALUES(%s,%s,%s)",
                    (version_id, scene_id, scene_draft))
            # 不在提交异常后删除文件：连接中断时提交结果可能未知，保留孤儿供管理员核对。
            # Windows 的 fsync 需要可写句柄；此处只同步，不修改成果内容。
            with open(temporary, "r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, final)
            sha = digest(final)
            (directory / "audit.json").write_text(json.dumps({
                "project_id": str(project_id), "version_id": str(version_id),
                "base_version": str(base_version) if base_version else None,
                "idempotency_key": str(idempotency_key), "request_hash": request_hash,
                "sha256": sha, "created_at": created.isoformat(),
                "images": {str(k): str(v) for k, v in scenes.items()},
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            db.execute("UPDATE lcc_versions SET sha256=%s WHERE id=%s", (sha, version_id))
            db.execute("UPDATE lcc_projects SET current_version=%s WHERE id=%s",
                       (version_id, project_id))
            return self._version_summary(db, db.execute(
                "SELECT * FROM lcc_versions WHERE id=%s", (version_id,)).fetchone())

    def _project(self, db, project_id, lock=False):
        row = db.execute("SELECT * FROM lcc_projects WHERE id=%s" + (" FOR UPDATE" if lock else ""),
                         (project_id,)).fetchone()
        if not row:
            raise ServiceError(404, "项目不存在。")
        return row

    def _selected_version(self, db, project_id, version_id):
        project = self._project(db, project_id)
        selected = version_id or project["current_version"]
        version = db.execute("SELECT * FROM lcc_versions WHERE id=%s AND project_id=%s",
                             (selected, project_id)).fetchone()
        if not version:
            raise ServiceError(404, "指定版本不存在、尚未发布或不属于该项目。")
        return project, version

    def _version_summary(self, db, version):
        summary = {key: version[key] for key in
                   ("id", "project_id", "base_version", "created_at", "sha256")}
        summary["image_ids"] = [row["image_id"] for row in db.execute(
            "SELECT image_id FROM lcc_version_images WHERE version_id=%s ORDER BY image_id",
            (version["id"],))]
        summary["feature_count"] = db.execute(
            "SELECT count(*) AS n FROM lcc_features WHERE version_id=%s",
            (version["id"],)).fetchone()["n"]
        return summary

    def detail(self, project_id, version_id=None):
        with self.connection() as db:
            project, version = self._selected_version(db, project_id, version_id)
            return {"id": project["id"], "name": project["name"],
                    "current_version": project["current_version"],
                    "version": self._version_summary(db, version)}

    def versions(self, project_id):
        with self.connection() as db:
            project, _ = self._selected_version(db, project_id, None)
            return [{**self._version_summary(db, row),
                     "is_current": row["id"] == project["current_version"]}
                    for row in db.execute(
                        "SELECT * FROM lcc_versions WHERE project_id=%s ORDER BY created_at DESC,id",
                        (project_id,)).fetchall()]

    def features(self, project_id, version_id, offset, limit):
        if offset and not version_id:
            raise ServiceError(422, "后续分页必须携带首个响应的 version_id。")
        with self.connection() as db:
            _, version = self._selected_version(db, project_id, version_id)
            rows = db.execute(
                """SELECT properties, ST_AsGeoJSON(geom,15)::json AS geometry
                   FROM lcc_features WHERE version_id=%s ORDER BY image_id,feature_id
                   LIMIT %s OFFSET %s""", (version["id"], limit + 1, offset)).fetchall()
            return {
                "type": "FeatureCollection", "version_id": version["id"],
                "features": [{"type": "Feature",
                              "id": row["properties"]["image_id"] + ":" + row["properties"]["feature_id"],
                              "geometry": row["geometry"], "properties": row["properties"]}
                             for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None,
            }

    def download(self, project_id, version_id=None):
        with self.connection() as db:
            _, version = self._selected_version(db, project_id, version_id)
            return version
