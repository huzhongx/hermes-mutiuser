# Nginx 网络层优化(TLS 会话复用 / 0-RTT / gzip / OCSP)

## 背景

前端(waw)访问 hermes 的 API 接口慢,实测:
- `GET /api/config` 前端耗时 **1816ms**
- `GET /api/skills/list` 前端耗时 **918ms**

排查后确认服务端处理极快(`/api/config` 直连 5ms,`/api/skills/list` 含首次迁移 139ms),
慢在**前端机到服务器的跨地域公网 RTT**。

## 根因(实测定位)

前端机 `123.57.50.51` ↔ hermes 机 `43.160.236.160` **ping RTT = 196ms**(跨地域)。

在 196ms RTT 下,一个 HTTPS 请求的构成(新连接):
```
TCP 握手:  1 RTT = 196ms
TLS 1.3 握手: 1 RTT = 196ms
请求+响应:  1 RTT = 196ms
合计:       3 RTT ≈ 600ms
```

用 `tc netem` 给本机回环加 98ms 单程延迟模拟,实测单个请求 **639ms**,与理论吻合。
前端 `/api/skills/list` 918ms ≈ 600ms(网络)+ 300ms(服务端首次迁移 reload + 传输)。

**关键瓶颈:每个新连接都要付 ~400ms 的 TCP+TLS 握手成本**(跨地域高 RTT 放大)。
原配置 `ssl_session_tickets off` 导致无法复用 TLS 会话,连接一旦断开就得重新 full handshake。

## 实施的优化

### 配置文件变更

**`/etc/nginx/sites-available/openclaw.conf`**(443 server SSL 段):

```nginx
# 原配置
ssl_session_tickets off;

# 改为
ssl_session_tickets on;
ssl_session_ticket_key /etc/nginx/ssl/ticket.key;   # 新生成 80 字节随机密钥
ssl_early_data on;                                    # TLS 1.3 0-RTT
ssl_stapling on;                                      # OCSP stapling
ssl_stapling_verify on;
resolver 223.5.5.5 8.8.8.8 valid=300s;                # OCSP 拉取用 DNS
resolver_timeout 5s;
```

**`/etc/nginx/nginx.conf`**(http 段 gzip):

```nginx
gzip_vary on;
gzip_proxied any;
gzip_comp_level 5;
gzip_min_length 256;
gzip_types text/plain text/css application/json application/javascript
           text/xml application/xml application/xml+rss text/javascript
           text/event-stream;   # 含 SSE 流式响应
```

**新生成**:`/etc/nginx/ssl/ticket.key`(80 字节随机,`openssl rand 80`)。

### 各项优化及收益

| 优化 | 原状 | 改后 | 收益(196ms RTT) |
|---|---|---|---|
| **① session tickets** | off(每次 full handshake) | on | 后续连接 TLS 握手 2 RTT → 1 RTT,**省 196ms** |
| **② TLS 1.3 0-RTT early data** | 无 | on | 已会话客户端请求随 ClientHello 发出,省整个握手往返 |
| **③ gzip JSON** | types 注释掉 | application/json 启用 | 13378B→5338B,**省 60% 传输** |
| **④ OCSP stapling** | 无 | on | 免客户端查证书吊销,省 1 RTT(reload 后 1-5min 后台填充) |
| HTTP/2 + ALPN | 已有 | 保持 | 浏览器连接复用 |

## 验证(netem 模拟 196ms RTT,本机回环加 98ms 延迟)

```bash
tc qdisc add dev lo root netem delay 98ms   # 模拟往返 196ms
# 首次请求 (full handshake)
curl ... -w "总=%{time_total}s"   # → 639ms
# 复用连接后续请求
curl ... --next ...               # → 428ms (省 211ms = 1 RTT)
tc qdisc del dev lo root          # 清除
```

- 首次请求:**639ms**(TCP+TLS+请求 = 3 RTT,物理极限无法再降)
- 复用连接后续请求:**428ms**(省 1 RTT)✅
- gzip:13378B → **5338B** ✅
- session 复用:`openssl s_client` 第二次连接显示 `Reused, TLSv1.3` ✅
- OCSP:reload 后后台拉取,证书链完整 + `ocsp.digicert.com` 可达(200/12ms),自动生效

## 前端实际收益预估

浏览器用 HTTP/2 连接复用后:
- **页面首次加载**:首个请求仍 ~600ms(握手不可避免)
- **后续 API 请求**:从 ~600ms 降到 **~400ms**(省握手)+ 响应体小 60%(传输更快)
- 配合 early data,老客户端(已建过会话)的首个请求可 0-RTT

## 物理极限说明

本机网络层能做的已做尽。剩余的 ~400ms/请求(2 RTT)是**跨地域 RTT 的物理下限**:
- 一个 HTTP 请求至少要 1 个完整往返(请求出去 + 响应回来)
- 跨地域 RTT 196ms 无法在服务端消除,只能靠:
  1. **部署就近**(hermes 迁到前端机同机房,RTT→<1ms)—— 根治
  2. **前端减少请求轮次**(串行→并行,或聚合接口)—— 前端侧
  3. **连接预热**(`<link rel="preconnect">`)—— 前端侧

## 回滚

备份在 `/tmp/openclaw.conf.bak.*` 和 `/tmp/nginx.conf.bak.*`。回滚:
```bash
cp /tmp/openclaw.conf.bak.<ts> /etc/nginx/sites-available/openclaw.conf
cp /tmp/nginx.conf.bak.<ts> /etc/nginx/nginx.conf
nginx -t && nginx -s reload
```

## 注意事项

- `ssl_session_ticket_key` 若多台 nginx 需**共享同一密钥**(否则票据跨机无效)。当前单机无影响。
- `ssl_early_data on` 允许 0-RTT 重放攻击面;前端 API 多为 GET 读 + token 认证,风险可接受。
  若有非幂等 POST 接口担心重放,可在 nginx 层对 early data 请求降级(检查 `$ssl_early_data`)。
