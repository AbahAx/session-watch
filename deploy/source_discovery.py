"""Continuously discover local sources. Docker API access is GET-only."""
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import subprocess
import sys

from deploy.local_collector import api_address, inventory
from app.identity import normalize_online_names


class DockerConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=3)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def docker_get(path, endpoint):
    if not re.fullmatch(r'/containers/(?:json|[a-f0-9]{12,64}/json)', endpoint):
        raise ValueError('Unsupported read-only Docker query')
    connection = DockerConnection(path)
    try:
        connection.request('GET', endpoint)
        response = connection.getresponse()
        data = response.read(4 * 1024 * 1024 + 1)
        if response.status != 200 or len(data) > 4 * 1024 * 1024:
            raise ValueError('Docker discovery unavailable')
        return json.loads(data)
    finally:
        connection.close()


def safe_path(value):
    if not isinstance(value, str) or not value.startswith('/') or any(c in value for c in '\0\r\n'):
        raise ValueError('请输入绝对路径')
    if '..' in PurePosixPath(value).parts:
        raise ValueError('路径不能包含 ..')
    return value


def mounted_path(container, path):
    """Longest destination wins, including nested file and volume mounts."""
    path = PurePosixPath(safe_path(path))
    mounts = sorted(container.get('Mounts', []), key=lambda m: len(m.get('Destination', '')), reverse=True)
    for mount in mounts:
        destination = PurePosixPath(mount.get('Destination', '/nonexistent'))
        if mount.get('Type') in ('bind', 'volume') and path.is_relative_to(destination):
            return Path(safe_path(mount['Source'])).joinpath(*path.relative_to(destination).parts)
    return None


def container_path(container, path):
    path = safe_path(path)
    pid = container.get('State', {}).get('Pid', 0)
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError('容器未运行')
    root = Path('/proc') / str(pid) / 'root'
    # Interpret absolute symlinks inside the container's root, never the host's.
    # Walk one component at a time so intermediate symlinks cannot escape.
    for _ in range(20):
        parts = PurePosixPath(path).parts[1:]
        for index in range(len(parts)):
            prefix = '/' + '/'.join(parts[:index + 1])
            candidate = mounted_path(container, prefix)
            candidate = candidate if candidate is not None else root / prefix.lstrip('/')
            if candidate.is_symlink():
                target = os.readlink(candidate)
                joined = PurePosixPath(target) if target.startswith('/') else PurePosixPath(prefix).parent / target
                import posixpath
                path = posixpath.normpath(str(joined.joinpath(*parts[index + 1:])))
                break
        else:
            mapped = mounted_path(container, path)
            return mapped if mapped is not None else root / path.lstrip('/')
    raise ValueError('容器路径符号链接过多')


def processes(proc_root=Path('/proc')):
    result = []
    if not proc_root.is_dir():
        return result
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            executable = Path(os.readlink(proc / 'exe').removesuffix(' (deleted)'))
            if not re.fullmatch(r'xray(?:-linux-(?:amd64|arm64))?', executable.name):
                continue
            args = (proc / 'cmdline').read_bytes().decode(errors='replace').strip('\0').split('\0')
            root_stat, host_stat = (proc / 'root').stat(), Path('/').stat()
            uid = int(next(line.split()[1] for line in (proc / 'status').read_text().splitlines() if line.startswith('Uid:')))
            result.append({'pid': int(proc.name), 'exe': executable, 'cwd': Path(os.readlink(proc / 'cwd')),
                           'uid': uid,
                           'native': (root_stat.st_dev, root_stat.st_ino) == (host_stat.st_dev, host_stat.st_ino),
                           'mount': os.readlink(proc / 'ns/mnt'), 'args': args})
        except (OSError, ValueError, StopIteration):
            continue
    return result


def core_config(process, default):
    args = process.get('args', [])
    for i, value in enumerate(args):
        if value in ('-config', '--config', '-c') and i + 1 < len(args):
            path = Path(args[i + 1])
            return str(path if path.is_absolute() else process['cwd'] / path)
        if value.startswith(('-config=', '--config=')):
            path = Path(value.split('=', 1)[1])
            return str(path if path.is_absolute() else process['cwd'] / path)
    return default


def candidates(hints, proc=None, docker=None):
    proc = processes() if proc is None else proc
    if docker is None:
        docker = []
        for sock in ('/run/docker.sock', '/var/run/docker.sock'):
            if Path(sock).exists():
                try:
                    for item in docker_get(sock, '/containers/json')[:100]:
                        identifier = item.get('Id', '')
                        if re.fullmatch(r'[a-f0-9]{12,64}', identifier):
                            docker.append(docker_get(sock, '/containers/' + identifier + '/json'))
                except (OSError, ValueError, http.client.HTTPException):
                    pass
                break
    result, container_mounts = [], set()
    for item in docker:
        try:
            pid = item.get('State', {}).get('Pid', 0)
            if not item.get('State', {}).get('Running') or not isinstance(pid, int) or pid <= 0:
                continue
            mount = os.readlink(Path('/proc') / str(pid) / 'ns/mnt')
            container_mounts.add(mount)
            cores = [p for p in proc if p['mount'] == mount]
            # Containers without an active Xray may still have an initialized DB.
            db = next((mounted_path(item, p) for p in ('/etc/x-ui/x-ui.db', '/usr/local/x-ui/db/x-ui.db')
                       if mounted_path(item, p) is not None and mounted_path(item, p).is_file()), None)
            if not cores and db is None:
                continue
            if len(cores) > 1:
                continue
            process = cores[0] if cores else None
            config = '/usr/local/x-ui/bin/config.json'
            cwd = '/usr/local/x-ui'
            if process:
                # Host /proc cwd strings show the container's own absolute path.
                cwd = str(process['cwd'])
                config = core_config(process, config)
            name = item.get('Name', '').lstrip('/')
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name):
                continue
            result.append({'id': 'docker:' + name, 'label': 'Docker · ' + name, 'kind': 'docker',
                           'container': item, 'database': db, 'config': container_path(item, config),
                           'cwd': cwd, 'process': process, 'binary': None})
        except (OSError, ValueError, KeyError):
            continue
    native = [p for p in proc if p['mount'] not in container_mounts and p.get('native', True) and p.get('uid', 0) == 0]
    if len(native) > 1:
        result.append({'id': 'native', 'label': '原生 · 多个 Xray 核心', 'kind': 'native',
                       'error': '发现多个原生 Xray 核心；请由管理员明确配置目标核心，避免混用日志'})
    # Never silently combine two cores/instances into one client's sessions.
    if len(native) <= 1:
        db = next((Path(p) for p in [hints.get('database'), '/etc/x-ui/x-ui.db', '/usr/local/x-ui/db/x-ui.db'] if p and Path(p).is_file()), None)
        if db is not None:
            process = native[0] if native else None
            config = core_config(process, hints.get('config') or '/usr/local/x-ui/bin/config.json') if process else hints.get('config') or '/usr/local/x-ui/bin/config.json'
            result.append({'id': 'native', 'label': '原生 3x-ui', 'kind': 'native', 'database': db,
                           'config': Path(config), 'cwd': str(process['cwd']) if process else '',
                           'process': process, 'binary': process['exe'] if process else Path(hints.get('binary') or '/usr/local/x-ui/bin/xray-linux-amd64')})
    return result


def select_source(choices, target):
    if target == 'auto':
        if len(choices) != 1:
            raise ValueError('发现多个 3x-ui，请选择采集实例' if choices else '尚未发现运行中的本机 3x-ui；将持续重试')
        found = choices[0]
    else:
        found = next((c for c in choices if c['id'] == target), None)
    if found is None:
        raise ValueError('指定实例尚未运行或已移除；将持续重试')
    if found.get('error'):
        raise ValueError(found['error'])
    return found


def log_source(source, preference, hints):
    config = json.loads(source['config'].read_text())
    raw = config.get('log', {}).get('access', '')
    configured = ''
    if raw and raw not in ('none', 'stdout', 'stderr'):
        configured = str(PurePosixPath(raw) if raw.startswith('/') else PurePosixPath(source['cwd']) / raw) if source['cwd'] or raw.startswith('/') else ''
    if preference.get('log_mode') == 'manual':
        value = preference.get('log_path', '')
        if value.startswith('docker://'):
            match = re.fullmatch(r'docker://([A-Za-z0-9_.-]+)(/.*)', value)
            if not match or source['id'] != 'docker:' + match[1]:
                raise ValueError('Docker 路径所属容器与所选实例不一致')
            value = match[2]
        safe_path(value)
        # Web inputs cannot turn the privileged collector into an arbitrary
        # root-file reader. Manual logs stay in this core's known log locations.
        roots = [PurePosixPath(configured).parent] if configured else []
        if source['cwd'] and source['cwd'] != '/':
            roots.append(PurePosixPath(source['cwd']))
        roots.append(PurePosixPath('/var/log/x-ui'))
        trusted = hints.get('log', '') if source['kind'] == 'native' else ''
        if value != configured and value != trusted and (not value.endswith('.log') or not any(PurePosixPath(value).is_relative_to(root) and str(root) != '/' for root in roots)):
            raise ValueError('手动路径需位于该 Xray 的运行目录或日志目录；其他宿主机路径请由管理员使用 --log 指定')
        selected = value
    else:
        selected = configured
        if not selected:
            if raw in ('stdout', 'stderr'):
                raise ValueError('访问日志输出到标准流，尚无文件可读；请在 3x-ui 设置访问日志文件')
            raise ValueError('Xray 尚未启用文件访问日志；开启后会自动重新检测')
    safe_path(selected)
    path = container_path(source['container'], selected) if source['kind'] == 'docker' else Path(selected)
    # Resolve a symlink only if it stays within its approved source directory.
    # Container paths through /proc use the core's own root, not the web root.
    if preference.get('log_mode') == 'manual' and selected != configured and selected != hints.get('log'):
        approved = [container_path(source['container'], str(r)) if source['kind'] == 'docker' else Path(str(r)) for r in roots if str(r) != '/']
        if not any(path.resolve().is_relative_to(root.resolve()) for root in approved):
            raise ValueError('手动日志路径的符号链接超出允许目录')
    if not path.is_file():
        raise ValueError('访问日志已配置但文件尚未生成；将自动等待，无需重新安装')
    with path.open('rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('访问日志必须是普通文件')
        stream.read(1)
    return path, selected


def collect_source(source):
    import time
    import sqlite3
    data = dict(version=1, generated_at=time.time(), clients_ok=False, online_ok=False, clients=[], online=[])
    try:
        if source['database'] is None:
            raise ValueError('客户端数据库需使用 Docker bind mount 或 volume 持久化')
        data['clients'] = inventory(source['database'])
        data['clients_ok'] = True
        address = api_address(json.loads(source['config'].read_text()))
        if source['kind'] == 'docker':
            process = source['process']
            if process is None:
                raise ValueError('Xray not running')
            # Execute only our trusted Python worker in the network namespace.
            # Never execute a container binary with host root privileges.
            command = ['nsenter', '--target', str(process['pid']), '--net', '--', sys.executable,
                       '-m', 'deploy.stats_query', address]
            proc = subprocess.run(command, capture_output=True, timeout=5, check=True)
            value = json.loads(proc.stdout)
        else:
            from deploy.stats_query import users
            value = {'users': users(address)}
        if not isinstance(value, dict) or set(value) - {'users'} or not isinstance(value.get('users', []), list):
            raise ValueError('Unexpected API response')
        data['online'] = sorted(normalize_online_names(value.get('users', []), (c['email'] for c in data['clients'])))
        data['online_ok'] = True
    except Exception:
        data['online_error'] = '在线查询不可用；请检查已有统计接口、核心运行状态及 Docker nsenter 权限'
    data['generated_at'] = time.time()
    return data
