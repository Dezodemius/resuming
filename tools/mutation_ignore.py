"""Отсев мутантов, которых бессмысленно убивать тестами.

Гейт настроен жёстко: любой выживший мутант — красная сборка. Это работает,
пока мутанты действительно про поведение. Но mutmut мутирует и то, что
поведения не меняет: текст лога, формулировку сообщения об ошибке, регистр
строкового литерала. Убить такого мутанта можно только проверкой на дословный
текст лога — такие тесты ломаются при любой правке формулировки и ничего не
стерегут.

Сравниваем не строки, а AST оригинальной функции и мутанта: mutmut меняет ровно
одно место, поэтому параллельный обход находит его точно. Для правила про
сообщения это принципиально — по тексту строки не отличить
`HTTPException(400, None)` (выброшено сообщение, поведение то же) от
`HTTPException(None, "…")` (выброшен код статуса, ответ ломается).

Автоматические правила:

`логирование` — различие внутри вызова `log.*(...)`. Что бы мутант ни сделал со
    строкой формата или аргументами, наружу это не выходит.
`текст сообщения об ошибке` — различие в человекочитаемых аргументах
    конструктора исключения (`HTTPException`, `ValueError`, …) при неизменном
    первом аргументе. Код статуса — первый аргумент, он остаётся под гейтом.
`только регистр литерала` — строковый литерал изменился лишь регистром. Такие
    мутанты почти всегда эквивалентны (ключевые слова и идентификаторы SQLite,
    имена HTTP-заголовков в httpx, значения вроде samesite), а там, где регистр
    важен, mutmut для того же литерала генерирует ещё и вариант `XXтекстXX` —
    он остаётся под гейтом и без теста не умрёт.

Явный список — `tools/mutation_ignore.txt`, по одному разобранному вручную
мутанту на строку. Ключ — хеш изменения, а не имя мутанта: имена нумеруются по
порядку и разъезжаются при любой правке функции. Если код изменится, хеш
перестанет совпадать и мутант вернётся под гейт — запись протухает в безопасную
сторону.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
from pathlib import Path

IGNORE_FILE = Path(__file__).resolve().parent / "mutation_ignore.txt"

# Только HTTPException: у него контракт однозначен — первый аргумент это код
# статуса, остальные текст для человека. ValueError сюда добавлять нельзя:
# в этом коде его сообщение используется как признак (`raise ValueError(
# "resume_limit")` и разбор через `if "resume_limit" in str(e)`), то есть текст
# там влияет на поведение и обязан оставаться под гейтом.
MESSAGE_CALLS = {"HTTPException"}

LOG_REASON = "логирование"
MESSAGE_REASON = "текст сообщения об ошибке"
CASE_REASON = "только регистр литерала"


# ── ключи для явного списка ─────────────────────────────────────────────────
def _diff_lines(original: list[str], mutated: list[str]) -> tuple[list[str], list[str]]:
    removed, added = [], []
    for line in difflib.unified_diff(original, mutated, lineterm="", n=0):
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("-"):
            removed.append(line[1:].strip())
        elif line.startswith("+"):
            added.append(line[1:].strip())
    return removed, added


def change_key(original: list[str], mutated: list[str]) -> tuple[str, str, str] | None:
    """(хеш, было, стало) — печатается рядом с выжившим мутантом и вставляется
    в tools/mutation_ignore.txt."""
    removed, added = _diff_lines(original, mutated)
    if not removed and not added:
        return None
    was, became = " ".join(removed), " ".join(added)
    digest = hashlib.sha256(f"{was}\n{became}".encode("utf-8")).hexdigest()[:8]
    return digest, was, became


# ── разбор сгенерированного mutmut файла ────────────────────────────────────
def _function_sources(mutants_file: Path) -> dict[str, list[str]]:
    """Имя функции → её строки в mutants/<файл>.

    mutmut кладёт рядом оригинал (`..._mutmut_orig`) и всех мутантов
    (`..._mutmut_<N>`) как отдельные функции верхнего уровня — сравнивать их
    можно прямо здесь, не заглядывая в исходный файл.
    """
    try:
        source = mutants_file.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return {}
    lines = source.splitlines()
    found: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno:
            found[node.name] = lines[node.lineno - 1 : node.end_lineno]
    return found


def _rename_signature(lines: list[str], original_name: str, mutant_name: str) -> list[str]:
    """Сигнатура мутанта отличается только именем функции — приводим к
    оригинальному. Значения аргументов по умолчанию не трогаем: они мутируются
    и остаются под гейтом."""
    if not lines:
        return lines
    return [lines[0].replace(mutant_name, original_name, 1), *lines[1:]]


def _parse_function(lines: list[str]) -> ast.AST | None:
    try:
        return ast.parse("\n".join(lines))
    except SyntaxError:
        return None


# ── поиск различия в AST ────────────────────────────────────────────────────
def _same(left: object, right: object) -> bool:
    if isinstance(left, ast.AST) and isinstance(right, ast.AST):
        return ast.dump(left) == ast.dump(right)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


def _find_difference(
    original: object, mutated: object, ancestors: tuple[tuple[object, object], ...] = ()
) -> tuple[object, object, tuple[tuple[object, object], ...]] | None:
    """Самая узкая пара различающихся узлов и цепочка родителей из оригинала.

    mutmut меняет ровно одно место, поэтому спускаемся, пока различие остаётся
    единственным. Как только оно «разъезжается» (разная длина списка аргументов,
    разные типы узлов), останавливаемся на текущей паре.
    """
    if type(original) is not type(mutated):
        return original, mutated, ancestors

    # Литерал считаем атомарным: спуск в его .value отрывает значение от узла,
    # и потом уже не понять, был ли это код статуса или текст сообщения.
    if isinstance(original, ast.Constant):
        return None if _same(original, mutated) else (original, mutated, ancestors)

    if isinstance(original, ast.AST):
        assert isinstance(mutated, ast.AST)
        differing = [
            (getattr(original, field, None), getattr(mutated, field, None))
            for field in original._fields
            if not _same(getattr(original, field, None), getattr(mutated, field, None))
        ]
        if not differing:
            return None
        if len(differing) > 1:
            return original, mutated, ancestors
        deeper = _find_difference(*differing[0], (*ancestors, (original, mutated)))
        return deeper or (original, mutated, ancestors)

    if isinstance(original, list):
        assert isinstance(mutated, list)
        if len(original) != len(mutated):
            return original, mutated, ancestors
        differing = [(a, b) for a, b in zip(original, mutated) if not _same(a, b)]
        if not differing:
            return None
        if len(differing) > 1:
            return original, mutated, ancestors
        deeper = _find_difference(*differing[0], ancestors)
        return deeper or (original, mutated, ancestors)

    return None if original == mutated else (original, mutated, ancestors)


# ── правила ─────────────────────────────────────────────────────────────────
def _is_log_call(node: object) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "log"
    )


def _message_call(node: object) -> ast.Call | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in MESSAGE_CALLS:
        return node
    return None


def _case_only(original: object, mutated: object) -> bool:
    if isinstance(original, ast.Constant) and isinstance(mutated, ast.Constant):
        original, mutated = original.value, mutated.value
    if not isinstance(original, str) or not isinstance(mutated, str):
        return False
    return original != mutated and original.lower() == mutated.lower()


def _belongs_to(parent: object, node: object) -> bool:
    if parent is node:
        return True
    if isinstance(parent, ast.AST):
        return any(_belongs_to(child, node) for child in ast.iter_child_nodes(parent))
    if isinstance(parent, list):
        return any(_belongs_to(item, node) for item in parent)
    return False


def _message_only(original: object, mutated: object,
                  ancestors: tuple[tuple[object, object], ...]) -> bool:
    """Различие затрагивает только человекочитаемые аргументы исключения.

    Первый аргумент обязан совпасть у обеих сторон: `HTTPException(400, None)`
    — это выброшенное сообщение и то же поведение, а `HTTPException(None, "…")`,
    `HTTPException(401, "…")` и `HTTPException("…")` меняют ответ и остаются под
    гейтом.
    """
    if _message_call(original) is not None:
        pair: tuple[object, object] | None = (original, mutated)
    else:
        pair = next(((o, m) for o, m in reversed(ancestors) if _message_call(o)), None)
    if pair is None:
        return False

    original_call, mutated_call = pair
    if _message_call(mutated_call) is None:
        return False
    if not original_call.args or not mutated_call.args:
        return False
    if not _same(original_call.args[0], mutated_call.args[0]):
        return False
    # Само различие не должно лежать внутри первого аргумента.
    return not _belongs_to(original_call.args[0], original)


def _reason(original_fn: ast.AST, mutated_fn: ast.AST) -> str | None:
    difference = _find_difference(original_fn, mutated_fn)
    if difference is None:
        return None
    original, mutated, ancestors = difference

    if _is_log_call(original) or any(_is_log_call(node) for node, _ in ancestors):
        return LOG_REASON
    if _case_only(original, mutated):
        return CASE_REASON
    if _message_only(original, mutated, ancestors):
        return MESSAGE_REASON
    return None


# ── явный список ────────────────────────────────────────────────────────────
def _load_explicit() -> dict[str, str]:
    entries: dict[str, str] = {}
    if not IGNORE_FILE.exists():
        return entries
    for raw in IGNORE_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        digest, _, reason = line.partition("#")
        digest = digest.strip()
        if digest:
            entries[digest] = reason.strip() or "без причины"
    return entries


# ── публичный вход ──────────────────────────────────────────────────────────
def _pair(mutants_dir: Path, files: list[str], name: str,
          cache: dict[str, dict[str, list[str]]]) -> tuple[list[str], list[str]] | None:
    """(строки оригинала, строки мутанта с приведённой сигнатурой)."""
    module, _, func = name.rpartition(".")
    path = next((p for p in files if p.removesuffix(".py").replace("/", ".") == module), None)
    if path is None:
        return None
    if path not in cache:
        cache[path] = _function_sources(mutants_dir / path)
    functions = cache[path]
    original_name = func.rsplit("__mutmut_", 1)[0] + "__mutmut_orig"
    if func not in functions or original_name not in functions:
        return None
    return functions[original_name], _rename_signature(functions[func], original_name, func)


def classify(mutants_dir: Path, files: list[str], names: list[str]) -> dict[str, str]:
    """Имя мутанта → причина отсева. Чего здесь нет — идёт под гейт.

    Мутант, который не удалось разобрать, остаётся под гейтом: молча прощать
    непонятное нельзя.
    """
    explicit = _load_explicit()
    cache: dict[str, dict[str, list[str]]] = {}
    ignored: dict[str, str] = {}

    for name in names:
        pair = _pair(mutants_dir, files, name, cache)
        if pair is None:
            continue
        original_lines, mutated_lines = pair

        key = change_key(original_lines, mutated_lines)
        if key and key[0] in explicit:
            ignored[name] = f"разобран вручную: {explicit[key[0]]}"
            continue

        original_tree = _parse_function(original_lines)
        mutated_tree = _parse_function(mutated_lines)
        if original_tree is None or mutated_tree is None:
            continue
        reason = _reason(original_tree, mutated_tree)
        if reason:
            ignored[name] = reason
    return ignored


def describe(mutants_dir: Path, files: list[str], name: str) -> tuple[str, str, str] | None:
    pair = _pair(mutants_dir, files, name, {})
    if pair is None:
        return None
    return change_key(pair[0], pair[1])
