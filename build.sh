#!/usr/bin/env bash
#
# 本地构建 Docker 镜像。
#
# 默认打 linux/amd64，避免本机是 ARM64(Apple Silicon) 时打出 ARM 版镜像，
# 导致在 x86_64 云服务器上启动报 "exec format error"。
# 若目标机器是 ARM64，用 -p linux/arm64。
#
# 用法:
#   ./build.sh                      # 构建 bing-img-proxy:latest (linux/amd64)
#   ./build.sh -t v1.0.0            # 指定 tag
#   ./build.sh -p linux/arm64       # 指定平台
#   ./build.sh -n myrepo/bing-img   # 指定镜像名
#   ./build.sh --no-cache           # 不使用缓存
#   ./build.sh --push               # 构建后推送到远端仓库
#
set -euo pipefail

cd "$(dirname "$0")"

IMAGE_NAME="bing-img-proxy"
TAG="latest"
PLATFORM="linux/amd64"
NO_CACHE=""
# 默认直接构建到本地 docker images；加 --push 则额外推送到远端仓库
OUTPUT=""

usage() {
  sed -n '2,/^set /p' "$0" | sed -e 's/^# \?//' -e 's/^#//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--name)   IMAGE_NAME="$2"; shift 2 ;;
    -t|--tag)    TAG="$2"; shift 2 ;;
    -p|--platform) PLATFORM="$2"; shift 2 ;;
    --no-cache)  NO_CACHE="--no-cache"; shift ;;
    --push)      OUTPUT="--push"; shift ;;
    -h|--help)   usage 0 ;;
    *) echo "未知参数: $1" >&2; usage 1 ;;
  esac
done

FULL_IMAGE="${IMAGE_NAME}:${TAG}"

echo "==> 构建镜像: ${FULL_IMAGE} (platform=${PLATFORM})"

DOCKER_BUILDKIT=1 docker build \
  --platform "${PLATFORM}" \
  --tag "${FULL_IMAGE}" \
  ${NO_CACHE} \
  ${OUTPUT} \
  .

echo "==> 完成: ${FULL_IMAGE}"
echo "==> 查看: docker images ${IMAGE_NAME}"
echo "==> 运行: docker run --rm -p 18088:18088 -v \$(pwd)/config:/app/config:ro ${FULL_IMAGE}"
