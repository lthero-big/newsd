# M2-3 按用户打分、过滤与分组配额（DEPTAI-220）

## 改了什么

每个用户从共享池（M2-2）里各自算一份“这一期”：规则打分 → 去重 →（有 key 时）模型层 → 配额裁剪 → 存库。规则永远跑；模型（M2-4）只是叠加在上面。

### 1. 规则打分 `newsd/scoring.py`

- 规则来自用户自己的关键词（M2-1 的 `keywords` 表）：`interest`（想看）、`boost`（加权，`weight` 生效）、`block`（屏蔽）。关键词如果同时是全局标签词（`tag_terms`），会继承它的别名、所属标签组和 broad 标记，比如关注 `GPT-5` 也能命中 “gpt 5”。
- 匹配范围：标题、URL、源摘要，以及**有全文时的抽取全文**（M2-2 `extractions`，最多前 2 万字）。匹配规则沿用 news-daily 的 `match_terms`（英文按词边界，中文按子串）。
- 过滤：命中屏蔽词（标题/正文/URL 任一处）直接剔除；用户只要有 interest/boost 词，一个都没命中的条目剔除；没有任何关注词的用户不过滤，全部按热度和时间排。
- 分数 0–100 = 源权重 ×（0.5 × 相关度 + 0.35 × 热度 + 0.15）×（0.4 + 0.6 × 时间衰减）。
  - 相关度：每个命中词 权重 × 位置系数（标题 1.0、正文 0.5、URL 0.3）× broad 词减半，求和后 /2 封顶 1。
  - 热度：HN/Reddit/GitHub/HF/X 的点赞评论取对数归一（尺度和 news-daily 一样），RSS 没有计数取 0.5。
  - 时间衰减：post/release 半衰期 24 小时，其他 96 小时。
- 模块：`model`（模型发布、官方博客、标题里有具体模型名且命中发布句式）、`opensource`（仓库/release、GitHub/HF/OSSInsight）、`community`（HN、Reddit、X）、`news`（其余 RSS）。
- 类别：命中词所属的标签组 + 命中词本身，供分组配额匹配。

### 2. 先去重

按分数从高到低，先按规范化 URL（去 www、跟踪参数、末尾斜杠等），再按标题相似度（`titles_similar`），留分最高的一条，其余记在它的 `also` 里（注明 url 还是 title 判重）。有 key 时之后还会再做一次 M2-4 的模型语义去重（同一事件不同说法）。

### 3. 配额裁剪

按分数贪心挑选，每条依次检查：
1. **话题上限**：标题里同一个具体词（非 broad）最多 2 条（管理员可用全局设置 `topic_cap` 改），防止热点刷屏；
2. **分组上限**：配额表里 `module + group_name + categories` 的组，条目类别和组的 categories 有交集就算进该组（按配额顺序取第一个匹配的组）；
3. **模块上限**：`group_name=""` 的配额；没设的模块用默认值 model 8、opensource 6、community 8、news 8；
4. **总上限**：`module="" group_name=""` 的配额，默认 30。

被裁掉的每一条都记原因（`blocked:<词>`、`no-keyword-match`、`duplicate-of:<id>:url|title|semantic`、`model-below-threshold:<类别>:<分>`、`topic-cap:<词>`、`group-cap:<模块>/<组>`、`module-cap:<模块>`、`total-cap`）。

### 4. 模型层（用户有 key 时，M2-4）

- 用户选的处理配置对规则留下的条目打分（结果缓存在 `item_scores`，成功的下次复用）。
- 有可用判定：不属于或低于该用户阈值的剔除；通过的最终分 = 0.4 × 规则分 + 0.6 × 模型分 × 10，并带上模型给的栏目。
- 模型没给出可用判定（非法 JSON 记 empty、接口出错）：按中性模型分 5 混合，规则决定去留，不会因为模型出错丢内容。
- 配额裁剪之后做 AI 重排，最后标题和摘要走全站共享翻译缓存。
- 401/403 时本期停止后续模型调用，规则结果照常出。没 key 时零模型调用（只读翻译缓存）。

### 5. 存库和接口

- 迁移 `0005_m2_issues`：`issues`（用户 + key 唯一，如 `2026-10-20`；AI 状态、模型、时间窗口、统计、配额快照、被裁条目及原因，最多 200 条）和 `issue_items`（顺序、模块、分组、最终分/规则分/模型分、类别、命中词、合并的重复项、译文）。同一个 key 重建会替换旧结果。
- 接口：`GET /api/me/issues`、`GET /api/me/issues/{key|latest}`（`?explain=1` 附带被裁条目和原因）、`POST /api/me/issues/build`（`key`、`since_hours`、`ai`）。都只能看自己的。
- 命令：`newsd issue build <用户> [--key] [--since-hours 26] [--no-ai]`、`newsd issue build --all`（一个用户失败不影响其他人，给调度器用）、`newsd issue show <用户> [--key latest]`。

## 测试

`.venv/bin/pytest`：141 个通过（M2-4 基线 131 个 + 新增 10 个，模型调用全部用模拟 HTTP）。

- `tests/scoring/test_scoring.py`
  - **同一个池子、两个用户、不同关键词 → 不同结果**：alice 关注 Qwen，bob 关注 Sora/OpenAI 并屏蔽 benchmark；各自的条目、剔除原因、命中词都不同。
  - 全文匹配：关键词只出现在抽取全文里也能命中（位置记为 body）；屏蔽词出现在全文里也会剔除。
  - 标签别名（GPT-5 ↔ “gpt 5”）和 boost 权重影响排序。
  - 去重先于配额：规范化 URL 重复和标题相似都合并到最高分那条，`also` 记录来源。
  - **配额生效**：总上限 6、news 模块上限 5、news/LLM 分组上限 3、话题上限 2 同时生效，计数和剔除原因都对；同一池子不设配额时选出更多。
  - 无关注词时全收；同一个 key 重建替换而不是追加。
  - 模型层叠加：高分条目被模型抬到最前（规则分更低也一样）、低于阈值的被剔、无判定的按中性分保留、译文入库；401 时退回纯规则。
- `tests/scoring/test_issues_api.py`：接口构建/读取、`latest`、`explain`、用户隔离、未登录 401、非法 key 422；`newsd issue build --all` 和 `show`。

## 已知限制 / 后续

- 一个条目只计入第一个匹配的分组（按配额顺序），不会同时占多个组的名额。
- 模块分类是规则推断的，没有让用户改；需要的话可以加按源覆盖。
- 话题上限只看标题里的具体词，同一事件换了说法靠去重（有 key 时还有模型语义去重）兜底。
- `newsd process run`（M2-4 的预览命令）仍用它自己的简单排序；正式结果以 `newsd issue build` 为准。
- 定时调度（每个用户什么时候出一期）和推送属于后续子任务；它们直接调用 `build_issue` / `build_all`，读 `issues` / `issue_items`。
