"""插件与服务共用的成果字段契约，仅依赖标准库。"""

import math
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class ResultField:
    """逻辑字段及 Shapefile 存储宽度、精度。"""

    name: str
    label: str
    kind: str
    length: int = 0
    shape_width: int = 0
    precision: int = 0


RESULT_FIELDS = (
    ResultField("tid", "遥感解译图编号", "string", 32, 32),
    ResultField("ygtsm", "遥感解译图说明", "text", 0, 254),
    ResultField("blc", "比例尺", "integer"),
    ResultField("ztz", "制图者", "string", 32, 128),
    ResultField("fhz", "复核者", "string", 32, 128),
    ResultField("shz", "审核者", "string", 32, 128),
    ResultField("sdz", "审定者", "string", 32, 128),
    ResultField("rq", "制图日期", "date"),
    ResultField("bz", "备注", "text", 0, 254),
    ResultField("lx", "类型", "string", 32, 128),
    ResultField("hdfx", "滑动方向", "string", 32, 128),
    ResultField("cd", "长度", "float", 10, 10, 2),
    ResultField("pjkd", "平均宽度", "float", 10, 10, 2),
    ResultField("mj", "面积", "float", 10, 24, 3),
    ResultField("wzcf", "物质成分", "string", 32, 128),
    ResultField("yfys", "诱发因素", "string", 32, 128),
    ResultField("jytz", "解译特征描述", "text", 0, 254),
)
RESULT_FIELD_NAMES = tuple(field.name for field in RESULT_FIELDS)


def normalize_cartographer(value):
    """去除制图者首尾空白，按 Unicode 字符限制长度。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("制图者必须为文本。")
    value = value.strip()
    if len(value) > 32:
        raise ValueError("制图者最多 32 个字符。")
    return value or None


def result_context(image_id, ztz=None):
    """在创建或覆盖文件前校验图级上下文，空成果也必须校验。"""
    try:
        tid = uuid.UUID(str(image_id)).hex
    except (ValueError, TypeError, AttributeError):
        raise ValueError("成果需要合法的项目影像 ID。") from None
    return tid, normalize_cartographer(ztz)


def build_result_attributes(image_id, ztz=None, *, mj):
    """生成全部标准属性；调用方负责按最终几何计算平方米面积。"""
    tid, ztz = result_context(image_id, ztz)
    if isinstance(mj, bool) or not isinstance(mj, (int, float)) or not math.isfinite(mj) or mj <= 0:
        raise ValueError("成果面积必须为有限正数（平方米）。")
    attributes = dict.fromkeys(RESULT_FIELD_NAMES)
    attributes.update(tid=tid, ztz=ztz, mj=float(mj))
    return attributes
