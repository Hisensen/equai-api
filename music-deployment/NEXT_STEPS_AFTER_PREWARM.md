# EQUAI 音乐生成功能：预热完成后的交接手册

这份文档交给下一位 AI 或工程师继续执行。当前模型已经下载到 COS，用户确认共绩 Worker 预热已经完成。后续工作从“实现任务链路和网页功能”开始，不要重复下载或预热模型。

## 一、当前已经完成的事情

### 模型仓库

COS 桶：

```text
h3-platform-1474752412
```

音乐模型已经放在现有 H3 模型根目录下面：

```text
models/h3-standard/music/2026-09-29-yue2-sheetsage2/
├── yue2/YuE2-3B/
├── yue2-vae/YuE2-Vae/
├── sheetsage2/SheetSage2/
├── mert/MERT-v2-FullSong/
└── manifest.json
```

当前版本索引：

```text
models/h3-standard/music/current.json
```

主权重已经核对过：

- YuE2-3B：7,261,441,640 bytes
- YuE2-Vae：530,512,720 bytes
- SheetSage2：228,738,564 bytes
- MERT-v2-FullSong：2,529,812,848 bytes

### 本地配置资料

- `music-deployment/README.md`
- `music-deployment/models-manifest.json`
- `music-deployment/compose.music-worker.yaml`
- `music-deployment/worker-runtime.env.example`
- `music-deployment/sync_models_to_cos.py`

### 预热状态

用户已确认共绩 Worker 预热完成。下一位 AI 不要自动再次预热，不要重新下载模型，不要修改已经验证过的 COS 模型目录。

## 二、目标功能

网页需要新增一个音乐生成工具，功能要求如下：

1. 上传 MP4、WAV、MP3。
2. 选择摇滚、R&B、爵士、民谣摇滚、Synth-pop、暗黑电影感、纯器乐等风格。
3. 选择男声、女声、不指定、无人声。
4. 支持自定义提示词。
5. 没有歌词时自动识别歌词，并允许用户修改确认。
6. 使用 SheetSage2 提取旋律、和弦、节拍、调性和 ABC。
7. 调用 YuE2 的 `cot=melody` 模式生成翻唱或重制歌曲。
8. 输出 MP3 和 FLAC。
9. 使用单 GPU 队列，一次只执行一个音乐生成任务。
10. 每个任务保存源文件、歌词、`score.abc`、`request.json`、音频结果和日志。
11. 账户、积分、任务状态继续使用现有 API 和数据库，不新建一套账户系统。
12. 前端不显示模型路径、镜像名、COS key、共绩参数、预热日志等实现细节。

## 三、推荐执行顺序

### 第 1 步：先检查现有项目

下一位 AI 先阅读：

- `main.py`
- `scheduler.py`
- `accounts.py`
- `database.py`
- `portal/index.html`
- `portal/app.js`
- `portal/creative.js`
- `gongji.py`
- `transfer_storage.py`

重点确认现有任务创建、积分冻结、任务轮询、结果下载和文件保留逻辑。音乐任务应复用这些机制，不要复制一套相似实现。

### 第 2 步：建立音乐任务数据结构

增加音乐任务需要的字段或 payload 结构，至少包括：

- 输入音频
- 风格
- 人声类型
- 用户提示词
- 原始歌词
- 用户修改后的歌词
- `score.abc`
- SheetSage2 运行状态
- YuE2 运行状态
- MP3/FLAC 结果
- provider task id
- 失败原因和运行日志

积分必须遵循现有的冻结、成功扣除、失败释放流程。任务创建接口必须支持幂等键，避免浏览器重复提交造成重复扣费。

### 第 3 步：实现单 GPU 音乐 Worker

Worker 启动时使用已经预热好的模型缓存，不要在每个任务里重复下载模型。

建议 Worker 阶段：

```text
接收任务
  ↓
检查音频格式和时长
  ↓
ASR：没有歌词时识别歌词
  ↓
SheetSage2 + MERT：提取旋律并生成 score.abc
  ↓
等待用户确认或直接使用歌词
  ↓
YuE2 cot=melody
  ↓
导出 FLAC
  ↓
转码 MP3
  ↓
上传结果和任务日志
```

如果网页允许用户修改歌词，任务应拆成“识别/提取”和“最终生成”两个阶段，用户确认歌词后再进入 YuE2。

### 第 4 步：接入共绩单 GPU 队列

使用 `music-deployment/compose.music-worker.yaml` 作为运行配置基础。生产配置中应明确：

- 单 GPU
- 队列并发为 1
- 模型缓存使用持久化磁盘
- 模型版本读取 `models/h3-standard/music/current.json`
- 任务日志和中间文件写入独立任务目录
- 预热由 Worker 生命周期管理，不由普通 job 重复触发

实际镜像发布后，再将 `MUSIC_WORKER_IMAGE` 写入服务器环境变量。不要把 token、COS 密钥、ModelScope token 或 Harbor 密码写入镜像和 Git。

### 第 5 步：实现 API

建议接口形态：

```text
POST /api/account/music/prepare
POST /api/account/music/generate
GET  /api/account/music/tasks
GET  /api/account/music/tasks/{task_id}
GET  /api/account/music/tasks/{task_id}/lyrics
PATCH /api/account/music/tasks/{task_id}/lyrics
```

具体路径可以按项目现有命名习惯调整，但必须复用现有登录、账户余额、积分冻结、任务列表和下载授权。

### 第 6 步：实现前端

前端只呈现用户需要的内容：

- 上传音频
- 风格选择
- 人声选择
- 自定义提示词
- 识别歌词编辑框
- 生成按钮和积分提示
- 任务进度
- MP3/FLAC 下载

前端不要显示：

- COS 路径
- 模型名称和权重大小
- 共绩资源规格
- Docker 镜像名
- 预热过程
- 内部 API token 或请求参数

建议先做一个最小可用页面，再接入完整的歌词编辑和任务历史，避免一次性改动现有视频生成页面。

## 四、验收标准

### 功能验收

- MP3、WAV、MP4 都能上传。
- 有歌词和无歌词两种流程都能完成。
- 用户能修改识别出来的歌词。
- SheetSage2 能生成 `score.abc`。
- YuE2 能以 `cot=melody` 生成结果。
- 同一任务能下载 MP3 和 FLAC。
- 两个任务同时提交时，第二个任务保持排队，不会同时占用 GPU。
- 任务失败时积分释放，成功时只扣一次。
- 刷新网页后任务状态和结果仍能恢复。

### 运行验收

- Worker 不会重复下载模型。
- Worker 重启后能从 COS 当前版本恢复缓存。
- manifest 校验失败时任务不会开始生成。
- 任务源文件、歌词、ABC、request、结果和日志都能找到。
- 服务器磁盘保留足够空间，不因模型临时文件阻塞视频任务调度。
- 现有视频生成、图片编辑、电商复刻功能保持可用。

## 五、特别注意事项

1. 不要删除现有 COS 对象、账户数据库或视频历史数据。
2. 不要把凭证写进代码、Dockerfile、镜像、`request.json` 或日志。
3. 不要直接覆盖视频生成配置；音乐 Worker 使用独立的环境变量和队列。
4. 不要重新执行模型下载脚本，除非明确要发布新的模型版本。
5. 目前线上视频 API 实际使用过本地存储模式，不能只看仓库里的生产 compose 文件就判断线上状态。修改部署前先读取服务器实际环境和容器配置。
6. 音乐模型授权是 CC BY-NC 4.0。若用于商业服务，先确认授权范围。

## 六、交付给项目负责人的内容

完成后必须报告：

- 音乐 Worker 镜像名称和版本
- 实际部署机器和队列配置
- 模型缓存目录
- API 接口列表
- 网页访问入口
- 一次成功任务的任务 ID
- MP3/FLAC 下载验证结果
- 失败任务和积分回滚验证结果
- 日志和排查命令

不要只报告“容器启动成功”。必须至少跑通一次真实音频生成任务，并保存对应的任务文件和结果。
