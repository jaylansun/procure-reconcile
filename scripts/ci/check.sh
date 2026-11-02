#!/usr/bin/env bash
# 容器化CI：检查失败时绝不接触正式服务或部署凭据。
set +x
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd -- "$script_dir/../.." && pwd)
cd -- "$repo_dir"
source "$script_dir/common.sh"

tag=${1:-} revision=${2:-} build=${3:-}
ci_validate_revision "$tag" "$revision"
if [[ ! "$build" =~ ^[1-9][0-9]*$ ]] || [[ "$tag" != "$revision-$build" ]]; then
    echo 'CI构建号与镜像标签不一致。' >&2
    exit 2
fi
ci_proxy_args
reports="$repo_dir/.local/ci-reports"
mkdir -p -- "$reports"
rm -f -- "$reports/backend-junit.xml" "$reports/client-junit.xml"

echo '构建后端与客户端CI镜像。'
docker build --progress plain ${CI_PROXY_ARGS[@]+"${CI_PROXY_ARGS[@]}"} \
    --file server/Dockerfile --target test \
    --label "org.opencontainers.image.revision=$revision" \
    --tag "procure-api-test:$tag" .
docker build --progress plain ${CI_PROXY_ARGS[@]+"${CI_PROXY_ARGS[@]}"} \
    --file deploy/Dockerfile.ci \
    --label "org.opencontainers.image.revision=$revision" \
    --tag "procure-client-ci:$tag" .

# 密码来自本次测试镜像中的标准库，不依赖Jenkins节点安装Python/openssl。
credentials=$(docker run --rm --entrypoint python "procure-api-test:$tag" -c \
    'import secrets; print(secrets.token_hex(24) + ":" + secrets.token_hex(24))')
IFS=: read -r PROCURE_CI_ADMIN_PASSWORD PROCURE_CI_APP_PASSWORD <<< "$credentials"
unset credentials
if [[ ! "$PROCURE_CI_ADMIN_PASSWORD" =~ ^[0-9a-f]{48}$ ]] || \
   [[ ! "$PROCURE_CI_APP_PASSWORD" =~ ^[0-9a-f]{48}$ ]]; then
    echo '无法生成独立CI数据库凭据。' >&2
    exit 1
fi
export PROCURE_CI_ADMIN_PASSWORD PROCURE_CI_APP_PASSWORD
export PROCURE_CI_TAG="$tag"
compose=(docker compose --env-file /dev/null --project-name "procure-ci-$build" --file deploy/compose.ci.yml)

cleanup() {
    local status=$?
    trap - EXIT
    # 只清理本次CI项目；正式卷和其他项目不在此命令范围内。
    "${compose[@]}" down --remove-orphans --timeout 10 >/dev/null 2>&1 || \
        echo '本次CI资源未完整清理，请检查该构建的独立项目。' >&2
    unset PROCURE_CI_ADMIN_PASSWORD PROCURE_CI_APP_PASSWORD
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"${compose[@]}" config --quiet
"${compose[@]}" create db
db_id=$("${compose[@]}" ps --all --quiet db)
[[ -n "$db_id" ]] || { echo 'CI数据库容器未创建。' >&2; exit 1; }
# Docker API传输文件；不能把Jenkins内部工作区作为daemon宿主bind源。
docker cp deploy/postgres/01-app-role.sh \
    "$db_id:/docker-entrypoint-initdb.d/01-app-role.sh"
"${compose[@]}" up --detach --wait --wait-timeout 90 db

"${compose[@]}" create backend client
backend_id=$("${compose[@]}" ps --all --quiet backend)
client_id=$("${compose[@]}" ps --all --quiet client)
[[ -n "$backend_id" && -n "$client_id" ]] || \
    { echo 'CI测试容器未创建。' >&2; exit 1; }
docker cp scripts/ci "$backend_id:/tmp/procure-ci"

echo '运行后端完整测试与真实PostgreSQL集成测试。'
docker start "$backend_id" >/dev/null
backend_status=$(docker wait "$backend_id")
# 后端runner已遮盖输出及报告中的本次测试密码，只提取指定的安全报告。
docker logs "$backend_id"
docker cp "$backend_id:/tmp/procure-ci-reports/backend-junit.xml" \
    "$reports/backend-junit.xml" 2>/dev/null || true
[[ "$backend_status" == 0 ]] || { echo '后端CI失败，停止发布。' >&2; exit 1; }
[[ -f "$reports/backend-junit.xml" ]] || \
    { echo '后端CI报告缺失，停止发布。' >&2; exit 1; }

echo '运行客户端类型检查、构建与组件测试。'
docker start "$client_id" >/dev/null
client_status=$(docker wait "$client_id")
docker logs "$client_id"
docker cp "$client_id:/tmp/procure-ci-reports/client-junit.xml" \
    "$reports/client-junit.xml" 2>/dev/null || true
[[ "$client_status" == 0 ]] || { echo '客户端CI失败，停止发布。' >&2; exit 1; }
[[ -f "$reports/client-junit.xml" ]] || \
    { echo '客户端CI报告缺失，停止发布。' >&2; exit 1; }
echo '全部CI检查通过。'
