"""验证 dotenv 配置读取及部署环境优先级。"""

from backend.config import Settings
from land_cover_classification.project_client import ProjectClient


def test_dotenv_and_environment_precedence(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("\n".join([
        "LCC_DATABASE_URL=from-file",
        "LCC_STORAGE_ROOT='" + str(tmp_path / "storage") + "'",
        "LCC_PUBLIC_API_KEY=" + "p" * 32,
    ]), encoding="utf-8")
    for key in ("LCC_STORAGE_ROOT", "LCC_PUBLIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LCC_ENV_FILE", str(env))
    monkeypatch.setenv("LCC_DATABASE_URL", "from-environment")
    settings = Settings.from_env()
    settings.validate()
    assert settings.database_url == "from-environment"
    assert settings.storage_root == tmp_path / "storage"


def test_plugin_config_requires_no_key(tmp_path, monkeypatch):
    config = tmp_path / "service.ini"
    config.write_text("[service]\nurl=http://127.0.0.1:9870\n", encoding="utf-8")
    monkeypatch.setenv("LCC_PLUGIN_CONFIG", str(config))
    client = ProjectClient.from_config()
    assert client.base_url == "http://127.0.0.1:9870"
