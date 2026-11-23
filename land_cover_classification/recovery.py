"""校验本地项目快照并通过已有 generation 原子发布流程恢复。"""

import uuid

from qgis.core import QgsVectorLayer, QgsRasterLayer, QgsWkbTypes

from .draft_session import layer_uri
from .local_project import snapshot_gpkg


def validate_source(panel, source, image=None):
    """逐要素校验 CRS、边界、分类、来源、唯一标识及几何。"""
    panel.dialog._validate_draft_generation(source)
    layer = QgsVectorLayer(layer_uri(source), '恢复检查', 'ogr')
    reference = QgsRasterLayer((image or panel.image)['image_path'], '恢复影像')
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
            raise ValueError('草稿包含无效或超出影像范围的几何。')
        ids.add(identifier)
        count += 1
    return count


def restore_source(panel, snapshot_id):
    """保留恢复前快照引用后发布新 generation，失败不破坏当前草稿。"""
    panel._check_idle()
    panel.export_context()
    source = panel.store.snapshot(panel.image['id'], snapshot_id)
    validate_source(panel, source)
    panel.store.retain_restore_target(panel.image['id'], snapshot_id)
    dialog = panel.dialog
    if dialog._layer_is_usable(dialog._draft_layer):
        if not dialog._commit_layer_if_needed(dialog._draft_layer):
            raise RuntimeError('当前编辑提交失败，恢复已取消。')
    panel.record_recovery('恢复前保留点')
    if panel._save_error:
        raise RuntimeError('无法登记当前编辑，恢复已停止。')
    panel.store.retain_before_restore(panel.image['id'])
    candidate = dialog._draft_session.next_generation_path('recovered')
    snapshot_gpkg(source, candidate)
    validate_source(panel, candidate)
    dialog._stop_ai_editing(silent=True)
    dialog._publish_draft_generation(candidate)
    dialog._invalidate_fusion_undo()
    dialog._switch_to_draft_tab()
    if not panel._save_error:
        panel.label.setText('草稿已恢复，待本地保存；恢复前草稿已保留。')
