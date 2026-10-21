from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@pytest.fixture(scope="session")
def db():
    from agent.db import Database

    try:
        database = Database()
    except (KeyError, psycopg.OperationalError) as e:
        pytest.skip(f"DB 없음 (docker compose up -d 후 다시 실행): {e}")
    yield database
    database.close()


@pytest.fixture(scope="session")
def toolbox(db):
    from agent.tools import Toolbox

    return Toolbox(db)
