import os
import pytest
import pytest_asyncio

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
# Клиент в тестах ходит по http://test: при https-значении APP_URL (из .env)
# session-cookie ставилась бы с Secure и не возвращалась бы клиентом.
os.environ["APP_URL"] = "http://test"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """SQLite connection with temp DB and initialized schema."""
    import config
    import main

    db_path = str(tmp_path / "test.db")
    # get_db/init_db читают config.DB_PATH в момент вызова
    monkeypatch.setattr(config, "DB_PATH", db_path)
    main.init_db()
    conn = main.get_db()
    yield conn
    conn.close()


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Async HTTP test client with isolated temp DB."""
    from httpx import AsyncClient, ASGITransport
    import config
    import main

    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)

    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as ac:
        yield ac
