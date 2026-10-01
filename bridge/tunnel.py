"""An opt-in Cloudflare Quick Tunnel whose lifetime follows this gateway."""
import re
import subprocess
import threading
from pathlib import Path


class QuickTunnel:
    URL = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com\b')

    def __init__(self, executable, port, data_dir, on_origin):
        self.executable = Path(executable)
        self.port = port
        self.data_dir = Path(data_dir)
        self.on_origin = on_origin
        self.process = None
        self.url = None
        self.ready = threading.Event()
        self.finished = threading.Event()
        self.failure = None

    def start(self):
        if not self.executable.is_file():
            raise RuntimeError("缺少 cloudflared，请查看 README 的外网访问配置")
        config = self.data_dir / 'cloudflared.yml'
        config.write_text('{}\n', encoding='utf-8')
        config.chmod(0o600)
        args = [str(self.executable), 'tunnel', '--config', str(config), '--no-autoupdate',
                '--url', 'http://127.0.0.1:' + str(self.port), '--protocol', 'http2',
                '--metrics', '127.0.0.1:0', '--grace-period', '2s']
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, encoding='utf-8', start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()
        for _ in range(60):
            if self.ready.wait(1):
                return self.url
            if self.finished.is_set():
                break
        self.close()
        raise RuntimeError("外网隧道未连接，请查看 .local/tunnel.log（校园网需允许向外连接 TCP 7844）")

    def _read(self):
        try:
            with (self.data_dir / 'tunnel.log').open('a', encoding='utf-8') as log:
                for line in self.process.stdout:
                    log.write(line)
                    log.flush()
                    match = self.URL.search(line)
                    if match and not self.url:
                        self.url = match[0]
                        self.on_origin(self.url)
                    if 'Registered tunnel connection' in line and self.url:
                        (self.data_dir / '外网地址.txt').write_text(self.url + '\n\n账号和密码与局域网网关相同。重启隧道后地址会变化。\n', encoding='utf-8')
                        self.ready.set()
        finally:
            self.finished.set()

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        path = self.data_dir / '外网地址.txt'
        if path.exists() and self.url and path.read_text(encoding='utf-8').startswith(self.url):
            path.unlink()
