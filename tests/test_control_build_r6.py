"""Pure source artifact tests; no Docker execution or environment changes."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from control_plane.sandbox_build_entry import build, gather


class ArtifactBuildTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="r6-artifact-"))
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "card_workflow").mkdir()
        (self.source / "card_workflow/main.py").write_text("raise RuntimeError('candidate must not execute')\n")
        self.output = self.root / "output"
        self.output.mkdir()

    def test_reproducible_bytes_and_manifest(self):
        one = build(self.source, self.output)
        other = self.root / "other"
        other.mkdir()
        two = build(self.source, other)
        self.assertEqual(one, two)
        with zipfile.ZipFile(self.output / one["artifact"]) as archive:
            metadata = json.loads(archive.read("BUILD-MANIFEST.json"))
            data = archive.read("card_workflow/main.py")
        self.assertEqual(metadata["source_manifest"]["card_workflow/main.py"], hashlib.sha256(data).hexdigest())
        self.assertFalse(one["image_built"])
        self.assertFalse(one["release_approved"])

    def test_invalid_python_refuses_artifact(self):
        (self.source / "card_workflow/main.py").write_text("def invalid(:\n")
        with self.assertRaises(SyntaxError):
            build(self.source, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_existing_output_preserved(self):
        first = build(self.source, self.output)
        before = (self.output / first["artifact"]).read_bytes()
        with self.assertRaises(ValueError):
            build(self.source, self.output)
        self.assertEqual(before, (self.output / first["artifact"]).read_bytes())

    def test_hardlink_rejected(self):
        os.link(str(self.source / "card_workflow/main.py"), str(self.source / "card_workflow/copy.py"))
        with self.assertRaises(ValueError):
            gather(self.source)

    def test_symlink_rejected(self):
        (self.source / "card_workflow/escape.py").symlink_to(self.root / "outside")
        with self.assertRaises(ValueError):
            gather(self.source)

    def test_fifo_rejected_without_opening(self):
        os.mkfifo(str(self.source / "card_workflow/fifo.py"))
        with self.assertRaises(ValueError):
            gather(self.source)

    def test_symlink_directory_rejected(self):
        (self.source / "card_workflow/escape").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            gather(self.source)

    def test_depth_bound(self):
        path = self.source / "card_workflow"
        for _ in range(25):
            path = path / "nested"
            path.mkdir()
        with self.assertRaises(ValueError):
            gather(self.source)

    def test_directory_enumeration_errors_propagate(self):
        from unittest.mock import patch
        with patch("control_plane.sandbox_build_entry.os.listdir", side_effect=PermissionError("synthetic denial")):
            with self.assertRaises(PermissionError):
                gather(self.source)

    def test_private_file_rejected(self):
        (self.source / "card_workflow/.env").write_text("synthetic-only")
        with self.assertRaises(ValueError):
            gather(self.source)

    def test_control_recipe_rejected(self):
        (self.source / "control_plane").mkdir()
        with self.assertRaises(ValueError):
            gather(self.source)

    def test_empty_source_refused(self):
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(ValueError):
            build(empty, self.output)


if __name__ == "__main__":
    unittest.main()
