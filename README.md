# runninghub-h3

给 Agent 的视频生成技能：在 RunningHub 云端跑 **MiniMax H3**，从提交、轮询到成片落盘全自动。

**它只提供视频生成能力。** 提示词内容、角色设定、分镜设计属于你的项目，不归本 skill 管。

## 它做什么

- **双通道提交**：RunningHub AI App API（`/openapi/v2/run/ai-app/<id>`）与
  Workflow API（`/task/openapi/create`），`--target aiapp|workflow` 或配置选择
- **素材自动上传**：`--file` 给本地路径即可（H.264 校验、sha256 去重命名、媒体字段名按配置自动纠正）
- **任务全生命周期**：`extract → dry-run → submit → watch → download`，外加服务端真取消（`cancel`）
- **NDJSON 事件流**：每步一个 JSON 事件，Agent/CI 以子进程方式编程消费；`submit --no-wait` + `watch` 可拆两个进程
- **项目参数档案**：画面比例/分辨率一次核对，整项目自动沿用，不一致时告警
- **内置 H3 提示词规范**：MiniMax H3 Ref2VA（参考生视频）六段式格式与避坑清单，
  见 `references/prompt-guide.md`
- **零依赖**：单文件 CLI（纯 Python 标准库）

## 前提

1. **RunningHub 账号 + API Key**：到国际站 [www.runninghub.ai](https://www.runninghub.ai)
   注册并在个人中心创建 API Key（默认扣 RH币 的 NORMAL 类型）；站点选择建议见 `SKILL.md` §1。
   然后找到你要调用的 **AI 应用 ID 或工作流 ID**（AI 应用页 / 工作流编辑器「导出」处可见）。
2. **写 Ref2VA 提示词需要 Agent 具备视觉分析能力**：Ref2VA 是「参考视频/图片 → 新视频」，
   分析参考素材、写保真与分镜描述都依赖 Agent 能看图看视频。没有视觉能力的 Agent 也可以用——
   提示词由人写好传入即可，生成流程不受影响。

## 安装

```bash
git clone https://github.com/2048Nemo/runninghub-h3.git ~/.zcode/skills/runninghub-h3

mkdir -p ~/.config/runninghub
cat > ~/.config/runninghub/config.json <<'EOF'
{"apiKey": "<你的 Key>", "host": "https://www.runninghub.ai", "target": "aiapp"}
EOF
chmod 600 ~/.config/runninghub/config.json
```

## 快速开始

```bash
cd ~/.zcode/skills/runninghub-h3/scripts

# ① 从 AI 应用提取参数白名单，固化为本地配置（一次性）
mkdir -p ~/.runninghub/workflows
python3 h3_run.py extract --webapp <你的webappId> -o ~/.runninghub/workflows/my-app.json

# ② 免费预览将发出的完整请求（素材会上传取 fileName，但不提交、不计费）
python3 h3_run.py submit --config ~/.runninghub/workflows/my-app.json --dry-run \
  --file "27:video=ref.mp4" --node "263:text=$(cat prompt.txt)"

# ③ 提交并看护：上传 → 提交 → 轮询 → 下载成片 → 系统通知
python3 h3_run.py submit --config ~/.runninghub/workflows/my-app.json \
  --file "27:video=ref.mp4" --file "51:image=design.png" --file "48:audio=voice.mp3" \
  --node "263:text=$(cat prompt.txt)" --node "259:value=10" \
  --project my-film --shot scene01_v1 --notify

# ④ 只提交不等结果（CI 两段式），之后任意进程接管轮询
python3 h3_run.py submit ... --no-wait
python3 h3_run.py watch --task <taskId> --project my-film --shot scene01_v1 --notify
```

成片默认落盘 `~/Downloads/runninghub/<项目>/<镜头>.mp4`，撞名自动加序号，绝不覆盖。

## 文档地图

| 文件 | 给谁看 | 内容 |
| --- | --- | --- |
| `SKILL.md` | **Agent** | 操作手册：计费规则、新手向导、五步接工作流、参数覆盖、排错手册、Agent/CI 集成、复用流、v2 接口要点 |
| `references/prompt-guide.md` | **Agent** | H3 Ref2VA 六段式提示词规范：结构、硬规则、实测避坑、一分钟模板 |
| `scripts/h3_run.py` | Agent / 人 | 单文件 CLI（~830 行），NDJSON 事件流契约写在文件头 |
| `examples/` | 开发者 | 脱敏的 extract 配置结构与六段式提示词示例 |

## 计费与安全

- 费率约 0.2 RH币/秒 GPU 时间；**失败与提交被拦的任务零扣费**（实测验证）。
  参考：10 秒竖屏 243 帧实测 254 RH币 / 1270 GPU 秒
- 密钥只存 `~/.config/runninghub/config.json`（600），不进仓库、日志与事件流
- 服务端请求仅允许 https + host 白名单，解析 IP 拒绝内网/回环/保留地址

## 已知限制

- 出片质量抽检需要人工确认，或外接多模态工具
- 上传的 fileName 有效期 1 天，随提随用；视频素材只收 H.264（AV1 需预先转码）
- `/openapi/v2/query` 轮询对经典 workflow 任务的兼容未实测（实战都在 AI App 通道）；
  workflow 通道若查不到状态，优先尝试老版 `/task/openapi/status`

## License

[MIT](LICENSE)
