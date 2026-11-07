# PaperRead Sync — 纸质书阅读时长同步微信读书（Codex 开发规格）

> 2026-11-05 用户变更：首次 1 分钟试验和手工 App 数字核验改为可选高级功能，
> 正常网页支持一次确认直接同步可用实际阅读时长，并自动查询写前/写后数据。
> 旧未决额度在本机明确预览并确认后保留，不重复发送、不伪造成功；仍保留异常停止、去重与真实服务器核验。
> 下文为原始开发规格，涉及强制首次试验/手工核验的流程条款由本变更替代。

> 文档日期：2026-10-22  
> 目标平台：Windows 11，本地运行；Python 3.11+；第一版 CLI，第二版可选本地 Web 界面  
> 基础项目：[GaviZhao/WeReadGears](https://github.com/GaviZhao/WeReadGears)  
> 独立校验文档：[Tencent/WeChatReading](https://github.com/Tencent/WeChatReading)  
> 当前结论：已核查两项目的公开代码/说明，但**尚未取得用户本地登录凭证，未执行真实账号写入测试。严禁将本规格当作“已成功入账”的证明。**

## A. 给 Codex 的首要指令（执行顺序不可更改）

你是本项目的工程实现者。请在**当前本地代码工作区**创建一个基于 WeReadGears 的、供用户手动补录实际纸质书阅读时间的工具。首要目标是**通过用户授权的小规模真实接口测试，证明某本指定书的服务器阅读时长以及账户总时长发生了可核验的变化**；在验证成立之前不得声称“同步成功”、不得向账号提交完整 60 分钟记录。

1. 先克隆/审计 WeReadGears，记录 `git rev-parse HEAD`；确认下列接口、字段、文件在当前 commit 中的真实行为。不要假定仓库未来不会更新。
2. 默认采用**离线/只读模式**。任何会改变微信读书服务器状态的操作必须由用户在本地明确点击或执行命令确认；默认绝对禁止后台自动阅读、自动重试写入、随机切书和自动回退到浏览器阅读模式。
3. 先完成本地记录、书籍绑定、只读统计快照、Mock 适配器、完整去重/恢复测试，之后才开放真实写入的 `--live --confirm` 参数。
4. 用一条 60 分钟纸质阅读记录做示例，允许将其**实际、经远端核验的 1 分钟试验额度**计入同一记录；若试验成功，剩余最多 59 分钟，而不是再提交 60 分钟。
5. 必须**先通过真实服务器数据核验**才能给记录打 `VERIFIED` 状态。仅 HTTP 200、`succ=1`、本地 history.json 增加，都不算通过。
6. 无法建立可信的每书统计读数、无法确认时长归属、服务器数据不变化时，输出 `UNVERIFIED` / `BLOCKED`，阻断后续写入，保留本地记录。
7. 任何密钥/Cookie/API Key、抓包数据都只能保存在用户的本地受保护位置；不得输出到聊天、提交 Git、日志或测试报告。
8. 只用于准确补记用户确实阅读的纸质书时长；不实现倍速虚增、挂机刷排行、绕过风控或任意伪造历史服务器日期。出现服务端限制时，停止并报告，不尝试绕过。
9. 每完成一个阶段，在 `reports/phase-N.md` 写入：改动文件、运行命令、测试用例、通过/失败、实际证据（脱敏）、未解决问题，并等待用户验收，再进入下一阶段。
10. 不要仅生成文档或伪代码；应按阶段交付可运行的代码和自动化测试。真实授权步骤必须由用户在自己的电脑上完成。

## B. 目标与不承诺的能力

**输入示例（真实纸质书阅读事件）：**

```json
{
  "id": "由系统生成 UUID，不可重复",
  "title": "安娜·卡列尼娜",
  "author": "列夫·托尔斯泰",
  "reading_date": "2026-10-22",
  "duration_minutes": 60,
  "source": "paper",
  "timezone": "Asia/Shanghai",
  "notes": "纸质书阅读；具体译本需由用户选择"
}
```

**预期用户工作流：** 搜书 → 人工选择译本并锁定 `bookId` → 本地保存纸质书记录 → 查看同步预览和重复提醒 → 读取微信读书远端基线 → 明确确认 1 分钟真实试验 → 调用写入适配器 → 再读远端数据核验 → 若成功，用户选择是否继续同步剩余时长 → 保存审计与证据。

**必须诚实告知的限制：**

- `reading_date` 是纸质阅读的实际发生日期；`sync_at` 是脚本调用服务器的时间。WeReadGears 源码按**当前时间**计算 `ct`/`ts` 等字段，不能推导出它能把旧日期写进微信读书的对应历史日桶。2026-10-22 的示例虽为当日，仍需在客户端核对所属自然日。
- 不承诺“输入 60 分钟，即刻在服务器增加 60 分钟”。现有阅读引擎基于持续会话和实际经过时间运行；具体可验证速度、服务器计入规则取决于实际测试。
- 如果服务器仅更新总量，不能证明《安娜·卡列尼娜》这一本书增加了时长，则**不得**称之为“同步成功”。
- 书籍版本不同（译者/出版社/ISBN），必须让用户确认微信读书的电子版；未找到确切版本时可用明确标记的“对应电子版”，不得静默选搜索第一项。
- 对已在微信读书上阅读的内容，无自动消重的可信统一来源：工具以用户提供的纸质阅读会话作为真实性基础。

## C. 已核对的源码事实和工程改造点

### WeReadGears

公开文件（实施时须按当前 commit 重新核查）：

- [`src/api_reader.py`](https://raw.githubusercontent.com/GaviZhao/WeReadGears/main/src/api_reader.py)：`ApiReader.READ_URL = https://weread.qq.com/web/book/read`；`UserCredentials`；`_prepare_payload()`；`_check_response()`；`start_reading()`； `_select_book_and_chapter()`。
- `_prepare_payload(last_time)` 生成当前 `ct`、`ts`、`rt`、签名等字段；`b` 对应 `bookId`，`c` 对应章节 UID，`sm/co/pr` 必须与书籍/章节一致。
- `start_reading()` 读取 `reading.target_duration`、会自动推进章节，在某些模式下随机切书，并维护**本地** `elapsed_seconds`。
- 仅收到 `succ=1` 是传输层成功判据，**不是服务器时长已入账的证明**；代码注释曾提到请求成功却静默不计入的情况。
- [`src/web/app.py`](https://raw.githubusercontent.com/GaviZhao/WeReadGears/main/src/web/app.py)：`GET /api/search-books?q=...`、`GET /api/shelf-books` 等可复用；`GET /statistics` 使用的是 `history_manager.get_statistics()`，**不是微信读书远端真实总时长**。
- [`src/credential_manager.py`](https://raw.githubusercontent.com/GaviZhao/WeReadGears/main/src/credential_manager.py)：管理扫码取得的本地凭证。
- [`config.example.yaml`](https://raw.githubusercontent.com/GaviZhao/WeReadGears/main/config.example.yaml)：`schedule.enabled: true` 默认开启，**第一步必须改为 false**；`daemon.enabled: false` 保持关闭；自动 API→浏览器兜底行为需专门取消。
- 发现一个需要修正的安全隐患：`api_reader.py` 的首次诊断日志会输出部分 Cookie、headers、payload 内容；开发版必须在真实登录前关闭这段日志或将全部敏感值完全脱敏。

**建议不要直接调用原始 `start_reading()` 来充当 `submit(minutes)`。** 该方法耦合随机选择、循环休息、配置和本地累计时间。应从其已有实现中提取/包装：

```python
class ReadWriteAdapter(Protocol):
    async def validate_session(self) -> bool: ...
    async def validate_book(self, book_id: str) -> bool: ...
    async def run_bounded_session(self, book_id: str, seconds: int, *, live: bool) -> 'WriteAttemptResult': ...

class RemoteStatsAdapter(Protocol):
    async def book_snapshot(self, book_id: str) -> 'BookStats': ...
    async def period_snapshot(self, mode: str) -> 'PeriodStats': ...
```

适配器必须固定单本书、单账号；不复用会随机切书的参数；不在失败时自行换引擎；不跨越用户批准的额度；允许 Ctrl+C 安全停止并进入待核验状态。

### 腾讯官方独立只读校验

官方项目说明：[`skills/SKILL.md`](https://raw.githubusercontent.com/Tencent/WeChatReading/main/skills/SKILL.md)，[`skills/book.md`](https://raw.githubusercontent.com/Tencent/WeChatReading/main/skills/book.md)，[`skills/readdata.md`](https://raw.githubusercontent.com/Tencent/WeChatReading/main/skills/readdata.md)。

官方 API Gateway 请求地址：`POST https://i.weread.qq.com/api/agent/gateway`；授权 Header 为 `Authorization: Bearer $WEREAD_API_KEY`，`WEREAD_API_KEY` 必须在用户本地自行取得。官方说明页有 [API Key 申请入口](https://weread.qq.com/r/weread-skills)。务必以**现场仓库的最新 `skill_version`** 作为 body 字段；文档核查时为 `1.0.5`。

**请求 1：指定书籍进度**（`book.recordReadingTime` 文档称累计阅读时长，单位秒；要通过 UI/测试确认与目标统计口径一致）。

```json
{"api_name":"/book/getprogress","bookId":"<用户已确认的真实bookId>","skill_version":"1.0.5"}
```

**请求 2：月度远端总量**（`totalReadTime` 为秒；`readTimes` 是日桶展示，不要把所有可见桶相加代替总量）。

```json
{"api_name":"/readdata/detail","mode":"monthly","skill_version":"1.0.5"}
```

**可选请求 3：周度/年度交叉核验**（更适合用户关心的阅读报告归属）。

```json
{"api_name":"/readdata/detail","mode":"weekly","skill_version":"1.0.5"}
```

注意：官方 `readLongest[]` 最多 10 条，低于 5 分钟的条目会被过滤；所以**1 分钟试验不能要求该书一定出现在排行榜**。此外 `recordReadingTime` 在不同接口文档里的解释可能存在口径差异，务必以实际返回和微信读书客户端为准；不许凭字段名直接宣布成功。

**只读核验应采用双重证据：**官方返回的该书有效单书时长（字段口径确认后）+ 官方周期 `totalReadTime`；如任一字段不可用，使用微信读书 App 的单书展示做人工复核，标注证据等级。不得使用 WeReadGears 本地 `/statistics` 替代。

## D. 推荐目录结构

```text
PaperReadSync/
├── README.md
├── pyproject.toml
├── .env.example               # 变量名，不含任何真实凭证
├── .gitignore                 # 忽略 .env、data、shared/credentials、log
├── paperread/
│   ├── cli.py                 # Typer CLI
│   ├── models.py              # Pydantic 数据模型
│   ├── db.py                  # SQLite 事务/索引/迁移
│   ├── book_match.py          # 搜索、候选匹配、人工绑定 bookId
│   ├── credentials.py         # 环境变量/本地凭证适配，不回显
│   ├── wereadgear_adapter.py  # 对 WeReadGears 的隔离封装
│   ├── official_stats.py      # 官方只读 Gateway 调用
│   ├── sync_service.py        # 状态机、额度控制、幂等
│   ├── verifier.py            # 前后对比、证据等级、异常识别
│   └── redaction.py           # 敏感日志清洗
├── tests/
│   ├── test_db.py
│   ├── test_dedup.py
│   ├── test_book_match.py
│   ├── test_state_machine.py
│   ├── test_verifier.py
│   ├── test_failure_recovery.py
│   └── fixtures/             # 不含真实个人数据的 Mock 数据
├── scripts/
│   └── live_probe.py          # 只能显式 --live --confirm 开启
├── docs/
│   ├── RESEARCH.md
│   ├── DATA_MODEL.md
│   └── LIVE_TEST_PROTOCOL.md
├── reports/
└── data/                      # .gitignore，仅本地；SQLite + 脱敏报告
```

建议依赖：`httpx`、`typer`、`pydantic`、`rich`、`pytest`，SQLite 使用标准库 `sqlite3`；浏览器/扫码/签名能力复用 WeReadGears 原项目并锁定版本，不要重复发明签名算法。安装、打包和 Docker 为第二优先级。

## E. 数据与状态机（重点）

### 数据表

**books**：`book_id TEXT PRIMARY KEY`, `title`, `author`, `translator`, `publisher`, `isbn`, `confirmed_by_user BOOLEAN`, `confirmed_at`。

**reading_entries**：

| 字段 | 用途 |
| --- | --- |
| `entry_id UUID PRIMARY KEY` | 单次纸质阅读记录不可变 ID；重复导入沿用该 ID |
| `book_id` | 已确认的微信读书版本 |
| `reading_date TEXT` | 真实阅读日期（示例 `2026-10-22`） |
| `timezone TEXT` | 示例 `Asia/Shanghai` |
| `duration_seconds INT` | 纸质书实际阅读时长，示例 `3600` |
| `verified_seconds INT DEFAULT 0` | 已可靠核验并归属本条记录的额度 |
| `source TEXT` | 固定 `paper` |
| `session_start`, `session_end` | 可选，区分同日同书的多次阅读 |
| `status TEXT` | 状态机当前状态 |
| `created_at`, `updated_at`, `sync_at` | 三种时间不可混用 |
| `note TEXT` | 用户备注 |

**sync_attempts**：`attempt_id UUID PRIMARY KEY`、`entry_id`、`attempt_no`、`requested_seconds`、`before_book_seconds`、`after_book_seconds`、`before_period_seconds`、`after_period_seconds`、`started_at`、`finished_at`、`transport_status`、`verification_status`、`request_hash`、`error_code`、`error_message_redacted`。保证 `UNIQUE(entry_id, attempt_no)`；写操作前先在一个 SQLite 事务中写入意图。

**remote_snapshots**：`snapshot_id UUID`、`book_id`、`period`、`source`（官方 gateway / App 手工录入）、`value_seconds`、`captured_at`、`attempt_id`、`raw_response_redacted`。不得保存 token、Cookie、完整认证头。

### 状态流

```text
LOCAL_ONLY（已存本地）
  -> READY（书籍确认、凭证及只读基线正常）
  -> PROBING（发送小额试验，已记录写入意图）
  -> AWAITING_VERIFICATION（请求已发，无权自行推断成功）
  -> PARTIALLY_VERIFIED（试验入账）
  -> SYNCING（用户再次确认剩余额度）
  -> VERIFIED（全额归属核验完成）
```

异常分支：`BLOCKED`（无法验证书籍/凭证）、`FAILED_CONFIRMED`（可确证没有写入）、`UNCERTAIN`（网络超时/响应模糊/统计未刷新）、`PARTIAL`（部分额度可核实）、`CANCELLED`（用户主动取消）。`UNCERTAIN` **绝不自动重发**；必须先查询服务端远端数据并人工对账。

### 去重与正确性规则

1. **严格幂等**：`entry_id` 唯一；同一导入 ID 再次提交返回既有记录；同一 `attempt_id` 永不重新发网络写入。
2. **近似重复提醒**：同账户 + 同书 `book_id` + 同日 + 同时长相同，弹出“可能重复”；**不能用这组字段强制唯一**，因为一天内读两次各 60 分钟是合法的。可通过开始/结束时间或用户“新建独立会话”确认新记录。
3. **额度不变量**：`0 <= verified_seconds <= duration_seconds`；任一次实际批准额度不能超过 `duration_seconds - verified_seconds`；基于已核验的远端增量调整额度，所有秒数都用整数。
4. **未知结果时停止**：如果请求已发出但客户端超时，可能远端已处理；不能当作失败直接重试。
5. **避免外部干扰**：试验期间用户暂停该账号在微信读书里阅读、听书及其他自动化；若数据变动超出目标额度或来自未知书籍，则中止归属判定。
6. **同时只能有一个写会话**：本机文件锁 + SQLite 状态锁；用户重开程序后先恢复未完成事务并对账。
7. **不保证远端真正 exactly-once**：远端未公开支持幂等键。应用只能提供本地防重和不确定结果的保守处理，不能虚构端到端 exactly-once 保证。

## F. 核心命令设计

```powershell
# 仅示意 CLI 协议；需要 Codex 实装，不保证当前存在
paperread doctor
paperread search "安娜·卡列尼娜"
paperread bind <书目候选ID>     # 再次显示作者、译者、出版社、ISBN 供确认
paperread add --book-id <真实bookId> --date 2026-10-22 --minutes 60 --source paper
paperread list
paperread preview <entry_uuid>   # 明确输出预计写入书籍/额度/同步日期/风险
paperread baseline <entry_uuid>  # 官方接口只读快照，不触发任何阅读
paperread probe <entry_uuid> --seconds 60 --live --confirm
paperread verify <entry_uuid>    # 查询独立远端统计、判定是否归属到指定书
paperread submit <entry_uuid> --remaining --live --confirm
paperread reconcile <entry_uuid> # 检查不确定状态、绝不自动重发
paperread export --format csv
```

`--live` 与 `--confirm` 缺任一者不得发写入请求；还要二次交互确认书名、译本、实际总分钟数和当前试验额度。CI/CD 测试环境强制禁止真实外网写请求。应提供 `--dry-run` 预览发送意图，但不能打印任何凭证、签名或 Cookie。

## G. 关键真实账号实验（**先做这个，再考虑 60 分钟**）

前置准备：用户本机扫码授权 WeReadGears，自己取得官方只读 `WEREAD_API_KEY`。确认使用的微信读书账号一致。`config.yaml` 的 `schedule.enabled=false`、`daemon.enabled=false`，且验证本地无其他阅读进程。**如果只能取得读 API Key，但无法登录 WeReadGears，不能提交写入。**

| 步骤 | 操作 | 可核验的结果 | 阻断条件 |
| --- | --- | --- | --- |
| L0 | 搜《安娜·卡列尼娜》，人工选版本 | 正确 `bookId`、作者、译者等 | 无法确认版本 |
| L1 | 只读查询书籍时长及当月总时长 | 两个带时间戳的服务器基线 `B0/T0` | 单书数据不可读且无人工验证办法 |
| L2 | 人工确认 `probe --seconds 60` | 一次有限额度的写会话，仅该 bookId | 背景任务未关、Cookie 异常、书籍不唯一 |
| L3 | 读取 `POST /web/book/read` 传输响应 | 记录 HTTP 状态、响应字段（脱敏） | 即使 succ=1 也**暂不判定成功** |
| L4 | 再取官方远端 `B1/T1`，必要时刷新客户端并重新查询 | `ΔB=B1-B0`, `ΔT=T1-T0` | 仅总时长增加、书籍时长不增 → 失败/未验证 |
| L5 | 独立客户端复核：打开该书阅读统计及周/月统计 | 确认属于此书、日期桶无异常 | 客户端与 API 数值冲突、服务端延迟无法排除 |
| L6 | 得出明确结论 | `VERIFIED` 或 `UNCERTAIN/BLOCKED` | 不成功不得继续大额写入 |

注：时长可能存在秒级误差或汇总延迟，**不要预先假定必然精确加 60 秒**。需要记录真实差量及统计口径；阈值依据观察到的服务器行为和实际经过的会话时间确定，并在报告中解释。如果试验本身无法建立可确认的每书时长测量，宁可停止，不为了“通过测试”放宽为只验总量。

如果 60 秒试验明确计入该条真实 60 分钟纸质阅读记录，则后续该条记录最多只剩 `3600 - verified_seconds` 秒。**不要**先写 1 分钟再另写完整 60 分钟造成重复。

验收报告样式：

```text
Entry ID: ...
微信读书匹配书籍: 安娜·卡列尼娜 | 作者... | bookId=...（ID 可适度遮盖）
纸质书日期: 2026-10-22
纸质阅读预算: 3600 秒
真实测试额度: 60 秒
官方单书读取: B0=...，B1=...，delta=... 秒
官方月度总量: T0=...，T1=...，delta=... 秒
客户端人工复核: 通过 / 不通过 / 尚未复核
其它账号阅读活动: 无 / 有（则归属证据不足）
结论: VERIFIED / UNCERTAIN / BLOCKED
本地已核验秒数: ...
本地剩余额度: ...
网络重试次数: 0（试验期间失败不自动重发）
敏感信息扫描: PASS/FAIL
```

## H. 逐阶段实施与测试项（每阶段完成后交付报告）

### 阶段 0 — 仓库审计与安全隔离

任务：克隆并锁定 upstream commit；分析 `api_reader.py` 的写入函数、凭证管理、书籍检索；确认官方读接口；关闭自动调度/守护进程及原框架的自动降级；审计所有日志，禁止输出凭证。

- [ ] `schedule.enabled=false`、`daemon.enabled=false`；程序启动、重启后均不会产生后台写入。
- [ ] 没有 `--live --confirm` 时，Mock HTTP 监视器确认写请求数为 0。
- [ ] 错误日志、控制台、`reports/` 中不含 `wr_skey` / `wr_vid` / `WEREAD_API_KEY` 值。
- [ ] 记录 fork commit、依赖锁文件及上游接口差异。

### 阶段 1 — 数据模型和书籍匹配

任务：SQLite 迁移；输入校验；`search/bind/add/list/export`；按译本人工绑定 `bookId`；独立 UUID 与近似重复警告。

- [ ] 示例 `2026-10-22`、`60min` 保存为 `3600sec`，日期未更改。
- [ ] 相同 `entry_id` 导入两次只保存 1 条；相同属性但不同 UUID 可经明确确认保存两次。
- [ ] 多个搜索结果不自动挑第一项；版本无法判断时不能绑定。
- [ ] 非法输入：0、负分钟、小数、未来日期、空书名；提供清晰错误信息（如支持小数需明确定义取整规则）。
- [ ] SQLite 写入事务失败不产生半条记录。

### 阶段 2 — 远端统计只读适配器

任务：本地读取官方 API Key；调用 `/store/search`、`/book/getprogress`、`/readdata/detail`，实现 `baseline`；Mock 读接口及服务端响应规范。

- [ ] 只读基线返回官方数据；保留服务器回包和抓取时间，且单位均为秒。
- [ ] 支持无登录、过期 Key、403、限流、断网、响应缺字段。
- [ ] `GET /statistics` 本地历史绝不用于远端核验。
- [ ] 书籍不进 `readLongest` 前十或小于 5 分钟时，不误判其不存在。
- [ ] 通过微信读书 App 对比，确认书籍字段的实际口径；不一致则标记 `BLOCKED`。

### 阶段 3 — 有界写入适配器 + Mock 验证

任务：基于 WeReadGears 提取可控制的固定书籍写入能力；禁止随机切书/自动浏览器兜底；实现写前事务、取消、停止、状态持久化。

- [ ] 传入 `book_id=A`，Mock 捕捉到所有写请求均归 A，不涉及 B。
- [ ] 缺少 `--live --confirm`，写请求数 0；运行配置不允许自主发送第二个会话。
- [ ] 请求返回 HTTP 200/succ=1 时只转 `AWAITING_VERIFICATION`，而非 VERIFIED。
- [ ] 网络超时即 `UNCERTAIN`，程序重启也不自动重发。
- [ ] 用户中断能停止新写请求，保留待核验信息。
- [ ] `verified_seconds` 不超过 `duration_seconds`。

### 阶段 4 — 首次真实 60 秒试验（人工批准）

任务：严格执行 §G 的 L0–L6；输出 `reports/live-probe.md`，脱敏证据；失败时不继续真实写入。

- [ ] 能明确分辨“总量增、单书不增”。
- [ ] 能明确分辨“API succ=1、远端没变化”。
- [ ] 对统计延迟/并发阅读/身份错配不误判。
- [ ] 仅双重归属证据充分时标记试验 VERIFIED。
- [ ] 未经用户再次确认不会继续 59 分钟额度。

### 阶段 5 — 余额提交、恢复和最终验收

任务：仅在阶段 4 成功后实现 remaining 提交；每批提交后核对；恢复流程；导出审计报表。

- [ ] 同条记录试验入账 60 秒后，待同步额度为 3540 秒（以实际经核验值为准）。
- [ ] 再次点击同一已完成条目，不产生任何新增写请求。
- [ ] 超时断网后 `reconcile` 不重发写请求；不能确认则保持 UNCERTAIN。
- [ ] 错误同步到其他 bookId 时停止且记录为重大失败，不继续冲量。
- [ ] 真正完成时：用户指定的纸质书累计核验额度达到目标，且账户总时长的变化证据可解释。
- [ ] 批量导入记录也默认本地保存，不触发任何远端写入。

### 阶段 6（可选）— 本地 Web UI

仅阶段 5 验收后考虑，采用 FastAPI + Jinja2/简单前端，监听 `127.0.0.1`（不要 `0.0.0.0`），显示书籍选择、记录表、预览、明确确认按钮、核验结果和错账提示。必须保留所有 CLI 安全限制。

## I. 失败分类与处理表

| 情形 | 状态 | 行为 |
| --- | --- | --- |
| 未登录、Key 过期 | `BLOCKED` | 提示本地重新授权；保留记录；无写入 |
| 找不到正确版本/章节 | `BLOCKED` | 要求人工确认；不猜 `bookId/chapterUid` |
| POST 200 或 succ=1，但远端单书不增加 | `UNCERTAIN` / `FAILED_CONFIRMED` | 停止，复查统计口径、章节身份；不可自动追加 |
| POST 已发后 timeout / 断网 | `UNCERTAIN` | 只读对账，禁止无条件重试 |
| 远端仅总量增加 | `UNCERTAIN` | 不归属指定书，不标记完成 |
| 远端统计持续延迟 | `AWAITING_VERIFICATION` | 有界只读轮询，再提示手工复核；不重复发送 |
| 增量远高于已授权额度 | `UNCERTAIN` | 检查并发及外部干扰，停止 |
| 进程崩溃后重启 | 保持已有状态 | 先对账再人工决定下一步 |
| 重复导入同一 UUID | 返回既有条目 | 不创建新的同步任务 |
| 非公开 API 变更/被风控 | `BLOCKED` | 不规避限制，保留本地纸质书阅读记录 |

## J. 面向用户的最低验收标准

用户完成上述步骤后，必须可以在本地终端回答四个问题：

1. 我在 `2026-10-22` 读了哪本纸质书、哪个译本、多少分钟？
2. 微信读书对应的 `bookId` 是哪个，是否由我亲自确认？
3. 实际服务器上该本书的阅读时间增加了多少秒，账号月度总时间增加了多少秒？这些证据分别来自哪个接口、何时采集？
4. 现在的状态是已完成、部分完成、未核验还是失败；有没有可能重复提交，应该如何安全恢复？

**终止规则**：无有效每书核验证据即停在“纸质书本地记录 + 官方统计只读查询”，不发布写入功能为稳定正式版。此时它仍然是一个有用的纸质书记录工具，只是**不能声称已同步到微信读书**。

---

### 给 Codex 的最终提示

从阶段 0 开始，逐阶段实现，先运行 `pytest` 再交付每阶段报告。不要为通过验收而伪造测试数据、伪造成功截图、宽松解释接口字段或偷偷跳过真人授权。需要用户扫码、提供本地 API Key 或确认真实试验时，只告知用户在自己的设备上完成，不得要求将任何凭证粘贴到聊天里。任何真实账号操作，优先保护现有阅读记录和账号安全。
