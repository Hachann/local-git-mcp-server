# local-git mcp server

Author: `hachann`

Containerized, stateless local-git mcp server (FastMCP over Streamable HTTP) that
shells out to the `git` CLI. Sandboxed to `REPO_ROOT`, mutations gated behind
`ALLOW_MUTATIONS=true`.

## Features

- Full local workflow: selective staging (`git_add`, `git_reset`), commits with a
  per-file audit trail, `git_show` / `git_log` / `git_blame` history inspection,
  branches, stash, and merges with conflict reporting
- Locale-independent, exit-code-based state detection (git always runs with
  `LC_ALL=C`); mutation refusal raises a real MCP error
- Sandboxed: every path is validated to stay inside `REPO_ROOT`; no remote
  operations (push/pull/fetch/clone) — local-only by design
- Repo auto-initialized on startup with a default `.gitignore`

## How it works: mounting and `REPO_ROOT`

One host folder is bind-mounted into the container at `WORKSPACE_DIR`
(`/workspace` by default — you can change it to any `/name` you like). You
then decide how much of it is the git repository, via `REPO_ROOT`:

- **The whole mount** — `REPO_ROOT=/workspace` (the default). The folder you
  mounted *is* the repository.
- **A specific subfolder** — e.g. mount a large project directory and set
  `REPO_ROOT=/workspace/project` so only that subfolder is git-managed.
  Everything else under `/workspace` stays untouched and is simply not
  exposed to the tools.

`REPO_ROOT` must be the root of an existing git repo (it contains the
`.git/` directory) and must live inside the mount; if it isn't a repo yet, it
is initialized in place on startup (with a default `.gitignore` if missing).
To use a different mount point, set `WORKSPACE_DIR` (e.g. `/data`) and point
`REPO_ROOT` inside it (e.g. `/data/project`). All tools receive container
paths, e.g. `repo_path="/workspace"` or `repo_path="/workspace/project"`.

## Tools

| Tool | Kind | Notes |
|---|---|---|
| `git_status` | read | `short=`, `path=` filter |
| `git_log` | read | `count=`, `path=` (per-file history), `full=` (commit bodies) |
| `git_diff` | read | `ref=`, `ref2=` (two-ref range), `staged=`, `stat=`, `path=` |
| `git_show` | read | commit message+diff, or `ref:path` file content at an old ref |
| `git_blame` | read | per-line commit/author/date; `ref=`, `start=`/`end=` line range (`start` alone = one line) |
| `git_branches` | read | local branches, `-vv` |
| `git_current_branch` | read | branch name or `HEAD (detached)` |
| `git_remote_url` | read | remote names + URLs |
| `git_add` | mutation | selective staging (`paths` list; default `.`) |
| `git_reset` | mutation | `soft` / `mixed` (default) / `hard`, to any ref |
| `git_commit` | mutation | `paths=` subset, `amend=`; without `paths` stages tracked changes only (`git add -u` — untracked files are NOT auto-added); in-progress merges commit the index as-is; result always lists every file in the commit (A/M/D/R) |
| `git_restore` | mutation | discard changes to a path, worktree+index, to any ref |
| `git_revert` | mutation | compensating commit for any ref |
| `git_branch_create` | mutation | from any ref, optional immediate checkout |
| `git_branch_delete` | mutation | refuses current branch; `force=` for unmerged (-D) |
| `git_checkout` | mutation | branch or hash (detached); refuses dirty tree unless `discard=`; `create=` new branch |
| `git_stash` | mutation | push/pop/apply/drop/list, `include_untracked=`, `index=` |
| `git_merge` | mutation | `branch` into current branch; `no_fast_forward=`, `message=`; returns conflicted-file list on conflict |
| `git_merge_abort` | mutation | discard an in-progress merge, restore pre-merge state |
| `git_init` | setup | inits a repo in place, writes default `.gitignore` |

## Environment variables

| Var | Default | Meaning |
|---|---|---|
| `WORKSPACE_DIR` | `/workspace` | where the host folder is bind-mounted inside the container (any `/name` works); `REPO_ROOT` must live inside it |
| `REPO_ROOT` | = `WORKSPACE_DIR` | git root: the whole mount or a subfolder of it (see How it works); auto-initialized if missing |
| `ALLOW_MUTATIONS` | `false` | `true` enables all mutation tools |
| `GIT_TIMEOUT` | `30` | per-command timeout (seconds) |
| `GIT_COMMITTER_NAME` / `GIT_COMMITTER_EMAIL` | `MCP Server` / `mcp@localhost` | commit identity (honors any `git config` values the client exports) |
| `PORT` | `8000` | HTTP port |
| `MAX_OUTPUT_BYTES` | `100000` | output truncation cap |

## Quick start (manager script)

The manager script at repo root (`local_git_mcp_server_launcher.sh`) builds
the image and manages the container. It's very human friendly: run it with no
arguments and you get an interactive menu that walks you step by step through
every parameter (folder to expose, git subfolder inside the mount —
empty = the whole mount is the repo, port, mutations, committer
name/email, container user, mount point inside the container) — each with
its default shown in the prompt, so you can just press Enter to accept it. It also verifies the server is healthy
and prints a ready-to-paste MCP client config at the end.

```bash
chmod +x local_git_mcp_server_launcher.sh

./local_git_mcp_server_launcher.sh                    # interactive menu, step by step
```

Prefer the command line? Every step can also be passed as an argument, in the
same order as the interactive prompts (everything after the folder is
optional):

```bash
./local_git_mcp_server_launcher.sh start /path/to/project [git-subfolder] [port] [mutations] [name] [email] [user] [mount]
```

`[mount]` (last argument, optional) is the mount point inside the container
— `/workspace` by default; e.g. `[mount]=/data` to use a different `/name`.

Or pass a folder directly as the first argument (shorthand for `start`):

```bash
./local_git_mcp_server_launcher.sh /path/to/project
```

Useful day-to-day commands:

```bash
./local_git_mcp_server_launcher.sh status
./local_git_mcp_server_launcher.sh stop
./local_git_mcp_server_launcher.sh logs
./local_git_mcp_server_launcher.sh rebuild
```

## Build & run (manually)

Build from this directory:

```bash
docker build -t local-git-mcp-server:1.0 .

# The whole mount is the repo:
docker run -d --name local-git-mcp-server-1.0 \
  -v "$PWD/project:/workspace" \
  -e REPO_ROOT=/workspace \
  -e ALLOW_MUTATIONS=true \
  -p 8001:8000 \
  local-git-mcp-server:1.0

# …or only a subfolder of the mount is the repo (change -e REPO_ROOT):
#   -e REPO_ROOT=/workspace/subfolder

# …or mount at a different point (any /name) and target a subfolder
# (change -v and -e WORKSPACE_DIR / -e REPO_ROOT):
#   -v "$PWD/monorepo:/data"  -e WORKSPACE_DIR=/data  -e REPO_ROOT=/data/project

curl -s localhost:8001/health
# {"status":"ok","service":"local-git-mcp-server","version":"1.0"}
```

MCP endpoint: `POST http://localhost:8001/mcp` (stateless Streamable HTTP).

## Client snippet (Python)

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client("http://localhost:8001/mcp") as (r, w, _):
    async with ClientSession(r, w) as s:
        await s.initialize()
        tools = await s.list_tools()
        print([t.name for t in tools.tools])
        print((await s.call_tool("git_log", {"repo_path": "/workspace"})).content)
```

## Known limitations

- No remote operations (push/pull/fetch/clone) — local-only by design.
- No auth on the HTTP endpoint; keep it on localhost/tailnet (a bearer token
  is the suggested next step).
- No lock around concurrent mutations on the same repo.
- Untracked files never appear in `git_diff` (use `git_status`); `git_diff` has
  no `--no-index` mode for brand-new file contents.
- No tests yet — a pytest suite against temp repos is the recommended next step.
