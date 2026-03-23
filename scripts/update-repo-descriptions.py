#!/usr/bin/env python3
"""
update-repo-descriptions.py

Weekly script that crawls all repositories in a GitHub organisation and uses
an AI model to generate or update each repo's GitHub description.

Available model IDs: https://github.com/marketplace/models
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlencode

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Model used for every AI call throughout the script.
# Override via the GITHUB_MODEL environment variable.
# See https://github.com/marketplace/models for available model IDs.
MODEL = os.environ.get("GITHUB_MODEL", "gpt-5-mini")

GITHUB_API_BASE = "https://api.github.com"
MODELS_API_URL = "https://models.inference.ai.azure.com/chat/completions"

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO_ADMIN_PAT = os.environ.get("REPO_ADMIN_PAT", "")
ORG_NAME = os.environ.get("ORG_NAME", "")

# Maximum estimated tokens to send to the model per repo.
TOKEN_BUDGET = 100_000
# Rough characters-per-token estimate (conservative for mixed content).
CHARS_PER_TOKEN = 4
# Maximum individual file size to include.
MAX_FILE_BYTES = 50_000

# ---------------------------------------------------------------------------
# File-filtering rules
# ---------------------------------------------------------------------------

EXCLUDED_DIRS = {
    "node_modules", "vendor", ".git", "dist", "build", "out",
    "__pycache__", ".next", ".cache",
}

EXCLUDED_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.lock", "poetry.lock",
}

BINARY_EXTENSIONS = {
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg", ".webp", ".tiff",
    # Fonts
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # Audio / video
    ".mp3", ".mp4", ".wav", ".ogg", ".avi", ".mov", ".mkv", ".flac",
    # Compiled / binary
    ".exe", ".dll", ".so", ".dylib", ".class", ".pyc", ".pyo",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
}

SOURCE_EXTENSIONS = {
    ".js", ".ts", ".jsx", ".tsx", ".py", ".go", ".rs", ".java",
    ".rb", ".swift", ".kt", ".cs", ".cpp", ".c", ".h", ".sh", ".bash",
    ".php", ".scala", ".r", ".m", ".lua", ".pl", ".ex", ".exs",
    ".elm", ".clj", ".cljs", ".hs", ".ml", ".mli", ".fs", ".fsx",
    ".vue", ".svelte",
}

CONFIG_FILENAMES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "Cargo.toml", "go.mod", "Gemfile", "Dockerfile",
    "docker-compose.yml", "docker-compose.yaml",
    ".env.example", "tsconfig.json", "jest.config.js",
    "webpack.config.js", "vite.config.ts", "vite.config.js",
    "babel.config.js", ".eslintrc.js", ".eslintrc.json",
    "requirements.txt", "Makefile", "CMakeLists.txt",
}

DOC_EXTENSIONS = {".md", ".rst", ".txt"}


def _is_excluded_path(path: str) -> bool:
    """Return True if any path component is an excluded directory."""
    parts = path.replace("\\", "/").split("/")
    for part in parts[:-1]:  # directories only
        if part in EXCLUDED_DIRS:
            return True
    return False


def _is_excluded_file(path: str) -> bool:
    """Return True if the file itself should be excluded."""
    filename = path.split("/")[-1]
    if filename in EXCLUDED_FILES:
        return True
    # Minified files
    if filename.endswith(".min.js") or filename.endswith(".min.css"):
        return True
    return False


def _file_priority(path: str) -> int:
    """Return priority for token-budget trimming (lower = higher priority)."""
    filename = path.split("/")[-1]
    ext = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
    if ext in DOC_EXTENSIONS:
        return 1
    if filename in CONFIG_FILENAMES:
        return 2
    if ext in {".yml", ".yaml"} and ".github/workflows" in path:
        return 2
    return 3  # source files


def _is_relevant(path: str) -> bool:
    """Return True if this file should be included in the prompt context."""
    if _is_excluded_path(path):
        return False
    if _is_excluded_file(path):
        return False
    filename = path.split("/")[-1]
    ext = "." + filename.rsplit(".", 1)[-1] if "." in filename else ""
    if ext in BINARY_EXTENSIONS:
        return False
    if ext in DOC_EXTENSIONS:
        return True
    if filename in CONFIG_FILENAMES:
        return True
    if ext in {".yml", ".yaml"} and ".github/workflows" in path:
        return True
    if ext in SOURCE_EXTENSIONS:
        return True
    return False


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _github_request(path: str, *, token: str, method: str = "GET",
                    body: dict | None = None, params: dict | None = None):
    """
    Make a GitHub REST API request and return the parsed JSON response.
    Raises urllib.error.HTTPError on non-2xx responses.
    """
    url = f"{GITHUB_API_BASE}{path}"
    if params:
        url += "?" + urlencode(params)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "describer-bot/1.0",
    }
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def _models_request(messages: list[dict]) -> str:
    """
    Call the GitHub Models inference API and return the assistant's reply text.
    """
    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 512,
    }
    data = json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "describer-bot/1.0",
    }
    req = urllib.request.Request(
        MODELS_API_URL, data=data, headers=headers, method="POST"
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode())
    return result["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def fetch_org_repos(org: str) -> list[dict]:
    """
    Return all non-archived, non-fork repositories in *org* (paginated).
    Uses REPO_ADMIN_PAT for authentication so that private repos are visible.
    """
    token = REPO_ADMIN_PAT or GITHUB_TOKEN
    repos = []
    page = 1
    while True:
        page_data = _github_request(
            f"/orgs/{org}/repos",
            token=token,
            params={"per_page": 100, "page": page, "type": "all"},
        )
        if not page_data:
            break
        for repo in page_data:
            if not repo.get("archived") and not repo.get("fork"):
                repos.append(repo)
        if len(page_data) < 100:
            break
        page += 1
    return repos


def fetch_file_tree(owner: str, repo: str) -> list[str]:
    """
    Return a flat list of all file paths in *repo* via the recursive tree API.
    """
    token = REPO_ADMIN_PAT or GITHUB_TOKEN
    try:
        data = _github_request(
            f"/repos/{owner}/{repo}/git/trees/HEAD",
            token=token,
            params={"recursive": "1"},
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            # Empty repository
            return []
        raise
    return [
        item["path"]
        for item in data.get("tree", [])
        if item.get("type") == "blob"
    ]


def fetch_file_contents(owner: str, repo: str,
                        paths: list[str]) -> dict[str, str]:
    """
    Fetch the decoded text contents of *paths* in *repo*.
    Files larger than MAX_FILE_BYTES or that cannot be decoded as UTF-8 are
    skipped with a log note.
    Returns a dict mapping path → content string.
    """
    token = REPO_ADMIN_PAT or GITHUB_TOKEN
    contents = {}
    for path in paths:
        try:
            data = _github_request(
                f"/repos/{owner}/{repo}/contents/{path}",
                token=token,
            )
            size = data.get("size", 0)
            if size > MAX_FILE_BYTES:
                print(f"  [skip] {path} — {size:,} bytes > {MAX_FILE_BYTES:,} limit")
                continue
            raw = base64.b64decode(data.get("content", "").replace("\n", ""))
            decoded = raw.decode("utf-8", errors="replace")
            if "\ufffd" in decoded:
                print(f"  [warn] {path} contains non-UTF-8 bytes; some characters replaced")
            contents[path] = decoded
        except urllib.error.HTTPError as exc:
            print(f"  [warn] Could not fetch {path}: HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] Could not fetch {path}: {exc}")
    return contents


def build_prompt_context(
    owner: str,
    repo: str,
    current_description: str,
    all_paths: list[str],
    file_contents: dict[str, str],
) -> tuple[str, int]:
    """
    Assemble the prompt context string, respecting the token budget.
    Priority order: docs > config/manifests > source files.
    """
    tree_listing = "\n".join(all_paths)

    # Sort included files by priority so we can truncate low-priority ones first.
    included_paths = sorted(file_contents.keys(), key=_file_priority)

    # Calculate tree budget (always fully included).
    tree_tokens = len(tree_listing) // CHARS_PER_TOKEN
    remaining_budget = TOKEN_BUDGET - tree_tokens

    sections = []
    used_tokens = 0
    for path in included_paths:
        text = file_contents[path]
        tokens = len(text) // CHARS_PER_TOKEN
        if used_tokens + tokens > remaining_budget:
            # Truncate to fit
            allowed_chars = (remaining_budget - used_tokens) * CHARS_PER_TOKEN
            if allowed_chars <= 0:
                break
            text = text[:allowed_chars] + "\n... [truncated]"
            sections.append(f"### {path}\n{text}")
            break
        sections.append(f"### {path}\n{text}")
        used_tokens += tokens

    file_contents_block = "\n\n".join(sections)
    estimated_tokens = (tree_tokens + used_tokens)

    context = (
        f"Current description (may be empty): {current_description}\n\n"
        f"Repository: {owner}/{repo}\n\n"
        f"File tree:\n{tree_listing}\n\n"
        f"File contents:\n{file_contents_block}"
    )
    return context, estimated_tokens


def call_model(context: str) -> str:
    """
    Ask the AI model to generate a one-sentence repository description.
    Returns the raw string from the model.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You are a technical writer who specializes in writing "
                "GitHub repository descriptions."
            ),
        },
        {
            "role": "user",
            "content": (
                "Analyse the following repository content and write a GitHub "
                "repository description.\n\n"
                "Rules:\n"
                "- One sentence, maximum 350 characters\n"
                "- Explain what the project actually does (not what it aspires to do)\n"
                "- Mention the main technology or platform if it helps clarify the purpose\n"
                "- If it is a browser extension, CLI tool, library, service, app, or "
                "framework, say so directly\n"
                "- Avoid vague marketing language (\"powerful\", \"seamless\", "
                "\"easy-to-use\") unless no technical alternative exists\n"
                "- Do not start with the repo name\n"
                "- Return ONLY the description string — no quotes, no explanation, "
                "no preamble\n\n"
                + context
            ),
        },
    ]
    return _models_request(messages)


def check_semantically_equal(existing: str, generated: str) -> bool:
    """
    Ask the model whether two descriptions convey the same meaning.
    Returns True if the model answers YES (skip update).
    """
    if not existing:
        return False
    messages = [
        {
            "role": "user",
            "content": (
                "Do these two GitHub repo descriptions convey the same meaning? "
                "Answer only YES or NO.\n"
                f"A: {existing}\n"
                f"B: {generated}"
            ),
        }
    ]
    answer = _models_request(messages).strip().upper()
    return answer.startswith("YES")


def update_repo_description(owner: str, repo: str, description: str) -> None:
    """
    Update the repository description via PATCH /repos/{owner}/{repo}.
    Uses REPO_ADMIN_PAT which must have the `repo` scope.
    """
    _github_request(
        f"/repos/{owner}/{repo}",
        token=REPO_ADMIN_PAT,
        method="PATCH",
        body={"description": description},
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not ORG_NAME:
        print("ERROR: ORG_NAME environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    if not REPO_ADMIN_PAT:
        print(
            "WARNING: REPO_ADMIN_PAT is not set — description updates will fail.",
            file=sys.stderr,
        )

    print(f"Organisation: {ORG_NAME}")
    print(f"Model: {MODEL}")
    print()

    repos = fetch_org_repos(ORG_NAME)
    print(f"Found {len(repos)} non-archived, non-fork repos.\n")

    total = len(repos)
    updated = 0
    skipped = 0
    failed = 0

    for repo_data in repos:
        owner = repo_data["owner"]["login"]
        repo_name = repo_data["name"]
        prefix = f"[{owner}/{repo_name}]"
        current_description = repo_data.get("description") or ""

        try:
            # 1. Fetch file tree
            print(f"{prefix} Fetching file tree...", end=" ", flush=True)
            all_paths = fetch_file_tree(owner, repo_name)
            relevant_paths = [p for p in all_paths if _is_relevant(p)]
            print(f"{len(all_paths)} files found, {len(relevant_paths)} included")

            # 2. Fetch file contents
            file_contents = fetch_file_contents(owner, repo_name, relevant_paths)

            # 3. Build prompt context
            context, estimated_tokens = build_prompt_context(
                owner, repo_name, current_description, all_paths, file_contents
            )
            print(f"{prefix} Context: {estimated_tokens:,} tokens (estimated)")

            # 4. Call model
            generated = call_model(context)
            # Sanitise: strip surrounding quotes if the model added them
            generated = generated.strip().strip('"').strip("'").strip()
            print(f'{prefix} Generated: "{generated}"')
            print(f'{prefix} Existing:  "{current_description}"')

            # 5. Semantic similarity check
            if check_semantically_equal(current_description, generated):
                print(f"{prefix} Semantic check: SAME → skipping")
                skipped += 1
            else:
                action = "updating" if current_description else "setting"
                print(f"{prefix} Semantic check: DIFFERENT → {action}")
                update_repo_description(owner, repo_name, generated)
                print(f"{prefix} ✓ Updated")
                updated += 1

        except Exception as exc:  # noqa: BLE001
            print(f"{prefix} ✗ Failed: {exc}")
            failed += 1

        print()

    # Summary table
    print("─" * 60)
    print(
        f"Summary: {total} repos | {updated} updated | "
        f"{skipped} skipped | {failed} failed"
    )


if __name__ == "__main__":
    main()
