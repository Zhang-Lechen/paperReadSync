# 阶段 0：仓库审计与安全隔离

后续说明：用户已改为全部阶段统一验收，阶段 0 的等待点已取消。以下为当时的历史交付记录；当前总体状态见 `FINAL_REVIEW.md`。

日期：2026-10-23，Asia/Shanghai。

**结论：阶段 0 本地检查通过，等待用户验收。真实账号核验 NOT_RUN。**
按实施计划 §A.9「每完成一个阶段……并等待用户验收，再进入下一阶段」，本轮停在此处，
未开始阶段 1，未开放真实写入参数。

## 交付内容和改动文件

| 文件 | 实现 |
| --- | --- |
| `pyproject.toml`、`requirements.lock` | 可安装 Python 包、Typer 命令、运行/测试依赖完整版本及包哈希锁 |
| `.gitignore`、`.gitattributes`、`.env.example` | 排除凭证和运行数据；保留补丁原始行尾及依赖副本字节；仅提供环境变量名 |
| `config.toml` | offline；调度/守护/自动重试/浏览器回退/真实写入全部关闭 |
| `paperread/__init__.py`、`paperread/__main__.py`、`paperread/cli.py` | 安装后的 `paperread doctor` 与 `python -m paperread doctor`；没有登录/读写账号入口 |
| `paperread/safety.py` | 缺配置、未知键、不安全值均阻断；所有 live/confirm 参数组合都不开放阶段 0 写入 |
| `paperread/network.py` | 默认传输层拒绝所有 HTTP 方法、主机及 loopback，不创建真实网络传输 |
| `paperread/redaction.py` | 敏感字典键、嵌套容器、带标记字符串清洗；格式化后覆盖异常堆栈 |
| `paperread/upstream.py`、`upstream.lock.json` | 验证两份真实 commit、工作树补丁 SHA-256 和非忽略的未知文件 |
| `patches/weread-gears-quarantine.patch` | 原入口/包整体隔离，关闭配置默认值，删除原始诊断和 credential user_info 回显，关闭上游全部日志 |
| `locks/weread-gears-requirements.txt` | 锁定上游原有依赖清单副本，未安装上游框架依赖 |
| `scripts/bootstrap_upstream.py` | 克隆固定 commit、校验并应用补丁；重复执行不覆盖现有修改 |
| `scripts/check_phase0.py` | 运行离线验收检查，保存脱敏结果 |
| `tests/conftest.py` | 所有测试禁用外部 socket 连接 |
| `tests/test_safety.py` | 参数守卫、HTTP 阻断、配置校验、启动/重启、源码 lock 异常测试 |
| `tests/test_redaction.py` | 凭证键、部分 token、格式化参数、异常和未知对象的合成数据测试 |
| `tests/test_upstream.py` | commit/补丁、原入口、空日志、幂等准备及从干净本地副本重现补丁 |
| `tests/test_repository_hygiene.py` | 用临时 Git 仓库验证凭证/数据忽略规则以及样例和源码可追踪 |
| `README.md`、`docs/RESEARCH.md` | 安装运行方式、逐接口审计、字段口径差异、未实现功能和边界 |
| `reports/phase-0-log-inventory.json` | 原上游 470 个静态日志调用位置，379 个动态消息调用；无消息值 |
| `reports/phase-0-evidence.json` | 实际检查命令、退出码、脱敏输出和带 UTC+08:00 的采集时间 |
| `reports/phase-0-secret-scan.json` | 本次可交付文本的凭证格式检查结果和局限 |

本地初始化了项目 Git 仓库 `main`，未创建提交、远端 fork、推送或 PR。
原实施计划文件保持原样；`vendor/` 和 `.venv/` 是已准备好的本地忽略目录。

## 上游版本与审计证据

实际执行 `git -C <目录> rev-parse HEAD`：

```text
WeReadGears  a3ab77bd767c73e6cca367e6ea95b4d9e98d465c
WeChatReading d98e66ee60a8c7bcd35da549d0643c4470439643
official skill_version 1.0.5
```

本地 fork 改动通过可复现补丁记录，补丁摘要在 `upstream.lock.json`。
`doctor` 同时验证 HEAD 和工作树 diff，不能只看 HEAD 判断本地副本安全。

已确认写入 URL、`b/c/sm/co/pr/ct/ts/rt` 字段、传输成功判断、随机选书、Cookie 续期、
自动重试、main/Web 双回退路径、搜索/书架实现、本地 statistics、明文凭证保存及日志风险。
官方单书和周期统计仅为文档确认，没有账号返回样本；详见 `docs/RESEARCH.md`。

## 实际运行命令与结果

```powershell
git clone https://github.com/GaviZhao/WeReadGears.git vendor/WeReadGears
git clone https://github.com/Tencent/WeChatReading.git vendor/WeChatReading
python -m venv .venv
.venv\Scripts\python -m pip install pip-tools==7.5.1
.venv\Scripts\python -m piptools compile --extra test --generate-hashes --strip-extras --no-emit-index-url --output-file requirements.lock pyproject.toml
.venv\Scripts\python -m pip install --require-hashes -r requirements.lock
.venv\Scripts\python -m pip install --no-deps -e .
git init -b main
.venv\Scripts\python scripts/check_phase0.py
```

源码下载与包安装访问公开网络；没有请求微信读书账号接口。
源码修改后的可复现准备方式是 `python scripts/bootstrap_upstream.py`。

最终检查（Python 3.12.4 / Windows）：

| 检查 | 结果 |
| --- | --- |
| `python -m pytest -q` | **53 passed** |
| `python -m paperread doctor` | exit 0；offline；两份上游 PASS；remote_verification=NOT_RUN |
| `python scripts/bootstrap_upstream.py` | exit 0；两份 pinned and isolated；重复执行无改动 |
| `python -m pip check` | exit 0；No broken requirements found |
| CLI `paperread --help` 与安装后的 `paperread doctor` | 手工命令检查通过，仅提供 doctor |
| 合成敏感值控制台/文件日志测试 | PASS；上游空日志不创建文件；本项目 formatter 不泄露测试值 |
| 文本凭证格式扫描 | PASS；详见单独扫描结果，未读取任何真实凭证 |

测试重点：

- 启动两次诊断，不发 HTTP 请求、不创建后台数据文件；设置合成环境 Key 不改变离线行为。
- Mock 写请求监视计数为 **0**，覆盖全部四种 live/confirm 组合；没有真实网络写入测试。
- 传输层拒绝原阅读 POST、官方 Gateway POST、GET 和本地 PUT；自动化测试禁止外部 socket。
- 配置重新读取时仍要求 schedule=false 和 daemon=false；拒绝 true、字符串 false、整数 0、未知键和缺失项。
- 原始 `src.main` 导入与直接脚本执行均在副作用及第三方依赖导入前被拒绝。
- 通过本地 Git 仓库克隆干净 commit 后成功应用同一补丁，第二次执行保持幂等；未知修改被保留并阻断。
- 未配置读接口、没有读取本地 `/statistics`，没有把 HTTP 200 或 succ=1 当作同步证明。

开发中发现并修复的检查问题：

1. Windows 文本 stdin 给 Git `check-ignore` 传入 CRLF，测试把 `\r` 当成路径字符；改为 NUL 分隔的字节协议，最终通过。
2. 上游 `api_reader.py` 原 commit 使用 CRLF，其他文件使用 LF；保留原 blob 行尾并在新副本关闭 autocrlf，避免整文件换行差异和补丁不可复现。
3. 一次报告时间戳采集使用 `ZoneInfo`，因 Windows 当前缺少 tzdata 失败；报告改为显式 UTC+08:00。未影响 CLI；阶段 1 需加入时区数据库支持。

## 阶段 0 验收项

- [x] schedule=false、daemon=false；本项目不启动原框架，启动/重启均无后台写入。
- [x] 没有 `--live --confirm` 时 Mock 写请求数为 0；本阶段带齐参数也拒绝写入。
- [x] 原始敏感日志在登录前关闭；合成凭证不会进入控制台、日志文件或生成的证据报告。
- [x] 记录真实 upstream commit、本地补丁、依赖锁及与原规格/依赖的差异。
- [x] 运行 pytest 并交付可执行代码和实际报告。
- [ ] 用户验收阶段 0。

## 未解决问题和下一阶段

1. 还没有 SQLite 记录、版本选择、搜索、去重、导出；验收后阶段 1 实现。
2. 真实凭证存储与 Windows ACL 未实现，本阶段不接收/导入凭证；忽略规则不能替代受保护存储。
3. 官方 Key、账号一致性、单书字段实际口径、客户端对照、同步归属日均未确认；保留为后续阻断条件。
4. 上游框架完全不可运行，未来只提取所需的签名/协议能力，必须重新审查有界会话；不能直接取消隔离并运行原 start_reading。
5. 未验证 Python 3.11、多版本 CI 或其他操作系统；上游没有完整传递依赖锁，其旧依赖尚未装入本环境。
6. 真实 60 秒试验、剩余 59 分钟和服务器成功状态均 **NOT_RUN / 未开放**。

本阶段没有“同步成功”的证据；只有通过独立单书及账户远端数据核验后，后续阶段才能记录已核验秒数。
