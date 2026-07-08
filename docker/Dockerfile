# ──────────────────────────────────────────────────────────────────────────
# Hermes Platform 镜像 —— 分层设计（稳定层在下，易变层在上）
#
# 分层顺序（改下层会作废上层缓存，所以把"最不爱变的"放最下）：
#   1. apt 系统依赖           —— 极少变
#   2. broker Python 依赖     —— requirements.txt 偶尔变
#   3. agent（含 venv + 补丁） —— 最重 1.7G，但几乎不变
#   4. uv python（venv 解释器）—— 104M，几乎不变
#   5. broker 代码 + 前端      —— 经常变（最上层，改它只重建几 MB）
#   6. nginx conf + entrypoint —— 偶尔变
#
# 这样改 broker 代码时，1-4 层全走缓存，秒级重建。
# ──────────────────────────────────────────────────────────────────────────

# 基础镜像必须与 venv 的 python 版本一致（venv 是 3.11.15）
FROM qx-images.tencentcloudcr.com/qunxing/python:3.11-slim

# ── 层1: 系统依赖 ──
RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx git ca-certificates curl patch \
        procps supervisor vim iputils-ping telnet sqlite3 \
    # Disable nginx's default site (conflicts with our pod config in conf.d)
    && rm -f /etc/nginx/sites-enabled/default \
    && rm -rf /var/lib/apt/lists/*

# ── 层2: broker Python 依赖 ──
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ── 层3: agent（现网已打补丁 + 已建 venv，service.sh 同步到 ./hermes-agent）──
# venv 含 591 个依赖包，broker spawn 用的 venv/bin/python 由此提供。
# 不在镜像内重打补丁 —— 补丁已在构建上下文的 agent 源码里应用过。
COPY hermes-agent /root/.hermes/hermes-agent

# ── 层3.5: broker spawn 必需的全局只读资源（不含密钥）──
# 这些是 broker _spawn 给用户 hermes_home 软链用的源文件。
# 密钥(auth.json/.env/config.yaml) 不在此 COPY——生产用 k8s Secret 挂载覆盖，
# 本地测试场景可临时 COPY 进 build context 验证完整链路。
# 详见 docs/k8s-base-image-plan.md §2.1 与评审文档支柱 2。
COPY hermes/skills /root/.hermes/skills
COPY hermes/cache /root/.hermes/cache
COPY hermes/pairing /root/.hermes/pairing
COPY hermes/models_dev_cache.json /root/.hermes/
COPY hermes/ollama_cloud_models_cache.json /root/.hermes/

# ── 层4: uv python（venv 的解释器）──
# venv/bin/python 是 symlink → /root/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu/bin/python3.11
# 必须放到与现网相同的绝对路径，否则 venv 的 symlink 和 pyvenv.cfg 都会断。
COPY uv-python/cpython-3.11.15-linux-x86_64-gnu /root/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu
# pyvenv.cfg 的 home = .../cpython-3.11-linux-x86_64-gnu/bin，需要这个指向 3.11.15 的 symlink
RUN ln -s /root/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu \
          /root/.local/share/uv/python/cpython-3.11-linux-x86_64-gnu

# ── 层5: broker 代码 + 前端（易变，最上层）──
# 注意：只显式拷 broker 运行所需文件，不用 COPY .（build context 含 1.7G 的
# hermes-agent / 104M 的 uv-python，COPY . 会把它们虚胖地拷进 /opt/hermes-platform
# 再靠后续层删除 —— 即使删了，那层仍固化 1.85GB。显式 COPY 避免这个问题。）
WORKDIR /opt/hermes-platform
COPY hermes_broker.py process_manager.py ws_pool.py api_server.py api_server_standalone.py /opt/hermes-platform/
COPY chat.html favicon.ico /opt/hermes-platform/
COPY patches /opt/hermes-platform/patches
COPY scripts /opt/hermes-platform/scripts

# ── 层6: nginx conf + entrypoint ──
# $connection_upgrade map（WS 升级握手）：必须放在 conf.d（nginx 主配置会 include），
# 在 http 上下文里 —— conf.d/*.conf 由 nginx.conf 的 http 块 include，所以放在这有效。
RUN printf '%s\n' \
        'map $http_upgrade $connection_upgrade {' \
        '    default upgrade;' \
        '    ""      close;' \
        '}' \
        > /etc/nginx/conf.d/00-map-upgrade.conf
COPY k8s/nginx-pod.conf /etc/nginx/conf.d/default.conf
COPY k8s/entrypoint.sh /entrypoint.sh

ENV PYTHONUNBUFFERED=1
EXPOSE 80 443
ENTRYPOINT ["/entrypoint.sh"]
