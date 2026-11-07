# PaperRead Sync

Windows 本地运行的纸质书阅读账本，支持版本确认、阅读记录、官方只读统计、保守核验与小额有界试验。
按根目录实施计划逐阶段实现，已改为最后统一验收。

2026-11-05 最新实际结果：《孟子译注》纸质记录已核验 **3600 秒**，状态 VERIFIED，剩余 0 秒。
真实分段执行、统计延迟恢复、手机并发阅读差额及授权续期证据见 [真实一小时报告](reports/real-hour-mencius-2026-11-05.md)。

**0.2.0 已接通本机扫码、书目/网页路由映射和直接同步。2026-11-05 已观察到目标书与月度同增 64 秒，
但超出原试验批准的 60 秒，尚未精确归属，不代表整条一小时记录已同步。**
旧版误读单书字段零值的结论已修正，详见 [字段修复](reports/book-time-field-fix-2026-11-05.md)。
正常同步不再要求先试一分钟或手工填写 App 数字，详见 [直接同步说明](reports/direct-sync-2026-11-05.md)。
随后已确认旧网页登录失效（-2012），完成同账号重新扫码与写前有效性校验；
见 [会话修复记录](reports/session-repair-2026-11-05.md)。新会话有效不等于已完成真实入账。
搜索 Key 的重启读取与恢复提示修复见 [搜索授权恢复记录](reports/search-key-recovery-2026-11-05.md)。
未决试验下的当前时长读取与恢复入口修复见 [读取时长恢复记录](reports/baseline-recovery-2026-11-05.md)。
同步前手机核对已改为显示读数后勾选确认，见 [核对流程修复记录](reports/client-review-fix-2026-11-05.md)。
重复查询保留有效确认；第 7 步也可一并完成显示读数核对，见 [试验按钮修复记录](reports/trial-button-fix-2026-11-05.md)。
首次试验及手工客户端核对保留在高级操作，正常网页可直接同步可用时长。
最新实施证据见 [2026-11-04 集成记录](reports/integration-2026-11-04.md)。
详情见 [统一验收说明](reports/FINAL_REVIEW.md) 和 [真实试验协议](docs/LIVE_TEST_PROTOCOL.md)。

## 直接体验

Windows 日常启动：双击项目根目录的 **`start.cmd`**。脚本使用本项目已有的 `.venv`，
启动后自动打开 `http://127.0.0.1:8766`；同一项目已运行时只打开现有页面，不启动第二个服务。
首次启动的命令窗口请保持打开，按 **Ctrl+C** 停止服务。打开页面不会自动开始阅读计时。

找不到启动窗口时，双击根目录的 **`close.cmd`** 即可关闭默认 8766 端口上的本项目服务，
再次双击 `start.cmd` 可重新启动。关闭脚本先核对项目路径、进程命令和端口 PID，
不会结束其他项目或所有 Python 进程；已经关闭时直接提示无需操作。账本和授权文件保留。
若正在同步，先在网页取消并等待停止，再关闭服务。关闭脚本出错时保留窗口显示原因。
自定义端口可运行 `close.cmd -Port 8767`；仅检查可运行 `close.cmd -CheckOnly`。

也可在项目根目录的 PowerShell 执行：

```powershell
.\start.cmd
# 不自动打开浏览器
.\start.cmd --no-browser
# 8766 被其他服务占用时选择其他端口
.\start.cmd --port 8767
```

右上角的“官方只读授权”和“微信读书网页登录”通过弹窗操作；搜索区点击标题展开/收起。
阅读账本默认只显示未完成记录，可用“显示”下拉框切换已完成或全部；只有全额核验的记录才算完成。点击某条记录展开操作流程，再次点击或点击“收起流程”可收起。
历史操作记录默认折叠，点击展开查看本次启动期间的记录；进行中的任务单独显示。
“待查保留”表示已尝试提交但未能准确核验的额度，暂扣以免重复提交；可提交为 0 仍可能未完成。页面可展开说明并进入条目高级操作进行只读诊断。
“已确认的书籍”下拉框和旁边的按钮均可打开书籍管理。移除只影响当前账号的新记录选书列表，可恢复；原有阅读记录、核验结果和待查额度保留。

网页登录以当前 Windows 用户 DPAPI 加密保存，重启后复用。真实 Web 服务启动时在后台检查一次，
准备书籍和每批同步前也会检查；仅会话过期时，先复用同账号的较新本机授权，再尝试一次官网
`/web/login/renewal` 正常续期，重新校验成功后保存。该过程不发送阅读时长、不改账本或书籍对应关系，
也不会重发失败的阅读请求。只读 API Key 仍独立保存。
官方撤销授权、拒绝续期或要求扫码时需要重新扫码，无法保证永久登录。
关闭应用后不运行保活任务；登录弹窗中显示本次启动检查的结果。

当前工作区已安装虚拟环境，可在项目根目录直接运行：

```powershell
.venv\Scripts\paperread doctor
.venv\Scripts\python scripts/mock_demo.py
.venv\Scripts\paperread --mock serve
```

真实模式使用 `.venv\Scripts\paperread serve --port 8766`，浏览器打开 `http://127.0.0.1:8766`。
页面顺序为：加密保存官方 Key → 本机扫码 → 搜索/人工绑定版本 → 保存实际记录 → 准备本书上下文 →
在“直接同步阅读时长”查看本次额度，勾选确认后开始。程序自动查询基线并按批核验，不要求手工抄写手机读数。
授权和准备阶段的浏览器计时请求全部在网络发送前中止。扫码只在新开的 Edge/Chrome 窗口进行，不需要手工抓 Cookie 或 JSON。

最后一条启动 [本地 Mock 界面](http://127.0.0.1:8765)，Ctrl+C 停止。它只操作合成数据，
可在表单绑定 `mock-` 开头的书籍并记录阅读，依次进行“基线与核对 → 小额试验 → 核验 → 余额”。
`mock_demo.py` 自动运行完整 CLI 演示，在临时数据库验证 3600 → 60 → 3540 → 3600 秒，
输出 `reports/mock-demo.json`；这些数字不是用户真实账号证据。

默认 CLI 离线；真实 Web 启动仅自动检查已保存的网页登录并按需续期，不启动阅读。
无调度、守护、后台阅读、阅读请求自动重试或浏览器回退。
CLI 只读查询使用全局 `--read-only`；CLI 真实写入使用双参数和精确交互确认，网页真实写入使用绑定书籍/额度的一次性预览及本人勾选确认。
Mock 与真实数据库严格分离；Mock 终态为 `MOCK_VERIFIED`，不能变成真实记录的核验证据。

## 本地记录

全局选项放在命令前：`--root`、`--db`、`--account`、`--mock`、`--read-only`。
以下 `BOOK_ID` 和 `ENTRY_UUID` 都需替换为真实、人工确认的值。

```powershell
# 离线人工录入版本；也可使用官方 search + bind
.venv\Scripts\paperread bind BOOK_ID --title "安娜·卡列尼娜" --author "列夫·托尔斯泰" --translator "本人已核对的译者" --confirm
.venv\Scripts\paperread add --book-id BOOK_ID --date 2026-10-22 --minutes 60
.venv\Scripts\paperread list
.venv\Scripts\paperread preview ENTRY_UUID
.venv\Scripts\paperread export --format csv --output data\entries.csv
.venv\Scripts\paperread export --format json --output data\entries.json
.venv\Scripts\paperread import data\entries.json
```

分钟只接受 1–1440 的整数；内部使用整秒。阅读日期、同步时间分开记录，不伪造历史服务器日期。
未来日期、空书名和非法时区被拒绝。UUID 可指定 `--entry-id`，同 ID 同内容导入返回原记录；
同 ID 不同内容报冲突。同书/同日/同样时长提醒可能重复，独立会话需显式 `--new-session`。
批量导入整体事务，永不触发远端同步。

## 官方只读与真实试验

本人在本机取得 `WEREAD_API_KEY`，并准备合法授权的目标书籍上下文；不要把任何凭证发到聊天。
项目使用独立的本机浏览器授权流程，不会启动被隔离的 WeReadGears 原框架。
也可以用 CLI `.venv\Scripts\paperread credentials key` 隐藏输入 Key 并以 DPAPI 保存，
`.venv\Scripts\paperread login` 扫码，`.venv\Scripts\paperread prepare ENTRY_UUID` 准备本书上下文。

```powershell
.venv\Scripts\paperread --read-only search "安娜·卡列尼娜"
.venv\Scripts\paperread --read-only bind BOOK_ID --confirm
.venv\Scripts\paperread --read-only baseline ENTRY_UUID
.venv\Scripts\paperread probe ENTRY_UUID --seconds 60 --dry-run
```

执行真实写入前需完成扫码及本书上下文准备，并确认同一账号及实际阅读。
正常网页直接同步会自动获取基线。网页直接同步和待查重新提交允许每批最多 5 秒的小幅统计误差，按批准额度核验，保留实际观测读数。高级手工对账继续支持 App 按秒或整分钟显示。
完整命令及 L0–L6 步骤见 [LIVE_TEST_PROTOCOL.md](docs/LIVE_TEST_PROTOCOL.md)。
真实 `probe --live --confirm` 仍需键入精确书籍/额度确认；缺任一条件、CI、基线过期/变化、
书籍上下文缺失或未决意图都阻断。不得将 HTTP 200、succ=1 或本地 history 视作入账。

`verify`/`reconcile` 只查询；不自动重发。必须以独立单书 + 月度总量核验增量，
并确认没有其他活动和自然日异常。直接同步及 `submit --remaining` 不再要求本记录先通过 probe；
每批最多 60 秒，自动核验每批。单书和月度可能更新时间不同，首次读数不齐时会先仅查询等待，
最多等待 60 秒后重新判断；不重新发送该批阅读请求。确认两者增量一致后才处理下一批。
真正异常或等待后仍未对齐才中止，结果页明确显示整项批准量、本次已核验量和尚未核验量。
部分增量保留未决意图，不提供强制成功开关。

待查记录可在条目中的“重新提交待查时长”勾选确认后重新提交，可能重复累计的提示会显示在按钮上方。程序从最新统计建立基线，按原记录剩余额度重新计时并核验。旧尝试的原始读数和响应记录保留，重复点击返回同一任务。

网页“直接同步阅读时长”可一次确认处理可用时长；一小时全新记录直接提交一小时，无需先单独提交一分钟。
程序仍需运行相应时长加查询时间，不支持瞬间补写。旧版第 7、8、9 步在高级操作中保留。
有旧未决写入时，直接同步预览会明确保留整个旧批准额度和已观察到的更大单书增量，再处理其余时长；
不会把旧写入标为成功，也不会重复发送。读数变化使保留额度变化时，需刷新后确认新预览。
旧写入属于同账号另一本书时，也可直接在当前书页面确认处理：待查额度保留在原条目，
不会从当前书的可用时长中扣除，不再要求先返回原书页面。

官方缺少可信单书字段时直接 BLOCKED，当前不支持人工单书值替代缺失的 API 字段。
单书历史累计使用 `book.readingTime`，不使用 `recordReadingTime`。2026-11-05 已修正旧版误读零值的问题；
旧试验缺少正确字段的写前快照，不能追认单书增量，详见 [修复说明](reports/book-time-field-fix-2026-11-05.md)。
读 API Key 和写 Cookie 对应账号仍需本人确认，不能根据这些接口文档自动证明。
已实测官方搜索/详情/统计读取、扫码、目标书上下文、真实分批一小时记录及单书/月度核验。
实际报告与离线合成测试分开保存；《十八家诗钞》的旧待查记录不混入《孟子译注》的完成结论。

## 安装与测试

本次验证环境：Windows / Python 3.12.4。项目声明 Python 3.11+，未声称其他版本已测试。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --require-hashes -r requirements.lock
.venv\Scripts\python -m pip install --no-deps -e .
.venv\Scripts\python scripts/bootstrap_upstream.py
.venv\Scripts\python scripts/check_project.py
```

`requirements.lock` 包含核心、测试、Web 及浏览器可选依赖的完整版本和包哈希。
包安装和源码准备访问公开包源/GitHub；验收脚本不访问真实账号，测试禁止外部网络。
Web 表单测试通过内存 ASGI 传输；Windows asyncio 的内部 socketpair 单独允许。

需要更新依赖时审查并运行：

```powershell
.venv\Scripts\python -m piptools compile --extra test --extra web --extra browser --generate-hashes --strip-extras --no-emit-index-url --output-file requirements.lock pyproject.toml
```

## 文件与安全边界

- `reports/phase-0.md` 至 `phase-6.md`：逐阶段历史及当前交付结果。
- `reports/final-evidence.json`：最终实际测试、诊断、依赖检查、Mock 演示和文本扫描结果。
- `reports/live-probe.md`：真实试验未通过，保留未决意图，与合成证据分开。
- `docs/DATA_MODEL.md`：事务、幂等、额度、单写锁与恢复规则。
- `docs/RESEARCH.md`：锁定上游及字段口径审计。
- `upstream.lock.json` 与 `patches/`：原上游隔离仍生效；纯协议提取代码保留 MIT 许可。

真实写入凭证使用当前 Windows 用户的 DPAPI 和私有 ACL，位置在本机 LocalAppData；
API Key 优先读取当前进程环境，否则读取本机 DPAPI 存储。Git 忽略 `.env`、data、shared、日志、vendor 和虚拟环境。
原凭证 JSON 导入后不会自动删除，需由本人自行保管。不要把受保护数据、抓包或 Cookie 放进报告。

真实网页直接同步要求一次性书目/额度预览和本人勾选实际阅读、同账号及暂停其他活动，
高级旧表单保留 live/confirm 和精确短语。共同复用事务、基线、去重、单写锁及独立核验。
取消不会重发已发送的请求；重启保留未决意图，可只读对账或在明确预览后保留旧额度再处理其余时长。
