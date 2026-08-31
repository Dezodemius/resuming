"""Слой конфигурации: разбор переменных окружения.

Настройка, которую нельзя ни задать, ни проверить, — хуже отсутствующей.
Именно так до этих тестов вела себя AI_CONCURRENCY: она была описана в
CLAUDE.md и в обоих .env.example, а в коде стояла зашитая двойка. Здесь
проверяется не «какие значения красивее», а то, что написанное в .env
действительно доезжает до приложения.
"""
import pytest

import config


# ── Булевы флаги ────────────────────────────────────────────────────────────
# До объединения каждый флаг разбирался по-своему: ROBOKASSA_TEST_MODE
# сравнивался строго с "1", и `ROBOKASSA_TEST_MODE=true` означал боевой режим
# с настоящими списаниями. Словарь «да»/«нет» теперь один на все флаги.

@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", "  On  ", "Yes"])
def test_flag_accepts_any_spelling_of_yes(raw):
    assert config.flag(raw, False, "MY_FLAG") is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "FALSE", "  Off  ", "No"])
def test_flag_accepts_any_spelling_of_no(raw):
    assert config.flag(raw, True, "MY_FLAG") is False


@pytest.mark.parametrize("default", [True, False])
def test_flag_empty_value_means_default(default):
    """Пустая строка — это «переменная не задана», а не «нет»."""
    assert config.flag("", default, "MY_FLAG") is default
    assert config.flag("   ", default, "MY_FLAG") is default


@pytest.mark.parametrize("default", [True, False])
def test_flag_unknown_value_falls_back_to_default(default):
    """Опечатка не должна ронять старт и не должна молча значить «нет».

    Молчаливое «нет» — это как раз тот случай, когда оператор уверен, что
    включил режим, а он выключен.
    """
    assert config.flag("маybe", default, "MY_FLAG") is default
    assert config.flag("2", default, "MY_FLAG") is default


def test_flag_warns_about_unknown_value(caplog):
    """Непонятное значение обязано быть видно в логе, иначе искать его негде."""
    with caplog.at_level("WARNING", logger="resuming"):
        config.flag("ага", False, "MY_FLAG")
    assert any("MY_FLAG" in r.getMessage() for r in caplog.records)


def test_flag_does_not_warn_about_understood_values(caplog):
    with caplog.at_level("WARNING", logger="resuming"):
        config.flag("yes", False, "MY_FLAG")
        config.flag("off", True, "MY_FLAG")
        config.flag("", True, "MY_FLAG")
    assert caplog.records == []


def test_env_flag_reads_the_environment(monkeypatch):
    monkeypatch.setenv("SOME_TEST_FLAG", "on")
    assert config.env_flag("SOME_TEST_FLAG") is True
    monkeypatch.setenv("SOME_TEST_FLAG", "off")
    assert config.env_flag("SOME_TEST_FLAG", True) is False


@pytest.mark.parametrize("default", [True, False])
def test_env_flag_missing_variable_means_default(monkeypatch, default):
    monkeypatch.delenv("SOME_TEST_FLAG", raising=False)
    assert config.env_flag("SOME_TEST_FLAG", default) is default


def test_env_flag_defaults_to_off(monkeypatch):
    """Умолчание флага — «выключено», и это не мелочь.

    Без явного default так читаются ROBOKASSA_TEST_MODE и OAUTH_LOGIN_ENABLED.
    Если умолчание перевернуть, ненастроенная установка молча уедет в тестовый
    режим Робокассы (платежи перестанут доходить) и покажет кнопки OAuth,
    которых никто не настраивал.
    """
    monkeypatch.delenv("SOME_TEST_FLAG", raising=False)
    assert config.env_flag("SOME_TEST_FLAG") is False


def test_env_flag_does_not_warn_when_variable_is_unset(monkeypatch, caplog):
    """Незаданная переменная — норма, а не повод шуметь.

    Почти все флаги в проде не заданы. Предупреждение на каждый превратило бы
    старт в стену warning-ов, среди которых настоящая опечатка потерялась бы.
    """
    monkeypatch.delenv("SOME_TEST_FLAG", raising=False)
    with caplog.at_level("WARNING", logger="resuming"):
        config.env_flag("SOME_TEST_FLAG")
    assert caplog.records == []


def test_env_flag_warning_names_the_variable(monkeypatch, caplog):
    """В предупреждении должно стоять имя переменной, а не «флаг»."""
    monkeypatch.setenv("SOME_TEST_FLAG", "ага")
    with caplog.at_level("WARNING", logger="resuming"):
        config.env_flag("SOME_TEST_FLAG")
    assert any("SOME_TEST_FLAG" in r.getMessage() for r in caplog.records)


def test_dev_mode_warning_names_the_variable(caplog):
    """DEV_MODE разбирается тем же flag() — и тоже обязан назвать себя.

    Иначе оператор видит в логе «непонятное значение» без единой подсказки,
    какую из десятка строк .env чинить.
    """
    with caplog.at_level("WARNING", logger="resuming"):
        assert config._dev_mode_enabled("ага", False) is False
    assert any("DEV_MODE" in r.getMessage() for r in caplog.records)


# ── Целочисленные переменные ────────────────────────────────────────────────

def test_env_int_reads_the_environment(monkeypatch):
    monkeypatch.setenv("SOME_TEST_INT", "7")
    assert config.env_int("SOME_TEST_INT", 2) == 7


@pytest.mark.parametrize("raw", ["", "   "])
def test_env_int_empty_value_means_default(monkeypatch, raw):
    """Пустое значение в .env (`AI_CONCURRENCY=`) — это «не задано»."""
    monkeypatch.setenv("SOME_TEST_INT", raw)
    assert config.env_int("SOME_TEST_INT", 3) == 3


def test_env_int_missing_variable_means_default(monkeypatch):
    monkeypatch.delenv("SOME_TEST_INT", raising=False)
    assert config.env_int("SOME_TEST_INT", 42) == 42


def test_env_int_accepts_negative_and_padded(monkeypatch):
    monkeypatch.setenv("SOME_TEST_INT", " -5 ")
    assert config.env_int("SOME_TEST_INT", 0) == -5


@pytest.mark.parametrize("raw", ["2  # столько-то", "2#комментарий"])
def test_env_int_ignores_trailing_comment(monkeypatch, raw):
    """Строки `ИМЯ=2  # подпись` разошлись по рабочим .env из .env.example,
    и не всякий разборщик .env отрезает такой хвост. Для числа это заведомо
    комментарий, а не значение, — иначе контейнер не поднимется из-за подписи.
    """
    monkeypatch.setenv("SOME_TEST_INT", raw)
    assert config.env_int("SOME_TEST_INT", 9) == 2


def test_env_int_comment_only_value_means_default(monkeypatch):
    monkeypatch.setenv("SOME_TEST_INT", "# ещё не решили")
    assert config.env_int("SOME_TEST_INT", 9) == 9


def test_env_int_names_the_variable_when_value_is_not_a_number(monkeypatch):
    """Голый int() сообщал бы только значение. Имя переменной важнее: их
    здесь десяток, и по «invalid literal for int(): 'два'» непонятно, какую
    строку .env чинить."""
    monkeypatch.setenv("SOME_TEST_INT", "два")
    with pytest.raises(RuntimeError) as exc:
        config.env_int("SOME_TEST_INT", 2)
    assert "SOME_TEST_INT" in str(exc.value)
    assert "два" in str(exc.value)


# ── Настройки, которые обязаны доезжать до приложения ───────────────────────

def test_ai_concurrency_is_configurable():
    """AI_CONCURRENCY читается из окружения, а не зашита числом.

    Регрессия из жизни: константа стояла числом, при этом переменная значилась
    настройкой в CLAUDE.md, в .env.example и в deploy/.env.staging.example, где
    стенду выставлен AI_CONCURRENCY=1. Стенд всё это время держал два
    параллельных запроса к модели.
    """
    import inspect

    source = inspect.getsource(config)
    assert 'env_int("AI_CONCURRENCY"' in source, (
        "AI_CONCURRENCY обязана читаться из окружения — иначе .env.example лжёт"
    )


def test_documented_env_vars_are_actually_read():
    """Каждая переменная из .env.example должна где-то читаться кодом.

    Обратное направление (код читает то, чего нет в примере) ловит
    test_env_example_documents_every_setting ниже. Вместе они держат
    .env.example честным: файл — единственная инструкция по настройке, и
    строка в нём, которую код игнорирует, стоит оператору часов.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    read_by_code = set()
    for name in ("config.py", "main.py"):
        text = (root / name).read_text(encoding="utf-8")
        read_by_code |= set(re.findall(r'(?:os\.getenv|env_int|env_flag)\(\s*["\']([A-Z0-9_]+)["\']', text))

    documented = set(re.findall(
        r"^([A-Z][A-Z0-9_]*)=", (root / ".env.example").read_text(encoding="utf-8"), re.M
    ))
    unused = documented - read_by_code
    assert not unused, f".env.example описывает переменные, которых код не читает: {sorted(unused)}"


def test_env_example_documents_every_setting():
    """Ни одной переменной окружения без строки в .env.example.

    DATA_DIR — исключение: её задаёт не человек, а образ (WORKDIR /app плюс
    том на /app/data), и предлагать её в шаблоне значит звать сломать путь
    к базе.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    read_by_code = set()
    for name in ("config.py", "main.py"):
        text = (root / name).read_text(encoding="utf-8")
        read_by_code |= set(re.findall(r'(?:os\.getenv|env_int|env_flag)\(\s*["\']([A-Z0-9_]+)["\']', text))

    documented = set(re.findall(
        r"^([A-Z][A-Z0-9_]*)=", (root / ".env.example").read_text(encoding="utf-8"), re.M
    ))
    missing = read_by_code - documented - {"DATA_DIR"}
    assert not missing, f"не описаны в .env.example: {sorted(missing)}"
