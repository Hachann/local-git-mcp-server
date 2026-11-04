#!/usr/bin/env bash
# Management of the local-git mcp server 1.0 container (local build).
# Builds the image, starts/stops the container, shows status/logs,
# and offers an interactive menu.

set -euo pipefail

NAME="local-git-mcp-server-1.0"
IMAGE="local-git-mcp-server:1.0"
BUILD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="8001"                       # default port; the container listens on the same port
BIND="127.0.0.1"
CONTAINER_DIR="/workspace"        # default mount point inside the container
ALLOW_MUTATIONS="false"
COMMIT_NAME="MCP Server"
COMMIT_EMAIL="mcp@localhost"
GIT_TIMEOUT="30"
MAX_OUTPUT_BYTES="100000"        # truncation cap for tool outputs
HOST_USER="$(id -u):$(id -g)"  # container uid:gid (default: host user),
                              # so that git files created in the mount belong to that user

# ── helpers ──────────────────────────────────────────────────────────

is_running() {
  [[ -n "$(docker ps -q --filter "name=^${NAME}$")" ]]
}

exists() {
  [[ -n "$(docker ps -aq --filter "name=^${NAME}$")" ]]
}

port_busy() {
  if { exec 3<>"/dev/tcp/${BIND}/$1"; } 2>/dev/null; then
    exec 3>&- || true
    return 0
  fi
  return 1
}

check_port() {
  local p="$1"
  if ! [[ "$p" =~ ^[0-9]{1,5}$ ]] || ((10#$p < 1 || 10#$p > 65535)); then
    echo "Invalid port: $p" >&2
    return 1
  fi
}

normalize_bool() {
  local v="${1,,}"
  case "$v" in
    true|1|y|yes|on) printf 'true' ;;
    *)              printf 'false' ;;
  esac
}

current_folder() {
  local dest
  dest="$(env_value WORKSPACE_DIR || printf '%s' "$CONTAINER_DIR")"
  docker inspect -f '{{range .Mounts}}{{if eq .Destination "'${dest}'"}}{{.Source}}{{end}}{{end}}' "$NAME" 2>/dev/null || true
}

env_value() {
  local key="$1" envs line
  envs="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$NAME" 2>/dev/null || true)"
  while IFS= read -r line; do
    if [[ "$line" == "${key}="* ]]; then
      printf '%s' "${line#"${key}="}"
      return 0
    fi
  done <<< "$envs"
  return 1
}

current_port()              { env_value PORT || printf '%s' "$PORT"; }
current_mutations()         { [[ "$(env_value ALLOW_MUTATIONS || true)" == "true" ]] && printf 'on' || printf 'off'; }
current_committer_name()    { env_value GIT_COMMITTER_NAME || printf '%s' "$COMMIT_NAME"; }
current_committer_email()   { env_value GIT_COMMITTER_EMAIL || printf '%s' "$COMMIT_EMAIL"; }
current_repo_root()         { env_value REPO_ROOT || env_value WORKSPACE_DIR || printf '%s' "$CONTAINER_DIR"; }
current_timeout()           { env_value GIT_TIMEOUT || printf '%s' "$GIT_TIMEOUT"; }
current_max_output()        { env_value MAX_OUTPUT_BYTES || printf '%s' "$MAX_OUTPUT_BYTES"; }
current_user()              { env_value RUN_USER || printf '%s' "$HOST_USER"; }

probe_health() {
  local port="${1:-$PORT}" code
  code="$(curl -sS -m 3 -o /dev/null -w '%{http_code}' "http://${BIND}:${port}/health" 2>/dev/null || true)"
  [[ "$code" =~ ^[23] ]]
}

probe_mcp() {
  local port="${1:-$PORT}" code
  code="$(curl -sS -m 5 -o /dev/null -w '%{http_code}' "http://${BIND}:${port}/mcp" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"local-git-mcp-probe","version":"0"}}}' \
    2>/dev/null || true)"
  [[ "$code" =~ ^[234] ]]
}

wait_healthy() {
  local port="$1" i
  for i in $(seq 1 15); do
    probe_health "$port" && return 0
    is_running || return 1
    sleep 1
  done
  return 1
}

remove_container() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
}

build_image() {
  echo "Building image 1.0 from ${BUILD_DIR}…"
  # On Docker CLI >= 23, `docker build` is routed to buildx:
  # - buildx present: force BuildKit explicitly (no DEPRECATED warning,
  #   more efficient layer cache).
  # - buildx absent: leave the legacy fallback — a warning appears
  #   but the build works. Forcing DOCKER_BUILDKIT=1 here would
  #   instead cause an ERROR (documented by Docker). Install buildx to
  #   get rid of the warning for good.
  if docker buildx version >/dev/null 2>&1; then
    if ! DOCKER_BUILDKIT=1 docker build -t "$IMAGE" "$BUILD_DIR"; then
      echo "Build failed — see above." >&2
      return 1
    fi
  elif ! docker build -t "$IMAGE" "$BUILD_DIR"; then
    echo "Build failed — see above." >&2
    return 1
  fi
}

start_failed() {
  echo "Failed to start — logs: " >&2
  docker logs --tail 50 "$NAME" 2>&1 | sed 's/^/    /' >&2 || true
  remove_container
  return 1
}

# ── validation ────────────────────────────────────────────────────────

validate_repo_root() {
  local folder="$1" root="$2"
  # Empty root means "the whole mount is the repo" — that's valid.
  [[ -z "$root" ]] && return 0
  # Reject absolute paths (must be relative to the mount point).
  [[ "$root" == /* ]] && { echo "The subfolder must be a path relative to the mount point (e.g. /workspace): $root" >&2; return 1; }
  # Reject path traversal.
  if [[ "$root" == *".."* ]]; then
    echo "Invalid path (contains '..'): $root" >&2; return 1
  fi
  [[ -d "${folder}/${root}" ]] || { echo "Subfolder not found: ${folder}/${root}" >&2; return 1; }
}

# ── core actions ─────────────────────────────────────────────────────

# Argument order: folder, subfolder, port, mutations, committer_name, committer_email, user, mount
start_container() {
  local folder="$1" repo_root="${2:-}" port="${3:-$PORT}" \
        mutations="${4:-false}" \
        c_name="${5:-$COMMIT_NAME}" c_email="${6:-$COMMIT_EMAIL}" \
        user="${7:-$HOST_USER}" mount="${8:-$CONTAINER_DIR}"

  [[ -d "$folder" ]] || { echo "Folder not found: $folder" >&2; return 1; }
  check_port "$port" || return 1
  validate_repo_root "$folder" "$repo_root" || return 1
  folder="$(cd "$folder" && pwd)"

  # Normalize mutations to "true"/"false"
  mutations="$(normalize_bool "$mutations")"

  # The container runs as a UID (or UID:GID), defaulting to the host user:
  # git files created in the mount then stay owned by them on the host.
  if ! [[ "$user" =~ ^[0-9]+(:[0-9]+)?$ ]]; then
    echo "Invalid user: $user (expected: UID or UID:GID)" >&2
    return 1
  fi

  # The mount point must be an absolute container path (e.g. /workspace, /data).
  if [[ "$mount" != /* || "$mount" == *..* ]]; then
    echo "Invalid mount point: $mount (expected: absolute path like /workspace)" >&2
    return 1
  fi
  mount="${mount%/}"

  # Build the REPO_ROOT container path (strip trailing slashes for cleanliness)
  repo_root="${repo_root%/}"
  local container_repo_root="${mount}"
  if [[ -n "$repo_root" ]]; then
    container_repo_root="${mount}/${repo_root}"
  fi

  # Port in use = tolerated only if it is OUR container that already uses it.
  if port_busy "$port"; then
    if is_running && [[ "$(current_port)" == "$port" ]]; then
      :
    else
      echo "Port ${port} is already in use by another program." >&2
      return 1
    fi
  fi

  build_image || return 1

  if exists; then
    echo "Restarting container…"
    remove_container
  fi

  if ! docker run -d \
    --rm \
    --user "$user" \
    --name "$NAME" \
    --restart no \
    -p "${BIND}:${port}:${port}" \
    -v "${folder}:${mount}" \
    -e "WORKSPACE_DIR=${mount}" \
    -e "REPO_ROOT=${container_repo_root}" \
    -e PORT="$port" \
    -e "ALLOW_MUTATIONS=${mutations}" \
    -e "GIT_COMMITTER_NAME=${c_name}" \
    -e "GIT_COMMITTER_EMAIL=${c_email}" \
    -e "GIT_TIMEOUT=${GIT_TIMEOUT}" \
    -e "MAX_OUTPUT_BYTES=${MAX_OUTPUT_BYTES}" \
    -e "RUN_USER=${user}" \
    "$IMAGE" >/dev/null; then
    start_failed || return 1
  fi

  if ! wait_healthy "$port"; then
    echo "The server did not respond on http://${BIND}:${port}/health."
    start_failed || return 1
  fi

  if probe_mcp "$port"; then
    :
  else
    echo "⚠ /health responds but /mcp does NOT — check with: $0 logs" >&2
  fi

  echo "local-git mcp server 1.0 started"
  echo "Version   : 1.0 (git_add, git_reset, git_show, git_blame, branches, stash, merge, extended diff)"
  echo "Folder    : $folder"
  echo "Git root  : ${container_repo_root}"
  echo "Port      : $port"
  echo "Mutations : $mutations"
  echo "Committer : ${c_name} <${c_email}>"
  echo "User      : $user"
  echo "URL       : http://${BIND}:${port}/mcp"
  echo
  echo "Paste into your MCP client config:"
  echo "  {\"mcpServers\":{\"git\":{\"url\":\"http://${BIND}:${port}/mcp\"}}}"
}

status() {
  if is_running; then
    local port mutations
    port="$(current_port)"
    mutations="$(current_mutations)"
    echo "Running (1.0)"
    echo "Folder    : $(current_folder)"
    echo "Git root  : $(current_repo_root)"
    echo "Port      : $port"
    echo "Mutations : $mutations"
    echo "Committer : $(current_committer_name) <$(current_committer_email)>"
    echo "User      : $(current_user)"
    echo "URL       : http://${BIND}:${port}/mcp"
    if probe_health "$port"; then
      echo "Health    : responding (/health)"
    else
      echo "Health    : NOT responding"
    fi
  elif exists; then
    echo "Residual container (no --rm) — remove with: docker rm -f $NAME"
  else
    echo "Stopped"
  fi
}

usage() {
  echo "Usage:"
  echo "  $0                                        (interactive menu)"
  echo "  $0 start /path [git-subfolder] [port] [mutations] [name] [email] [user] [mount]"
  echo "  $0 stop"
  echo "  $0 status"
  echo "  $0 logs"
  echo "  $0 rebuild"
  echo "  $0 /path [git-subfolder] [port] [mutations] [name] [email] [user] [mount]   (shorthand for start)"
  echo "  $0 -h|--help"
  echo
  echo "  git-subfolder : path relative to the mount point of the git folder."
  echo "                  Empty = the whole mounted folder."
  echo
  echo "  port       : 8001 by default"
  echo "  mutations  : true/false (also accepts y/yes/on/1)"
  echo "  user       : UID or UID:GID of the container (default: $(id -u):$(id -g)) —"
  echo "               git files created in the mount belong to this uid."
  echo "  mount      : mount point inside the container (default: ${CONTAINER_DIR});"
  echo "               REPO_ROOT is derived from it."
}

# ── interactive prompts ──────────────────────────────────────────────

ask_all() {
  local folder root port mutations c_name c_email user mount
  echo >&2
  echo "─── Starting local-git mcp server 1.0 ──────────────────────" >&2
  IFS= read -r -e -p "Folder to expose (absolute path): " folder || return 1
  IFS= read -r -e -p "Mount point inside the container [${CONTAINER_DIR}]: " mount || return 1
  IFS= read -r -e -p "Git subfolder inside the mount point (empty = all): " root || return 1
  IFS= read -r -e -p "Port [${PORT}]: " port || return 1
  IFS= read -r -e -p "Enable mutations? (y=yes / n=no) [n]: " mutations || return 1
  IFS= read -r -e -p "Committer name [${COMMIT_NAME}]: " c_name || return 1
  IFS= read -r -e -p "Committer email [${COMMIT_EMAIL}]: " c_email || return 1
  IFS= read -r -e -p "Container user [${HOST_USER}]: " user || return 1
  # Trailing newline so the last read in the consumer block succeeds.
  printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n%s\n' "$folder" "$root" "$port" "$mutations" "$c_name" "$c_email" "$user" "$mount"
}

# ── entry point ──────────────────────────────────────────────────────

docker info >/dev/null 2>&1 || { echo "Docker is not reachable." >&2; exit 1; }

cmd="${1:-}"
case "$cmd" in
  start)
    [[ -n "${2:-}" ]] || { usage; exit 1; }
    start_container "$2" "${3:-}" "${4:-$PORT}" "${5:-false}" \
      "${6:-$COMMIT_NAME}" "${7:-$COMMIT_EMAIL}" "${8:-$HOST_USER}" \
      "${9:-$CONTAINER_DIR}"
    ;;

  stop)
    if exists; then
      docker stop "$NAME" >/dev/null 2>&1
      echo "Stopped and removed (--rm) — restart with: $0 start <path>"
    else
      echo "Already stopped."
    fi
    ;;

  status)
    status
    ;;

  logs)
    is_running || { echo "No container." >&2; exit 1; }
    docker logs -f --tail 100 "$NAME"
    ;;

  rebuild)
    build_image && echo "Image 1.0 rebuilt. Restart with: $0 start <path>"
    ;;

  -h|--help)
    usage
    ;;

  "")
    while true; do
      echo "=== local-git mcp server 1.0 — local-git-mcp-server-1.0 ==="
      status
      echo
      echo "1) Start / reconfigure"
      echo "2) Stop"
      echo "3) Logs"
      echo "4) Rebuild the image"
      echo "5) Quit"
      read -r -p "Choice: " choice || exit 0
      case "$choice" in
        1)
          # Read eight lines from ask_all (trailing newline guarantees success)
          {
            IFS= read -r folder
            IFS= read -r root
            IFS= read -r port
            IFS= read -r mutations
            IFS= read -r c_name
            IFS= read -r c_email
            IFS= read -r user
            IFS= read -r mount
          } < <(ask_all) || { echo "Cancelled."; continue; }

          # Apply defaults for the optional fields
          port="${port:-$PORT}"
          mutations="$(normalize_bool "${mutations:-false}")"
          c_name="${c_name:-$COMMIT_NAME}"
          c_email="${c_email:-$COMMIT_EMAIL}"
          user="${user:-$HOST_USER}"
          mount="${mount:-$CONTAINER_DIR}"

          validate_repo_root "$folder" "$root" || continue
          start_container "$folder" "$root" "$port" "$mutations" "$c_name" "$c_email" "$user" "$mount" || true
          ;;
        2)
          if exists; then
            docker stop "$NAME" >/dev/null 2>&1
            echo "Stopped and removed (--rm)."
          else
            echo "Already stopped."
          fi
          ;;
        3)
          if is_running; then
            docker logs --tail 100 "$NAME" 2>&1 || true
          else
            echo "No container."
          fi
          ;;
        4)
          build_image && echo "Image 1.0 rebuilt."
          ;;
        *)
          exit 0
          ;;
      esac
      read -r -p $'\nPress Enter to continue…' || true
      echo
    done
    ;;

  *)
    if [[ -d "$cmd" ]]; then
      start_container "$cmd" "${2:-}" "${3:-$PORT}" "${4:-false}" \
        "${5:-$COMMIT_NAME}" "${6:-$COMMIT_EMAIL}" "${7:-$HOST_USER}" \
        "${8:-$CONTAINER_DIR}"
    else
      usage
      exit 1
    fi
    ;;
esac
