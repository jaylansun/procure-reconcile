#!/bin/sh
# Jenkins只传Secret file的路径；内容通过stdin进入受控ops容器。
set +x
set -eu
umask 077

if [ "$#" -ne 3 ]; then
  printf '%s\n' '{"status":"failed","code":"invalid_release_arguments"}'
  exit 2
fi
image_tag=$1
commit_sha=$2
build_number=$3
case "$image_tag" in ''|*[!a-z0-9_.-]*) exit 2 ;; esac
case "$commit_sha" in ''|*[!0-9a-f]*) exit 2 ;; esac
case "$build_number" in ''|*[!0-9]*) exit 2 ;; esac
if [ "${#commit_sha}" -ne 40 ] || [ -z "${PROCURE_DEPLOY_ENV:-}" ] || [ ! -r "$PROCURE_DEPLOY_ENV" ]; then
  printf '%s\n' '{"status":"failed","code":"missing_release_configuration"}'
  exit 2
fi

report_directory=${PROCURE_RELEASE_REPORT_DIR:-.local/release-reports}
mkdir -p "$report_directory"
report="$report_directory/release-$build_number.jsonl"
errors="$report_directory/.launcher-$build_number.stderr"
set +e
docker run --rm -i \
  --name "procure-release-$build_number" \
  --user 10001:10001 --group-add 0 \
  --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock \
  --mount source=procure_files,target=/app/data \
  --mount source=procure_backups,target=/backups \
  --mount source=procure_release_state,target=/state \
  --mount source=procure_tls,target=/tls,readonly \
  --tmpfs /tmp \
  "procure-ops:$image_tag" "$image_tag" "$commit_sha" "$build_number" \
  < "$PROCURE_DEPLOY_ENV" > "$report" 2> "$errors"
status=$?
set -e
cat "$report"
rm -f "$errors"
if [ "$status" -ne 0 ]; then
  printf '%s\n' '{"status":"failed","code":"deployment_controller_failed"}'
fi
exit "$status"
