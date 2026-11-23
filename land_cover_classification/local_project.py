"""本地项目元数据和不可变草稿快照，仅使用标准库。"""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .result_attributes import normalize_cartographer


FORMAT_VERSION = 1


def now():
    """返回可排序的 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat()


def file_digest(path):
    """流式计算快照完整性摘要。"""
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def input_fingerprint(path):
    """有限读取文件首、中、尾，辅助识别重新定位的输入，避免整图读取。"""
    size = os.path.getsize(path)
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for offset in sorted({0, max(0, size // 2 - 32768), max(0, size - 65536)}):
            stream.seek(offset)
            digest.update(stream.read(65536))
    return {'size': size, 'sample_sha256': digest.hexdigest()}


def snapshot_gpkg(source, destination):
    """用 SQLite 在线备份合并 WAL，连接关闭后校验完整性。"""
    source = Path(source).resolve()
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError('快照目标已存在，拒绝覆盖。')
    last_progress = time.monotonic()
    previous_remaining = None

    def progress(status, remaining, total):
        """数据库持续锁定时及时失败，有进展的大快照不受总时长限制。"""
        nonlocal last_progress, previous_remaining
        current = time.monotonic()
        if previous_remaining is None or remaining != previous_remaining:
            last_progress = current
            previous_remaining = remaining
        elif current - last_progress > 5:
            raise TimeoutError('草稿数据库持续忙碌，本次快照已停止，旧成果仍保留。')

    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)) as src:
        with closing(sqlite3.connect(str(destination), timeout=5)) as dst:
            src.backup(dst, pages=256, progress=progress, sleep=0.1)
            if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('草稿快照完整性校验失败。')
            tables = {row[0] for row in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'draft', 'gpkg_contents'}.issubset(tables):
                raise ValueError('文件不是有效的草稿 GeoPackage。')


class LocalProject:
    """元数据事务只引用已完成的文件；调用方持有 Qt 项目锁至关闭。"""

    def __init__(self, directory, lock=None):
        self.root = Path(directory).resolve()
        self.lock = lock
        self.db = None
        try:
            path = self.root / 'project.sqlite'
            if not path.is_file() or path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                raise ValueError('项目数据库不存在或不是普通文件。')
            self.db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=5)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA foreign_keys=ON')
            if self.db.execute('PRAGMA user_version').fetchone()[0] != FORMAT_VERSION:
                raise ValueError('不支持此本地项目格式版本，未修改项目。')
            if self.db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('项目数据库已损坏。')
            self.project = dict(self.db.execute('SELECT * FROM project').fetchone())
            uuid.UUID(self.project['id'])
            self.project['directory'] = str(self.root)
        except Exception:
            self.close()
            raise

    @classmethod
    def create(cls, directory, name, ztz=None, lock=None):
        """在空项目目录创建格式，不覆盖已有数据库或未知文件。"""
        name = name.strip()
        if not name or len(name) > 200:
            raise ValueError('项目名称须为 1–200 个字符。')
        ztz = normalize_cartographer(ztz)
        root = Path(directory).resolve()
        root.mkdir(parents=True, exist_ok=True)
        if any(path.name != '.project.lock' for path in root.iterdir()):
            raise ValueError('请选择空目录创建项目，不覆盖已有文件。')
        with closing(sqlite3.connect(str(root / 'project.sqlite'))) as db:
            db.executescript('''
                PRAGMA user_version=1;
                PRAGMA foreign_keys=ON;
                CREATE TABLE project(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    ztz TEXT, created_at TEXT NOT NULL);
                CREATE TABLE images(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES project(id),
                    name TEXT NOT NULL, image_path TEXT NOT NULL UNIQUE, dem_path TEXT,
                    grid TEXT NOT NULL, fingerprint TEXT NOT NULL, removed_at TEXT,
                    active_generation TEXT, revision TEXT, saved_revision TEXT,
                    draft_id TEXT, recovery_id TEXT);
                CREATE TABLE snapshots(id TEXT PRIMARY KEY,
                    image_id TEXT NOT NULL REFERENCES images(id), path TEXT NOT NULL UNIQUE,
                    sha256 TEXT NOT NULL, revision TEXT NOT NULL, save_kind TEXT NOT NULL,
                    save_reason TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX snapshot_history ON snapshots(image_id, created_at);
            ''')
            with db:
                db.execute('INSERT INTO project VALUES(?,?,?,?)',
                           (str(uuid.uuid4()), name, ztz, now()))
        return cls(root, lock)

    def close(self):
        """只关闭数据库并释放锁，绝不删除项目文件。"""
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock is not None:
            self.lock.unlock()
            self.lock = None

    def update_project(self, name, ztz=None):
        """修改项目名称和制图者，保持 UUID 不变。"""
        name = name.strip()
        if not name or len(name) > 200:
            raise ValueError('项目名称须为 1–200 个字符。')
        ztz = normalize_cartographer(ztz)
        with self.db:
            self.db.execute('UPDATE project SET name=?,ztz=? WHERE id=?', (name, ztz, self.project['id']))
        self.project.update(name=name, ztz=ztz)
        return self.project.copy()

    def internal_path(self, relative):
        """拒绝绝对路径、链接及项目目录外部引用。"""
        if not relative or Path(relative).is_absolute():
            raise ValueError('项目内部路径无效。')
        path = self.root / relative
        if path.resolve() == self.root or self.root not in path.resolve().parents:
            raise ValueError('项目内部路径超出项目目录。')
        for part in (path, *path.parents):
            if part == self.root:
                break
            if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
                raise ValueError('项目内部文件不能使用链接。')
        return path

    def relative_path(self, path):
        """保存项目内部相对路径，使项目目录可以整体移动。"""
        relative = Path(path).absolute().relative_to(self.root).as_posix()
        self.internal_path(relative)
        return relative

    def _image(self, row):
        result = dict(row)
        result['grid'] = json.loads(result['grid'])
        result['fingerprint'] = json.loads(result['fingerprint'])
        return result

    def image(self, image_id, writable=False):
        """读取身份；已移除影像不能写入新草稿。"""
        row = self.db.execute('SELECT * FROM images WHERE id=?', (image_id,)).fetchone()
        if row is None or (writable and row['removed_at']):
            raise ValueError('影像不存在或已从项目移除。')
        return self._image(row)

    def images(self, include_removed=False):
        """列出本地关联，失效的外部路径仍然可管理。"""
        query = 'SELECT * FROM images' + ('' if include_removed else ' WHERE removed_at IS NULL')
        return [self._image(row) for row in self.db.execute(query + ' ORDER BY rowid')]

    def register_image(self, path, dem, grid):
        """同一路径沿用稳定 UUID；重新加入保留草稿。"""
        path = os.path.normcase(os.path.realpath(path))
        fingerprint = input_fingerprint(path)
        existing = self.db.execute('SELECT * FROM images WHERE image_path=?', (path,)).fetchone()
        if existing and (json.loads(existing['fingerprint']) != fingerprint):
            raise ValueError('此路径的影像内容已改变，请作为新影像登记。')
        if dem and not os.path.isfile(dem):
            raise ValueError('DEM 文件不存在。')
        image_id = existing['id'] if existing else str(uuid.uuid4())
        with self.db:
            if existing:
                self.db.execute('UPDATE images SET dem_path=?,removed_at=NULL WHERE id=?',
                                (os.path.realpath(dem) if dem else None, image_id))
            else:
                self.db.execute('''INSERT INTO images
                    (id,project_id,name,image_path,dem_path,grid,fingerprint) VALUES(?,?,?,?,?,?,?)''',
                    (image_id, self.project['id'], os.path.basename(path), path,
                     os.path.realpath(dem) if dem else None,
                     json.dumps(grid), json.dumps(fingerprint)))
        return self.image(image_id)

    def relocate(self, image_id, path, grid):
        """网格与有限内容指纹均匹配时才沿用原影像身份。"""
        row = self.image(image_id)
        old = row['grid']
        if (old['width'], old['height'], old['bands']) != (grid['width'], grid['height'], grid['bands']):
            raise ValueError('重新定位的影像尺寸或波段不匹配，请作为新影像登记。')
        import math
        if old['crs'] != grid['crs'] or not all(
                math.isclose(a, b, rel_tol=0, abs_tol=1e-8)
                for a, b in zip(old['transform'], grid['transform'])):
            raise ValueError('重新定位的影像 CRS 或参考网格不匹配。')
        if row['fingerprint'] != input_fingerprint(path):
            raise ValueError('重新定位的影像内容指纹不匹配，请作为新影像登记。')
        with self.db:
            self.db.execute('UPDATE images SET image_path=? WHERE id=?',
                            (os.path.normcase(os.path.realpath(path)), image_id))
        return self.image(image_id)

    def set_removed(self, image_id, removed):
        """只修改工作关联，不删除任何输入或历史。"""
        self.image(image_id)
        with self.db:
            self.db.execute('UPDATE images SET removed_at=? WHERE id=?',
                            (now() if removed else None, image_id))
        return self.image(image_id)

    def update_dem(self, image_id, path):
        """更新可选 DEM，不修改影像身份、草稿或历史。"""
        self.image(image_id)
        if path and not os.path.isfile(path):
            raise ValueError('DEM 文件不存在。')
        with self.db:
            self.db.execute('UPDATE images SET dem_path=? WHERE id=?',
                            (os.path.realpath(path) if path else None, image_id))
        return self.image(image_id)

    def working_directory(self, image_id):
        """为影像分配持久工作目录。"""
        self.image(image_id, writable=True)
        path = self.internal_path('images/{}/working'.format(image_id))
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    def _prepare_snapshot(self, image_id, source, revision, kind, reason):
        identifier = str(uuid.uuid4())
        path = self.internal_path('images/{}/snapshots/{}.gpkg'.format(image_id, identifier))
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.gpkg.part')
        snapshot_gpkg(source, temporary)
        digest = file_digest(temporary)
        with open(temporary, 'r+b') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return (identifier, image_id, self.relative_path(path), digest, revision, kind, reason, now())

    def register_generation(self, image_id, source, reason, revision=None):
        """文件完成后事务登记当前草稿及最新自动恢复；事务失败保留旧引用。"""
        row = self.image(image_id, writable=True)
        relative = self.relative_path(source)
        if Path(source).parent.resolve() != Path(self.working_directory(image_id)).resolve():
            raise ValueError('generation 必须位于当前影像的工作目录。')
        revision = revision or (row['revision'] if row['active_generation'] == relative else relative)
        snapshot = None
        if revision != row['revision'] or not row['recovery_id']:
            snapshot = self._prepare_snapshot(image_id, source, revision, 'automatic', reason)
        with self.db:
            if snapshot:
                self.db.execute('INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?)', snapshot)
            self.db.execute('UPDATE images SET active_generation=?,revision=?,recovery_id=? WHERE id=?',
                            (relative, revision, snapshot[0] if snapshot else row['recovery_id'], image_id))
        self._prune_automatic(image_id)
        return self.image(image_id)

    def save(self, image_id, source, manual=True, reason='手动保存'):
        """同一修订复用不可变快照；手动确认自动快照不再复制文件。"""
        row = self.register_generation(image_id, source, reason)
        with self.db:
            self.db.execute('UPDATE images SET draft_id=?,saved_revision=? WHERE id=?',
                            (row['recovery_id'], row['revision'], image_id))
            if manual:
                self.db.execute("UPDATE snapshots SET save_kind='manual',save_reason=? WHERE id=?",
                                (reason, row['recovery_id']))
        self._prune_automatic(image_id)
        return self.image(image_id)

    def snapshot(self, image_id, snapshot_id):
        """先核对身份及完整 SHA-256，再把文件交给 QGIS 验证几何。"""
        row = self.db.execute('SELECT * FROM snapshots WHERE id=? AND image_id=?',
                              (snapshot_id, image_id)).fetchone()
        if row is None:
            raise ValueError('快照不属于当前项目影像。')
        path = self.internal_path(row['path'])
        if not path.is_file() or file_digest(path) != row['sha256']:
            raise ValueError('草稿快照缺失或完整性校验失败。')
        return str(path)

    def has_saved_snapshot(self, image):
        """界面刷新只核对已登记文件存在性，点击操作再校验完整 SHA。"""
        if not image['draft_id']:
            return False
        row = self.db.execute('SELECT path FROM snapshots WHERE id=? AND image_id=?',
                              (image['draft_id'], image['id'])).fetchone()
        if row is None:
            return False
        try:
            path = self.internal_path(row['path'])
            return path.is_file() and path.stat().st_size > 0
        except (OSError, ValueError):
            return False

    def history(self, image_id):
        """显示手动历史、恢复前保留点及最新未手动确认的自动状态。"""
        image = self.image(image_id)
        return [dict(row) for row in self.db.execute('''SELECT * FROM snapshots
            WHERE image_id=? AND (save_kind IN ('manual','restore_backup') OR id=?)
            ORDER BY created_at DESC,rowid DESC''', (image_id, image['recovery_id']))]

    def retain_before_restore(self, image_id):
        """固定恢复前自动快照引用，不额外复制整份草稿。"""
        row = self.image(image_id, writable=True)
        if row['recovery_id']:
            with self.db:
                self.db.execute("UPDATE snapshots SET save_kind='restore_backup',save_reason='恢复前保留点' "
                                "WHERE id=? AND save_kind='automatic'", (row['recovery_id'],))

    def retain_restore_target(self, image_id, snapshot_id):
        """确认恢复后固定自动候选，防止当前编辑提交时轮换清理它。"""
        self.snapshot(image_id, snapshot_id)
        with self.db:
            self.db.execute("UPDATE snapshots SET save_kind='restore_backup' "
                            "WHERE id=? AND image_id=? AND save_kind='automatic'", (snapshot_id, image_id))

    def _prune_automatic(self, image_id):
        """只回收明确登记且不再被引用的自动快照；未知文件保持原样。"""
        image = self.image(image_id)
        obsolete = list(self.db.execute("SELECT id,path FROM snapshots WHERE image_id=? AND save_kind='automatic' "
                                        'AND id NOT IN (?,?)',
                                        (image_id, image['draft_id'] or '', image['recovery_id'] or '')))
        for row in obsolete:
            try:
                path = self.internal_path(row['path'])
                path.unlink(missing_ok=True)
                with self.db:
                    self.db.execute('DELETE FROM snapshots WHERE id=?', (row['id'],))
            except (OSError, ValueError, sqlite3.Error):
                # 清理失败不能改变已经完成的保存结果，下次操作仍可重试。
                continue
