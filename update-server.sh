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
root=$(cd -- "$root" && pwd -P) || fail '项目目录不存在，请设置 BJMF_UPDATE_ROOT。'
[[ "$root" != / && "$root" != *,* ]] || fail '项目目录不能是根目录或含逗号。'
env_file=${BJMF_UPDATE_ENV_FILE:-$root/docker.env}
[[ -f "$env_file" && -r "$env_file" ]] || fail "找不到 $env_file。请先复制 docker.env.example 为 docker.env，填好管理员配置。"
exec 9>"$root/.bjmf-update.lock"
flock -n 9 || fail '该目录已有更新正在进行。'

read_env() {
    # Read the last unquoted KEY=value entry without executing the env file.
    local line found=''
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == "$1="* ]]; then
            found=${line#*=}
        fi
    done < "$env_file"
    printf '%s' "${found%$'\r'}"
}

container_port=$(read_env WEB_SIGNIN_PORT)
container_port=${container_port:-9988}
[[ "$container_port" =~ ^[1-9][0-9]{0,4}$ ]] && (( container_port <= 65535 )) || fail '请在 docker.env 中填写有效的 WEB_SIGNIN_PORT（通常为 9988）。'
pip_index=${BJMF_UPDATE_PIP_INDEX_URL:-$(read_env PIP_INDEX_URL)}
pip_index=${pip_index:-https://pypi.org/simple}

inspect() { docker container inspect --format "$1" "$2"; }

old_id=$(inspect '{{.Id}}' "$container" 2>/dev/null) || fail '没有找到 bjmf-web 容器。首次安装请按部署教程启动。'
[[ "$(inspect '{{.State.Running}}' "$old_id")" == true ]] || fail '原容器未运行，请先确认并启动 bjmf-web。'
compose_project=$(inspect '{{with .Config.Labels}}{{index . "com.docker.compose.project"}}{{end}}' "$old_id")
[[ -z "$compose_project" || "$compose_project" == '<no value>' ]] || fail '当前由 Docker Compose 管理，请使用部署教程的 Compose 更新命令。'
[[ "$(inspect '{{.HostConfig.AutoRemove}}|{{.HostConfig.Privileged}}' "$old_id")" == 'false|false' ]] || fail '自动删除或特权容器请按原 Docker 参数手动升级。'
network=$(inspect '{{.HostConfig.NetworkMode}}' "$old_id")
[[ "$network" == default || "$network" == bridge ]] || fail '自定义网络请按原 Docker 参数手动升级。'

bindings=$(inspect '{{range $port, $items := .HostConfig.PortBindings}}{{range $items}}{{printf "%s|%s|%s\n" $port .HostIp .HostPort}}{{end}}{{end}}' "$old_id")
[[ -n "$bindings" && "$bindings" != *$'\n'* ]] || fail '脚本要求旧容器只有一个端口映射。多个端口请按原 Docker 参数手动升级。'
IFS='|' read -r old_port host_ip host_port <<< "$bindings"
[[ "$old_port" == */tcp && "$host_port" =~ ^[1-9][0-9]{0,4}$ ]] || fail '旧容器没有可保留的固定 TCP 端口映射。'
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

mounts=$(inspect '{{range .Mounts}}{{printf "%s|%s|%s|%s|%t\n" .Destination .Type .Source .Name .RW}}{{end}}' "$old_id")
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

version="$(date -u +%Y%m%d-%H%M%S)-$$"
image="bjmf-web:online-$version"
backup_name="$container-backup-$version"
backup_dir="$root/bjmf-web-backups/$version"
scratch=$(mktemp -d /tmp/bjmf-update.XXXXXXXX)
switched=false
candidate_attempted=false

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

rollback() {
    local active_id label
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
        rollback || printf '自动恢复未完成。请保留旧容器 %s 和备份 %s，再检查 Docker 状态。\n' "$old_id" "$backup_dir" >&2
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

docker rename "$old_id" "$backup_name"
candidate_attempted=true
docker run -d --name "$container" --restart unless-stopped --env-file "$env_file" \
    --env "WEB_SIGNIN_PORT=$container_port" --publish "$publish" --mount "$replacement_mount" \
    --label "bjmf.update.run=$version" "$image" >/dev/null

for (( attempt=0; attempt<45; attempt++ )); do
    code=$(curl --noproxy '*' --silent --max-time 2 --output /dev/null --write-out '%{http_code}' \
        "http://$health_host:$host_port/health") || code=000
    if [[ "$code" == 200 ]]; then
        switched=false
        log "更新完成。网站端口 $host_port，旧容器保留为 $backup_name。"
        log "数据库备份 $backup_dir"
        exit 0
    fi
    sleep 2
done
fail '新版健康检查没有返回 HTTP 200。'
