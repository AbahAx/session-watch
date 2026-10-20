# 本机免密接入

新安装优先使用 [Linux 一键部署说明](../docs/LINUX_INSTALL.md)，自动生成独立服务和隔离目录。
下方手动配置示例用于理解只读接入或维护原有手工安装，不要同时创建重复服务。

v0.6.0 的安装器生成 `source_agent.py` 服务，持续发现原生与 Docker 3x-ui，并内置只读日志镜像。
它不因安装时没有日志而退出；日志之后启用、文件之后生成、配置路径变化及容器重建后会重新检测。
页面支持自动/手动路径和实例选择；Web 只写独立请求目录，root 采集器验证请求范围并只输出无密钥快照。
Docker 数据库需持久化 bind mount/volume，日志支持挂载与容器内部路径；见 Linux 部署说明。

以下 `local_collector.py` 手动示例用于旧版部署维护。
Web 管理服务使用独立的非 root 账号。`local_collector.py` 在单独服务中只读
3x-ui 数据库和运行中的 Xray 配置，调用已有的 `statsgetallonlineusers` 查询。
不登录 3x-ui，不读取管理员密码，不修改数据库、配置、流量计数或源文件权限。

默认探测 `/etc/x-ui/x-ui.db`、`/usr/local/x-ui/bin/config.json` 和同目录的
`xray-linux-amd64` / `xray-linux-arm64`。其他安装位置用 `--database`、`--config`、
`--binary` 显式指定，并同步修改服务文件中的只读目录。

安装为 systemd 服务之前，在本项目代码已安装到 `/opt/xray-session-notify`、
Web 服务账号 `session-watch` 已创建的情况下：

```bash
install -d -o root -g session-watch -m 0750 /var/lib/session-watch-local
install -m 0644 deploy/session-watch-local.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now session-watch-local.service
```

Web 服务需有 `/var/lib/session-watch-local` 的只读权限，配置的快照路径使用
`/var/lib/session-watch-local/snapshot.json`。不要把原面板目录挂载给 Web 服务。
快照每 10 秒更新，原子替换，包含客户端 email、启用标记、关联入站、在线 email
列表与采样时间。不导出 UUID、代理密码、订阅 ID 或面板凭据。

核心可能返回 `user>>>客户端名>>>online` 这样的统计名称；采集器将其规范为
清单中的客户端标识，Web 端再次校验。无法唯一对应的标识将本轮状态记为未知，
不会创建新客户端，也不会当成成功的空在线列表。

新版本 `clients` / `client_inbounds` 表和旧版本入站 settings 中的客户端均可读取；
禁用和未关联入站的客户端也会导入。新增、删除和启用状态随清单同步，删除仅标记
来源已移除，保留本项目历史。自定义备注和监控开关不被覆盖。支持有 WAL 的运行中
SQLite 数据库，不使用会忽略 WAL 的 immutable 模式。

必须已有 Xray StatsService、本机 loopback API 入站、`stats` 和在线统计 policy，
且核心支持 `statsgetallonlineusers`。缺少任一条件会显示异常，不自动修改 3x-ui
去启用它，也不把失败当成“全部离线”。快照超时同样视为未知，取消离线确认计时。
此方式只覆盖本机 Xray；其他节点或 TUIC 等独立核心不由这个 Xray 接口代表。
远程面板仍可在页面选择 API 接入，使用原有账号密码方式。

访问日志仍须由管理员预先开启；root-only 日志可使用 `log_bridge.py`。
Docker 部署时，采集器运行在宿主机，向容器只读挂载快照目录和日志镜像目录；
不要挂载面板数据库、Xray 控制接口或 Docker socket。安装示例中路径需按实际环境调整。

初始不会启动活动统计或 Telegram 发送。在“日志来源”检查配置并启动监控；
通知中心可另行开启实际发送。暂停活动统计时仍同步客户端清单。

