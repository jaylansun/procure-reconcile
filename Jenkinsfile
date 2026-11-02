pipeline {
    // 运行在既有可信Docker节点；采购使用独立工作区、任务和部署凭据。
    agent any

    options {
        skipDefaultCheckout(true)
        timestamps()
        disableConcurrentBuilds()
        timeout(time: 75, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '15'))
    }

    stages {
        stage('检出main的确定提交') {
            steps {
                script {
                    def source = checkout scm
                    def branch = source.GIT_BRANCH ?: env.GIT_BRANCH
                    if (!(branch in ['origin/main', 'main'])) {
                        error('发布任务仅接受可信main分支')
                    }
                    if (!(source.GIT_COMMIT ==~ /[0-9a-f]{40}/)) {
                        error('SCM没有返回有效的完整提交SHA')
                    }
                    env.COMMIT_SHA = source.GIT_COMMIT
                    env.IMAGE_TAG = "${source.GIT_COMMIT}-${env.BUILD_NUMBER}"
                    echo "发布提交：${env.COMMIT_SHA}"
                }
            }
        }

        stage('隔离CI检查') {
            steps {
                sh 'bash scripts/ci/check.sh "$IMAGE_TAG" "$COMMIT_SHA" "$BUILD_NUMBER"'
            }
        }

        stage('构建SHA镜像') {
            steps {
                sh 'bash scripts/ci/build-images.sh "$IMAGE_TAG" "$COMMIT_SHA"'
            }
        }

        stage('受控发布到OrbStack') {
            steps {
                // 正式秘密只在所有检查和构建通过后提供，不进入镜像构建。
                withCredentials([
                    file(credentialsId: 'procure-deploy-env', variable: 'PROCURE_DEPLOY_ENV')
                ]) {
                    sh '''
                        set +x
                        bash scripts/deploy/launch.sh "$IMAGE_TAG" "$COMMIT_SHA" "$BUILD_NUMBER" < "$PROCURE_DEPLOY_ENV"
                    '''
                }
            }
        }
    }

    post {
        always {
            // CI被中止后仍只清理本构建资源，不修改正式数据或DevPilot。
            script {
                if (env.IMAGE_TAG && fileExists('deploy/compose.ci.yml')) {
                    sh '''
                        set +x
                        export PROCURE_CI_TAG="$IMAGE_TAG"
                        export PROCURE_CI_ADMIN_PASSWORD=cleanup-only
                        export PROCURE_CI_APP_PASSWORD=cleanup-only
                        docker compose --env-file /dev/null --project-name "procure-ci-$BUILD_NUMBER" --file deploy/compose.ci.yml down --remove-orphans --timeout 10 >/dev/null 2>&1 || true
                        docker image rm "procure-api-test:$IMAGE_TAG" "procure-client-ci:$IMAGE_TAG" >/dev/null 2>&1 || true
                    '''
                }
            }
            archiveArtifacts(
                artifacts: '.local/ci-reports/**,.local/release-reports/**',
                allowEmptyArchive: true,
                fingerprint: true
            )
        }
    }
}
