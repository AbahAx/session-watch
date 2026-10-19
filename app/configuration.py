from pathlib import Path
import os

from app.integrations import IntegrationError, PanelClient
from app.local_source import read_snapshot, online_set

SOURCE_FIELDS = {'source_type', 'local_snapshot_path', 'panel_url', 'panel_username',
                 'panel_password', 'panel_totp', 'online_api', 'log_path', 'log_timezone'}


async def inspect_source(settings):
    result = {'ok': False, 'message': '', 'online_count': 0, 'client_count': 0, 'log_readable': False,
              'checks': {'inventory': False, 'online': False, 'log': False}}
    try:
        path = Path(settings.log_path)
        if not path.is_file():
            raise OSError()
        with path.open('rb') as stream:
            stream.read(1)
        heartbeat = os.environ.get('SESSION_WATCH_BRIDGE_HEARTBEAT')
        if heartbeat and path == Path(heartbeat).parent / 'access.log':
            import time
            if time.time() - Path(heartbeat).stat().st_mtime > 20:
                raise OSError()
        result['log_readable'] = result['checks']['log'] = True
    except OSError:
        result['message'] = '日志不可读或日志桥接已停止，请检查日志路径和采集服务'
    panel = PanelClient()
    try:
        if settings.source_type == 'local':
            data = read_snapshot(settings)
            result['checks']['inventory'] = data['clients_ok']
            result['client_count'] = len(data['clients']) if data['clients_ok'] else 0
            online = online_set(data)
        else:
            online = await panel.onlines(settings)
            result['checks']['inventory'] = True
        result['checks']['online'] = True
        result['online_count'] = len(online)
        result['ok'] = all(result['checks'].values())
        if result['ok']:
            result['message'] = (f"本机读取正常：{result['client_count']} 个客户端，{len(online)} 个在线，日志可读"
                                 if settings.source_type == 'local' else '远程在线查询和日志读取均正常')
    except IntegrationError as exc:
        result['message'] = str(exc)
    finally:
        await panel.close()
    return result
