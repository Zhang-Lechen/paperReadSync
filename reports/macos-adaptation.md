# macOS 适配验证（2026-11-14）

环境：macOS / Apple Silicon（arm64），Python 3.11.18。

## 修改

- `start.command` 支持 Finder 双击、任意工作目录、带空格路径和启动参数；使用本机 `.venv/bin/python`。
- POSIX 启动子进程使用独立会话，由启动器转发 Ctrl+C，避免重复中断。
- macOS 的 API Key、浏览器会话及每本书写入凭证使用系统登录钥匙串；通过原生 Security Framework 调用，不把秘密传给子进程参数或写入明文文件。Windows 保持 DPAPI / ACL。
- macOS 配置目录遵循 `~/Library/Application Support/PaperReadSync`；按目录、账号和书目隔离钥匙串项目。
- 自动检测 `/Applications` 下的 Chrome / Edge，未安装时使用 Playwright Chromium；保留显式浏览器选择。
- 修复两处 Python 3.12 专用 f-string 引号，协议方法 AST 对照测试通过。
- 原锁文件的 pyee 14 要求 Python 3.12。重新解析跨平台锁文件，使用 Playwright 1.65.2（不再依赖 pyee），保留 Windows 条件依赖。
- 更新 macOS 安装、授权迁移和停止服务说明；网页不再只显示 Windows 授权提示。

## 验证

- 完整 pytest：301 passed，6 skipped（仅 Windows 原生 DPAPI / ACL 测试）。
- macOS Chrome：实际运行两项离线网页导航与桌面/移动布局测试，均通过；使用临时浏览器配置和 ASGI 路由，不访问真实账号。
- 原生钥匙串：临时独立钥匙串内用合成数据完成不存在查询、保存、读取（含 NUL / UTF-8）、更新和再次读取；完成后删除临时钥匙串，未访问用户已有授权。
- 实际本地 HTTP：临时 Mock 账本、随机回环端口，启动、`/health`、首页 200、SIGINT 优雅退出均通过。
- `pip check`、`compileall`、`paperread doctor` 通过；两个锁定上游均 PASS。
- Mock 演示达到 MOCK_VERIFIED，重复提交没有新增网络写入。

真实扫码与阅读同步未执行；Intel Mac 与 Windows 未在本轮实际运行。Windows 原生凭证测试保留，需在 Windows 执行。自动化测试使用内存钥匙串替身，隔离用户授权。
