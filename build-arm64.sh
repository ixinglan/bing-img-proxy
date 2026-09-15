#!/usr/bin/env bash
#
# 本地构建 linux/arm64 架构的 Docker 镜像（用于 ARM64 云服务器，如阿里云/腾讯云 ARM 实例、飞腾等）。
#
# 注意：若本机是 x86_64(Intel/AMD) 或 Apple Silicon 但 Docker 未开启 Rosetta/QEMU，
#       跨架构构建需要一次性的 binfmt 支持，先执行（每台机器只需一次）：
#         docker run --privileged --rm tonistiigi/binfmt --install arm64
#       Apple Silicon(M1/M2/M3) 本机构建 linux/arm64 为原生构建，无需上述步骤。
#
# 用法:
#   ./build-arm64.sh                    # 构建 bing-img-proxy:latest-arm64 (linux/arm64)
#   ./build-arm64.sh -t v1.0.0          # 指定 tag
#   ./build-arm64.sh -n myrepo/bing-img # 指定镜像名
#   ./build-arm64.sh --no-cache         # 不使用缓存
#   ./build-arm64.sh --push             # 构建后推送到远端仓库
#
set -euo pipefail

cd "$(dirname "$0")"

IMAGE_NAME="bing-img-proxy"
TAG="latest-arm64"
PLATFORM="linux/arm64"
NO_CACHE=""
OUTPUT=""

usage() {
  sed -n '2,/^set /p' "$0" | sed -e 's/^# \?//' -e 's/^#//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--name)     IMAGE_NAME="$2"; shift 2 ;;
    -t|--tag)      TAG="$2"; shift 2 ;;
    --no-cache)    NO_CACHE="--no-cache"; shift ;;
    --push)        OUTPUT="--push"; shift ;;
    -h|--help)     usage 0 ;;
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
echo "==> 查看架构: docker image inspect ${FULL_IMAGE} --format '{{.Architecture}}'"
echo "==> 运行: docker run --rm -p 18088:18088 -v \$(pwd)/config:/app/config:ro ${FULL_IMAGE}"
