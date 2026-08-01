from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_compose_has_only_loopback_dashboard_publication():
    text = (ROOT / "deploy/docker-compose.production.yml").read_text()
    assert '"127.0.0.1:8652:8050"' in text
    assert '"8050:8050"' not in text
    assert "privileged:" not in text
    assert "network_mode: host" not in text
    assert "/var/run/docker.sock" not in text
    assert "seccomp:unconfined" not in text


def test_demo_environment_contains_no_sensitive_names_or_values():
    text = (ROOT / "deploy/demo.env").read_text()
    for name in ("NOBITEX", "MYSQL", "TELEGRAM"):
        assert name not in text


def test_sarkhatti_is_never_referenced_by_deployment_artifacts():
    for path in (ROOT / "deploy").rglob("*"):
        if path.is_file():
            assert "sarkhatti" not in path.read_text().lower()
