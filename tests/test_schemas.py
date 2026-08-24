"""schemas._clip_profile: рекурсивная обрезка анонимного profile.

profile у AnonymousPreviewReq — намеренно свободный dict (см. докстринг
AnonymousPreviewReq в schemas.py), единственная защита от гигантского или
искусственно вложенного JSON — _clip_profile. Он рекурсивно спускается по
dict/list и на каждом шаге увеличивает depth на 1, а на глубине больше
_PROFILE_MAX_DEPTH отрезает ветку в None. Тесты ниже фиксируют именно эту
арифметику: где проходит граница глубины и что при рекурсии передаётся
дальше не теряется исходное значение и не подменяется на что-то другое.
"""
from schemas import _PROFILE_MAX_DEPTH, _clip_profile


def test_clip_profile_keeps_value_at_exact_max_depth():
    """Вложенность ровно в _PROFILE_MAX_DEPTH уровней (4 dict) — листовое
    значение обрабатывается на depth=4, это ещё не "больше лимита" и должно
    сохраниться дословно. depth не передаётся явно — используется значение по
    умолчанию (0), как в реальном вызове из _limit_profile."""
    nested = {"a": {"a": {"a": {"a": "leaf"}}}}
    assert _clip_profile(nested) == {"a": {"a": {"a": {"a": "leaf"}}}}


def test_clip_profile_drops_value_one_level_deeper_than_max_depth():
    """На один dict глубже — листовое значение обрабатывается на depth=5,
    что уже строго больше _PROFILE_MAX_DEPTH, и должно превратиться в None."""
    nested = {"a": {"a": {"a": {"a": {"a": "leaf"}}}}}
    assert _clip_profile(nested) == {"a": {"a": {"a": {"a": {"a": None}}}}}


def test_clip_profile_depth_limit_is_a_real_constant():
    """Тест выше опирается на _PROFILE_MAX_DEPTH=4 буквально — если константу
    когда-нибудь поменяют, тест должен упасть по понятной причине, а не молча
    проверять не то."""
    assert _PROFILE_MAX_DEPTH == 4


def test_clip_profile_preserves_values_through_list_nesting():
    """Списки — такой же рекурсивный шаг, как словари: depth должен расти
    ровно на 1 на каждом уровне вложенности, а сам элемент — доходить до
    результата, а не подменяться на None/число/константу.

    5 уровней вложенных списков дают листовому значению depth=5 (>4) — оно
    обязано превратиться в None, а вся цепочка списков вокруг — остаться
    на месте (это отличает "рекурсия сломана" от "рекурсии нет вовсе")."""
    nested = [[[[["deep"]]]]]
    assert _clip_profile(nested) == [[[[[None]]]]]


def test_clip_profile_preserves_short_list_values():
    """Без экстремальной вложенности элементы списка обязаны доходить до
    результата как есть — а не превращаться в None или в число (депту)."""
    assert _clip_profile({"tags": ["python", "sql"]}) == {"tags": ["python", "sql"]}


def test_clip_profile_preserves_values_through_dict_nesting():
    """Тот же сценарий, что и для списков, но через цепочку словарей с
    разными ключами на каждом уровне — чтобы не спутать с list-веткой."""
    nested = {"k1": {"k2": {"k3": {"k4": {"k5": "leaf"}}}}}
    assert _clip_profile(nested) == {"k1": {"k2": {"k3": {"k4": {"k5": None}}}}}


def test_clip_profile_clips_long_strings():
    from schemas import _PROFILE_STR_MAX
    long_value = "x" * (_PROFILE_STR_MAX + 100)
    result = _clip_profile({"bio": long_value})
    assert result["bio"] == "x" * _PROFILE_STR_MAX


def test_clip_profile_truncates_long_lists():
    from schemas import _PROFILE_LIST_MAX
    result = _clip_profile({"tags": list(range(_PROFILE_LIST_MAX + 20))})
    assert len(result["tags"]) == _PROFILE_LIST_MAX


def test_clip_profile_passes_through_non_container_scalars():
    """int/bool/None и т.п. — не str/list/dict, возвращаются как есть."""
    assert _clip_profile(42) == 42
    assert _clip_profile(True) is True
    assert _clip_profile(None) is None
