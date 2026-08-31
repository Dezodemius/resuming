"""Регрессии адверсариального ревью: попытки сломать приложение и данные.

Отдельный файл, а не дописывание в test_logic/test_security: правки едут
параллельным PR рядом с двумя другими ревью, и общий новый файл не конфликтует
с их изменениями в тех же тестовых модулях.

Каждый тест здесь воспроизводит подтверждённый сценарий отказа: вход, который
принимался до правки, и последствие, которое он оставлял в базе.
"""
import json

import httpx
import pytest
from fastapi import HTTPException

import main
from main import _as_resume_dict, _fail_stuck_generations, _parse_ai, _same_amount


# ── Разбор ответа модели: только JSON-объект, без NaN/Infinity ─────────────
# json.loads принимает и список, и голое число, и NaN. Всё это уезжало в
# resume_data как есть: карточка сохранялась, генерация списывалась, а
# открыть резюме потом было нельзя.

def test_parse_ai_accepts_plain_object():
    assert _parse_ai('{"target_role": "Dev"}') == {"target_role": "Dev"}


def test_parse_ai_still_extracts_object_from_surrounding_text():
    """Прежнее поведение: пояснения вокруг {…} не должны ломать разбор."""
    raw = 'Вот резюме:\n{"target_role": "Dev"}\nГотово.'
    assert _parse_ai(raw) == {"target_role": "Dev"}


@pytest.mark.parametrize("raw", ['[1, 2, 3]', '"просто строка"', '123', 'null', 'true'])
def test_parse_ai_rejects_non_object_json(raw):
    """Список/строка/число — валидный JSON, но не резюме."""
    with pytest.raises(HTTPException) as e:
        _parse_ai(raw)
    assert e.value.status_code == 502


def test_parse_ai_unwraps_object_wrapped_in_list():
    """Резюме, завёрнутое моделью в список, вытаскиваем как из текста вокруг."""
    assert _parse_ai('[{"target_role": "Dev"}]') == {"target_role": "Dev"}


@pytest.mark.parametrize("raw", ['{"ats_match": NaN}', '{"ats_match": Infinity}',
                                 '{"ats_match": -Infinity}'])
def test_parse_ai_rejects_nan_and_infinity(raw):
    """NaN/Infinity json.loads принимает, а отдать обратно в ответе нельзя."""
    with pytest.raises(HTTPException) as e:
        _parse_ai(raw)
    assert e.value.status_code == 502


def test_as_resume_dict_passes_dict_through():
    assert _as_resume_dict({"a": 1}) == {"a": 1}


# ── NaN из тела запроса не должен попадать в базу ──────────────────────────
# Сценарий отказа до правки: PUT с {"resume_data": {"x": NaN}} отвечал 200,
# писал в базу строку `{"x": NaN}`, после чего GET /api/resumes/{id} падал
# навсегда (Starlette сериализует ответы с allow_nan=False). Тот же трюк с
# профилем закрывал вход в генератор: /api/profile переставал отвечать.

async def _login(client, email):
    main.init_db()
    with main.get_db() as db:
        db.execute(
            "INSERT INTO magic_tokens (token, email, expires_at, used)"
            " VALUES (?,?,datetime('now','+10 minutes'),0)",
            (f"tok-{email}", email),
        )
        db.commit()
    await client.get(f"/auth/email/verify?token=tok-{email}", follow_redirects=False)
    with main.get_db() as db:
        return db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


def _add_resume(user_id, data='{"target_role":"x"}'):
    with main.get_db() as db:
        cur = db.execute(
            "INSERT INTO resumes (user_id, resume_data) VALUES (?,?)", (user_id, data)
        )
        db.commit()
        return cur.lastrowid


async def test_resume_put_rejects_nan_and_stays_readable(client):
    uid = await _login(client, "nan-resume@test.com")
    rid = _add_resume(uid)

    r = await client.put(
        f"/api/resumes/{rid}",
        content=b'{"resume_data": {"target_role": NaN}}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400

    with main.get_db() as db:
        stored = db.execute("SELECT resume_data FROM resumes WHERE id=?", (rid,)).fetchone()[0]
    assert "NaN" not in stored
    # Главное последствие: резюме по-прежнему читается.
    assert (await client.get(f"/api/resumes/{rid}")).status_code == 200


async def test_profile_rejects_nan_and_stays_readable(client):
    await _login(client, "nan-profile@test.com")
    body = ('{"name":"Иван","phone":"1","city":"Москва","experience":[{"x":NaN}],'
            '"education":[],"skills":"Python","languages":"RU"}')
    r = await client.post("/api/profile", content=body.encode(),
                          headers={"content-type": "application/json"})
    assert r.status_code == 400
    # Профиль не сохранён — и ручка чтения жива.
    r2 = await client.get("/api/profile")
    assert r2.status_code == 200
    assert r2.json()["profile"] is None


# ── PUT /api/resumes/{id}: статус из белого списка, ограниченные поля ──────
# До правки принималась любая строка любой длины: карточка со статусом
# «взломан» пропадала с доски (её нет ни в одной колонке), а company_name на
# 100 000 символов сохранялся как есть.

async def test_resume_put_rejects_unknown_status(client):
    uid = await _login(client, "status-bad@test.com")
    rid = _add_resume(uid)
    r = await client.put(f"/api/resumes/{rid}", json={"status": "взломан"})
    assert r.status_code == 400
    with main.get_db() as db:
        assert db.execute("SELECT status FROM resumes WHERE id=?", (rid,)).fetchone()[0] == "draft"


@pytest.mark.parametrize("status", ["draft", "sent", "waiting", "accepted", "rejected"])
async def test_resume_put_accepts_board_statuses(client, status):
    """Все пять статусов доски (templates/resumes.html) обязаны проходить."""
    uid = await _login(client, f"status-{status}@test.com")
    rid = _add_resume(uid)
    r = await client.put(f"/api/resumes/{rid}", json={"status": status})
    assert r.status_code == 200
    with main.get_db() as db:
        assert db.execute("SELECT status FROM resumes WHERE id=?", (rid,)).fetchone()[0] == status


async def test_resume_put_rejects_huge_company_name(client):
    uid = await _login(client, "company-huge@test.com")
    rid = _add_resume(uid)
    r = await client.put(f"/api/resumes/{rid}", json={"company_name": "х" * 100_000})
    assert r.status_code == 400
    with main.get_db() as db:
        assert db.execute("SELECT company_name FROM resumes WHERE id=?", (rid,)).fetchone()[0] is None


async def test_resume_put_rejects_non_object_body(client):
    """Тело-список: `"resume_data" in body` на нём работает и роняло ручку."""
    uid = await _login(client, "body-list@test.com")
    rid = _add_resume(uid)
    r = await client.put(f"/api/resumes/{rid}", json=["resume_data"])
    assert r.status_code == 400


# ── POST /api/resumes/save: сохраняем только объект резюме ────────────────

@pytest.mark.parametrize("payload", ["строка", [1, 2], 42])
async def test_save_resume_json_rejects_non_object(client, payload):
    await _login(client, f"save-{type(payload).__name__}@test.com")
    r = await client.post("/api/resumes/save", json={"resume_data": payload})
    assert r.status_code == 400
    with main.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM resumes").fetchone()[0] == 0


async def test_save_resume_json_accepts_object(client):
    await _login(client, "save-ok@test.com")
    r = await client.post("/api/resumes/save", json={"resume_data": {"target_role": "Dev"}})
    assert r.status_code == 200
    assert r.json()["resume_id"]


# ── Сумма платежа сравнивается по значению, а не по написанию ─────────────
# Робокасса не обещает вернуть OutSum ровно в том виде, в каком он ушёл.
# Строковое сравнение на "399.0000" давало «amount mismatch»: деньги списаны,
# Pro не выдан, и наружу это никак не видно.

@pytest.mark.parametrize("got", ["399.00", "399", "399.0", "399.0000", " 399.00 "])
def test_same_amount_accepts_equal_values_written_differently(got):
    assert _same_amount(got, "399.00") is True


@pytest.mark.parametrize("got", ["400.00", "39.90", "399,00", "abc", "", None, "399.01"])
def test_same_amount_rejects_other_values(got):
    assert _same_amount(got, "399.00") is False


_TEST_PASSWORD2 = "test-password2"


class _FakeResp:
    def __init__(self, text):
        self.status_code = 200
        self.text = text

    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self, text):
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        return _FakeResp(self._text)


def _opstate_xml(out_sum):
    return (
        '<?xml version="1.0"?>'
        '<OperationStateResponse xmlns="http://merchant.roboxchange.com/WebService/">'
        '<Result><Code>0</Code></Result><State><Code>100</Code></State>'
        f'<Info><OutSum>{out_sum}</OutSum></Info></OperationStateResponse>'
    )


async def _pending_payment(client, email, inv_id):
    uid = await _login(client, email)
    with main.get_db() as db:
        db.execute("INSERT INTO payments (user_id, pay_id, idem_key, amount) VALUES (?,?,?,?)",
                   (uid, inv_id, f"idem-{inv_id}", main.PRO_PRICE))
        db.commit()
    return uid


def _webhook_form(inv_id, out_sum):
    return {"OutSum": out_sum, "InvId": inv_id,
            "SignatureValue": main._robokassa_signature(out_sum, inv_id, _TEST_PASSWORD2)}


async def test_webhook_grants_pro_when_amount_written_differently(client, monkeypatch):
    """OutSum "399.0000" — та же сумма, что "399.00": Pro обязан быть выдан."""
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", _TEST_PASSWORD2)
    uid = await _pending_payment(client, "amt-fmt@test.com", "8001")
    monkeypatch.setattr(main.httpx, "AsyncClient",
                        lambda *a, **k: _FakeClient(_opstate_xml("399.0000")))

    r = await client.post("/api/pay/webhook", data=_webhook_form("8001", "399.0000"))
    assert r.text == "OK8001"
    with main.get_db() as db:
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (uid,)).fetchone()[0] == 1


async def test_webhook_still_rejects_different_amount(client, monkeypatch):
    """Послабление в сравнении не должно пропускать другую сумму."""
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", _TEST_PASSWORD2)
    uid = await _pending_payment(client, "amt-wrong@test.com", "8002")
    monkeypatch.setattr(main.httpx, "AsyncClient",
                        lambda *a, **k: _FakeClient(_opstate_xml("1.00")))

    r = await client.post("/api/pay/webhook", data=_webhook_form("8002", "1.00"))
    assert not r.text.startswith("OK")
    with main.get_db() as db:
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (uid,)).fetchone()[0] == 0


# ── Зависшие генерации после перезапуска ──────────────────────────────────
# /api/match/start списывает квоту и уходит в BackgroundTasks. Рестарт между
# ответом и завершением задачи оставлял карточку в статусе generating
# навсегда, а библиотека опрашивала /api/resumes по кругу на каждой вкладке.

def test_fail_stuck_generations_marks_only_generating(db):
    db.execute("INSERT INTO users (email) VALUES ('stuck@test.com')")
    uid = db.execute("SELECT id FROM users WHERE email='stuck@test.com'").fetchone()["id"]
    db.execute("INSERT INTO resumes (user_id, resume_data) VALUES (?,?)",
               (uid, json.dumps({"generation_status": "generating"})))
    db.execute("INSERT INTO resumes (user_id, resume_data) VALUES (?,?)",
               (uid, json.dumps({"target_role": "Dev"})))
    db.commit()

    assert _fail_stuck_generations() == 1

    rows = [json.loads(r["resume_data"])
            for r in db.execute("SELECT resume_data FROM resumes ORDER BY id").fetchall()]
    assert rows[0]["generation_status"] == "failed"
    assert rows[0]["generation_error"]
    assert rows[1] == {"target_role": "Dev"}          # готовое резюме не тронуто
    assert _fail_stuck_generations() == 0             # повторный запуск ничего не меняет


# ── Формат `updated`: тот же, что у остальных записей таблицы ─────────────
# _save_resume писал локальное время через isoformat() ('2026-08-31T09:17:35'),
# а UPDATE в /api/resumes/{id} — UTC через пробел. ORDER BY updated —
# текстовое сравнение, и 'T' > ' ': свежесозданное резюме всегда всплывало
# выше только что отредактированного, независимо от реального времени.

def test_saved_resume_updated_matches_created_format(db):
    from main import _save_resume

    db.execute("INSERT INTO users (email) VALUES ('fmt@test.com')")
    uid = db.execute("SELECT id FROM users WHERE email='fmt@test.com'").fetchone()["id"]
    db.commit()
    rid = _save_resume(db, uid, {"target_role": "Dev"}, "general")

    row = db.execute("SELECT created, updated FROM resumes WHERE id=?", (rid,)).fetchone()
    assert "T" not in row["updated"], "updated обязан быть в формате datetime('now')"
    assert row["updated"] == row["created"]


async def test_resumes_are_ordered_by_real_update_time(client):
    uid = await _login(client, "order@test.com")
    old = _add_resume(uid)
    with main.get_db() as db:
        db.execute("UPDATE resumes SET updated=datetime('now','-2 days') WHERE id=?", (old,))
        db.commit()
    fresh = await client.post("/api/resumes/save", json={"resume_data": {"target_role": "Dev"}})
    fresh_id = fresh.json()["resume_id"]

    # Правим старое резюме — оно обязано подняться наверх списка.
    assert (await client.put(f"/api/resumes/{old}", json={"status": "sent"})).status_code == 200
    ids = [r["id"] for r in (await client.get("/api/resumes")).json()["resumes"]]
    assert ids[0] == old, f"ожидали сверху только что обновлённое {old}, получили {ids}"
    assert fresh_id in ids
