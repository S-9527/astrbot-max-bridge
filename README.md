# astrbot-max-bridge

把 QQ 和 [Max](https://github.com/HCHogan/max) 接起来：AstrBot 拿 QQ 连接，Max
当大脑。**Max 用上游原版**，桥不要求任何改动。

```
QQ ──► AstrBot (qq_official)
       ├─► OneBot 反向 WS   ws://bot:8080/onebot       ──► Max（记忆/人格/skills）
       └─► LLM 代理         http://astrbot:6199/v1     ──► provider ──► 模型
                            http://astrbot:6198/media/… ◄── Max 取入站图片
```

为什么要有这个东西：Max 自带 QQ 官方适配器，但它连不上群聊——个人认证开发者
拿不到群场景权限，而开放平台不会报错，只是静默不推群事件（文档里「权限」那节
写明「如果拥有的某个特殊事件类型的权限被取消……将不会收到对应的事件类型」）。
走 AstrBot 的连接路径就能拿到群消息，同时保留 Max 全部的大脑能力。

## 快速开始

见 [DEPLOY.md](DEPLOY.md)。三行：

```bash
cd deploy && cp .env.example .env      # 填 6 个随机密钥
git clone https://github.com/HCHogan/max max-src && git -C max-src checkout <pinned>
docker compose up -d
```

## 装成插件（不部署整套）

```bash
git clone https://github.com/S-9527/astrbot-max-bridge \
  <astrbot>/data/plugins/max_bridge
```

然后在 AstrBot WebUI →「服务提供商」里配好模型，并让 Max 指向
`http://<astrbot-host>:6199/v1`。桥的环境变量见 `deploy/compose.yaml` 里的
`MAX_BRIDGE_*`，每项都有注释。

## 它做什么

- **入站**：AstrBot 消息 → OneBot 11 事件 → Max 的反向 WS。openid 与整数的映射
  落 sqlite，跨重启稳定（否则同一个群重启后变成新会话）。
- **出站**：Max 的 action → AstrBot 的发送。文字、图片、视频、文件、表情都
  保真，不压成 `[图片]` 这种占位文本。
- **图片**：QQ 适配器把图片下载到自己容器的 `data/temp/`，桥用一个只读 HTTP
  服务发布出去，Max 从那里取。Max 不接受内联 base64（见 DEPLOY.md 坑位 3）。
- **引用**：平台跨会话引用时不给消息 id，桥用 `Reply.chain` 里已有的内容，
  把被引用的图随当前消息一起送出。
- **模型**：桥在 AstrBot 内开一个 OpenAI 兼容端点，用 AstrBot 当前 provider
  的凭据转发。密钥只有一份，改 provider 不用动 Max、不用重启。

## 它不做什么

- **不谎报成功**。做不到的动作明确失败，而不是回一个假的成功——Max 的投递
  记录会写下「做过了」，那比失败更难查。表情和戳一戳例外：它们是回复的装饰，
  拒绝它们会让整个投递被判失败，所以如实跳过并记录。
- **不接管 AstrBot 自己的模型**。转发的事件会被 `stop_event()`，同一句话只有
  一个大脑回答。`MAX_BRIDGE_ENABLED=0` 可以随时交还给 AstrBot。
- **不假装历史存在**。Max 拉历史时回空页——那是答案；拒绝会让 Max 判定投递
  需要重试，回复就永远发不出去。

## 目录

```
main.py            插件主体：转发、action 处理、能力边界
ids.py             openid ↔ 整数映射（sqlite，跨重启稳定）
onebot.py          OneBot 11 编解码，按 Max 的解析器形状写的
media_server.py    只读媒体服务，给 Max 抓入站图片
llm_proxy.py       OpenAI 兼容端点，转发到 AstrBot 当前 provider
test_bridge.py     43 项单测，不需要 AstrBot 在跑
deploy/            compose、Max 配置、密钥模板
DEPLOY.md          部署文档，含 8 个实际踩过的坑
```
