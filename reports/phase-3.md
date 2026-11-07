# 阶段 3：有界写适配器与状态机

2026-10-23。软件与内存 HTTP / Mock 状态机测试通过；原上游 runtime 继续隔离。

文件：`upstream_protocol.py`、`wereadgear_adapter.py`、`file_lock.py`、`sync_service.py`、`verifier.py`、CLI probe/verify/reconcile。
协议四个方法直接从锁定 commit 提取，MIT 许可保存在 `third_party/`；AST 测试确认方法未改动，没有重新发明签名。
不调用原 start_reading，不继承其启动减 30 秒、随机切书、自动续期、重试、抓章节或浏览器回退。

运行命令：`python -m pytest -q`，最终集成结果记录于 `final-evidence.json`。
实际定向有界协议测试捕捉同一本书/同一章节、当前时间、实测 60 秒、累计 rt=60；超时/403/429/空响应/succ=0
均只发一次即停止。缺参数、错误书籍、CI 无真实请求。Ctrl+C 首次请求前取消，发出后视为未决。

写前 SQLite 事务保存 UUID 意图、额度、基线、请求摘要与唯一序号；OS 文件锁和跨数据库 pending 文件约束本机单写会话。
重启不触发重发，verify/reconcile 只读取远端统计，逐次归属差额。HTTP 200/succ=1 只进入待核验。
本次实际演示与全部自动化测试均使用明确标记的合成数据；未加载真实账号凭证或发真实阅读请求。

未解决：真实服务器的章节/心跳兼容性、字段口径与计时规则仍需阶段 4 本机试验。
现有扫码入口没有复用，用户需自行在设备准备合法授权的上下文；缺失则停止。
