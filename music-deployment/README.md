# EQUAI 音乐模型部署资料

这份目录只保存音乐服务的部署规划、模型版本和运行约定，不修改现有视频 API。

## 目标链路

```text
香港生产 API → 音乐任务队列 → 共绩单 GPU Worker
                             ├─ SheetSage2 / MERT-v2-FullSong
                             └─ YuE2-3B / YuE2-Vae
```

模型权重与现有 H3 模型共用 COS 的 `models/h3-standard/` 仓库：音乐模型放在
`models/h3-standard/music/`，沿用现有预热扫描根目录。共绩 Worker 启动时将模型
同步到本地持久化磁盘，校验 manifest 后预热；后续 jobs 只使用本地缓存和显存中的模型。

## COS 目录

```text
models/h3-standard/music/<release>/
├── yue2/YuE2-3B/
├── yue2-vae/YuE2-Vae/
├── sheetsage2/SheetSage2/
├── mert/MERT-v2-FullSong/
└── manifest.json
```

真实 COS bucket、region 和生产凭据只从香港服务器 `/opt/equai/api.env` 读取，不写入代码、文档或 Git。

已上传并核对的主权重大小：YuE2-3B `7,261,441,640` bytes、YuE2-Vae
`530,512,720` bytes、SheetSage2 `228,738,564` bytes、MERT-v2-FullSong
`2,529,812,848` bytes。核对时间为 2026-09-29，四个文件均与 COS 对象大小一致。

## 模型职责

- `YuE2-3B`：歌词、风格、ABC 旋律到完整歌曲。
- `YuE2-Vae`：YuE2 音频解码器。
- `SheetSage2`：从源音频提取旋律、和弦、节拍、调性和 ABC。
- `MERT-v2-FullSong`：SheetSage2 的父模型。

## 预热规则

1. 检查本地模型目录和 `manifest.json`。
2. 缺失或 SHA256 不一致时，从 COS 同步。
3. 先加载 SheetSage2，再释放或切换到 YuE2。
4. 运行最小音频预热测试。
5. 只有所有测试成功后，Worker 才标记为 `ready`。

## 镜像运行配置

`compose.music-worker.yaml` 和 `worker-runtime.env.example` 只负责把单 GPU
Worker、COS 模型版本、持久化模型目录和单任务队列接起来。当前默认
`MUSIC_PREWARM=0`，因此不会替你启动预热；模型已经在 COS 中，等镜像确认后
再由你手动执行预热即可。

## 安全约定

- COS 密钥、共绩 token、HF/ModelScope token 只放服务器环境变量或密钥管理器。
- 不把凭据写进模型包、Docker 镜像、request.json 或日志。
- 现有账户数据库和视频数据不放进音乐模型目录。

## 公网发布配置

所有已启用商户都可使用音乐功能。生产需设置 `H3_MUSIC_ENABLED=1`、有效的
`GONGJI_MUSIC_IMAGE` 和 `H3_MUSIC_PRICE_MILLI=500`（每首成功生成扣 0.5 积分）；
账户状态、余额冻结、失败退款和现有商户任务并发规则仍由 API 与任务队列处理。
无需配置商户白名单。若 Worker 不可用，可将 `H3_MUSIC_ENABLED` 设为 `0` 暂停新任务。
