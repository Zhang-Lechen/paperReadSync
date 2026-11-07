# 阶段 6（可选）：本地 Web 界面

2026-10-23。实现了可选 FastAPI + Jinja2 本地账本与 Mock 流程预览；真实模式继续受阶段 5 门槛限制。

文件：`web.py`、`templates/index.html`、`static/app.css`、CLI serve、Web 可选依赖与 `test_web.py`。
只监听 127.0.0.1；不提供 0.0.0.0 入口。默认 GET 仅显示本地数据，不查询远端或启动阅读。
展示版本绑定、实际纸质日期、分钟、已核验/余额、状态和合成试验流程按钮。

实际运行：`python -m pytest tests/test_web.py -q`，**4 passed**。覆盖表单流程、Host/Origin、CSRF、HTML 转义、
只绑定回环接口和真实阶段门槛。Windows 异步事件循环内部 socketpair 被测试专门允许，任意其他外部/回环连接仍被拒绝。
前端采用 CSP、禁止嵌入、no-store；没有浏览器账号凭证表单。

另实际启动了 `python -m paperread --mock --db data/ui-preview.sqlite serve --port 8765`，
在内置浏览器检查空状态与版本表单布局，预览截图 `ui-mock-preview.png`；检查后已停止服务器。

真实模式只有当前账户至少一条完整记录达到真实 VERIFIED 才可启动。真实写入和完整核验保留在 CLI，
不通过网页绕开其参数与精确交互确认。网页写入按钮仅用于 Mock；真实模式仅新增本地记录。
因此本阶段是可选 UI 的本地预览交付，不是阶段 5 真实成功后的正式 UI 验收结论。
