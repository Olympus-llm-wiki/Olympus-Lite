"""Evidence-bearing drafts and resumable acceptance of an exact revision."""
import re
from urllib.parse import quote

from .storage import (LiteError, Vault, atomic_write, check_text, digest, fingerprint,
                      json_bytes, now, publish_directory, read_bytes, read_json, relative)

KINDS = {"note", "concept", "topic", "project", "guide"}
PROPOSAL_RE = re.compile(r"draft-[0-9a-f]{32}\Z")


def escape(text: str) -> str:
    return text.replace("\n", " ").replace("|", "\\|").replace("[", "\\[").replace("]", "\\]")


def validate_evidence(vault: Vault, evidence) -> list[dict]:
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 50:
        raise LiteError("evidence_required")
    checked = []
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"source_id", "version_id", "claim", "quote"}:
            raise LiteError("invalid_evidence_shape")
        if any(not isinstance(v, str) or not v.strip() for v in item.values()):
            raise LiteError("empty_evidence_field")
        if len(item["quote"]) > 4000 or len(item["claim"]) > 4000:
            raise LiteError("evidence_too_large")
        source = vault.source(item["source_id"], item["version_id"])
        if source["text"] is None or item["quote"] not in source["text"]:
            raise LiteError("quote_not_found")
        checked.append(dict(item))
    return checked


def propose(vault: Vault, body: str, *, title: str, slug: str, evidence: list,
            kind="note") -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", slug) or kind not in KINDS:
        raise LiteError("invalid_page_name_or_kind")
    if not body.strip() or len(body.encode()) > 4 * 1024 * 1024 or not title.strip() or len(title) > 500:
        raise LiteError("invalid_page_body_or_title")
    check_text(body + title)
    with vault.lock():
        evidence = validate_evidence(vault, evidence)
        target = f"Knowledge/{kind}/{slug}.md"
        path = vault.path(target)
        base_sha = digest(read_bytes(path)) if path.exists() else None
        # Plain Markdown links work in both Obsidian and ordinary editors.
        refs = []
        for e in evidence:
            source = vault.source(e["source_id"], e["version_id"])
            ref = "../../" + source["relative_path"] + "/text.md"
            refs.append(f"- [{escape(source['content']['title'])}]({quote(ref, safe='/.-')}) — {escape(e['claim'])}")
        page = ("---\n" + f"title: {json_bytes(title.strip()).decode().strip()}\n"
                + f"kind: {kind}\nstatus: proposed\n---\n\n# {title.strip()}\n\n"
                + body.strip() + "\n\n## Основания\n\n" + "\n".join(refs) + "\n")
        core = {"schema": 1, "target": target, "base_sha256": base_sha,
                "page_sha256": digest(page.encode()), "title": title.strip(), "kind": kind,
                "evidence": evidence}
        pid = "draft-" + fingerprint(core)[:32]
        dest = vault.path(f"Drafts/{pid}")
        if dest.exists():
            existing = read_proposal(vault, pid)
            if existing["core"] != core:
                raise LiteError("proposal_identity_mismatch")
        else:
            publish_directory(dest, {"page.md": page.encode(),
                "proposal.json": json_bytes({"proposal_id": pid, "created_at": now(), "core": core})})
        navigation = vault.reindex_unlocked()
        return {"proposal_id": pid, "expected_sha256": core["page_sha256"],
                "path": str(dest / "page.md"), "target": target, "state": "awaiting_review",
                "navigation": navigation}


def read_proposal(vault: Vault, pid: str) -> dict:
    if not isinstance(pid, str) or not PROPOSAL_RE.fullmatch(pid):
        raise LiteError("invalid_proposal_id")
    record = read_json(vault.path(f"Drafts/{pid}/proposal.json"))
    core = record["core"]
    page = read_bytes(vault.path(f"Drafts/{pid}/page.md"))
    if (record["proposal_id"] != pid or "draft-" + fingerprint(core)[:32] != pid
            or digest(page) != core["page_sha256"]):
        raise LiteError("proposal_changed")
    target = relative(core["target"])
    if not re.fullmatch(r"Knowledge/(?:note|concept|topic|project|guide)/[a-z0-9][a-z0-9-]{0,79}\.md", target):
        raise LiteError("invalid_proposal_target")
    return {**record, "page": page}


def approve(vault: Vault, pid: str, *, expected_sha256: str, reviewer: str) -> dict:
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 200:
        raise LiteError("reviewer_required")
    check_text(reviewer)
    with vault.lock():
        proposal = read_proposal(vault, pid)
        core = proposal["core"]
        if core["page_sha256"] != expected_sha256:
            raise LiteError("approval_hash_mismatch")
        validate_evidence(vault, core["evidence"])
        page = proposal["page"].replace(b"status: proposed\n", b"status: reviewed\n", 1)
        accepted_sha = digest(page)
        path = vault.path(core["target"])
        current = digest(read_bytes(path)) if path.exists() else None
        receipt_path = vault.path(f"Journal/{pid}.json")
        if receipt_path.exists():
            receipt = read_json(receipt_path)
            if (receipt["accepted_sha256"] != accepted_sha or receipt["proposal_id"] != pid
                    or receipt.get("target") != core["target"]
                    or receipt.get("evidence") != core["evidence"]):
                raise LiteError("approval_receipt_mismatch")
            return {"state": "already_accepted", "target": str(path),
                    "verification_current": current == accepted_sha, "receipt": str(receipt_path)}
        transaction = vault.path(f".olympus-lite/transactions/{pid}")
        if transaction.exists():
            tx = read_json(transaction / "transaction.json")
            if (tx["proposal_id"] != pid or tx["accepted_sha256"] != accepted_sha
                    or tx.get("target") != core["target"]
                    or tx.get("base_sha256") != core["base_sha256"]
                    or tx.get("evidence") != core["evidence"]
                    or read_bytes(transaction / "page.md") != page):
                raise LiteError("approval_transaction_mismatch")
        else:
            if current != core["base_sha256"]:
                raise LiteError("target_changed_since_proposal")
            tx = {"schema": 1, "kind": "approval", "proposal_id": pid, "target": core["target"],
                  "base_sha256": core["base_sha256"], "accepted_sha256": accepted_sha,
                  "reviewer": reviewer.strip(), "accepted_at": now(), "evidence": core["evidence"],
                  "verification_scope": "exact_revision_and_quote_presence; not_truth_proof"}
            publish_directory(transaction, {"transaction.json": json_bytes(tx), "page.md": page})
        if current not in {core["base_sha256"], accepted_sha}:
            raise LiteError("target_changed_during_acceptance")
        if current != accepted_sha:
            atomic_write(path, page)
        atomic_write(receipt_path, json_bytes(tx))
        navigation = vault.reindex_unlocked()
        return {"state": "accepted", "target": str(path), "receipt": str(receipt_path),
                "verification_current": True, "navigation": navigation}
