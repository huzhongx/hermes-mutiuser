#!/usr/bin/env bash
# 先不取版本号
# TAG=${1}

# 本地临时调试 先固定写死版本号
TAG=latest
SERVICE_NAME=pro-service-hermes-mutiuser
DOCKERFILE=docker/Dockerfile
IMAGE="qx-images.tencentcloudcr.com/qunxing/"${SERVICE_NAME}:${TAG}

git clone https://github.com/NousResearch/hermes-agent 2>/dev/null
cd hermes-agent && git checkout v2026.6.5 && git status && cd ..

docker build -f ${DOCKERFILE} --pull . -t ${IMAGE}
echo "${IMAGE}"

# 先不push
# docker push ${IMAGE}

# 本地临时测试
docker rm -f hermes-mutiuser 2>/dev/null
docker run -itd -p 8080:8080 -p 80:80 -p 443:443 --name hermes-mutiuser ${IMAGE}
# docker ps -a
# docker exec -it hermes-mutiuser bash

