# 0.2.0 集成记录（2026-11-04）

> 后续真实试验已返回 WRITE_RESPONSE，目标书增量为 0；当前保持 UNCERTAIN，
> 最新进展见 [排查报告](write-response-2026-11-04.md)。以下为首次真实试验前的集成记录。

已补齐真实网页与 CLI 的扫码、书籍上下文准备、官方基线、App 核对、小额试验、独立核验、余额和取消。
用户要求最后统一验收，开发不再逐阶段等待。真实时长入账仍须本人在本机批准并以服务器及 App 证明。

## 实现

- 真正克隆并锁定 WeReadGears `a3ab77bd767c73e6cca367e6ea95b4d9e98d465c` 与
  WeChatReading `d98e66ee60a8c7bcd35da549d0643c4470439643`，保留隔离补丁与许可。
  四个上游纯协议方法按原实现提取，AST 测试检查来源；没有启动原框架的自动阅读、调度或重试。
- `browser_session.py` 使用独立可见 Edge/Chrome 扫码，DPAPI 保存授权和每书上下文。
  授权及准备期间所有阅读计时请求在发送前拦截，禁止自动翻页、服务工作线程和 WebSocket。
- `book_routes.py`、schema 2 的 `book_routes` 与适配器区分官方 bookId 和网页 ID。
  readerLink、页面、payload、referer 和凭证摘要必须相符，未知映射不猜测。
- `credentials.py`、`official_stats.py` 支持分账户 API Key 的 DPAPI 加密存储与环境变量优先读取。
- `jobs.py`、`web_operations.py`、`web.py` 与模板接入有界后台任务、幂等提交、取消和完整真实流程。
  写操作要求一次性预览、明确额度和本机事实声明，仍复用原有事务/锁/未决恢复/独立核验。
- App 可按秒或整分钟展示；分钟精度仅用于人工显示比对，不改变 API 双增量与额度要求。
  保留用户已有的 Referrer-Policy=same-origin 修复。

## 检查与真实前置

最终执行 `.venv\Scripts\python scripts/check_project.py`：174 passed in 10.91s；compileall、
doctor（0.2.0）、pip check、完整 Mock CLI 演示和 Git 可见文本敏感格式扫描均通过。
最终统一检查命令为 `.venv\Scripts\python scripts/check_project.py`，原始输出见 `final-evidence.json`。
该脚本只运行本地/合成检查，不能证明真实入账。

已使用官方只读接口匹配《十八家诗钞》，保存用户提供的 2026-11-04、60 分钟记录；
用户在本机扫码成功，授权阶段阅读转发/拦截为 0/0；准备本书成功，阅读转发/拦截为 0/2。
重启新版真实网页后，通过页面只读按钮取得 BASELINE_SAVED，单书 0 秒、月度 11409 秒，
采集时间 2026-11-04T09:29:00.589425+00:00；尚待本人 App 复核，写前须在 120 秒有效期内重新确认。
当前数据库仅一条该书记录，verified_seconds=0，写尝试和未决写意图均为 0。
密钥、Cookie、storage state、payload 和完整认证头未写入报告。

## 尚待真实证据

本人核对 App 同账号、单书/月度时长、暂停其他阅读及自然日，并在新鲜基线后批准首次最多 60 秒。
不得代填 App 读数或代勾事实确认。试验后独立双增量与 App 复核通过，才可归属并开放余额。
若只增加总量、无变化、超额、网络结果不确定或服务端限制，即停止，只读对账，不自动重发。
详细实时进展见 `live-probe.md`；尚无“真实同步成功”的结论。
