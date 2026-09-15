# hermes-update-check

> **先检查 → 再评估 → 给建议 → 你确认 → 才更新。**
> 这个工具永远不会自己更新 Hermes。

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
7. [硬门禁 Hard Gates](#六硬门禁-hard-gates)
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
│   ├── clusters.py                # ★ 回归聚类：严重程度 × 独立性 × 可信度
│   ├── gates.py                   # ★ 硬门禁：规则优先于评分
│   ├── advisor.py                 # ★ 最终裁决：数据 → 环境 → 门禁 → 评分 → 观察期 → UPDATE
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

### 方式 C：手动安装（uv / pip）

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

> 依赖只有两个：`PyYAML`（配置文件）、`rich`（终端美化）。
> 网络请求、SQLite、JSON、子进程全部用标准库，`rich` 缺失时自动降级为纯文本。

---

## 三、使用命令

```bash
hermes-update-check check                 # 快速检查：一屏给结论（默认命令）
hermes-update-check report                # 完整报告：每个风险因子的加减分明细 + Issue 样本
hermes-update-check report --format markdown --output r.md
hermes-update-check report --format json  # 机器可读（cron/CI/看板）
hermes-update-check preflight             # 只看更新前检查（只读）
hermes-update-check health                # 只看健康检查（只读，加 --smoke 会真的调一次模型）
hermes-update-check watch                 # 定时检查：无变化不打扰，有变化才通知
hermes-update-check update                # 备份 → 更新 → 健康检查（需要确认，默认 N）
hermes-update-check update --dry-run      # 演练：显示 hermes update --plan，什么都不做
hermes-update-check rollback              # 回滚到 update_state.json 记录的版本
hermes-update-check config init           # 生成带注释的配置文件
hermes-update-check config show --json    # 查看生效配置（含环境变量覆盖后的结果）
hermes-update-check notify-test           # 测试 Telegram/Webhook 是否配置成功
```

常用参数：

| 参数 | 说明 |
|---|---|
| `--lang zh\|en` | 报告语言（默认取配置） |
| `--json` | 输出 JSON |
| `--plain` / `--no-color` | 纯文本 / 关颜色（`NO_COLOR=1` 也生效） |
| `--no-cache` | 忽略本地缓存，强制重新请求 GitHub |
| `--timeout 20` | 网络超时（秒） |
| `-v` / `-vv` | 打开日志（INFO / DEBUG） |
| `update --yes` | 跳过交互确认（脚本/自动化里才用） |
| `update --force` | 风险门禁不通过时仍继续（明确自担风险） |
| `update --branch main` | 指定更新分支 |
| `update --auto-rollback` | 健康检查失败时自动回滚 |

一次典型输出：

```
Hermes Update Report

  Current Version   v0.21.2 (2026.9.11)
  Latest Version    v0.21.3 (v2026.9.14)
  Released          2026-09-14T16:04:14Z (0.6 天 ago)
  Commits           1039+
  PRs               338+
  Releases behind   1
  Risk Score        78/100
  Risk Level        HIGH
  Stability         22/100
  Data confidence   100%

结论
  Update Risk   78/100  HIGH
  稳定性         22/100
  建议           建议暂缓 / 继续观察

主要风险 / 信号：
  - 涉及 Session / 状态存储（命中 12 次）
  - 涉及数据库 / 迁移 / schema（命中 9 次）
  - 新版本发布不足 24 小时
  - 本次发布包含大量 commit（1039）
  - 当前安装跟踪 main 分支，稳定性可能低于正式 Release

建议
  暂时不要更新。
  原因：
    - 风险评分 78 高于阈值 40
    - 新版本发布仅 0.6 天（低于观察期 5 天）
  建议继续观察 3 天，如果没有新的严重 Issue，再考虑升级。
  重新检查： hermes-update-check check   （或 watch 模式自动通知）
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

## 五、风险模型（组件化）

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

## 六、硬门禁（Hard Gates）

门禁是**规则**，不是分数：门禁触发时，即使风险分只有 8 分也不会建议更新。

```yaml
hard_gates:
  enabled: true
  minimum_release_age_hours: 48          # BLOCK：发布不足 48 小时
  block_main_branch_update: true         # BLOCK：跟踪 main 开发分支
  block_dirty_worktree: true             # BLOCK：工作区有未提交修改
  block_prerelease: true                 # BLOCK：预发布版本
  block_active_database_regression: true # BLOCK：活跃的数据库回归（open + 有佐证）
  block_active_session_regression: true  # BLOCK：活跃的 Session/数据丢失回归
  block_active_gateway_regression: false # 噪声较大，默认关闭但可开启
  block_active_update_failure: true      # BLOCK：活跃的「升级失败」回归
  block_on_insufficient_data: true       # BLOCK：观测数据不完整
```

裁决优先级（固定顺序，`advisor.py`）：

```
1. 无需更新（已最新 / 已领先 Release）
2. 数据不足            → WAIT — INSUFFICIENT OBSERVATION DATA
3. 本地环境异常        → MANUAL_REVIEW（先修环境）
4. Hard Gate 触发      → WAIT（附最早可重新评估时间）
5. 风险评分            → AVOID(≥81) / WAIT(>阈值)
6. 发布观察期未满      → WAIT（minimum_release_age_days）
7. UPDATE
```

报告会把门禁逐条列出（`BLOCK` / `WARN` / `PASS` / `SKIP`）并说明原因，例如：

```
硬门禁 ───────────────────────────────────────
  BLOCK Release 年龄 >= 48h
        Release 仅发布 15.2 小时，低于最小观察期 48 小时
  BLOCK 工作区必须干净
        工作区有 4 个未提交修改
  WARN  Channel 与 preferred_channel 不一致
        当前跟踪的是 MAIN，而 preferred_channel 是 stable：建议备份后切换到正式 Release（本工具不会自动降级）
```

`preferred_channel`（`stable` / `main` / `prerelease`，默认 `stable`）与实际 channel 不一致时产生 WARN，
但**绝不自动降级或切换分支**。

## 七、回归聚类与可信度

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
  "recommendation": "WAIT",
  "recommended_recheck": "2026-09-15T19:21:04Z",
  "recommended_recheck_hours": 12.0
}
```

`recommendation` 取值：`UPDATE` / `WAIT` / `AVOID` / `INSUFFICIENT_DATA` / `UP_TO_DATE` /
`AHEAD_OF_STABLE` / `MANUAL_REVIEW`（后两个是二阶段新增，`update_available` 布尔字段保留兼容）。

## 十三、退出码（cron / CI 用）

| 码 | 含义 |
|---|---|
| 0 | 成功：已是最新 / 已领先 Release（AHEAD_OF_STABLE），或风险可接受（UPDATE） |
| 10 | 检查完成，但建议 WAIT / AVOID / MANUAL_REVIEW（含 Hard Gate 触发） |
| 11 | 数据不足（INSUFFICIENT DATA） |
| 12 | 更新后健康检查失败（需要 rollback） |
| 13 | 用户取消 / 非交互环境未给 `--yes` |
| 14 | 更新前检查失败 |
| 1 / 2 / 3 | 运行错误 / 参数错误 / 配置错误 |

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
.venv/bin/python -m pytest -q          # 253 个测试，全部离线：不需要网络、不碰真实 Hermes 安装
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

## 许可

MIT
