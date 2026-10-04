"""Runs the end-to-end harness (interface + proxy manager + proxy subprocess + pymodbus simulator) in a subprocess.

Needs only the installed packages and two free local ports; no hardware. Skipped if pymodbus has no server support.
"""
import socket
import subprocess
import sys

from pathlib import Path

import pytest

pytest.importorskip('pymodbus.server')

HARNESS = Path(__file__).with_name('e2e_harness.py')
SERVER_HARNESS = Path(__file__).with_name('e2e_server_harness.py')


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def test_end_to_end(tmp_path):
    log = tmp_path / 'e2e.log'
    proc = subprocess.run([sys.executable, str(HARNESS), str(_free_port())], capture_output=True, text=True,
                          timeout=120, env={**__import__('os').environ, 'MODBUS_E2E_LOG': str(log)})
    report = proc.stdout + proc.stderr
    if proc.returncode != 0 and log.exists():
        report += '\n--- log ---\n' + log.read_text()[-4000:]
    assert proc.returncode == 0, report
    assert '15/15 passed' in proc.stdout, report


def test_end_to_end_slave_role(tmp_path):
    """A served unit and a master, both interfaces through one real proxy process, with a proxy crash in between."""
    log = tmp_path / 'e2e_server.log'
    proc = subprocess.run([sys.executable, str(SERVER_HARNESS), str(_free_port())], capture_output=True, text=True,
                          timeout=180, env={**__import__('os').environ, 'MODBUS_E2E_LOG': str(log)})
    report = proc.stdout + proc.stderr
    if proc.returncode != 0 and log.exists():
        report += '\n--- log ---\n' + log.read_text()[-4000:]
    assert proc.returncode == 0, report
    assert '16/16 passed' in proc.stdout, report
