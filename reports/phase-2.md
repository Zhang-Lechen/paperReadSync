# 阶段 2：官方只读统计适配器

2026-10-23。软件实现与 Mock 接口验证完成；真实 App 字段口径仍未核对。

文件：`credentials.py`、`official_stats.py`、`mock_adapter.py`、`sync_service.py` 的基线/人工确认部分、CLI baseline/attest。
官方版本从锁定 SKILL.md 读取并核对 1.0.5；Gateway 参数平铺，api_name 白名单只含搜索、书籍详情、进度和月度统计。
不会使用本地 statistics、截断桶合计或 readLongest 代替单书/总量。

实际运行：`python -m pytest tests/test_official_stats.py -q`，**17 passed**。
覆盖未授权、过期/403、429、500/302、不合法 JSON、缺字段、布尔/负秒数、断网、不重试、升级信号、
白名单阻断其他操作、scope=17 搜索、详情译者、月度边界、单书与月度读数及认证字段不进入快照。

基线保留采集时间、服务器时间与白名单字段。Key 只从环境读取，错误只输出固定错误码。
写入凭证支持本机路径导入与 Windows DPAPI/ACL 加密；合成凭证加密回读测试通过，不代表真实登录有效。

未解决：账号同一性与 book.recordReadingTime 的实际口径必须由本人对照 App；文档不足以自动证明。
缺少单书 API 读数时直接 BLOCKED，尚未提供人工替代单书 API 字段的快照模式。真实接口样本 NOT_RUN。
