# BeauClaw

用 **uv + Python 3.12** 管理的多平台榜单监控工具，支持 **GitCode CANN** 和 **阿里云天池** 同时采集。每个已添加赛事独立每 **10 秒**采样，保存完整榜单、原始响应和历史变化；**只有监控榜首换队或分数上升时**，才向全部通知邮箱发送邮件。榜首分数下降也永久保留为关键记录，但不发邮件。

终端提示为英文，登录、加载榜单、启动、停止和测试发信带加载动效。邮件使用中文，发信单位 **ICTHub**，包含榜首变化、分隔线以及同次快照的前十名。GitCode 沿用橙红色样式；天池使用赛事网页的蓝紫色、浅色背景和灰色表头，前十名为 **排名、团队名称、组织、分数** 四列。

## 安装与升级

```bash
curl -fsSL https://github.com/winbeau/tools-claw/releases/latest/download/install.sh | sh
```

支持 Linux / macOS。安装器校验 SHA-256，准备 uv、Python 3.12、tmux，并按 `uv.lock` 安装依赖。缺少 tmux 时会使用系统包管理器，可能需要 sudo；macOS 需要已有 Homebrew。

升级再次运行上述命令，然后执行 `beauclaw stop && beauclaw start`。已有 SMTP 配置、登录凭据、通知邮箱和历史数据会保留。旧版单赛事数据库会自动登记到榜单列表，继续使用原历史；新赛事使用独立数据库。

## 榜单管理

```bash
beauclaw ranking add https://competition.gitcode.com/competition/2094722369343447042/live-ranking
beauclaw ranking add https://tianchi.aliyun.com/competition/entrance/532499/rankingList
beauclaw ranking list
beauclaw ranking delete d2e527
```

`list` 格式：

```text
d2e527-2026年CANN挑战赛_西北赛区 -> https://competition.gitcode.com/competition/2094722369343447042/live-ranking
f67930-CSIG图像图形技术挑战赛-赛道一：生成式图像增强可控性挑战 -> https://tianchi.aliyun.com/competition/entrance/532499/rankingList
```

自动读取比赛名称，支持 `add URL --name "自定义名称"`。同一赛事的 URL 会规范化并去重，分配稳定的 **6 位哈希短名**，遇到哈希碰撞会选择另一短名。删除时使用 `list` 中的短名。

平台由链接自动识别。赛事身份由“平台 + 比赛编号”共同确定，不同平台的相同数字编号可同时添加，各自保存历史；升级保留旧 GitCode 榜单的短名、顺序、历史和邮件队列。仅传数字编号仍按 GitCode 处理。

| 平台 | 监控范围 | 登录 |
| --- | --- | --- |
| GitCode CANN | 完整赛区榜与总榜，仅赛区榜首触发通知 | `beauclaw login` 保存的 GitCode 网页会话 |
| 阿里云天池 | 当前公开赛程的完整分页榜单，首支队伍为榜首 | 公开榜单无需登录，不读取或发送 GitCode 凭据 |

天池通过[赛事网页](https://tianchi.aliyun.com/competition/entrance/532499/rankingList)使用的公开接口读取比赛名称、赛程与榜单。每轮先取第一页，再以最多 4 个并发请求补齐其余页，并复核第一页是否发生变化。分页失败、缺页、重复队伍或跨赛程数据不会推进比较基线；一次完整采集超过 `--timeout` 时整轮保留为失败记录并退避重试。默认请求期限为 8 秒，最多采集 100 页；大榜单可适当提高 `--timeout`。空榜或尚未公开的赛程也保留上次有效记录。

天池按网页顺序保留并列排名，组织未公开时显示 `—`。GitCode 与天池在本地榜单、历史最高分、变化时间线及邮件中均将分数四舍五入为 **3 位小数**，不足补零，例如 `4.1105259860559` 显示为 `4.111`。原始快照、存储和变化判断保留完整精度；小于显示精度的上涨仍按原规则通知。

天池偶尔会返回有效队伍 ID、名次和分数，却缺少队名或组织。此时仍正常采集：按队伍 ID 关联历史队名，并标明“上次公开队名”；没有历史队名时显示“未公开队名（ID …）”。资料缺失或首次补全队名不当作换队、改名；真实换队、已知队名变更和分数变化仍正常记录。缺少有效 ID、分数或分页不完整时继续拒绝比较。

列表按首次添加时间排列。后台运行期间增删榜单会自动生效，无需重启。删除会停止该赛事的后续采集、取消待发邮件并保留历史；重新添加会复用短名和历史。每个赛事独立采样、退避和发信，一个赛事的慢请求不会拖慢其他赛事。

## 邮箱配置与通知列表

```bash
beauclaw config set
beauclaw notice add notify@example.com another@example.com
beauclaw notice list
beauclaw notice delete 六位短名
```

邮箱列表同样显示 `六位哈希短名-邮箱地址`，重复添加会去重，删除按短名进行。升级时原有邮箱自动获得短名。删除会取消该邮箱在所有榜单中的待发邮件；已开始投递或已被 SMTP 接受的邮件无法撤回。

`config set` 交互设置发信厂商、完整发信地址和隐藏输入的 SMTP 密码。当前厂商只有 `aliyun`，复用 **smtpdm.aliyun.com:465 SSL**，支持批量邮件类型的发信地址。

```bash
beauclaw config set mail.provider aliyun
beauclaw config set mail.sender cann@mail.icthub.top
beauclaw config set mail.password
beauclaw config show
```

密码使用阿里云邮件推送控制台为该发信地址设置的 SMTP 密码，不作为命令行参数输入。`config show` 不显示密码。端口还支持 25 / 80，使用 STARTTLS；例如 `beauclaw config set mail.port 80`。

参见阿里云官方的 [SMTP 服务地址](https://help.aliyun.com/zh/direct-mail/smtp-endpoints)与[发信方式](https://help.aliyun.com/zh/direct-mail/getting-started/three-mail-sending-methods)。每个收件人单独投递，不披露其他通知邮箱。

## 登录与测试邮件

```bash
beauclaw login --browser
beauclaw test
beauclaw notice test xxxx@yy.com
beauclaw notice test xxxx@yy.com --ranking f67930
```

浏览器登录使用独立配置目录。页面导航超时或登录跳转时临时读不到 Token，会继续检查会话；以榜单接口验证通过作为成功依据。浏览器清理异常不会覆盖已验证的结果。等待上限 10 分钟，Ctrl+C 取消。缺少浏览器或图形桌面时会给出对应的英文提示。

纯终端可运行 `beauclaw login` 隐藏输入已登录赛事网页的 `access_token`，或使用 `login --token-file PATH`。`login --competition URL` 可指定用于验证登录的赛事。`login --from-gc` 尝试复用 gc 凭据，但 GitCode CLI 的个人访问令牌不一定适用于赛事接口。

**`beauclaw test` 会实际向所有通知邮箱发送邮件**：实时读取 `ranking list` 中第一个赛事的监控榜单，仅展示前十名，省略榜首变化区块。无需启动后台监控，需要 SMTP 配置；只有 GitCode 榜单需要可用的登录会话。测试不会写入快照或改动比较基线。

**`beauclaw notice test xxxx@yy.com` 只向指定邮箱发送一封测试邮件**，不修改通知列表；即使通知列表为空也可使用。省略地址的 `beauclaw notice test` 与 `beauclaw test` 一样向列表中所有邮箱发送。队名在列宽足够时单行展示，超长队名在列内换行，不挤压分数列。

两种测试命令均支持 `--ranking 六位短名`，选择具体赛事并自动采用该平台的邮件样式；省略时仍使用列表中的第一个赛事。例子中的 `f67930` 对应天池 `532499`，请以实际 `ranking list` 为准。

如果没有监控赛事，提示 `Rankings is empty; no leaderboard is being monitored.`，不请求榜单、不发送邮件。首个赛事的接口失败、空榜或封榜时也不发送测试邮件。每个邮箱显示独立结果，部分失败时继续处理其余邮箱并返回非零退出码。旧命令 `beauclaw notice test` 继续可用。

## 启动、状态与停止

```bash
beauclaw start
beauclaw status
beauclaw status --json
beauclaw stop
```

`start` 使用专用 tmux socket 与会话，终端退出或 SSH 断开后继续运行。重复启动不会生成第二个进程。空榜单列表时后台保持等待，添加后自动开始采样。

`status` 显示进程、各赛事的最近采样状态和邮件队列。进程运行不代表登录凭据有效，请关注采样错误。`stop` 正常停止并保留历史与队列，不影响其他 tmux 会话；默认等待 30 秒，必要时可 `stop --force`。不包含开机自启。

```bash
beauclaw watch          # 前台监控所有已添加赛事
beauclaw watch --once   # 每个赛事抓一次后退出
beauclaw --no-animation start
```

支持 `--interval 10`、`--timeout 8`、`--missing-samples 2`、`--no-web`、`--no-mail`、`--port 8766` 和 `--db PATH`。`--competition ID` 保留单 GitCode 赛事采集模式；多平台监控请通过 `ranking add URL` 管理，并直接 `start`。自定义 `--db` 时，各管理命令也要使用同一个注册数据库路径。

动画仅在交互终端启用，重定向日志和 JSON 输出保持纯文本。`BEAUCLAW_NO_ANIMATION=1` 可全局关闭动效；`NO_COLOR` 关闭颜色。

## 历史页面与导出

打开 **http://127.0.0.1:8765**，按比赛和赛程切换记录，查看成绩、观测最高分、缺席队伍、变化时间线和原始快照。平台名称与“打开原榜单”链接随赛事切换，天池队伍记录额外显示组织。默认白色主题，右上角“关灯 / 开灯”切换黑色和白色，浏览器会记住选择。页面每 3 秒读取本地数据库，不增加赛事接口请求。

每次采样后自动清理：每个赛事保留 **100 份普通快照**，优先保护仍在使用的有效比较基线，其余保留最近的采样。若历史赛程的比较基线本身超过 100 份，会优先完整保留基线。监控榜首换队、改名、分数上升或下降的快照，以及对应的**变动前快照**，标记为关键并永久保留，**不占普通快照的 100 份额度**，因此总数可以超过 100。初次采集、新赛程基线与接口失败不算榜首变化。

旧版榜首变化会自动补上关键标记，下次采样开始清理。清理同时回收无人引用的压缩响应，保留变化日志、历史最高分以及已发、待发和已取消的邮件记录。SQLite 会复用回收的空间，数据库文件大小不一定立即缩小。页面显示关键记录和变动前快照；已清理的普通快照显示“已自动清理”，其变化日志仍可查看和导出。`status` 和快照 JSON 也提供关键标记或计数。

```bash
beauclaw export --ranking d2e527 --output events.csv
beauclaw snapshot 1 --ranking d2e527 --output snapshot-1.json
beauclaw serve
```

多个赛事时导出需指定 `--ranking`。不同赛事的快照编号独立。

| 内容 | 默认路径 |
| --- | --- |
| SMTP 配置和密码 | `~/.config/beauclaw/mail.json` |
| GitCode 登录凭据 | `~/.config/beauclaw/gitcode.json` |
| 榜单注册表、通知邮箱、旧版历史 | `~/.local/share/beauclaw/beauclaw.sqlite3` |
| 新增赛事历史与发信队列 | `~/.local/share/beauclaw/rankings/六位短名.sqlite3` |
| 后台日志 | `~/.local/share/beauclaw/beauclaw.log` |
| 独立登录浏览器 | `~/.local/share/beauclaw/browser-profile/` |
| 安装版本 | `~/.local/share/beauclaw/app/v0.4.2/` |

路径尊重 XDG 设置；`BEAUCLAW_CONFIG_DIR` / `BEAUCLAW_DATA_DIR` 可重定向目录。凭据文件权限为 `0600`。`BEAUCLAW_TOKEN` / `BEAUCLAW_SMTP_PASSWORD` 可提供环境变量凭据。每轮重新读取配置。

## 通知和记录规则

- GitCode 保存赛区榜与总榜，天池保存当前公开赛程的完整分页榜。仅监控榜首换队（身份或队名变化）或分数上升触发通知。与上次有效采样比较，下降后回升也通知；换队即使分数更低也通知。同队分数下降只保存关键记录。
- 首次采集和新赛程首次出现只建立基线。其他队伍的分数、名次、缺席与重现会记录但不发邮件。
- HTTP 失败、登录过期、缺字段、非法分数、空榜与封榜不会被当成正常榜首变化，不推进缺席次数。
- 通知的前十名与触发事件来自同一次快照，队名与分数经过 HTML 转义。邮件同时包含中文 HTML 和纯文本版本，采样时间使用北京时间。正式通知标注“关键邮件”并设置高重要性邮件头；测试邮件不标重要性。升级前排队的同队降分通知会取消投递，记录仍保留。
- 每个邮箱独立入队、投递、退避和重试。删除或重新添加邮箱后不会收到旧订阅的排队邮件。
- SMTP 接受后不再重发；连接在服务器接受后断开或进程在确认落盘前退出，仍可能造成重试重复。SMTP 接受不等于最终进入收件箱。
- 休眠、关机或退出会中断采集，失败与限流会延长采样间隔。普通快照按上述规则自动清理；关键记录、变化日志和进程日志不自动清理。

记录只能反映实际观测到的公开成绩，不能证明有人故意藏榜，也无法观测从未公开或在两次采样间出现又消失的成绩。

## 源码开发和验证

```bash
uv sync --extra browser
uv run --extra browser python -m unittest discover -s tests -v
uv run python scripts/preview_email.py
uv run python scripts/build_release.py
```

`.python-version` 为 `3.12`，`requires-python = "==3.12.*"` 禁止其他 Python 次版本，依赖版本由 `uv.lock` 锁定。采集、邮件、动效和存储使用标准库；浏览器登录使用可选的 Playwright。

测试使用合成榜单、模拟 SMTP 和真实 tmux，不发送真实邮件。预览脚本输出标有“示例数据”的通知与测试邮件。构建脚本输出 wheel、包含锁定依赖的发布包、安装脚本和 SHA-256 校验文件。
