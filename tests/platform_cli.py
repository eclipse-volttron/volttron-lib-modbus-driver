"""Drive a real VOLTTRON platform through its command-line tools for integration tests.

The platform runs in a temporary VOLTTRON_HOME using the Python environment running the tests. Agents are installed
with ``vctl`` and points are read and written with ``vdrv`` (the Platform Driver's command-line tool), which is how an
operator would exercise a driver. This avoids ``volttrontesting.PlatformWrapper.build_agent``, which does not yet
support the modular platform's credential-based authentication.
"""
import ast
import os
import re
import subprocess
import sys
import time

from pathlib import Path

PLATFORM_CONFIG = """[volttron]
instance-name = modbus-driver-test
agent-isolation-mode = False
messagebus = zmq
auth-enabled = True
server-messagebus-id = vip.server
agent-monitor-frequency = 600
"""
# Keep poetry (which the platform uses to record installed agents) in the environment running the tests.
POETRY_LOCAL_CONFIG = "[virtualenvs]\ncreate = false\n\n[keyring]\nenabled = false\n"


def poetry_project_for_current_environment() -> str:
    """A pyproject.toml pinning every installed distribution, with editable ones as develop path dependencies.

    On first start the platform builds this file itself, but it adds the editable packages *before* pinning the
    rest, and resolving their dependencies at that point upgrades packages such as volttron-core to the newest
    release on PyPI, changing the environment the tests run in. Providing the file up front means the platform only
    locks it and installs nothing.
    """
    import json
    import sys
    from importlib import metadata
    pins, paths = [], []
    for dist in metadata.distributions():
        name = dist.metadata['Name']
        if not name or name.lower() == 'volttron':
            continue
        direct_url = dist.read_text('direct_url.json')
        info = json.loads(direct_url) if direct_url else {}
        if info.get('dir_info', {}).get('editable'):
            paths.append(f'"{name}" = {{path = "{info["url"].removeprefix("file://")}", develop = true}}')
        else:
            pins.append(f'"{name}" = "{dist.version}"')      # quoted: names may contain dots (backports.tarfile)
    v = sys.version_info
    return '\n'.join([
        '[tool.poetry]', 'name = "volttron"', 'version = "0.1.0"', 'description = ""',
        'authors = ["volttron <volttron@pnnl.gov>"]', 'package-mode = false', '',
        '[tool.poetry.dependencies]', f'python = "~{v.major}.{v.minor}.{v.micro}"',
        *sorted(paths), *sorted(pins), '',
        '[[tool.poetry.source]]', 'name = "test-pypi"', 'url = "https://test.pypi.org/simple/"',
        'priority = "supplemental"', '',
        '[build-system]', 'requires = ["poetry-core>=2.0.0,<3.0.0"]', 'build-backend = "poetry.core.masonry.api"', '',
    ])


class PlatformError(RuntimeError):
    pass


class PlatformCLI:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.log = self.home.parent / 'volttron.log'
        self.bin = Path(sys.executable).parent
        venv = self.bin.parent
        self.env = {**os.environ, 'VOLTTRON_HOME': str(self.home), 'VIRTUAL_ENV': str(venv),
                    'PATH': f"{self.bin}:{os.environ.get('PATH', '')}",
                    'PYTHON_KEYRING_BACKEND': 'keyring.backends.null.Keyring'}
        self.process: subprocess.Popen | None = None

    @classmethod
    def available(cls) -> bool:
        bin = Path(sys.executable).parent
        return all((bin / exe).exists() for exe in ('volttron', 'vctl', 'vdrv'))

    # ------------------------------------------------------------------ lifecycle

    def start(self, timeout: float = 480.0):
        """Start the platform and wait until vctl can talk to it.

        The first start in a new home takes a minute or two: the platform creates a poetry project there and records
        every package of the environment in it.
        """
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / 'config').write_text(PLATFORM_CONFIG)
        (self.home / 'poetry.toml').write_text(POETRY_LOCAL_CONFIG)
        (self.home / 'pyproject.toml').write_text(poetry_project_for_current_environment())
        self.stdout = self.home.parent / 'volttron.stdout'
        with open(self.stdout, 'w') as out:
            self.process = subprocess.Popen([str(self.bin / 'volttron'), '-vv', '-l', str(self.log)], env=self.env,
                                            cwd=str(self.home.parent), stdout=out, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise PlatformError(f'platform exited with {self.process.returncode}:\n{self.log_tail()}')
            if self._run(['vctl', 'status'], check=False, timeout=30).returncode == 0:
                return
            time.sleep(3)
        raise PlatformError(f'platform did not become ready within {timeout}s:\n{self.log_tail()}')

    def shutdown(self):
        if self.process is None:
            return
        self._run(['vctl', 'shutdown', '--platform'], check=False, timeout=60)
        try:
            self.process.wait(30)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.process = None

    def log_tail(self, lines: int = 40) -> str:
        parts = []
        stdout = getattr(self, 'stdout', None)
        if stdout and stdout.exists() and stdout.read_text().strip():
            parts.append('--- stdout/stderr ---\n' + '\n'.join(stdout.read_text().splitlines()[-lines:]))
        parts.append('--- log ---\n' + ('\n'.join(self.log.read_text().splitlines()[-lines:]) if self.log.exists() else '(no log)'))
        return '\n'.join(parts)

    def log_size(self) -> int:
        """Current length of the platform log, for use as the ``since`` argument of wait_for_log and log_since."""
        return len(self.log.read_text()) if self.log.exists() else 0

    def log_since(self, since: int) -> str:
        """The platform log text appended after the given log_size() value."""
        return self.log.read_text()[since:] if self.log.exists() else ''

    def wait_for_log(self, pattern: str, timeout: float = 90.0, since: int = 0) -> str:
        """Wait for a line matching the regular expression to appear in the platform log (after offset ``since``)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.log.exists():
                for line in reversed(self.log_since(since).splitlines()):
                    if re.search(pattern, line):
                        return line
            time.sleep(1)
        raise PlatformError(f'{pattern!r} did not appear in the log within {timeout}s:\n{self.log_tail()}')

    # ------------------------------------------------------------------ commands

    def _run(self, args, check=True, timeout=120.0) -> subprocess.CompletedProcess:
        args = [str(self.bin / args[0]), *map(str, args[1:])]
        result = subprocess.run(args, env=self.env, capture_output=True, text=True, timeout=timeout)
        if check and result.returncode != 0:
            raise PlatformError(f"{' '.join(args)} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
        return result

    def vctl(self, *args, timeout=180.0) -> str:
        return self._run(['vctl', *args], timeout=timeout).stdout

    def install_agent(self, source: str, vip_identity: str, tag: str, timeout=400.0):
        """Install (editable when source is a directory) and start an agent, waiting until it reports running."""
        self.vctl('install', source, '--vip-identity', vip_identity, '--tag', tag, '--start', timeout=timeout)
        self.wait_for_running(vip_identity)

    def restart_agent(self, vip_identity: str, tag: str):
        """Restart an agent, so that it reloads every configuration from the store, and wait until it is running."""
        self.vctl('restart', '--tag', tag)
        self.wait_for_running(vip_identity)

    def wait_for_running(self, vip_identity: str):
        deadline = time.time() + 60
        while time.time() < deadline:
            if re.search(rf'\s{re.escape(vip_identity)}\s.*running', self.vctl('status')):
                return
            time.sleep(2)
        raise PlatformError(f'{vip_identity} did not start:\n{self.vctl("status")}\n{self.log_tail()}')

    def store_config(self, identity: str, name: str, path: Path, csv: bool = False):
        self.vctl('config', 'store', identity, name, str(path), *(['--csv'] if csv else []))

    def vdrv(self, *args, timeout=90.0):
        """Run a vdrv driver command and return its printed Python literal (a dict, or a (results, errors) tuple)."""
        output = self._run(['vdrv', 'driver', *args], timeout=timeout).stdout
        start = min((i for i in (output.find('{'), output.find('(')) if i >= 0), default=-1)
        if start < 0:
            raise PlatformError(f'unexpected vdrv output: {output!r}')
        return ast.literal_eval(output[start:].strip())

    def get(self, topic: str):
        """Values keyed by full topic. For a point topic the dict has one entry."""
        return self.vdrv('get', topic)

    def get_point(self, topic: str):
        values = self.get(topic)
        if topic not in values:
            raise PlatformError(f'{topic} not returned; got {values}')
        return values[topic]

    def set_point(self, topic: str, value):
        results, errors = self.vdrv('set', '--', topic, str(value))
        if errors:
            raise PlatformError(f'set {topic} failed: {errors}')
        return results[topic]

    def revert(self, topic: str):
        return self.vdrv('revert', topic)
