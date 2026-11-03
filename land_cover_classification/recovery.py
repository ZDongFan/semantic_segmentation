# -*- coding: utf-8 -*-
"""登记本地已发布 generation，并安全恢复到当前影像的新 generation。"""

import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
import uuid

from qgis.PyQt.QtCore import QStandardPaths
from qgis.core import QgsVectorLayer, QgsRasterLayer, QgsWkbTypes

from .draft_session import layer_uri
from .project_client import snapshot_gpkg


def recovery_root():
    """恢复前备份位于应用数据目录，不依赖系统临时目录长期保留。"""
    return Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)) / 'LandCoverClassification' / 'recovery'


def write_record(panel, directory=None, generation=None, reason='编辑会话'):
    """只登记成功激活的 generation；临时候选文件绝不作为恢复入口。"""
    session = panel.dialog._draft_session
    if not panel.active or not session.is_active:
        return
    panel.export_context()
    directory = Path(directory or session.directory)
    generation = Path(generation or session.generation_path).resolve()
    if generation.parent != directory.resolve():
        raise ValueError('恢复文件必须位于记录所在目录。')
    data = {'schema': 1, 'service': panel._client().base_url,
            'project_id': panel.project['id'], 'image_id': panel.image['id'],
            'image_path': os.path.realpath(panel.image['image_path']),
            'base_draft_id': panel.image.get('draft_id'), 'generation': generation.name,
            'created_at': datetime.now(timezone.utc).isoformat(), 'reason': reason}
    temporary = directory / 'recovery.json.tmp'
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, directory / 'recovery.json')


def read_record(path, panel):
    """拒绝路径逃逸、关联不符与符号链接记录，不修改原始恢复文件。"""
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 65536:
        raise ValueError('恢复记录无效。')
    row = json.loads(path.read_text(encoding='utf-8'))
    if row.get('schema') != 1 or (row.get('service'), row.get('project_id'), row.get('image_id')) != (
            panel._client().base_url, panel.project['id'], panel.image['id']):
        raise ValueError('恢复记录不属于当前服务、项目和影像。')
    if os.path.normcase(row['image_path']) != os.path.normcase(os.path.realpath(panel.image['image_path'])):
        raise ValueError('恢复记录的输入影像不一致。')
    source = path.parent / row['generation']
    if source.is_symlink() or source.resolve().parent != path.parent.resolve() or not source.is_file():
        raise ValueError('恢复文件不存在或超出记录目录。')
    return {**row, 'path': str(source), 'record': str(path), 'kind': 'local'}


def local_records(panel):
    """仅枚举带身份记录的会话和备份；无法关联的旧文件需由用户显式导入。"""
    candidates = list(Path(tempfile.gettempdir()).glob('lcc_draft_session_*/recovery.json'))
    candidates += list(recovery_root().glob('lcc_recovery_*/recovery.json'))
    active = os.path.realpath(panel.dialog._draft_session.directory or '.')
    rows = []
    for path in candidates:
        if os.path.realpath(path.parent) == active:
            continue
        try:
            rows.append(read_record(path, panel))
        except (ValueError, OSError, KeyError, TypeError):
            continue
    return sorted(rows, key=lambda row: row['created_at'], reverse=True)


def validate_source(panel, source):
    """逐要素校验恢复文件，拒绝错误 CRS、重复标识与越界几何。"""
    panel.dialog._validate_draft_generation(source)
    layer = QgsVectorLayer(layer_uri(source), '恢复检查', 'ogr')
    reference = QgsRasterLayer(panel.image['image_path'], '恢复影像')
    if not reference.isValid() or layer.crs() != reference.crs() or not layer.crs().isValid():
        raise ValueError('恢复草稿与当前影像的 CRS 不一致。')
    ids = set()
    bounds = reference.extent()
    bounds.grow(max(bounds.width(), bounds.height()) * 1e-8)
    count = 0
    for feature in layer.getFeatures():
        identifier = str(uuid.UUID(str(feature['feature_uuid'])))
        geometry = feature.geometry()
        if identifier in ids or feature['origin'] not in ('user', 'inference'):
            raise ValueError('草稿要素编号重复或来源不合法。')
        if feature['class_id'] != 1 or feature['class_name'] != 'landslide':
            raise ValueError('草稿类别不符合滑坡契约。')
        if (geometry.isEmpty() or QgsWkbTypes.geometryType(geometry.wkbType()) != QgsWkbTypes.PolygonGeometry
                or not geometry.isGeosValid() or not bounds.contains(geometry.boundingBox())):
            raise ValueError('恢复草稿包含无效或超出影像范围的几何。')
        ids.add(identifier)
        count += 1
    return count


def restore_source(panel, source):
    """复制并验证候选，备份当前草稿后原子切换；失败保留原 generation。"""
    panel._check_idle()
    if not panel.active or panel.image.get('project_id') != panel.project['id']:
        raise ValueError('请先打开恢复记录所属的项目影像。')
    validate_source(panel, source)
    dialog = panel.dialog
    if dialog._layer_is_usable(dialog._draft_layer):
        if not dialog._commit_layer_if_needed(dialog._draft_layer):
            raise RuntimeError('当前编辑提交失败，恢复已取消。')
    if not dialog._ensure_draft_session(panel.image['image_path']):
        raise RuntimeError('无法创建恢复会话。')
    session = dialog._draft_session
    candidate = session.next_generation_path('recovered')
    snapshot_gpkg(source, candidate)
    validate_source(panel, candidate)
    root = recovery_root()
    root.mkdir(parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='lcc_recovery_', dir=str(root)))
    saved = backup / 'draft.gpkg'
    snapshot_gpkg(session.generation_path, saved)
    write_record(panel, backup, saved, '恢复前自动备份')
    dialog._stop_ai_editing(silent=True)
    dialog._publish_draft_generation(candidate)
    dialog._invalidate_fusion_undo()
    panel._synced_generation = None
    dialog._switch_to_draft_tab()
    panel.label.setText('草稿已恢复到本地，尚未保存到服务端；当前草稿的恢复前副本已保留。')
    return str(backup)
