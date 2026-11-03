"""验证内部历史边界与真实 PostGIS 差异查询。"""

import uuid
import shutil
from unittest.mock import Mock

import fiona
import pytest
from fastapi.testclient import TestClient

from backend.api import create_app
from backend.service import ServiceError
from .test_postgis import create_scene, publish, add_image


def test_history_routes_are_internal_and_validate_ids(settings):
    service = Mock()
    service.versions.return_value = []
    service.drafts.return_value = {'items': [], 'next_offset': None}
    service.compare_versions.return_value = {'items': [], 'summary': {}}
    project, image, before, after = [str(uuid.uuid4()) for _ in range(4)]
    internal = TestClient(create_app(True, settings, service))
    public = TestClient(create_app(False, settings, service))
    base = '/internal/projects/' + project
    assert internal.get(base + '/versions').status_code == 200
    assert internal.get(base + '/images/' + image + '/drafts').status_code == 200
    assert internal.get(base + '/compare', params={'before': before, 'after': after}).status_code == 200
    assert internal.get(base + '/compare', params={'before': before, 'after': after, 'limit': 201}).status_code == 422
    assert internal.get(base + '/compare', params={'before': 'invalid', 'after': after}).status_code == 422
    assert public.get(base + '/versions', headers={'X-API-Key': settings.public_key}).status_code == 404


def test_compare_ignores_publication_metadata_and_rejects_foreign(postgis, draft):
    project, image = create_scene(postgis, draft)
    before = publish(postgis, project, image)
    after = publish(postgis, project, image, before['id'])
    result = postgis.compare_versions(project['id'], before['id'], after['id'])
    assert result['summary'] == {'unchanged': 1}
    assert result['items'] == []
    other = postgis.create_project('其他项目')
    with pytest.raises(ServiceError) as error:
        postgis.compare_versions(other['id'], before['id'], after['id'])
    assert error.value.status == 404
    assert postgis.versions(other['id'], allow_empty=True) == []
    with pytest.raises(ServiceError):
        postgis.versions(other['id'])


def test_compare_added_removed_modified_and_pagination(postgis, draft, tmp_path):
    project, image = create_scene(postgis, draft)
    before = publish(postgis, project, image)
    with fiona.open(draft, layer='draft') as src:
        schema, crs = src.schema, src.crs_wkt
        feature = next(iter(src))
        geometry, props = dict(feature.geometry), dict(feature.properties)
    modified = tmp_path / 'modified.gpkg'
    props['source_id'] = 77
    with fiona.open(modified, 'w', driver='GPKG', layer='draft', crs_wkt=crs, schema=schema) as dst:
        dst.write({'geometry': geometry, 'properties': props})
    saved = postgis.save_draft(project['id'], image['id'], modified, image['draft_id'])
    second = add_image(postgis, project['id'], draft, 'second')
    after = publish(postgis, project, image, before['id'])
    result = postgis.compare_versions(project['id'], before['id'], after['id'], 0, 1)
    assert result['summary'] == {'modified': 1, 'added': 1}
    assert result['next_offset'] == 1
    page = postgis.compare_versions(project['id'], before['id'], after['id'], 1, 1)
    assert page['next_offset'] is None
    assert result['items'][0]['image_id'] != page['items'][0]['image_id']
    reverse = postgis.compare_versions(project['id'], after['id'], before['id'])
    assert reverse['summary'] == {'modified': 1, 'removed': 1}
    # 仅几何变化也应单独标为修改，不依赖属性面积变化。
    with postgis.connection() as db:
        db.execute('UPDATE lcc_features SET geom=ST_Translate(geom,0.0001,0) WHERE version_id=%s AND image_id=%s',
                   (after['id'], image['id']))
    result = postgis.compare_versions(project['id'], before['id'], after['id'])
    assert result['summary']['modified'] == 1


def test_draft_history_is_scoped_and_keeps_current_pointer(postgis, draft, tmp_path):
    project, image = create_scene(postgis, draft)
    original = image['draft_id']
    copy = tmp_path / 'another.gpkg'
    shutil.copy2(draft, copy)
    with fiona.open(copy, 'a', layer='draft') as dst:
        with fiona.open(draft, layer='draft') as src:
            feature = next(iter(src))
            props = dict(feature.properties)
            props['feature_uuid'] = str(uuid.uuid4())
            dst.write({'geometry': dict(feature.geometry), 'properties': props})
    saved = postgis.save_draft(project['id'], image['id'], copy, original)
    history = postgis.drafts(project['id'], image['id'], 0, 1)
    assert history['items'][0]['id'] == saved['draft_id']
    assert history['items'][0]['is_current']
    assert 'file_path' not in history['items'][0]
    assert history['next_offset'] == 1
    assert postgis.draft(project['id'], image['id'], original)['id'] == original
    assert postgis.images(project['id'])[0]['draft_id'] == saved['draft_id']
    other = postgis.create_project('其他项目')
    with pytest.raises(ServiceError):
        postgis.draft(other['id'], image['id'], original)
    assert postgis.drafts(other['id'], image['id'])['items'] == []


def test_history_http_download_and_comparison(postgis, draft):
    import hashlib
    project, image = create_scene(postgis, draft)
    first = publish(postgis, project, image)
    second = publish(postgis, project, image, first['id'])
    client = TestClient(create_app(True, postgis.settings, postgis))
    base = '/internal/projects/' + str(project['id'])
    versions = client.get(base + '/versions')
    assert versions.status_code == 200 and len(versions.json()) == 2
    response = client.get(base + '/compare', params={'before':str(first['id']), 'after':str(second['id'])})
    assert response.status_code == 200
    assert response.json()['summary'] == {'unchanged':1}
    path = base + '/images/' + str(image['id'])
    records = client.get(path + '/drafts').json()['items']
    response = client.get(path + '/draft', params={'draft_id':records[0]['id']})
    assert response.status_code == 200
    assert response.headers['X-Draft-ID'] == records[0]['id']
    assert hashlib.sha256(response.content).hexdigest() == records[0]['sha256']
    assert client.get(path + '/draft', params={'draft_id':str(uuid.uuid4())}).status_code == 404
