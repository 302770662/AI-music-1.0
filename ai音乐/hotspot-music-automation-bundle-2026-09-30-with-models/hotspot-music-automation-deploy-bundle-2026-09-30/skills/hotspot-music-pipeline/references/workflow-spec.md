# Hotspot Music Pipeline Workflow Spec

## A. 数据合同

### `trend_snapshot`

```json
{
  "run_date": "2026-08-31",
  "platform": "weibo",
  "item_id": "platform-native-id",
  "title": "公开榜单标题",
  "url": "https://example.invalid/item",
  "rank": 1,
  "heat": 0,
  "captured_at": "2026-08-31T08:00:00+08:00",
  "source_mode": "official_api|public_page|user_provided",
  "license_status": "unknown|analysis_only|authorized"
}
```

### `content_analysis`

至少包括：`item_id`、`language`、`summary`、`keywords`、`entities`、`sentiment`、`emotion_distribution`、`narrative_beats`、`ocr_segments`、`asr_segments`、`visual_motifs`、`tempo_hint`、`risk_flags`、`model_versions`。

### `music_features`

至少包括：`asset_id`、`sha256`、`duration_sec`、`sample_rate`、`bpm`、`key`、`loudness`、`energy`、`spectral_centroid`、`segments`、`effnet_embedding_ref`、`effnet_labels`、`embedding_model_version`、`license_status`。原始音频不要塞进分析表。

## B. 阶段状态机

```text
DISCOVERED -> NORMALIZED -> ANALYZED -> RIGHTS_REVIEW
RIGHTS_REVIEW -> READY_TO_CREATE | ANALYSIS_ONLY | REJECTED
READY_TO_CREATE -> PROMPTED -> GENERATED -> QA_REVIEW
QA_REVIEW -> READY_TO_PUBLISH | HUMAN_FIX | REJECTED
READY_TO_PUBLISH -> PUBLISHED | PUBLISH_FAILED
```

任何状态都应记录 `changed_at`、`actor`、`reason`；重跑不能覆盖历史结果。

## C. 采集适配器

为每个平台实现独立适配器，统一返回 `trend_snapshot`。适配器只使用官方接口、公开榜单或用户提供数据；配置 `max_requests_per_minute`、缓存 TTL、分页上限和停止条件。第三方抓榜项目只能作为线索，必须复核其许可、依赖安全和数据来源。

音乐平台榜单与视频平台热门内容应分开采集，不能把“榜单热度”直接当作“可下载授权”。

## D. 音频分析与训练

1. 接收授权音频，校验 MIME、大小、时长并计算 SHA-256。
2. 用 ffmpeg/Essentia 做统一预处理，并保留预处理参数。
3. 用 Essentia 的 `TensorflowPredictEffnetDiscogs` 提取 embedding 和 Discogs 风格预测。
4. 每日新增数据先进入人工抽检集；通过阈值、聚类或规则的自动标签必须标记为 `weak_label`。
5. 只有在标签质量、数据许可和样本量达标后才微调下游分类器。不要默认修改 Effnet-Discogs 权重；优先训练轻量分类头或校准层。
6. 按日期、平台、语言、流派和授权状态做切分，防止同一歌曲的不同版本泄漏到训练集和测试集。

## E. 创作提示词模板

```markdown
Title: <原创标题>
Style: <genre>, <tempo>, <instrumentation>, <vocal_character>, <rhythm>, <mix_texture>, <emotional_arc>
Avoid: named artists, existing song titles, quoted lyrics, voice imitation, copyrighted samples
Context: <事实谨慎的热点摘要与意象>
Lyrics:
<原创中文歌词，包含 [Verse] [Pre-Chorus] [Chorus] 等结构标记>
```

提示词必须从统计属性和抽象叙事出发，例如“中速、颗粒感合成器、克制主歌、开阔副歌”，而不是“做成某歌手/某首歌的风格”。歌词先生成，再做重复度和引用扫描。

## F. Suno 与发布闸门

- API：只从环境变量读取密钥；不把密钥写入日志、Markdown、提交记录或任务输出。
- 浏览器：只操作用户授权的当前会话；允许填写字段和读取结果，不绕过 CAPTCHA、风控或访问控制。提交生成和下载前显示待确认摘要。
- MP4：记录生成任务 ID、下载 URL/本地校验和、使用条款和保存期限。
- 发布：每个平台独立 `publish_adapter`，仅接官方 API/创作者中心允许的流程。自动化失败则停止该平台并生成人工操作清单。

## G. 验收清单

- 数据来源和时间可追溯；跨平台去重有效。
- 未授权媒体没有自动下载、改编或发布动作。
- Effnet 标签包含模型版本，人工抽检样本可回溯。
- 歌词无长段引用、无具体艺人模仿指令、无未经核实的事实断言。
- 生成、下载、上传都具备幂等键和失败回执。
- 任一平台政策或授权不明确时，系统默认停在 `ANALYSIS_ONLY` 或 `HUMAN_FIX`。

## H. Effnet-Discogs 的职责与边界

`TensorflowPredictEffnetDiscogs` 通常用于从音频片段提取固定维度 embedding，并输出训练标签集合上的概率。它适合回答“这段声音在音色/编曲/流派空间中接近什么”，不适合回答“歌词表达了什么”。

推荐保存三层结果：

1. `audio_embedding`：用于近邻搜索、聚类、相似曲风检索和下游模型输入。
2. `effnet_labels`：保留标签、概率、模型版本和窗口聚合方式，不把低置信度预测当作事实。
3. `interpretable_features`：BPM、调性、响度、能量、谱质心、起伏、段落边界和乐器检测，供提示词生成器解释。

Effnet 标签与人工/规则标签的融合应记录来源，例如 `effnet:0.72`、`human:confirmed`、`rule:slow_bpm`。微调时优先训练分类头、校准层或多模态融合模型；只有在数据许可、样本规模和验证集都明确时才考虑改动基础模型。

## I. 歌词自动打标

歌词标注器不使用 Effnet，而使用歌词文本本身和段落标记。建议字段：

```json
{
  "theme": ["离别", "旅行", "海浪", "四季"],
  "narrative_perspective": "first_person_plural",
  "emotion_curve": ["warm", "nostalgic", "melancholic", "hopeful"],
  "imagery": ["星光", "雪地", "潮汐", "露营"],
  "section_map": [{"section": "Chorus", "function": "emotional_peak"}],
  "line_length": "short_to_medium",
  "rhyme_density": "low",
  "repetition_ratio": 0.18,
  "risk_flags": []
}
```

处理顺序：解析 `[Verse]`、`[Chorus]`、`[Bridge]`、`[Outro]` 和乐器指令；分句并统计字数、韵脚、重复；抽取主题/意象/情绪；生成情绪曲线和段落功能；最后执行引用相似度、敏感内容和事实核验。LLM 结果标为 `weak_label`，重要字段由人工抽检校准。

## J. 音乐 + 歌词融合为 Suno 提示词

提示词生成器将音频统计、Effnet 标签、歌词标签和热点摘要映射为属性词，不直接复制参考歌曲。可使用如下规则：

- 抒情爵士：`melancholic, jazz, warm, sentimental, improvisation, medium, piano and violin`
- 抒情蓝调：`blues, slow, warm, base, sorrowful`
- 民谣舒缓：`folk and Britpop, slow, desolate, BPM 40`
- 颤音琴日系民谣：`melancholic, modern Japanese folk, vibraphone, sentimental, medium, spring, light-hearted`
- 动漫感：`melancholic, modern Japanese folk, Japanese animation atmosphere, sentimental, improvisation, medium, spring, tension`
- 悲伤冬季大乐队：`folk big band, very slow, warm, weathered, raspy, sorrowful, winter, snowy, autumn`

上述词典只是可解释的起点。`slow/very slow` 应由 BPM 和段落密度共同决定；`medium` 用于中速抒情爵士，避免把慢速标签机械叠加。将 Effnet 的高置信度标签与可解释特征合并后，再按歌词情绪曲线调节 `emotional_arc`、配器和动态。

## K. Solo 与器乐片段规划

段落标记应服务于编曲，不应无意义重复。推荐模板：

```text
[Powerful Intro]
[Melodic Instrumental]
[Verse 1]
[Verse 1][Violin Solo]
[Chorus]
[Melodic Instrumental]
[Violin Solo]
[Verse 2]
[Bridge][Instrumental Build]
[Chorus 2]
[Sax Solo]
[Outro][Sax Solo]
```

生成器根据情绪和配器选择 solo：小提琴适合叙事、怀旧、抒情和副歌回应；萨克斯适合爵士/蓝调、间奏和尾奏；`[Melodic Instrumental]` 用于歌词段落之间的呼吸。每个 solo 需写入 `instrument`、`section`、`duration_hint`、`role`（回应/过渡/高潮/尾奏），而不是只重复标签。对用户给出的示例歌词，保留其结构意图即可；生成新歌词时必须重新创作文本。

最终输出至少包含：`style_prompt`、`lyrics`、`section_map`、`instrumental_breaks`、`avoid_terms`、`source_feature_refs` 和 `generation_version`。
