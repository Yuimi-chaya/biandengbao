"""Read existing App SSH metadata; turns still use the desktop's IPC owner."""
import base64
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from .catalog import CatalogError


class RemoteUnavailable(RuntimeError):
    pass


def ssh_read(alias, source, timeout=18):
    if not alias or alias.startswith('-') or any(c.isspace() for c in alias):
        raise RemoteUnavailable('SSH 别名无效')
    command = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ClearAllForwardings=yes',
               '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'UpdateHostKeys=no', alias, 'python3 -']
    try:
        result = subprocess.run(command, input=source, text=True, encoding='utf-8', capture_output=True, timeout=timeout)
        if result.returncode:
            raise RemoteUnavailable('SSH 读取失败，请检查电脑上该主机的 SSH 连接')
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise RemoteUnavailable('SSH 主机暂不可用，请检查电脑上的连接') from exc


def payload(value):
    data = base64.b64encode(json.dumps(value).encode()).decode()
    return 'json.loads(__import__("base64").b64decode(' + repr(data) + '))'


class RemoteStore:
    def __init__(self, alias):
        self.alias = alias
        self.home = None  # Never map remote paths onto the local filesystem.
        self.cache = {}
        self.lock = threading.Lock()

    def call(self, method, args):
        source = Path(__file__).with_name('store.py').read_text(encoding='utf-8')
        return ssh_read(self.alias, source + '\nimport os\ns = SessionStore(Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex"))))\n' +
                        'print(json.dumps(s.' + method + '(**' + payload(args) + '), ensure_ascii=False))\n', timeout=30)

    def list(self, **kwargs):
        key = json.dumps(kwargs, sort_keys=True)
        with self.lock:
            previous = self.cache.get(key)
            if previous and time.monotonic() - previous[0] < 15:
                return [dict(row) for row in previous[1]]
            rows = self.call('list', kwargs)
            self.cache[key] = (time.monotonic(), rows)
            return rows

    def get(self, thread_id):
        return self.call('get', {'thread_id': thread_id})

    def history(self, thread_id):
        return self.call('history', {'thread_id': thread_id})

    def usage(self, thread_id):
        return self.call('usage', {'thread_id': thread_id})


class RemoteCatalog:
    def __init__(self, alias):
        self.alias = alias
        self.cache = {}
        self.lock = threading.Lock()

    def get(self, cwd, refresh=False):
        with self.lock:
            previous = self.cache.get(cwd)
            if previous and not refresh and time.monotonic() - previous[0] < 300:
                return previous[1]
            source = Path(__file__).with_name('catalog.py').read_text(encoding='utf-8')
            source += '\nimport shutil\nhome=Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex")))\n'
            source += 'runtime=shutil.which("codex") or str(Path.home()/".local/bin/codex")\n'
            source += 'print(json.dumps(Catalog(home, runtime).get(' + payload(cwd) + '), ensure_ascii=False))\n'
            try:
                result = ssh_read(self.alias, source, timeout=60)
            except RemoteUnavailable as exc:
                raise CatalogError(str(exc)) from exc
            self.cache[cwd] = (time.monotonic(), result)
            return result


class AppHosts:
    def __init__(self, home):
        self.home = Path(home)

    def state(self):
        try:
            return json.loads((self.home / '.codex-global-state.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def hosts(self):
        state = self.state()
        relevant = {p['hostId'] for p in state.get('remote-projects', [])}
        relevant.update(state.get('thread-project-membership-host-ids', {}).values())
        return {h['hostId']: h for h in state.get('codex-managed-remote-connections', [])
                if h.get('hostId') in relevant and h.get('alias')}

    def decorate(self, rows, host, label):
        state = self.state()
        if host == 'local':
            projects = [{'id': p['id'], 'name': p['name'], 'roots': p.get('rootPaths', [])}
                        for p in state.get('local-projects', {}).values()]
        else:
            projects = [{'id': p['id'], 'name': p.get('label') or p['remotePath'].rsplit('/', 1)[-1], 'roots': [p['remotePath']]}
                        for p in state.get('remote-projects', []) if p['hostId'] == host]
        assignments = state.get('thread-project-assignments', {})
        def normalized(path):
            if host == 'local' and os.name == 'nt':
                value = path.replace('\\', '/').rstrip('/').casefold()
                if value.startswith('//?/unc/'):
                    value = '//' + value[8:]
                elif value.startswith('//?/'):
                    value = value[4:]
                return value
            return path.rstrip('/')
        for row in rows:
            raw_cwd = row.get('cwd', '')
            cwd = normalized(raw_cwd)
            assigned = assignments.get(row['id'], {})
            project = next((p for p in projects if p['id'] == assigned.get('projectId') and assigned.get('hostId', host) == host), None)
            explicitly_unassigned = row['id'] in assignments and project is None
            if host == 'local' and row['id'] in state.get('projectless-thread-ids', []):
                explicitly_unassigned = True
            if project is None and not explicitly_unassigned:
                matches = [(len(normalized(root)), p) for p in projects for root in p['roots'] if cwd == normalized(root) or cwd.startswith(normalized(root) + '/')]
                project = max(matches, key=lambda v: v[0])[1] if matches else None
            row.update(host=host, hostLabel=label,
                       projectId=project['id'] if project else None,
                       projectKey=host + '|' + (project['id'] if project else 'unassigned'),
                       projectName=project['name'] if project else '无归属线程',
                       recency=row.get('recency_at_ms') or (row.get('recency_at') or 0) * 1000 or row.get('updated_at_ms') or (row.get('updated_at') or 0) * 1000)
        return rows
