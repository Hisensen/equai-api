# 电商复刻直传创建失败（2026-10-10）

线上创建返回 500 的根因是专用 API 镜像 overlay 的 `video_api.py` 在 `direct_assets_as_uploads()` 使用 `Headers` 时缺少导入，同时缺少 COS 回退分支所需的 `download_path` 导入（并补齐 `io`）。生产日志对应 `NameError: name 'Headers' is not defined`。

已修复 `.gpu-staging/commerce-create-build/video_api.py`，构建并部署 `equai-api:commerce-direct-create-20261010-v2`。原 v1 容器以 `equai-api-local-pre-commerce-direct-v2-20261010` 保留供回滚。部署仅更换 `equai-api-local`，音乐 API sidecar 与上传 relay 的容器 ID 未变化；任务状态计数未变化。

验证：38 项 `test_z_commerce.py` 和 8 项 `test_local_transfer.py` 均通过；在目标镜像内以 `--network none`、临时数据库、GPU dispatch 循环禁用的方式提交合成 5 秒 MP4 和商品图，电商复刻 API 返回 202，创建 queued 任务且素材登记为本地存储。部署后 `/health` 返回 ready，电商复刻路由导入探针通过，未向线上 GPU 提交真实任务。
