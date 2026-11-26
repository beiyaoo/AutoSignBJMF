#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

repository=${BJMF_UPDATE_REPOSITORY:-beiyaoo/AutoSignBJMF}
ref=${BJMF_UPDATE_REF:-feat/admin-running-tasks}
root=${BJMF_UPDATE_ROOT:-/opt/AutoSignBJMF}
container=bjmf-web
db_path=/app/web_signin/db

log() { printf '%s\n' "$*"; }
fail() { printf '更新停止：%s\n' "$*" >&2; exit 1; }

for program in docker curl tar flock; do
    command -v "$program" >/dev/null || fail "请先在服务器安装 $program。"
done
[[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ && "$repository" != *..* ]] || fail 'GitHub 仓库名无效。'
[[ "$ref" =~ ^[A-Za-z0-9._-][A-Za-z0-9._/-]*$ && "$ref" != *..* ]] || fail 'GitHub 分支、标签或提交号无效。'
inspect() { docker container inspect --format "$1" "$2"; }
container_label() { inspect "{{with index .Config.Labels \"$1\"}}{{.}}{{end}}" "$2"; }
image_id() { docker image inspect --format '{{.Id}}' "$1"; }

read_env() {
    # Read the last unquoted KEY=value entry without executing the env files.
    local key=$1 line file found=''
    shift
    for file; do
        [[ -f "$file" && -r "$file" ]] || continue
        while IFS= read -r line || [[ -n "$line" ]]; do
            if [[ "$line" == "$key="* ]]; then
                found=${line#*=}
            fi
        done < "$file"
    done
    printf '%s' "${found%$'\r'}"
}

compose() (
    cd -- "$compose_dir" || exit 1
    COMPOSE_REMOVE_ORPHANS=false "${compose_command[@]}" "${compose_env_args[@]}" "$@"
)

compose_containers() {
    docker ps --all --quiet --no-trunc \
        --filter "label=com.docker.compose.project=$compose_project" \
        --filter "label=com.docker.compose.service=$compose_service" \
        --filter 'label=com.docker.compose.oneoff=False'
}

compose_hash_matches() {
    local hashes
    hashes=$(compose config --hash "$compose_service" 2>/dev/null) || return 1
    awk -v service="$compose_service" -v expected="$compose_hash" '
        $1 == service && $2 == expected { matches++ }
        END { exit (matches == 1 ? 0 : 1) }
    ' <<< "$hashes"
}

compose_create() {
    compose up -d --no-deps --no-build --pull never --force-recreate --no-start "$compose_service"
}

compose_volume_matches() {
    [[ "$mount_type" == volume ]] || return 0
    # Stream resolved config directly to the parser; it can contain credentials.
    compose config --format json "$compose_service" 2>/dev/null | python3 -c '
import json
import sys

service, target, expected = sys.argv[1:]
try:
    config = json.load(sys.stdin)
    mounts = config["services"][service].get("volumes", [])
    selected = [mount for mount in mounts if mount.get("target") == target and mount.get("type") == "volume"]
    physical_name = config["volumes"][selected[0]["source"]]["name"] if len(selected) == 1 else None
except (ValueError, KeyError, TypeError, AttributeError):
    sys.exit(1)
sys.exit(0 if physical_name == expected else 1)
' "$compose_service" "$db_path" "$volume_name"
}

old_id=$(inspect '{{.Id}}' "$container" 2>/dev/null) || fail '没有找到 bjmf-web 容器。首次安装请按部署教程启动。'
[[ "$(inspect '{{.State.Running}}' "$old_id")" == true ]] || fail '原容器未运行，请先确认并启动 bjmf-web。'
compose_project=$(container_label 'com.docker.compose.project' "$old_id")
compose_mode=false
if [[ -n "$compose_project" && "$compose_project" != '<no value>' ]]; then
    compose_mode=true
    compose_dir=$(container_label 'com.docker.compose.project.working_dir' "$old_id")
    compose_service=$(container_label 'com.docker.compose.service' "$old_id")
    compose_configs=$(container_label 'com.docker.compose.project.config_files' "$old_id")
    compose_hash=$(container_label 'com.docker.compose.config-hash' "$old_id")
    [[ "$compose_project" =~ ^[a-z0-9][a-z0-9_-]*$ && "$compose_service" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ &&
        "$compose_hash" =~ ^[a-f0-9]{64}$ && "$compose_dir" == /* && -d "$compose_dir" && -n "$compose_configs" ]] \
        || fail '无法识别原 Compose 项目的配置文件或工作目录，请在原项目目录使用 Compose 命令升级。'
    docker compose version >/dev/null 2>&1 || fail '请先安装 Docker Compose 插件，并运行 docker compose version 确认可用。'
    root=${BJMF_UPDATE_ROOT:-$compose_dir}
else
    env_file=${BJMF_UPDATE_ENV_FILE:-$root/docker.env}
fi
root=$(cd -- "$root" && pwd -P) || fail '项目目录不存在，请设置 BJMF_UPDATE_ROOT。'
[[ "$root" != / && "$root" != *,* ]] || fail '项目目录不能是根目录或含逗号。'
lock_dir=$root
if "$compose_mode"; then lock_dir=$compose_dir; fi
exec 9>"$lock_dir/.bjmf-update.lock"
flock -n 9 || fail '该项目已有更新正在进行。'

pip_env_files=()
if "$compose_mode"; then
    compose_command=(docker compose --project-name "$compose_project" --project-directory "$compose_dir")
    compose_env_args=()
    IFS=, read -r -a config_files <<< "$compose_configs"
    for filename in "${config_files[@]}"; do
        [[ "$filename" == /* && -f "$filename" && -r "$filename" ]] \
            || fail "找不到原 Compose 配置文件 $filename。请恢复该文件后重试。"
        compose_command+=(-f "$filename")
    done

    if [[ -n "${BJMF_UPDATE_ENV_FILE:-}" ]]; then
        env_file=$BJMF_UPDATE_ENV_FILE
        if [[ "$env_file" != /* ]]; then env_file=$PWD/$env_file; fi
        env_files=("$env_file")
    else
        compose_env=$(container_label 'com.docker.compose.project.environment_file' "$old_id")
        env_files=()
        if [[ -n "$compose_env" && "$compose_env" != '<no value>' ]]; then
            IFS=, read -r -a env_files <<< "$compose_env"
        fi
    fi
    for filename in "${env_files[@]}"; do
        if [[ "$filename" != /* ]]; then filename=$compose_dir/$filename; fi
        [[ -f "$filename" && -r "$filename" ]] || fail "原 Compose 环境文件 $filename 不可读。请恢复该文件或用 BJMF_UPDATE_ENV_FILE 指定原配置。"
        compose_env_args+=(--env-file "$filename")
        pip_env_files+=("$filename")
    done

    if [[ ${#compose_env_args[@]} -eq 0 ]]; then
        pip_env_files=("$compose_dir/.env")
        # Older Compose versions may not record the --env-file argument.
        if ! compose_hash_matches && [[ -r "$compose_dir/docker.env" ]]; then
            compose_env_args=(--env-file "$compose_dir/docker.env")
            pip_env_files=("$compose_dir/docker.env")
        fi
    fi
    compose_hash_matches || fail '当前 Compose 配置与运行中的服务不同，或 Compose 版本不支持配置核验。请使用启动时的配置和环境变量（可用 BJMF_UPDATE_ENV_FILE 指定原环境文件），并运行 docker compose version 检查版本。原容器仍在运行。'
    [[ "$(compose_containers)" == "$old_id" ]] || fail '原项目的服务容器不唯一，请在原 Compose 项目中手动升级。'
    compose_image=$(inspect '{{.Config.Image}}' "$old_id")
    old_image_id=$(inspect '{{.Image}}' "$old_id")
    configured_images=$(compose config --images "$compose_service" 2>/dev/null) \
        || fail '无法读取当前 Compose 服务的镜像配置。原容器仍在运行。'
    [[ -n "$compose_image" && "$compose_image" != *@* && "$compose_image" != sha256:* &&
        "$configured_images" == "$compose_image" && "$(image_id "$compose_image" 2>/dev/null)" == "$old_image_id" ]] \
        || fail 'Compose 服务镜像标签已变化，或使用了固定镜像摘要。请按原 Compose 配置手动升级。原容器仍在运行。'
else
    [[ -f "$env_file" && -r "$env_file" ]] || fail "找不到 $env_file。请先复制 docker.env.example 为 docker.env，填好管理员配置。"
    container_port=$(read_env WEB_SIGNIN_PORT "$env_file")
    container_port=${container_port:-9988}
    [[ "$container_port" =~ ^[1-9][0-9]{0,4}$ ]] && (( container_port <= 65535 )) || fail '请在 docker.env 中填写有效的 WEB_SIGNIN_PORT（通常为 9988）。'
    pip_env_files=("$env_file")
fi
pip_index=${BJMF_UPDATE_PIP_INDEX_URL:-$(read_env PIP_INDEX_URL "${pip_env_files[@]}")}
pip_index=${pip_index:-https://pypi.org/simple}

[[ "$(inspect '{{.HostConfig.AutoRemove}}|{{.HostConfig.Privileged}}' "$old_id")" == 'false|false' ]] || fail '自动删除或特权容器请按原 Docker 参数手动升级。'
if ! "$compose_mode"; then
    network=$(inspect '{{.HostConfig.NetworkMode}}' "$old_id")
    [[ "$network" == default || "$network" == bridge ]] || fail '自定义网络请按原 Docker 参数手动升级。'
fi

port_bindings() { inspect '{{range $port, $items := .HostConfig.PortBindings}}{{range $items}}{{printf "%s|%s|%s\n" $port .HostIp .HostPort}}{{end}}{{end}}' "$1"; }
bindings=$(port_bindings "$old_id")
[[ -n "$bindings" && "$bindings" != *$'\n'* ]] || fail '脚本要求旧容器只有一个端口映射。多个端口请按原 Docker 参数手动升级。'
IFS='|' read -r old_port host_ip host_port <<< "$bindings"
[[ "$old_port" == */tcp && "$host_port" =~ ^[1-9][0-9]{0,4}$ ]] || fail '旧容器没有可保留的固定 TCP 端口映射。'
if "$compose_mode"; then container_port=${old_port%/tcp}; fi
if [[ "$host_ip" == *:* ]]; then
    publish="[$host_ip]:$host_port:$container_port"
    health_host="[$host_ip]"
    [[ "$host_ip" != '::' ]] || health_host='[::1]'
elif [[ -n "$host_ip" ]]; then
    publish="$host_ip:$host_port:$container_port"
    health_host=$host_ip
    [[ "$host_ip" != '0.0.0.0' ]] || health_host=127.0.0.1
else
    publish="$host_port:$container_port"
    health_host=127.0.0.1
fi

container_mounts() { inspect '{{range .Mounts}}{{printf "%s|%s|%s|%s|%t\n" .Destination .Type .Source .Name .RW}}{{end}}' "$1"; }
mounts=$(container_mounts "$old_id")
db_mount=''
if [[ -n "$mounts" ]]; then
    [[ "$mounts" != *$'\n'* ]] || fail '旧容器还有其他挂载，请按原 Docker 参数手动升级。'
    IFS='|' read -r destination mount_type mount_source volume_name writable <<< "$mounts"
    [[ "$destination" == "$db_path" && "$writable" == true ]] || fail "只支持可写的 $db_path 数据库目录挂载。"
    case "$mount_type" in
        bind)
            [[ "$mount_source" == /* && "$mount_source" != / && "$mount_source" != *,* ]] || fail '数据库挂载目录无法安全复用。'
            db_mount="type=bind,source=$mount_source,target=$db_path"
            ;;
        volume)
            [[ "$volume_name" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]+$ ]] || fail '数据库卷名无法安全复用。'
            db_mount="type=volume,source=$volume_name,target=$db_path,volume-nocopy"
            ;;
        *) fail '数据库使用了不支持的挂载方式，请按部署教程手动迁移。' ;;
    esac
fi
if "$compose_mode"; then
    [[ -n "$db_mount" ]] || fail "Compose 服务需要持久化挂载 $db_path。请先按部署教程备份并迁移数据库。原容器仍在运行。"
    if [[ "$mount_type" == volume ]]; then
        command -v python3 >/dev/null || fail '请先安装 Python 3（Ubuntu 可执行 sudo apt install python3）以核验 Compose 数据卷。原容器仍在运行。'
        compose_volume_matches || fail '当前 Compose 配置使用的数据卷与原容器不同，或无法读取配置。请恢复原数据卷配置再重试。原容器仍在运行。'
    fi
fi

version="$(date -u +%Y%m%d-%H%M%S)-$$"
image="bjmf-web:online-$version"
backup_name="$container-backup-$version"
backup_image="bjmf-web:backup-$version"
backup_dir="$root/bjmf-web-backups/$version"
scratch=$(mktemp -d /tmp/bjmf-update.XXXXXXXX)
switched=false
candidate_attempted=false
compose_tagged=false

verify_compose_container() {
    local expected_image=$1 id
    id=$(inspect '{{.Id}}' "$container" 2>/dev/null) || return 1
    [[ "$(compose_containers)" == "$id" &&
        "$(inspect '{{.Image}}' "$id")" == "$expected_image" &&
        "$(inspect '{{.State.Running}}' "$id")" == false &&
        "$(port_bindings "$id")" == "$bindings" &&
        "$(container_mounts "$id")" == "$mounts" ]] || return 1
    printf '%s\n' "$id"
}

restore_database() {
    docker run --rm -i --network none --mount "$db_mount" --entrypoint sh "$image" -ceu '
        cd /app/web_signin/db
        staging=$(mktemp -d ./.bjmf-rollback.XXXXXXXX)
        tar -xpf - -C "$staging"
        test -s "$staging/web_signin.db"
        mv -fT -- "$staging/web_signin.db" web_signin.db
        for name in web_signin.db-wal web_signin.db-shm web_signin.db-journal; do
            if [ -f "$staging/$name" ]; then
                mv -fT -- "$staging/$name" "$name"
            else
                rm -f -- "$name"
            fi
        done
        rmdir -- "$staging"
    ' < "$backup_dir/database.tar"
}

rollback_compose() {
    local ids id restored_id
    ids=$(compose_containers) || return 1
    for id in $ids; do
        [[ "$id" != "$old_id" ]] || continue
        # Only touch a candidate from this update, never an independently replaced service.
        [[ -n "${new_image_id:-}" && "$(inspect '{{.Image}}' "$id")" == "$new_image_id" ]] || return 1
        docker logs --tail 100 "$id" > "$backup_dir/new-container.log" 2>&1 || true
        if [[ "$(inspect '{{.State.Running}}' "$id")" == true ]]; then
            docker stop "$id" >/dev/null || return 1
        fi
        docker rm "$id" >/dev/null || return 1
    done
    if "$candidate_attempted"; then
        restore_database || return 1
    fi
    if "$compose_tagged"; then
        docker tag "$old_image_id" "$compose_image" || return 1
    fi
    if inspect '{{.Id}}' "$old_id" >/dev/null 2>&1; then
        if [[ "$(inspect '{{.Name}}' "$old_id")" != "/$container" ]]; then
            docker rename "$old_id" "$container" || return 1
        fi
        restored_id=$old_id
    else
        compose_hash_matches && compose_volume_matches || return 1
        compose_create || return 1
        restored_id=$(verify_compose_container "$old_image_id") || return 1
    fi
    docker start "$restored_id" >/dev/null || return 1
    log '已恢复原 Compose 服务。'
}

rollback() {
    local active_id label
    if "$compose_mode"; then
        rollback_compose
        return
    fi
    active_id=$(inspect '{{.Id}}' "$container" 2>/dev/null) || active_id=''
    if [[ -n "$active_id" && "$active_id" != "$old_id" ]]; then
        label=$(inspect '{{index .Config.Labels "bjmf.update.run"}}' "$active_id") || return 1
        [[ "$label" == "$version" ]] || return 1
        docker logs --tail 100 "$active_id" > "$backup_dir/new-container.log" 2>&1 || true
        docker rm -f "$active_id" >/dev/null || return 1
    fi
    if "$candidate_attempted" && [[ -n "$db_mount" ]]; then
        restore_database || return 1
    fi
    if [[ "$(inspect '{{.Name}}' "$old_id")" != "/$container" ]]; then
        docker rename "$old_id" "$container" || return 1
    fi
    docker start "$old_id" >/dev/null || return 1
    log '已恢复原容器。'
}

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if (( status != 0 )) && "$switched"; then
        log "新版启动失败，正在恢复原容器。备份位置 $backup_dir"
        if ! rollback; then
            if "$compose_mode"; then
                printf '自动恢复未完成。请保留旧镜像 %s 和备份 %s，在原 Compose 项目目录检查服务状态。\n' "$backup_image" "$backup_dir" >&2
            else
                printf '自动恢复未完成。请保留旧容器 %s 和备份 %s，再检查 Docker 状态。\n' "$old_id" "$backup_dir" >&2
            fi
        fi
    fi
    [[ "$scratch" == /tmp/bjmf-update.* && -d "$scratch" ]] && rm -rf -- "$scratch"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

log "从 GitHub 下载 $repository ($ref) 并构建新版。此时旧服务继续运行。"
curl --fail --location --silent --show-error --retry 3 --connect-timeout 15 --max-time 180 \
    --proto '=https' --proto-redir '=https' \
    --output "$scratch/source.tar.gz" "https://codeload.github.com/$repository/tar.gz/$ref" \
    || fail '无法下载 GitHub 源码，请核对仓库、分支和服务器网络。'

# Only regular files and directories from a single GitHub archive root.
tar -tvzf "$scratch/source.tar.gz" > "$scratch/archive-types"
LC_ALL=C awk 'substr($0, 1, 1) !~ /^[-d]$/ { exit 1 }' "$scratch/archive-types" || fail '源码归档含特殊文件。'
tar -tzf "$scratch/source.tar.gz" > "$scratch/archive-paths"
LC_ALL=C awk -F/ 'NR == 1 { root = $1 } $1 == "" || $1 != root || /(^|\/)\.\.?($|\/)/ { exit 1 } END { if (!NR) exit 1 }' \
    "$scratch/archive-paths" || fail '源码归档目录结构不正确。'
mkdir "$scratch/source"
tar -xzf "$scratch/source.tar.gz" --strip-components=1 --no-same-owner --no-same-permissions -C "$scratch/source"
for required in .dockerignore web_signin/Dockerfile web_signin/run.py; do
    [[ -f "$scratch/source/$required" ]] || fail "源码缺少 $required。请检查下载的分支。"
done
docker build --build-arg "PIP_INDEX_URL=$pip_index" -t "$image" \
    -f "$scratch/source/web_signin/Dockerfile" "$scratch/source" || fail '新镜像构建失败，旧容器仍在运行。'
[[ "$(inspect '{{.Id}}' "$container")" == "$old_id" ]] || fail '构建期间 bjmf-web 已被替换，本次未切换容器。'
if "$compose_mode"; then
    compose_hash_matches && compose_volume_matches || fail '构建期间 Compose 配置发生变化，本次未切换容器。'
    [[ "$(compose_containers)" == "$old_id" && "$(image_id "$compose_image")" == "$old_image_id" ]] \
        || fail '构建期间 Compose 服务或镜像标签发生变化，本次未切换容器。'
    new_image_id=$(image_id "$image")
    docker tag "$old_image_id" "$backup_image" || fail '无法保留原 Compose 镜像，本次未切换容器。'
fi

mkdir -p -- "$backup_dir"
log "停止原容器并备份签到数据库到 $backup_dir。"
switched=true
docker stop "$old_id" >/dev/null
if [[ -n "$db_mount" ]]; then
    docker run --rm --network none --mount "$db_mount,readonly" --entrypoint sh "$image" -ceu '
        cd /app/web_signin/db
        test -s web_signin.db
        set -- web_signin.db
        for name in web_signin.db-wal web_signin.db-shm web_signin.db-journal; do
            if [ -e "$name" ] || [ -L "$name" ]; then set -- "$@" "$name"; fi
        done
        for name; do test -f "$name" && test ! -L "$name" || exit 1; done
        tar -cf - -- "$@"
    ' > "$backup_dir/database.tar" || fail '读取原数据库挂载失败。请用 docker inspect 检查实际数据目录。'
    replacement_mount=$db_mount
else
    mkdir "$backup_dir/db"
    docker cp "$old_id:$db_path/." "$backup_dir/db/" || fail '从原容器复制数据库失败。'
    [[ -s "$backup_dir/db/web_signin.db" && ! -L "$backup_dir/db/web_signin.db" ]] || fail '未找到原 web_signin.db，已停止更新。'
    replacement_dir="$root/bjmf-web-data-$version"
    mkdir "$replacement_dir"
    cp -a -- "$backup_dir/db/." "$replacement_dir/"
    replacement_mount="type=bind,source=$replacement_dir,target=$db_path"
fi

if "$compose_mode"; then
    compose_hash_matches && compose_volume_matches || fail 'Compose 配置发生变化，正在恢复原服务。'
    compose_tagged=true
    docker tag "$new_image_id" "$compose_image"
    compose_create
    candidate_id=$(verify_compose_container "$new_image_id") \
        || fail '新版 Compose 容器名称、端口或数据库挂载与原服务不符，已阻止启动。'
    candidate_attempted=true
    docker start "$candidate_id" >/dev/null
else
    docker rename "$old_id" "$backup_name"
    candidate_attempted=true
    docker run -d --name "$container" --restart unless-stopped --env-file "$env_file" \
        --env "WEB_SIGNIN_PORT=$container_port" --publish "$publish" --mount "$replacement_mount" \
        --label "bjmf.update.run=$version" "$image" >/dev/null
fi

for (( attempt=0; attempt<45; attempt++ )); do
    code=$(curl --noproxy '*' --silent --max-time 2 --output /dev/null --write-out '%{http_code}' \
        "http://$health_host:$host_port/health") || code=000
    if [[ "$code" == 200 ]]; then
        switched=false
        if "$compose_mode"; then
            log "更新完成。网站端口 $host_port，服务仍由原 Compose 项目 $compose_project 管理。旧镜像保留为 $backup_image。"
        else
            log "更新完成。网站端口 $host_port，旧容器保留为 $backup_name。"
        fi
        log "数据库备份 $backup_dir"
        exit 0
    fi
    sleep 2
done
fail '新版健康检查没有返回 HTTP 200。'
