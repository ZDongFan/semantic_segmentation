# 模型目录结构

插件会扫描指定的 *模型根目录*,把每个一级子目录尝试识别为一个 PaddleRS
分割模型并加入下拉框。

默认模型根目录是
`land_cover_classification/models/semantic_segmentation/`(相对于插件安装
路径解析)。可以在对话框的「模型根目录」字段中改到任意目录;所选路径
持久化在 `QSettings("LandCoverClassification/model_root")` 中。

## 单个模型的目录文件

每个模型放在独立子目录里 —— 这个子目录就是 PaddleRS 所说的「模型目录」,
也就是 `paddlers.deploy.Predictor()` 的入参。必需文件:

```
models/semantic_segmentation/
└── unet/                                # 子目录名任意
    ├── model.yml                        # 必需 —— 用于识别是否是分割模型
    ├── model.pdmodel                    # 静态图结构
    ├── model.pdiparams                  # 权重参数
    └── pipeline.yml                     # 推理流水线 / 预处理配置
```

`model.yml` 中 `_Attributes.model_type` 必须是 `segmenter`,否则该子目录
会被静默跳过(原因会写入 QGIS 消息日志的 **LandCoverClassification** 标签
之下)。`Model` 字段会作为下拉框中显示的模型名。

## 模型获取途径

1. **PaddleRS 官方模型库** —— 在
   [PaddleCV-SIG/PaddleRS](https://github.com/PaddleCV-SIG/PaddleRS) 文档中
   下载预训练分割模型,注意要下载「导出 / 推理」版本,而不是训练 checkpoint。
2. **自训模型导出** —— 用 `paddlers.tasks.SegModel` 训练后,通过
   `SegModel.export_inference_model(save_dir=...)` 导出,产物可直接放入。
3. **复用其他项目中的模型** —— 任何 PaddleRS 分割模型导出目录都可直接拷入。

## 跳过规则

满足以下任一条件的子目录会被静默跳过,并在日志中留下说明:

- 没有 `model.yml`
- `model.yml` 解析失败
- 缺少 `_Attributes.model_type` 字段
- `_Attributes.model_type` 不是 `"segmenter"`(例如目标检测、场景分类、
  变化检测等其他类型)
