# Describer

A GitHub Actions workflow and Python script that automatically generates and updates GitHub repository descriptions for every non-archived, non-fork repo in your organisation using an AI model from [GitHub Models](https://github.com/marketplace/models).

## How it works

1. **Crawls** all non-archived, non-fork repos in the configured GitHub org.
2. **Reads** each repo's file tree and fetches the content of relevant files (docs, config manifests, source files), capped at a 100 k-token budget.
3. **Asks** an AI model to write a one-sentence description of the repo.
4. **Checks** whether the generated description is semantically equivalent to the existing one — if yes, it skips the update.
5. **Updates** the repo description via the GitHub API when a meaningful change is detected.

The script runs entirely on Python standard library — no third-party dependencies.

## Workflow schedule

The workflow (`.github/workflows/update-repo-descriptions.yml`) is triggered:

- **Automatically** every Monday at 07:00 UTC (`0 7 * * 1`).
- **Manually** via `workflow_dispatch` from the Actions tab.

## Setup

### 1. Fork or copy this repository into your organisation

The workflow must live in a repository that GitHub Actions can execute.

### 2. Create a Personal Access Token (PAT) with repo admin rights

The `GITHUB_TOKEN` provided automatically by Actions can read repos but **cannot update another repository's description** via `PATCH /repos/{owner}/{repo}`. You need a separate PAT scoped to the target org.

1. Go to **GitHub → Settings → Developer settings → Personal access tokens**.
2. Create a token with at minimum the classic `repo` scope or the fine-grained `repository : content: read/write` (scoped to the target org).
3. Copy the token value — you will only see it once.

### 3. Configure secrets and variables

In the repository that hosts this workflow go to **Settings → Secrets and variables → Actions**.

#### Secrets

| Name | Required | Description |
|---|---|---|
| `REPO_ADMIN_PAT` | **Yes** | PAT with `repo` scope used to update repo descriptions (see step 2). |

> `GITHUB_TOKEN` is provided automatically by GitHub Actions — you do **not** need to create it.

#### Variables

| Name | Required | Default | Description |
|---|---|---|---|
| `ORG_NAME` | **Yes** | — | The GitHub organisation whose repos will be processed (e.g. `my-org`). |
| `AI_MODEL` | No | `gpt-5-mini` | GitHub Models model ID to use for description generation. See [GitHub Marketplace models](https://github.com/marketplace/models) for available IDs. |

> **Note:** GitHub does not allow variable names that start with `GITHUB_`. That is why the model variable is named `AI_MODEL` rather than `GITHUB_MODEL`.

### 4. Enable GitHub Models access

The script calls the [GitHub Models inference API](https://models.inference.ai.azure.com) using the workflow's built-in `GITHUB_TOKEN`. Ensure your organisation has access to GitHub Models (currently available to organisations on GitHub Teams / Enterprise or via the public beta).

## Environment variables (script reference)

The Python script reads the following environment variables at runtime:

| Variable | Source | Description |
|---|---|---|
| `GITHUB_TOKEN` | `secrets.GITHUB_TOKEN` (automatic) | Authenticates GitHub API reads and GitHub Models API calls. |
| `REPO_ADMIN_PAT` | `secrets.REPO_ADMIN_PAT` | Authenticates repo description `PATCH` calls. Must have `repo` scope. |
| `ORG_NAME` | `vars.ORG_NAME` | GitHub organisation to crawl. |
| `AI_MODEL` | `vars.AI_MODEL` | GitHub Models model ID (default: `gpt-5-mini`). |

## Running locally

```bash
export GITHUB_TOKEN="ghp_..."        # token with read:org + repo scopes
export REPO_ADMIN_PAT="ghp_..."      # token with repo scope for PATCH calls
export ORG_NAME="my-org"
export AI_MODEL="gpt-5-mini"         # optional

python scripts/update-repo-descriptions.py
```

## Example output

```
Organisation: my-org
Model: gpt-5-mini

Found 24 non-archived, non-fork repos.

[my-org/repo-name] Fetching file tree... 312 files found, 48 included
[my-org/repo-name] Context: 12,400 tokens (estimated)
[my-org/repo-name] Generated: "A Node.js CLI tool that scaffolds Firebase projects..."
[my-org/repo-name] Existing:  "Firebase scaffolding tool"
[my-org/repo-name] Semantic check: DIFFERENT → updating
[my-org/repo-name] ✓ Updated

────────────────────────────────────────────────────────────
Summary: 24 repos | 6 updated | 17 skipped | 1 failed
```

## File-filtering rules

The script includes only files that are relevant to understanding what a repo does:

- **Docs:** `*.md`, `*.rst`, `*.txt`
- **Config manifests:** `package.json`, `Cargo.toml`, `go.mod`, `Dockerfile`, `requirements.txt`, etc.
- **CI workflows:** `.github/workflows/*.yml`
- **Source code:** `.js`, `.ts`, `.py`, `.go`, `.rs`, `.java`, and many more

The following are always excluded:

- Directories: `node_modules/`, `vendor/`, `dist/`, `build/`, `.git/`, etc.
- Lock files: `package-lock.json`, `yarn.lock`, `Cargo.lock`, etc.
- Binary and media files (images, fonts, audio, video, compiled artifacts)
- Minified files (`*.min.js`, `*.min.css`)
- Files larger than 50 KB

## License

See [LICENSE](LICENSE).
