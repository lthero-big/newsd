# 使用

**管理员**
1. 在「管理 → 用户与分组」里选择注册方式，建分组（角色、权限、默认订阅源、每日配额），然后生成邀请链接发给用户。命令行也可以：`newsd admin invite`、`newsd create-user <名字>`、`newsd set-role <名字> user|admin|readonly`。
2. 在「源健康」里看哪些源抓取失败，一键重试或停用。
3. 在「日志与任务」里看每次定时任务的时间线和出错详情，也可以手动运行一次。

**普通用户**
1. 注册后在引导页一键订阅一组预设信息源，之后在「订阅源」里按类别和分区调整。
2. 在「关键词、配额与推送」里设置关注 / 加权 / 屏蔽关键词、各分组条数，以及推送渠道和时间。
3. （可选）在「模型」里添加一个或多个大模型 provider，点「测试」确认可用。
4. 每天到点后在首页看当期日报，用日历翻看往期，收藏或删除条目。

**命令行**（Docker 里在命令前加 `docker compose exec web`）

```bash
newsd worker run-once            # 立即跑一轮：采集 + 给所有用户出日报
newsd worker runs                # 查看最近的运行记录
newsd issue build alice          # 只给 alice 生成今天的日报（--all 所有用户，--no-ai 不调用模型）
newsd push test alice telegram   # 用 alice 保存的配置发一条测试推送
```

**API**：先登录拿到 cookie，再用 `POST /api/tokens` 生成 API key（只显示一次，服务器只存哈希），之后放在 `Authorization: Bearer <key>` 请求头里调用：

```bash
curl -c cookie.txt -d username=alice -d password='你的密码' http://localhost:8080/login
curl -b cookie.txt -X POST -d kind=api -d name=script http://localhost:8080/api/tokens
curl -H "Authorization: Bearer $NEWSD_API_KEY" http://localhost:8080/api/me/issues
```
