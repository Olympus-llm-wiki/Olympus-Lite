"""One offline CLI for agents and terminal users."""
import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .migration import inventory, make_plan, apply_plan
from .review import KINDS, propose, approve, read_proposal
from .storage import LiteError, ROLES, Vault, atomic_write, json_bytes, read_bytes, read_json
from .views import lint, search


def parser():
    p = argparse.ArgumentParser(description="Olympus-Lite: файловая библиотека без Hindsight и сервера")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--vault", help="Папка личной библиотеки")
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("init", help="Создать новую пустую библиотеку")
    a.add_argument("path")
    a.add_argument("--title", default="Моя библиотека")
    a = sub.add_parser("capture", help="Сохранить полный источник")
    a.add_argument("path")
    a.add_argument("--key")
    a.add_argument("--title")
    a.add_argument("--role", choices=sorted(ROLES), default="primary")
    a.add_argument("--text-file")
    a.add_argument("--locator")
    a = sub.add_parser("inventory", help="Показать переносимые файлы старой вики")
    a.add_argument("root")
    a.add_argument("--output")
    a = sub.add_parser("plan", help="Зафиксировать явный отбор старых материалов")
    a.add_argument("root")
    a.add_argument("--namespace", required=True)
    a.add_argument("--select", action="append", required=True, help="Относительный путь, папка или glob; повторяемый")
    a.add_argument("--exclude", action="append", default=[])
    a.add_argument("--role", choices=sorted(ROLES), default="unclassified")
    a.add_argument("--output", required=True)
    a = sub.add_parser("import", help="Применить план; старые файлы не изменяются")
    a.add_argument("plan")
    a = sub.add_parser("search", help="Найти слова в знаниях и источниках")
    a.add_argument("query")
    a.add_argument("--include-history", action="store_true")
    a.add_argument("--limit", type=int, default=20)
    a = sub.add_parser("propose", help="Подготовить страницу с проверенными цитатами")
    a.add_argument("body")
    a.add_argument("--title", required=True)
    a.add_argument("--slug", required=True)
    a.add_argument("--kind", choices=sorted(KINDS), default="note")
    a.add_argument("--evidence", required=True)
    a = sub.add_parser("show", help="Прочитать точный черновик и hash для решения")
    a.add_argument("proposal")
    a = sub.add_parser("approve", help="Принять показанную владельцу точную версию")
    a.add_argument("proposal")
    a.add_argument("--expected-sha256", required=True)
    a.add_argument("--reviewer", required=True)
    sub.add_parser("reindex", help="Обновить производные каталоги")
    sub.add_parser("lint", help="Проверить целостность и актуальность одобрений")
    sub.add_parser("status", help="Показать локальное состояние библиотеки")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.command == "init":
            vault = Vault.create(args.path, title=args.title)
            result = {"state": "created", "vault": str(vault.root), "home": str(vault.path("Home.md"))}
        elif args.command in {"inventory", "plan"}:
            if args.command == "inventory":
                result = inventory(args.root)
            else:
                result = make_plan(args.root, namespace=args.namespace, select=args.select,
                                   exclude=args.exclude, role=args.role)
            if args.output:
                output = Path(args.output).expanduser().absolute()
                if output.exists():
                    raise LiteError("output_already_exists")
                atomic_write(output, json_bytes(result))
                result = {"output": str(output), "plan_id": result.get("plan_id"),
                          "selected": len(result.get("items", result.get("files", [])))}
        else:
            if not args.vault:
                raise LiteError("vault_argument_required")
            vault = Vault(args.vault)
            if args.command == "capture":
                result = vault.capture(args.path, key=args.key, title=args.title, role=args.role,
                                       text_file=args.text_file, locator=args.locator)
            elif args.command == "import":
                result = apply_plan(vault, read_json(Path(args.plan)))
            elif args.command == "search":
                result = search(vault, args.query, include_history=args.include_history, limit=args.limit)
            elif args.command == "propose":
                result = propose(vault, read_bytes(Path(args.body)).decode("utf-8"), title=args.title,
                    slug=args.slug, kind=args.kind, evidence=read_json(Path(args.evidence)))
            elif args.command == "show":
                record = read_proposal(vault, args.proposal)
                result = {**record, "page": record["page"].decode("utf-8")}
            elif args.command == "approve":
                result = approve(vault, args.proposal, expected_sha256=args.expected_sha256, reviewer=args.reviewer)
            elif args.command == "reindex":
                result = vault.reindex()
            else:
                result = lint(vault)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if args.command == "lint" and not result["ok"] else 0
    except LiteError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, UnicodeError, KeyError, ValueError, TypeError):
        print(json.dumps({"error": "file_or_format_error"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
