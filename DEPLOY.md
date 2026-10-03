# QQ 助理部署

Max 当大脑，AstrBot 当 QQ 通道，OmniRoute（可选）当模型中转。Max 用**上游原版**
——桥只依赖上游已有的 OneBot 反向 WS，不需要任何改动。

```
QQ ──► AstrBot (qq_official)
       ├─► OneBot 反向 WS   ws://bot:8080/onebot       ──► Max
       └─► LLM 代理         http://astrbot:6199/v1     ──► provider ──► 模型
                            http://astrbot:6198/media/… ◄── Max 取入站图片
```

## 30 秒版本

```bash
git clone https://github.com/S-9527/astrbot-max-bridge
cd astrbot-max-bridge/deploy

cp .env.example .env            # 填 6 个随机密钥，每个都是 openssl rand -hex 32

# Max 的版本钉在 deploy/max.pin 第一行，不要用 main
deploy/deploy.sh                # 拉仓库、检出钉住的 Max、起 compose、探活

open http://127.0.0.1:6185      # QQ 凭据、模型 provider 都在这里配
```

第一次部署会构建 Max，约 4-10 分钟（取决于网速）。`max-nix` 卷留着，之后改 Max
代码是增量重建，约 3 分钟。

先看它打算做什么而不真做：

```bash
deploy/deploy.sh --dry-run
```

## 前置条件

| 需要 | 说明 |
| --- | --- |
| Docker + Compose v2 | `docker compose version` 能出版本号 |
| 能出网的机器 | 拉镜像、拉 nix 依赖 |
| 4 核 8G 起 | OmniRoute 吃内存；不用它可省 4G |
| QQ 机器人凭据 | 开放平台创建，见下 |

**为什么钉 commit**：桥按 OneBot 11 的事件形状构造消息。上游若改了必填字段，
桥要跟着改；钉住版本能让「升级后图片/引用全失效」这类问题变成一次明确的
diff，而不是玄学。钉在 `deploy/max.pin`，部署脚本读它，CI 还会验证这个 sha 在
上游 `main` 上真的存在——钉一个被删掉的 sha，失败点会出现在 nix 构建中间，很难
看出是版本问题。

**端口**：只有 `6185`（AstrBot WebUI）和 `20128`（OmniRoute 面板）映射到宿主，
且都绑 `127.0.0.1`。远程访问走隧道：

```bash
ssh -N -L 6185:127.0.0.1:6185 user@server
```

不要改成 `0.0.0.0`：那两个是管理面。`6198`/`6199` 是容器内互联用的，**不映射
到宿主**——Max 按服务名直接访问。

## 第一次配置（在 WebUI 里做，密钥不经手文件）

1. **QQ 机器人**：开放平台创建机器人拿 AppID/AppSecret。AstrBot WebUI →
   「消息平台」→ 添加 `QQ 官方机器人（WebSocket）` → 填凭据 → 保存。
   凭据存在 `deploy/state/astrbot/cmd_config.json`。

2. **加进群**（可选）：手机 QQ → 联系人 → 机器人页签 → 添加到群聊。
   **只能加到自己为群主的群**，且个人认证开发者可能开不了群场景（平台对企业
   认证灰度）。单聊不受限。

3. **模型**：AstrBot WebUI →「服务提供商」→ 新增 → `OpenAI 兼容`。
   - 不用 OmniRoute：填供应商的 base_url + key。
   - 用 OmniRoute：base_url 填 `http://omniroute:20128/v1`，key 填面板
     `Settings → API Keys` 生成的。**端口是 20128，不是 20129**（见坑位 4）。

   改完 provider，桥的代理立刻跟着变，Max 和桥都不用重启。

## 三段链路各自的职责

**Max（bot）** 只做大脑：记忆、人格、skills、群 @ 判定、回复决策。它**不连 QQ
网关**，所以配置里没有任何 QQ 凭据。

**AstrBot** 持有 QQ 连接，并暴露两个只给容器内用的端口：

- `6199` —— LLM 代理。Max 把模型请求发到这里，桥用 AstrBot 当前 provider 的
  凭据转发。好处是密钥只有一份，换 provider 不用动 Max。
- `6198` —— 媒体服务。QQ 适配器把入站图片下载到自己的 `data/temp/`，桥按
  `http://astrbot:6198/media/<文件名>` 发布，Max 从这里取。

**桥**（本仓库）是 AstrBot 插件，把消息转成 OneBot 11 事件喂给 Max，再把 Max
的 action 翻回 AstrBot 的发送。它 `stop_event()` 掉 AstrBot 自己的模型——
同一句话只会有一个大脑回答。

## 服务器 vs 本机（沙箱）

| 项 | 沙箱 | 正式环境 |
| --- | --- | --- |
| QQ 域名 | `sandbox.api.bot.qq.com` | `api.bot.qq.com` |
| IP 白名单 | 不生效 | **必须**：开放平台填服务器公网 IP |
| 公网 IP | 不要求 | **固定 IP**。家用宽带 IP 会变，会频繁掉线 |

正式环境务必用固定 IP 的云主机。

## 已知坑位

都是实际踩出来的，不是通则。

**1. AstrBot 的 API Base URL 不能有前导空格。**
它把字段拼成 `/{base_url}`，一个空格变成请求路径 `/%20http://…`。症状是
「可用模型 0」，日志里能看到 `Request('GET', '/%20http://…')`。

**2. Max 的 profile 必须叫 `default`。**
`llm.default` 只是指定默认；运行时某些路径按字面量 `default` 查。改名会让每次
派发报 `unknown llm profile: default`。

**3. Max 不接受内联图片数据。**
`Max.IR.mediaRemoteRef` 只收 `http(s)://` 和 `mxc://`，注释写明 inline / data /
file「必须在成为 canonical 之前导入 BlobStore」。桥必须给真实 URL；内联 base64
会被静默丢弃（`fetch_jobs` 表会是空的）。

**4. OmniRoute 的 API 用 20128，不是 20129。** 20129 那个 bridge 返回 503。

**5. OmniRoute 的 API key 是 fail-closed 的。**
面板新建的 key，`allowedConnections` 默认空数组 = **一个连接都不放行**。症状是
`403 … connection(s) exist but are excluded by this API key's connection
allowlist`。面板里的「测试通过」只证明 ping 通，不证明有额度——额度耗尽的模型
会返回 402/429。

**6. 跨会话引用图片时，平台不给引用 id。**
QQ 的引用只带消息 id，跨会话时那个 id 是空的。AstrBot 适配器把被引用内容放在
`Reply.chain` 里，桥直接把它随当前消息发出，不依赖引用关系。

**7. 模型目录的元数据不可信。**
`oc/space-bunny-free` 在 OmniRoute 目录里 `input_modalities` 为空，实际发图
测试能正确识别。判断模型能力要实测，别看目录。

**8. `max_tokens` 越大首字越慢。**
带思考的模型尤其明显：同一请求 `max_tokens=2000` 是 1.9s，`111732` 是 7.1s。
`deploy/config/max.yaml` 里设 8192。

## 自动部署（CI）

推到 `main` 就部署。改 README 不会触发——只有 `*.py`、`metadata.yaml`、
`deploy/**` 和部署工作流本身变了才部署，因为桥的代码变了就必须重启 astrbot，
那是几秒钟的 QQ 断线，不该为一句文档改动付这个代价。

### 两个工作流

| 工作流 | 什么时候跑 | 做什么 |
| --- | --- | --- |
| `ci` | 每次 push / PR | 桥的 43 项单测（py 3.11/3.12/3.13）、探针自测、compose 能不能解析、脚本语法与 shellcheck、钉的 Max 版本在上游还在不在、**仓库里有没有密钥** |
| `deploy` | 推 main（限上面那些路径）或手动 | 先跑一遍 ci 的关键项，再 ssh 到服务器执行 `deploy/deploy.sh` |

### 服务器上一次性要做的

```bash
# 1. 给 CI 一把只能部署、不能乱来的钥匙
sudo useradd -m -s /bin/bash deployer || true
sudo -u deployer ssh-keygen -t ed25519 -N '' -C 'github-actions deploy'
cat ~deployer/.ssh/id_ed25519.pub        # 贴到 GitHub 的 DEPLOY_SSH_KEY

# 2. deployer 要能操作 docker
sudo usermod -aG docker deployer

# 3. 目标机的仓库和密钥
sudo -u deployer git clone https://github.com/S-9527/astrbot-max-bridge ~/max-qq
cd ~/max-qq/deploy && cp .env.example .env
# 填 6 个随机密钥：openssl rand -hex 32，一个一行
```

### GitHub 上要配的

**Secrets**（Settings → Secrets → Actions）：

| 名字 | 内容 |
| --- | --- |
| `DEPLOY_HOST` | 服务器地址 |
| `DEPLOY_USER` | `deployer` |
| `DEPLOY_PORT` | 可选，默认 22 |
| `DEPLOY_SSH_KEY` | 上一步那个私钥（**私钥**，不是 .pub） |
| `DEPLOY_KNOWN_HOSTS` | 见下 |

`DEPLOY_KNOWN_HOSTS` 用 `ssh-keyscan -p 22 <host>` 生成，但要**从可信途径核对
指纹**（云厂商控制台、或第一次手动登录时记下的）。写进仓库的必须是这份核对过的
结果：部署时现扫 `ssh-keyscan` 等于把「有没有人冒充这台机器」交给 DNS。

**Variables**：

| 名字 | 值 | 作用 |
| --- | --- | --- |
| `DEPLOY_ENABLED` | `true` | 不设就**跳过**部署而不是报错。第一次把仓库放上去时，服务器上的 `.env` 和 QQ 凭据还没建好，不该被一次 push 逼着部署 |
| `DEPLOY_DIR` | `max-qq` | 相对 `deployer` 的家目录 |

### 手动触发

Actions → deploy → Run workflow：

| 输入 | 作用 |
| --- | --- |
| `ref` | 部署别的分支或 tag，不动就填 `main` |
| `dry_run` | **先看它要做什么而不真做**，第一次部署强烈建议先勾这个 |
| `pull_images` | 顺带拉新基础镜像。默认不拉：部署要只跟着 commit 走，不跟着 `latest` 走 |
| `force` | 丢掉目标机上的本地改动（默认拒绝，见下） |
| `wait` | 等容器健康的最长秒数，默认 900 |

### 部署脚本做什么

`deploy/deploy.sh` 在目标机上跑，顺序是：前置检查 → `git fetch` + `reset --hard`
到目标 ref → 把 `max-src` 对齐到 `deploy/max.pin` → `docker compose up -d --wait`
→ 桥的代码变了才 `restart astrbot` → 探针 → 打印结果。日志会贴进 run 摘要。

它刻意不做几件事：

- **不生成 `.env`**。密钥一旦被换掉，OmniRoute 磁盘上加密的旧数据就解不开了，
  这种事不能让它顺手做掉。缺 `.env` 就停下来报错。
- **不碰 `state/` 和 `.env`**。两者都在 `.gitignore` 里，`reset --hard` 只动已
  跟踪的文件，所以 QQ 凭据和 id 映射库每次部署都原样保留。
- **CI 从头到尾看不到任何密钥**。GitHub 上只需要一把 SSH 私钥，模型 key 始终
  只存在于服务器上。
- **不删未跟踪的文件**。只有在目标机上改了**已跟踪**文件时才停下来问（加
  `--force` 才继续）——否则某次部署会静默丢掉你手改的 compose。

### 探针

部署完会跑 `deploy/probe.py`，问桥在容器内的两个端口还活着吗。这两个端口只有
在插件导入成功、后台任务起来之后才存在：插件挂了的时候 AstrBot 照常启动、日志
里只有一条 traceback、QQ 那边完全静默，从外面看「服务是好的」。探针发的请求走一
条没有路由匹配的路径，由 aiohttp 就地 404，不外发、不花额度。

它只证明插件活着，不证明 QQ 消息进得来。要确认整条链路，给机器人发一条消息，
然后查 `message_deliveries`（见下面「运维」）。

### 回滚

```bash
cd ~/max-qq
git reset --hard <旧的 commit>       # 记得回到 main 上，否则下次 CI 拉不动
(cd deploy && docker compose up -d --wait)
```

## 运维

```bash
cd deploy
docker compose ps
docker compose logs -f bot            # Max：记忆、派发、投递
docker compose logs -f astrbot        # AstrBot + 桥
```

**判断一次投递到底发生了什么**（比翻日志快）：

```sql
-- 回复有没有发出去
select delivery_id, status, attempt_count, last_error
  from message_deliveries order by delivery_id desc limit 5;
-- 图片有没有被抓下来（空 = 图片链路没通）
select canonical_message_id, seg_index, left(sha256,16) from message_images;
-- 引用关系有没有建立
select canonical_message_id, reply_to_canonical_message_id
  from messages where reply_to_canonical_message_id is not null
 order by canonical_message_id desc limit 5;
```

```bash
docker compose exec db psql -U max -d max -c "<上面任一条>"
```

**注意**：Max 日志量大时会自己丢弃（`compact: logger thread terminated`）。那时
「没有日志」不代表「没收到」，以上面几条 SQL 为准。

## 备份

有状态的只有两处：

- `deploy/state/astrbot/` —— QQ 凭据、provider 配置、会话、**id 映射库**。
  丢了 id 映射，同一个群在 Max 眼里会变成新会话（历史、成员、去重键全断）。
- 卷 `max-qq_omniroute-data` —— 面板配置、API key、连接授权。

Max 的记忆在 Postgres 卷 `max-qq_pgdata` 里。

```bash
cd deploy
docker compose stop db astrbot
docker run --rm -v max-qq_pgdata:/d -v "$PWD":/backup alpine \
  tar czf /backup/pgdata.tar.gz -C /d .
docker compose start db astrbot
```

## 停掉 / 清理

```bash
docker compose stop      # 停，保留数据
docker compose down      # 删容器，保留卷
docker compose down -v   # 连数据一起删（丢记忆和凭据）
```

## 开发

本地跑的东西和 CI 完全一样：

```bash
python3 test_bridge.py              # 43 项桥单测，不需要 AstrBot 在跑
python3 deploy/test_probe.py        # 12 项探针自测：每个分支都用真的端口试一遍
.github/check-secrets.sh            # 仓库里有没有密钥
bash -n deploy/deploy.sh && shellcheck deploy/deploy.sh
actionlint                          # 工作流语法
deploy/deploy.sh --dry-run          # 部署脚本会做什么（只读）
```

桥单测覆盖 id 映射的跨重启稳定性、整数约束、私聊事件不带 `group_id`、同秒内
消息顺序、引用往返，以及每种 OneBot action 的应答形状。探针自测覆盖：404 算活着、
只连上不回话不算活着、非 404 要提醒、目录缺失要报错。

改桥的代码就在本仓库根目录（`main.py` 等），`deploy/compose.yaml` 把仓库根挂进
`/AstrBot/data/plugins/max_bridge`，所以重启容器即生效：

```bash
cd deploy && docker compose restart astrbot
```

推到 `main` 之后，CI 会自动部署到服务器（见上面「自动部署」）。
