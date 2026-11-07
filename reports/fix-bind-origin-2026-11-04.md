# 修复确认绑定后的“不允许的来源”

日期：2026-11-04（Asia/Shanghai）。

## 原因与修复

原响应头为 `Referrer-Policy: no-referrer`。在实际 Chromium 浏览器中，原生表单 POST 导航
因此携带 `Origin: null`，触发 `web.py` 对来源的精确校验，在绑定逻辑之前返回 403。
此前内存 HTTP 测试没有模拟浏览器根据响应策略生成 Origin 的行为。

现将策略改为 `same-origin`，保留同源表单的正确 Origin，同时不给其他站点发送 Referer。
Host 精确匹配、Origin 精确匹配、CSRF 令牌与 CSP 继续保留；未将 `null` 或任意来源加入白名单。

改动文件：`paperread/web.py`、`tests/test_web.py`。

## 验证

- 修改前在独立 Mock 数据库、127.0.0.1:8766 实际点击表单，复现 403“不允许的来源”；服务端观测 Origin=null。
- 修改后重启测试服务，回首页重新加载并点击相同表单，浏览器返回首页，服务器观测 Origin=http://127.0.0.1:8766。
- 新绑定的《战争与和平》在右侧“已确认的书籍”下拉框出现并可选择；截图 `bind-origin-fixed-2026-11-04.png`。
- `python -m pytest tests/test_web.py -q`：10 passed。
- `python -m pytest -q`：144 passed。
- 回归测试覆盖同源绑定/303 跳转、页面策略，以及正确令牌下仍拒绝 null、其他站点、其他端口和不同主机；正确来源不能替代 CSRF。

已核对进程命令行后重启用户原 8765 Mock 服务，以相同默认数据库加载修复。没有改动或删除用户账本。
独立测试数据库位于忽略目录 `data/origin-regression.sqlite`；测试没有调用微信读书或操作真实凭证。
测试 8766 服务验证后关闭，8765 用户服务继续运行。

用户需从 http://127.0.0.1:8765/ 首页强制刷新（Ctrl+F5），取得新的页面策略与表单令牌，再重新提交。
原先停在 /action/book 的拒绝页面不会自动恢复表单；被拒绝的那次提交没有执行绑定。
