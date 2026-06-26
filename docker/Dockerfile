# 基础镜像必须与 venv 的 python 版本一致（venv 是 3.11.15）
FROM qx-images.tencentcloudcr.com/qunxing/python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends nginx git ca-certificates curl patch procps supervisor vim iputils-ping telnet sqlite3 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# agent：service.sh 已把现网「已打补丁 + 已建 venv」的 agent 同步到 ./hermes-agent，
# 直接 COPY 即可（venv 含 591 个依赖包，broker spawn 用的 venv/bin/python 由此提供）。
# 不在镜像内重打补丁 —— 补丁已在构建上下文的 agent 源码里应用过。
COPY hermes-agent /root/.hermes/hermes-agent
# patches 仅随镜像留档（provenance），便于溯源 agent 版本与改动
COPY patches /tmp/patches

# venv 的解释器：uv 管理的 python 3.11.15（venv/bin/python 是指向它的 symlink）。
# 必须放到与现网相同的绝对路径，否则 venv 的 symlink 和 pyvenv.cfg 都会断。
COPY uv-python/cpython-3.11.15-linux-x86_64-gnu /root/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu
# pyvenv.cfg 的 home = .../cpython-3.11-linux-x86_64-gnu/bin，需要这个指向 3.11.15 的 symlink
RUN ln -s /root/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu \
          /root/.local/share/uv/python/cpython-3.11-linux-x86_64-gnu


WORKDIR /opt/hermes-platform
COPY . /opt/hermes-platform
COPY k8s/nginx-pod.conf /etc/nginx/conf.d/default.conf
EXPOSE 80 443

COPY k8s/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]