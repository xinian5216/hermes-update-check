# Changelog

All notable changes to this project are documented here. The format loosely
follows [Keep a Changelog](https://keepachangelog.com/) and the project uses
semantic versioning.

## [1.3.1] - 2026-09-20

修复：交互式检查不再静默使用数小时前的磁盘缓存（用户报告：桌面快捷方式每次
检测都显示上一次的结果）。设计缺陷是 `check` / `report` / `watch` / `update`
与缓存的 TTL（默认 360 分钟）共用同一条缓存策略——缓存本是为 `watch` 的限流
保护设计的，却让手动检查拿到陈旧答案。

### Fixed

- `run_check` 对**决定性且廉价**的端点（release 列表、compare、tag commit）
  一律实时拉取（`fresh=True`）：跳过新鲜缓存读，成功后仍回写缓存，网络失败
  时照旧回退到陈旧缓存并如实标注 degraded。
- **昂贵的 issue 搜索/评论端点保留正常缓存 TTL**（限流保护不变）。
- 报告头部新增数据时效行：「更新数据：实时（刚从 GitHub 获取）」或
  「更新数据：缓存（约 N 分钟前获取，GitHub 不可达时的兜底）」；JSON 契约
  新增 `release_data_from_cache` / `release_data_age_seconds`。

## [1.3.0] - 2026-09-18

第四阶段：**Managed Local Overrides**（受管本地修改）+ GitHub compare 404 的本地 git 兜底。

核心原则：`Intentional local customization is not corruption.`
`Known dirty` 与 `unknown dirty` 不是一回事。

### Added

* **`overrides` 模块与命令**：`detect` / `status` / `list` / `diff` / `register` /
  `unregister` / `refresh` / `export` / `doctor`。`detect` 只展示、绝不自动登记；
  未跟踪文件必须显式指定。
* **登记表**（`~/.hermes-update-check/overrides/registry.json`）：记录真正的基线
  （base commit + base/current/patch 的 SHA256 + 策略），patch 用 `git diff --binary`
  保存（文本 / 删除 / 改名 / 二进制 / CRLF / 非 ASCII 文件名均已测试）。
* **分类**：`MANAGED/EXACT`、`MANAGED/DRIFTED`、`MISSING`、`UNKNOWN` 与
  `Override Safety`（PASS/WARN/FAIL/UNKNOWN）。忽略文件永不阻断。
* **冲突预测 Reapply Confidence**：比较 `base→你的定制` 与 `base→目标版本` 的 hunk，
  HIGH（上游没动这些文件）/ MEDIUM（同文件不同区域）/ LOW（同区域，需人工合并）；
  `overrides diff --target vX.Y.Z`（可选 `--fetch`）。
* **事务式更新**：`update_state.json` 记录 `PREPARED → CLEANED → UPDATED →
  OVERRIDES_REAPPLIED → VERIFIED → COMMITTED`，中断后下次运行能报出停在哪一步。
* **更新保全流程**：快照 patch → 仅把登记路径还原为上游内容（`git restore`，
  非 `reset --hard`）→ 更新 → `git apply --3way`（失败回退普通 apply）→ 健康检查；
  更新失败时恢复定制；冲突时保存 patch 与冲突信息，绝不自动 ours/theirs。
* **安装状态分类**：`STANDARD_RELEASE` / `MAIN_CLEAN` / `MAIN_WITH_MANAGED_OVERRIDES` /
  `CUSTOM_COMMIT` / `FORK` / `DETACHED` / `UNKNOWN`；fork / 本地未推送提交不再被判为异常。
* **本地 git 兜底**：GitHub compare 404（本地提交未推送 / tag 未 fetch / fork）时，
  用 `git rev-list --left-right --count` 与 `origin/main` 对照给出 ahead/behind；
  取不到发布 commit 时如实说明"距离未知"，但不再进入 MANUAL_REVIEW。
* `report` / `check` 新增「本地定制」一节；JSON 契约新增 `local_overrides`、
  `install_state`、`recommendation_detail` 的相关字段。
* 配置新增 `local_overrides` 一节（`enabled` / `block_unknown_changes` /
  `block_drifted_overrides` / `auto_preserve` / `reapply` / `backup`）。

### Changed

* **dirty worktree 门禁按"已知/未知"分流**：只有已登记且未变的定制不再阻断；
  漂移与未登记修改仍然阻断（可配置）。登记表为空时保持第三阶段行为。
* `rollback` 不再用 `git stash` 粗暴收起工作区：先收起已登记定制，回滚后重新应用 patch。
* `preflight` / `health` 增加定制完整性、patch 哈希、patch 目录可写、事务状态检查。
* `watch` 对稳定的受管定制保持安静，只在漂移 / 新增未登记修改 / 冲突预测变差时通知。
* `update --dry-run` 输出本地定制保全计划。

### Security

* 保存与导出 patch 前跑密钥扫描；发现疑似密钥只告警并限制 `export` / 日志 / 报告输出，
  **不删除** patch，也不把内容打印出来。导出是本机操作，永不自动上传。

### Fixed

* GitHub compare 404 不再把"本地提交未推送"翻译成"安装状态异常"。
* `console.kv`（不存在的 API）导致 `overrides status` 与报告新节点崩溃。
* 报告里超长路径会挤掉 rich 表格的值列（状态显示为空）。

## [1.2.0] - 2026-09-18

第三阶段：决策模型从「全局风险驱动」改为「用户影响 + 核心功能可用性驱动」。

### Added

* **使用画像 `usage_profile`**（`usage_profile.py`）：功能与 Provider 的使用等级
  `critical` / `important` / `optional` / `unused`（权重 1.0 / 0.6 / 0.25 / 0），
  以及 `profile show|detect|edit` 命令。自动检测只区分「已配置 → important」与
  「未配置 → unused」，从不替你判断 critical，也只读配置的**键名**与环境变量**名字**。
* **`Personal Impact` 与 `Core Feature Readiness`**（`impact.py`）：个人影响用线性权重，
  可用性用阻尼权重，且只有"报告称该功能不可用"才会拉低可用性；一个未使用功能的 Bug
  贡献恰好 0。
* **`Systemic Critical Risk`**：数据损坏 / Session 丢失 / 凭证丢失 / 配置损坏 / 安装损坏 /
  回滚失败 / 无法启动 / 全部 Provider 不可用 —— 与画像无关，但需要高可信度 + 独立佐证才阻断。
* **`Rollback Safety`**（`rollback_safety.py`）：更新前探测回滚路径（提交是否可达、
  `update_state.json`、状态目录可写、磁盘、venv、备份支持），`FAIL` 会阻断执行。
* **回归聚类新增 `affected_features`**、按 Provider 细分归因、重复报告折叠
  （标题相似度 / 显式引用 / duplicate 标签）与相关性折扣（primary 1.0 / secondary 0.4 / tertiary 0.2）。
* **两种时钟**：`Next Monitoring Check` 与 `Earliest Policy Clearance` 分开报告。
* 报告新增 `PERSONAL READINESS` / `YOUR CORE FEATURES` / `KNOWN ISSUES`（含 `UNUSED` 标记）/
  `SYSTEMIC RISKS` 四个区块；JSON 契约新增 `personal_readiness`、`rollback_safety`、
  `usage_profile`、`policy_clearance*`。
* 配置迁移：旧的 `hard_gates` 键仍然可用，加载时给出 `Deprecated: … Use: …` 提示。

### Changed

* **阻断门禁精简为五条**：系统级风险、已确认的关键工作流回归、回滚路径不可用、
  本地工作区不干净、观测数据不足。
* **发布年龄改为分段策略**（`release_age_policy`）：`<6h` 阻断、`6-12h` 谨慎、
  `12-24h` 可接受、`>24h` 正常 —— 不再有 48 小时墙。
* **main 分支与预发布版本只提示**（计入 Environment Risk），可用
  `block_main_branch_update` / `block_prerelease` 恢复旧行为。
* **普通 Gateway / MCP / Provider / 认证 / 崩溃回归只提示**，通过个人影响与可用性计分。
* **结论等级改为 `BLOCKED` / `WAIT` / `ACCEPTABLE` / `SAFE`**；全局风险只作为背景信息，
  最多把结论限制在 ACCEPTABLE。
* `watch` 按**结论变化**与**你的关键工作流**变化通知；全局风险在结论不变时不再打扰。
* `minimum_release_age_days` 不再延迟建议，只把结论限制在 ACCEPTABLE。

### Fixed

* 三个真实数据误报：把"为防止数据库损坏而中止"读成数据库损坏、把"两个会话被这次折腾耽误了"
  读成 Session 丢失、把"难以区分是不是启动崩溃"读成启动崩溃。
* Session / CRASH 两类聚类模式收紧（"session restore 缺少某段错误处理"不再被判为严重）。

### Migration

* 无需改配置即可升级；`config.example.yaml` 里有新节的完整注释。
* 建议运行一次 `hermes-update-check profile detect`，再把你真正依赖的功能设为 `critical`
  （尤其是你日常使用的 CLI / Session / 常用 Provider）。

## [1.1.0] - 2026-09-15

### Added

* **Code index for agents** (`AGENTS.md`, `docs/CODE_MAP.md`, `docs/index.json`,
  `scripts/build_index.py`): every module's purpose, public symbols and line numbers,
  generated from the source with `ast` and verified in CI (`--check`), so an agent can
  find its way without reading the whole tree.
* **Quality gates in CI**: `ruff check` + `ruff format --check` and a coverage floor
  (`--cov-fail-under=80`, currently 83%) on the Ubuntu/Python 3.12 leg.
* **Code provenance** (`provenance.py`, `channel`): the tool no longer trusts
  `hermes --version` alone. It reports branch / commit / nearest release tag /
  ahead-behind counts / dirty worktree and classifies the install as `STABLE`,
  `MAIN`, `PRERELEASE`, `DETACHED`, `CUSTOM` or `UNKNOWN`. A main-tracking install
  that is *ahead* of the latest stable release is reported as "not an update
  candidate" instead of "update available".
* **Component risk model**: `Change Risk`, `Regression Signal`, `Data Confidence`,
  `Environment Risk`, combined into `Overall Risk` = 1-(1-change)(1-regression)(1-environment)
  minus a stability bonus.
* **Hard gates** (`gates.py`): rules that override the score — release age
  (`minimum_release_age_hours`), main branch, dirty worktree, prerelease, active
  database/session/update-failure regressions, insufficient data. Verdict priority is
  data → environment → gates → score → cooling period → `UPDATE`.
* **Regression clustering** (`clusters.py`): nine classes (database, session,
  gateway, config migration, update failure, auth, crash, MCP, provider) graded by
  severity × confidence, where confidence comes from independent reporters,
  maintainer confirmation, linked PRs, reproduction steps, version mentions and
  title-signature agreement.
* **Advisor** (`advisor.py`): one explainable verdict
  (`UPDATE` / `WAIT` / `AVOID` / `INSUFFICIENT_DATA` / `UP_TO_DATE` /
  `AHEAD_OF_STABLE` / `MANUAL_REVIEW`) with the deciding layer, the blocking gates
  and a recommended recheck time.
* **Watch deltas**: notifications only for meaningful transitions (new release,
  risk band change, gate triggered/cleared, CRITICAL cluster appeared/resolved,
  channel change, WAIT→UPDATE) — not for score wobble.
* **One-click installers**: `install.sh` (Linux/macOS/WSL/git-bash) and
  `install.ps1` (Windows), plus a copy-paste prompt for an AI agent in the README.
* **Secret/privacy guard**: `scripts/scan_secrets.py` with a pre-commit hook, a CI
  job (full history + gitleaks) and outbound text mode (`--text`, `--stdin`);
  `SECURITY.md` documents the policy.
* **JSON contract** extended with `local` provenance, `risk` components,
  `hard_gates`, `regressions`, `recommendation`, `recommended_recheck`.
* Report rebuilt as `Hermes Update Advisor` (local installation / latest stable /
  risk / hard gates / regression signals / recommendation).

### Changed

* The whole codebase is formatted with `ruff format` (no behaviour change) and now
  lints clean under the configured rule set.
* Missing data never lowers risk any more: unknown parts count at a floor, the
  score is published as a lower bound, and an incomplete observation window makes
  the verdict degrade to `WAIT — INSUFFICIENT OBSERVATION DATA`.
* A zero cache TTL now means "never fresh" (was: a same-instant cache hit).
* Windows-style paths are caught by the privacy scanner with either separator.

### Fixed

* Dead code removed (unused imports/variables) as found by the new lint gate; the
  `UNKNOWN_REGRESSION_FLOOR` re-export is now explicit so the public surface is stable.
* Printing Chinese help text or a report on a Windows console with a legacy code
  page (cp1252/cp936) raised `UnicodeEncodeError` and exited 1; the CLI now
  switches the console to UTF-8 and reconfigures its streams with
  `errors="replace"`, so output degrades gracefully instead of crashing.
  * Found by the new CI installer job, which runs the tool under a plain
    PowerShell console after installing it.
* Date-style release tags (`v2026.9.14`) are no longer compared as versions.
* The local commit is compared against the release tag (a checkout can be *behind*
  the tag while the remote branch is hundreds of commits ahead).
* Clustered regressions are damped (0.6 per cluster, scaled by issue-signal
  confidence) so a noisy repository cannot pin the signal at 100.
* Fresh cache hits are no longer marked as degraded data.

## [1.0.0] - 2026-09-15

### Added

* Initial release: version/install/git detection, GitHub release + compare +
  issue analysis, keyword/age/volume risk scoring, natural-language
  recommendation, preflight checks, post-update health checks, backup, rollback,
  watch mode with Telegram/webhook notifications, YAML configuration, JSON and
  Markdown output, 146 offline tests.
