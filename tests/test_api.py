
async def test_homepage_returns_200(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "Резюмирую" in r.text


async def test_pricing_page(client):
    r = await client.get("/pricing")
    assert r.status_code == 200


async def test_privacy_page(client):
    r = await client.get("/privacy")
    assert r.status_code == 200


async def test_contacts_page(client):
    r = await client.get("/contacts")
    assert r.status_code == 200


async def test_offer_page(client):
    r = await client.get("/offer")
    assert r.status_code == 200


async def test_me_anonymous(client):
    r = await client.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False


async def test_resumes_redirects_unauthenticated(client):
    r = await client.get("/resumes", follow_redirects=False)
    assert r.status_code in (302, 303)


async def test_settings_redirects_unauthenticated(client):
    r = await client.get("/settings", follow_redirects=False)
    assert r.status_code in (302, 303)


async def test_generate_requires_auth(client):
    body = {
        "name": "Test",
        "phone": "1234567890",
        "city": "Moscow",
        "target": "Python dev",
        "experience": [],
        "education": [],
        "skills": "Python",
        "languages": "Russian",
    }
    r = await client.post("/api/generate", json=body)
    assert r.status_code == 401


async def test_logout_returns_200(client):
    r = await client.post("/auth/logout")
    assert r.status_code == 200
