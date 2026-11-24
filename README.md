# TG-Forwarder

基于 **Telethon (UserBot)** 与 **aiohttp** 构建的 Telegram 频道/群组自动化无痕搬运工具，内置完整 Web 控制台，支持多账号管理、零下载原样复制、受限频道兜底重传、相册保持及富文本无损正则清洗。

## 核心特性

- **无来源转发（Drop Author）**：自动去除“转发自 xxx”来源头；普通频道文案未改动时走服务端零下载直接复制。
- **富文本无损正则处理**：
  - `skip_patterns`：命中广告/推广关键词整条跳过（在清空文案前优先判定）。
  - `strip_text_patterns` / `strip_all_text`：保留媒体、按条件或强制清空配文。
  - `strip_patterns` / `replace_patterns`：删行、删词、查找替换，并按 **UTF-16 实体偏移** 自动重算加粗、链接、代码等格式范围。
  - 文本修改后在普通频道仍复用服务端媒体句柄零下载重发。
- **完整相册（Album）支持**：多图/多视频消息整组过滤、清洗并保持相册合并发送。
- **受限频道兜底重传（`noforwards`）**：针对禁止转发的频道，可自动临时下载媒体并保持视频流式播放属性与封面重新上传，发送完毕即清理临时目录。
- **多账号 + Web 控制台**：
  - 支持扫码（QR）或手机号 + 验证码 + 2FA 在线登录多个 Telegram 账号，按规则绑定执行账号。
  - 内置频道目录选择器（自动过滤已迁移/停用的僵尸群组）。
  - 内置无代码**正则生成器**与实时预览测试工具。
  - 支持在 Web「全局设置」中热更新 Telegram API ID / API Hash 并自动重连账号。
  - SQLite 持久化去重、7 日数据统计与转发记录审计，支持配置热重载（Web 保存、`SIGHUP` 或文件变更）。

## 快速开始

### 1. 环境准备

需要 Python 3.11+（推荐 3.12 / 3.13）以及在 [my.telegram.org](https://my.telegram.org) 申请的 `API ID` 和 `API Hash`。

```bash
git clone https://github.com/jiuliking/tg-forwarder.git
cd tg-forwarder

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
chmod 600 .env
# 编辑 .env 填入你的 TELEGRAM_API_ID 和 TELEGRAM_API_HASH
```

可选：复制示例配置文件（也可以启动后直接在 Web 界面可视化配置）：

```bash
cp config.example.yaml config.yaml
chmod 600 config.yaml
```

### 2. 启动服务

```bash
.venv/bin/python forwarder.py
```

首次启动后，在服务目录内用交互式终端设置管理员密码：

```bash
.venv/bin/python common.py reset-web-password
```

Web 控制台默认监听 `http://127.0.0.1:28081`。登录后可在全局设置复制免密链接。Token 放在 URL 片段中，由浏览器通过登录请求提交。轮换 Token 会使所有现有 Web 会话失效。

### 3. 登录账号与配置规则

1. 打开 Web 控制台，进入 **Telegram 账号** 页面扫码或用手机号登录小号。
2. 进入 **转发规则** 新建规则，选择执行账号、源频道与目标频道。
3. 如需过滤广告，可在 **正则生成** 页面直接输入关键词生成规则并实时测试效果。

## 命令行辅助工具

- 仅通过终端扫码登录默认账号：
  ```bash
  .venv/bin/python login.py --qr
  ```
- 查看当前默认账号已加入的频道/群组 ID：
  ```bash
  .venv/bin/python list_chats.py
  ```
- 运行核心单元测试：
  ```bash
  .venv/bin/python test_common.py
  .venv/bin/python -m unittest test_forwarder
  ```

## 生产部署（systemd + Nginx）

项目在 `deploy/` 下提供了参考配置：

- `deploy/tg-forwarder.service.example`：systemd 守护进程单元文件（支持 `systemctl reload` 发送 `SIGHUP` 热重载配置）。
- `deploy/nginx.conf.example`：HTTPS 反向代理配置，建议配合域名 TLS 与 IP 白名单使用，避免直接暴露 `28081` 端口到公网。

## 安全说明

- `.env`、`config.yaml`、`state.db` 与 `session/` 目录均包含敏感凭据或登录态，已在 `.gitignore` 中排除，切勿提交到仓库。
- 服务默认仅绑定 `127.0.0.1:28081`，建议通过 HTTPS 反向代理或内网隧道（如 Tailscale）访问。

## License

[MIT](LICENSE)
