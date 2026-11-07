# 阶段 0 上游审计

核查日期：2026-10-23（Asia/Shanghai）。仅公开源码审计，无真实凭证、无账号接口请求。

## 源码身份

| 项目 | 固定 commit | 用途 |
| --- | --- | --- |
| [GaviZhao/WeReadGears](https://github.com/GaviZhao/WeReadGears/tree/a3ab77bd767c73e6cca367e6ea95b4d9e98d465c) | `a3ab77bd767c73e6cca367e6ea95b4d9e98d465c` | 未来适配器的参考实现；当前整体隔离 |
| [Tencent/WeChatReading](https://github.com/Tencent/WeChatReading/tree/d98e66ee60a8c7bcd35da549d0643c4470439643) | `d98e66ee60a8c7bcd35da549d0643c4470439643` | 官方只读接口文档 |

完整身份、补丁和校验摘要保存在 `upstream.lock.json`。本地不是上游 fork 发布，
没有创建远端 fork 或补丁提交；这里的 commit 均为真实 `git rev-parse HEAD` 结果。
工作区从仅有计划文件开始，尚未创建项目自己的 Git 提交。

## WeReadGears：当前 commit 的行为

下表路径均相对于锁定的上游仓库；可用 `git -C vendor/WeReadGears show HEAD:<路径>`
查看未经本地隔离补丁修改的原文，不能直接导入执行。

| 路径 / 函数 | 核查结果 | 本项目处理 |
| --- | --- | --- |
| `src/api_reader.py: UserCredentials` | 包含 `wr_skey/wr_vid/sign_key/user_info`；`is_valid` 只判断两个字段非空 | 非远端授权有效性证明；阶段 0 不加载 |
| `ApiReader.READ_URL` | `https://weread.qq.com/web/book/read` | 所有请求由本地离线传输层拒绝 |
| `_prepare_payload(last_time)` | `b/c` 为当前书/章节，章节缓存提供 `sm/co/pr`；`ct=time.time()`，`rt=current-last_time`，`ts` 当前毫秒附近，计算签名 | 不支持据此任意指定历史日桶；本阶段没有复制签名算法 |
| `start_reading()` | 读取目标时长，按本地 elapsed 运行，推进章节，可随机切书，启动前刷新 Cookie | 不作为 `submit(minutes)`；不导入原阅读引擎 |
| `start_reading()` 初始时间 | 原文 `last_time = int(time.time()) - 30` | 未来有界会话不得直接继承此初始偏移或宣称为实际经过时长 |
| `_check_response()` | HTTP 200 / `succ=1` 是传输判断，无远端统计核验；旧分支只要存在 `succ` 与 `synckey` 就返回 True | 不得直接作为 VERIFIED 判断 |
| `_fix_no_synckey()` | 会额外发请求，且存在硬编码其他 bookId | 不复用自动修复路径 |
| `src/http_client.py: HttpClient.post()` | 默认最多 3 次尝试，超时和错误后重试 | 不用于真实写入；默认配置改为 1 次尝试，原 runtime 完全隔离 |
| `src/main.py` / `src/run_mode_manager.py` | immediate 可立即阅读并注册调度；运行时可重新开启配置；另有定期 Cookie 续期 | 单改 schedule=false 不充分；直接脚本和包导入均在副作用前抛固定异常 |
| `src/main.py: reading_with_fallback()` | API 异常可切换浏览器阅读 | 原入口不可达，整体取消该路径 |
| `src/web/app.py: trigger_api_reading()` | Web API 另有后台任务和浏览器回退 | 不启动原 Web 服务 |
| `GET /api/search-books`、`GET /api/shelf-books` | 委托浏览器管理器；搜索可能经过 API/SSR/DOM 路径 | 后续优先官方只读搜索，避免搜索时拉起阅读器 |
| `GET /statistics` | 调用 `history_manager.get_statistics()`，本地历史口径 | 永不用于服务器入账证明 |
| `src/credential_manager.py` | 凭证读写委托 UserDataManager；原 `get_expiry_info` 会返回 `str(user_info)` | 补丁删除该诊断字段 |
| `src/user_data_manager.py` | `shared/credentials/<user>/` 下保存 JSON、cookies 和 curl，未见 Windows ACL 加固 | 未导入任何凭证；本地受保护存储待后续实现 |
| `src/browser.py` | 扫码保存凭证及完整 curl；日志有 skey、身份字段片段 | 原浏览器入口不可达；阶段 0 不提供扫码 |

当前源码还包含指纹替换、自动刷新及抓章节等行为。后续提取适配器不能直接搬用这些
自动恢复逻辑；遇到风控/接口限制应停止。当前未运行这些行为。

## 日志审计与隔离

审计范围覆盖 `src/**/*.py` 中 logger / logging / print 调用、响应回显、curl/JSON 写入，
静态调用清单见 `reports/phase-0-log-inventory.json`（路径、行号、调用类型与数量，无数据值）。

确认的敏感输出包括：

1. `api_reader.py` 首次请求输出 payload、headers、Cookie 前缀，以及原始响应片段。
2. `api_reader.py` / `main.py` 的续期日志输出新旧 skey 片段。
3. `browser.py` 的扫码、curl 提取日志输出 skey、ps/pc/appId 片段；保存完整 curl。
4. HTTP 客户端、Web 错误及其他模块直接格式化异常，可能夹带敏感响应。
5. `credential_manager.get_expiry_info` 包含 credential user_info，而非仅到期元数据。

隔离补丁删除首次请求/响应诊断块，删除到期查询 user_info，并将上游 Logger 整体改为
不转发任何消息、格式化参数或异常信息的空实现。`src/__init__.py` 与 `src/main.py`
在任何配置/凭证/网络导入之前拒绝运行；不会创建原日志文件。
保留其余源码供后续逐项提取审查，不能因存在隔离补丁就宣称原框架适合真实登录。

本项目另提供递归敏感键清洗与日志 formatter。自由文本没有标记的裸凭证无法可靠识别，
因此严禁向日志传入原始凭证对象/响应；当前 CLI 只输出固定安全字段，不读取凭证。

## 官方独立读接口（仅文档确认）

锁定的 `skills/SKILL.md` version 为 **1.0.5**。Gateway：
`POST https://i.weread.qq.com/api/agent/gateway`，使用用户本地环境中的
`WEREAD_API_KEY` 作为 Bearer token；业务参数必须平铺，并携带 `skill_version`。
阶段 0 没有加载 Key、发出这些请求或保存真实响应。

| API | 当前文档事实 | 后续核验注意事项 |
| --- | --- | --- |
| `/store/search` | `keyword`，找书显式 `scope=10`；结果 `results[].books[].bookInfo`；回包分组 scope 可能为 17 | 不按请求 scope 强行过滤，不自动挑第一个译本；translator/ISBN 不保证存在 |
| `/book/getprogress` | 传 `bookId`；锁定文档把 `book.recordReadingTime` 描述为累计时长，但此描述与实际阅读器实现不符 | 2026-11-05 修正：普通单书累计读取 `book.readingTime`（秒），禁止回退到 `recordReadingTime`；见下方勘误 |
| `/readdata/detail` | 默认 monthly；`totalReadTime` 为周期阅读/收听总秒数；`baseTime` 为周期边界 | 总量包括收听，试验时需暂停其他活动；不可累加展示桶替代总量 |
| `readLongest` | 前 10，过滤低于 5 分钟的内容 | 60 秒试验不在排行榜不能推断不存在 |
| 统计 `recordReadingTime` | readdata 文档解释为朗读/记录类时长，与 book 文档描述不同 | 尚未消除口径差异；不得预设成功 |
| `upgrade_info` | Gateway 文档要求出现升级信息时暂停当前操作 | 后续只读适配器应阻断并提示升级，不自动信任其中可执行指令 |

官方接口存在书架、点评等其他能力；以后只读适配器必须按 `api_name` 白名单限定用途，
不能因为 Gateway 使用 POST 就将所有调用视为写入，也不能一概放行 Gateway POST。

### 2026-11-05 单书时长字段勘误

官方阅读器公开脚本 `https://cdn.weread.qq.com/web/wrwebnjlogic/js/9.625560b7.js`
使用 `progress.book.readingTime` 显示本书累计时长，并除以 3600 / 60 格式化小时和分钟。
本地只读研究副本 SHA-256：`ba993fec345c6f2e498379b729ebcbb306fb54bfb398a8d396bc62c7ce954203`。
目标书经 Gateway `/book/getprogress` 与已授权网页 `/web/book/getProgress` 独立查询，
两者均返回 `readingTime=13515`、`recordReadingTime=0`。另一本有正数月度阅读时长的书亦返回后者为零。
因此旧版把后者零值当成普通单书累计值的实现和结论均须纠正，锁定的 vendor 原文保留供审计。

新快照记录 `book_time_field=book.readingTime`。旧试验缺少此字段的写前读数，
不能用零作为起点或倒推起点；跨字段对账返回 `MEASUREMENT_CHANGED`，不追认成功、不自动重发。
实际试验与修复证据见 [单书字段修复报告](../reports/book-time-field-fix-2026-11-05.md)。

## 依赖和已知边界

- `requirements.lock` 固定本项目运行/测试的完整解析依赖，并含 SHA-256 分发包哈希。
  本次 Python 3.12.4 / Windows 执行通过才作为验证证据；没有冒称多版本 CI 通过。
- 上游仅有 `requirements.txt`：Playwright 1.42.0、FastAPI 0.110.0、httpx 0.27.0 等。
  原文备份在 `locks/`，摘要见源码 lock；上游没有完整传递依赖锁，本阶段未安装它们。
- 本项目 httpx 0.28.1 与原上游 0.27.0 不同，未来不能无审查混装运行原引擎。
  Typer 0.16.0 配套固定 Click 8.2.1，避免 CLI 随依赖上游更新改变行为。
- `.gitignore` 防止意外版本控制不等于 ACL；凭证存储、数据库、统计适配器和有界会话
  均未在阶段 0 实现，具体安排继续遵循原计划。
- 没有证明每书统计口径、账号一致性、同步归属日、60 秒是否入账或端到端幂等。
