# -*- coding: utf-8 -*-
"""登记本地已发布 generation，并安全恢复到当前影像的新 generation。"""

import json
import hashlib
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
import uuid

from qgis.core import QgsVectorLayer, QgsRasterLayer, QgsWkbTypes

from .draft_session import layer_uri
from .project_client import snapshot_gpkg


def manual_save_key(panel):
    """按服务、项目及影像隔离本机手动保存时间，供离线恢复筛选使用。"""
    identity = json.dumps([panel._client().base_url, panel.project['id'], panel.image['id']])
    return 'LandCoverClassification/manual_draft_save/' + hashlib.sha256(identity.encode('utf-8')).hexdigest()


def mark_manual_saved(panel):
    """手动保存成功后记录本机时间，抑制已被确认保存的旧本地恢复状态。"""
    panel.settings.setValue(manual_save_key(panel), datetime.now(timezone.utc).isoformat())


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
            'created_at': datetime.now(timezone.utc).isoformat(), 'reason': reason,
            'pending': panel._synced_generation != (panel.image['id'], session.generation_path)}
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
    """仅保留最新的未同步编辑会话；独立备份不再作为恢复入口。"""
    candidates = list(Path(tempfile.gettempdir()).glob('lcc_draft_session_*/recovery.json'))
    active = os.path.realpath(panel.dialog._draft_session.directory or '.')
    rows = []
    for path in candidates:
        if os.path.realpath(path.parent) == active:
            continue
        try:
            row = read_record(path, panel)
            if row.get('reason') not in ('恢复前自动备份', '移除影像前备份'):
                rows.append(row)
        except (ValueError, OSError, KeyError, TypeError):
            continue
    rows.sort(key=lambda row: record_time(row['created_at']), reverse=True)
    manual_at = record_time(panel.settings.value(manual_save_key(panel), ''))
    if not rows or not rows[0].get('pending', True) or record_time(rows[0]['created_at']) <= manual_at:
        return []
    return [{**rows[0], 'save_kind': 'automatic'}]


def record_time(value):
    """统一比较服务端与本地记录的带时区时间。"""
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)


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
    """复制并验证候选后原子切换，不另建备份；失败保留原 generation。"""
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
    dialog._stop_ai_editing(silent=True)
    dialog._publish_draft_generation(candidate)
    dialog._invalidate_fusion_undo()
    panel._synced_generation = None
    dialog._switch_to_draft_tab()
    panel.label.setText('草稿已恢复到本地，尚未保存到服务端。')
