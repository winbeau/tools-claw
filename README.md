# BeauClaw

每 **10 秒**记录一次 [CANN 西北赛区榜单](https://competition.gitcode.com/competition/2094722369343447042/live-ranking)，保存原始响应、排名变化和观测最高分。**仅西北赛区榜首队伍或分数变化时**向通知列表中的每个邮箱发信，包含变化前后的队名、分数和采样时间（北京时间）。

## 安装

```bash
curl -fsSL https://github.com/winbeau/tools-claw/releases/latest/download/install.sh | sh
```

安装脚本校验发布包的 SHA-256，自动准备 uv、Python 3.12 和 tmux，并按 `uv.lock` 安装依赖。缺少 tmux 时会使用本机包管理器，可能需要 sudo。支持 Linux/macOS；macOS 安装 tmux 需要已有 Homebrew。

```bash
beauclaw config set
beauclaw notice add notify@example.com
beauclaw login --browser
beauclaw start
beauclaw status
```

将 `notify@example.com` 换成通知邮箱。配置和数据在用户目录持久化，重新安装和升级会保留。安装完成后若当前终端提示找不到命令，请按安装输出将命令目录加入 PATH。

指定版本安装：

```bash
curl -fsSL https://github.com/winbeau/tools-claw/releases/latest/download/install.sh | BEAUCLAW_VERSION=0.2.0 sh
```

再次运行安装脚本即可升级，然后执行 `beauclaw stop && beauclaw start` 使用新版本。安装脚本不会自动停止运行中的采集进程。可用 `BEAUCLAW_BIN_DIR` / `BEAUCLAW_INSTALL_DIR` 自定义命令目录和程序目录。

从源码开发：

使用 **uv + Python 3.12**：`.python-version` 指定 `3.12`，`requires-python = "==3.12.*"` 禁止其他次版本，`uv.lock` 锁定依赖。本次使用 Python 3.12.12。支持 Linux/macOS。

```bash
uv sync --extra browser
uv run beauclaw --help

# 开发时安装为任意目录可用的 beauclaw 命令
uv tool install --python 3.12 --editable '.[browser]'
beauclaw --version
```

下面的命令都可改为 `uv run beauclaw ...` 在项目中运行。采集、SMTP、SQLite 和页面使用标准库；只有可选的浏览器登录依赖 Playwright。

## 配置发信邮箱

```bash
beauclaw config set
```

依次选择服务器厂商（目前只有 `aliyun`）、填写完整发信地址、隐藏输入 SMTP 密码。自动复用 `smtpdm.aliyun.com:465` 和 SSL。**使用阿里云邮件推送控制台中为该发信地址设置的 SMTP 密码**。

也可逐项设置：

```bash
beauclaw config set mail.provider aliyun
beauclaw config set mail.sender your-name@mail.icthub.top
beauclaw config set mail.password
beauclaw config show
```

密码只能隐藏输入，不能作为命令行参数传入。`config show` 仅显示密码是否已配置。`beauclaw config set mail` 也是交互配置入口。

预设支持 465 / 25 / 80：465 使用隐式 SSL，25 / 80 使用 STARTTLS 加密后登录。用 `beauclaw config set mail.port 80` 切换。`smtpdm.aliyun.com` 对应阿里云邮件推送杭州地域，详见[官方 SMTP 地址说明](https://help.aliyun.com/zh/direct-mail/smtp-endpoints)。

**批量邮件类型的发信地址可以直接使用 SMTP**，无需云端邮件模板或 BatchSendMail 收件人列表。BeauClaw 按自己的列表逐个投递。参见[阿里云发信方式说明](https://help.aliyun.com/zh/direct-mail/getting-started/three-mail-sending-methods)。

## 通知邮箱列表

```bash
beauclaw notice add first@example.com
beauclaw notice add second@example.com third@example.com
beauclaw notice list
beauclaw notice delete second@example.com
beauclaw notice delete 3
```

添加会去重。删除支持邮箱或列表编号，并取消该邮箱排队中的未发送通知；已开始投递或已发送的邮件无法撤回。列表修改自动生效，新增邮箱从下一次榜首变化开始收信。

每个收件人单独发送、独立重试；某个地址失败不会阻塞其他地址，邮件不披露其他通知邮箱。以下命令向**当前列表所有邮箱实际发送测试邮件**：

```bash
beauclaw notice test
```

## 登录赛事与采集

`gc auth login` 使用的个人访问令牌与赛事网页登录会话不同。本机实测 gc 已登录，但该凭据访问赛事接口返回 `TOKEN_INVALID_ERROR`。

终端启动一次网页登录，成功后自动保存会话，之后采集无需浏览器：

```bash
beauclaw login --browser
```

使用独立浏览器配置目录。没有 Google Chrome 时，在项目中执行 `uv run --extra browser playwright install chromium` 安装浏览器。

已有赛事网页的 `access_token` 时可用 `beauclaw login` 在终端隐藏输入。Token 位于已登录赛事网页开发者工具 Application / Local Storage / `access_token`。另支持 `login --token-file /path/to/token`、运行时环境变量 `BEAUCLAW_TOKEN`、`login --from-gc` 尝试复用 gc。工具只在榜单访问通过后保存凭据。

```bash
beauclaw watch --once  # 验证一次抓取
beauclaw start         # 通过 tmux 每 10 秒后台采集并通知
```

打开 **http://127.0.0.1:8765** 查看最近分数、观测最高分、缺席队伍、变化时间线和原始快照。页面默认选择西北赛区，每 3 秒读取本地数据库，不增加赛事接口请求。

## 启动、状态与停止

```bash
beauclaw start
beauclaw status
beauclaw status --json
beauclaw stop
```

`start` 使用 BeauClaw 专用 tmux socket 和会话，终端退出、SSH 断开后继续运行；重复 `start` 不会创建第二个进程。`status` 显示运行/停止状态、PID、启动时间、版本、日志路径，以及最近采样状态和邮件队列。进程正在运行不代表鉴权成功，请留意最近采样的错误信息。

`stop` 向确认属于该 tmux 会话的采集进程发送 SIGTERM，等待当前采样落盘，保留数据库与邮件队列；不影响其他 tmux 会话。默认等 30 秒，超时后可用 `beauclaw stop --force` 强制结束专用会话。关机、重启、休眠会中断采集，需要重新 `start`，不包含开机自启。

```bash
tail -f ~/.local/share/beauclaw/beauclaw.log

# 排查问题时也可以在前台采集，Ctrl+C 停止
beauclaw watch
```

## 导出与路径

```bash
beauclaw export --output events.csv
beauclaw snapshot 1 --output snapshot-1.json
beauclaw serve  # 停止采集后单独看历史
```

默认路径尊重 XDG 设置：

| 内容 | 路径 |
| --- | --- |
| 发信配置与 SMTP 密码 | `~/.config/beauclaw/mail.json` |
| GitCode 登录凭据 | `~/.config/beauclaw/gitcode.json` |
| 快照、通知列表、发件队列 | `~/.local/share/beauclaw/beauclaw.sqlite3` |
| 后台进程状态、日志 | `~/.local/share/beauclaw/beauclaw.service.json` / `beauclaw.log` |
| 通过安装脚本安装的版本 | `~/.local/share/beauclaw/app/v0.2.0/` |
| 独立浏览器会话 | `~/.local/share/beauclaw/browser-profile/` |

凭据文件权限 `0600`。`BEAUCLAW_CONFIG_DIR` / `BEAUCLAW_DATA_DIR` 可重定向目录，`BEAUCLAW_SMTP_PASSWORD` 可覆盖文件中的密码。每轮重新读取配置，更新密码无需重启。

`start` / `watch` 支持 `--interval 10`、`--timeout 8`、`--missing-samples 2`、`--no-web`、`--no-mail`、`--db PATH`、`--port 8766`。`watch` 还支持 `--samples 3` / `--once`。`--no-mail` 暂停入队与发送。自定义 `--db` 时，`notice` / `status` / `stop` 命令也要指定同一数据库。

## 规则与边界

- 两个榜单均保存；仅 `realtime_region_ranking`（本赛事西北赛区榜单）榜首换队或分数变化时通知。首次采集、首次进入新赛程只建基线，其他名次变化不发邮件。
- HTTP 失败、登录过期、缺字段、非法分数、空榜和封榜不当作队伍消失。有效非空快照首次缺席即记录，默认缺席两份后标为持续未出现；失败和空榜不推进计数。
- 相同响应按 SHA-256 去重压缩，每次请求的时间与状态均保存。重启续记，不丢观测最高分。不同榜单和赛程分别记录。
- 优先以 `team_id` / `namespace_id` 匹配队伍，缺少稳定 ID 时用队名，因此不能仅凭改名认定是同一队。最高分指开始监控以来观测到的最高分。
- 快照与各邮箱发件任务一起提交到 SQLite。独立线程发信，失败退避重试，重启后继续；SMTP 慢响应不拖慢采样。
- SMTP 接受后不重发。若服务器已接收，但连接中断或进程在确认落盘前退出，重试仍可能重复；重试使用同一 Message-ID。SMTP 接受不等于最终进入收件箱。
- 日志使用 UTC，页面使用本机时区，邮件使用北京时间。网络失败、限流会延长采样间隔并遵循 `Retry-After`。休眠、关机或进程退出会停止采集。数据与日志不自动清理。

记录只是排查线索，无法证明有人故意藏榜，也无法观测从未公开的成绩、服务器尚未公布的更新、两次采样之间出现又消失的成绩。

## 验证

```bash
uv run python -m unittest discover -s tests -v
uv run python scripts/build_release.py
```

测试使用合成榜单与模拟 SMTP，并测试真实 tmux 启停、重复启动、旧状态文件和其他会话隔离；不会发送真实邮件。构建脚本在 `dist/v0.2.0/` 输出 wheel、包含锁定依赖的源码包、安装脚本与 `SHA256SUMS`。
