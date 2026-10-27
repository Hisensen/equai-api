# 2026-10-07 电商作品展示与音乐编辑排查

## 线上根因

- 电商复刻成功后，`commerce_remix.py` 将 `_tool` 改成 `remix` 并将深度任务标记为 `_remix.commerce_depth_worker=true`。门户仍按 `tool=commerce_remix` 筛选，导致 GPU 已完成的电商作品从列表消失。
- 音乐任务使用独立 `h3-music-worker` 镜像，但 `gongji.create_music_job()` 没有传自定义启动脚本。`create_job()` 因腾讯云本地运行包存在而套用了普通视频 Worker 的启动脚本，最后把音乐请求交给视频 API，Worker 报 `mode 必须是 ...` 并失败。旧 API 又没启动 music-only 状态轮询，所以失败任务长期显示生成中。

## 修复与发布

- `video_api._list_for()` 的电商筛选现在同时匹配原始 `commerce_remix` 与已交给 H3 的深度复刻任务；`_task_view()` 将后者的工具类型仍返回为 `commerce_remix`。
- API lifespan 增加独立 `scheduler.poll_loop(music_only=True)`，音乐任务终态会及时回写。
- `gongji.create_music_job()` 显式启动镜像内的 `/app/worker.py`，避免使用普通视频运行包和 `/app/entrypoint.sh`。
- 腾讯云主 API 已热更新并重启；上传中继、音乐 sidecar 和 GPU 镜像未改动。更新前备份位于腾讯云 `/data/equai-local/deploy-backups/commerce-music-fix-20261007/`，回滚镜像标签为 `equai-api:commerce-music-fix-20261007-r2`。

## 验证

- 本地定向回归：54 passed，包括音乐 Worker 启动脚本、API lifespan 音乐轮询和电商任务后续仍可出现在电商筛选列表。
- 腾讯云 `/health` ready；最近失败的音乐任务 `mus_09426d54204e49f8adc23486b06beb50` 已结算为 failed、积分状态 released，当前 `music_running` 数量为 0。
- 电商任务 `vid_4afe4b2000104090a4460a092da2d8ef` 在门户筛选中可见，返回 `tool=commerce_remix` 且有预览 URL。
- 使用之前已上传到腾讯云的音频跑真实端到端回归任务 `mus_588fd25f0b424b229c0f83ef12dbea12`：自动歌词识别、GPU 生成、结果回传和试听/下载链接均成功；生成 MP3（1,559,661 bytes）与 FLAC（11,637,295 bytes），成功扣除 0.5 pt。
