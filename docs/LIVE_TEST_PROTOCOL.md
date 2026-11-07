# 真实同步与可选试验：本机操作手册

**2026-11-05 已完成《孟子译注》3600 秒真实记录的服务端核验，剩余 0 秒。**
执行含中途暂停和恢复，证据见 [真实一小时报告](../reports/real-hour-mencius-2026-11-05.md)。
真实写入仍需本人明确批准，旧待查条目的结论不随本条完成而改变。
用户已改为最后统一验收，开发过程不再逐阶段等待；真实账号授权和每次真实写入的本机确认继续保留。

## 默认流程：直接同步

2026-11-05 按用户要求简化：完成以下授权、书目和上下文准备后，正常网页使用“直接同步阅读时长”。
检查书名、版本、实际纸质记录和本次时长，勾选一项确认并点击同步即可。
程序自动采集写前快照、每批最多 60 秒执行并查询独立单书/月度统计，不要求先做一分钟试验或手工输入 App 数字。
一小时全新记录可一次批准一小时，实际需运行约一小时加查询时间；不是瞬间补写历史时长。

有旧未决写入时，预览会列出需要保留的旧额度（整个批准额与已观察到的更大单书增量取较大值），
本人确认后才保留，再处理其余实际阅读额度。不将旧笔记为成功或释放成可重写余额。
手机展示匹配和服务器精确增量归属分别记录；自动核验不伪造 `client_confirmed`。
首次查询只要两种增量都在本批批准上限内，且表现为无更新、部分更新或相互不齐，
会每 3 秒仅查询一次，最多等待 60 秒，直到独立双增量完全一致后继续下一批。
超过批准额度、来源/账号/周期变化、计数下降或等候后仍无法归属时停止，不自动重发。

下面的 L0–L6 是可选试验与高级只读对账路径，不是正常直接同步的前置门槛。

## 授权与书籍

1. 在自己的设备上取得官方只读 API Key：[官方入口](https://weread.qq.com/r/weread-skills)。
   在本机网页保存 Key，或用 `paperread credentials key` 隐藏输入并加密保存；也支持进程环境变量
   `WEREAD_API_KEY`。不要把值写进本仓库、聊天或报告。
2. 启动 `paperread serve --port 8766`，在网页点击扫码登录，进入新开的本机 Edge/Chrome，用手机微信扫码。
   Key、Cookie、storage state 和本书上下文由当前用户 DPAPI 加密，不要求手工抓包、拼 JSON。
   所有浏览器阅读计时请求在发送前中止，不自动翻页，不复制上游的自动调度、指纹覆盖或重试逻辑。
3. 网页搜索并人工确认具体版本，保存真实纸质记录。CLI 也支持：

```powershell
.venv\Scripts\paperread credentials key
.venv\Scripts\paperread login
.venv\Scripts\paperread --read-only search "安娜·卡列尼娜"
.venv\Scripts\paperread --read-only bind BOOK_ID --confirm
.venv\Scripts\paperread add --book-id BOOK_ID --date 2026-10-22 --minutes 60
```

把 `BOOK_ID` 替换为人工核对作者、译者、出版社、ISBN 后的实际版本 ID；不要直接复制示例字符串。
`bind` 会读取详情并显示供确认。若版本不明确，需明确 --corresponding-edition，不能假定第一项正确。
记下 add 返回的真实 `entry_id`（下称 `ENTRY_UUID`）。

之后点击网页“准备本书上下文”，或执行 `paperread prepare ENTRY_UUID`。程序只使用该书官方返回的
HTTPS readerLink，保存官方 bookId 与实际网页路由 ID 的对应关系。捕获的 b、页面 URL 与 referer 必须相符。
API 搜索和统计继续使用官方 ID，阅读请求使用已验证网页 ID；不猜 ID、不把搜索第一项自动绑定。
准备完成后浏览器自动关闭。源码入口隔离继续保留，不需要运行 vendor 原框架。

## L0–L2：独立基线与小额批准

暂停同账号阅读、听书及其他自动化，确认没有原框架或其他阅读进程。确认读 API Key、写凭证和 App
确属同一账号；本程序不能从官方文档中的这些接口自动证明账号身份对应关系，必须由本人核对。

```powershell
.venv\Scripts\paperread --read-only baseline ENTRY_UUID
```

网页第 6 步直接显示服务器查询到的本书与本月所有书的时长，不再要求手工抄写数字。
本人在手机 App 对照这些读数，核对统计口径、同一账号及暂停其他阅读后，勾选一项确认。
手机只显示小时/分钟时核对相应展示精度；确认会记录为 display_confirmation，
不会把服务器数字伪装成用户手工输入的 App 数字。点击时重新查询，值相同则更新有效采集时间，
值变化则显示新值并要求重新核对，不自动批准新数值。

第 7 步也支持在提交时一并核对显示读数：确认项明确包括手机读数相符，后台先复核，再创建写意图。
不必反复返回第 6 步。相同数值的重复查询会保留已有确认；数值变化仍要求本人核对新值。

CLI 的数值确认方式保留，输入单位仍为精确秒。App 读数必须由本人查看，不能把查询数字复制成独立证据。
按秒显示的 CLI 示例：

```powershell
.venv\Scripts\paperread --read-only attest ENTRY_UUID --app-book-seconds BOOK_SECONDS --app-period-seconds MONTH_SECONDS --same-account --no-other-reading
.venv\Scripts\paperread probe ENTRY_UUID --seconds 60 --dry-run
.venv\Scripts\paperread probe ENTRY_UUID --seconds 60 --live --confirm
```

真正创建写入意图前会重新获取 120 秒内有效的服务器测量，并核对数值与用户批准的显示一致。
页面停留时间不再单独导致重复核对；数值变化、来源变化或实际远端测量过期仍阻止提交。
首次写入还会显示书名/译本、纸质实际总时长和本次额度，要求键入
`BOOK_ID:60`。缺少任一参数、交互确认不匹配、CI/pytest 环境、凭证/书籍上下文不一致、
基线变化或过期，都不会发送写入。授权上下文缺失时停止，不自动开阅读器、换书、刷新凭证或尝试绕过限制。

会话实际等待最多批准的阅读时间，使用当前时间生成上游协议，单次最多 60 秒。
HTTP 超时和错误不重试；Ctrl+C 后也必须先对账。已发送请求可能远端已处理。

## L3–L6：服务器 + 客户端复核

命令返回 AWAITING_VERIFICATION 只表示收到传输响应。刷新 App，确认该书统计和月度总量的实际秒数，
检查同步自然日是否正确，再执行：

网页第 8 步仍需本人提供写后的 App 读数，默认按手机常见的整分钟填写，可切换为精确秒数。
展示精度不放宽服务器精确双增量、实际时间和额度要求。

```powershell
.venv\Scripts\paperread --read-only verify ENTRY_UUID --app-book-seconds AFTER_BOOK_SECONDS --app-period-seconds AFTER_MONTH_SECONDS --no-other-reading --same-day-confirmed
.venv\Scripts\paperread audit ENTRY_UUID --output data\live-probe-evidence.json
```

这里的 --same-day-confirmed 是本人核对服务器当前同步自然日，并不表示把旧纸质日期写进服务器历史桶。
paper reading_date 和 sync_at 分开保存。需要归属到历史日桶的能力当前不提供。

只有独立单书和周期增量一致、未超过批准/实测额度、首次 App 复核一致，才能归属到这条记录。
1 分钟书籍可能不进入 readLongest；工具完全不依赖该排行判断存在与否。

无变化、仅总量增加、账号/周期变动、读数异常、响应缺字段或限制均停止。当前不提供缺失单书
API 字段时的人工单书数值替代功能；此时按终止规则保留本地记录并 BLOCKED，需要进一步确认接口口径。
不同版本不能混计，不能用 WeReadGears 的本地 /statistics 代替服务器统计。

统计延迟时手动再次 verify 或 reconcile；不重发。小于授权额度的增量记为 PARTIAL，保留意图，
不为了“通过”扩大误差容忍。未经实际观察与客户端证明不进行强制结算。

如果本人明确确认该批期间在手机打开了同一本书正文，且有成功响应、完整实际经过时间、
同书/月度相等的超额增量，可使用高级只读人工对账 `--concurrent-reading-confirmed`。
这不会改变自动核验阈值：仅归属该批原批准额度，超出部分单独记录为 `excluded_surplus_seconds`，
同时记录人工确认及原始差量，`automatic_verification=false`，不伪造手机数字输入。
其他书活动、增量不相等、响应不确定或日期未确认时不能使用此规则通过。
继续写入前必须退出手机正文，暂停其他计时。示例：

```powershell
.venv\Scripts\paperread --read-only reconcile ENTRY_UUID --concurrent-reading-confirmed --same-day-confirmed
```

```powershell
.venv\Scripts\paperread --read-only reconcile ENTRY_UUID --app-book-seconds ACTUAL_BOOK_SECONDS --app-period-seconds ACTUAL_MONTH_SECONDS --no-other-reading --same-day-confirmed
```

verify/reconcile 返回码 0 表示核验已完成或取消已处理；2 表示仍待核验；1 表示前置条件或本地/远端错误。
对账进程即使重启也不会调用写适配器。不要删除 SQLite、writer.pending.json 或修改 verified_seconds 来跳过阻断。

## 剩余额度

remaining 不再要求本条记录先通过 probe。若可选试验实际已核验 60 秒，
余额只能是 3540 秒；没有试验的全新记录可直接批准全额。用户在本机确认可用余额，分为最多 60 秒一批，每批后都查询独立统计。
一批未通过即停止，不自动重新发送或开启另一条记录。

```powershell
.venv\Scripts\paperread submit ENTRY_UUID --remaining --live --confirm --no-other-reading --same-day-confirmed
.venv\Scripts\paperread audit ENTRY_UUID --output data\final-audit.json
```

最终报告必须基于实际 audit/远端/App 证据填写 `reports/live-probe.md`，包括书籍、实际纸质日期、
批准时长、B0/B1/T0/T1、差量、来源时间、客户端复核、活动排除、已核验和剩余额度。
当前该报告保留首次真实试验 WRITE_RESPONSE、UNCERTAIN / UNVERIFIED 的历史。
2026-11-05 已纠正单书累计字段为 `book.readingTime`；此前所谓“目标书无增量”基于错误字段，不能成立。
旧试验没有正确字段的写前读数，跨口径对账返回 `MEASUREMENT_CHANGED`，不会补记成功或自动重发。
详情见 [字段修复与旧试验证据](../reports/book-time-field-fix-2026-11-05.md)。
可使用 `paperread --read-only diagnose ENTRY_UUID` 或网页“只读诊断上次试验”查看差量，
诊断不提交时长、不代替 App 复核、不解锁写入。不得用 Mock demo 替换真实证据。
