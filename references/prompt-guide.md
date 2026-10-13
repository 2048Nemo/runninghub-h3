# Ref2VA 六段提示词指南（通用版）

与 MiniMax 官方 `skills/h3-prompt-writing`（github.com/MiniMax-AI/MiniMax-H3，
`references/ref-en.txt` + `base-en.txt`）同源；本篇是实战浓缩，字段级完整规范直接读官方
ref-en.txt（**注意：本机并未随 skill 附带 `h3-prompt-writing/references/` 拷贝，需自行从
GitHub 或 `npx skills add https://github.com/MiniMax-AI/MiniMax-H3 --skill h3-prompt-writing` 获取**）。
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

> 本节管"声音安错人/音色乱配"；**漏说、咿呀崩坏、漏词**看
> `anti-gibberish.md`（MiniMax H3 解决乱说话问题，中文实战格式）。

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

## 音频：`<Audio N>` / `audio reuse` / `audio reference`（2026-10-04 逐字核官方文档后重写）

> 本节逐字核自官方 `h3-prompt-writing/references/ref-en.txt`（§2.4/2.5/3/4.2/5.4/6）与仓库
> README 规格表。**⚠️ 本 skill 早前版本写入过的三条 ——「`sings along to <Audio 1>` 跟唱模板」
> 「音色移植（A 嗓音 × B 演唱）官方模板」「32×32 纯音频产出」—— 在官方文档中查无实据，
> 已删除，不要再用。** 官方**没有**「跟唱」专章，也没有 "lip sync" 专章。

**硬规格**（官方 README）
- 输出音轨 **32 kHz 立体声**（原生生成，非后期配音）；输出时长 4–15s；24 FPS。
- Ref2VA 参考输入上限：图 ≤9；视频 ≤3 段（每段 2–15s，总计 ≤15s）；**音频 ≤3 段（每段 2–15s，
  总计 ≤15s）**；混合文件总数 ≤12。
- 稳定支持 11 种语言（含中/日/英）。

**`<Audio N>` 的五种用途**（官方逐字）
> Copying all or part of an audio signal · Referencing a background-music style · Referencing a
> speaker's voice timbre and delivery · Using dialogue, lyrics, or sound effects from the original
> audio · Referencing beat, rhythm, or audio continuity

**绑定说话人**：`<Audio 1> is the voice-timbre reference for <Subject 1> (S1).`
（`(Sx)` 来自成片全局发声顺序，音频定义里**不得另行编号**。）一个音频承担多角色时用**一句话**写清，
不要另起小节。`<Video N>` 与 `<Audio N>` **独立编号、不构成配对**；**参考视频不因为"文件里有
声音"就自动产生 `<Audio N>`**。

**音频 retention 标记与画面不是一套**（混用是常见错误）

| 标记 | 含义（官方逐字） |
| --- | --- |
| `fully_copy` | The complete source audio serves as the target video's complete final audio track |
| `partially_copy` | Only part of the timeline or selected audio layers are copied, or other sounds are added, removed, or replaced after copying |
| `reference` | The signal is not copied directly; only timbre, rhythm, music style, dialogue content, or sound texture is referenced |
| `weak_reference` | Only broad similarity in category or atmosphere is retained |

**给音频写 `fully_preserved` 是错的** —— 那是画面/图片/视频用的标记。

**两种任务类型前缀**：`[audio reuse]`（the same audio signal is reused in full or in part）／
`[audio reference]`（不直接拷贝，只借 music style / timbre / dialogue or lyric content /
sound-effect texture / beat / continuity）。可 ` + ` 组合、不重复同类型。取用规则：
**编辑源视频且原声仍可闻 → 同时写 `audio reuse`**；**续写但不直接拷音频、只延续原轨可闻特征
→ 写 `audio reference`**。

**官方完整实例：音色克隆 + 说全新台词 + 口型同步**（README case-Ref2VA，5s）
这是"让画面里的人用另一条音频的音色说新台词"的官方写法，结构可直接照抄：

```text
<Audio 1> is the synchronized audio track of <Video 1>, providing the background music.
<Audio 2> is the voice timbre reference for <Subject 1>'s voice, containing a spoken male voiceover.

summary:
[video editing + audio reference + audio reuse] ... The edit animates <Subject 1>'s face to speak
the user-provided dialogue. <Audio 1> is partially reused as the continuous background music, while
the target references the calm male voice timbre of <Audio 2> for <Subject 1>'s spoken lines.

retention_analysis:
<Subject 1> (appears in [Shot 1]): fully_preserved - ... with his mouth newly animated to speak.
<Audio 1>: partially_copy - the atmospheric background music from <Audio 1> is reused, mixed
beneath the newly added spoken dialogue.
<Audio 2>: reference - the target audio references the male voice timbre from <Audio 2> to generate
<Subject 1>'s spoken dialogue.

detailed_description:
[Shot 1] ... <Subject 1> physically speaks, his mouth movements naturally syncing to the new
dialogue, with his voice timbre referencing the calm male delivery from <Audio 2>. ... <Subject 1>
(S1) speaks softly, <d>[English] Follow the wind, live free.</d> Exactly as his voice stops, his
lips meet in a relaxed, peaceful smile, and his jaw ceases speaking motion.
```

**两条互斥铁律**（ref-en §5.4）
- **只借音色/节奏/情绪/演绎时**：`When only timbre, rhythm, emotion, or delivery is referenced,
  do not carry the original dialogue from the reference audio into the target video.`
- **直接复用或用户明确要求"重现"歌词/台词时**：`<d>` 内**保留原词与原语言**；听不清写
  `[unclear]`、不许猜或意译；标点收敛到 `, . ? !`，去掉波浪号堆叠/emoji/装饰标点。

**`(Sx)` 分配规则**（ref-en §5.4）
- 按成片实际发声顺序**一次性分配**、跨镜复用；有具身发声者（人/角色/旁白）才分配 `(Sx)`。
- **只存在于被直接复用的 BGM / 完整音轨里的口头线索**（无具身发声者）→ 用 `<Audio N>` 当可闻
  来源，**不要新造 `(Sx)`**。
- **`retention_analysis` 里禁止出现 `(Sx)`**；合唱/齐说用 `(S1,S2)`。

**声音归到哪一段**（ref-en §6 + base-en §4.6/4.7）
- **dialogue、singing、与某镜同步的音效 → 一律留在 `detailed_description`**。
- 环境声/物理音/非语言人声 → `overall_soundscape`（1–4 句）。
- 角色听不到的观众配乐 → `non_diegetic_music`（1–3 句，写配器/速度/节奏/动态，**不用抽象情绪词**）。
- **歌词与台词的完整文本只准出现在 `<d>` 里**，上面两段禁止复述。
- 角色**能听到**的唱歌/乐器/收音机/电视/手机音乐 = diegetic → 写在正文，不进 `non_diegetic_music`。
- N/A 规则两段不同：`overall_soundscape` **只在明确要求全片静音时**才写 N/A；
  `non_diegetic_music` 没有配乐就写 N/A。
- 同一参考音频同时提供两类内容时，两段各写各的拷贝/参考关系。

**延伸阅读**：官方仓库另有 `skills/music-video-subtitle-generator`（MV 工作流规范，非字段规范），
其中真正有用的是**音频连续性做法**：锁定唯一 Master Audio、分段片段一律对齐 Master 时间戳、
**切点必须落在歌词句间停顿或换气、或强鼓点上，严禁在元音或唱词中途硬切**（除非后镜是极近特写
且口型接续）、人物嘴型/下颌/呼吸/点头要跟随歌词 phrasing 与 rhythm、有人声时贴字必须逐字匹配
正在演唱的歌词、剪辑点裁在 1/4 或 1/8 拍网格上。

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
