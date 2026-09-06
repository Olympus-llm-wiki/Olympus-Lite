import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from olympus_lite.migration import inventory, make_plan, apply_plan
from olympus_lite.review import propose, approve
from olympus_lite.storage import Vault, LiteError, digest, fingerprint, read_bytes
from olympus_lite.views import lint, search


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.vault = Vault.create(self.base / "library")
        self.old = self.base / "old"
        self.old.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, rel, text):
        p = self.old / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def source(self, text="При переносе исходная папка сохраняется.", role="primary"):
        p = self.write("source.md", text)
        return self.vault.capture(p, key="test:source", role=role)

    def draft(self, source=None, body="Сохраняем исходную папку.", slug="migration"):
        s = source or self.source()
        evidence = [{"source_id": s["source_id"], "version_id": s["version_id"],
                     "quote": "При переносе исходная папка сохраняется.",
                     "claim": "Оригинал остаётся доступным."}]
        return propose(self.vault, body, title="Перенос материалов", slug=slug,
                       kind="guide", evidence=evidence)

    def accept(self, d):
        return approve(self.vault, d["proposal_id"], expected_sha256=d["expected_sha256"], reviewer="Test owner")

    def test_empty_vault_is_readable_and_healthy(self):
        self.assertIn("Knowledge/index.md", self.vault.path("Home.md").read_text())
        self.assertTrue(lint(self.vault)["ok"])

    def test_pre_rename_vault_remains_usable_without_rewriting_sources(self):
        d = self.draft()
        original_ids = list(self.vault.source_refs())
        root = self.vault.root
        (root / ".olympus-lite").rename(root / ".olympus-light")
        meta = root / ".olympus-light/vault.json"
        info = json.loads(meta.read_text())
        info["product"] = "olympus-light"
        meta.write_text(json.dumps(info))
        self.vault = Vault(root)
        self.accept(d)
        self.assertTrue(lint(self.vault)["ok"])
        self.assertEqual(list(self.vault.source_refs()), original_ids)
        self.assertFalse((root / ".olympus-lite").exists())

    def test_ambiguous_control_directories_are_rejected(self):
        (self.vault.root / ".olympus-light").mkdir()
        with self.assertRaisesRegex(LiteError, "multiple_control_directories"):
            Vault(self.vault.root)

    def test_init_preserves_existing_directory(self):
        p = self.write("important.md", "keep")
        with self.assertRaisesRegex(LiteError, "not_empty"):
            Vault.create(self.old)
        self.assertEqual(p.read_text(), "keep")

    def test_capture_repeat_has_same_identity(self):
        first = self.source()
        second = self.source()
        self.assertEqual(first["version_id"], second["version_id"])
        self.assertEqual(second["state"], "existing")
        self.assertEqual(len(list(self.vault.source_refs())), 1)

    def test_changed_source_keeps_two_versions(self):
        a = self.source("first")
        b = self.source("second")
        self.assertEqual(a["source_id"], b["source_id"])
        self.assertNotEqual(a["version_id"], b["version_id"])
        self.assertEqual(self.vault.source(a["source_id"], a["version_id"])["text"], "first")

    def test_binary_without_text_is_preserved(self):
        p = self.old / "book.pdf"
        p.write_bytes(b"%PDF synthetic\x00\xff")
        result = self.vault.capture(p)
        row = self.vault.source(result["source_id"], result["version_id"])
        self.assertIsNone(row["text"])
        self.assertEqual(Path(row["original_path"]).read_bytes(), p.read_bytes())

    def test_corrupt_original_is_not_an_idempotent_success(self):
        a = self.source()
        row = self.vault.source(a["source_id"], a["version_id"])
        Path(row["original_path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(LiteError, "original_hash"):
            self.source()
        self.assertFalse(lint(self.vault)["ok"])

    def test_known_secret_rejected_without_content_in_diagnostic(self):
        secret = "ghp_" + "A" * 40
        p = self.write("secret.md", secret)
        with self.assertRaises(LiteError) as caught:
            self.vault.capture(p)
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(list(self.vault.source_refs()), [])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO unavailable")
    def test_non_regular_source_fails_without_blocking(self):
        p = self.old / "pipe.md"
        os.mkfifo(p)
        with self.assertRaisesRegex(LiteError, "not_regular_file"):
            self.vault.capture(p)

    def test_inventory_excludes_system_and_hidden_content(self):
        self.write("notes/one.md", "one")
        self.write("AGENTS.md", "override")
        self.write(".private/two.md", "private")
        self.write("script.py", "print('no')")
        got = inventory(self.old)
        self.assertEqual([r["path"] for r in got["files"]], ["notes/one.md"])
        self.assertEqual({r["reason"] for r in got["skipped"]},
                         {"instructions_or_license", "hidden", "unsupported_format"})

    def test_symlink_is_never_imported(self):
        outside = self.base / "outside.md"
        outside.write_text("outside")
        try:
            (self.old / "link.md").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not available")
        got = inventory(self.old)
        self.assertFalse(got["files"])
        self.assertEqual(got["skipped"][0]["reason"], "symlink")

    def test_subset_import_and_repeat_preserve_old_files(self):
        self.write("topic/one.md", "one")
        self.write("topic/two.md", "two")
        self.write("other/three.md", "three")
        before = {p.relative_to(self.old).as_posix(): p.read_bytes() for p in self.old.rglob("*.md")}
        plan = make_plan(self.old, namespace="archive", select=["topic"], exclude=["topic/two.md"])
        self.assertEqual([x["path"] for x in plan["items"]], ["topic/one.md"])
        one = apply_plan(self.vault, plan)
        two = apply_plan(self.vault, plan)
        self.assertEqual((one["captured"], two["captured"], two["existing"]), (1, 0, 1))
        self.assertEqual(before, {p.relative_to(self.old).as_posix(): p.read_bytes() for p in self.old.rglob("*.md")})
        self.assertEqual(lint(self.vault)["knowledge_pages"], 0)

    def test_changed_second_item_blocks_entire_batch(self):
        self.write("a.md", "one")
        b = self.write("b.md", "two")
        plan = make_plan(self.old, namespace="archive", select=["*.md"])
        b.write_text("changed")
        with self.assertRaisesRegex(LiteError, "input_changed"):
            apply_plan(self.vault, plan)
        self.assertEqual(list(self.vault.source_refs()), [])

    def test_plan_tamper_fails(self):
        self.write("one.md", "one")
        plan = make_plan(self.old, namespace="archive", select=["one.md"])
        plan["items"][0]["role"] = "decision"
        with self.assertRaisesRegex(LiteError, "invalid_import_plan"):
            apply_plan(self.vault, plan)

    def test_rehashed_path_traversal_still_fails(self):
        self.write("one.md", "one")
        plan = make_plan(self.old, namespace="archive", select=["one.md"])
        plan["items"][0]["path"] = "../outside.md"
        core = {k: plan[k] for k in ("schema", "root", "namespace", "select", "exclude", "items")}
        plan["plan_id"] = "imp-" + fingerprint(core)
        with self.assertRaisesRegex(LiteError, "invalid_relative_path"):
            apply_plan(self.vault, plan)

    def test_selection_required_and_no_match_is_error(self):
        self.write("one.md", "one")
        with self.assertRaisesRegex(LiteError, "selection_required"):
            make_plan(self.old, namespace="archive", select=[])
        with self.assertRaisesRegex(LiteError, "selection_is_empty"):
            make_plan(self.old, namespace="archive", select=["absent"])

    def test_legacy_trust_labels_remain_historical(self):
        self.write("idea.md", "---\nstatus: canonical\nverified: true\n---\nМожет быть, создадим проект.")
        plan = make_plan(self.old, namespace="archive", select=["idea.md"], role="discussion")
        result = apply_plan(self.vault, plan)["results"][0]
        row = self.vault.source(result["source_id"], result["version_id"])
        self.assertIn("verified: true", row["text"])
        self.assertFalse(row["content"]["owner_decision_confirmed"])
        self.assertFalse(search(self.vault, "проект"))
        got = search(self.vault, "проект", include_history=True)
        self.assertEqual(got[0]["role"], "discussion")
        self.assertFalse(got[0]["verified_fact"])

    def test_decisions_are_excluded_by_default(self):
        self.source("Историческое решение о проекте.", role="decision")
        self.assertEqual(search(self.vault, "решение"), [])
        self.assertEqual(len(search(self.vault, "решение", include_history=True)), 1)

    def test_russian_search_and_casefold(self):
        self.source("Исходная ПАПКА остаётся доступной.")
        self.assertEqual(len(search(self.vault, "исходная папка")), 1)
        self.assertEqual(search(self.vault, "невстречающееся слово"), [])

    def test_missing_quote_rejected(self):
        s = self.source("Something else")
        with self.assertRaisesRegex(LiteError, "quote_not_found"):
            self.draft(s)

    def test_proposal_is_not_published_until_acceptance(self):
        d = self.draft()
        self.assertFalse(self.vault.path(d["target"]).exists())
        self.assertTrue(self.vault.path(f"Drafts/{d['proposal_id']}/page.md").exists())
        got = self.accept(d)
        self.assertTrue(got["verification_current"])
        self.assertTrue(lint(self.vault)["ok"])
        self.assertEqual(self.accept(d)["state"], "already_accepted")

    def test_wrong_approval_hash_rejected(self):
        d = self.draft()
        with self.assertRaisesRegex(LiteError, "approval_hash_mismatch"):
            approve(self.vault, d["proposal_id"], expected_sha256="0" * 64, reviewer="Owner")
        self.assertFalse(self.vault.path(d["target"]).exists())

    def test_draft_edit_requires_new_proposal(self):
        d = self.draft()
        Path(d["path"]).write_text("manually changed")
        with self.assertRaisesRegex(LiteError, "proposal_changed"):
            self.accept(d)

    def test_source_corrupted_after_proposal_blocks_acceptance(self):
        s = self.source()
        d = self.draft(s)
        row = self.vault.source(s["source_id"], s["version_id"])
        Path(row["original_path"]).write_text("changed")
        with self.assertRaisesRegex(LiteError, "original_hash"):
            self.accept(d)
        self.assertFalse(self.vault.path(d["target"]).exists())

    def test_target_manual_edit_is_not_overwritten(self):
        d = self.draft()
        self.accept(d)
        update = self.draft(body="Обновлённая версия страницы.")
        path = self.vault.path(d["target"])
        path.write_text("User's manual change")
        with self.assertRaisesRegex(LiteError, "target_changed"):
            self.accept(update)
        self.assertEqual(path.read_text(), "User's manual change")
        self.assertIn("review_stale", {i["code"] for i in lint(self.vault)["issues"]})

    def test_acceptance_recovers_after_page_write(self):
        d = self.draft()
        import olympus_lite.review as review
        original = review.atomic_write

        def fail_receipt(path, data):
            if path.parent.name == "Journal":
                raise OSError("simulated interruption")
            return original(path, data)

        with patch.object(review, "atomic_write", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.accept(d)
        target = self.vault.path(d["target"])
        previous = target.stat().st_mtime_ns
        got = self.accept(d)
        self.assertEqual(got["state"], "accepted")
        self.assertEqual(target.stat().st_mtime_ns, previous)
        self.assertTrue(lint(self.vault)["ok"])

    def test_catalog_manual_edit_is_preserved(self):
        catalog = self.vault.path("Sources/index.md")
        catalog.write_text("Manual content")
        result = self.vault.reindex()
        self.assertIn("Sources/index.md", result["conflicts"])
        self.assertEqual(catalog.read_text(), "Manual content")

    def test_capture_reports_navigation_conflict_but_keeps_source(self):
        self.vault.path("Sources/index.md").write_text("Manual catalog")
        result = self.source()
        self.assertEqual(result["state"], "captured")
        self.assertIn("Sources/index.md", result["navigation"]["conflicts"])
        self.assertEqual(len(list(self.vault.source_refs())), 1)

    def test_import_repeat_detects_bad_receipt(self):
        self.write("note.md", "one")
        plan = make_plan(self.old, namespace="test", select=["note.md"])
        result = apply_plan(self.vault, plan)
        p = Path(result["receipt"])
        record = json.loads(p.read_text())
        record["items"] = []
        p.write_text(json.dumps(record))
        with self.assertRaisesRegex(LiteError, "import_receipt_mismatch"):
            apply_plan(self.vault, plan)

    def test_missing_approved_source_is_visible(self):
        s = self.source()
        d = self.draft(s)
        self.accept(d)
        row = self.vault.source(s["source_id"], s["version_id"])
        Path(row["original_path"]).unlink()
        result = lint(self.vault)
        self.assertFalse(result["ok"])
        self.assertIn("page_evidence_unavailable", {i["code"] for i in result["issues"]})

    def test_concurrent_capture_is_idempotent(self):
        from concurrent.futures import ThreadPoolExecutor
        p = self.write("parallel.md", "one source")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: Vault(self.vault.root).capture(p, key="parallel"), range(2)))
        self.assertEqual({r["state"] for r in results}, {"captured", "existing"})
        self.assertEqual(len(list(self.vault.source_refs())), 1)

    def test_vault_copy_works_without_original_location_or_lock_database(self):
        import shutil
        d = self.draft()
        self.accept(d)
        copied = self.base / "copied-library"
        shutil.copytree(self.vault.root, copied)
        self.vault.root.rename(self.base / "original-library-moved")
        (copied / ".olympus-lite/lock.sqlite3").unlink()
        restored = Vault(copied)
        restored.reindex()
        self.assertTrue(lint(restored)["ok"])
        results = search(restored, "перенос")
        self.assertTrue(results)
        self.assertTrue(all(Path(r["absolute_path"]).is_relative_to(copied) for r in results))


class OfflineCLITest(unittest.TestCase):
    def test_cli_cycle_with_socket_disabled(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            blocker = base / "blocker"
            blocker.mkdir()
            (blocker / "sitecustomize.py").write_text(
                "import socket\ndef deny(*a, **k): raise RuntimeError('network forbidden')\n"
                "socket.socket = deny\nsocket.create_connection = deny\n")
            env = dict(os.environ, PYTHONPATH=str(blocker))
            for key in list(env):
                if any(x in key.upper() for x in ("HINDSIGHT", "OPENAI", "ANTHROPIC")):
                    env.pop(key)

            def run(*args):
                result = subprocess.run([sys.executable, str(repo / "olympus.py"), *map(str, args)],
                    cwd=base, env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            vault = base / "vault"
            run("init", vault)
            old = base / "legacy"
            old.mkdir()
            (old / "note.md").write_text("Пример: оригиналы сохраняются.")
            self.assertEqual(len(run("inventory", old)["files"]), 1)
            plan = base / "plan.json"
            run("plan", old, "--namespace", "demo", "--select", "note.md", "--output", plan)
            result = run("--vault", vault, "import", plan)
            source = result["results"][0]
            self.assertEqual(len(run("--vault", vault, "search", "оригиналы")), 1)
            body = base / "body.md"
            body.write_text("Исходные материалы остаются доступными.")
            evidence = base / "evidence.json"
            evidence.write_text(json.dumps([{"source_id": source["source_id"],
                "version_id": source["version_id"], "claim": "Материалы сохраняются.",
                "quote": "оригиналы сохраняются"}], ensure_ascii=False))
            draft = run("--vault", vault, "propose", body, "--title", "Сохранность", "--slug", "preservation",
                        "--evidence", evidence)
            run("--vault", vault, "approve", draft["proposal_id"], "--expected-sha256", draft["expected_sha256"],
                "--reviewer", "Synthetic demo owner")
            self.assertTrue(run("--vault", vault, "lint")["ok"])


if __name__ == "__main__":
    unittest.main()
