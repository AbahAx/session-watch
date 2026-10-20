# Session Watch

独立的 Xray 客户端活动监控与 Telegram 通知面板。

**v0.6.0。** 同机部署可免输 3x-ui 账号密码，自动同步客户端清单、查询已有 Xray
在线状态并只读解析访问日志；不修改 3x-ui 程序、数据库、入站或客户端配置。
仅提供正式运行，默认暂停活动统计与 Telegram 发送。本仓库不包含真实服务器资料或凭据。

## Linux 安装包

发布包自带已构建的前端，VPS 无需 Node.js。支持独立服务、开机启动、自动检测本机来源、
升级备份、失败回退、手动备份/回滚和卸载。完整命令、兼容范围、HTTPS 和只读边界见
[Linux 一键部署说明](docs/LINUX_INSTALL.md)。

```bash
sha256sum --check --ignore-missing SHA256SUMS
tar -xzf session-watch-v0.6.0-linux.tar.gz
cd session-watch
sudo bash install.sh
```

在 SSH 终端安装时，向导会询问访问方式、端口、管理员用户名和密码。
可选择本机 SSH 隧道访问，或使用现有证书通过 `https://你的域名:端口` 访问。
升级保留现有账号；页面“系统设置 → 管理员账号”可修改用户名和密码。
新安装默认关闭监控与 Telegram 发送，不自动启用 3x-ui 的访问日志或统计接口。
源码仓库：[AbahAx/session-watch](https://github.com/AbahAx/session-watch)。
正式安装包发布在 [Releases](https://github.com/AbahAx/session-watch/releases)。

```bash
bash <(curl -fsSL https://github.com/AbahAx/session-watch/releases/latest/download/online-install.sh) --repo AbahAx/session-watch
```

独立 `online-install.sh` 入口会解析正式版本、下载同一标签下的安装包和校验文件，
校验通过后执行本机安装器；具体发布步骤见部署说明的“像 3x-ui 一样在线安装”。

## 功能

- 持续自动发现原生与 Docker 3x-ui：读取当前核心日志配置，跟随日志延迟生成、路径变更和容器重建。
- 支持自动或手动日志路径；多个实例明确选择，日志和在线数据保持来自同一实例。
- 中文响应式管理界面：活动概览、客户端、活动记录、日志来源、通知中心、系统设置。
- 独立管理员账号、登录限流、HttpOnly 会话 Cookie、修改用户名/密码及其他会话失效。
- 按 email 识别账号，自定义显示名称、暂停单个账号监控。
- 在线状态从离线变为在线时生成一条上线消息；持续离线满 **5 分钟**后生成一条下线汇总。
- 等待期间恢复在线：继续同一轮活动，不重复发送上线提醒。
- API 错误记为未知，离线确认计时重新开始；停止监控、程序停机不算已观测离线。
- 汇总来源 IP、入站、连接记录数、不同目标数和访问次数排行。
- 可选推送 IP 归属地：在上线 IP、下线最后 IP 下方显示省市信息，支持 IPv4/IPv6。
- SQLite 保存活动、读取游标及发送队列；重启后恢复。读取游标与日志事件同一事务提交。
- 跟随日志追加、轮转、截断，等待半行写完；首次接入跳过历史内容。
- Telegram 失败重试、限流等待、手动重试、消息长度限制及消息预览。
- 本机客户端自动同步，包括禁用和未关联入站的账号；保留本项目备注与监控偏好。
- 未开启实际发送时生成的预览消息不会自动补发。
- 普通设置即时生效；节点接入支持自动检测、检查当前输入、检查通过后保存。
- 暂停推送独立于暂停监控；恢复时可保留或忽略积压提醒，保留通知历史。
- 首页分别展示客户端同步、在线查询、日志读取和 Telegram 健康状态及成功时间。
- 上下线通知包含节点名称，配置版本校验避免旧页面覆盖较新的修改。

## 通知规则

```text
离线 → 在线：上线提醒 + 开始统计
在线 → 离线：进入待确认状态
待确认期间恢复：回到在线，保留本轮统计，不重复提醒
连续离线满 300 秒：下线汇总 + 结束本轮
汇总后重新上线：开始下一轮
任何查询失败：状态未知，不发送下线提醒，不累计未知时间
```

第一次健康状态快照用作基线。启动时已经在线的账号会建立观察记录，但不补发
上线提醒。之后新出现的账号、或由离线转在线的账号，正常触发通知。

查询默认每 10 秒一次，实际通知存在查询周期误差。程序只观测快照，不能发现
两次查询之间全部发生并结束的极短连接。等待确认的最后五分钟不计入活动时长。
期间发生短暂离线又恢复，则整段归为同一轮活动，时长是这一轮的时间跨度。

上线消息会最多等待一个查询周期来补齐第一条访问记录，确保只发一条消息。
没有日志时显示“暂无记录”。“首次访问”是该轮被程序观测到的第一条目标，
不是还原账号的完整历史。

### 示例

```text
🟢 LIN 上线

🌐 IP：203.0.113.10
🚪 入站：in-24641-tcp
🎯 首次访问：example.com
🕐 时间：20:30:00
```

```text
🔴 LIN 下线

🌐 最后 IP：203.0.113.10
⏱ 在线：6 分 25 秒
📊 请求：30
🔄 IP：1 个 · 入站：1 个
🌍 目标：2 个
🕐 最后活动：20:36:20

🌍 主要访问
1. example.com ×20
2. example.org ×10

💤 已连续离线 5 分钟
```

“请求”指被接受连接的访问日志条数，不等于 HTTP 请求数或网页浏览次数。
若访问日志出现读取异常，状态查询仍继续，汇总附加“统计可能不完整”提示。
如果只是正在追赶积压日志，会等读取完成再生成汇总。

### IP 归属地（可选）

通知中心的“推送 IP 归属地”默认关闭。开启后，在发送消息前通过
[IP9 HTTPS API](https://www.ip9.com.cn/) 查询该条通知对应的公网 IP，追加
`📍 IP归属地：河北省保定市` 这样的字段。上线使用首次 IP，下线使用最后 IP；
发送记录保存实际文本，关闭开关后新发送消息不再查询或附加归属地。

只向服务商提交 IP，不发送客户端名、访问目标、日志或 Telegram 凭据。内网、
保留、无效 IP 不对外查询。成功结果缓存七天，失败缓存十分钟，最多缓存 5000 条；
请求间隔至少两秒，尊重 HTTP 429 的 Retry-After。网络或定位失败时显示明确占位，
原通知照常发送。等待查询期间暂停监控或关闭发送，会保留待发消息，不越过暂停状态。

归属地只是服务商 IP 库的估计，并非设备实际位置。不拼接不存在的县区；数据源
返回到城市就只显示城市，返回县区时才附加。功能不会改写已发送的历史消息。
未发送的消息预览可显示“待查询”，实际归属地在发送前填入。

## 本地启动

需要 **Python 3.11+、Node.js 22.12+（建议 24）、pnpm 11.25.0**。

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

cd frontend
npm install --global pnpm@11.25.0
pnpm install --frozen-lockfile
pnpm build
cd ..

.\start-local.ps1
```

macOS / Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cd frontend
npm install --global pnpm@11.25.0
pnpm install --frozen-lockfile
pnpm build
cd ..
.venv/bin/python -m uvicorn app.main:create_app --factory \
  --host 127.0.0.1 --port 8765 --no-access-log --no-proxy-headers
```

访问 <http://127.0.0.1:8765>。首次创建独立管理员账号，密码至少 12 字符。
没有出厂账号或默认密码，也没有内置虚构数据。同机部署使用独立采集器自动读取
客户端；开发机未安装 3x-ui 时显示空列表与未配置状态，不产生虚构在线记录。

前端开发可额外在 `frontend/` 运行 `pnpm dev`，其 `/api` 请求代理到后端 8765。
后端启动时需已经存在 `frontend/dist/` 才会注册静态页面路由，新增构建后重启后端。
请只运行一个后端 worker；同一数据目录有跨进程锁，避免重复采集和发送。

## 接入与启动

1. 同机部署默认使用“本机自动读取”。按 [本地采集器说明](deploy/LOCAL_SOURCE.md)
   安装隔离服务，无需 3x-ui 账号密码。读取现有客户端数据库与已有的 Xray 在线查询接口，
   不会自动改动 3x-ui 去开启统计。若当前核心不支持，显示明确异常。
2. 由管理员预先开启 Xray 访问日志。在“日志来源”填写本应用可读取的日志路径和
   日志时区，保存后检查配置。日志时间与页面显示时区分开配置。
3. 在“节点接入”使用自动检测，或手动填写来源；“仅检查当前输入”不保存，
   “检查并保存”成功才替换配置，失败继续使用原配置。检查通过后启动监控。
   客户端 email 须与日志对应；本地模式自动导入全部有效 email
   客户端。清单每 10 秒更新，禁用、未关联入站和已移除来源均有标记，移除保留历史。
4. 在“通知中心”配置 Telegram Bot Token 和 Chat ID，开启实际发送并保存。
   测试按钮会真实发送消息；关闭实际发送时只生成预览。

远程面板可另选“远程面板 API”，填写含自定义路径的根地址、账号、密码和可选验证码。
当前远程适配器查询在线账号；自动导入全部客户端是本机接入能力。
普通设置通过保存即时生效，无需暂停监控。采集来源变化使用“检查并保存”，
成功后重建观测基线并标记当前统计可能不完整；失败不改变原配置。
修改 Telegram 接收配置前先暂停推送，避免发送中的消息突然切换接收者。
本机清单在监控暂停时仍同步。关闭浏览器不影响后台任务。
空密码、空 Token 保持原值，接口仅返回是否配置。2FA 是当次验证码，非种子；
远程面板重新登录时可能需要更新。登录失败冷却五分钟。

### 监控与推送分别控制

“暂停监控”停止活动统计与自动发送，当前在线显示未知，恢复后重新确认状态。
“暂停推送”只停止后续自动投递，继续查询状态、统计访问并生成待发消息。
恢复推送时可选择保留积压，或将待发/重试/失败消息标记为已忽略；历史文本仍保留。
保留的失败消息需要在通知中心手动重试。关闭“启用实际发送”则生成预览，预览不补发。
手动测试为明确操作，推送暂停时仍可使用，不会恢复自动推送。

首页健康状态中的“日志最近成功”指成功读取文件的时间，“最后访问”是最近一条
访问事件时间；没有新访问不等于日志故障。Telegram 显示待发、失败数和最近成功投递，
未配置、暂停推送、监控暂停分别展示。

面板适配仅允许以下路径，均相对于面板根地址：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `csrf-token` | 新版 CSRF 校验 |
| POST | `login` | 建立查询会话 |
| POST | `panel/api/clients/onlines` | 新版在线列表 |
| POST | `panel/api/inbounds/onlines` | 旧版在线列表 |

自动模式仅在接口不存在（404/405）时尝试旧路径。其他错误一律记为未知，
避免将认证失败、格式不兼容等当成“所有人离线”。

## Docker 文件

仓库包含 `Dockerfile` 与 `compose.yaml`。容器构建与运行仍需在实际 Docker 环境验证。
首次本机开发环境中 Docker 引擎未运行，镜像构建尚未验证。

以后需要部署时：

```bash
docker compose up --build -d
docker compose exec session-watch python -m app.cli init-admin
```

Compose 默认将页面绑定到宿主机 loopback。容器以非 root 用户运行，源日志目录需
预先有相应读取权限，应用不会更改该目录权限。按需启用 Compose 中只读挂载示例，
UI 填容器路径 `/logs/access.log`。本机模式另将宿主机采集器快照目录只读挂载进来，
按 [本地采集器说明](deploy/LOCAL_SOURCE.md) 配置。不要将整个 3x-ui 安装目录、面板数据库或 Docker
socket 挂载进来。目录挂载用于跟随轮转后的新日志；只挂载单文件可能固定在旧 inode。

首次管理员初始化也可以在停止的原生服务上运行：

```bash
python -m app.cli init-admin --data-dir ./data
```

## 数据与边界

默认数据目录 `data/`，可用 `SESSION_WATCH_DATA` 修改。数据库只保存在本项目
的数据目录内。`secret.key` 用于加密面板密码和 Bot Token；备份应包含数据库及密钥。
需要应用一致性备份时先停止程序，或使用 SQLite backup API，不能只复制运行中的
主数据库而忽略 WAL。保护数据目录及其备份，不要提交至 GitHub。

- `SESSION_WATCH_SECURE_COOKIE=1`：HTTPS 访问时开启 Secure Cookie。
- `SESSION_WATCH_ALLOWED_HOSTS`：逗号分隔的允许 Host，默认仅本机地址；使用反向代理
  域名时需要显式添加该域名，防止 DNS 重绑定。不要填写 `*`。
- `SESSION_WATCH_PUBLIC_ORIGIN`：HTTPS 反向代理后的完整公开源，例如
  `https://watch.example.com`，用于同源校验。代理应保留原始 Host，并清除外部传入的
  转发头；应用默认不信任转发头。
- `SESSION_WATCH_BRIDGE_HEARTBEAT`：使用隔离日志桥接服务时的心跳文件路径。
  超过 20 秒未更新会将日志来源标为异常，汇总注明统计可能不完整。
- 真实部署建议通过 SSH 隧道或 HTTPS 反向代理访问；首次网页初始化仅允许 loopback。
- 单账号多设备共享 email 时合并统计，无法把账号在线状态当成单台手机状态。
- 面板在线状态取决于上游版本及核心能力，可能基于活跃连接、流量变化和宽限时间，
  不代表设备是否联网或客户端开关是否开启。
- 只统计日志实际记录的目标域名/IP，不包含 HTTPS 完整网址、页面内容或可靠流量统计。
- Telegram 为持久化重试投递；远端已收到但响应丢失时，重试可能重复，不能保证严格一次。
- 已生成的待发送消息仍然使用当前 Telegram 配置。更换接收者前应确认队列状态。
- 不提供纯日志推断模式、多节点采集端、多人权限、WebSocket 实时推送或自动更新；
  第一版按面板在线状态判断，页面每 10 秒刷新。
- 数据保留按已结束活动的结束时间清理；原始临时事件主要用于首次日志关联，
  超过 10 分钟会在下一轮采集时清理。源日志轮转由服务器管理员管理。
- 如果停机期间旧日志已被移走或压缩，重启只能从当前文件恢复，不能找回所有
  旧文件中的未读事件；轮转与读取权限仍需在目标 Linux 环境验证。

详细安全边界见 [SECURITY.md](SECURITY.md)。

### 原始日志仅 root 可读时

可选的 `deploy/log_bridge.py` 用于把新增日志复制到本项目自己的只读消费目录，
网页进程继续以独立非 root 用户运行，不必修改源目录或日志权限。

桥接服务应由系统管理员以 systemd 沙箱运行：源目录使用 `ReadOnlyPaths`，仅自己的
spool 使用 `ReadWritePaths`，隔离面板数据库、证书、SSH 和应用凭据目录，禁止网络，
去除 capabilities。不要将整个网页服务作为 root 运行。

```bash
python3 deploy/log_bridge.py \
  --source /var/log/x-ui/access.log \
  --spool /var/lib/session-watch-bridge
```

首次安装跳过历史；游标保存后可恢复，输出只在自己的目录内轮转（默认约 32 MiB × 2），
不修改源文件。spool 的所属组需要允许应用用户读取。将日志路径配置为桥接输出
`/var/lib/session-watch-bridge/access.log`，并配置上述心跳环境变量。
不要把 source 放进 spool 目录。桥接本身增加了一个采集环节：停机跨轮转时可能缺失
已移走的源日志，极端崩溃窗口下也不能保证日志统计严格一次。

## 测试

```bash
python -m pytest -q
cd frontend
pnpm build
```

测试使用临时数据库、模拟 HTTP 响应和虚拟时间，不连接真实 3x-ui 或 Telegram。
包括五分钟边界、恢复不重复、未知状态、读取游标、IPv6、日志截断、访问排行、
鉴权、凭据加密、接口版本回退、Telegram 限流和消息顺序。

前端包含桌面和窄屏布局。仓库提供 GitHub Actions 配置用于运行后端测试和前端构建。
`requirements.lock.txt` 记录本次本地验证环境，`frontend/pnpm-lock.yaml` 锁定前端依赖。
本次完整验证与限制见 [开发记录](docs/DEVELOPMENT.md)。

## 目录

```text
app/            API、状态机、日志读取、适配器、SQLite 与后台任务
frontend/       Vue 3 + TypeScript + Vite 管理页面
tests/          离线自动化测试
docs/           开发和验证记录
.github/        持续集成配置
compose.yaml    本地构建和运行示例
start-local.ps1 Windows 本地启动
```

## 上游参考与许可证

本项目是独立实现，与 3x-ui、Xray 或 Telegram 无隶属关系。

- [3x-ui 在线列表前端调用](https://github.com/MHSanaei/3x-ui/blob/main/frontend/src/pages/inbounds/useInbounds.ts)
- [3x-ui 登录与 CSRF](https://github.com/MHSanaei/3x-ui/blob/main/internal/web/controller/index.go)
- [Xray 访问日志结构](https://github.com/XTLS/Xray-core/blob/main/common/log/access.go)
- [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage)

代码使用 [MIT License](LICENSE)。依赖库保留各自许可证。上传 GitHub 前请自行确定
仓库名、作者署名和许可证是否符合自己的发布计划。
