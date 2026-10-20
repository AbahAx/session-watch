# Linux 一键部署（v0.6.0）

这是独立的 Session Watch 安装包。服务器不需要 Node、npm 或 Docker。
同机模式只读本机已有 3x-ui 数据库、Xray 在线统计接口和访问日志；不需要面板账号密码。
安装器不会修改原面板配置、数据库、账号、日志权限，不会重启 3x-ui/Xray。

## 兼容范围

| 系统/环境 | 安装方式 | 验证状态 |
| --- | --- | --- |
| Debian 12，x86_64，Python 3.11 | 自动安装依赖，一键部署 | 已实机验证安装、读取来源、升级、回滚、卸载 |
| Debian 13，Python 3.13 | 自动安装依赖 | 已适配，未实机验证 |
| Ubuntu 24.04 LTS，Python 3.12 | 自动安装依赖 | 已适配，未实机验证 |
| RHEL / Rocky / AlmaLinux 9，仓库提供 Python 3.11 | 自动安装依赖，使用独立 python3.11 | 已适配，未实机验证；RHEL 从 9.2 起提供该软件包 |
| aarch64 / ARM64 | 同一 Linux 安装包，pip 选择对应平台 wheel | 已适配，未实机验证 |
| 其他 glibc + systemd 发行版 | 自备 Python 3.11–3.13、venv、sqlite3、pip、useradd/groupadd/runuser，使用 `--no-install-deps` | 条件兼容，不承诺自动安装 |
| Alpine、OpenWrt、非 systemd、32 位 Linux | 此安装器不支持 | 不支持自动部署 |
| Docker | 不使用本安装器；源码包另有 Dockerfile/Compose，不能用精简 Linux 包构建 | 当前没有完成容器构建/运行验证 |

基础条件：root/sudo、systemd 正在运行、Python **3.11–3.13**、glibc、x86_64 或 ARM64。
建议至少 1 GB 内存、500 MB 可用磁盘；升级保留旧版本和备份，需要额外空间。
默认需要访问发行版软件仓库和 PyPI。所有 Python 包安装在项目专用 venv，使用固定运行依赖，
只下载 wheel；没有匹配 wheel 时停止安装，不在小内存 VPS 上临时编译。
Python 3.14 尚未纳入本版本支持范围。

依据：[Debian 12 Python 3.11](https://packages.debian.org/bookworm/python/python3.11)、
[Debian 13 Python 3.13 venv](https://packages.debian.org/stable/python3.13-venv)、
[Ubuntu 24.04 Python 3.12 venv](https://packages.ubuntu.com/noble-updates/python3.12-venv)、
[RHEL 9 Python 版本说明](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html-single/installing_and_using_dynamic_programming_languages/index)。
软件包可获得不代表项目已在该系统实机通过测试，以上两者分别标注。

## 1. 上传并安装

将 `session-watch-v0.6.0-linux.tar.gz` 和 `SHA256SUMS` 上传到新服务器的同一目录。
官方源码仓库为 [AbahAx/session-watch](https://github.com/AbahAx/session-watch)。
安装包从 [Releases](https://github.com/AbahAx/session-watch/releases) 下载并核对校验值；
如果尚无正式版本，请使用已下载的本地安装包，不要使用网上猜测的安装链接。

```bash
sha256sum --check --ignore-missing SHA256SUMS
tar -xzf session-watch-v0.6.0-linux.tar.gz
cd session-watch
sudo bash install.sh
```

在 SSH 终端执行 `sudo bash install.sh` 或下方在线命令，会出现交互向导：

1. 选择本机访问（SSH 隧道），或域名 + 端口的 HTTPS 访问。
2. 设置面板端口，范围 1024–65535。本机默认 8765，域名 HTTPS 默认 8443。
3. 域名模式输入已解析的域名，例如 `watch.example.com`，以及现有证书/私钥路径。
   自动查找 `/root/cert/域名`、Let’s Encrypt 和 acme.sh 的常见证书位置，确认路径后只读使用。
4. 设置管理员用户名（默认 `admin`）和密码（12–256 个字符），密码隐藏输入并要求确认。
   密码留空可生成随机密码。自选密码不会写入初始账号文件或安装日志。

访问地址例如 `https://watch.example.com:8443`，端口请在服务器防火墙和云安全组放行。
域名需指向本机，证书需匹配域名；本版本复用现有证书，不自动申请证书。

非交互安装使用 `--non-interactive`，无真实终端时也自动采用非交互模式。
默认仅监听 `127.0.0.1:8765`，管理员 `admin`，生成随机密码。
初始随机密码保存在服务器 root 专用文件，安装日志不打印密码：

```bash
sudo cat /etc/session-watch/initial-admin.txt
```

自动化部署可通过 `--admin-username NAME --admin-password-file /root/watch-password`
设置初始凭据；密码文件必须 root 所有、权限 600（或更严格），只包含一行密码。
参数中不直接传明文密码。已有管理员时拒绝这些初始化参数，升级保留原账号。
`--interactive` 可显式要求真实终端；`--dry-run` 始终不询问密码。

登录后进入“系统设置 → 管理员账号”，验证当前密码即可修改用户名；
新密码留空表示只改用户名。保存后当前浏览器保持登录，其他会话失效。

在自己电脑上建立 SSH 隧道（替换 `YOUR_SERVER`，非 22 端口增加 `-p SSH端口`）：

```bash
ssh -L 8765:127.0.0.1:8765 root@YOUR_SERVER
```

选择本机访问且使用默认端口时，浏览器打开 `http://127.0.0.1:8765`；自定义端口时相应替换隧道端口。
已经改过密码的实例继续使用当前密码，初始密码文件不会跟随页面修改更新。

新安装的 **监控和 Telegram 推送均关闭**。在“节点接入”查看来源健康状态，
确认客户端和入站正确、日志时区正确，再配置 Telegram 并启用监控/推送。
默认首次日志接入从文件末尾开始，不读取旧访问记录。

## 像 3x-ui 一样在线安装

在 root SSH 终端运行以下命令，下载正式最新版并进入安装向导。
发布者需推送与 `app/__init__.py` 一致的版本标签；Actions 构建并生成草稿 Release，
检查附件后发布为正式版。分支仓库维护者将下方两处仓库名称替换为自己的仓库。

```bash
bash <(curl -fsSL https://github.com/AbahAx/session-watch/releases/latest/download/online-install.sh) --repo AbahAx/session-watch
```

入口不依赖 Git、jq、Node 或 gh。需要下载入口时有 curl，目标机需要能通过 HTTPS
访问 GitHub API、GitHub Release 下载地址和软件源；连不上时停止，不自动切换第三方镜像。
缺少 Python 时先通过官方发行版仓库准备下载器所需的 Python，随后由本机安装器准备应用依赖。

支持指定版本、HTTPS 参数、额外实例或升级：

```bash
# 安装指定正式版本
bash online-install.sh --repo AbahAx/session-watch --version v0.6.0 install

# 安装并直接提供公网 HTTPS；替换示例域名/现有证书路径
bash online-install.sh --repo AbahAx/session-watch install \
  --non-interactive --host 0.0.0.0 --port 8443 --domain watch.example.com \
  --cert /etc/ssl/watch/fullchain.pem --key /etc/ssl/watch/privkey.pem

# 下载最新版并升级由本安装器创建的默认实例
bash online-install.sh --repo AbahAx/session-watch upgrade
```

脚本先从 GitHub API 获取正式版本，再把所有安装包下载固定在**同一个版本标签**，
避免安装过程中 latest 改变导致混用文件。下载后校验 SHA-256，拒绝危险压缩包条目，
再调用已有安装器；安装器还会检查逐文件清单并处理服务、备份及失败恢复。
在线入口不提供不联网的 `--dry-run`；只读预检查请下载解压后执行本机安装器的 `--dry-run`。
SHA-256 用于校验一致性；使用前仍需信任脚本所在的仓库及发布者。

GitHub 的固定最新附件链接格式见
[GitHub 官方说明](https://docs.github.com/en/repositories/releasing-projects-on-github/linking-to-releases)。
只有公开发布的正式 Release 能供这个匿名入口使用，草稿不会成为可下载的 latest。

## 2. 直接使用域名 HTTPS

准备指向这台服务器的域名，以及该域名**已有的有效证书和私钥**。
下面全部是示例地址，需替换成自己的值：

```bash
sudo bash install.sh install \
  --host 0.0.0.0 --port 8443 \
  --domain watch.example.com \
  --cert /etc/ssl/watch/fullchain.pem \
  --key /etc/ssl/watch/privkey.pem
```

访问 `https://watch.example.com:8443`。安装器不自动申请证书、不接管 443、
不修改防火墙/云安全组/SELinux；公网访问需要自行放行选定的 TCP 端口。
公开监听必须提供域名和证书，禁止直接用 HTTP 对外暴露登录页面。
证书通过 systemd `LoadCredential` 交给 Web 服务，原文件权限保持不变；
证书文件更新时仅重启本项目 Web 服务。证书链、域名匹配和续期由证书管理工具负责。

## 3. 自动检测及手动指定来源

从 v0.6.0 起，独立 source-agent 持续发现来源，不再要求安装时日志文件已经存在。
默认每约 5 秒重新检测：原生 Xray 实际进程、当前配置、运行目录，以及本机 Docker 容器、
bind mount、named volume、容器内文件。仅安装在 Docker 宿主机上的本项目 Linux 服务支持此自动模式；
本项目 Web 容器自身不需要、也不应挂载 Docker socket。

“节点接入”可选择“自动检测”或“手动指定”。自动模式下，日志后开启、文件后生成或路径变化后自行接入；
手动模式固定所填路径，不会被自动检测覆盖。本机使用绝对路径；Docker 可填所选容器内绝对路径，
或 `docker://容器名/绝对路径`。手动文件位于该核心的运行/日志目录，其他宿主机目录由 root 使用 `--log` 授权。

发现多个 3x-ui 时，在页面“采集实例”选择一个；程序不猜测、不合并不同节点的数据。
Docker 目标按容器名跟踪，重建同名容器后重新解析 PID、根目录与挂载。
Docker 客户端数据库需要 bind mount 或 named volume（官方 `/etc/x-ui` 持久卷布局受支持）；
日志可在挂载目录中，也可仅存在容器内部。自动模式不会自行开启 Xray 日志或统计接口。
标准输出/标准错误未配置文件时会提示原因，不猜测 Docker 控制台混合输出为访问日志。

采集器使用 Docker GET 查询容器信息；在线查询仅进入目标网络 namespace，执行本项目自己的固定查询程序，
调用已有只读 `GetAllOnlineUsers`。不执行容器程序、不使用 Docker exec、不修改或重启原容器。
Web 继续为无特权用户，只访问快照和日志副本；独立采集服务有只读源访问及 network namespace 所需能力。
自动模式限同一 Linux 宿主机、普通 rootful Docker；远程 Docker、Windows Docker Desktop、rootless Docker 暂不承诺。

原生安装位置异常时，仍可显式指定来源路径：

```bash
sudo bash install.sh install \
  --database /etc/x-ui/x-ui.db \
  --config /usr/local/x-ui/bin/config.json \
  --binary /usr/local/x-ui/bin/xray-linux-amd64 \
  --log /var/log/x-ui/access.log \
  --log-timezone UTC
```

日志时区默认取 Linux 系统时区；展示时区默认 Asia/Shanghai。必须与日志中的时间含义一致，
不能把服务器 UTC 日志按北京时间解析。可在页面修改。

原系统 `access: none`、日志未写入文件，或未开启/不支持在线用户统计时，
安装器只提示缺失条件，不自动开启原系统功能。没有日志就没有访问目标/IP明细；
没有可靠的在线状态就不能完成所需上线/下线判断。页面能运行不等于来源已全部就绪。
核心现有配置需包含 StatsService、stats、statsUserOnline 和本机回环 API。

采集进程作为独立 root 服务运行，以 systemd 将源文件所在系统置为只读，只向本项目
脱敏快照/日志镜像目录写入。Web 进程为无登录权限的独立账号，原数据库、配置和日志被隔离。
如果源文件放在 `/root` 或用户 home，服务的 `ProtectHome` 会阻止读取；
请使用正常系统级来源路径，不要为此取消隔离或放宽原始文件权限。

容器里的 3x-ui 不保证自动发现。需要已有的主机侧文件/二进制/API，
或安装时使用 `--no-collector`，之后在页面配置已有的面板只读查询接入。
不要为了自动发现而挂载 Docker socket。

## 4. 预检查、自定义 Python 和离线依赖

```bash
# 不安装软件包、不创建账号、不写文件、不启动服务
sudo bash install.sh install --dry-run

# 自备受支持的 Python，不更换系统 Python、不增加第三方系统软件源
sudo env SESSION_WATCH_PYTHON=/usr/bin/python3.12 \
  bash install.sh install --no-install-deps

# 在相同 Python/架构/兼容 glibc 的联网 Linux 上准备 wheelhouse
python3 -m pip download --only-binary=:all: \
  -r requirements-linux.lock.txt --dest wheelhouse

# 目标机已预装系统依赖，将 wheelhouse 一同上传后安装
sudo bash install.sh install --no-install-deps --wheel-dir /root/wheelhouse
```

压缩包包含前后端代码，不包含所有架构的 Python wheels。
`--wheel-dir` 只解决 Python 包离线安装；系统依赖仍需提前准备。
`--dry-run` 检查文件校验、来源路径、平台和端口计划，不代替真正启动后的健康检查。

## 5. 日常管理、升级和回滚

```bash
sudo session-watch status
sudo session-watch doctor
sudo journalctl -u session-watch -n 80 --no-pager
sudo journalctl -u session-watch-local -n 80 --no-pager
sudo journalctl -u session-watch-bridge -n 80 --no-pager
sudo session-watch backup
```

把**新版本**解压到另一个空目录，校验后运行新包里的脚本：

```bash
cd /root/new-release/session-watch
sudo bash install.sh upgrade

# 或用已安装的管理命令指定干净的新包目录
sudo session-watch upgrade --package-dir /root/new-release/session-watch --no-install-deps
```

升级先准备新版本和 venv，依赖成功后才暂停本项目服务；
再用 SQLite 备份接口保存一致的数据副本及加密密钥、服务配置，切换到新版本并检查 Web 健康。
升级会保留管理员、Telegram、监控开关和页面配置；不会重置密码，也不自动补发测试消息。
新版本无法启动时自动恢复之前的代码、数据和服务配置。
备份失败时保持原版本运行；下载/依赖安装失败时原版本继续运行。

```bash
# 回到最近的可用备份；回滚前再次备份当前数据
sudo session-watch rollback

# 指定备份目录名（替换示例）
sudo session-watch rollback --backup-id 20261019-120000-abcdef
```

回滚会恢复该备份时间点的本项目数据库，之后的新活动保留在回滚前生成的备份中。
旧代码目录和备份不会自动清理。备份目录权限为 700，包含数据库、密钥和初始凭据，
不要上传 GitHub。建议定期把备份加密保存到其他机器。
本版本提供同一实例的备份恢复，不提供跨机器的一键导入功能。

## 6. 多实例和已有旧安装

```bash
sudo bash install.sh install --instance test --port 18765
sudo session-watch-test status
sudo bash install.sh upgrade --instance test
sudo session-watch-test uninstall --purge
```

`--instance` 是 1–12 位小写字母、数字或连字符，以字母开头。
每个实例有自己的账号、端口、数据、服务和配置。
检测到未由本安装器创建的同名服务、账号或目录时会拒绝覆盖。
原手工安装的 `/opt/xray-session-notify` 不会被静默接管，
应另起实例验证；旧实例的迁移需单独设计，不能通过覆盖目录处理。

## 7. 卸载

```bash
# 删除程序和本项目服务，保留数据、密钥、账号和备份；可通过安装包重装
sudo session-watch uninstall

# 同时删除本项目数据、密钥、初始凭据和独立账号；备份仍保留
sudo session-watch uninstall --purge
```

卸载仅处理带本项目所有权标记的固定目录和固定服务名，拒绝符号链接或挂载点根目录。
不会删除 3x-ui，也不会撤销用户原有的证书、系统依赖或防火墙规则。
`--purge` 是明确的数据删除操作；不要在需要保留活动记录时使用。
卸载后旧版本程序也被删除，数据备份仍在；需要恢复时应重新准备对应版本安装包。
保留数据卸载后如需彻底清理，可在解压的安装包目录执行
`sudo bash install.sh uninstall --purge`；非默认实例另加 `--instance 名称`。

## 目录

| 默认路径 | 用途 |
| --- | --- |
| `/opt/session-watch/releases/` | 每个版本的只读代码与独立 venv |
| `/opt/session-watch/current` | 当前版本的原子切换链接 |
| `/etc/session-watch/` | 所有权标记、服务环境、初始管理员凭据；root 专用 |
| `/var/lib/session-watch/` | 本项目 SQLite 和加密密钥；服务账号专用 |
| `/var/lib/session-watch-local/` | 脱敏客户端清单、在线快照 |
| `/var/lib/session-watch-bridge/` | 有界日志镜像、读取游标和心跳 |
| `/var/backups/session-watch/` | 私密备份，root 专用 |
| `/usr/local/bin/session-watch` | 管理命令 |

## 开源打包

在开发机先安装前端依赖并构建，再执行：

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm build
cd ..
python deploy/build_release.py
```

输出 `dist/session-watch-v版本-linux.tar.gz`、`dist/session-watch-v版本-source.zip`
和 `dist/SHA256SUMS`，另输出单文件在线入口 `dist/online-install.sh`。
安装包包含构建前端；源码包额外包含前端源码和测试。
发布使用正向文件白名单，排除 data、secret.key、venv、node_modules、缓存以及仓库外的部署资料；
每个包包含逐文件 SHA-256 清单。SHA-256 用于检测损坏/不一致，不代替可信下载渠道或签名。
修改包内文件后校验会失败，应从源码重新构建包。

GitHub Release 工作流见 `.github/workflows/release.yml`。创建仓库、公开资料检查完成后，
推送与 `app/__init__.py` 版本一致的 `vX.Y.Z` 标签才会生成**草稿** Release，不会在本地打包时自动发布。
