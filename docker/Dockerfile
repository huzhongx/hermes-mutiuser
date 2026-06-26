FROM qx-images.tencentcloudcr.com/qunxing/python:3.10-slim
RUN apt-get update && apt-get install -y --no-install-recommends nginx git ca-certificates curl patch procps supervisor vim iputils-ping telnet sqlite3 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY hermes-agent /root/.hermes/hermes-agent
COPY patches /tmp/patches
# 执行有问题 先忽略
# RUN cd /root/.hermes/hermes-agent && for p in /tmp/patches/*.patch; do git apply "$p" || patch -p1 < "$p"; done


WORKDIR /opt/hermes-platform
COPY . /opt/hermes-platform
COPY k8s/nginx-pod.conf /etc/nginx/conf.d/default.conf
EXPOSE 80 443

COPY k8s/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]