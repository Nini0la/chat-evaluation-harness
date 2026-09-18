from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_docker_sqlite_default_uses_app_owned_persistent_directory():
    dockerfile = (ROOT / "Dockerfile").read_text()
    readme = (ROOT / "README.md").read_text()

    assert "DATABASE_URL=sqlite:////app/data/chat_eval.db" in dockerfile
    assert "install -d -o app -g app /app/data" in dockerfile
    assert "-v chat-eval-data:/app/data" in readme
    assert "alembic upgrade head" in readme.split("## Docker", 1)[1]
