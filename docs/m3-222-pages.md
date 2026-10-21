# M3 页面：按用户渲染日报、设置页、管理后台、主题换色（DEPTAI-222）

## 改了什么

所有页面都是服务端按当前登录用户渲染（Jinja2 + 一份静态 CSS，没有前端构建）。页面路由里每条查询都带 `user.id`，URL 里没有任何能指定别的用户的参数。

### 用户页面 `newsd/pages.py`

| 路径 | 内容 |
|---|---|
| `/` | 当前用户最新一期日报（M2-3 的 `issues`），按模块（模型 / 开源 / 社区 / 资讯）分组；每条有来源、命中词、分数、栏目，可收藏 / 移除；“立即生成日报”按钮调用 `POST /api/me/issues/build`。没登录跳 `/login`，没做过 onboarding 且没有订阅的新用户跳 `/welcome`。 |
| `/issue/{key}` | 指定一期，带上一期 / 下一期；不是自己的期次一律 404。 |
| `/calendar?month=YYYY-MM` | 月历，有日报的日子可点（显示条数）；非日期 key 的期次列在下面。 |
| `/favorites`、`/trash` | 收藏、回收站（可取消收藏 / 恢复）。 |
| `/settings` | 主题色、订阅源、关键词、配额、推送偏好、模型与 API key（M2-4）、内容类别与阈值（M2-4）。 |
| `/welcome` | 注册后的可选引导：勾兴趣，一键订阅对应预设源组。 |
| `/admin` | 管理后台（仅管理员，其他人 403）。 |

### 收藏 / 回收站 `newsd/marks.py`（迁移 `0006_m3_marks`，表 `item_marks`）

- 每个（用户, 条目）一个状态：`favorite` 或 `trash`。回收站里的条目不再出现在这个用户的日报页，但其他用户不受影响，共享池也不动。
- 只能标记**出现在自己某一期里**的条目（否则 404），不能借此探测别人的条目。
- `PUT /api/me/items/{id}/mark`（`{"state": "favorite"|"trash"|null}`，null 是清除/恢复）、`GET /api/me/marks?state=`、`DELETE /api/me/marks?state=`（全部恢复 / 清空收藏）。`GET /api/me/issues/...` 的每个条目多了 `state` 字段。

### 设置页的数据

不新增存储，全部走已有接口：订阅 `/api/me/subscriptions`、关键词 `/api/me/keywords`、配额 `/api/me/quotas`（M2-1），模型 `/api/me/llm`、类别与阈值 `/api/me/profiles`（M2-4，key 用主密钥加密，页面只显示后 4 位）。新增 `newsd/userprefs.py`：

- **推送偏好** `GET/PUT /api/me/push`：开关、时间 `HH:MM`、时区（IANA）、三个渠道（email / telegram / webhook）各一个开关和目的地，存在 `user_settings.push`。校验：时间格式、时区存在、邮箱格式、Telegram 数字 chat id 或 `@频道`、webhook 必须 http(s)；启用渠道必须填目的地。**只存偏好，真正发送是 M4**（机器人 token / SMTP 属服务器配置；webhook 目标的内网地址限制放在发送端做）。
- **主题色** `GET/PUT /api/me/theme`：固定调色板 indigo / emerald / amber / rose / slate，存在 `user_settings.theme`，页面 `<html data-theme>` 取用户自己的值；同时写一个 `newsd_theme` cookie，让登录 / 注册页也跟随上次的颜色。亮色 / 暗色跟随系统。**“每周自动换主题”没有迁移过来**：新项目里没有任何轮换逻辑，颜色只有用户自己点才会变。

### 管理后台 `newsd/admin.py`

- 用户：列表（订阅数、期数、最近一期）、启用 / 停用（不能停用自己）。
- 邀请码：`GET /api/admin/invites` 列状态（unused / used / expired，谁建的谁用的；库里只有哈希，所以明文只在生成那一刻显示一次）、`DELETE` 作废未使用的。
- 注册方式：`invite` / `open` / `closed`，页面下拉保存（沿用 M1 的 `POST /api/admin/registration`）。
- 信息源目录 `GET /api/admin/sources?days=30`：每个源的订阅人数、最近一次抓取状态，以及质量参考：
  - 采集 = 近 N 天该源提供的不同条目数；
  - 入选 = 其中进入过至少一位用户日报的条目数；
  - **通过率** = 入选 / 采集；**均分** = 这些入选条目在日报里的平均最终分（0–100，所有用户的记录一起算）。
  - `PATCH /api/admin/sources/{key}` 启用 / 停用、改名、开关全文抽取；`POST /api/admin/sources` 加 RSS 源（只收 http(s)）。停用的源不会出现在用户的目录里。
- 运行记录 `GET /api/admin/runs`：最近的信息源抓取（`source_fetches`）和各用户日报生成记录（`issues`：状态、候选 → 入选、模型）。

### 注册后的引导 `newsd/onboarding.py`（可选项）

`GET /api/onboarding` 列出兴趣方向（= M2-4 的处理类别）和各自的预设源；`POST /api/onboarding {"interests": [...]}` 一键订阅这些预设源并把对应处理类别设成用户的选择（保留已有阈值），空列表表示“先跳过”。预设写在代码里，目录里不存在 / 被停用的 key 会被跳过；订阅是幂等的，只加不删。只要订阅过任何源或点过跳过，就不会再被带去 `/welcome`。

## 测试

`.venv/bin/pytest`：152 个通过（M2-3 基线 141 个 + 新增 11 个）。新增 `tests/pages/`：

- `test_pages.py`
  - **验收：两个用户各看各的**：同一个共享池，alice 关注 Qwen、bob 关注 Sora，各自 `/` 只有自己的条目和用户名；`/issue/{key}` 不会串。
  - **设置页互不影响**：alice 设了屏蔽词 / 配额 / 推送 / 主题 / 模型与 key，bob 的设置页里没有任何这些内容（包括 key 明文，alice 自己的页面也只有后 4 位）；bob 改自己的主题、配额、推送后 alice 的不变。
  - 收藏 / 回收站按用户隔离：alice 移除的条目 bob 仍然能看到；不能标记不在自己期次里的条目；恢复、取消收藏。
  - 日历只列自己的期次，月份导航和非法月份参数。
  - 所有页面和新接口未登录跳转 / 401；外部数据里的 `javascript:` 链接不会变成 href、标题里的 HTML 被转义；主题 / 推送的校验，登录页跟随 cookie 主题。
- `test_admin_onboarding.py`：普通用户访问 `/admin` 和管理接口 403；用户停用 / 启用、不能停用自己；邀请码状态、只在生成时见明文、已使用的不可作废；注册方式切换；源目录质量统计（通过率 100% / 25%，未订阅源 0，抓取状态）、运行记录、停用 / 新增源（拒绝 `file://`）；onboarding 一键订阅、非法兴趣不留半截数据、幂等、跳过、不影响其他用户。

## 已知限制 / 后续

- 页面是服务端渲染 + 少量原生 JS，改设置后用接口保存，没有实时刷新；没有 CSRF token，依赖 SameSite=Lax 和 JSON 请求体（与 M1 的 session 方案一致），上线前建议加。
- 源质量按全站统计，不区分用户；入选条目的分数包含不同用户的打分，只作取舍参考。
- 推送偏好只存不发（M4）；调度器（谁几点出刊）仍待做，现在用页面按钮或 `newsd issue build`。
- 管理员不能在页面里改别人的角色或重置密码（仍用命令行）。
