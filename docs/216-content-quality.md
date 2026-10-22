# DEPTAI-216 · 内容质量：多媒体、标题、摘要

## 多媒体

- 采集：`collect.parse_feed` → `entry_media()` 读取 RSS `<enclosure>`、Atom `rel=enclosure`、Media RSS
  `media:content` / `media:thumbnail`（含 `media:group`）、正文 HTML 里的 `<img>` / `<video>`、YouTube / v.redd.it /
  i.redd.it / 直链音视频链接；条目链接本身是媒体时也算。只收 http(s)，头像 / 徽章 / 图标 / 小于 120px 的图丢弃。
- 入池：`pool_items.extra.media`（列表）+ `pool_items.image`（首图 / 视频封面）。插件源（HN 等）链接是 YouTube / 媒体文件时同样记录。
- og:image 探测：feed 没带图、源没开全文抽取的新条目，每源每轮最多 10 条，抓一次文章页读 `og:image` / `twitter:image`
  （正则，不跑全文抽取），结果记在 `extra.og_probe`，同一条永不重抓。github.com / reddit / HN / HF / X / YouTube 不探测
  （通用卡片或已有媒体）。`NEWSD_MEDIA_PROBE=0` 或 `run_cycle(extract=False)` 关闭。
- 展示：卡片显示首图；音频 / 直链视频用 `<audio>` / `<video preload="none">`（点播放前不下载）；YouTube 与 Reddit 视频
  显示封面 + 外链，不嵌 iframe。图片带 `referrerpolicy="no-referrer"`、`loading="lazy"`，加载失败自动移除。

## 标题

`newsd.content.display_title`：GitHub release 的裸版本号标题（`b11801`、`v0.42.3`、`[**b11801**](...)`）显示为
`<仓库名> <tag>: <发布说明第一句>`，如 `llama.cpp b11801: model: Add support for SenseNova U1 model`；其他标题只去掉
markdown。池里的原始标题不改，展示和送模型时用处理后的标题。

## 摘要

- 有模型 key：入选日报的条目走 `newsd/llm/digest.py`（参考 Horizon 的单条分析 / 富化提示词），每批 6 条，模型拿到
  标题、来源、URL、热度和清洗后的正文节选（头 40% / 中 30% / 尾 30%），输出中文标题 + 1–3 句摘要（是什么 + 为什么值得关注），
  发布说明要求归纳而不是逐条翻译。提示词声明输入不可信、不得编造事实。结果按 标题 + URL + 正文 的哈希缓存在共享的
  `translation_cache`（语言标记 `zh:dg1`），全站每条只生成一次；无 key 用户也能读到缓存。摘要失败的条目回退到原来的翻译
  （翻译的输入改为清洗后的摘要，并按字数分批、按长度给输出 token，避免长批次被截断后整批丢失）。
- 无模型 key：显示清洗后的原文（`clean_summary`）：去 markdown / 反斜杠转义（`test\_kpool` → `test_kpool`）/ HTML、
  PR 编号、`by @user in #123`、Co-authored-by、llama.cpp 的 Website / Attestations / 构建产物清单、Reddit 的
  `submitted by /u/...`，合并"提交标题 + 正文第一行"的重复，按句子截断；和标题相同的摘要不显示。
