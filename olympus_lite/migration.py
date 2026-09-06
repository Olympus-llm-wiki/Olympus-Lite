"""Explicit, read-only legacy discovery and hash-bound selection plans."""
from fnmatch import fnmatchcase
import os
from pathlib import Path

from .storage import (LiteError, MAX_BATCH, MAX_FILE, ROLES, TEXT_EXTENSIONS,
                      Vault, atomic_write, contained, digest, fingerprint,
                      json_bytes, now, read_bytes, relative, root_path, source_payload)

SYSTEM_NAMES = {"agents.md", "claude.md", "gemini.md", "skill.md", "license", "license.md"}
MAX_ITEMS = 1000


def inventory(path) -> dict:
    root = root_path(path)
    if not root.is_dir():
        raise LiteError("legacy_root_not_directory")
    files, skipped, scanned = [], [], 0

    def error(_):
        raise LiteError("inventory_unreadable_directory")

    for parent, dirs, names in os.walk(root, followlinks=False, onerror=error):
        for name in sorted(dirs + names):
            scanned += 1
            if scanned > 50000:
                raise LiteError("inventory_limit_reached")
            p = Path(parent) / name
            rel = p.relative_to(root).as_posix()
            reason = None
            if p.is_symlink():
                reason = "symlink"
            elif name.startswith("."):
                reason = "hidden"
            elif name.casefold() in SYSTEM_NAMES:
                reason = "instructions_or_license"
            elif name in names and p.suffix.lower() not in TEXT_EXTENSIONS:
                reason = "unsupported_format"
            elif name in names and p.stat().st_size > MAX_FILE:
                reason = "file_too_large"
            if reason:
                skipped.append({"path": rel, "reason": reason})
                if name in dirs:
                    dirs.remove(name)
            elif name in names:
                relative(rel)
                files.append({"path": rel, "size": p.stat().st_size})
        dirs.sort()
    return {"schema": 1, "root": str(root), "complete": True,
            "files": sorted(files, key=lambda x: x["path"]), "skipped": skipped}


def matches(path: str, pattern: str) -> bool:
    return path == pattern or path.startswith(pattern.rstrip("/") + "/") or fnmatchcase(path, pattern)


def make_plan(path, *, namespace: str, select: list[str], exclude: list[str] | None = None,
              role="unclassified") -> dict:
    if not namespace.strip() or len(namespace) > 200 or role not in ROLES:
        raise LiteError("invalid_namespace_or_role")
    if not select:
        raise LiteError("explicit_selection_required")
    for pattern in select + (exclude or []):
        relative(pattern.rstrip("/"))
    listing = inventory(path)
    root = Path(listing["root"])
    selected = [item for item in listing["files"]
                if any(matches(item["path"], p) for p in select)
                and not any(matches(item["path"], p) for p in (exclude or []))]
    if not selected:
        raise LiteError("selection_is_empty")
    if len(selected) > MAX_ITEMS or sum(x["size"] for x in selected) > MAX_BATCH:
        raise LiteError("selection_too_large")
    items = []
    for item in selected:
        rel = item["path"]
        raw = read_bytes(contained(root, rel))
        payload = source_payload(raw, key=f"legacy:{namespace}:{rel}", title=Path(rel).stem,
            role=role, locator=rel, suffix=Path(rel).suffix,
            metadata={"import_namespace": namespace, "legacy_relative_path": rel,
                      "imported_status": "historical_unreviewed"})
        items.append({"path": rel, "size": len(raw), "sha256": digest(raw), "role": role,
                      "source_id": payload["source_id"], "version_id": payload["version_id"]})
    core = {"schema": 1, "root": str(root), "namespace": namespace, "select": select,
            "exclude": exclude or [], "items": items}
    return {**core, "plan_id": "imp-" + fingerprint(core), "created_at": now(),
            "warnings": ["Imported content remains historical and unreviewed.",
                         "Original links are preserved; omitted linked files are not fetched."]}


def validate_plan(plan: dict) -> dict:
    fields = {"schema", "root", "namespace", "select", "exclude", "items"}
    try:
        core = {k: plan[k] for k in fields}
        valid = (plan["schema"] == 1 and plan["plan_id"] == "imp-" + fingerprint(core)
                 and isinstance(plan["items"], list) and 0 < len(plan["items"]) <= MAX_ITEMS
                 and isinstance(plan["namespace"], str) and plan["namespace"].strip()
                 and plan["select"])
        if not valid:
            raise LiteError("invalid_import_plan")
        seen = set()
        for item in plan["items"]:
            rel = relative(item["path"])
            if rel in seen or item["role"] not in ROLES:
                raise LiteError("invalid_import_item")
            seen.add(rel)
        return core
    except (KeyError, TypeError, ValueError) as exc:
        raise LiteError("invalid_import_plan") from exc


def apply_plan(vault: Vault, plan: dict) -> dict:
    validate_plan(plan)
    root = root_path(plan["root"])
    if root.is_relative_to(vault.root) or vault.root.is_relative_to(root):
        raise LiteError("legacy_and_destination_overlap")
    allowed = {x["path"] for x in inventory(root)["files"]}
    payloads, total = [], 0
    # Freeze a bounded complete batch in memory before publishing any sources.
    for item in plan["items"]:
        rel = relative(item["path"])
        if (rel not in allowed or not any(matches(rel, p) for p in plan["select"])
                or any(matches(rel, p) for p in plan["exclude"])):
            raise LiteError("import_item_outside_selection")
        raw = read_bytes(contained(root, rel))
        total += len(raw)
        if total > MAX_BATCH:
            raise LiteError("selection_too_large")
        if digest(raw) != item["sha256"] or len(raw) != item["size"]:
            raise LiteError("import_input_changed")
        payload = source_payload(raw, key=f"legacy:{plan['namespace']}:{rel}",
            title=Path(rel).stem, role=item["role"], locator=rel, suffix=Path(rel).suffix,
            metadata={"import_namespace": plan["namespace"], "legacy_relative_path": rel,
                      "imported_status": "historical_unreviewed"})
        if any(payload[k] != item[k] for k in ("source_id", "version_id")):
            raise LiteError("import_identity_mismatch")
        payloads.append(payload)
    with vault.lock():
        receipt_rel = f"Journal/{plan['plan_id']}.json"
        prior = vault.path(receipt_rel)
        results = [vault.store_source(payload) for payload in payloads]
        receipt = {"schema": 1, "kind": "import", "plan_id": plan["plan_id"],
                   "namespace": plan["namespace"], "applied_at": now(),
                   "items": [{k: r[k] for k in ("source_id", "version_id")} for r in results],
                   "bytes": total, "legacy_modified": False, "knowledge_pages_created": 0}
        if prior.exists():
            from .storage import read_json
            old_receipt = read_json(prior)
            if any(old_receipt.get(k) != receipt[k] for k in
                   ("schema", "kind", "plan_id", "namespace", "items", "bytes")):
                raise LiteError("import_receipt_mismatch")
        else:
            atomic_write(prior, json_bytes(receipt))
        navigation = vault.reindex_unlocked()
        return {"plan_id": plan["plan_id"], "state": "applied", "results": results,
                "captured": sum(r["state"] == "captured" for r in results),
                "existing": sum(r["state"] == "existing" for r in results),
                "receipt": str(prior), "navigation": navigation}
