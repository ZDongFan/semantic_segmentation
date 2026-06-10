# SAM 模型权重目录

请将 SAM1 ViT-B 模型权重 `sam_vit_b_01ec64.pth` 放在以下默认路径:

```
land_cover_classification/models/sam/sam_vit_b_01ec64.pth
```

默认文件说明:

- 名称: SAM ViT-B
- 文件: sam_vit_b_01ec64.pth
- 大小: ~358MB
- 用途: 供 AI 辅助编辑功能默认加载
- 分发方式: 仓库不直接附带该权重文件,需用户或部署方自行准备

如果你需要升级为新的 ViT-B 权重,可以直接覆盖同名文件,插件下次启动 AI 编辑
时会优先读取该默认路径。

若需要切换为其他规模的 SAM 模型(ViT-L、ViT-H),请同步修改插件
`sam_deps_check.py` 中的 `DEFAULT_MODEL_RELATIVE`,以及 sam_worker
启动时传入的 `model_type`。

注意:

- 插件不会自动联网下载权重文件。
- 如果替换了权重文件后仍使用旧的 `model_type`,可能导致加载失败。
- 仅替换 `.pth` 文件而不修改代码时,建议继续使用 ViT-B 兼容权重。
