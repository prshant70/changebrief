# ChangeBrief

A repo-aware companion for AI-assisted development — both ends of the loop:

- **Before changes**, give your coding agent the right context. `ai-context`
  generates per-agent guidance files (`CLAUDE.md`, `CURSOR.md`, `CODEX.md`)
  from real repo signals — and can learn your internal frameworks once and
  apply that knowledge across every repo that uses them.
- **Before merges**, turn the Git diff into a pre-merge validation report.
  `validate` tells you what actually changed, which areas are most likely to
  break, what to verify, and whether it's safe to merge — with an exit code
  you can wire into CI.

ChangeBrief is **deterministic-first**. The static stages — repo scanning,
change analysis, risk classification, framework extraction — make zero
network calls. The LLM is asked only for what an LLM is uniquely good at
(synthesising idiomatic guidance, behavioural impact, validation planning),
its output is JSON-schema enforced, every claim it makes must cite a real
file path which ChangeBrief verifies on disk, and prompts go through secret
redaction at the LLM boundary so the tool stays safe to run against private
repositories.

It is intentionally **not** a test generator and **not** an autocomplete —
it's a thin, repo-aware layer between you, your coding agent, and your
reviewer.

---

## Two capabilities, one CLI

### 1. `ai-context` — give your coding agent the right context

```bash
# Generate CLAUDE.md / CURSOR.md / CODEX.md from real signals in the current repo
changebrief ai-context init

# Teach ChangeBrief about an internal framework once; every consumer repo benefits
changebrief ai-context build --path /path/to/torpedo
```

`init` scans the repo (language, declared dependencies, top-level *and*
nested directories, frequently-imported packages, license, CI, file-naming
patterns) and writes per-agent guidance files at the repo root, wrapped in
safe re-run markers so anything you add outside the markers is preserved.

`build` is the second half of the loop: it AST-walks an internal framework's
source — public API from `__init__.py` `__all__`, exception hierarchy,
notable subdirectories, `config.json` keys, the `requires-python` pin — and
adds a citation-bearing entry to `~/.changebrief/context.yaml`. From then
on, every repo that imports the framework gets framework-aware bullets in
its generated `CLAUDE.md` / `CURSOR.md` / `CODEX.md` automatically. See
[AI context files](#ai-context-files) for details.

### 2. `validate` — pre-merge change report

```bash
changebrief validate --base main --feature my-branch
```

When configured with an API key, you get a structured pre-merge report:

```
🧭 Change Intent:
Intentional (High Confidence)

🎯 Analysis Confidence:
High
- Clear structural change detected (declarations / route handlers)
- Change is localized

🔍 Behavioral Impact:
Adds a new public endpoint POST /v1/orders and routes merchants without
a cached token through the dynamic-token issuer.

💥 Change-Induced Risks:

🔥 HIGH RISK:
- Change: OrdersController.create
  Impact: alters handling of merchants without cached tokens; adds a new
  dependency on TokenIssuer.fetch().

🧪 Suggested Validations:

🔥 1. Merchant with no cached token issues a fresh order.
   → Expect: routed via TokenIssuer.fetch(), HTTP 200 with an order id.

🚨 Merge Risk: HIGH
```

Without an API key (or with `CHANGEBRIEF_DISABLE_LLM=1`), the LLM stages are
skipped and you get a deterministic, minimal report with `merge_risk: medium`
and one catch-all "verify manually" suggestion. The local heuristics layer
(intent, risk types, confidence) always runs.

---

## Status

- Currently supported LLM provider: **OpenAI**. Other names accepted by the
  config schema are placeholders for future support.
- Default model: `gpt-4o-mini`. Override via `default_model`.
- Tested on macOS and Linux, Python 3.10+.
- The CLI surface and exit codes are stable for `0.2.x`.

---

## Install

Requires **macOS or Linux**, **Python 3.10+**, and **Git**.

The installer prefers `pipx` for an isolated install and falls back to
`pip --user`.

Latest from `main`:

```bash
curl -fsSL https://raw.githubusercontent.com/prshant70/changebrief/main/install.sh | bash
```

Pinned to a specific ref (recommended for CI):

```bash
CHANGEBRIEF_REF=v0.2.0 \
  curl -fsSL https://raw.githubusercontent.com/prshant70/changebrief/v0.2.0/install.sh | bash
```

From source:

```bash
git clone https://github.com/prshant70/changebrief.git
cd changebrief
pip install -e ".[dev]"
```

---

## First use

```bash
# Interactive setup: prompts for provider, model, and API key.
changebrief init

# Generate per-agent context files in the current repo
changebrief ai-context init

# Optional: teach ChangeBrief about a custom internal framework, once
changebrief ai-context build --path /path/to/torpedo

# Run a pre-merge validation report against any base/feature pair
changebrief validate --base main --feature my-branch

# `--path` works on every command, so you can stay outside the repo
changebrief ai-context init --path /path/to/repo
changebrief validate --base main --feature my-branch --path /path/to/repo

# Inspect or re-validate your config
changebrief config show
changebrief config check
```

---

## Commands

```bash
# AI context — give your coding agent the right context
changebrief ai-context init    [--path /path/to/repo] [--targets claude|cursor|codex]
                               [--enrich] [--dry-run]
changebrief ai-context build   --path /path/to/custom-framework
                               [--name <pkg>] [--description "..."] [--note "..."]
                               [--enrich/--no-enrich] [--force] [--dry-run]

# Pre-merge validation — analyse a Git diff
changebrief validate --base <ref> --feature <ref>
                  [--path /path/to/repo]
                  [--nocache]
                  [--format pretty|json|markdown]
                  [--fail-on never|low|medium|high]
                  [--dry-run]

# Setup / config
changebrief init
changebrief config show
changebrief config check

# Local cache (per repo, commit pair, model, and prompt version; 7-day TTL)
changebrief cache list
changebrief cache purge --expired
changebrief cache purge --all
```

### Notable flags

| Flag | Used by | Description |
| --- | --- | --- |
| `--path`, `-p` | `ai-context`, `validate` | Point at a repo outside your current directory. |
| `--targets` | `ai-context init` | Which agent files to emit (`claude` / `cursor` / `codex`, repeatable). |
| `--enrich` / `--no-enrich` | `ai-context build`, `ai-context init` | Toggle the LLM synthesis pass. |
| `--base` / `--feature` | `validate` | Required. Any valid Git ref (branch, tag, SHA). |
| `--nocache` | `validate` | Ignore the local cache and re-run the full pipeline. |
| `--format` | `validate` | `pretty` (default, emoji), `json` (CI-friendly), or `markdown` (PR comments). |
| `--fail-on` | `validate` | Exit non-zero when `merge_risk` is at or above this level. One of `never` (default), `low`, `medium`, `high`. |
| `--dry-run` | All | Preview output without writing files / calling the LLM / committing the entry. |

---

## AI context files

Coding agents (Claude, Cursor, Codex) produce much better changes when the
repo ships its own guidance file. `changebrief ai-context init` generates those
files from the repo itself — no hand-written boilerplate, no LLM-only fluff.

```bash
# Generate CLAUDE.md, CURSOR.md, CODEX.md in the current repo
changebrief ai-context init

# Or point at a repo outside your current directory
changebrief ai-context init --path /path/to/repo

# Pick a subset; preview without writing
changebrief ai-context init --targets cursor --dry-run

# Optional LLM polish on top of the deterministic context (off by default)
changebrief ai-context init --enrich
```

The generator scans real repo signals — language, declared dependencies,
top-level *and* nested directories, frequently-imported packages, license,
CI, file-naming patterns — and composes an evidence-backed context file.
Each section includes the path or import that justifies it. The output is
wrapped in `<!-- changebrief:ai-context:start v1 -->` /
`<!-- changebrief:ai-context:end v1 -->` markers so you can re-run safely;
anything outside the markers is preserved.

### Teaching ChangeBrief about a custom framework

Run `ai-context build` once against the framework's own repo. The command
extracts a *rich*, citation-bearing entry for the home-level
`~/.changebrief/context.yaml` so future `init` runs in any consumer repo
get framework-specific guidance instead of generic boilerplate.

```bash
# Scan the framework's repo and add it to ~/.changebrief/context.yaml
changebrief ai-context build --path /path/to/torpedo

# Skip the LLM pass (faster; deterministic baseline only)
changebrief ai-context build --path /path/to/torpedo --no-enrich

# Override the auto-detected name / description and append usage notes
changebrief ai-context build --path /path/to/torpedo \
  --name torpedo \
  --description "Torpedo — async microservice chassis built on Sanic." \
  --note "Open a 1mg-Service-Templates PR before bumping the Torpedo pin."

# Preview the YAML diff without writing
changebrief ai-context build --path /path/to/torpedo --dry-run
```

The pipeline runs in two phases:

1. **Deterministic extraction** (always on) — AST-walks the framework
   source for the public API surface (from `__init__.py` `__all__`),
   the exception family, decorator candidates, notable subdirectories
   (`api_clients/`, `circuit_breaker/`, `middlewares/`, …), `config.json`
   keys, the `requires-python` pin, and the smallest example under
   `examples/`. Each fact lands in `notes:` with a real file path attached.
2. **LLM synthesis** (default; `--no-enrich` to skip) — feeds the
   verified facts plus the README, docs, and example files to the
   configured LLM through the same redaction + JSON-schema pipeline the
   rest of `changebrief` uses. The model returns a polished framework
   description, related-frameworks entries (e.g. Sanic for Torpedo,
   aiohttp for the API-client base), and idiomatic `do:` / `dont:` /
   `notes:` bullets. **Every bullet must cite a path inside the framework
   repo**; bullets whose citations don't resolve on disk are dropped
   before write.

Outputs are cached in `~/.changebrief/cache/ai-context-build/` keyed on the
framework root, model, prompt version, and a digest of the cited files —
unchanged repos are free to re-run. The command refuses to overwrite an
existing framework entry unless `--force` is passed, and unrelated keys
(`do:` / `dont:` / `notes:` you've curated by hand) are preserved.

`--enrich` calls the configured LLM to add a polished overview and a few
*Inferred conventions / Gotchas* bullets. It's strictly bounded:

- JSON-schema enforced output;
- every claim must cite an `evidence_path`, which ChangeBrief verifies on disk
  before keeping it (hallucinated paths are dropped);
- prompts go through the same redaction as `validate`;
- responses are cached on a content digest;
- failures fall back silently to the deterministic file.

### Customising what gets written

Two layers of overrides, both optional, both YAML. Both layers apply when
present — the per-repo file extends (rather than fully shadows) the per-user
file. Repo-level entries win on conflicting keys; lists are concatenated and
de-duplicated.

| Path                                     | Scope     | Use for                                                   |
| ---------------------------------------- | --------- | --------------------------------------------------------- |
| `<repo>/.changebrief/context.yaml`         | per-repo  | Repo-specific summary, `do` / `dont` / `notes`.           |
| `~/.changebrief/context.yaml`              | per-user  | Org-wide framework descriptions and conventions.          |

Schema (all keys optional):

```yaml
project_summary: "Notification core service."
frameworks:
  torpedo: "Torpedo (v6) — 1mg's microservice chassis built on Sanic."
do:
  - "Return responses through `from torpedo import send_response`."
dont:
  - "Don't `print()` in handlers — bypasses structured logging."
notes:
  - "Response envelope: `{is_success, status_code, data|error, ...}`."
```

The home-level file is the right place to teach ChangeBrief about a custom
framework once, so every repo using it picks up the same description.

---

## `validate` output formats

Pretty (default) is the human-readable emoji format shown above.

JSON (`--format json`) is suitable for CI consumption:

```json
{
  "intent":     { "label": "intentional", "score": 0.85, "signals": ["..."] },
  "confidence": { "level": "High",        "score": 0.85, "reasons": ["..."] },
  "plan": {
    "behavioral_impact": "...",
    "risks":       [{ "level": "high",    "change": "...", "impact": "..." }],
    "validations": [{ "priority": "high", "scenario": "...", "expected": "..." }],
    "merge_risk":  "high",
    "notes":       []
  }
}
```

The `plan` object always conforms to the JSON Schema defined in
`changebrief.core.llm.validation_planner.VALIDATION_PLAN_SCHEMA`, enforced
end-to-end via OpenAI's structured-output mode. If the model ever returns a
non-conforming response, ChangeBrief falls back to a typed default with a `notes`
entry explaining why — it never emits malformed JSON.

Markdown (`--format markdown`) is a drop-in for PR comments and CI summaries.

---

## CI / pre-merge gating (`validate`)

Use the exit code to gate your merge.

| Exit code | Meaning |
| --- | --- |
| `0`  | Success (or `merge_risk` was below `--fail-on`). |
| `2`  | Validation error (not a Git repo, invalid ref, missing path). |
| `3`  | Config error (missing or invalid `~/.changebrief/config.yaml`). |
| `30` | Merge-risk gate tripped (`--fail-on` threshold met or exceeded). |
| `1`  | Unexpected error. |

GitHub Actions example:

```yaml
- name: Install ChangeBrief
  run: pipx install changebrief

- name: Configure ChangeBrief
  run: |
    mkdir -p ~/.changebrief
    cat > ~/.changebrief/config.yaml <<EOF
    llm_provider: openai
    llm_api_key: ${{ secrets.OPENAI_API_KEY }}
    default_model: gpt-4o-mini
    log_level: INFO
    EOF

- name: Pre-merge validation
  run: |
    git fetch origin ${{ github.base_ref }}
    changebrief validate \
      --base origin/${{ github.base_ref }} \
      --feature ${{ github.sha }} \
      --format json \
      --fail-on high \
      | tee pre-merge.json
```

A `--fail-on high` job fails the build only when the report says HIGH; the
report is preserved as an artifact regardless.

---

## Privacy & data handling

ChangeBrief is designed to be safe to run against private repositories.

**What stays local**

- Your repo, the diff, scanned source files, and the on-disk caches
  (`~/.changebrief/cache/...`) never leave your machine.
- All deterministic stages — repo scanner, AST extractor, change analyzer,
  risk / intent / confidence classifiers — make zero network calls.

**What is sent to the configured LLM provider**

Only the parts each LLM stage needs (the diff for `validate`; sampled source
files for `ai-context init --enrich`; public-API + README + examples for
`ai-context build`), **after redaction** of:

- PEM private keys (`-----BEGIN ... PRIVATE KEY-----` blocks),
- JWTs and `Authorization: Bearer ...` headers,
- OpenAI / GitHub / Slack / Stripe / AWS Access Key IDs / Google API keys,
- generic `key: value` secrets (`api_key`, `secret`, `password`, `token`,
  `access_token`, `auth_token`, `passwd`),
- email addresses.

Redaction runs at the OpenAI boundary on every prompt and every tool result —
the same code path is shared by `validate`, `ai-context init --enrich`, and
`ai-context build`. The placeholder is `[REDACTED:KIND]`, so the model still
has enough structure to reason about the input without seeing the secret.

**Verify before sending anything**

```bash
# Validate: prints the redacted diff and prompt that would go to the LLM
changebrief validate --base main --feature my-branch --dry-run

# ai-context build: prints the YAML diff that would be written
changebrief ai-context build --path /path/to/framework --dry-run

# ai-context init: prints the per-agent files that would be written
changebrief ai-context init --dry-run
```

**Disabling the LLM entirely**

Set `CHANGEBRIEF_DISABLE_LLM=1` (or simply leave `llm_api_key` empty). Every
command still produces useful output: `validate` falls back to deterministic
heuristics, `ai-context init` skips enrichment but writes the evidence-backed
context, and `ai-context build` writes a deterministic-only entry from
public-API and exception signals.

---

## How it works

Both capabilities follow the same shape: a deterministic core that always
runs, plus an optional LLM pass that's bounded, schema-enforced, and
citation-verified.

### `ai-context` pipeline

```
Repo on disk
  → Repo Scanner         deterministic — language, deps, dirs (top + nested), CI, license, naming
  → Layered Config       deterministic — merges <repo>/.changebrief/context.yaml + ~/.changebrief/context.yaml
  → Composer             deterministic — drops any claim not backed by an Evidence record
  → Renderer             deterministic — wraps body in re-run markers per agent

build only:
  → AST Extractor        deterministic — public API, exception family, notable dirs, examples, config keys
  → LLM Synthesizer      LLM, JSON-Schema enforced output; bullets dropped if citations don't resolve

init only:
  → LLM Enricher         LLM, JSON-Schema enforced; conventions / gotchas with on-disk evidence_path verification
```

Everything except the two LLM stages runs offline. The composer refuses to
emit unsupported claims; the renderer's safe-rerun markers mean re-running
either subcommand only updates the generated block — anything you've added
outside the markers is preserved.

### `validate` pipeline

```
Git Diff (excludes lockfiles)
  → Change Analyzer        deterministic
  → Risk Classifier        deterministic, language-aware patterns over added/removed lines
  → Intent Classifier      deterministic, multi-framework route detection
  → Confidence Scorer      deterministic; downgrades merge_risk when low
  → Impact Mapper          LLM-assisted (function calling); optional
  → Validation Planner     LLM, JSON-Schema enforced output
```

The four deterministic stages run on every invocation and produce the bulk of
the headline output (Change Intent, Analysis Confidence, Risk Types). The two
LLM stages run only when `llm_api_key` is configured and `CHANGEBRIEF_DISABLE_LLM`
is unset.

Heuristics are language-aware (Python, JavaScript / TypeScript, Go, Java, Ruby,
SQL today): patterns are anchored on word boundaries, scoped per-language, and
applied only to **added or removed source lines** — never the full diff blob.
This avoids the classic substring traps where `"db"` matches `stub`, `"http"`
matches every URL in a comment, or `"sql"` matches `mysql_url`.

Output from the planner is parsed by schema, not by string scanning, so format
drift between model versions falls back cleanly to a typed default rather than
silently breaking the report.

---

## Configuration

Config lives at `~/.changebrief/config.yaml` (created by `changebrief init`).

| Field           | Description                                         | Default       |
| --------------- | --------------------------------------------------- | ------------- |
| `llm_provider`  | LLM provider. `openai` is currently the only implemented option. | `openai`      |
| `llm_api_key`   | API key used for LLM calls.                         | *(empty)*     |
| `default_model` | Model used for analysis.                            | `gpt-4o-mini` |
| `log_level`     | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.    | `INFO`        |

Environment overrides:

- `CHANGEBRIEF_DISABLE_LLM=1` — disable all network LLM calls (useful in CI for
  smoke tests, or for review-only runs).
- `PYTEST_CURRENT_TEST` (set automatically by pytest) also disables LLM calls,
  so the test suite never makes a real API call.

---

## Caching

Results are cached on disk under
`~/.changebrief/cache/<repo>/<base_sha>..<feature_sha>/<context>/<key>.json`.
The `<context>` segment includes the pipeline version, the model name, and a
hash of the planner system prompt + JSON schema, so any of:

- bumping the pipeline version,
- switching `default_model`, or
- changing the planner contract,

invalidates older entries automatically. Cache TTL is 7 days. Writes are atomic
(tempfile + `os.replace`) so concurrent runs cannot leave half-written files.

Pass `--nocache` for a one-off bypass, or run `changebrief cache purge --all` to
clear everything.

---

## Limitations

Honest, current state — please open an issue if any of these blocks you:

- **Single LLM provider.** Only OpenAI is implemented today, despite the
  `llm_provider` config field listing other names.
- **Stale base ref is not detected.** ChangeBrief uses `git diff base...feature`
  on whatever your local refs point to. If your local `main` is behind
  `origin/main`, the report is too. Run `git fetch` first (or pass
  `origin/main` as `--base`).
- **Diff size cap.** Diffs sent to the LLM are truncated at 16 KB to keep the
  token budget predictable. Larger changes still produce a full deterministic
  report; the LLM-derived sections may be incomplete.
- **API key storage.** The key currently lives in `~/.changebrief/config.yaml`.
  System-keyring and `OPENAI_API_KEY` env-var support is on the roadmap.
- **Heuristic coverage.** Language-aware fast paths exist for Python, JS/TS,
  Go, Java, Ruby, SQL. Other languages still get full LLM analysis but no
  local pattern boost.
- **Advisory, not authoritative.** The merge-risk verdict is an aid for review.
  It does not replace tests, code review, or staging.

---

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

The full suite is hermetic — it sets `CHANGEBRIEF_DISABLE_LLM=1` and isolates
`HOME` in a temp dir, so it never makes real network calls or writes to your
real config / cache.

---

## License

MIT — see [`LICENSE`](LICENSE).
