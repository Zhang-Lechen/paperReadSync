# 数据模型与恢复

数据库使用 SQLite、WAL、FULL synchronous、外键、整秒 CHECK 约束。迁移版本保存在
`PRAGMA user_version`，当前为 3；版本 1、2 无损迁移到 3，更高版本直接阻断。修改以 `BEGIN IMMEDIATE` 为边界。

`metadata.backend` 绑定为 real 或 mock，不能在同一文件间切换。默认分别为
`data/ledger.sqlite` 与 `data/mock.sqlite`。本地账户别名分隔记录，并不等于已经证明远端账号相同。

| 表 | 用途 |
| --- | --- |
| books | 版本文档、人工确认；已绑定版本不能静默修改 |
| removed_books | 按账号保存从选书列表移除的书籍及时间；可恢复，保留原版本、账本及核验证据 |
| candidates | 搜索结果缓存，不代表用户已选择 |
| book_routes | 账户、官方 bookId、可信 readerLink、网页路由 ID、采集时间和凭证摘要；与具体书籍上下文一致才可写入 |
| reading_entries | UUID、账户、纸质日期/时区/会话、整数额度、已核验额度、状态及不可变输入摘要 |
| sync_attempts | 独立 UUID、attempt_no、额度、写前/后单书与周期读数、传输和核验状态、请求摘要 |
| remote_snapshots | 每次成对测量的采集时间、单书值和成对证据 JSON；JSON 同时保留月度总量及周期边界 |
| attestations | 对指定基线与本地读写凭证摘要的人工核对，不保存原始凭证 |
| write_guard | 同一数据库只能有一个尚未解决的写意图 |
| audit_events | 本地状态事件及脱敏证据 |

写意图、额度检查、序号与状态在同一事务中提交，之后才允许网络发送。授权请求摘要只包含
entry/attempt/book/seconds/purpose；不包含 Cookie、签名或 API Key。

`baseline` 在全局锁中区分新的写前快照与恢复观察。有未决试验时仍可查询当前时长，
但保存为 diagnostic/observation，不覆盖原写前读数、人工核对或尝试，不赋予新的写入许可。
页面对这种预期状态显示时长及恢复入口，而非将其当作读取失败。

写前人工核对区分 `review_method=numeric`（CLI/旧表单的独立输入）和
`review_method=display_confirmation`（本人对照手机确认页面显示的值）。后者不保存伪造的 App 输入，
只记录显示过的官方快照及本人明确确认的事实；勾选默认不选。确认和创建意图前均重新查询，
只有显示值与新测量一致才继续。输入错误和漏确认会精确说明原因，不再一律把记录标为 BLOCKED。

2026-11-05 增加 `review_method=direct_sync_authorization`：本人确认实际阅读、书籍、额度、账号及活动暂停，
程序自动采集写前值；此方法明确 `app_reviewed_by_user=false`，不声称本人核对或输入了手机数字。
`remaining` 不再要求先有已核验的 probe，直接同步中的首批也自动核验。

当前 Windows 用户范围的 OS 文件锁串行化本项目各账户与数据库；其他设备/Windows 用户及客户端需本人暂停。
首次发送前另写本地 pending 意图文件，记录数据库路径和
attempt ID。进程崩溃时文件锁由 OS 释放，但 pending 文件与数据库状态仍阻止新写入。
对账不会调用写适配器。原数据库对账完成后清除 pending；若提交成功但删除标记前崩溃，
再次 reconcile 可只读清理已完成标记。它不能保证远端 exactly-once。

相同 entry UUID 与内容返回原记录；相同 ID 不同内容报 ID_CONFLICT。同书/账户/日期/时长
近似重复只提醒，明确 --new-session 可保存另一个 UUID。批量导入整体事务失败则全部回滚。

传输成功只转 AWAITING_VERIFICATION；超时/模糊响应转 UNCERTAIN。独立双读数必须来源、账户、
书籍、周期和凭证摘要一致，增量相同且不超过批准和实测会话秒数。首次试验还要求客户端读数、
暂停其他活动及自然日复核。没有变化保持待核验；仅总量增加、不一致或过量增量不归属。

小于批准额度的双重增量可记为 PARTIAL，但保留写意图，防止尚未刷新的尾部再次提交；
后续对账仅追加差额。没有真实样本时不放宽阈值，也不提供强制解锁/强制成功命令。
若长期只能证明部分额度，应停止并人工检查服务器口径，再决定是否需要调整适配器。

Mock 可走完整状态流，但最终入口状态专用 MOCK_PARTIALLY_VERIFIED / MOCK_VERIFIED，
审计明确 source=mock，独立文件不能混入真实账本。真实 VERIFIED 必须由 official 双统计核验产生；
可选 probe 仍有客户端复核，直接批量自动核验另记 `automatic_verification=true`、`client_confirmed=false`。
HTTP succ 和本地 history 不具有核验效力。

凭证在 Windows 当前用户 DPAPI 加密、私有 ACL 的 `%LOCALAPPDATA%/PaperReadSync/secrets/`
中保存。只读 API Key 优先从本机环境读取，否则读取分账户 DPAPI 存储，不写入数据库、审计或日志。
浏览器 storage state 与每书阅读上下文分别加密保存；网页路由 ID 不等同于官方 bookId。
路由只能来自该书官方 readerLink，捕获的页面、referer 和 payload 必须对应；重新登录后须重新准备。
未决写入时禁止切换账户或修改原书路由；同一账号可以准备其他书的独立上下文，不解除写入阻断。
持有同一个全局锁时可本人重新扫码修复同账号会话，
并仅更新同账号同路由的凭证，原试验和未决意图保持不变。新扫码必须通过官方只读会话校验。
原 JSON 文件由用户自行保管；
导入不会自动删除它。DPAPI 用户范围加密不支持直接复制到另一台机器或另一 Windows 用户。

真实写入在创建意图前先只读 GET `/web/getConfig` 验证网页会话；失效不会消耗一次试验。
凭证模型保存同源完整 Cookie（DPAPI 中每个值仍为私密字段）；签名配置从当前有效会话读取，
不把动态配置或凭证回显。四个已提取上游纯协议方法保持原样。

本人可在本机显式确认把尚无目标书增量的旧小额试验转为 `WITHDRAWN_UNVERIFIED`。
这不会把它认定成功或认定远端失败，而是将其**全部批准额度保留**，从可提交额度中扣除。
原始请求、响应摘要、快照和审计均保留，其余尚未批准的实际阅读额度可走直接同步或可选试验。
`verified_seconds + reserved_seconds + available_seconds = duration_seconds`，不会自动释放保留额度。
存在保留额度时不能把整条记录标为 VERIFIED；对账不会将旧额度重新变为可写余额。

网页支持本人明确接受重复累计后重新提交历史保留额度。释放保留额度、新基线和确认在同一事务中保存，旧尝试继续保留 WITHDRAWN_UNVERIFIED 与原始读数，通过 result_json.reservation_released_for_retry 标记额度已用于重新提交。重复或过期预览会复用原任务或被拒绝。释放后中断时，余额可继续使用正常同步入口处理。

网页直接同步和重新提交允许每批最多 5 秒、短批次约 10% 的统计误差。核验要求单书与月度增量均为正、接近批准时长且相互接近，按批准量入本地账本，实际增量保留在结果中。传输结果不明、计数未变化、账号或周期不一致继续保留异常。高级手工对账及 CLI 默认使用原来的严格核验。

直接同步还支持本人明确预览后搁置已变化的旧写入：保留 `max(requested_seconds, observed_book_delta)`，
跨旧/新字段口径时仅保留整个旧批准额。原 `requested_seconds`、原始快照和 `credited_seconds` 不变；
实际保留值记在旧 `result_json.reserved_seconds`，数据库汇总优先读取它，旧数据缺此键则使用原批准额。
旧笔继续标记 `WITHDRAWN_UNVERIFIED`。新查询导致所需保留值变化则更新观察并要求刷新预览，未发送新阅读请求。
不允许跨账号/周期、负向计数或已部分归属的旧笔用此路径跳过检查。

同账号切换到另一条记录时，直接同步预览仍可携带原条目的待查保留操作。
预览令牌绑定当前提交条目及原 attempt/entry/保留秒数/书名；本人明确确认后，
从原条目扣除待查保留，再为当前条目采集新基线。原条目已核验秒数不变，
当前条目不承担其他书的保留额度。账号或原意图变化则停止，不发送新书请求。
