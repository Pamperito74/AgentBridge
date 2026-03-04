from pathlib import Path

from click.testing import CliRunner

from agentbridge.cli import cli


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_doctor_success(monkeypatch, tmp_path):
    def fake_get(url, timeout=0, headers=None):
        if url.endswith("/health"):
            return _Resp(payload={"status": "ok"})
        if url.endswith("/agents"):
            return _Resp(status_code=200, payload=[])
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("agentbridge.cli.requests.get", fake_get)
    monkeypatch.setenv("AGENTBRIDGE_LOG_DIR", str(tmp_path / "logs"))
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 0
    assert "OK   http_health" in result.output
    assert "OK   log_dir" in result.output


def test_doctor_fails_when_health_unreachable(monkeypatch, tmp_path):
    def fake_get(url, timeout=0, headers=None):
        raise RuntimeError("down")

    monkeypatch.setattr("agentbridge.cli.requests.get", fake_get)
    monkeypatch.setenv("AGENTBRIDGE_LOG_DIR", str(tmp_path / "logs"))
    runner = CliRunner()
    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "FAIL http_health" in result.output
