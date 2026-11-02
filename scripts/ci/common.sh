#!/usr/bin/env bash
# CI公共参数检查。只读环境变量，不加载任何本机或部署私有配置。

ci_validate_revision() {
    local tag=${1:-} revision=${2:-}
    if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]] || \
       [[ ! "$tag" =~ ^${revision}-[1-9][0-9]*$ ]]; then
        echo 'CI需要完整提交SHA及对应的SHA-构建号镜像标签。' >&2
        return 2
    fi
}

ci_proxy_args() {
    # 不打印代理值；代理只供构建网络使用，部署秘密不会进入构建参数。
    CI_PROXY_ARGS=()
    local proxy=${PROCURE_BUILD_PROXY:-${HTTPS_PROXY:-${HTTP_PROXY:-}}}
    if [[ -n "$proxy" ]]; then
        CI_PROXY_ARGS+=(--build-arg "HTTP_PROXY=$proxy" --build-arg "HTTPS_PROXY=$proxy")
    fi
}
