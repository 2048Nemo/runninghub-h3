# runninghub-h3

> 给 Agent 的云端视频生成技能 —— 通过 RunningHub 远程驱动 **MiniMax H3** 出片

**核心定位：专为没有多模态、没有 GPU、跑不了视频模型的 Agent 软件设计的云端生成 skill。**

编码类 Agent（Claude Code、ZCode、Cursor CLI……）会读代码、会写代码、会调命令行，
但它没有「眼睛」也没有「摄像机」——看不了片，更生成不了片。本 skill 把 RunningHub
云端算力接成 Agent 的**视频生成器官**：Agent 只需要会启动子进程、会读 JSON，
就能完成从写提示词到成片落盘的全流程。

**主要面向 MiniMax H3（Ref2VA 参考生视频）**，仓库已内置 H3 六段式提示词格式规范
（`references/prompt-guide.md`）——这是让 H3 稳定出片的关键知识，Agent 不需要额外
的多模态理解能力，照格式写提示词即可。

---

## 它补上的能力缺口

| 没有 GPU / 多模态的 Agent | 装上本 skill 之后 |
| --- | --- |
| 无法生成视频 | 云端提交 H3 任务，成片**自动下载到本地约定目录** |
| 不了解 H3 的提示词写法 | 内置 Ref2VA 六段式格式规范 + 实测避坑清单 |
| 不知道工作流有哪些参数可调 | `extract` 一键提取参数白名单与默认值，固化为本地配置 |
| 不敢随便花钱 | `--dry-run` 免费预览完整请求；事件流实时上报成本；失败/被拦任务零扣费判定 |
| 长任务只能干等 | NDJSON 事件流 + 后台轮询 + 系统通知；`submit`/`watch` 可拆成两个进程给 CI 用 |
| 每次都要问用户「用什么比例」 | 项目参数档案：画面比例/尺寸**一次核对，整项目自动沿用** |

## 功能特性

- **双通道 API**：同时支持 RunningHub 经典 Workflow API（`/task/openapi/create`）
  与 AI App API（`/openapi/v2/run/ai-app/<id>`，Bearer），由 `--target` 或配置选择
- **素材自动上传**：`--file` 直接给本地路径，sha256 命名自动去重；媒体字段名按
  extract 配置自动纠正；AV1 → H.264 转码检测；QuickTime 容器头自动修复
- **任务全生命周期**：`extract → dry-run → submit → watch → download`，外加
  `cancel`（服务端真取消，只杀本地进程不停 GPU 是新手最常见的烧钱误区）
- **成本透明**：每个任务结束上报 RHB 消耗、GPU 秒数；NORMAL 密钥失败任务免费（实测验证）
- **项目参数档案**：`params --set` 固化比例/分辨率；提交时自动回填并做一致性告警
  （`param_confirm` / `param_pinned` / `param_mismatch` 事件）
- **零依赖**：单文件 CLI，纯 Python 标准库，`python3 h3_run.py` 即用

## 安装

```bash
# 1. 把本仓库放进（或克隆为）你的 Agent 技能目录
git clone <repo-url> ~/.zcode/skills/runninghub-h3

# 2. 配置密钥（600 权限，绝不进仓库）
mkdir -p ~/.config/runninghub
cat > ~/.config/runninghub/config.json <<'EOF'
{"host": "https://www.runninghub.ai", "key": "<你的 API Key>", "target": "aiapp"}
EOF
chmod 600 ~/.config/runninghub/config.json
```

API Key 在 RunningHub 个人中心创建（选默认扣 RH币 的类型）。站点的选择建议见
`SKILL.md` §1（生图/生成类应用实测结论：直接用国际站 .ai 账号）。

## 快速开始

```bash
cd ~/.zcode/skills/runninghub-h3/scripts

# ① 固化配置：从 AI App ID 一键提取参数白名单（一次性）
mkdir -p ~/.runninghub/workflows
python3 h3_run.py extract --webapp <你的webappId> -o ~/.runninghub/workflows/my-app.json

# ② 免费预览：打印将要发出的完整请求，不提交
python3 h3_run.py submit --config ~/.runninghub/workflows/my-app.json --dry-run \
  --file "27:video=ref.mp4" --node "263:text=$(cat prompt.txt)"

# ③ 提交并看护：自动上传素材 → 提交 → 轮询 → 下载成片 → 系统通知
python3 h3_run.py submit --config ~/.runninghub/workflows/my-app.json \
  --file "27:video=ref.mp4" --file "51:image=design.png" --file "48:audio=voice.mp3" \
  --node "263:text=$(cat prompt.txt)" --node "259:value=10" \
  --project my-film --shot scene01_v1 --notify

# ④ 只提交不等待（CI 两段式的第一段；--no-wait 提交即返回）
python3 h3_run.py submit ... --no-wait
python3 h3_run.py watch --task <taskId> --project my-film --shot scene01_v1 --notify
```

成片默认落盘 `~/Downloads/runninghub/<项目>/<镜头>.mp4`，撞名自动加序号绝不覆盖。

## 文档地图

| 文件 | 给谁看 | 内容 |
| --- | --- | --- |
| `SKILL.md` | **Agent** | 操作手册：计费规则、新手向导、五步接工作流、参数覆盖、排错手册、Agent/CI 集成、复用流、v2 接口要点 |
| `references/prompt-guide.md` | **Agent** | H3 Ref2VA 六段式提示词规范：结构、硬规则、实测避坑、一分钟模板 |
| `scripts/h3_run.py` | Agent / 人 | 单文件 CLI（~830 行，纯标准库），NDJSON 事件流契约写在文件头 |
| `examples/` | 开发者 | 脱敏的 extract 配置结构与六段式提示词示例 |

## 设计与安全模型

- **密钥隔离**：密钥只存在 `~/.config/runninghub/config.json`（600），不出现在仓库、
  日志、事件流和命令行示例中
- **SSRF 防护**：服务端请求仅允许 http/https，host 校验拒绝 localhost / 回环 /
  内网 / 保留地址段
- **NDJSON 事件流**：所有输出为一行一个 JSON 事件（`uploading` / `uploaded` /
  `param_pinned` / `progress` / `done` / `failed` / `error`…），供 Agent 或 CI
  以子进程方式编程消费；**退出码属于 python 进程本身**，接管道（如 `| tee`）时
  注意 tee 会掩盖退出码
- **失败零扣费**：提交校验拦截（枚举错、字段名错）与运行失败（如 VRAM OOM）均不计费，
  事件流中可据 `costCoins` / `gpuSeconds` 判定

## 实测战绩

本 skill 在开发过程中经历了完整的实战验证（真实任务、真实计费）：

- 10s 竖屏 Ref2VA 镜头（243 帧，0.5MP × 1.0× 二采）：**254 RHB / 1270 GPU 秒** 成功出片，
  角色一致性抽检通过
- VRAM OOM（errorCode 805）定位与缓解：参考视频降采样 + 降低二采倍率
- 服务端 cancel 接口的单数 `taskId` 参数坑、以及「杀本地进程≠停云端任务」的省钱要点
- 实战抓出并修复的自身 bug：媒体字段名不匹配、长任务看护窗口过短、watch 下载参数未透传

详见 `SKILL.md` §5 排错手册与附录 B。

## 已知限制

- Agent 本身依然没有「眼睛」：出片质量抽检需要人类确认，或外接多模态工具
- 上传的 fileName 有效期 1 天，随提随用
- 视频素材只收 H.264；AV1（B 站下载的 av01 编码）会运行时报错，需预先转码
- `/openapi/v2/query` 轮询对经典 workflow 任务的兼容未实测（当前全部实战在 AI App 通道）；
  若 workflow 通道查不到状态，优先尝试老版 `/task/openapi/status`

## License

[MIT](LICENSE)
