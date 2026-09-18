# AGENTS.md — how to work in this repository

Guidance for AI agents (and humans) touching `hermes-update-check`. Read this first,
then `docs/CODE_MAP.md`; only open source files once you know which ones you need.

## What this is

A safety-first **advisor** for updating Hermes Agent: it inspects the local install and
the published releases, weighs what *you* actually use (usage profile), checks whether
your critical workflows are still usable, applies the few blocking rules and then says
`SAFE` / `ACCEPTABLE` / `WAIT` / `BLOCKED` (plus `AHEAD_OF_STABLE` / `MANUAL_REVIEW` /
`INSUFFICIENT_DATA`). It also backs up, updates (only after an explicit `y`) and can
roll back. Global risk is background information, never the decision.

### Invariants — do not break these

1. **Nothing updates Hermes without an explicit confirmation.** `check`, `report`,
   `watch`, `preflight`, `health` and `config` are read-only. `update`/`rollback` ask
   (default answer: no) unless `--yes`/`auto_update: true` is set by the user.
2. **Missing data is never reported as safe.** Unknown parts count at a floor, the score
   becomes a lower bound, and an incomplete observation window degrades the verdict to
   `WAIT — INSUFFICIENT OBSERVATION DATA`.
3. **Exit codes are a public contract** (`errors.py`): 0 ok / up-to-date / `SAFE` /
   `ACCEPTABLE` · 1 internal · 2 usage · 3 config · 10 `WAIT` / `BLOCKED` /
   `MANUAL_REVIEW` · 11 insufficient data · 12 health check failed · 13 aborted ·
   14 preflight failed. (Legacy `UPDATE` / `AVOID` values are still understood.)
4. **No plaintext secrets anywhere.** Configuration carries environment-variable
   *names*; values come from the environment at runtime. See `SECURITY.md`.
5. **The test suite never touches the network** and never runs `hermes update`.

## Layout

| path | what lives there |
|---|---|
| `src/hermes_update_check/` | the package (see `docs/CODE_MAP.md` for every module) |
| `tests/` | 467 offline tests; fake GitHub client in `tests/conftest.py` |
| `docs/CODE_MAP.md`, `docs/index.json` | generated code map for agents (see below) |
| `scripts/build_index.py` | regenerates `docs/CODE_MAP.md` + `docs/index.json` |
| `scripts/scan_secrets.py` | secret/privacy scanner (pre-commit hook + CI) |
| `install.sh` / `install.ps1` | one-click installers |
| `README.md` | user-facing documentation (Chinese), incl. the risk model |
| `CHANGELOG.md` | what changed, per version |

Runtime state lives **outside** the repo: `~/.hermes-update-check/` (cache, snapshots,
logs, `update_state.json`) and the config at `~/.config/hermes-update-check/config.yaml`
(Windows: `%APPDATA%\hermes-update-check\config.yaml`).

## Setup

```bash
uv venv .venv                                     # or python -m venv .venv
uv pip install -e ".[dev]" --python .venv/bin/python
git config core.hooksPath .githooks               # enables the pre-commit secret scan
```

Windows: the interpreter is at `.venv\Scripts\python.exe` and the command at
`.venv\Scripts\hermes-update-check.exe`. Console encoding is handled by the CLI itself.

## The loop

```bash
.venv/bin/python -m pytest -q                         # 467 tests, must stay green, offline
.venv/bin/python -m pytest --cov --cov-fail-under=80  # coverage gate (currently 84%)
.venv/bin/python -m ruff check .                      # lint gate (0 findings)
.venv/bin/python -m ruff format --check .             # formatting gate
.venv/bin/python scripts/build_index.py               # after touching the public surface
.venv/bin/python scripts/scan_secrets.py --staged     # before committing (hook does this)
```

Pre-push checklist: tests green · ruff lint+format clean · coverage ≥ 80% · index regenerated ·
scanner clean · `CHANGELOG.md` updated for user-visible changes · version bumped for a
release (`pyproject.toml` + `__init__.py`, then tag `vX.Y.Z`).

## Cutting a release

1. bump the version in **both** `pyproject.toml` and `src/hermes_update_check/__init__.py`
   (a test asserts they match) and add the matching `CHANGELOG.md` section - that section
   becomes the release notes;
2. run the checklist above;
3. `git tag -a vX.Y.Z -m "<one line>" && git push origin vX.Y.Z`;
4. `uv build`, then
   `gh release create vX.Y.Z --title "..." --notes-file <changelog section> dist/*`;
5. verify the way a stranger would: download the wheel from the release URL, install it in
   a fresh venv, run `hermes-update-check --version`. `gh release view` only proves the
   release exists, not that the artifact installs.

Currently released: **v1.2.0** (wheel + sdist attached; phase-3 decision model).

## Conventions

- **Everything user-visible is bilingual.** Route text through `console` / `i18n` with
  zh/en pairs (`tr.t("中文", "English")`), never a bare string.
- **A new risk factor** = a named factor with zh/en reasons, a cap in `RiskWeights`, a
  line in the README table and tests for the boundaries.
- **A new blocking rule is a deliberate act.** Exactly five things may block
  (`block_on_systemic_risk`, `block_on_critical_workflow`, `block_on_rollback_safety`,
  `block_dirty_worktree`, `block_on_insufficient_data`). Everything else is a *caution*
  that caps the verdict at `ACCEPTABLE` - add a `warn_*` rule instead of a blocker, and
  give it a config key in `gates.py` + `config.example.yaml` + README + tests
  (blocking, warning and pass paths).
- **A new usage-profile feature/provider** = an entry in `usage_profile.py`'s catalogue
  (zh/en name, detection evidence) + a mapping in the cluster -> feature attribution +
  tests. Unknown entries must resolve to `important` (features) or the group default
  (providers), never to `unused` - silence is not evidence of disuse.
- **A new notification channel** = one file under `notify/`, registered in
  `build_notifiers()`; keep secrets in environment variables.
- **Be conservative about data, not about the user's time**: when in doubt the answer is
  `WAIT`, but a bug in a feature the user marked `unused` must contribute exactly 0, and
  a blocker the user cannot act on (tracking `main`) is a caution, not a block. The tool
  must always explain *why* (which factor, which rule, which cluster).
- Windows compatibility is a feature: wrap subprocess launchers (`wrap_command`), probe
  interpreters by executing them, and never assume `/tmp` or a POSIX path separator.

## Regenerating the index

`docs/CODE_MAP.md` and `docs/index.json` are generated - never edit them by hand. Run
`python scripts/build_index.py` after adding/removing/renaming modules, public functions
or CLI commands; `--check` (used by CI) fails when they are stale.
