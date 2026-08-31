"""Что шаблон гарантированно получает от приложения.

Цифры тарифа раньше перечисляли в контексте каждого хендлера, и наборы
разошлись: /pricing передавал семь ключей, /new — четыре, /resumes — ни
одного. Забытый ключ Jinja подставляет пустой строкой и молчит, поэтому
пропавшая со страницы цена обнаруживалась бы только глазами. Теперь набор один
и приезжает через context processor — здесь проверяется и состав набора, и то,
что он действительно доезжает до рендера.
"""
import main


def test_plan_context_lists_every_tariff_number():
    """Состав набора — под тестом, а не «сколько вспомнили в этом хендлере»."""
    assert main._plan_context(None) == {
        "pro_price":          main.PRO_PRICE,
        "pro_days":           main.PRO_DAYS,
        "free_uses":          main.FREE_USES,
        "free_resumes":       main.FREE_RESUMES,
        "anon_limit":         main.ANON_LIMIT_CONST,
        "paid_pack":          main.PAID_PACK,
        "pro_fair_use_limit": main.PRO_FAIR_USE_LIMIT,
        "pro_fair_use_days":  main.PRO_FAIR_USE_DAYS,
    }


def test_plan_context_is_wired_into_the_template_engine():
    """Без регистрации в Jinja2Templates набор никуда не поедет."""
    assert main._plan_context in main.tpl.context_processors


def test_plan_context_reads_values_at_render_time(monkeypatch):
    """Значения берутся на каждый рендер, а не замораживаются на импорте.

    Иначе PRO_FAIR_USE_LIMIT, вынесенный в переменную окружения ради смены без
    выкатки кода, менялся бы только на перезапуске интерпретатора, а тесты,
    подменяющие его через monkeypatch, проверяли бы прошлое значение.
    """
    monkeypatch.setattr(main, "PRO_FAIR_USE_LIMIT", 1234)
    assert main._plan_context(None)["pro_fair_use_limit"] == 1234


async def test_tariff_numbers_reach_pages_that_pass_no_context(client, monkeypatch):
    """Страница, которая не передаёт ничего, всё равно видит цифры тарифа.

    /pricing и /offer именно такие после объединения набора — если бы
    context processor отвалился, обе отдали бы страницу с пустыми местами
    вместо цены, и статус ответа остался бы 200.
    """
    monkeypatch.setattr(main, "PRO_FAIR_USE_LIMIT", 137)
    for path in ("/pricing", "/offer"):
        r = await client.get(path)
        assert r.status_code == 200
        assert "137" in r.text, f"{path} отдана без числа из конфига"
        assert str(int(float(main.PRO_PRICE))) in r.text, f"{path} отдана без цены"
