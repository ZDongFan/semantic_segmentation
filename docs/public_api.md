# 对外成果查询 API

本文档对应 `backend.api:public_app` 的五个只读接口。部署方提供公共服务的 HTTPS 基础地址；下文以 `https://api.example.com` 为占位示例。项目、版本和要素只来自已发布成果。

## 通用约定

| 项目 | 说明 |
| --- | --- |
| 请求方法 | 均为 `GET`，无请求体 |
| 鉴权 | 每次请求都传入请求头 `X-API-Key: <API_KEY>` |
| 标识 | `project_id`、`version_id` 均为 UUID 字符串 |
| 版本选择 | 未传 `version_id` 时读取当前发布版本；指定版本不存在或不属于该项目时返回 404，不回退当前版本 |
| 时间 | 日期时间为 ISO 8601 字符串 |
| 成功响应 | 项目列表与详情是 JSON；要素是带分页字段的 GeoJSON；下载是 GeoPackage 文件 |

以下 UUID、摘要、坐标和地址仅用于展示格式，不代表实际数据。

## 1. 列出已发布项目

`GET /projects`

只返回有当前发布版本的项目，按项目创建时间、项目 ID 升序排列。没有已发布项目时返回 `[]`。

```json
[
  {
    "id": "11111111-1111-4111-8111-111111111111",
    "name": "示例项目",
    "current_version": "22222222-2222-4222-8222-222222222222",
    "created_at": "2026-09-28T08:00:00+00:00",
    "published_at": "2026-09-28T09:00:00+00:00",
    "sha256": "<64 位十六进制 SHA-256>"
  }
]
```

`current_version` 为当前版本 ID；`published_at` 是其发布时间，`sha256` 是完整 GeoPackage 文件摘要。

## 2. 获取项目版本详情

`GET /projects/{project_id}`

| 参数 | 位置 | 必填 | 说明 |
| --- | --- | --- | --- |
| `project_id` | 路径 | 是 | 项目 UUID |
| `version_id` | 查询 | 否 | 指定发布版本 UUID；省略时查询当前版本 |

响应示例：

```json
{
  "id": "11111111-1111-4111-8111-111111111111",
  "name": "示例项目",
  "current_version": "22222222-2222-4222-8222-222222222222",
  "version": {
    "id": "22222222-2222-4222-8222-222222222222",
    "project_id": "11111111-1111-4111-8111-111111111111",
    "base_version": null,
    "created_at": "2026-09-28T09:00:00+00:00",
    "sha256": "<64 位十六进制 SHA-256>",
    "image_ids": ["33333333-3333-4333-8333-333333333333"],
    "feature_count": 1
  }
}
```

`version.id` 是实际查询的版本；查询历史版本时可与 `current_version` 不同。`base_version` 是发布所基于的版本，首版为 `null`。`image_ids` 是该版本实际纳入的影像 ID，按 ID 排序；`feature_count` 是该版本全部影像的要素总数。

## 3. 列出历史版本

`GET /projects/{project_id}/versions`

`project_id` 为必填路径 UUID。响应是 JSON 数组，按发布时间降序排列，同一时间按版本 ID 排序。每项包含上节 `version` 对象中的所有字段，另有 `is_current` 布尔值，标明是否为项目当前版本。

```json
[
  {
    "id": "22222222-2222-4222-8222-222222222222",
    "project_id": "11111111-1111-4111-8111-111111111111",
    "base_version": null,
    "created_at": "2026-09-28T09:00:00+00:00",
    "sha256": "<64 位十六进制 SHA-256>",
    "image_ids": ["33333333-3333-4333-8333-333333333333"],
    "feature_count": 1,
    "is_current": true
  }
]
```

项目不存在或尚未发布时返回 404。

## 4. 分页获取要素

`GET /projects/{project_id}/features`

| 参数 | 位置 | 必填 | 默认值 / 约束 |
| --- | --- | --- | --- |
| `project_id` | 路径 | 是 | 项目 UUID |
| `version_id` | 查询 | 首页否，后续页是 | 发布版本 UUID；首页省略时选当前版本 |
| `offset` | 查询 | 否 | 默认 0，非负整数 |
| `limit` | 查询 | 否 | 默认 100，范围 1–1000 |

响应为扩展了 `version_id` 和 `next_offset` 字段的 GeoJSON `FeatureCollection`。几何坐标使用 EPSG:4326，类型为 `MultiPolygon`；空结果时 `features` 为 `[]`。

```json
{
  "type": "FeatureCollection",
  "version_id": "22222222-2222-4222-8222-222222222222",
  "features": [
    {
      "type": "Feature",
      "id": "33333333-3333-4333-8333-333333333333:44444444-4444-4444-8444-444444444444",
      "geometry": {
        "type": "MultiPolygon",
        "coordinates": [[[[110.0, 30.0], [110.1, 30.0], [110.1, 30.1], [110.0, 30.0]]]]
      },
      "properties": {
        "feature_id": "44444444-4444-4444-8444-444444444444",
        "class_id": 1,
        "class_name": "landslide",
        "source_id": 1,
        "project_id": "11111111-1111-4111-8111-111111111111",
        "image_id": "33333333-3333-4333-8333-333333333333",
        "version_id": "22222222-2222-4222-8222-222222222222",
        "run_id": null,
        "created_at": "2026-09-28T09:00:00+00:00",
        "perimeter_m": 40.0,
        "center_lon": 110.05,
        "center_lat": 30.03,
        "tid": "33333333333343338333333333333333",
        "ygtsm": null,
        "blc": null,
        "ztz": "张三",
        "fhz": null,
        "shz": null,
        "sdz": null,
        "rq": null,
        "bz": null,
        "lx": null,
        "hdfx": null,
        "cd": null,
        "pjkd": null,
        "mj": 100.0,
        "wzcf": null,
        "yfys": null,
        "jytz": null
      }
    }
  ],
  "next_offset": null
}
```

`Feature.id` 由 `image_id:feature_id` 组成，用于跨影像区分要素。面积单位为平方米，周长单位为米；`run_id` 可为 `null`。要素按 `image_id`、`feature_id` 稳定排序。每页最多返回 `limit` 个；有下一页时 `next_offset` 给出下一页偏移量，否则为 `null`。

首次请求使用 `offset=0`，保存响应的 `version_id`。后续请求必须带上该版本 ID 和上一页的 `next_offset`，直到 `next_offset` 为 `null`。`offset > 0` 且未传 `version_id` 返回 422。

```text
GET /projects/{project_id}/features?offset=0&limit=100
GET /projects/{project_id}/features?version_id={首个响应的 version_id}&offset={next_offset}&limit=100
```

## 5. 下载完整成果

`GET /projects/{project_id}/download`

| 参数 | 位置 | 必填 | 说明 |
| --- | --- | --- | --- |
| `project_id` | 路径 | 是 | 项目 UUID |
| `version_id` | 查询 | 否 | 指定发布版本 UUID；省略时下载当前版本 |

成功时返回完整 GeoPackage 二进制文件，响应头包括：

| 响应头 | 值 |
| --- | --- |
| `Content-Type` | `application/geopackage+sqlite3` |
| `Content-Disposition` | 下载文件名为 `project_{version_id}.gpkg` |
| `X-Version-ID` | 实际下载的发布版本 UUID |
| `ETag` | 文件的 SHA-256 摘要 |

GeoPackage 按影像分层，图层名为 `image_<影像 UUID 去横线>`。文件内几何保留成果源 CRS；如需 EPSG:4326 的要素 JSON，使用 `/features`。

## 错误响应

业务错误统一返回 `{"detail": "错误说明"}`。FastAPI 参数校验产生的 422 中，`detail` 通常是结构化错误列表。

| 状态码 | 触发场景 |
| --- | --- |
| 401 | 缺少或传错 `X-API-Key` |
| 404 | 项目不存在、尚未发布，指定版本不存在或不属于该项目；公共应用没有内部管理路由 |
| 422 | UUID、分页参数不合法，或后续页未提供 `version_id` |
| 503 | 下载所需成果文件不可用 |
| 500 | 服务内部异常；响应不包含内部路径等诊断信息 |

409 是内部草稿或发布的并发冲突，413 是内部草稿上传超限；这五个只读接口正常调用时不使用这两个状态码。

## 调用示例

```bash
curl -H 'X-API-Key: <API_KEY>' 'https://api.example.com/projects'
curl -H 'X-API-Key: <API_KEY>' 'https://api.example.com/projects/{project_id}/features?offset=0&limit=100'
curl -H 'X-API-Key: <API_KEY>' -o result.gpkg 'https://api.example.com/projects/{project_id}/download?version_id={version_id}'
```

当前公共 API 不提供 bbox、裁剪、单景下载或区域查询。



## 标准字段与空值

每个要素 properties 精确包含 29 个业务属性，完整字段、类型和来源表见 [标准成果属性](project_service.md#标准成果属性2026-09)。原 area_m2 已归并为自动计算的 mj（平方米，WGS84 椭球，双精度不舍入）。tid 为所属 image_id 的 UUID hex，同景不同要素及跨版本保持不变。制图者 ztz 继承项目创建时的可选值；未填写时为 null。ygtsm 为正确字段名；两个 bz 合并。所有 17 个标准键均返回，不因值为空省略；除 tid、mj 和选填 ztz 外本期均为 null。class_name 保留，lx、cd、pjkd、rq 不从旧字段推算。内部数据库 JSONB、GeoPackage 与本接口使用发布时同一属性字典，查询不临时补字段。
