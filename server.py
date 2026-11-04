"""local-git mcp server 1.0 — full local version-control toolkit over Streamable HTTP.

The mounted folder is exposed at WORKSPACE_DIR (default /workspace). The git
repo lives at REPO_ROOT (default: WORKSPACE_DIR; can be a subfolder of it).
Both are configurable via env.
On startup, if REPO_ROOT isn't yet a git repo, it gets initialized in place.

Highlights:
- Selective staging: git_add, git_reset (soft/mixed/hard), git_commit(paths=…)
- History inspection: git_show (commit or ref:path file content), per-file git_log, git_blame
- Local workflow core: git_branch_create, git_branch_delete, git_checkout, git_stash,
  git_merge (+ git_merge_abort) with structural conflict reporting
- Locale-independent state detection: every emptiness/unknown-ref check is done
  structurally (exit codes of rev-parse / status --porcelain / diff --quiet),
  and git always runs under LC_ALL=C.
- Diff upgrades: ref ranges (a..b / a...b), --staged, --stat, per-path filter
- Mutations refused with a real MCP tool error, not a success-with-message string
- Commit safety: git_commit never implicitly `git add -A`s — untracked
  files are never silently committed (paths omitted = tracked changes only,
  in-progress merges commit the index exactly as staged) and every commit
  result lists per-file what it contains (including newly-tracked files and
  their mode), for normal commits AND merge commits
"""

import os
import re
import subprocess
from pathlib import Path

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

# ── Configuration (via environment variables) ─────────────────────────────
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "/workspace")).resolve()
REPO_ROOT = Path(os.getenv("REPO_ROOT", str(WORKSPACE_DIR))).resolve()
ALLOW_MUTATIONS = os.getenv("ALLOW_MUTATIONS", "false").lower() == "true"
GIT_TIMEOUT = int(os.getenv("GIT_TIMEOUT", "30"))
COMMIT_NAME = os.getenv("GIT_COMMITTER_NAME", "MCP Server")
COMMIT_EMAIL = os.getenv("GIT_COMMITTER_EMAIL", "mcp@localhost")
PORT = int(os.getenv("PORT", "8000"))

# Truncate very large outputs so we don't blow the client's context window.
MAX_OUTPUT_BYTES = int(os.getenv("MAX_OUTPUT_BYTES", 100_000))

mcp = FastMCP(
    "local-git-mcp-server",
    instructions=(
        f"Git tools sandboxed to {REPO_ROOT}. "
        f"The full project tree is visible at {WORKSPACE_DIR}, but only "
        f"{REPO_ROOT} is git-managed. "
        f"Paths in tool arguments are container paths (e.g. {WORKSPACE_DIR}/folderC). "
        "Mutations gated behind ALLOW_MUTATIONS."
    ),
)

# Deterministic git messages (C locale) + commit identity, for every subprocess.
_ENV = dict(os.environ)
_ENV["LC_ALL"] = "C"
_ENV["LANG"] = "C"
_ENV.setdefault("GIT_AUTHOR_NAME", COMMIT_NAME)
_ENV.setdefault("GIT_AUTHOR_EMAIL", COMMIT_EMAIL)
_ENV.setdefault("GIT_COMMITTER_NAME", COMMIT_NAME)
_ENV.setdefault("GIT_COMMITTER_EMAIL", COMMIT_EMAIL)

DEFAULT_GITIGNORE = (
    "# Default ignores — edit as needed\n"
    "__pycache__/\n"
    "*.pyc\n"
    ".venv/\n"
    "venv/\n"
    "node_modules/\n"
    "dist/\n"
    "build/\n"
    ".env\n"
    ".env.local\n"
    ".env.*.local\n"
    "*.log\n"
    ".DS_Store\n"
    "Thumbs.db\n"
)


# ── Helpers ──────────────────────────────────────────────────────────────
def _truncate(text: str) -> str:
    """Cap output size to protect the client's context window."""
    if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        encoded = text.encode("utf-8")[:MAX_OUTPUT_BYTES]
        return encoded.decode("utf-8", errors="replace") + "\n…[truncated]"
    return text


def _resolve_repo(repo_path: str) -> str:
    """Ensure repo_path is inside REPO_ROOT and is a git repository.

    repo_path must be the root of a git repo (i.e. it must contain .git/).
    Pass the REPO_ROOT path itself, or a nested repo root within it.
    """
    p = Path(repo_path).resolve()
    if not p.is_relative_to(REPO_ROOT):
        raise ValueError(f"repo_path must be inside {REPO_ROOT} (got: {repo_path})")
    if not (p / ".git").exists():
        raise ValueError(f"Not a git repository: {p}")
    return str(p)


def _run_git(args: list[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run one git command; return the full CompletedProcess.

    With check=True (default) a non-zero exit raises RuntimeError carrying the
    stderr. With check=False the caller inspects result.returncode — this is
    the basis for all state detection (no parsing of message text).
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            env=_ENV,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"`git {' '.join(args)}` timed out after {GIT_TIMEOUT}s")
    if check and result.returncode != 0:
        raise RuntimeError(
            f"`git {' '.join(args)}` failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result


def _require_mutations() -> None:
    """Refuse mutation tools with a real MCP error when disabled."""
    if not ALLOW_MUTATIONS:
        raise PermissionError(
            "Refused: mutation tools are disabled. "
            "Restart the container with -e ALLOW_MUTATIONS=true to enable."
        )


def _check_ref(ref: str) -> str:
    """Reject empty refs and anything starting with '-' (option injection)."""
    ref = ref.strip()
    if not ref or ref.startswith("-"):
        raise ValueError(f"invalid ref: {ref!r}")
    return ref


def _check_range(ref: str) -> str:
    """Validate a ref that may contain a .. or ... range separator."""
    for part in re.split(r"\.{2,3}", ref):
        if not part:
            raise ValueError(f"invalid ref range: {ref!r}")
        _check_ref(part)
    return ref


def _check_path(cwd: str, path: str) -> str:
    """Validate a repo-relative path and make sure it cannot escape the repo."""
    path = path.strip()
    if not path or path.startswith("-"):
        raise ValueError(f"invalid path: {path!r}")
    target = (Path(cwd) / path).resolve()
    if not target.is_relative_to(Path(cwd)):
        raise ValueError(f"path escapes repo: {path}")
    return path


# ── Structural state detection (locale-independent) ──────────────────────
def _has_commits(cwd: str) -> bool:
    """True if the repo has at least one commit (rev-parse exit code)."""
    r = _run_git(["rev-parse", "--verify", "-q", "--end-of-options", "HEAD"], cwd, check=False)
    return r.returncode == 0


def _ref_exists(cwd: str, ref: str) -> bool:
    """True if `ref` resolves to an object (rev-parse exit code)."""
    r = _run_git(["rev-parse", "--verify", "-q", "--end-of-options", ref], cwd, check=False)
    return r.returncode == 0


def _current_branch(cwd: str) -> str:
    """Name of the checked-out branch, or '' if detached HEAD."""
    return _run_git(["branch", "--show-current"], cwd).stdout.strip()


def _has_local_changes(cwd: str) -> bool:
    """True if there are tracked changes (staged or unstaged), ignoring untracked."""
    return bool(_run_git(["status", "--porcelain", "-uno"], cwd).stdout.strip())


# ── Read-only tools ──────────────────────────────────────────────────────
@mcp.tool()
def git_status(repo_path: str, short: bool = False, path: str | None = None) -> str:
    """Show the working tree status of a git repository.
    Default: full format with branch info and staged/unstaged/untracked sections.
    short=True: compact two-column format (one line per file).
    path: limit status to that file or folder."""
    cwd = _resolve_repo(repo_path)
    args = ["status"]
    if short:
        args.append("--short")
    if path:
        args += ["--", _check_path(cwd, path)]
    out = _run_git(args, cwd).stdout.strip()
    return _truncate(out) or "(working tree clean)"


@mcp.tool()
def git_log(repo_path: str, count: int = 10, path: str | None = None, full: bool = False) -> str:
    """Show recent commit history, newest first (default: last 10).
    count: number of commits (max 1000).
    path: only commits that touched this file or folder (per-file history).
    full=True: include commit bodies and author lines instead of one-line format."""
    cwd = _resolve_repo(repo_path)
    if not _has_commits(cwd):
        return "(no commits yet — repository is empty)"
    n = max(1, min(int(count), 1000))
    args = ["log", "-n", str(n)]
    if not full:
        args += ["--format=%h %s (%an, %ad)", "--date=short"]
    if path:
        args += ["--", _check_path(cwd, path)]
    out = _run_git(args, cwd).stdout.strip()
    return _truncate(out) or "(no commits)"


@mcp.tool()
def git_diff(
    repo_path: str,
    ref: str = "HEAD",
    ref2: str | None = None,
    staged: bool = False,
    stat: bool = False,
    path: str | None = None,
) -> str:
    """Show changes between states as a unified diff. `-` lines come from the
    left/first argument and `+` lines from the right/second argument.
    - Default (ref="HEAD", ref2=None, staged=False): all uncommitted changes to
      tracked files (`git diff <ref>` compares the whole working tree against it,
      so this is staged + unstaged). A lone `<ref>` is a single-tree diff vs HEAD.
    - staged=True: only what is staged for the next commit (index vs HEAD).
    - ref2 given: change from `ref` → `ref2`, i.e. `git diff <ref> <ref2>`. The
      `-` lines show how `<ref>` looked and `+` lines show `<ref2>`, so the +/-
      direction follows that order. You can also pass a range directly in ref:
      "main..feature" (two-tree) or "main...feature" (merge-base diff).
    - stat=True: compact per-file summary (added/removed lines) instead of the
      full patch.
    - path: limit the diff to a file or folder.
    Untracked (new) files are never shown — use git_status."""
    cwd = _resolve_repo(repo_path)
    args = ["diff"]
    if stat:
        args.append("--stat")
    if staged:
        args.append("--staged")
    if ref2 is not None:
        args += [_check_ref(ref), _check_ref(ref2)]
    else:
        has_commits = _has_commits(cwd)
        if not has_commits:
            # Staged-only diff is still meaningful against the empty tree.
            if staged and ref == "HEAD":
                pass
            else:
                return "(no commits yet — nothing to diff against)"
        else:
            args.append(_check_range(ref) if ".." in ref else _check_ref(ref))
    if path:
        args += ["--", _check_path(cwd, path)]
    out = _run_git(args, cwd).stdout.strip()
    return _truncate(out) or "(no changes)"


@mcp.tool()
def git_show(repo_path: str, ref: str = "HEAD", path: str | None = None) -> str:
    """Inspect history precisely.
    - path=None: show the commit's full message and the diff it introduced.
    - path given: show a file's content as it was at `ref`
      (e.g. ref="HEAD~1", path="src/app.py") — reads the old version from history.
    You may also pass the combined form ref="HEAD~1:src/app.py" with path omitted."""
    cwd = _resolve_repo(repo_path)
    ref = _check_ref(ref)
    if not _has_commits(cwd):
        return "(no commits yet — repository is empty)"
    if path is not None:
        _check_path(cwd, path)
        obj = f"{ref}:{path}"
    else:
        obj = ref
    out = _run_git(["show", obj], cwd).stdout.rstrip("\n")
    return _truncate(out) or "(empty)"


@mcp.tool()
def git_blame(repo_path: str, path: str, ref: str = "HEAD",
              start: int | None = None, end: int | None = None) -> str:
    """Blame a file: for each line, show which commit, author and date introduced it.
    The go-to tool for 'when/why did this line change?'.
    path: the file to blame (required).
    ref: blame at this ref instead of HEAD (e.g. a branch or HEAD~1).
    start / end: restrict to a line range (1-indexed, inclusive).
      start alone: blame exactly that one line."""
    cwd = _resolve_repo(repo_path)
    target = _check_path(cwd, path)
    ref = _check_ref(ref)
    if not _has_commits(cwd):
        return "(no commits yet — nothing to blame)"
    if not _ref_exists(cwd, ref):
        return f"(unknown ref: {ref})"
    if start is not None and start < 1:
        raise ValueError("start must be >= 1")
    if end is not None and end < 1:
        raise ValueError("end must be >= 1")
    args = ["blame", "--date=format:%Y-%m-%d"]
    if start is not None:
        args += ["-L", f"{start},{end if end is not None else start}"]
    args += [ref, "--", target]
    out = _run_git(args, cwd).stdout.strip()
    return _truncate(out) or "(empty file)"


@mcp.tool()
def git_branches(repo_path: str) -> str:
    """List local branches with tracking info and last-commit data."""
    cwd = _resolve_repo(repo_path)
    if not _has_commits(cwd):
        return "(no branches yet — no commits made)"
    out = _run_git(["branch", "-vv"], cwd).stdout.strip()
    return out or "(no branches yet)"


@mcp.tool()
def git_current_branch(repo_path: str) -> str:
    """Return the currently checked-out branch, or 'HEAD (detached)'."""
    cwd = _resolve_repo(repo_path)
    return _current_branch(cwd) or "HEAD (detached)"


@mcp.tool()
def git_remote_url(repo_path: str) -> str:
    """Show configured remote names and URLs."""
    out = _run_git(["remote", "-v"], _resolve_repo(repo_path)).stdout.strip()
    return out or "(no remotes configured)"


# ── Mutation tools (gated behind ALLOW_MUTATIONS) ─────────────────────────
@mcp.tool()
def git_add(repo_path: str, paths: list[str] | None = None) -> str:
    """Stage specific file(s) or folder(s) for the next commit (selective staging).
    paths: list of paths relative to the repo (e.g. ["src/app.py", "docs"]).
    Omitted or ["."] stages everything, including new and deleted files.
    Unstage with git_reset (mode='mixed'). Only available when ALLOW_MUTATIONS=true."""
    _require_mutations()
    cwd = _resolve_repo(repo_path)
    items = paths if paths else ["."]
    _run_git(["add", "-A", "--", *[_check_path(cwd, p) for p in items]], cwd)
    staged = _run_git(["diff", "--cached", "--name-only"], cwd).stdout.strip()
    if not staged:
        return "Nothing staged (no matching changes found)."
    return f"Staged {len(staged.splitlines())} file(s). " \
           "Commit with git_commit, unstage with git_reset."


@mcp.tool()
def git_reset(repo_path: str, mode: str = "mixed", ref: str = "HEAD") -> str:
    """Move the current branch pointer to `ref` and control what is kept.
    **WARNING: `ref` defaults to HEAD, which resets onto the current branch tip
    — that never moves history and is a no-op unless something was staged.**
    With the default `mixed` mode it will only unstage any currently-staged
    changes; with nothing staged it truly does nothing. Pass an explicit ref
    such as `HEAD~1` (or `HEAD^`, `main~2`) to actually rewind the branch.
    mode controls what happens to changes relative to `ref`:
    - soft:     keep changes staged.
    - mixed:    unstage, keep worktree edits (default).
    - hard:     DESTROY uncommitted changes in the worktree and index
                (untracked files are kept) — destructive, so default this mode.
    Returns the reset target, mode, and the resulting working-tree status.
    Only available when ALLOW_MUTATIONS=true."""
    _require_mutations()
    cwd = _resolve_repo(repo_path)
    mode = mode.strip().lower()
    if mode not in ("soft", "mixed", "hard"):
        raise ValueError("mode must be one of: soft, mixed, hard")
    ref = _check_ref(ref)
    if not _has_commits(cwd):
        return "(no commits yet — nothing to reset)"
    if not _ref_exists(cwd, ref):
        return f"(unknown ref: {ref})"
    # A reset only moves history if it changes the HEAD commit. Compare HEAD
    # before/after (locale-independent exit codes); mixed mode can also unstage
    # what was already staged, which we surface separately so a no-op is never
    # mistaken for success.
    tip_before = _run_git(
        ["rev-parse", "--verify", "-q", "HEAD"], cwd, check=False
    ).stdout.strip()
    staged_before = set(
        _run_git(["diff", "--cached", "--name-only"], cwd).stdout.split()
    )
    _run_git(["reset", f"--{mode}", ref], cwd)

    if mode == "hard":
        # Destructive regardless of pointer movement — discards worktree+index.
        return f"Reset to {ref} --hard. All uncommitted changes were discarded."

    tip_after = _run_git(
        ["rev-parse", "--verify", "-q", "HEAD"], cwd, check=False
    ).stdout.strip()

    if tip_before != tip_after:
        # The branch pointer moved — a genuine rewind of the requested ref.
        status = _run_git(["status", "--short"], cwd).stdout.strip()
        return f"Reset to {ref} ({mode}).\n{status or '(working tree clean)'}"

    # Pointer did not move (resetting onto the current tip).
    if mode == "mixed":
        staged_after = set(
            _run_git(["diff", "--cached", "--name-only"], cwd).stdout.split()
        )
        if staged_after != staged_before:
            # mixed reset unstaged what was previously staged.
            status = _run_git(["status", "--short"], cwd).stdout.strip()
            return (
                f"Reset to {ref} ({mode}). No branch movement — the pointer was "
                f"already at {ref}; this unstaged any previously-staged changes.\n"
                f"{status or '(working tree clean)'}"
            )

    # Nothing moved and nothing was staged → a genuine no-op.
    return (
        f"No change: resetting to {ref} does nothing — the branch pointer is "
        f"already there, so this is a no-op. Pass an explicit ref such as "
        f"HEAD~1 to rewind."
    )


@mcp.tool()
def git_commit(
    repo_path: str,
    message: str,
    paths: list[str] | None = None,
    amend: bool = False,
) -> str:
    """Create a commit. This does NOT push. Commits **only** the current index
    (`git commit` semantics) — it never runs `git add -A`, so UNTRACKED files
    are NEVER silently added to history.
    paths: if given, stage exactly these file(s)/folder(s) first and commit
    them; other uncommitted changes stay unstaged. If omitted, only TRACKED
    file changes (modifications + deletions) are staged via `git add -u`. To
    include new/untracked files, pass them in paths, or pass ["."] to stage
    everything, or git_add them first — there is no implicit add-all; nothing
    enters history silently.
    During an in-progress merge, nothing is staged: the index (including any
    resolved conflicts) is committed as-is, exactly like plain `git commit`.
    The result ALWAYS lists every file the commit contains — including
    newly-tracked files and their mode (added / modified / deleted / renamed) —
    on normal commits AND merge commits, plus anything auto-staged, so the
    caller can verify exactly what entered history. Nothing enters silently.
    amend=True rewrites the latest commit (message replaces it) — use with care
    on shared branches. Only available when ALLOW_MUTATIONS=true."""
    _require_mutations()
    cwd = _resolve_repo(repo_path)
    if amend and not _has_commits(cwd):
        return "(no commits yet — nothing to amend)"
    # Structural merge detection — implicit staging is never done during a
    # merge (the index, incl. the resolved conflicts, is committed exactly
    # as staged). Explicit paths are still honored: staging a resolved
    # file and committing it in one call keeps working.
    merge_in_progress = _ref_exists(cwd, "MERGE_HEAD")
    auto_staged: list[str] = []
    if paths is not None or not merge_in_progress:
        staged_before = set(
            _run_git(["diff", "--cached", "--name-only"], cwd).stdout.split()
        )
        if paths is None:
            # Tracked modifications/deletions only — untracked files stay out.
            _run_git(["add", "-u"], cwd)
        else:
            _run_git(["add", "-A", "--", *[_check_path(cwd, p) for p in paths]], cwd)
        staged_after = set(
            _run_git(["diff", "--cached", "--name-only"], cwd).stdout.split()
        )
        auto_staged = sorted(staged_after - staged_before)
    if not amend and not merge_in_progress:
        # Structural emptiness check — no output parsing, locale-proof.
        r = _run_git(["diff", "--cached", "--quiet"], cwd, check=False)
        if r.returncode == 0:
            msg = "Nothing to commit — no staged changes."
            untracked = [
                line[3:]
                for line in _run_git(["status", "--porcelain"], cwd).stdout.splitlines()
                if line.startswith("??")
            ]
            if untracked:
                shown = ", ".join(untracked[:20])
                if len(untracked) > 20:
                    shown += f", … (+{len(untracked) - 20} more)"
                msg += (
                    f" Untracked files were NOT staged: {shown} — "
                    f"stage them with git_add or pass them in paths."
                )
            return msg
    args = ["commit", "-m", message]
    if amend:
        args.append("--amend")
    result = _run_git(args, cwd, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"Commit failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    out = result.stdout.strip()
    # Per-file audit trail, always — also for merge commits (where plain
    # `git commit` shows only the short status). Merge commits are diffed
    # against the first parent (two trees) so everything they contain is
    # listed; a lone commit arg would diff it against ITS parent instead.
    if merge_in_progress:
        files = _run_git(
            ["diff-tree", "--no-commit-id", "--name-status", "-r", "HEAD^1", "HEAD"],
            cwd,
        ).stdout
    else:
        # --root makes a first commit list all of its files as A.
        files = _run_git(
            ["diff-tree", "--no-commit-id", "--root", "--name-status", "-r", "HEAD"],
            cwd,
        ).stdout
    file_lines = []
    for line in files.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        code = parts[0][0]
        label = {"A": "added", "M": "modified", "D": "deleted",
                 "R": "renamed", "C": "copied"}.get(code, code)
        # Renames are reported as R<score>\told\tnew — name the new path.
        target = parts[-1] if code in ("R", "C") and len(parts) >= 3 else parts[1]
        file_lines.append(f"{label:<9} {target}")
    if file_lines:
        out += "\n" + "\n".join(file_lines)
    if auto_staged:
        shown = ", ".join(auto_staged[:20])
        if len(auto_staged) > 20:
            shown += f", … (+{len(auto_staged) - 20} more)"
        out += f"\nAuto-staged (not previously staged): {shown}"
    return _truncate(out)


@mcp.tool()
def git_restore(repo_path: str, path: str = ".", ref: str = "HEAD") -> str:
    """Restore file(s) to their state at `ref`, discarding uncommitted changes
    in BOTH the worktree and the index. This is the 'undo what I just edited'
    tool. Does NOT create a commit.
    path='.' restores the entire working tree and index.
    Untracked (new) files are NOT removed. Only available when ALLOW_MUTATIONS=true."""
    _require_mutations()
    cwd = _resolve_repo(repo_path)
    target = _check_path(cwd, path)
    ref = _check_ref(ref)
    if not _ref_exists(cwd, ref):
        return f"(cannot restore to {ref} — unknown ref or no commits yet)"
    _run_git(["restore", "--source", ref, "--staged", "--worktree", "--", target], cwd)
    return f"Restored '{target}' (worktree + index) to state at {ref}."


@mcp.tool()
def git_revert(repo_path: str, ref: str = "HEAD") -> str:
    """Create a new commit that undoes the changes introduced by `ref`.
    The original commit is NOT deleted — a compensating commit is added.
    Only available when ALLOW_MUTATIONS=true."""
    _require_mutations()
    cwd = _resolve_repo(repo_path)
    ref = _check_ref(ref)
    if not _has_commits(cwd):
        return "(no commits yet — nothing to revert)"
    if not _ref_exists(cwd, ref):
        return f"(unknown ref: {ref})"
    result = _run_git(["revert", "--no-edit", ref], cwd, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"Revert failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip()


@mcp.tool()
def git_branch_create(
    repo_path: str,
    name: str,
    ref: str = "HEAD",
    checkout: bool = False,
) -> str:
    """Create a new branch `name` at `ref` (default: current HEAD).
    checkout=True switches to it immediately.
    Only available when ALLOW_MUTATIONS=true."""
    _require_mutations()
    cwd = _resolve_repo(repo_path)
    name = _check_ref(name)
    ref = _check_ref(ref)
    if not _has_commits(cwd):
        return "(no commits yet — branches need at least one commit)"
    if ref != "HEAD" and not _ref_exists(cwd, ref):
        return f"(unknown ref: {ref})"
    args = ["branch", name]
    if ref != "HEAD":
        args.append(ref)
    r = _run_git(args, cwd, check=False)
    if r.returncode != 0:
        raise RuntimeError(
            f"Branch creation failed (exit {r.returncode}): "
            f"{(r.stderr or r.stdout).strip()}"
        )
    if checkout:
        _run_git(["checkout", name], cwd)
        return f"Created and checked out branch '{name}' (from {ref})."
    return f"Created branch '{name}' (from {ref}). " \
           f"Current branch unchanged: '{_current_branch(cwd)}'."


@mcp.tool()
def git_branch_delete(repo_path: str, name: str, force: bool = False) -> str:
    """Delete local branch `name`. Fails if it is not fully merged unless
    force=True (uses -D). The currently checked-out branch cannot be deleted.
    Only available when ALLOW_MUTATIONS=true."""
    _require_mutations()
    cwd = _resolve_repo(repo_path)
    name = _check_ref(name)
    if _has_commits(cwd) and _current_branch(cwd) == name:
        raise ValueError(f"Refusing to delete the current branch '{name}'. Switch first.")
    args = ["branch", "-D" if force else "-d", name]
    r = _run_git(args, cwd, check=False)
    if r.returncode != 0:
        raise RuntimeError(
            f"Branch deletion failed (exit {r.returncode}): "
            f"{(r.stderr or r.stdout).strip()}"
        )
    return r.stdout.strip() or f"Deleted branch '{name}'."


@mcp.tool()
def git_checkout(
    repo_path: str,
    ref: str,
    create: bool = False,
    discard: bool = False,
) -> str:
    """Switch to branch `ref` (or a commit hash → detached HEAD).
    create=True: create a new branch named `ref` from the current HEAD, then switch.
    Refuses to switch while there are uncommitted tracked changes,
    unless discard=True (forces the switch, discarding those changes).
    Untracked files never block a switch. Only available when ALLOW_MUTATIONS=true."""
    _require_mutations()
    cwd = _resolve_repo(repo_path)
    ref = _check_ref(ref)
    if create:
        if _ref_exists(cwd, ref):
            return f"(ref '{ref}' already exists — use create=False to switch to it)"
        _run_git(["checkout", "-b", ref], cwd)
        return f"Created and checked out branch '{ref}'."
    if not _ref_exists(cwd, ref):
        return f"(unknown ref: {ref})"
    if _has_local_changes(cwd) and not discard:
        return (
            "Working tree has uncommitted changes; refusing to switch. "
            "Commit or git_stash them first, or pass discard=True to throw them away."
        )
    args = ["checkout"]
    if discard:
        args.append("-f")
    args.append(ref)
    _run_git(args, cwd)
    branch = _current_branch(cwd)
    return f"Now on '{branch}'" if branch else f"Now on detached HEAD at {ref}."


@mcp.tool()
def git_stash(
    repo_path: str,
    action: str = "push",
    message: str = "",
    include_untracked: bool = False,
    index: int = 0,
) -> str:
    """Temporarily shelve uncommitted changes (the 'save my WIP' tool).
    action: 'push' (stash current changes), 'list', 'pop' (apply the
    stash at `index` and remove it), 'apply' (apply but keep the stash),
    'drop' (delete the stash at `index`).
    include_untracked: push also stashes new (untracked) files.
    index: stash number for pop/apply/drop (0 = newest).
    Only available when ALLOW_MUTATIONS=true."""
    _require_mutations()
    cwd = _resolve_repo(repo_path)
    action = action.strip().lower()
    if action not in ("push", "list", "pop", "apply", "drop"):
        raise ValueError("action must be one of: push, list, pop, apply, drop")
    if not (0 <= index < 1000):
        raise ValueError("index must be a non-negative integer")

    stash_lines = _run_git(["stash", "list"], cwd).stdout.splitlines()
    n_stashes = len(stash_lines)
    if action == "list":
        return "\n".join(stash_lines) or "(no stashes)"

    if action == "push":
        porcelain = _run_git(["status", "--porcelain"], cwd).stdout
        lines = [l for l in porcelain.splitlines() if l.strip()]
        tracked = [l for l in lines if not l.startswith("??")]
        untracked = [l for l in lines if l.startswith("??")]
        if not tracked and not (include_untracked and untracked):
            if untracked:
                return "Nothing tracked to stash — only untracked files present. Pass include_untracked=True."
            return "Nothing to stash — working tree is clean."
        args = ["stash", "push"]
        if include_untracked:
            args.append("-u")
        if message:
            args += ["-m", message]
        _run_git(args, cwd)
        n = len(tracked) + (len(untracked) if include_untracked else 0)
        return f"Stashed {n} file(s) as stash@{{{n_stashes}}}."

    if index >= n_stashes:
        return f"(no stash #{index} — {n_stashes} stash(es) exist)"
    stash_ref = f"stash@{{{index}}}"
    if action == "drop":
        _run_git(["stash", "drop", stash_ref], cwd)
        return f"Dropped {stash_ref} ({n_stashes - 1} remaining)."
    verb = "pop" if action == "pop" else "apply"
    result = _run_git(["stash", verb, stash_ref], cwd, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"Stash {verb} failed (exit {result.returncode}): "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip() or f"Applied {stash_ref}."


@mcp.tool()
def git_merge(
    repo_path: str,
    branch: str,
    message: str = "",
    no_fast_forward: bool = False,
) -> str:
    """Merge `branch` into the currently checked-out branch.
    Fast-forwards when possible; otherwise creates a merge commit.
    no_fast_forward=True always creates a merge commit (--no-ff).
    message: custom message for the merge commit (ignored on fast-forward).
    On conflict: the merge is paused with conflict markers in the tree and a
    list of the conflicted files is returned. Then resolve them (edit the
    files, git_add, git_commit — which completes the merge), or discard
    everything with git_merge_abort.
    Only available when ALLOW_MUTATIONS=true."""
    _require_mutations()
    cwd = _resolve_repo(repo_path)
    branch = _check_ref(branch)
    cur = _current_branch(cwd)
    if not cur:
        return "(detached HEAD — check out a branch to merge into first)"
    if branch == cur:
        return f"Refusing to merge '{branch}' into itself."
    if not _has_commits(cwd):
        return "(no commits yet — nothing to merge)"
    if not _ref_exists(cwd, branch):
        return f"(unknown branch or ref: {branch})"
    if _ref_exists(cwd, "MERGE_HEAD"):
        return "A merge is already in progress — resolve the conflicts or git_merge_abort first."
    # Structural 'already merged' check — no output parsing.
    if _run_git(["merge-base", "--is-ancestor", branch, "HEAD"], cwd, check=False).returncode == 0:
        return f"Branch '{branch}' is already up to date with '{cur}' — nothing to merge."
    args = ["merge"]
    if no_fast_forward:
        args.append("--no-ff")
    args.append(branch)
    if message:
        args += ["-m", message]
    result = _run_git(args, cwd, check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    # Structural conflict detection: unmerged entries in the index.
    conflicted = _run_git(["diff", "--name-only", "--diff-filter=U"], cwd).stdout.strip()
    if conflicted:
        files = "\n".join(f"- {p}" for p in conflicted.splitlines())
        return (
            f"Merge conflict in {len(conflicted.splitlines())} file(s):\n{files}\n"
            "The tree now contains conflict markers. To finish: resolve the files,\n"
            "git_add them, then git_commit (this completes the merge). "
            "Or discard the merge: git_merge_abort."
        )
    raise RuntimeError(
        f"Merge failed (exit {result.returncode}): "
        f"{(result.stderr or result.stdout).strip()}"
    )


@mcp.tool()
def git_merge_abort(repo_path: str) -> str:
    """Discard an in-progress merge (e.g. after unresolved conflicts) and
    restore the pre-merge state. Only available when ALLOW_MUTATIONS=true."""
    _require_mutations()
    cwd = _resolve_repo(repo_path)
    if not _ref_exists(cwd, "MERGE_HEAD"):
        return "(no merge in progress — nothing to abort)"
    _run_git(["merge", "--abort"], cwd)
    status = _run_git(["status", "--short"], cwd).stdout.strip()
    return f"Merge aborted — back to pre-merge state.\n{status or '(working tree clean)'}"


# ── Setup ─────────────────────────────────────────────────────────────────
@mcp.tool()
def git_init(repo_path: str) -> str:
    """Initialize a new git repository at repo_path. If one already exists,
    returns its current status instead. Writes a default .gitignore if none
    exists (same as auto-init)."""
    p = Path(repo_path).resolve()
    if not p.is_relative_to(REPO_ROOT):
        raise ValueError(f"repo_path must be inside {REPO_ROOT} (got: {repo_path})")
    if not p.is_dir():
        raise ValueError(f"Directory does not exist: {p}")
    if (p / ".git").exists():
        return f"Already a git repo.\n{_run_git(['status', '--short'], str(p)).stdout.strip()}"
    _run_git(["init"], str(p))
    gi = p / ".gitignore"
    if not gi.exists():
        gi.write_text(DEFAULT_GITIGNORE)
    return f"Initialized git repo at {p}"


# ── Health endpoint (served next to the MCP endpoint) ────────────────────
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "local-git-mcp-server", "version": "1.0"})


# ── Auto-init on startup ─────────────────────────────────────────────────
def _auto_init() -> None:
    """On startup: if REPO_ROOT isn't a git repo yet, init it in place."""
    if not REPO_ROOT.is_relative_to(WORKSPACE_DIR):
        print(
            f"[init] ERROR: REPO_ROOT ({REPO_ROOT}) is not inside the mount "
            f"({WORKSPACE_DIR})",
            flush=True,
        )
        return
    if not REPO_ROOT.is_dir():
        print(f"[init] ERROR: REPO_ROOT does not exist: {REPO_ROOT}", flush=True)
        return
    if (REPO_ROOT / ".git").exists():
        return
    print(f"[init] initializing git repo: {REPO_ROOT}", flush=True)
    result = subprocess.run(
        ["git", "init"],
        cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=GIT_TIMEOUT, env=_ENV,
    )
    if result.returncode != 0:
        print(f"[init] git init failed: {result.stderr.strip()}", flush=True)
        return
    gi = REPO_ROOT / ".gitignore"
    if not gi.exists():
        gi.write_text(DEFAULT_GITIGNORE)
        print("[init] wrote default .gitignore", flush=True)


# ── Entry point ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    _auto_init()
    mcp.run(
        transport="http",
        stateless_http=True,
        host="0.0.0.0",
        port=PORT,
    )
