# 安装

## 方式一：交给你的 Agent（推荐）

把下面整段复制，粘贴给你的 Agent（Claude Code / ZCode / Cursor CLI 等均可），它会自己完成安装、配置和自检：

```text
请帮我安装 runninghub-h3 视频生成技能。仓库：https://github.com/2048Nemo/runninghub-h3

1. 克隆到你的技能目录（用你实际在用的那个，如 ~/.zcode/skills/、~/.claude/skills/、~/.agents/skills/）：
   git clone https://github.com/2048Nemo/runninghub-h3.git <你的技能目录>/runninghub-h3

2. 配置密钥（需要我先提供）：
   - 向我询问我的 RunningHub API Key（在 https://www.runninghub.ai 个人中心创建，
     选默认扣 RH币 的 NORMAL 类型）。不要编造或使用示例值。
   - 写入 ~/.config/runninghub/config.json：
     {"apiKey": "<我的Key>", "host": "https://www.runninghub.ai", "target": "aiapp"}
   - 文件权限设为 600（Windows 下设置仅当前用户可读）。
   - 密钥不要写进任何源码、日志或会话记录。

3. 自检（不需要密钥、不联网、不产生费用）：
   python3 -m py_compile <技能目录>/runninghub-h3/scripts/h3_run.py
   python3 <技能目录>/runninghub-h3/scripts/h3_run.py params --project _selfcheck
   （第二条应输出一行 JSON 事件 {"event": "params", ...}；params 是纯本地操作）

4. 学习用法：通读安装目录下的 SKILL.md（操作手册）和 references/prompt-guide.md
   （H3 Ref2VA 六段提示词规范）。之后我要求生成视频时，按 SKILL.md 流程执行：
   extract 固化配置 → dry-run 预览 → submit 提交看护。

注意事项：长镜头看护加 --max-seconds 3600；失败与提交被拦的任务零扣费；
视频素材只收 H.264（AV1 需先转码）；需要 Python 3.10+，无第三方依赖。
```

## 方式二：手动安装

```bash
git clone https://github.com/2048Nemo/runninghub-h3.git ~/.zcode/skills/runninghub-h3

mkdir -p ~/.config/runninghub
cat > ~/.config/runninghub/config.json <<'EOF'
{"apiKey": "<你的 Key>", "host": "https://www.runninghub.ai", "target": "aiapp"}
EOF
chmod 600 ~/.config/runninghub/config.json
```

前提条件与快速开始见 [README](README.md)；给 Agent 的完整操作手册是 [SKILL.md](SKILL.md)。
