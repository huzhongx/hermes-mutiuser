#!/usr/bin/env bash
set -e
# 加载 ConfigMap/Secret 注入的环境（k8s 已通过 envFrom 注入, 无需 source .env）
nginx -g 'daemon off;' &                # sidecar
exec python3 /opt/hermes-platform/hermes_broker.py