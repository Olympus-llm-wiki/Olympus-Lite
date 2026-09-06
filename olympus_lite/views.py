"""Rebuildable Markdown navigation, lexical retrieval, and integrity reports."""
from pathlib import Path
import re
from urllib.parse import quote

from .storage import (HISTORY_ROLES, LiteError, Vault, atomic_write, digest, json_bytes,
                      read_bytes, read_json)
from .review import PROPOSAL_RE, escape, read_proposal, validate_evidence

ROLE_NAMES = {"primary": "Первичный источник", "synthesis": "Синтез", "discussion": "Обсуждение",
              "decision": "Историческое решение", "unclassified": "Не классифицировано",
              "catalog": "Каталог", "artifact": "Артефакт", "assessment": "Оценка"}
STATUS_NAMES = {"reviewed_revision": "Проверена эта версия", "review_stale": "Нужна повторная проверка",
                "unreviewed": "Ещё не проверена"}


def link(label, path):
    return f"[{escape(label)}]({quote(path, safe='/.-')})"


def heading(text: str, fallback: str) -> str:
    return next((x[2:].strip() for x in text.splitlines() if x.startswith("# ")), fallback)


def receipts(vault: Vault) -> list[dict]:
    result = []
    for p in sorted(vault.path("Journal").glob("*.json")):
        rel = p.relative_to(vault.root).as_posix()
        result.append(read_json(vault.path(rel)))
    return result


def page_records(vault: Vault) -> list[dict]:
    latest = {}
    for r in receipts(vault):
        if r.get("kind") == "approval":
            target = r["target"]
            if target not in latest or r["accepted_at"] > latest[target]["accepted_at"]:
                latest[target] = r
    rows = []
    for path in sorted(vault.path("Knowledge").rglob("*.md")):
        if path.name == "index.md":
            continue
        rel = path.relative_to(vault.root).as_posix()
        data = read_bytes(vault.path(rel))
        text = data.decode("utf-8")
        receipt = latest.get(rel)
        status = "unreviewed"
        if receipt:
            status = "reviewed_revision" if digest(data) == receipt["accepted_sha256"] else "review_stale"
        rows.append({"path": rel, "title": heading(text, path.stem), "status": status,
                     "text": text, "receipt": receipt})
    return rows


def rebuild(vault: Vault) -> dict:
    sources = [vault.source(s, v) for s, v in vault.source_refs()]
    page_rows = page_records(vault)
    journal = receipts(vault)
    accepted = {r["proposal_id"] for r in journal if r.get("kind") == "approval"}
    drafts = []
    for p in sorted(vault.path("Drafts").iterdir()):
        if PROPOSAL_RE.fullmatch(p.name) and p.name not in accepted:
            drafts.append(read_proposal(vault, p.name))
    pages = {}
    pages["Sources/index.md"] = "# Источники\n\nВерсии сохранены как свидетельства; импорт не подтверждает их утверждения.\n\n| Материал | Роль | Текст |\n|---|---|---|\n" + "".join(
        f"| {link(s['content']['title'], s['relative_path'][8:] + '/' + s['content']['original_name'])} | {ROLE_NAMES[s['content']['role']]} | {'Есть' if s['text'] is not None else 'Не извлечён'} |\n"
        for s in sources)
    pages["Knowledge/index.md"] = "# Знания\n\nСвязанные страницы библиотеки. Проверка относится к конкретной версии текста.\n\n| Страница | Проверка |\n|---|---|\n" + "".join(
        f"| {link(p['title'], p['path'][10:])} | {STATUS_NAMES[p['status']]} |\n" for p in page_rows)
    pages["Drafts/index.md"] = "# Предложения страниц\n\nПрочитай текст и основания перед принятием. Изменение черновика требует нового предложения.\n\n" + "".join(
        "- " + link(d["core"]["title"], d["proposal_id"] + "/page.md") + "\n" for d in drafts)
    pages["Journal/index.md"] = "# История библиотеки\n\n| Время | Действие | Запись |\n|---|---|---|\n" + "".join(
        f"| {r.get('accepted_at', r.get('applied_at', ''))} | {'Принята страница' if r['kind'] == 'approval' else 'Импортированы источники'} | {link(r.get('target', r.get('namespace', '')), r.get('proposal_id', r.get('plan_id')) + '.json')} |\n"
        for r in journal)
    state_path = vault.path(".olympus-lite/generated.json")
    prior = read_json(state_path) if state_path.exists() else {}
    conflicts, written = [], []
    for rel, body in pages.items():
        data = (body + "\n[На главную](../Home.md)\n").encode()
        path = vault.path(rel)
        current = digest(read_bytes(path)) if path.exists() else None
        if current is not None and current not in {prior.get(rel), digest(data)}:
            conflicts.append(rel)
            continue
        if current != digest(data):
            atomic_write(path, data)
            written.append(rel)
        prior[rel] = digest(data)
    atomic_write(state_path, json_bytes(prior))
    return {"written": written, "conflicts": conflicts}


def search(vault: Vault, query: str, *, include_history=False, limit=20) -> list[dict]:
    terms = list(dict.fromkeys(re.findall(r"\w+", query.casefold())))
    if not terms or not 1 <= limit <= 100:
        raise LiteError("invalid_search_query_or_limit")
    candidates = []
    for row in page_records(vault):
        candidates.append({"title": row["title"], "path": row["path"], "role": "knowledge_page",
            "status": row["status"], "text": row["text"], "verified_fact": False})
    for sid, vid in vault.source_refs():
        s = vault.source(sid, vid)
        role = s["content"]["role"]
        if (role in HISTORY_ROLES and not include_history) or s["text"] is None:
            continue
        candidates.append({"title": s["content"]["title"], "path": s["relative_path"] + "/text.md",
            "role": role, "status": s["content"]["epistemic_status"], "text": s["text"],
            "source_id": sid, "version_id": vid, "verified_fact": False,
            "owner_decision_confirmed": False})
    matches = []
    for row in candidates:
        hay = (row["title"] + "\n" + row["text"]).casefold()
        if not all(term in hay for term in terms):
            continue
        text = row.pop("text")
        lines = text.splitlines()
        snippet = next((line for line in lines if any(t in line.casefold() for t in terms)), text[:250])
        score = sum(min(hay.count(t), 20) for t in terms) + (5 if row["role"] == "knowledge_page" else 0)
        matches.append({**row, "score": score, "excerpt": snippet[:500],
                        "absolute_path": str(vault.path(row["path"]))})
    return sorted(matches, key=lambda x: (-x["score"], x["path"]))[:limit]


def lint(vault: Vault) -> dict:
    issues, versions, page_count = [], 0, 0
    try:
        refs = list(vault.source_refs())
    except (LiteError, OSError):
        refs = []
        issues.append({"path": "Sources", "code": "source_inventory_invalid"})
    for sid, vid in refs:
        versions += 1
        try:
            vault.source(sid, vid)
        except (LiteError, OSError, KeyError, TypeError, UnicodeError) as exc:
            issues.append({"path": f"Sources/{sid}/{vid}",
                           "code": str(exc) if isinstance(exc, LiteError) else "source_unreadable"})
    try:
        rows = page_records(vault)
        page_count = len(rows)
        for row in rows:
            if row["status"] != "reviewed_revision":
                issues.append({"path": row["path"], "code": row["status"]})
            if row["receipt"]:
                try:
                    validate_evidence(vault, row["receipt"]["evidence"])
                except (LiteError, OSError, KeyError, TypeError):
                    issues.append({"path": row["path"], "code": "page_evidence_unavailable"})
        existing = {row["path"] for row in rows}
        for record in receipts(vault):
            if record.get("kind") == "approval" and record["target"] not in existing:
                issues.append({"path": record["target"], "code": "accepted_page_missing"})
        for p in vault.path("Drafts").iterdir():
            if PROPOSAL_RE.fullmatch(p.name):
                try:
                    draft = read_proposal(vault, p.name)
                    validate_evidence(vault, draft["core"]["evidence"])
                except (LiteError, OSError, KeyError, TypeError):
                    issues.append({"path": f"Drafts/{p.name}", "code": "draft_invalid"})
        generated = read_json(vault.path(".olympus-lite/generated.json"))
        for rel, expected in generated.items():
            try:
                if digest(read_bytes(vault.path(rel))) != expected:
                    issues.append({"path": rel, "code": "catalog_changed"})
            except (LiteError, OSError):
                issues.append({"path": rel, "code": "catalog_missing_or_unsafe"})
    except (LiteError, OSError, KeyError, TypeError, UnicodeError):
        issues.append({"path": "Knowledge", "code": "library_metadata_unreadable"})
    return {"ok": not issues, "source_versions": versions, "knowledge_pages": page_count,
            "issues": issues, "network_used": False}
