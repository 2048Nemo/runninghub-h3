# Skill: runninghub-h3

连接 RunningHub 上的 MiniMax H3 视频工作流并出片的**通用、自包含**教程与工具：
站点/密钥/计费规则、workflowId 直跑、节点参数覆盖、素材上传（含 AV1 转码）、排错手册、
Ref2VA 六段提示词格式，以及供 Agent/CI 以子进程方式感知任务进度的事件流 runner（h3_run.py）。

**边界（重要）**：本 skill 只管「怎么连、怎么跑、怎么排错」这类**通用机制**；
角色设定、分镜脚本、画风偏好属于使用者自己的项目，请放在你的项目目录，不要写进本 skill。
全部代码在本目录内（纯 Python 标准库），不依赖任何外部项目代码或配置，拷走整个目录即可用。

## 什么时候用这个 skill

- 要通过 API 连接并运行 RunningHub（国际站 `www.runninghub.ai`）上的任意 MiniMax H3 工作流
  （导演台 / 多参考生视频 Ref2VA 等），自动等待并下载成片。
- 要让 Agent/CI 提交任务后异步感知结果（NDJSON 事件流 + 系统通知 + 自动归档）。

与其他 skill 的关系：**互不调用**，以下仅为可选延伸阅读——`h3-prompt-writing`（提示词字段级
更完整规范）、`runninghub`（通用端点菜单客户端）。本 skill 的 `references/prompt-guide.md`
已含实战所需全部要点。

## 1. 站点、密钥与计费（2026-09 实测）

| 事实 | 内容 |
| --- | --- |
| 站点 | 国际站 `https://www.runninghub.ai`；国内站 `.cn` 的**密钥与工作流互不相通**，workflowId 必须是对应站点的 |
| 合规与选站 | 国内外在 API 调用机制上因**合规**而有区别：生图这类应用国内站受合规限制，**基本没有特别好用的生图模型**。踩坑结论：跑生图/生成类应用，一般直接**订阅国际站（.ai）账号**使用 |
| 密钥配置 | env `RUNNINGHUB_API_KEY` / `RUNNINGHUB_HOST`，或 `~/.config/runninghub/config.json`：`{"apiKey": "<Key>", "host": "https://www.runninghub.ai"}`（权限 `chmod 600`）。**密钥不进源码/日志** |
| 密钥类型 | `apiType: SHARED`（企业级-共享）→ **API 任务扣钱包现金（USD）**；`apiType: NORMAL`（消费级会员）→ **API 任务扣 RH币**。网页工作台运行永远扣 RH币 |
| 费率 | ≈0.2 RHB/秒 GPU 时间（两次实测一致）；10 秒视频约 55~100 RHB、GPU 4~9 分钟 |
| 失败任务 | **一分不扣**（两本账都不动）。所以「先试错」是免费的，参数校验错误在提交时就会被拦 |

## 2. 新手向导：拿密钥、拿工作流 ID、导出（一次看懂）

### 2.1 拿 API 密钥（默认扣 RH币 的那种）

1. 打开密钥页：`https://www.runninghub.ai/zh-cn/bill-task?tab=keys&type=consumer`
   （任务与账单 → 密钥；`type=consumer` 即**消费级会员**密钥，API 任务默认扣 RH币。
   千万别拿成「企业级-共享」密钥——那种按钱包现金/USD 扣，见 §1）
2. 创建并复制 API Key（32 位十六进制串）
3. 发给 Agent，或自己写入 `~/.config/runninghub/config.json`（密钥只放配置/环境变量，不进源码）：
   `{"apiKey": "<你的Key>", "host": "https://www.runninghub.ai"}`，并 `chmod 600` 该文件

### 2.2 拿工作流 ID 并导出工作流

1. 打开工作台：`https://www.runninghub.ai/zh-cn/workspace`，点开目标工作流
2. **复制浏览器地址栏链接**：`https://www.runninghub.ai/zh-cn/workflow/<纯数字>`——
   数字串就是 **workflowId**，发给 Agent（或直接填 `--workflow`）
3. 点编辑器上方 **「导出工作流」**（编辑器格式 JSON：全量节点 + 连线 + 开关状态）或
   **「导出工作流 API」**（API 格式：运行图视角，被旁路的节点已被剔除）。
   把 JSON 发给 Agent 当「参数地图」，用来查 nodeId / fieldName（方法见 §4）
4. ⚠️ **想用「被禁用」的节点（节点带禁用蒙层/斜纹）？必须先解禁再导出、再保存**：
   在编辑器里点选该节点，按 **Ctrl+B**（Mac：**Command+B**）解除旁路（bypass）
5. **必须知道**：不解禁就导出的话，导出的工作流里**根本没有那个节点**——API 的
   nodeInfoList（load list）引用不到它，Agent 传那个 nodeId 也不生效（运行图里不存在，
   传了等于没传）。API 永远无法解禁旁路节点，只能回填「已解禁且已保存」的槽位；
   编辑器里的任何改动（解禁、改参数、换素材）都要**点保存**才会被 API 看到

## 3. 连接 H3 工作流五步

```bash
# ① 前置：目标工作流的 workflowId（来自 §2.2，注意是目标站点账号里的）

# ② 素材若为 AV1（B站下载的 av01 编码），必须先转 H.264，否则服务端 OpenCV 解不了：
#    macOS（自带 avconvert）：
avconvert --preset PresetHighestQuality --source in.mp4 --output h264.mp4
#    Windows / Linux（装 ffmpeg）：
ffmpeg -i in.mp4 -c:v libx264 -crf 18 -pix_fmt yuv420p h264.mp4
#   校验：python3 -c "b=open('h264.mp4','rb').read(); print(b'avc1' in b, b'av01' in b)"

# ③ 提交（素材自动上传并回填；fileName 有效期 1 天）
#    --project/--shot 决定落盘位置：~/Downloads/runninghub/<项目>/<镜头>.mp4
#    （Agent 按当前项目/分镜传；撞名自动加序号不覆盖；--output 可完全自定义路径）
python3 <本skill目录>/scripts/h3_run.py submit \
  --workflow <你的workflowId> \
  --project <项目名> --shot <镜头名> \
  --file "27:video=h264.mp4" \
  --node "263:text=$(cat prompt.txt)" \
  --node "259:value=10" \
  --output /可选/自定义/路径.mp4 \
  --notify --open

# ④ 只提交不等待（之后用 watch 接管，适合 CI 分两个进程）
h3_run.py submit ... --no-wait          # 事件流里取 taskId
h3_run.py watch --task <taskId> --notify

# ⑤ 结果：任务 SUCCESS 时**自动下载到本地**并修复容器头；
#    --open 完成后在文件管理器中显示；--notify 弹系统通知
```

## 4. 节点参数覆盖：通用方法与注意事项

- **怎么知道有哪些可覆盖参数**：唯一途径是「导出工作流 API」的 JSON（顶层键=节点ID，
  每节点有 class_type / inputs / _meta.title）。nodeId / fieldName 必须与之一致，
  `fieldValue` 一律传字符串。
- **旁路节点不可覆盖**：编辑器里被禁用（mode=4）的节点不进运行图——API 传那个 nodeId
  等于没传。要开放：Ctrl+B / Command+B 解禁 → 接好素材 → **点保存**（未保存的改动 API 看不到）。
- **枚举字段写错**：提交即拦、零扣费，报错里会列出全部合法值，照抄重提即可。
- **H3 通用知识**：时长节点一般是「秒数」输入，帧数 = `max(5, round(秒×24))` 向上对齐
  17 的倍数（4s→107帧≈4.46s，10s→243帧≈10.13s）；两段采样（先低清一采、latent 上采样、
  高清二采）的工作流通常有**上采样倍率节点**，提交前确认其值 ≥ 1.0，否则运行必失败。
- **看护窗口**：`watch`/`submit` 默认 1200s 就放弃轮询——**只是本地看护退出，云端任务
  继续跑**（杀本地不停 GPU）。10s/243 帧双采样实测超 20 分钟仍未完，长镜头一律
  `--max-seconds 3600`；超时后用 `watch --task <id> --max-seconds 3600` 续盯即可，
  别重复提交（会双份计费）。
- **媒体槽**：图片/视频/音频槽先 `upload`（`/task/openapi/upload`，multipart，fileType=input）
  拿 fileName 再回填，fileName 有效期 1 天；视频只收 H.264（AV1 会在运行时报
  `could not be loaded with cv`）。
- **媒体字段名**：各槽的 fieldName 由作者工作流决定（实测 goldfish：视频 27=`video`、
  图 51=`image`、音频 48=`audio`），写错会在**提交时**被
  `NODE_INFO_MISMATCH(field_not_found_in_node_inputs)` 拒绝（零扣费）。submit 现在会按
  extract 配置自动纠正字段名（事件 `field_fixed`）；手工调 API 时以 extract 的
  `media.*.field` 为准。
- **占位节点**：inputs 为 `"None"` 的占位节点通常被运行时容忍，不必清理；
  但语义上空槽不提供参考内容。

## 5. 排错手册（实测案例）

1. **运行失败 code 805**：用 v2 接口拿 `failedReason`（含 node_id / node_name /
   exception_message / traceback），`h3_run.py watch` 的 failed 事件已带全这些字段。案例：
   - 上采样器 `This model only supports upscaling (effective scale >= 1.0.)`
     → 倍率节点被存成 <1.0 的值，用 nodeInfoList 覆盖为 ≥1.0，或编辑器改好保存。
   - `VHS_LoadVideo: ... could not be loaded with cv.` → 参考视频是 AV1，转 H.264 再传。
2. **提交即失败（免费）**：`value_not_in_list` = 枚举写错，报错详情里有全部合法值，照抄重提。
3. **轮询**：v2 query 用 `Authorization: Bearer <key>`（其余端点用 body 里的 apiKey 字段，
   两套并存），5 秒间隔、20 分钟上限；网络失败退避重试，超限报 POLL_TIMEOUT。
4. **结果容器**：出片常是 QuickTime 头（ftyp qt ），播放器可能不认——runner 会自动改写
   ftyp 盒品牌为 isom（保持原盒长，不越过盒边界；越界会踩坏后继盒导致「媒体损坏」）。

## 6. Agent / CI 集成（「进程一返回就通知」的实现）

`h3_run.py` 把整个过程写成 **stdout NDJSON 事件流**，就是为子进程/IPC 消费设计的：

```
h3_run.py submit --workflow ... --node ... --notify
  ↓ stdout 逐行
{"event":"uploaded",...} → {"event":"submitted","taskId":"..."} → {"event":"progress","status":"RUNNING",...}
  ↓ 结束（互斥）
{"event":"done","file":"<本地绝对路径>","costCoins":48,"costMoney":null,"gpuSeconds":237}
{"event":"failed","node":"140","exception":"..."}
{"event":"error","reason":"SUBMIT_FAILED",...}
```

- **Agent 集成**（ZCode 等）：以后台方式启动 runner → 完成通知带回日志路径 → 逐行读 stdout
  即得结构化进度；exit 0=成功、1=失败。
- **人的感知（跨平台）**：加 `--notify`，结束（成功/失败）弹系统通知——macOS 走 osascript、
  Windows 走 PowerShell 气泡（免装模块）、Linux 走 notify-send，都没有则终端响铃；
  加 `--open`，成功后自动在文件管理器中显示（Finder / 资源管理器 / xdg-open）。
  SUCCESS 即自动下载到本地（默认 `~/Downloads/runninghub/[项目/]<镜头>.mp4`）。
- **两个进程接力**：submit `--no-wait` 拿 taskId 退出；任意后续进程用 `watch --task` 接管轮询。
- **参数核对提问（画面比例/分辨率）**：提交（含 `--dry-run`）时，`aspect_ratio`/`megapixels`
  等比例尺寸参数若未在本项目核对过，会先发 `param_confirm` 事件，列出当前生效值与可直接复制的
  固化命令——**Agent 应把它转述给用户确认，不要默默替用户决定**；固化后同项目提交自动沿用
  （`param_pinned`），手动传了不一致的值会得到 `param_mismatch` 告警。详见 §8 ④。
- 轮询在状态变化时发事件，静止时按 `--heartbeat`（默认 60s）发心跳，便于监控端判断进程存活。

## 7. 提示词（Ref2VA 六段式）与内容边界

六段式结构、标签规则、时长换算、常见坑见 `references/prompt-guide.md`（自包含，含通用模板）；
字段级更完整规范可选参考 `h3-prompt-writing` skill 的 `references/ref-en.txt`。

**内容边界**：本 skill 与其提示词指南只提供**格式与通用技巧**。具体角色设定、分镜脚本、
画风修订属于使用者自己的项目（例如作者的放在自己的项目仓库里），不要提交到本 skill。

## 8. 标准复用流：搜索 AI 应用 → 导出 → 固化为本地配置 → CLI 复用

未来主要形态：直接使用 RunningHub 站内搜到的可用 **AI 应用**（作者已封装好参数白名单），
把它的 API 处理成本地配置文件，之后所有调用都走 CLI，无需再碰网页。

```bash
# ① 固化：从 App ID 一键提取参数白名单 → 存入本地配置库（一次性的）
python3 <skill>/scripts/h3_run.py extract --webapp <id> \
  --out ~/.runninghub/workflows/<名字>.json

# ② 复用：提交时 --config 带上配置，默认值自动兜底，CLI 参数优先覆盖
python3 <skill>/scripts/h3_run.py submit \
  --config ~/.runninghub/workflows/<名字>.json \
  --file "27:video=ref.mp4" \
  --node "263:text=$(cat prompt.txt)" --node 259:value=10 \
  --project <项目> --shot <镜头> --notify --open

# ③ 预览（免费）：不真正提交，只打印将发出的 nodeInfoList
... submit ... --dry-run

# ④ 参数核对：画面比例/尺寸一次确认，整项目自动沿用（档案 ~/.runninghub/projects/<项目>.json）
python3 <skill>/scripts/h3_run.py params --project <项目>          # 查看
python3 <skill>/scripts/h3_run.py params --project <项目> \
  --set "252:aspect_ratio=9:16 (Portrait Widescreen)" --set "252:megapixels=0.5"
# 或首次提交时直接加 --confirm-params：把本次生效的比例/尺寸存为项目标准
```

- **为什么需要**：App 的作者默认比例（如 16:9）常与你的项目实际（如竖屏短视频）不符，
  一次看错整批镜头全错；而同一项目内比例/分辨率几乎总是不变。sticky 字段按 fieldName 识别
  （`aspect_ratio`/`megapixels`/`resolution`/`ratio`/`width`/`height` 等），跨工作流通用。
- **回填优先级**：`--node/--file` 显式值 > 项目档案 > extract 默认值；`--dry-run` 输出里的
  `param_sources` 会标注每个参数值的来源（`cli` / `project` / `config`），便于核对。
- Agent 约定（见 §6）：看到 `param_confirm` 必须转述给用户确认；未经用户同意不要执行
  `--confirm-params` / `params --set`。

- **CLI 只负责两件事**：上传素材/参数（`--file`/`--node`，配置默认值自动补齐），
  以及掌控任务返回（NDJSON 事件流轮询 → 自动下载到 `~/Downloads/runninghub/` → 退出码）。
- **风险提示**：App 配置绑定**作者的发布快照**——作者重发布/下架会导致调用失败。
  extract 存的参数默认值就是快照，失败时先 `extract` 一次 diff 即可定位变更。
- （工作流直跑路径同理：导出 JSON → 人工/Agent 整理成同格式配置 → `--config` 复用。）
- 曾有的本地面板/画布已从本 skill 迁出冻结（个人项目），与本 skill 无关。

## 附录 B. v2 AI 应用接口要点（2026-09 官方文档实测）

- **提交**：`POST /openapi/v2/run/ai-app/<webappId>`，`Authorization: Bearer <Key>`，
  body `{"nodeInfoList": [...], "instanceType": "default|plus|ultra", "usePersonalQueue": bool,
  "retainSeconds": 10~180(企业Key), "webhookUrl": "https://…"}`；
  工作流直跑仍用 `POST /task/openapi/create`（apiKey 在 body）。两壳同引擎。
- **上传**：`POST /openapi/v2/media/upload/binary`，Bearer 头 + multipart 仅 `file` 字段，
  返回 `data.fileName`（**1 天有效**）。
- **文件传参三通道**：fileName / 公共 URL / Base64 data URI。
- **回调**：提交带 `webhookUrl`，任务结束平台 POST `TASK_END` 事件（内容同查询结果）——
  需公网可达地址；本地轮询阶段不启用。
- **结果 URL 仅 24 小时有效**，务必及时下载。
- **分辨率换算**（作者工作流实测）：megapixels 只是参考值，一采分辨率由它决定，
  二采/成片 = 一采 × 上采样倍率（如 0.5MP 16:9 → 一采 960×544 × 1.5 = 1440×816）。
  已实测复核：0.5MP + 16:9 + 倍率 1.5 的 aiapp 任务成片 1440×832@24fps，与换算一致；
  倍率 1.0 时一采=二采=megapixels 档位尺寸（如 0.2MP 9:16 → 352×608）。
- **取消任务**（2026-09 实测）：`POST <host>/task/openapi/cancel`，body
  `{"apiKey":"...","taskId":"<单个任务ID>"}`——参数名是**单数 `taskId`**，传 `taskIds`
  数组会得到 `code:301 taskId cannot be empty`。成功返回 `code:0`；取消后不扣费，
  v2 query 对已取消任务返回空 status。注意：只杀本地轮询进程不会停服务端 GPU 任务，
  已提交的任务要省钱必须调这个接口。

## 附录 A. 作者工作流实例（仅作参考示例，非本 skill 的依赖）

作者自己的多参考生视频工作流：workflowId `2102728104627478530`（9:16 竖屏、一采+二采双阶段）。
其实测节点覆盖速查——**换任何工作流时都要以它自己的导出 JSON 为准**：

| nodeId | fieldName | 作用 | 注意 |
| --- | --- | --- | --- |
| 263 | text | 整段 Ref2VA 提示词（六段式） | value 可含换行，shell 用 `$(cat f)` 传入 |
| 259 | value | 视频时长（秒） | 帧数 = `max(5,round(a*24))` 向上对齐 17 的倍数 |
| 283 | value | 一采→二采 latent 上采样倍率 | 服务器保存版曾是非法值 0.2，需覆盖 ≥1.0 |
| 252 | aspect_ratio | 画幅枚举：`1:1 (Square)` `2:3 (Portrait Photo)` `3:2 (Photo)` `3:4 (Portrait Standard)` `4:3 (Standard)` `9:16 (Portrait Widescreen)` `16:9 (Widescreen)` `21:9 (Ultrawide)` | 写错提交即拦（免费）并列出全表 |
| 27 | video | 参考视频槽（唯一开放的视频槽） | 只收 H.264 |
| 51 / 49 / 50 | image | 三个已生效参考图槽 | 先 upload 拿 fileName |
| 43 / 19 / 23、48 / 14 / 15 | image / audio | `"None"` 占位节点 | 运行时被容忍；空槽无参考内容 |
| 25 / 26、199 / 200 / 201 | — | **旁路节点**（mode=4），API 不可见 | Ctrl+B / Command+B 解禁并保存后才能用 |
