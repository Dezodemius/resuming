import os
import pytest
import pytest_asyncio

os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")


@pytest.fixture
def db(tmp_path, monkeypatch):
    """SQLite connection with temp DB and initialized schema."""
    import main

    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    main.init_db()
    conn = main.get_db()
    yield conn
    conn.close()


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    """Async HTTP test client with isolated temp DB."""
    from httpx import AsyncClient, ASGITransport
    import main

    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(main, "DB_PATH", db_path)

    async with AsyncClient(
        transport=ASGITransport(app=main.app), base_url="http://test"
    ) as ac:
        yield ac
