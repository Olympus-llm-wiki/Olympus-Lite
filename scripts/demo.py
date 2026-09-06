#!/usr/bin/env python3
"""Generate a self-contained synthetic example through real CLI processes."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description="Создать отдельную демонстрацию на синтетических данных")
    p.add_argument("--output", required=True)
    args = p.parse_args()
    out = Path(args.output).expanduser().absolute()
    if out.exists():
        p.error("output directory must not exist")
    out.mkdir(parents=True)
    old, vault = out / "Legacy", out / "Library"
    (old / "Engineering").mkdir(parents=True)
    (old / "Chats").mkdir()
    (old / "Engineering/intake.md").write_text(
        "# Синтетический источник: перенос\n\n"
        "При переносе исходная папка сохраняется. Выбор файлов фиксируется до копирования.\n"
        "Изменённый после выбора файл требует нового плана.\n", encoding="utf-8")
    (old / "Engineering/checks.md").write_text(
        "# Синтетический источник: проверки\n\n"
        "Совпадение хешей подтверждает совпадение байтов. Оно не доказывает истинность содержания.\n"
        "У новой страницы сохраняются ссылки на использованные исходники.\n", encoding="utf-8")
    (old / "Chats/idea.md").write_text(
        "# Синтетическое обсуждение\n\nМожет быть, создадим школу космической кулинарии. Решение не принято.\n",
        encoding="utf-8")
    (old / "AGENTS.md").write_text("Синтетический пример старых инструкций: этот файл не импортируется.\n", encoding="utf-8")
    before = {p.relative_to(old).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in old.rglob("*.md")}

    def run(*args):
        r = subprocess.run([sys.executable, str(REPO / "olympus.py"), *map(str, args)],
                           capture_output=True, text=True, timeout=30)
        if r.returncode:
            raise RuntimeError(r.stderr)
        return json.loads(r.stdout)

    run("init", vault, "--title", "Olympus-Lite — учебная библиотека")
    run("inventory", old, "--output", out / "inventory.json")
    run("plan", old, "--namespace", "synthetic-demo", "--select", "Engineering", "--role", "primary",
        "--output", out / "engineering-plan.json")
    imported = run("--vault", vault, "import", out / "engineering-plan.json")
    repeat = run("--vault", vault, "import", out / "engineering-plan.json")
    assert repeat["captured"] == 0 and repeat["existing"] == 2
    run("plan", old, "--namespace", "synthetic-demo", "--select", "Chats", "--role", "discussion",
        "--output", out / "history-plan.json")
    run("--vault", vault, "import", out / "history-plan.json")
    plan = json.loads((out / "engineering-plan.json").read_text())
    sources = {Path(x["path"]).stem: x for x in plan["items"]}
    evidence = [
        {"source_id": sources["intake"]["source_id"], "version_id": sources["intake"]["version_id"],
         "quote": "При переносе исходная папка сохраняется.", "claim": "Перенос сохраняет исходную папку."},
        {"source_id": sources["checks"]["source_id"], "version_id": sources["checks"]["version_id"],
         "quote": "Совпадение хешей подтверждает совпадение байтов. Оно не доказывает истинность содержания.",
         "claim": "Проверку сохранности нужно отделять от проверки содержания."},
    ]
    ep = out / "evidence.json"
    ep.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    pages = [
        ("concept", "source-provenance", "Происхождение материала",
         "Это учебная страница на синтетических источниках. Происхождение связывает вывод с сохранённым материалом.\n\n"
         "Проверка сохранности отвечает на вопрос о совпадении байтов. Оценка содержания — отдельная работа.\n\n"
         "Применение: [перенос темы](../../Knowledge/guide/import-a-topic.md). Контекст: [учебный проект](../../Knowledge/project/example-library.md)."),
        ("guide", "import-a-topic", "Как перенести выбранную тему",
         "Учебный пример: сначала выбираем материалы и фиксируем план, затем сохраняем копии и сверяем результат. "
         "Исходная папка остаётся доступной.\n\n"
         "После переноса проверяем смысл будущей страницы отдельно от совпадения файлов.\n\n"
         "Связано: [происхождение материала](../../Knowledge/concept/source-provenance.md), [учебный проект](../../Knowledge/project/example-library.md)."),
        ("project", "example-library", "Учебная библиотека",
         "Полностью синтетический пример устройства Olympus-Lite. Здесь показаны связанные понятие и инструкция.\n\n"
         "Начать чтение: [происхождение материала](../../Knowledge/concept/source-provenance.md), затем [перенос темы](../../Knowledge/guide/import-a-topic.md).\n\n"
         "Этот пример не утверждает фактов о настоящих проектах владельца."),
    ]
    for kind, slug, title, body in pages:
        body_path = out / f"{slug}-body.md"
        body_path.write_text(body, encoding="utf-8")
        d = run("--vault", vault, "propose", body_path, "--title", title, "--slug", slug,
                "--kind", kind, "--evidence", ep)
        run("--vault", vault, "approve", d["proposal_id"], "--expected-sha256", d["expected_sha256"],
            "--reviewer", "Synthetic demo owner")
    assert not run("--vault", vault, "search", "кулинарии")
    assert run("--vault", vault, "search", "кулинарии", "--include-history")[0]["role"] == "discussion"
    report = run("--vault", vault, "lint")
    after = {p.relative_to(old).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in old.rglob("*.md")}
    assert before == after and report["ok"]
    summary = {"synthetic_only": True, "home": str(vault / "Home.md"),
               "source_versions": report["source_versions"], "knowledge_pages": report["knowledge_pages"],
               "legacy_unchanged": True, "repeat_created_versions": repeat["captured"],
               "lint": report, "model_called": False}
    (out / "verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
