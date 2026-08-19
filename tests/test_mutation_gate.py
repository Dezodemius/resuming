"""Тесты на отсев мутантов (tools/mutation_ignore.py).

Фильтр решает, какой выживший мутант роняет сборку, а какой нет, — то есть
ошибка в нём молча ослабляет весь мутационный гейт. Поэтому проверяем обе
стороны: что шум действительно отсеивается и, главное, что настоящие мутации
под гейтом остаются.
"""
import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import mutation_ignore  # noqa: E402


def reason(original: str, mutated: str) -> str | None:
    return mutation_ignore._reason(ast.parse(original), ast.parse(mutated))


# ── шум: отсеивается ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("original, mutated", [
    ('log.info("качаем %s", url)', 'log.info(None, url)'),
    ('log.info("качаем %s", url)', 'log.info("качаем %s", None)'),
    ('log.info("качаем %s", url)', 'log.info("XXкачаем %sXX", url)'),
    ('log.warning("готово")', 'log.warning("ГОТОВО")'),
    ('log.exception("сбой у %s", user_id)', 'log.exception("сбой у %s")'),
])
def test_logging_changes_are_noise(original, mutated):
    assert reason(original, mutated) == mutation_ignore.LOG_REASON


@pytest.mark.parametrize("original, mutated", [
    ('raise HTTPException(400, "плохая ссылка")', 'raise HTTPException(400, None)'),
    ('raise HTTPException(400, "плохая ссылка")', 'raise HTTPException(400)'),
    ('raise HTTPException(400, "плохая ссылка")', 'raise HTTPException(400, "XXплохая ссылкаXX")'),
])
def test_exception_message_changes_are_noise(original, mutated):
    assert reason(original, mutated) == mutation_ignore.MESSAGE_REASON


@pytest.mark.parametrize("original, mutated", [
    ('db.execute("SELECT uses FROM anon_usage")', 'db.execute("select uses from anon_usage")'),
    ('db.execute("SELECT uses FROM anon_usage")', 'db.execute("SELECT USES FROM ANON_USAGE")'),
    ('response.set_cookie("a", samesite="lax")', 'response.set_cookie("a", samesite="LAX")'),
])
def test_case_only_changes_are_noise(original, mutated):
    assert reason(original, mutated) == mutation_ignore.CASE_REASON


# ── настоящие мутации: остаются под гейтом ──────────────────────────────────
@pytest.mark.parametrize("original, mutated", [
    # Код статуса — это поведение, даже рядом с текстом сообщения.
    ('raise HTTPException(400, "плохая ссылка")', 'raise HTTPException(401, "плохая ссылка")'),
    ('raise HTTPException(400, "плохая ссылка")', 'raise HTTPException(None, "плохая ссылка")'),
    ('raise HTTPException(400, "плохая ссылка")', 'raise HTTPException("плохая ссылка")'),
    ('raise HTTPException(502, "нет сети")', 'raise HTTPException(503, "нет сети")'),
    # Содержимое строки вне логов и сообщений — ключи, схемы, имена полей.
    ('check = data.get("hash")', 'check = data.get("XXhashXX")'),
    ('ok = url.startswith("https")', 'ok = url.startswith("XXhttpsXX")'),
    ('row = db.execute("SELECT uses FROM anon_usage")', 'row = db.execute("SELECT uses FROM XXanon_usageXX")'),
    # Логика и числа.
    ('if scheme not in allowed or not host:\n    pass',
     'if scheme not in allowed and not host:\n    pass'),
    ('if size >= MAX_JOB_BYTES:\n    pass',
     'if size > MAX_JOB_BYTES:\n    pass'),
    # Текст ValueError в этом коде — признак, а не сообщение: _save_resume
    # бросает ValueError("resume_limit"), а ручки разбирают его по строке.
    ('raise ValueError("resume_limit")', 'raise ValueError("XXresume_limitXX")'),
    ('raise ValueError("нет профиля")', 'raise ValueError("XXнет профиляXX")'),
    ('return time.time() - auth_date <= 3600', 'return time.time() - auth_date <= 3601'),
    ('limit = 10', 'limit = 11'),
    ('ok, col, left = _deduct(db, user_id)', 'ok, col, left = _deduct(None, user_id)'),
    # Логирование рядом не должно прикрывать соседнюю мутацию.
    ('log.info("ок"); status = r.status_code', 'log.info("ок"); status = None'),
])
def test_real_mutations_stay_gated(original, mutated):
    assert reason(original, mutated) is None


def test_identical_code_has_no_reason():
    assert reason('x = 1', 'x = 1') is None


# ── явный список ────────────────────────────────────────────────────────────
def test_change_key_is_stable_and_content_addressed():
    before = ['def f():', '    return digest[:32]']
    after = ['def f():', '    return digest[:33]']
    key = mutation_ignore.change_key(before, after)
    assert key is not None
    digest, was, became = key
    assert len(digest) == 8
    assert was == "return digest[:32]" and became == "return digest[:33]"
    # Тот же дифф — тот же ключ; изменившийся код — другой ключ, то есть запись
    # в mutation_ignore.txt перестанет действовать и мутант вернётся под гейт.
    assert mutation_ignore.change_key(before, after)[0] == digest
    assert mutation_ignore.change_key(before, ['def f():', '    return digest[:34]'])[0] != digest


def test_explicit_entries_are_documented():
    """Каждая запись явного списка обязана нести причину — иначе через год
    никто не поймёт, почему мутант прощён."""
    entries = mutation_ignore._load_explicit()
    assert entries, "явный список пуст — проверка потеряла смысл"
    for digest, why in entries.items():
        assert len(digest) == 8, digest
        assert why != "без причины", f"у записи {digest} нет объяснения"
        assert len(why) > 20, f"причина у {digest} слишком куцая: {why}"
