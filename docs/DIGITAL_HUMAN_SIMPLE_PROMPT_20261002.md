# 数字人 1/2 简洁提示词（2026-10-02）

用户反馈两种模式口型仍不合格，要求提示词简洁、图片与音频明确对应。本次只调整提示词，不改模型、采样步数、SelfLift、SolAttn、素材处理或分段时间线。

## 当前固定 H3 提示词

```text
subject_definitions:
<Subject 1> 是 <Picture 1> 中的人物。
summary:
[reference generation] <Subject 1> 跟随 <Audio 1> 的内容表演。
retention_analysis:
<Subject 1>: fully_preserved from <Picture 1>.
detailed_description:
[Shot 1] <Subject 1> 的口型严格与 <Audio 1> 同步。
overall_soundscape:
保留 <Audio 1> 原声。
non_diegetic_music:
N/A
```

- 数字人一、数字人二使用同一个模板，每段都使用当前段 Audio 1。
- 删除音频的 Subject 2 定义，以及额外的分段续接、重复音节、时长和画幅说明；画幅由 API 参数决定。
- 用户未填写额外要求时不调用 DeepSeek。
- 有额外要求时只调用一次 DeepSeek，固定模板不可改写，只在 detailed_description 追加用户内容；若改错人物/音频编号或删除同步要求，则退回固定模板并保留用户原文。
- 历史任务和已生成视频不被改写，新提交任务使用新模板。

## 部署与检查

- 相关测试合计 62 项通过，包括真实 chat transport 的模板注册检查。
- 已补齐此前缺失的 DigitalHumanRef2VA 模板注册，避免有额外要求时触发 KeyError。
- 新镜像：`equai-api:avatar-simple-prompt-r2-20261002`，基于当前生产镜像，只替换三个提示词相关 Python 文件。
- 已部署腾讯云 `equai-api-local`；公网 `https://equai.vip/health` 返回 ready/local。
- 旧容器保留为 `equai-api-local-pre-simple-prompt-20261002`，可用于回滚。
- 原有环境变量、端口、挂载、重启策略保持现有配置。
- 已从真实接口各提交一条任务，确认新任务的实际 payload 两种模式均为上述模板，图片 images、音频 audios 顺序一致。

## 真实生成实验

人物图沿用此前同一素材，原唱歌音频《童话》截取前 15 秒，额外提示词留空，画幅 9:16。

- 数字人一：`vid_a5749217374348aa8cc50a894691170c`。
- 数字人二：`vid_bd8ffb1554ce486aa9d5a59eb25f418f`。
- 两条任务均已完成，实际供应商结果如下：
  - 数字人一：任务 `vid_a5749217374348aa8cc50a894691170c`，Gongji `3328598`，GPU 306 秒，计费 `0.245827`，输出 15.000 秒。
  - 数字人二：任务 `vid_bd8ffb1554ce486aa9d5a59eb25f418f`，Gongji `3328597`，GPU 348 秒，计费 `0.276870`，输出 15.000 秒。
- 两个文件均通过 ffprobe 和完整音视频解码检查，均为 H.264 + AAC，含原始音频轨道；口型是否符合预期由用户观看确认。
- 本地结果：[数字人一视频](../output/digital-human1-simple-prompt-15s-20261002.mp4)、[数字人二视频](../output/digital-human2-simple-prompt-15s-20261002.mp4)。

## 27 秒续段口型修复

排查发现，续段音频切片对 MP3 使用了不精确的包级定位，第二段的驱动音频起点会产生偏移。现已改为对解码后的音频流按时间精确裁切，并保留 22 帧 latent 上下文。

- 修复包：`c6912df9409beb78a24c70e1f2fd14bd92929d9055a1570530d997db2fab9b57`
- 修复测试：数字人音频与长视频相关测试 `32 passed`。
- 修复版任务：`vid_4547c071f0b045c0abe71977d1425889`，27.625 秒，GPU 558 秒，供应商成本 `0.449704`。
- 修复版视频：[数字人二 27 秒口型修复版](../output/digital-human2-simple-prompt-27s-lipsync-fix-20261002.mp4)。

第二次复核又发现合并器把第一段也提前裁了 4 帧，造成第二段画面整体提前约 0.167 秒；现已改为只裁续段开头的 4 帧。最终复测任务 `vid_44c9c6cb72b547d2830c1d2c67749ba9` 已成功，GPU 569 秒，供应商成本 `0.470679`，结果在桌面文件 `digital-human2-simple-prompt-27s-lipsync-final-20261002.mp4`。
