# Ref2VA 六段提示词指南（通用版）

与 MiniMax 官方 `skills/h3-prompt-writing`（github.com/MiniMax-AI/MiniMax-H3，
`references/ref-en.txt` + `base-en.txt`）同源；本篇是实战浓缩，字段级完整规范直接读官方
ref-en.txt（本机 `h3-prompt-writing/references/` 有同版拷贝，已核对与 GitHub 一致）。
**角色、分镜、画风属于你自己的项目**——本篇只管格式与技巧。

## 六段结构（顺序固定）

| 段 | 内容 |
| --- | --- |
| `subject_definitions` | 每个要复用/修改的参考内容一行：`<Subject N>`（角色/场景/道具/风格）、`<Video N>`（整段参考视频）、`<Audio N>`（音频）；来源素材写进定义里 |
| `summary` | 首行任务类型前缀：`[reference generation]`（纯参考生成）/ `video editing` / `video continuation` / `audio reuse` / `audio reference`，可 `+` 组合；随后一句话概括，引用标签 |
| `retention_analysis` | 每个标签一行：`(appears in [Shot N]...): fully_preserved / partially_preserved / attribute_transfer / weak_reference`；音频用 `fully_copy / partially_copy / reference` |
| `detailed_description` | 主体，350~500 词英文；风格开场 1~2 句；`[Shot 1]` 无时间戳，其后 `[Shot N] At MM:SS.mmm`；每镜头写清构图、主体外观与位置、环境光、动作变化、镜头运动、声音、参考内容生效点 |
| `overall_soundscape` | 环境声与物理音效总结 |
| `non_diegetic_music` | 观众可闻的配乐；无则写 `N/A` |

## 硬规则

- **语言**：六段全英文；唯独 `<d>` 内对话与画面可见文字保留原语言：`<d>[Chinese] 今天也要加油哦。</d>`
- **说话人**：主体说话写 `<Subject N> (S1)`；没有上传参考音频就不要造 `<Audio N>`；
  没有第二个说话人就不要发明 `(S2)`
- **时长匹配**：分镜切点必须落在实际时长内（留 0.5s 余量）。H3 帧数公式：
  `帧数 = max(5, round(秒×24))` 向上对齐 17 的倍数（4s→107帧≈4.46s，10s→243帧≈10.13s）
- 标签全文一致；不要在 summary 里新造标签；标点收在 `,` `.` `?` `!`，去掉波浪号堆叠

## 对白与说话人（防「乱说话」，官方 base-en/ref-en §5.4）

`<d>` 是台词的硬绑定通道：模型拿它直接驱动 TTS 与口型，结构写错 = 角色乱嘟囔。
逐条照官方规则：

1. **`<d>` 内只放两样**：语言标签 + 台词原文。说话人身份、动作、语气全写在 `<d>` 外：
   `The young woman with a quiet, breathy voice (S1) says: <d>[English] I get off at the next station.</d>`
2. **语言标签用半角方括号** `[Chinese]` / `[English]`（B站评论里 `【chinese】` 是手打不严谨）。
   标签告诉模型用什么音素读——中文台词缺标签就容易读出乱腔。
3. **说话人 ID 按目标视频实际发声顺序**编 `(S1)` `(S2)`，跨镜头保持；合唱用 `(S1,S2)`；
   不出声的角色不给 ID。首次出现给足音色信息（年龄/性别/音高/语速/口音）。
4. **音色参考必须绑定说话人**：`<Audio 1> is the voice-timbre reference for <Subject 1> (S1).`
   ——不绑定，模型可能把参考音色安到别人嘴里。
5. **只参考音色时，禁止把参考音频的原台词搬进目标视频**。台词密集时优先保证完整口语
   时间线，不硬凑 350~500 词。
6. **画外音**用精确短语 `says in an off-screen voiceover`，并紧跟
   `while his lips remain completely closed`（声明画面角色闭嘴，防口型乱动）。
7. **台词跨切**：切点两侧都标 `<scenetrans>` 并声明 `continues seamlessly across the cut`；
   被片尾截断的半句话标 `<cutoff>`。
8. **听不清就写 `[unclear]`**，禁止猜词意译；`<d>` 内台词逐字保留，不翻译不改写。
9. **完整台词只准出现在 detailed_description 的 `<d>` 里**，`overall_soundscape` /
   `non_diegetic_music` 禁止复述。BGM 里的人声归 `<Audio N>`，角色开口归 `(Sx)`，别混。

## 常见坑（实测教训）

1. **情绪词不要改写身体形状**：写 `cheeks puffed round and trembling`（气鼓鼓）会被字面执行成
   松鼠颊囊脸。情绪改走眼神/眉毛/泪花/瘪嘴等不改轮廓的描写；身体形状在 `<Subject>` 定义里
   正面锁定（如 `cheeks stay smoothly rounded, never bulging into pouches`）。
2. **修改参考素材自带特征**（牙齿、脸颊、服饰…）：在 `<Subject>` 定义里**正面描述新特征 +
   否定旧特征**（只写否定句不稳），并把 retention 降为 `partially_preserved`、注明改的是哪一项。
3. 只写否定（`no double fangs`）不如「正面新特征 + 否定旧特征」组合稳。
4. 改剧情时只动 `detailed_description` 与台词，`subject_definitions` / `retention_analysis`
   保持稳定——角色设定层与分镜层分离，便于批量迭代。
5. 提交前核对：时长切点 ≤ 视频末尾、标签无遗漏、每段顺序完整。

## 一分钟模板

```text
subject_definitions:
<Subject 1> is <角色要点> from <Video 1>, with <外观锁定>.
<Subject 2> is <场景/环境> from <Video 1>.
<Audio 1> is the voice-timbre reference for <Subject 1> (S1).   ← 有参考音频时必写

summary:
[reference generation] One short paragraph: what the target video shows, citing <Subject N> / <Video 1>.

retention_analysis:
<Subject 1> (appears in [Shot 1], [Shot 2]): fully_preserved - <逐项列出保留特征>.
<Subject 2> (appears in [Shot 1]): fully_preserved - ...
<Video 1> (camera feel and pacing): weak_reference - ...

detailed_description:
The target video is <风格开场>.
[Shot 1] <构图/主体/动作/声音/参考生效点> <d>[中文] 台词</d>
[Shot 2] At 00:03.500, <新动作> <d>[中文] 台词</d>

overall_soundscape:
<环境声一句话>

non_diegetic_music:
<配乐一句话 或 N/A>
```
