# hermes-update-check

[![CI](https://github.com/xinian5216/hermes-update-check/actions/workflows/ci.yml/badge.svg)](https://github.com/xinian5216/hermes-update-check/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/xinian5216/hermes-update-check)](https://github.com/xinian5216/hermes-update-check/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](pyproject.toml)
[![Tests: 538 offline](https://img.shields.io/badge/tests-538%20offline-brightgreen.svg)](AGENTS.md)

> **先检查 → 再评估 → 给建议 → 你确认 → 才更新。**
> 这个工具永远不会自己更新 Hermes。

最新发布：**v1.3.0**（[Release 页](https://github.com/xinian5216/hermes-update-check/releases/latest)附带 wheel 与 sdist，
可直接 `pip install` 安装，无需 git 与 curl 脚本）。

面向 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的「更新检测与风险评估」工具，用来避免因为盲目更新导致的
严重 Bug、配置损坏、Session 数据异常、Gateway 故障和兼容性问题。

设计原则：**宁可漏掉一次更新，也不要推荐一个可能有严重回归的新版本。**

## 快速开始（一键安装）

```bash
# Linux / macOS / WSL
curl -fsSL https://raw.githubusercontent.com/xinian5216/hermes-update-check/main/install.sh | bash
```

```powershell
# Windows PowerShell
irm https://raw.githubusercontent.com/xinian5216/hermes-update-check/main/install.ps1 | iex
```

装完直接 `hermes-update-check check` 看结论。想交给 AI 助手代劳？
[第二节「方式 B」](#方式-b把下面这段直接发给-hermes或任何-ai-助手让它自己装)
有一段可以直接复制发给 Hermes 的指令。安装脚本不用 `sudo`、不动 `HERMES_HOME`、**不执行 `hermes update`**。

---

## 目录

1. [它到底做什么](#它到底做什么)
2. [项目目录结构](#一项目目录结构)
3. [安装方式](#二安装方式)
4. [使用命令](#三使用命令)
5. [代码来源判定 Code Provenance](#四代码来源判定code-provenance)
6. [风险模型（组件化）](#五风险模型组件化)
7. [阻断规则与使用画像](#六阻断规则与使用画像第三阶段核心)
8. [回归聚类与可信度](#七回归聚类与可信度)
9. [回滚机制](#八回滚机制)
10. [Telegram / Webhook 通知配置](#九如何配置-telegram-通知)
11. [配置文件参考](#十配置文件参考)
12. [watch 模式与 cron](#十一watch-模式与-cron)
13. [JSON 输出契约](#十二json-输出契约)
14. [退出码](#十三退出码cron--ci-用)
15. [安全边界（明确说明）](#十四安全边界)
16. [隐私与密钥](#十六隐私与密钥公开仓库的三道防线)
17. [测试与开发](#十五测试与开发)
18. [代码索引与开发规范](#十七代码索引与开发规范给-ai-agent-用)

---

## 它到底做什么

| 步骤 | 命令 | 是否修改系统 |
|---|---|---|
| 采集本地环境（版本 / 安装方式 / git 状态） | `check` | 否 |
| 读取 GitHub Release、Release Notes、commit 数量、Issue | `check` `report` | 否 |
| 计算 0-100 风险分 + 自然语言建议 | `check` `report` | 否 |
| 定期检查、只在有变化时通知 | `watch` | 否（仅写状态文件） |
| 备份 → 更新 → 健康检查 | `update` | **是（需确认）** |
| 恢复到更新前的版本 | `rollback` | **是（需确认）** |

信息不足时它输出 **UNKNOWN / INSUFFICIENT DATA**，而不是 LOW RISK —— GitHub API 挂了不等于新版本安全。

---

## 一、项目目录结构

```
hermes-update-check/
├── pyproject.toml                 # 打包 / 依赖 / 入口点 / pytest 配置
├── config.example.yaml            # 带注释的完整配置示例（含所有可调参数）
├── install.sh / install.ps1       # 一键安装（Linux/macOS/WSL 与 Windows）
├── scripts/scan_secrets.py        # 发布前密钥/隐私扫描（pre-commit 钩子 + CI 都调它）
├── CHANGELOG.md                   # 版本变更记录
├── SECURITY.md                    # 安全策略：不存密钥、如何报告问题、仓库如何保持干净
├── AGENTS.md                      # 给 AI agent 的约定与开发循环
├── docs/CODE_MAP.md, index.json   # 自动生成的代码索引（scripts/build_index.py）
├── README.md
├── LICENSE
├── src/hermes_update_check/
│   ├── __init__.py                # 版本号
│   ├── __main__.py                # python -m hermes_update_check
│   ├── cli.py                     # 子命令、确认交互、退出码（唯一入口）
│   ├── config.py                  # 配置加载/校验/环境变量覆盖 + 路径解析
│   ├── console.py                 # rich ↔ 纯文本降级渲染
│   ├── i18n.py                    # 中英双语
│   ├── logging_setup.py           # 轮转日志
│   ├── util.py                    # 子进程执行（超时/流式）、时间、JSON、哈希
│   ├── errors.py                  # 异常类型 + 退出码常量
│   ├── http.py                    # 标准库 HTTP 客户端：超时/重试/缓存/限流识别
│   ├── github_api.py              # Release / Compare / Issue Search 解析
│   ├── versioning.py              # 双版本号体系（0.21.3 与 v2026.9.14）解析与比较
│   ├── local_env.py               # 当前版本、安装方式、git 状态、进程、Gateway
│   ├── provenance.py              # ★ 代码来源（channel / ahead / behind / detached）
│   ├── checker.py                 # 编排：把观测数据收集成一次完整的 UpdateCheck
│   ├── risk.py                    # ★ 风险引擎（关键词/年龄/规模 + 组件合成）
│   ├── clusters.py                # ★ 回归聚类：严重程度 × 独立性 × 可信度 + 功能归因/去重/相关性折扣
│   ├── usage_profile.py           # ★ 使用画像：功能与 Provider 的 critical/important/optional/unused
│   ├── impact.py                  # ★ Personal Impact / Core Feature Readiness / 系统级风险
│   ├── rollback_safety.py         # ★ 回滚路径探测（提交可达/状态可写/磁盘/venv/备份）
│   ├── overrides.py               # ★ 本地定制：登记表 / patch / 分类 / 冲突预测 / 应用
│   ├── gates.py                   # ★ 阻断规则（只剩五条）与提示规则
│   ├── advisor.py                 # ★ 最终裁决：系统级 → 关键工作流 → 回滚 → 年龄策略 → 可用性 → SAFE/ACCEPTABLE
│   ├── report.py                  # 人类可读报告 / Markdown / JSON
│   ├── preflight.py               # 更新前检查（只读）
│   ├── health.py                  # 更新后健康检查（7 项探针）
│   ├── updater.py                 # 备份快照 / 执行更新 / 回滚
│   ├── state.py                   # update_state.json、watch_state.json、缓存目录
│   └── notify/
│       ├── __init__.py            # 模块化通知注册表（加渠道 = 加一个文件）
│       ├── base.py                # Notifier 接口 + NotificationMessage
│       ├── telegram.py            # Telegram Bot（第一版即支持）
│       └── webhook.py             # 通用 JSON Webhook（Slack/n8n/自建）
└── tests/
    ├── conftest.py                # 假 GitHub 客户端 + fixtures（全部离线）
    ├── test_provenance.py         # ★ 五个 channel 场景、版本冲突、preferred_channel
    ├── test_clusters.py           # ★ 聚类严重度、同一作者/多作者、maintainer 确认
    ├── test_gates.py              # ★ 每条门禁、优先级、脏工作区压过低分
    ├── test_advisor.py            # ★ 裁决优先级、复查时间、UNKNOWN 不降风险
    ├── test_watch.py              # ★ 只对“有意义的变化”通知
    ├── test_versioning.py         # 版本解析/比较（含真实 hermes --version 输出）
    ├── test_risk.py               # 风险评分单元测试 + 场景测试
    ├── test_config.py             # 配置优先级、校验、hard_gates
    ├── test_github_api.py         # GitHub 响应解析、compare 截断、查询构造
    ├── test_http_cache.py         # 缓存 TTL / 过期降级 / 限流识别
    ├── test_state.py              # update_state.json 往返读写
    ├── test_checker.py            # 编排逻辑（注入假客户端）
    ├── test_report.py             # 报告渲染（中/英、Markdown、JSON 无标记）
    ├── test_preflight_health.py   # 更新前/后检查
    ├── test_updater.py            # 命令构造、快照、回滚计划
    └── test_cli.py                # 退出码、「未确认绝不更新」等安全属性
```

运行时目录（默认 `~/.hermes-update-check/`，可用 `paths.state_dir` 改）：

```
~/.hermes-update-check/
├── update_state.json    # 更新前记录（版本/commit/tag/branch/备份路径）→ 回滚依据
├── watch_state.json     # watch 模式上次看到的状态（用于「无变化不打扰」）
├── snapshots/<时间戳>/   # 更新前快照：config.yaml 副本 + state.db 一致副本 + 指纹清单
├── cache/               # GitHub 响应缓存（默认 6 小时 TTL）
├── reports/             # 可选：report --output 输出
└── logs/hermes-update-check.log
```

---

## 二、安装方式

需要 Python ≥ 3.9（Debian/Ubuntu 优先，macOS / WSL / Windows 同样可用）。

### 方式 A：一键安装脚本（推荐）

**Linux / macOS / WSL：**

```bash
curl -fsSL https://raw.githubusercontent.com/xinian5216/hermes-update-check/main/install.sh | bash
```

带参数（例如装到 `/opt`、跳过钩子）：

```bash
curl -fsSL https://raw.githubusercontent.com/xinian5216/hermes-update-check/main/install.sh   | bash -s -- --dir /opt/hermes-update-check --no-hook
```

**Windows（PowerShell 5.1+，不需要管理员）：**

```powershell
irm https://raw.githubusercontent.com/xinian5216/hermes-update-check/main/install.ps1 | iex
```

脚本会：克隆仓库 → 建**独立虚拟环境**（有 `uv` 用 `uv`，否则 `python -m venv`）→ 安装包 →
把 `hermes-update-check` 命令链接到 `~/.local/bin` → 启用提交前脱敏扫描钩子 → 跑一次自检。

脚本**不会**：用 `sudo`、改动 `HERMES_HOME`、执行 `hermes update`（安装脚本只负责安装；
升级 Hermes 永远是另一个需要你确认的动作）。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--dir PATH` / `-Dir PATH` | `~/projects/hermes-update-check` | 安装位置 |
| `--repo URL` | 本仓库 | 换成 fork 或本地路径 |
| `--branch NAME` | `main` | 分支 |
| `--bin-dir PATH` / `-BinDir PATH` | `~/.local/bin` | 命令链接位置 |
| `--no-hook` / `-NoHook` | 关闭 | 不启用 pre-commit 脱敏钩子 |

重复执行同一个命令 = 升级（`git pull --ff-only` + 重新安装依赖），不会重复建环境。

### 方式 B：把下面这段直接发给 Hermes（或任何 AI 助手），让它自己装

> 复制整段发过去即可。它是自包含的，不需要额外解释。

```text
帮我安装 hermes-update-check —— Hermes Agent 的"更新前风险检查"工具
（只做检查和评估，从不自动更新 Hermes）。请按顺序执行，任何一步失败就停下并告诉我原因：

1. git clone https://github.com/xinian5216/hermes-update-check.git ~/projects/hermes-update-check
2. 进入该目录并把包装进独立虚拟环境：
   uv venv .venv && uv pip install -e . --python .venv/bin/python
   （没有 uv 就用 python3 -m venv .venv && .venv/bin/pip install -e .）
3. 启用提交前脱敏扫描：git config core.hooksPath .githooks
4. 自检：.venv/bin/hermes-update-check version
5. 运行 .venv/bin/hermes-update-check check，用中文把结果复述给我：
   当前 channel（STABLE/MAIN/PRERELEASE/DETACHED）、代码来源、最新正式版本、
   Change Risk / Regression Signal / Data Confidence、触发的硬门禁、最终建议与复查时间。
   注意：退出码 10 表示"建议暂缓"，11 表示"数据不足"，都属于正常结论而不是报错。
6. 可选：把 GITHUB_TOKEN 写进 Hermes 的 .env（未认证的 GitHub API 只有 60 次/小时）。

约束：不要执行 `hermes update`，不要修改 HERMES_HOME，不要改动我现有的配置、备份和定时任务。
```

> Windows 上把第 2、4、5 步的路径换成 `.venv\Scripts\python.exe` 与
> `.venv\Scripts\hermes-update-check.exe`，或者直接用上面的 `install.ps1`。

### 方式 C：从 Release 安装（不用 git、不用一键脚本）

每个版本都会在 [Release 页](https://github.com/xinian5216/hermes-update-check/releases/latest)
附带构建好的 wheel 与 sdist，适合内网、离线镜像或想固定版本的场景：

```bash
# 文件名里带版本号，换版本时把 1.3.0 改掉即可
pip install "https://github.com/xinian5216/hermes-update-check/releases/download/v1.3.0/hermes_update_check-1.3.0-py3-none-any.whl"
hermes-update-check --version
```

也可以先下载再用本机工具装（`pipx install ./hermes_update_check-1.3.0-py3-none-any.whl`）。
装完就是同一个命令，`check` / `report` / `watch` 全都能用。两点要知道：

* `update` 只是替你调用 **Hermes 自身的** `hermes update`（本工具从不自己改 Hermes），
  所以和 Hermes 是不是 Git 安装无关；
* `rollback` 需要 **Hermes 本身**是 Git 安装 —— 它用 `git checkout <commit>` + 重装依赖回退；
  如果 Hermes 是 pip/uv 安装，它会明确报错（`... is not a git checkout`）而不是乱试。

### 方式 D：手动安装（uv / pip）

```bash
# uv（推荐）
cd /opt && git clone https://github.com/xinian5216/hermes-update-check.git
cd hermes-update-check && uv venv .venv && uv pip install -e . --python .venv/bin/python

# 或者 pip / pipx
sudo apt-get install -y python3 python3-venv python3-pip     # Debian/Ubuntu
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
pipx install .
```

### 方式 D：不安装，直接跑

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install pyyaml rich            # 两个运行依赖（缺 rich 也能跑，只是没颜色）
PYTHONPATH=src python -m hermes_update_check check
```

安装后得到命令 `hermes-update-check`。**建议设置 GitHub Token**（未认证 60 次/小时，认证 5000 次/小时）：

```bash
echo 'export GITHUB_TOKEN=ghp_xxx' >> ~/.bashrc     # 或写进 Hermes 的 .env
```

> 已经在用 [GitHub CLI](https://cli.github.com/) 的话不用再申请一个：凭据就在系统 keyring 里，
> 现取现用即可 —— `GITHUB_TOKEN=$(gh auth token) hermes-update-check check`。
> Windows 的 `.cmd` 包装同理（`for /f %%t in ('gh auth token') do set GITHUB_TOKEN=%%t`），
> 这样密钥不会以明文出现在任何文件里。

> 依赖只有两个：`PyYAML`（配置文件）、`rich`（终端美化）。
> 网络请求、SQLite、JSON、子进程全部用标准库，`rich` 缺失时自动降级为纯文本。

---

## 三、使用命令

```bash
hermes-update-check check                 # 快速检查：一屏给结论（默认命令）
hermes-update-check report                # 完整报告：每个风险因子的加减分明细 + Issue 样本
hermes-update-check report --format markdown --output r.md
hermes-update-check report --format json  # 机器可读（cron/CI/看板）
hermes-update-check overrides status      # 本地定制：已登记/未登记/漂移 + Override Safety
hermes-update-check overrides detect      # 只展示 git 看到的本地修改（不自动登记）
hermes-update-check profile show          # 你的使用画像（哪些功能算关键）
hermes-update-check profile detect        # 从本机 Hermes 配置自动推断画像（只读键名）
hermes-update-check profile edit          # 打印/写入 usage_profile（--write 会先备份 .bak）
hermes-update-check preflight             # 只看更新前检查（只读）
hermes-update-check health                # 只看健康检查（只读，加 --smoke 会真的调一次模型）
hermes-update-check watch                 # 定时检查：无变化不打扰，有变化才通知
hermes-update-check update                # 备份 → 更新 → 健康检查（需要确认，默认 N）
hermes-update-check update --dry-run      # 演练：显示 hermes update --plan，什么都不做
hermes-update-check rollback              # 回滚到 update_state.json 记录的版本
hermes-update-check config init           # 生成带注释的配置文件
hermes-update-check config show --json    # 查看生效配置（含环境变量覆盖后的结果）
hermes-update-check notify-test           # 测试 Telegram/Webhook 是否配置成功
hermes-update-check version               # 版本号
```

### 参数一览

**全局**（所有命令通用）

| 参数 | 说明 |
|---|---|
| `--config PATH` | 指定配置文件（默认 `~/.config/hermes-update-check/config.yaml`） |
| `--lang zh\|en` | 报告语言（默认取配置） |
| `--json` | 输出 JSON（纯机器可读，不含 ANSI/Rich 标记） |
| `--plain` / `--no-color` | 纯文本 / 关颜色（`NO_COLOR=1` 也生效） |
| `--no-cache` | 忽略本地缓存，强制重新请求 GitHub |
| `--timeout 20` | 网络超时（秒） |
| `-q` / `-v` / `-vv` | 安静 / INFO / DEBUG 日志 |

**check · report**：`--no-issues`（跳过 Issue 分析，少几次请求）、`--offline`（只用缓存，不发请求）、
`--format text|markdown|json`、`--output FILE`

**watch**：`--notify`（强制发送）、`--no-notify`（只判断不发送）、`--dry-run`（只显示会通知什么）

**update**：`--yes`、`--dry-run` / `--plan`、`--backup`、`--no-backup`、`--branch NAME`、
`--force`、`--skip-check`、`--skip-health-check`、`--auto-rollback`、`--restart-gateway`

**rollback**：`--yes`、`--to REF`（换成别的 git ref）、`--restore-backup PATH`、
`--in-place`（用备份覆盖 HERMES_HOME）、`--no-deps`、`--dry-run`

**health · preflight**：`--smoke`（真跑一次模型对话，会消耗一次调用）、`--timeout SECONDS`、
`--no-processes`（跳过进程扫描）

**config · notify-test**：`config init --force`（覆盖已有配置）、`notify-test --message "…"`

### 实际输出（节选，真实运行结果）

```
Hermes Update Advisor

本地安装状态 ────────────────────────────────────────────────────────────────
  Reported Version    v0.21.2
  Channel             MAIN   (跟踪开发分支（main）)
  Git Branch          main
  Git Commit          5eb99eb2
  Nearest Release     v2026.9.11 (v0.21.2)
  Behind Latest       125 commits
  Working Tree        dirty（4 个未提交修改）

最新正式版本 ────────────────────────────────────────────────────────────────
  Release         v0.21.3 (v2026.9.14)
  Published       33.6 小时前  (2026-09-14T16:04:14Z)
  Commits         1039+
  Merged PRs      338（来自 Release Notes）

更新状态 ────────────────────────────────────────────────────────────────────
  存在可用的正式版本更新
  当前在 main 分支且落后最新正式版本 125 个 commit：可以同步代码，但注意更新后拿到的是
  main 而不是该 Release。

全局风险（背景信息） ────────────────────────────────────────────────────────
  Change Risk          73 / 100
  Regression Signal    83 / 100
  Data Confidence      100 / 100
  Environment Risk     67 / 100
  Overall Risk         98/100          Risk Level  VERY HIGH
  Stability            2/100
  Issue Signal Confidence: 80/100（扫描 89 条，聚类 8 个）

个人就绪度 ──────────────────────────────────────────────────────────────────
  个人影响 Personal Impact         94 / 100  VERY HIGH
  核心功能可用性 Core Readiness    83 / 100  VERY HIGH
  回滚路径 Rollback Safety         警告（尚无 update_state.json，首次更新会创建）
  画像来源 Profile                 自动检测（运行 `profile detect` 后请人工调整）

你的核心功能 ────────────────────────────────────────────────────────────────
  浏览器工具                 WARN  HIGH / HIGH
  Provider（未细分）         WARN  HIGH / HIGH
  Session / 会话数据         WARN  HIGH / HIGH
  工具系统                   WARN  HIGH / HIGH
  CLI / Agent                WARN  HIGH / MEDIUM · 报告称不可用
  Gateway                    UNUSED（未使用，不计入）
  MCP                        UNUSED（未使用，不计入）

已知问题 ────────────────────────────────────────────────────────────────────
  Gateway    HIGH     HIGH     UNUSED
  MCP        HIGH     HIGH     UNUSED
  Telegram   HIGH     HIGH     UNUSED
  Session / 会话数据    HIGH     HIGH     IMPORTANT

系统级风险 ──────────────────────────────────────────────────────────────────
  数据库损坏              无
  Session 丢失            无
  凭证丢失 / 泄露         无
  安装损坏                证据不足（仅提示）
  Hermes 无法启动         无

门禁与提示 ──────────────────────────────────────────────────────────────────
  PASS  Release 年龄（<6h 阻断 / <12h 谨慎）
  WARN  开发分支（main）提示
        当前安装跟踪 main 开发分支（代码可能领先正式 Release，稳定性低于正式版本）
  BLOCK 工作区必须干净
        工作区有 4 个未提交修改
  WARN  Gateway 回归提示
        Gateway 存在 7 个报告（7 位报告人，4 个 open），严重程度 HIGH，可信度 HIGH

建议 ────────────────────────────────────────────────────────────────────────
  BLOCKED
  BLOCKED —— 本地工作区不安全
  （由 本地工作区 决定；你的风险：个人影响 94 / 核心可用性 83；全局风险 98——仅作背景）

  原因：
    - 工作区有 4 个未提交修改
    - Gateway：HIGH / 可信度 HIGH —— 你的画像标记为 unused，不计入个人影响

  下一步：
    - 先提交或 stash 本地修改（更新会 stash，但可能与新版冲突）

  下次监控复查 Next Monitoring Check：2026-09-19T01:46:21Z（约 24 小时后）
  （复查时间只是下一次观察的时机，不代表届时一定能更新）
```

---

## 四、代码来源判定（Code Provenance）

**`hermes --version` 显示的版本号不代表当前实际运行的代码位置。** 一个跟踪 main 的安装会一直自称 `v0.21.2`，
即使它的 HEAD 已经领先 `v0.21.3` 标签 300 个 commit。所以本工具把版本号降级为 `reported_version`，
真正的判断依据是 git 来源：

```
git provenance  →  release tag  →  hermes --version（最后的兜底）
```

| Channel | 判定条件 | 是否可作为常规更新目标 |
|---|---|---|
| `STABLE` | HEAD 正好在某个正式 Release tag 上（或已安装版本与某 Release 一致） | ✅ 是 |
| `MAIN` | 分支是 main/master，且不在 tag 上 | ⚠️ 否（进入开发分支模式） |
| `PRERELEASE` | HEAD 在预发布 tag 上，或报告版本带 rc/beta/alpha | ⚠️ 需显式允许 |
| `DETACHED` | detached HEAD 且对应不上任何 Release tag | ❌ 需人工判断 |
| `CUSTOM` | 其他分支/构建，无法对应已知 Release | ❌ 需人工判断 |
| `UNKNOWN` | 完全拿不到 git 或版本信息 | ❌ 输出 INSUFFICIENT DATA |

判定更新状态时（`decide_update`）：

* `MAIN`/`CUSTOM`/`DETACHED` 且**领先**最新 Release → `AHEAD_OF_STABLE`：
  > Current installation is tracking development/main and is already ahead of the latest stable release.
  > 明确说明「切回 tag 不是升级，而可能是降级」，并且**不会**显示 “Update available to vX”。
* `MAIN` 且**落后**最新 Release（在真实环境中很常见：本地没拉，远端分支却领先）→ 视为可更新，
  但 main 门禁仍然拦下，理由是「更新后拿到的是 main 而不是该 Release」。
* 领先与落后同时存在（diverged）→ 人工判断。
* `STABLE` 且落后 → 正常升级路径。

报告与 JSON 都会给出：`reported_version / channel / branch / commit / nearest_tag / ahead_by / behind_by / dirty`。

## 五、风险模型（组件化，第三阶段起作为背景信息）

> 第三阶段起，以下这些分数**不再单独决定**能否更新：它们提供背景与解释，
> 真正的裁决来自「六、阻断规则与使用画像」。全局风险高会把结论限制在 ACCEPTABLE，
> 但不会单独产生 WAIT/AVOID。

风险不再是一个数字，而是三个可解释的组件（各自 0-100）：

```
Change Risk       86 / 100      # 更新本身：Release Notes、年龄、改动规模
Regression Signal 88 / 100      # 外部已经出现的问题：分级回归聚类
Data Confidence  100 / 100      # 判断依据是否充分
Environment Risk  67 / 100      # 本地环境（main / 脏工作区 / 安装方式不明）

Overall Risk      99 / 100      # = 1-(1-change)(1-regression)(1-environment) − 稳定性加分
Stability          1 / 100      # = 100 − Overall
```

### Change Risk（上限 = keyword_cap + age_cap + volume_cap = 76 分，归一化到 100）

| 关键词族 | 权重 | 例子 |
|---|---|---|
| breaking change | **+30** | “BREAKING CHANGE:”，deprecated，移除支持 |
| database / migration / schema | **+25** | database, migration, schema, sqlite, state.db |
| session / state store | **+20** | session, state store, transcript, checkpoint |
| auth / credential / token | **+20** | auth, oauth, token, credential, secret |
| rewrite / refactor | **+20** | rewrite, refactor, overhaul, rework, revamp |
| gateway | **+15** | gateway |
| config / config migration | **+15** | config, configuration, yaml |
| storage / serialization / backend | **+10** | storage, serialization, backend, persistence, memory |
| provider / SDK / API / MCP / tool system | **+8** | provider, sdk, mcp, tool system, api |

* 每个族按命中次数做强度折算（1 次 ≈ 0.55，8 次以上 = 1.0），再按权重和归一化。
* 只出现 `docs / ui / typo / minor fix / new provider / changelog` 时该组件为 0 并触发稳定性加分。

发布年龄：<24h 满额(18)、1-3 天 13、3-7 天 8、7-14 天 3、14-30 天 0、>30 天 **−5（加分）**。
改动规模：commit/PR 数分档（<10 → 0 … ≥1500 → 16/18）；`X.Y.0` 额外 +4、主版本 +6；
**patch 却 rollup 了 >300 commit 再 +3**（不单纯相信 SemVer）；PR 数优先采用 Release Notes 里声明的数字。

### Data Confidence（0-100，清单式，可审计）

| 项 | 分值 |
|---|---|
| Release API 拿到了 Release | 25 |
| Compare API 拿到了差异规模 | 20 |
| Issue API 回答成功 | 20（仅从缓存回答 15） |
| Git provenance 可用 | 20 |
| Release Notes 可读（≥200 字符） | 15 |

`confidence < 0.45`、拿不到 Release、或拿不到发布时间 → **UNKNOWN / INSUFFICIENT DATA**（不是 LOW）。

### UNKNOWN 不会降低风险（硬性要求）

某个组件缺失时，它按**下限**计入，并把结果标成下界：

```
Known Risk: >= 61        # 不是 61 以下
Confidence: 80%
Recommendation: WAIT — INSUFFICIENT OBSERVATION DATA
```

* 回归信号未知 → 按下限 25/100 计入，并在报告中标 `UNKNOWN (assume >= 25)`；
* Issue 数据缺失会让 `block_on_insufficient_data` 门禁触发 → 结论只能是 WAIT，绝不可能是 UPDATE；
* 测试 `test_missing_issue_data_never_makes_a_release_look_safer` 用「有数据的干净版本 vs 没数据的同一版本」
  来钉死这条规则：**没数据时的分数一定不低于有数据时**。

## 六、阻断规则与使用画像（第三阶段核心）

第三阶段把决策模型从 **Global Risk Driven** 改成 **User Impact + Core Feature Readiness Driven**：

> 全局风险继续保留，但只作为背景信息；真正决定「能不能更新」的是——
> **这个版本对你的关键工作流是否已经可用。**

设计前提是：Hermes 每天都在合入大量改动，如果门槛定成"近乎零 Bug 才更新"，工具会永远说 WAIT，
最后没人再用。目标不是更宽松，而是**更符合实际使用影响**：

```
Global instability does not necessarily mean personal unusability.
A bug in an unused feature should not block an update.
Data loss and installation corruption remain non-negotiable blockers.
```

### 6.1 使用画像 `usage_profile`

每项功能（以及每个 Provider）都有一个使用等级：

| 等级 | 权重 | 含义 |
|---|---|---|
| `critical` | 1.0 | 关键：**已确认**的严重回归会阻断更新 |
| `important` | 0.6 | 重要：计入个人影响 |
| `optional` | 0.25 | 可选：只造成很小的扣分 |
| `unused` | 0.0 | 未使用：**完全不计**，这个功能的 Bug 不会让工具说 WAIT |

```yaml
usage_profile:
  features:
    cli_agent: critical      # 你每天用的 CLI
    desktop: critical
    sessions: critical
    tools: critical
    mcp: important
    gateway: unused          # 没跑 Gateway 就不该被 Gateway 的 Bug 拖住
    telegram: unused
    web_tools: important
    browser_tools: optional
    docker: unused
  providers:
    providers: important     # 未单独列出的 Provider 的组默认值
    openai: critical
    anthropic: important
    gemini: unused
```

三个命令管理画像：

```bash
hermes-update-check profile show      # 当前生效的画像（含来源：配置 / 自动检测 / 内置默认）
hermes-update-check profile detect    # 从你的 Hermes 配置自动推断，并列出每项的判断依据
hermes-update-check profile edit      # 打印可粘贴的 YAML；加 --write 直接写入配置（先备份 .bak）
```

自动检测**只区分「已配置 → important」与「未配置 → unused」，从不替你判断什么叫 critical**——
它读的是配置里的**键名**与环境变量**名字**（例如 `TELEGRAM_BOT_TOKEN` 是否存在），
从不读取任何值，也从不联网。

### 6.2 两个新指标

```
个人影响 Personal Impact      0-100   已知问题对你所用功能的暴露程度
核心功能可用性 Core Readiness 0-100   你的关键工作流是否真的「不可用」
```

`Personal Impact` 用线性权重（一个未使用功能的 Bug 贡献 0），`Core Readiness` 用阻尼后的权重驱动结论：
**只有"报告称该功能不可用"（unusable / cannot start / no longer works / data is unrecoverable…）
才会把可用性拉低**；"某个边界场景有 Bug" 只反映在个人影响里，不会让工具说"你的核心功能挂了"。

判定分档：`>= 85` 可判 SAFE；`60-84` 最高 ACCEPTABLE；`< 60` → WAIT。

### 6.3 系统级风险（与画像无关，永远不可协商）

```
数据损坏 / Session 丢失 / 凭证丢失或泄露 / 配置损坏
安装损坏 / 回滚失败 / Hermes 无法启动 / 所有 Provider 不可用
```

这些类别需要**高可信度 + 独立佐证**（≥2 位独立报告人，或 maintainer 确认/打标，或含复现步骤）才阻断；
只有一条未确认的报告 → 只提示、不阻断（`UNKNOWN` 从不被当作安全）。

### 6.4 现在只有这五件事会阻断

```yaml
hard_gates:
  block_on_systemic_risk: true       # 系统级风险（上一节）
  block_on_critical_workflow: true   # 你标记为 critical 的功能被确认不可用
  block_on_rollback_safety: true     # 回滚路径不可用（提交找不回 / 状态目录不可写 / 磁盘不足）
  block_dirty_worktree: true         # 本地工作区有未提交修改（本地安全，不是版本质量问题）
  block_on_insufficient_data: true   # 观测数据不完整
```

**被降级的旧门禁**（只提示、只把结论限制在 ACCEPTABLE，不再阻止更新）：

| 旧规则 | 现在 |
|---|---|
| `minimum_release_age_hours: 48` 永久阻断 | 分段策略：`<6h` 阻断 / `6-12h` 谨慎 / `12-24h` 可接受 / `>24h` 正常（`release_age_policy`） |
| 跟踪 main 分支 → 阻断 | WARN + 计入 Environment Risk（本地安全类仍然阻断，例如脏工作区） |
| 预发布版本 → 阻断 | WARN（可用 `block_prerelease: true` 恢复旧行为） |
| 普通 Gateway / MCP / Provider / 认证 / 崩溃回归 → 阻断 | WARN + 计入对应功能的个人影响 |

旧配置**不会被拒绝**：`minimum_release_age_hours` 等键仍然能读，会被映射到新策略并给出弃用提示
（例如 `Deprecated: hard_gates.minimum_release_age_hours → Use: release_age_policy`）。

### 6.5 结论等级与两种时钟

| 结论 | 含义 |
|---|---|
| `BLOCKED` | 有系统级风险或已确认的关键工作流回归：现在不要更新 |
| `WAIT` | 证据不足，或关键功能存在**尚未确认**的严重报告：先观察 |
| `ACCEPTABLE` | 有已知 Bug，但没打到你的关键工作流，且备份/回滚/预检通过：可以更新（先备份） |
| `SAFE` | 没有明显严重回归 |

外加三种特殊状态：`AHEAD_OF_STABLE`（当前代码已领先正式版本）、`MANUAL_REVIEW`（安装状态非标准）、
`INSUFFICIENT_DATA`（数据不足）。

报告里给出**两种时间**，避免误读：

```
下次监控复查 Next Monitoring Check   什么时候再看一眼
策略最早放行 Earliest Policy Clearance   发布年龄/阻断条件什么时候解除
```

"下次复查 12 小时"**不代表**"12 小时后就能更新"。

### 6.6 真实数据对比（同一台机器、同一个 release）

同一个 `v2026.9.14`（发布 81.7 小时、全局风险 98/100 VERY HIGH）：

| | 旧模型（Global Risk 驱动） | 新模型（Personal Readiness 驱动） |
|---|---|---|
| 结论 | `WAIT` | `BLOCKED`（仅因本地工作区有 4 个未提交修改） |
| 阻断门禁 | `main_branch` + `dirty_worktree` + `active_update_failure_regression` | 仅 `dirty_worktree` |
| 个人影响 / 核心可用性 | — | 94 / 83 |
| 提示（不再阻断） | `channel_mismatch` | main 分支提示、Channel 提示、Gateway/Provider 回归提示（均标记为 unused 或 WARN） |
| 工作区干净时 | 仍然 `WAIT`（main 分支是**永久**属性） | **`ACCEPTABLE`**（"已知回归没有伤到你的关键工作流，可以更新，先备份"） |

也就是说：旧模型会把用户永远卡在一个**他无法通过小心操作改变的属性**（跟踪 main）上；
新模型只在**本地确实不安全**时阻断，并且明确告诉用户"提交/暂存后即可更新"。

## 七、回归聚类与可信度

> 第三阶段起，每个聚类还会给出 **`affected_features`**（影响哪些功能，按 Provider 细分）、
> **重复报告折叠**（标题相似度 / 显式引用 / duplicate 标签，同一根因不重复计分）与
> **相关性折扣**（一个 Issue 命中多个类别时：primary 1.0 / secondary 0.4 / tertiary 0.2）。
> 单个 Provider 完全不可用按 `HIGH` 处理，只有「全体 Provider / 凭证体系」故障才是 `CRITICAL`。

Issue 数量会被高噪声仓库淹没，所以 9 类回归分别做**严重程度 × 独立性 × 佐证**评分：

```
DATABASE / SESSION / GATEWAY / CONFIG_MIGRATION / UPDATE_FAILURE / AUTH / CRASH / MCP / PROVIDER
```

严重程度：`CRITICAL`（数据损坏、Session/凭证丢失、无法启动、update 破坏配置、回滚失败）、
`HIGH`（Gateway 反复崩溃、升级失败、MCP 完全不可用）、`MEDIUM`（个别 provider 失效）、`LOW`（UI/日志）。

独立性（不把 3 个 Issue 当 3 个独立报告）：

| 因素 | 权重 |
|---|---|
| 不同报告人（1 位报告人只算 0.15，且可信度封顶 MEDIUM） | 0.30 |
| Issue 仍 open | 0.15 |
| maintainer 确认（评论 author_association = OWNER/MEMBER/COLLABORATOR + 确认语句） | 0.15 |
| linked PR | 0.10 |
| 含复现步骤 / traceback | 0.10 |
| 明确提到当前版本 | 0.10 |
| 多条报告描述同一个故障（标题签名一致） | 0.10 |

→ 可信度 `HIGH (≥0.65) / MEDIUM (≥0.35) / LOW`。

Regression Signal 由各聚类按概率 OR 组合，**每个聚类先打 0.6 折**（避免高噪声仓库直接顶到 100），
再按 **Issue Signal Confidence** 折算（`0.55 + 0.45 × confidence`），最后把「命中量」组件按半权重计入。

Issue Signal Confidence（0-100）：Issue API 回答 40 + 基线可用 20 + 有 ≥2 独立报告人的聚类 20
（单报告人 10）+ 评论富化成功 10 + 非降级数据 10。

`issue_enrichment_limit: 3` 控制每次运行最多对几个严重 Issue 拉取评论（每个 1 次 API 调用），
用来判断「maintainer 是否确认 / 是否已有 linked PR」。

## 八、回滚机制

**为什么不能只 `git checkout`**：Hermes 的 venv 里装着与代码版本匹配的依赖，回退代码而不重装依赖会得到一个“代码旧、依赖新”的混合环境。
所以回滚分两步：**git 引用 + 依赖重装**。

### 更新前记录（`update_state.json`）

```json
{
  "previous_version": "0.21.2",
  "previous_tag": "v2026.9.11",
  "previous_commit": "5eb99eb2...",
  "previous_branch": "main",
  "previous_dirty": true,
  "install_kind": "git",
  "install_dir": "/opt/hermes/hermes-agent",
  "hermes_home": "/home/hermes/.hermes",
  "venv_python": "/opt/hermes/hermes-agent/venv/bin/python",
  "python_version": "3.11.16",
  "uv_path": "/usr/local/bin/uv",
  "backup_path": "/home/hermes/.hermes/backups/hermes-backup-20260915.zip",
  "snapshot_path": "/home/hermes/.hermes-update-check/snapshots/20260915-061745",
  "update_time": "2026-09-15T06:17:45+00:00",
  "target_version": "0.21.3",
  "target_tag": "v2026.9.14",
  "command": "hermes update --backup --yes",
  "status": "succeeded",
  "new_version": "0.21.3",
  "new_commit": "abc12345"
}
```

同时 `snapshots/<时间戳>/` 里有一份快照：`config.yaml` 的**副本**、`state.db` 的**SQLite 一致性副本**（用 backup API，运行中也安全）、
以及 `.env`/`auth.json`/`skills`/`cron` 的**指纹**（只哈希不复制 —— 不把密钥写到第二个地方）。

### 回滚流程

```bash
hermes-update-check rollback --dry-run      # 先看计划
hermes-update-check rollback                # 交互确认后执行
hermes-update-check rollback --yes          # 非交互（脚本/无人值守）
```

实际动作：

1. `git fetch --tags`（尽力而为，失败只记录不中断）；
2. 工作区脏 → `git stash push -u -m "hermes-update-check rollback"`（不丢你的改动，只 stash）；
3. `git checkout <previous_commit>`；
4. **重装依赖**：优先 `uv pip install -e ".[all]" --python <venv python>`，否则 `<venv python> -m pip install -e ".[all]"`；
5. 可选恢复数据：`--restore-backup <zip|目录>`（先解压到临时目录；只有加 `--in-place` 才会覆盖 `HERMES_HOME`，而且会先把现有目录改名为 `HERMES_HOME.broken-<时间戳>`，不删除）；
6. 回滚后自动重跑健康检查，并把 `status` 记为 `rolled_back`。

`status` 取值：`in_progress` / `succeeded` / `health_check_failed` / `rolled_back` / `failed`。

---

## 九、如何配置 Telegram 通知

1. 在 Telegram 里找 **@BotFather** → `/newbot` → 拿到 token（形如 `123456:ABC-DEF...`）。
2. 把 token 放进**环境变量**（不要写进 config.yaml）：

   ```bash
   # Hermes 的 .env（推荐，Hermes 会加载）
   echo 'HERMES_UPDATE_CHECK_TELEGRAM_TOKEN=123456:ABC-DEF...' >> ~/.hermes/.env
   # 或 shell
   export HERMES_UPDATE_CHECK_TELEGRAM_TOKEN=123456:ABC-DEF...
   ```

3. 给这个 bot 发一条任意消息，然后打开
   `https://api.telegram.org/bot<TOKEN>/getUpdates`，复制 `result[].message.chat.id`。
4. 编辑 `~/.config/hermes-update-check/config.yaml`：

   ```yaml
   notify:
     telegram:
       enabled: true
       bot_token_env: HERMES_UPDATE_CHECK_TELEGRAM_TOKEN
       chat_id: "123456789"
   ```

5. 测试：

   ```bash
   hermes-update-check notify-test
   # → notify/telegram  [ OK ] sent
   ```

6. 挂上定时任务（见下一节），以后只有「新 Release / 风险等级下降 / 现在可以安全更新」时才推送。

Webhook 同样简单（Slack、Discord、n8n、自建服务均可）：

```yaml
notify:
  webhook:
    enabled: true
    url: https://example.com/hooks/hermes
    bearer_token_env: HERMES_UPDATE_CHECK_WEBHOOK_TOKEN   # 可选
```

Webhook 收到的是 JSON：`{title, body, level, tag, url, fields:{current, latest, risk, recommendation}}`。

---

## 十、配置文件参考

> 第三阶段新增 `usage_profile` / `release_age_policy` / `smoke_tests` 三节，
> 并把 `hard_gates` 简化到五个阻断开关；旧的 `minimum_release_age_hours`、`block_active_*` 仍然能读，
> 加载时会打印弃用提示并映射到新语义（见 `config.example.yaml` 的注释）。

默认位置：Linux/macOS `~/.config/hermes-update-check/config.yaml`，Windows `%APPDATA%\hermes-update-check\config.yaml`。
优先级：**内置默认值 < 配置文件 < `HERMES_UPDATE_CHECK_*` 环境变量 < 命令行参数**。
用 `hermes-update-check config init` 生成完整注释版本；`config show --json` 查看生效值。

```yaml
risk_threshold: 40             # 风险分高于它就不建议更新
minimum_release_age_days: 5    # 发布不足 N 天不建议更新（观察期，软规则）
auto_update: false             # 永远默认为 false；改成 true = 显式接受无人值守更新
backup_before_update: true     # 更新时传 --backup
check_github_issues: true      # 是否分析发布后的 Issue
check_issue_baseline: true     # 是否拉基线窗口做归一化（多 1 次请求）
preferred_channel: stable      # stable | prerelease | main（与实际 channel 不一致会 WARN）
allow_prerelease: false
language: zh                   # zh | en
plain_output: false

hard_gates:                    # 硬门禁：规则优先于评分（详见第六节）
  enabled: true
  minimum_release_age_hours: 48
  block_main_branch_update: true
  block_dirty_worktree: true
  block_prerelease: true
  block_active_database_regression: true
  block_active_session_regression: true
  block_active_gateway_regression: false
  block_active_update_failure: true
  block_on_insufficient_data: true

github:
  repo: NousResearch/hermes-agent
  token_env: GITHUB_TOKEN              # 环境变量名（不存密钥本体）
  token_env_fallbacks: [GH_TOKEN, HERMES_UPDATE_CHECK_GITHUB_TOKEN]
  cache_ttl_minutes: 360               # 网络失败时会退回过期缓存并标记 degraded
  max_issue_searches: 3                # 每次运行最多几次 Issue 搜索（未认证搜索限 10 次/分钟）
  issue_enrichment_limit: 3            # 每次运行最多对几个严重 Issue 拉评论（判断 maintainer 确认/linked PR）

network: {timeout_seconds: 15, retries: 2}
paths:
  hermes_home: null            # 默认 $HERMES_HOME，再默认 ~/.hermes
  state_dir: null              # 默认 ~/.hermes-update-check

update:
  branch: main
  extra_args: []
  restart_gateway: false
  health_check_after_update: true
  auto_rollback_on_failed_health: false   # 失败自动回滚（默认关，需要你明确开启）
  min_free_disk_gib: 2.0

watch:
  notify_on_new_release: true
  notify_on_risk_improvement: true
  notify_when_safe: true
  notify_on_first_run: false    # 首次运行只记录状态，不打扰
  min_interval_hours: 20

notify:
  telegram: {enabled: false, bot_token_env: HERMES_UPDATE_CHECK_TELEGRAM_TOKEN, chat_id: ""}
  webhook: {enabled: false, url: "", bearer_token_env: HERMES_UPDATE_CHECK_WEBHOOK_TOKEN}

logging: {level: INFO, file: null, console: false}

risk:                          # 调高 → 更保守；调低 → 更激进
  keyword_cap: 40
  age_cap: 18
  volume_cap: 18
  issues_cap: 30
  context_cap: 12
  bonus_cap: 15
```

环境变量覆盖（适合 VPS 上临时调参）：

```bash
HERMES_UPDATE_CHECK_RISK_THRESHOLD=25
HERMES_UPDATE_CHECK_MINIMUM_RELEASE_AGE_DAYS=7
HERMES_UPDATE_CHECK_LANGUAGE=en
HERMES_UPDATE_CHECK_STATE_DIR=/var/lib/hermes-update-check
HERMES_UPDATE_CHECK_HERMES_HOME=/home/hermes/.hermes
HERMES_UPDATE_CHECK_LOG_LEVEL=DEBUG
HERMES_UPDATE_CHECK_CONFIG=/etc/hermes-update-check.yaml
```

未知配置键会打印告警（防止把 `risk_threshold` 拼错后悄悄失去保护）。

---

## 十一、watch 模式与 cron

> 第三阶段起，通知跟随**结论变化**（`BLOCKED → WAIT`、`WAIT → ACCEPTABLE`、`ACCEPTABLE → SAFE`、
> `SAFE → WAIT`、`ACCEPTABLE → BLOCKED`）以及**你的关键工作流**的变化
> （"你的关键工作流出现回归" / "关键工作流回归已解除"）。
> 只有全局风险变化、但结论没变时（例如 80 → 70）**不会打扰你**。

`watch` 只做检查 + 状态对比，**不会更新**：

* 首次运行：只记录状态（除非 `notify_on_first_run: true`）；
* 发现**没见过的 Release** → 通知；
* 风险等级从 HIGH/VERY HIGH **降到** MEDIUM 及以下 → 通知；
* 此前建议 WAIT/AVOID，现在变成 UPDATE → 通知（“现在可以更新了”）；
* 没有变化 → 只打印一行状态，不打扰（`min_interval_hours` 兜底限流）。

```bash
# 每天 09:10 检查一次（crontab -e）
10 9 * * * /opt/hermes-update-check/.venv/bin/hermes-update-check watch >> /var/log/hermes-update-check.cron.log 2>&1

# 只想看会通知什么，不发通知：
hermes-update-check watch --dry-run --no-notify
```

systemd timer 版本：

```ini
# /etc/systemd/system/hermes-update-check.service
[Unit]
Description=Hermes update check
[Service]
Type=oneshot
User=hermes
Environment=GITHUB_TOKEN=ghp_xxx
ExecStart=/opt/hermes-update-check/.venv/bin/hermes-update-check watch
```

```ini
# /etc/systemd/system/hermes-update-check.timer
[Unit]
Description=Run Hermes update check daily
[Timer]
OnCalendar=*-*-* 09:10:00
Persistent=true
[Install]
WantedBy=timers.target
```

---

## 十二、JSON 输出契约

`--json`（`check` / `report` 均可）输出**纯 JSON，不含任何 ANSI 或 Rich 标记**（有测试保证）。
沿用 v1 的键，并新增二阶段字段：

```json
{
  "local": {
    "reported_version": "0.21.2",
    "channel": "MAIN",
    "branch": "main",
    "commit": "5eb99eb2",
    "nearest_tag": "v2026.9.11",
    "ahead_by": null,
    "behind_by": 125,
    "dirty": true
  },
  "release": { "tag": "v2026.9.14", "display_version": "0.21.3", "age_hours": 15.3, "commit_count": 1039, "pr_count": 338 },
  "risk": {
    "change_risk": 86,
    "regression_signal": 88,
    "data_confidence": 100,
    "environment_risk": 67,
    "overall": 99,
    "stability": 1,
    "level": "VERY HIGH",
    "score_is_lower_bound": false
  },
  "hard_gates": [ { "key": "release_age", "status": "BLOCK", "reason": "…" } ],
  "regressions": [ { "key": "GATEWAY", "severity": "CRITICAL", "confidence": "HIGH", "reports": 7, "unique_reporters": 6, "open": 6 } ],
  "update_status": "update_available",
  "recommendation": "ACCEPTABLE",
  "recommendation_detail": {
    "action": "ACCEPTABLE",
    "decided_by": "caution",
    "personal_impact": 16,
    "personal_impact_level": "LOW",
    "core_readiness": 92,
    "core_readiness_level": "HIGH",
    "systemic_risks": [ { "key": "DATA_CORRUPTION", "detected": false, "blocking": false } ],
    "cautions_zh": [ "…" ],
    "policy_clearance_at": "2026-09-15T10:04:14Z",
    "policy_clearance_hours": 8.6
  },
  "personal_readiness": {
    "profile_source": "detected",
    "personal_impact": 16,
    "core_readiness": 92,
    "features": [ { "key": "sessions", "level": "critical", "status": "OK", "unavailable": false } ],
    "systemic": [ { "key": "SESSION_LOSS", "detected": false } ]
  },
  "rollback_safety": { "status": "PASS", "previous_ref": "5eb99eb2", "checks": [ … ] },
  "install_state": "MAIN_WITH_MANAGED_OVERRIDES",
  "local_overrides": {
    "managed_count": 4,
    "unknown_count": 0,
    "drifted": [],
    "missing": [],
    "safety": "PASS",
    "prediction": { "confidence": "HIGH", "target": "v2026.9.14", "manual_merge_likely": false }
  },
  "usage_profile": { "features": { "sessions": "critical" }, "providers": { "openai": "important" } },
  "recommended_recheck": "2026-09-15T19:21:04Z",
  "recommended_recheck_hours": 12.0
}
```

`recommendation` 取值（第三阶段）：`BLOCKED` / `WAIT` / `ACCEPTABLE` / `SAFE`，以及特殊状态
`AHEAD_OF_STABLE` / `MANUAL_REVIEW` / `INSUFFICIENT_DATA` / `UP_TO_DATE`。
旧值 `UPDATE` / `AVOID` 仍然被接受（老的状态文件与旧调用方）。

`recommended_recheck_hours` 是**下次监控复查**；`policy_clearance_hours` 是**策略最早放行时间**
（发布年龄等阻断条件何时解除），两者含义不同，不要混用。

## 十三、退出码（cron / CI 用）

| 码 | 含义 |
|---|---|
| 0 | 成功：已是最新 / 已领先 Release（AHEAD_OF_STABLE），或**可以更新**（`SAFE` / `ACCEPTABLE`；兼容旧值 `UPDATE`） |
| 1 | 运行内部错误（未捕获异常，堆栈在 `~/.hermes-update-check/logs/`） |
| 2 | 用法错误（参数不对） |
| 3 | 配置 / 前置条件错误（例如没有 `update_state.json` 可回滚） |
| 10 | 检查完成，但结论是 `BLOCKED` / `WAIT` / `MANUAL_REVIEW`（含任何阻断门禁；兼容旧值 `AVOID`） |
| 11 | 数据不足（INSUFFICIENT DATA） |
| 12 | 更新后健康检查失败（需要 rollback） |
| 13 | 用户取消 / 非交互环境未给 `--yes` |
| 14 | 更新前检查失败 |

例：只在“可以安全更新”时再去做别的事

```bash
if hermes-update-check check >/dev/null; then
  echo "安全：可以安排更新窗口"
else
  echo "exit=$? 暂缓"
fi
```

---

## 十四、安全边界

明确写下来，方便审计：

1. **默认绝不自动更新。** `check`/`report`/`watch`/`preflight`/`health`/`config` 全部只读；
   只有 `update` 会改系统，并且：
   * 风险门禁不通过 → 直接停下（除非 `--force`）；
   * 更新前检查失败 → 直接停下（除非 `--force`）；
   * 需要你输入 `y`（**默认 N**）；非交互环境（stdin 不是 tty）且没给 `--yes` 时**拒绝执行**；
   * `auto_update: true` 是唯一能跳过确认的开关，默认 false，改了会收到告警。
2. **先备份再更新。** `update` 默认 `--backup`；另外自己再做快照（config 副本 + state.db 一致副本 + 指纹）。
3. **密钥不进磁盘副本。** `.env` / `auth.json` 只记哈希；Telegram token 只从环境变量读，永不写进报告。
4. **数据不足 ≠ 安全。** 缺 Issue 数据 → 记半个上限分 + `UNKNOWN`；缺 Release/发布时间 → 直接 `UNKNOWN`。
5. **不因为 API 失败就阻塞你。** 有缓存用缓存并标记 `degraded`；彻底没数据时输出 `INSUFFICIENT DATA` 让你人工判断。
6. **回滚是真实的回滚。** 代码 + 依赖 + （可选）数据，且覆盖 `HERMES_HOME` 前先把旧目录改名而不是删除。
7. **健康检查失败有明确信号。** 打印 `UPDATE FAILED HEALTH CHECK` 并给出 `rollback` 命令；`--auto-rollback` 可自动执行。

---

## 十五、测试与开发

```bash
uv venv .venv && uv pip install -e ".[dev]" --python .venv/bin/python
.venv/bin/python -m pytest -q          # 538 个测试（472 个函数），全部离线：不用网络、不碰真实安装
.venv/bin/python -m pytest -q tests/test_provenance.py tests/test_gates.py tests/test_advisor.py
```

测试覆盖（对应二阶段要求的 30 项场景）：

| 场景 | 落点 |
|---|---|
| stable / main-ahead / main-behind / detached / custom / dirty worktree / prerelease | `test_provenance.py`、`test_gates.py` |
| Release <24h、<48h（观察期门禁与"最早可重新评估时间"） | `test_gates.py` |
| Issue / Compare / Release API 不可用，UNKNOWN 不降风险 | `test_risk.py`、`test_provenance.py`、`test_checker.py` |
| 重复 Issue、同一作者多条、多独立作者、maintainer 确认、linked PR | `test_clusters.py` |
| CRITICAL 数据库回归 / Session 丢失 / Gateway 故障 | `test_clusters.py`、`test_gates.py` |
| Hard Gate 优先于低风险分 | `test_gates.py`、`test_advisor.py` |
| Rich / plain / JSON / Markdown 四种输出 | `test_report.py` |
| watch 只对有意义的变化通知、channel 不一致、preferred_channel、复查时间 | `test_watch.py`、`test_cli.py`、`test_advisor.py` |
| 密钥/隐私扫描器自身的规则与误报 | `test_scan_secrets.py` |
| `python -m` 入口与日志配置 | `test_entrypoints.py` |

以及一阶段既有测试（版本号双体系解析、每一组风险因子、场景化评估、配置优先级与校验、
GitHub 响应解析与查询长度限制、缓存过期降级、状态文件往返、更新前/后检查、更新命令构造与回滚计划、
「没有确认就绝不更新」这条最重要的安全属性）——**二阶段没有删除任何旧测试**。

---

## 十六、隐私与密钥（公开仓库的三道防线）

1. **代码里不写死任何凭据**：配置只写**变量名**（如 `bot_token_env: HERMES_UPDATE_CHECK_TELEGRAM_TOKEN`），值一律来自环境变量；
   `.gitignore` 已排除 `.env*`、`*.pem`、`*.key`、`*.p12`、`config.yaml`、`update_state.json` 等本地文件。
2. **提交前扫描**（pre-commit hook，每个 clone 启用一次）：

   ```bash
   git config core.hooksPath .githooks
   ```

   之后每次 `git commit` 都会对**暂存内容**跑 `scripts/scan_secrets.py --staged`，命中即中止提交。
3. **CI 双保险**：`.github/workflows/ci.yml` 在 push / PR 时运行
   `scan_secrets.py --all-history`（扫描全部历史对象，能抓"提交过又删掉"的内容）+ [gitleaks](https://github.com/gitleaks/gitleaks)。
   公开仓库还默认开启 GitHub 自带的 secret scanning 与 push protection（会直接拒绝包含已知密钥的推送）。

手动检查（发布前建议跑一遍）：

```bash
python scripts/scan_secrets.py                  # 工作区全部文件
python scripts/scan_secrets.py --staged         # 只查暂存区（hook 用的就是它）
python scripts/scan_secrets.py --all-history    # 全部 git 历史对象
```

规则分两类：

* **凭据**：GitHub token（`ghp_/gho_/ghs_/github_pat_`）、OpenAI/Anthropic key、Telegram bot token、AWS/GCP key、JWT、PEM 私钥块、Stripe、npm/PyPI token，以及 `password = "..."` / `token = "..."` 这类赋值；
* **隐私**：Windows 绝对路径（`C:\Users\...`）、真实 home 目录、公网 IP、个人邮箱域名、手机号样式数字串。

命中时退出码为 1，输出**脱敏**片段（只留前几个字符），全文只打印规则名与行号。

`examples/` 里的示例是**脱敏后的真实输出**：安装路径替换为 `/opt/hermes`、`/home/hermes`，本地修改文件名单替换为占位符。发布新示例前请先跑一遍扫描。

---

## 十七、代码索引与开发规范（给 AI agent 用）

改这个项目时**不要一上来就通读全部源码**——先读索引，再按需打开文件：

| 文件 | 内容 | 生成方式 |
|---|---|---|
| [`AGENTS.md`](AGENTS.md) | 项目铁律（5 条不可破坏的不变量）、目录速查、开发循环、约定 | 手写 |
| [`docs/CODE_MAP.md`](docs/CODE_MAP.md) | 每个模块的一句话职责 + 公开类/函数/常量 + **行号** + CLI 命令表 + 测试清单 | `scripts/build_index.py` |
| [`docs/index.json`](docs/index.json) | 同上，机器可读（模块 / 符号 / 签名 / 测试名） | 同上 |

```bash
python scripts/build_index.py           # 改完代码后重新生成
python scripts/build_index.py --check   # 只校验是否过期（CI 用，过期即失败）
```

索引由 `ast` 从源码分析生成，**不会和代码脱节**：CI 里 `--check` 会拦住忘记更新的提交。

### 开发循环（本地 = CI 同一套门槛）

```bash
python -m pytest -q                                   # 538 个测试（472 个函数），离线
python -m pytest --cov --cov-fail-under=80            # 覆盖率门槛（当前 82%）
ruff check .                                          # lint（0 findings 才能过）
ruff format --check .                                 # 格式检查（如需改写：ruff format .）
python scripts/scan_secrets.py --staged               # 提交前脱敏扫描（钩子已自动执行）
python scripts/build_index.py                         # 公开接口有变动时
```

发布流程（维护者）：

1. 同时改 `pyproject.toml` 与 `src/hermes_update_check/__init__.py` 的版本号（有测试断言两者一致）；
2. 在 `CHANGELOG.md` 写对应小节 —— 它会直接成为 Release 说明；
3. 跑上面这套门槛，全绿后打 tag：`git tag -a vX.Y.Z -m "..." && git push origin vX.Y.Z`；
4. `uv build` 生成 wheel/sdist，再 `gh release create vX.Y.Z --notes-file <CHANGELOG 小节> dist/*`；
5. **像外人一样验收**：从 Release 页面下载 wheel → 在全新 venv 里 `pip install` → 运行
   `hermes-update-check --version`。`gh release view` 只能证明发布存在，不能证明产物装得上。

---

## 十八、本地定制（Managed Local Overrides，第四阶段）

用户会**有意**修改本地 Hermes 源码：去掉不喜欢的 UI、改掉某段提示、调整默认行为。
这些修改是经过思考的定制，不是意外。但 `git` 看起来只有一句 "dirty"，
旧版本因此把"有意修改"和"临时文件 / 半成品 / 冲突残留"一视同仁地拦下来——
只要你有定制，就永远得不到有用的结论。

第四阶段把工作区状态拆成两类：

```
known dirty   已登记、有哈希、可生成 patch、可恢复  -> 可管理
unknown dirty 未登记，可能是缓存/临时文件/半成品   -> 仍然阻断
```

### 18.1 命令

```bash
huc overrides detect      # 只展示 git 看到的修改（绝不自动登记）
huc overrides register    # 把你选中的修改登记为受管定制
huc overrides status      # 已登记 / 未登记 / 漂移 + Override Safety
huc overrides list        # 逐文件列出状态与策略
huc overrides diff        # 你的定制内容；--target vX.Y.Z 预测与新版的冲突
huc overrides refresh     # 把当前内容确立为新基线（drift 之后）
huc overrides unregister  # 不再受管（文件内容不动）
huc overrides export out.zip
huc overrides doctor      # 登记表 / patch / 哈希 / 事务自检
```

`detect` 默认**不登记任何东西**，未跟踪文件（缓存、日志、下载物）默认跳过——
它们必须由你显式指定才会被登记。

### 18.2 登记表

```
~/.hermes-update-check/overrides/
├── registry.json          # 版本 / base commit / 每个文件的哈希与策略
├── patches/<时间>.patch    # git diff --binary（支持文本、删除、改名、二进制）
└── snapshots/<时间>/…      # 未跟踪文件的原始字节（用于"新增文件"式定制）
```

登记时记录的是**真正的基线**，而不是文件名：

```json
{ "path": "hermes/a.py", "status": "modified", "policy": "preserve",
  "base_commit": "0bca6a32…", "base_sha256": "…", "current_sha256": "…",
  "patch_sha256": "…", "registered_at": "2026-09-18T04:13:46Z" }
```

`patch + metadata` 才是长期格式；`git stash` 只在更新过程中临时使用（它依赖当前仓库、
可能被清理、无法审计、难以跨机器迁移）。patch 从不包含密钥以外的额外处理——
相反，保存与导出前会跑一遍密钥扫描（发现疑似密钥只 **WARN 并限制输出**，绝不删你的 patch）。

### 18.3 判定规则

| 磁盘现状 | 结论 |
|---|---|
| 文件哈希 == 登记时的哈希 | `MANAGED / EXACT` |
| 文件仍是修改状态，但内容变了 | `MANAGED / DRIFTED`（不认为安全，先 refresh 或确认） |
| 登记项在磁盘上已不存在 | `MISSING`（可能你自己还原了；提示 refresh/unregister） |
| git 里存在、登记表里没有 | `UNKNOWN` → **更新前阻断** |
| 忽略文件（build/、*.log…） | 只展示，永不阻断 |

`Override Safety` 取 `PASS` / `WARN` / `FAIL` / `UNKNOWN`：
`FAIL` = 有未登记修改；`WARN` = 有漂移或登记项消失；`UNKNOWN` = 登记表损坏或 git 不可用
（"无法验证"从不等于"安全"）。

### 18.4 门禁的变化

```
dirty
├─ 只有已登记且未变的定制   -> PASS
├─ 有漂移                   -> BLOCK（block_drifted_overrides，可关）
└─ 有任何未登记修改         -> BLOCK
```

登记表为空时行为与第三阶段完全一致（任何修改都阻断）——老用户不会被静默改变行为。

### 18.5 更新时的保全流程

```text
1  preflight            2  备份 HERMES_HOME        3   快照 override patch
4  记录 commit/branch/哈希  5  验证 patch 可读      6   仅把登记路径还原为上游内容
7  hermes update         8  取新 HEAD             9   git apply --3way 重新应用
10 验证结果             11  smoke test           12  写入新的 base
```

事务状态写进 `update_state.json`：`PREPARED → CLEANED → UPDATED →
OVERRIDES_REAPPLIED → VERIFIED → COMMITTED`。中途崩溃时，下次运行会告诉你
**停在哪一步**，而不是让你猜。

安全红线（代码与测试都强制）：

* **绝不**为了更新成功而销毁你的修改；不使用 `git reset --hard`；
* 只有确认"所有 dirty 都属已登记"才动工作区（否则 ABORT）；
* 冲突时 `git apply --3way` 失败就停下来：保存 patch、保存冲突信息、
  **不自动 ours/theirs**，交给你人工合并；
* 更新本身失败时，优先恢复旧 commit / 旧工作区 / 旧 override patch。

### 18.6 与其他命令的联动

* `rollback`：先收起已登记定制，checkout 回旧版本后**重新应用 patch**——不会只回滚 Hermes 而丢掉你的定制；
* `health`：登记表完整性、patch 哈希、托管文件状态、未完成事务；
* `preflight`：登记表/patch 可写/git 可用/未登记修改；
* `watch`：4 个稳定定制**不会**每天打扰你；只有漂移、新出现的未登记修改、
  或冲突预测变差才通知。
* `report` / `check`：新增「本地定制」一节（`Managed / Unknown / Drifted / Override Safety`）。

### 18.7 真实数据（本机实测）

同一台机器、同一棵工作区（4 个本地修改），只切换"是否已登记"：

| | 未登记 | 已登记 |
|---|---|---|
| Working Tree | 有本地修改（已登记 0，未登记 4） | 有本地修改（已登记 4，未登记 0） |
| 门禁 | `BLOCK 工作区必须干净` | `PASS 工作区必须干净` |
| 结论 | **BLOCKED** | **ACCEPTABLE** |

`huc overrides diff --target v2026.9.14` 预测：**4 HIGH / 0 MEDIUM / 0 LOW**
（上游没有改动这些文件的同一区域 → 重新应用把握 HIGH）。

### 18.8 GitHub compare 404 与本地 git

旧版本在 GitHub compare 返回 404 时（本地提交未推送、来自其他 remote、tag 未 fetch）
会把安装判成"非标准状态 → MANUAL_REVIEW"。第四阶段改为：

```text
GitHub compare 不可用 -> 本机 git rev-list --left-right --count <目标>...HEAD
                       -> 拿不到目标对象库时：对照 origin/main 给出本地领先/落后
                       -> 仍然未知：如实说明，但绝不因此判定安装异常
```

安装状态也细化成 `STANDARD_RELEASE` / `MAIN_CLEAN` / `MAIN_WITH_MANAGED_OVERRIDES` /
`CUSTOM_COMMIT` / `FORK` / `DETACHED` / `UNKNOWN`；**只有** 以下情况才进 MANUAL_REVIEW：
仓库无法识别、定制漂移、未登记修改、预测冲突 LOW、本机 git 历史无法解析、事务中断。

## 许可

MIT
