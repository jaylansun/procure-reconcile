#!/usr/bin/env bash
# 只构建确定提交的镜像，不停止或更新正在运行的服务。
set +x
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd -- "$script_dir/../.." && pwd)
cd -- "$repo_dir"
source "$script_dir/common.sh"
tag=${1:-} revision=${2:-}
ci_validate_revision "$tag" "$revision"
ci_proxy_args

echo '构建采购API、维护工具、数据库及HTTPS入口镜像。'
docker build --progress plain ${CI_PROXY_ARGS[@]+"${CI_PROXY_ARGS[@]}"} \
    --file server/Dockerfile --target runtime \
    --label "org.opencontainers.image.revision=$revision" \
    --tag "procure-api:$tag" .
docker build --progress plain ${CI_PROXY_ARGS[@]+"${CI_PROXY_ARGS[@]}"} \
    --file deploy/Dockerfile.ops --build-arg "RUNTIME_IMAGE=procure-api:$tag" \
    --label "org.opencontainers.image.revision=$revision" \
    --tag "procure-ops:$tag" .
docker build --progress plain ${CI_PROXY_ARGS[@]+"${CI_PROXY_ARGS[@]}"} \
    --file deploy/postgres/Dockerfile \
    --label "org.opencontainers.image.revision=$revision" \
    --tag "procure-db:$tag" .
docker build --progress plain ${CI_PROXY_ARGS[@]+"${CI_PROXY_ARGS[@]}"} \
    --file deploy/gateway/Dockerfile \
    --label "org.opencontainers.image.revision=$revision" \
    --tag "procure-gateway:$tag" .

# 仅记录镜像标识与源码SHA，不导出可能包含配置的完整inspect。
mkdir -p .local/ci-reports
{
    printf 'commit=%s\nimage_tag=%s\n' "$revision" "$tag"
    for image in api ops db gateway; do
        printf 'procure-%s=' "$image"
        docker image inspect --format '{{.Id}}' "procure-$image:$tag"
    done
} > .local/ci-reports/images.txt
