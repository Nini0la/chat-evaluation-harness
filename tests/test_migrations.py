import os
import subprocess
import sys


def test_postgresql_downgrade_sql_drops_turnstatus_after_turns_table():
    environment = {
        **os.environ,
        "DATABASE_URL": "postgresql+psycopg://user:pass@localhost/chat_eval",
    }

    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0001_initial:base", "--sql"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    sql = completed.stdout
    assert "DROP TYPE turnstatus;" in sql
    assert sql.index("DROP TABLE turns;") < sql.index("DROP TYPE turnstatus;")


def test_postgresql_shadow_upgrade_reuses_existing_turnstatus():
    environment = {
        **os.environ,
        "DATABASE_URL": "postgresql+psycopg://user:pass@localhost/chat_eval",
    }

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "upgrade",
            "0001_initial:0002_shadow_chat",
            "--sql",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert "CREATE TYPE turnstatus" not in completed.stdout
    assert "status turnstatus NOT NULL" in completed.stdout
