#!/usr/bin/env python3
"""Инкрементальный прогон mutmut: мутируем только то, что изменил git.

Полный прогон по main.py (2000+ строк) — это десятки минут, поэтому мутанты
отбираются по диффу с базовой веткой: берём изменённые .py из `source_paths`
mutmut (setup.cfg), сужаем до функций, чьи строки попали в дифф, и передаём
mutmut глобы имён мутантов. Любой невыживший мутант роняет прогон.

    python tools/mutation_diff.py                    # дифф с origin/main
    python tools/mutation_diff.py --base HEAD~1
    python tools/mutation_diff.py --scope files      # изменённые файлы целиком
    python tools/mutation_diff.py --scope all        # весь проект (долго)
    python tools/mutation_diff.py --dry-run          # только показать цели

Коды возврата: 0 — всё убито (или мутировать нечего), 1 — есть выжившие,
2 — ошибка запуска (неизвестный ref, упавший mutmut).

mutmut использует os.fork() и работает только на Linux/macOS/WSL.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from configparser import ConfigParser
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
MUTANTS_DIR = ROOT / "mutants"

# Отсев мутантов, которых бессмысленно убивать тестами, живёт рядом.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutation_ignore  # noqa: E402

# Разделитель класса в мангленных именах mutmut: xǁClassǁmethod__mutmut_1.
CLASS_SEP = "ǁ"

# Коды возврата мутанта → статус (зеркало status_by_exit_code из mutmut/__main__.py,
# включая порядок переопределений: -24 в итоге означает timeout, а не killed).
STATUS_BY_EXIT_CODE: dict[int | None, str] = {
    None: "not checked",
    0: "survived",
    1: "killed",
    2: "interrupted",
    3: "killed",
    5: "no tests",
    24: "timeout",
    -24: "timeout",
    33: "no tests",
    34: "skipped",
    35: "suspicious",
    36: "timeout",
    37: "caught by type check",
    152: "timeout",
    255: "timeout",
    -9: "segfault",
    -11: "segfault",
}
# Всё неизвестное mutmut считает suspicious — то есть тоже «не убит».
UNKNOWN_STATUS = "suspicious"

# Мутант жив, пока тест его не поймал. timeout — это пойманный бесконечный цикл,
# skipped/caught by type check отсеяны до запуска; всё остальное — провал.
KILLED_STATUSES = {"killed", "timeout", "skipped", "caught by type check"}

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


# ── git ─────────────────────────────────────────────────────────────────────
def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def _rev_parse(ref: str) -> str | None:
    try:
        return _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip() or None
    except RuntimeError:
        return None


def resolve_base(explicit: str | None) -> str | None:
    """Коммит, относительно которого считаем дифф.

    Берём merge-base с базовой веткой, а не саму ветку: иначе в дифф попадут
    чужие коммиты, приехавшие в main после ответвления.
    """
    if explicit:
        candidates = [explicit]
    else:
        env_ref = os.getenv("MUTATION_BASE_REF")
        candidates = [env_ref] if env_ref else ["origin/main", "origin/master", "HEAD~1"]

    for ref in candidates:
        sha = _rev_parse(ref)
        if sha is None:
            continue
        try:
            return _git("merge-base", sha, "HEAD").strip() or sha
        except RuntimeError:
            return sha

    if explicit:
        raise SystemExit(f"не удалось разрешить базовый ref: {explicit}")
    return None


def changed_lines(base: str) -> dict[str, set[int]]:
    """Изменённые строки .py-файлов: путь → номера строк в текущей версии.

    Дифф считается с рабочим деревом, а не с HEAD, чтобы локальный прогон видел
    ещё не закоммиченные правки. Новые файлы берём целиком.
    """
    result: dict[str, set[int]] = defaultdict(set)
    # --ignore-cr-at-eol: рабочее дерево, выкаченное на Windows с core.autocrlf=true,
    # из WSL или контейнера иначе выглядит целиком изменённым (CRLF против LF в блобах),
    # и в мутанты попадает весь файл вместо правок.
    diff = _git(
        "diff", "--unified=0", "--no-color", "--no-renames", "--ignore-cr-at-eol",
        "--diff-filter=ACM", base, "--", "*.py",
    )
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            current = None if path == "/dev/null" else path.removeprefix("b/")
        elif current and (match := _HUNK_RE.match(line)):
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            result[current].update(range(start, start + count))

    for path in _git("ls-files", "--others", "--exclude-standard", "--", "*.py").splitlines():
        if path:
            result[path].update(_all_lines(ROOT / path))
    return result


def _all_lines(path: Path) -> set[int]:
    try:
        return set(range(1, len(path.read_text(encoding="utf-8").splitlines()) + 1))
    except OSError:
        return set()


# ── отбор мутантов ──────────────────────────────────────────────────────────
def source_paths() -> list[str]:
    config = ConfigParser()
    config.read(ROOT / "setup.cfg", encoding="utf-8")
    raw = config.get("mutmut", "source_paths", fallback="")
    paths = [line.strip() for line in raw.splitlines() if line.strip()]
    if not paths:
        raise SystemExit("в setup.cfg нет [mutmut] source_paths — нечего мутировать")
    return paths


def is_mutated_source(path: str, sources: list[str]) -> bool:
    candidate = PurePosixPath(path)
    for source in sources:
        base = PurePosixPath(source.rstrip("/"))
        if candidate == base or base in candidate.parents:
            return True
    return False


def module_name(path: str) -> str:
    """main.py → main, tools/mutation_diff.py → tools.mutation_diff (как в mutmut)."""
    return PurePosixPath(path).with_suffix("").as_posix().replace("/", ".").removeprefix("src.")


def mutant_glob(path: str, mangled: str) -> str:
    # Полное имя мутанта в mutmut: <модуль>.<мангленное имя>__mutmut_<N>.
    return f"{module_name(path)}.{mangled}__mutmut_*".replace(".__init__.", ".")


def touched_functions(path: Path, lines: set[int]) -> list[str]:
    """Мангленные имена функций (x_foo / xǁClassǁmethod), задетых диффом.

    Мутанты mutmut существуют только для функций верхнего уровня и методов
    классов — вложенные функции входят в мутанты внешней. Строки декораторов
    считаем частью функции: mutmut их тоже мутирует.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []

    found: list[str] = []

    def visit(body: list[ast.stmt], class_name: str = "") -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                end = node.end_lineno or node.lineno
                if any(start <= line <= end for line in lines):
                    prefix = f"x{CLASS_SEP}{class_name}{CLASS_SEP}" if class_name else "x_"
                    found.append(f"{prefix}{node.name}")
            elif isinstance(node, ast.ClassDef):
                visit(node.body, node.name if not class_name else f"{class_name}.{node.name}")

    visit(tree.body)
    return found


def select_targets(scope: str, base: str | None) -> tuple[list[str], list[str]]:
    """(глобы имён мутантов, изменённые файлы) для нужного режима."""
    if scope == "all" or base is None:
        return ["*"], sorted(source_paths())

    sources = source_paths()
    changed = {
        path: lines
        for path, lines in changed_lines(base).items()
        if is_mutated_source(path, sources)
    }
    if not changed:
        return [], []

    if scope == "files":
        return [f"{module_name(path)}.*" for path in sorted(changed)], sorted(changed)

    globs = [
        mutant_glob(path, mangled)
        for path in sorted(changed)
        for mangled in touched_functions(ROOT / path, changed[path])
    ]
    return globs, sorted(changed)


# ── запуск и оценка ─────────────────────────────────────────────────────────
def mutmut_cmd() -> list[str]:
    executable = shutil.which("mutmut")
    # `python -m mutmut` mutmut ругает предупреждением про перезапуск __main__,
    # поэтому предпочитаем консольный скрипт.
    return [executable] if executable else [sys.executable, "-m", "mutmut"]


# mutmut падает с этим текстом, когда под фильтр не попало ни одного мутанта —
# например если в диффе только декорированные роут-хендлеры, которых он не
# мутирует вовсе. Это не поломка прогона, а «проверять нечего».
NOTHING_MATCHES = "Filtered for specific mutants, but nothing matches"


def run_mutmut(globs: list[str], extra: list[str]) -> tuple[int, str]:
    """(код возврата, объединённый вывод).

    Вывод захватываем: по нему отличаем пустой фильтр от настоящего падения,
    а заодно не тащим в лог CI километр спиннера — печатаем только хвост."""
    command = [*mutmut_cmd(), "run", *extra, *globs]
    print(f"$ {' '.join(command)}\n", flush=True)
    result = subprocess.run(
        command, cwd=ROOT, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    output = result.stdout or ""
    print("\n".join(output.splitlines()[-40:]), flush=True)
    return result.returncode, output


def collect_results(globs: list[str], files: list[str]) -> dict[str, list[str]]:
    """Имена мутантов по статусам — читаем meta-файлы mutmut напрямую."""
    from fnmatch import fnmatch

    by_status: dict[str, list[str]] = defaultdict(list)
    for path in files:
        meta_path = MUTANTS_DIR / f"{path}.meta"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        for name, exit_code in meta.get("exit_code_by_key", {}).items():
            if not any(fnmatch(name, glob) for glob in globs):
                continue
            by_status[STATUS_BY_EXIT_CODE.get(exit_code, UNKNOWN_STATUS)].append(name)
    return by_status


def report(by_status: dict[str, list[str]], allow_survived: int, mutmut_exit_code: int = 0,
           files: list[str] | None = None) -> int:
    total = sum(len(names) for names in by_status.values())
    if not total:
        print("Мутанты не создавались — нечего проверять.")
        return 0
    if mutmut_exit_code:
        print(f"\nВНИМАНИЕ: mutmut завершился с кодом {mutmut_exit_code} — часть мутантов могла остаться непроверенной.")

    killed = sum(len(names) for status, names in by_status.items() if status in KILLED_STATUSES)
    alive = {status: names for status, names in by_status.items() if status not in KILLED_STATUSES}
    alive_count = sum(len(names) for names in alive.values())

    print("\n" + "─" * 72)
    print(f"Мутантов проверено: {total}   убито: {killed}   выжило: {alive_count}")
    print(f"Mutation score: {killed / total * 100:.1f}%")
    for status in sorted(by_status):
        print(f"  {status:<22} {len(by_status[status])}")

    # Мутанты, которых бессмысленно убивать тестами (лог, текст сообщения,
    # регистр литерала, разобранные вручную), считаем отдельно и под гейт не
    # ставим — но показываем, чтобы отсев оставался на виду.
    alive_names = sorted(name for names in alive.values() for name in names)
    ignored = mutation_ignore.classify(MUTANTS_DIR, files or [], alive_names) if alive_names else {}
    remaining = [name for name in alive_names if name not in ignored]

    if ignored:
        by_reason: dict[str, list[str]] = defaultdict(list)
        for name, reason in ignored.items():
            by_reason[reason].append(name)
        print(f"\nОтсеяно как не про поведение: {len(ignored)}")
        for reason in sorted(by_reason):
            names = sorted(by_reason[reason])
            shown = ", ".join(name.rpartition(".")[2] for name in names[:6])
            more = f" … и ещё {len(names) - 6}" if len(names) > 6 else ""
            print(f"  {reason}: {len(names)} — {shown}{more}")

    if remaining:
        print("\nНе убитые мутанты (посмотреть диф: mutmut show <имя>):")
        for name in remaining[:40]:
            print(f"  {name}")
            described = mutation_ignore.describe(MUTANTS_DIR, files or [], name)
            if described:
                digest, was, became = described
                print(f"      - {was}")
                print(f"      + {became}")
                print("      если эквивалентный — строка для tools/mutation_ignore.txt:")
                print(f"      {digest}  # причина")
        if len(remaining) > 40:
            print(f"  … ещё {len(remaining) - 40}")

    if len(remaining) > allow_survived:
        print(f"\nFAIL: не убито {len(remaining)} мутантов при допустимых {allow_survived}.")
        return 1
    print("\nOK: значимых выживших мутантов нет.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", help="базовый git-ref (по умолчанию MUTATION_BASE_REF или origin/main)")
    parser.add_argument(
        "--scope", choices=("functions", "files", "all"), default="functions",
        help="что мутировать: задетые функции (по умолчанию), изменённые файлы целиком или всё",
    )
    parser.add_argument(
        "--allow-survived", type=int, default=0, metavar="N",
        help="сколько выживших мутантов допустимо (по умолчанию 0 — падаем на любом)",
    )
    parser.add_argument("--dry-run", action="store_true", help="только показать, что было бы проверено")
    parser.add_argument("mutmut_args", nargs="*", help="дополнительные аргументы для mutmut run")
    args = parser.parse_args()

    os.chdir(ROOT)
    base = resolve_base(args.base)
    print(f"База для диффа: {base or 'нет — полный прогон'}")

    globs, files = select_targets(args.scope, base)
    if not globs:
        print("В диффе нет изменений в мутируемых модулях — мутационный прогон пропущен.")
        return 0

    print(f"Файлов в диффе: {len(files)} → {', '.join(files)}")
    print(f"Целей для мутации ({args.scope}): {len(globs)}")
    for glob in globs:
        print(f"  {glob}")
    if args.dry_run:
        return 0

    exit_code, output = run_mutmut(globs, args.mutmut_args)
    by_status = collect_results(globs, files)
    if not by_status and NOTHING_MATCHES in output:
        # Функции в диффе есть, а мутантов у них нет. Так бывает с
        # декорированными роут-хендлерами: mutmut их не мутирует вовсе.
        # Проверять нечего — это не повод ронять сборку.
        print("\nУ изменённых функций нет мутантов — проверять нечего.")
        print("Так бывает с декорированными роут-хендлерами (@app.post и т.п.):")
        print("mutmut их не мутирует, поэтому HTTP-слой под гейт не попадает вовсе.")
        for glob in globs:
            print(f"  {glob}")
        return 0
    if exit_code != 0 and not by_status:
        print(f"\nmutmut завершился с кодом {exit_code} и не оставил результатов.")
        return 2
    return report(by_status, args.allow_survived, exit_code, files)


if __name__ == "__main__":
    sys.exit(main())
