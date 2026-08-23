from __future__ import annotations

from pathlib import Path

from server.app import digests
from server.app.digests import cases_hash, digest_paths, pipeline_digest, prompts_digest


def test_digests_are_sha256_and_stable() -> None:
    a, b = pipeline_digest(), pipeline_digest()
    assert a == b and len(a) == 64 and int(a, 16)
    assert prompts_digest() == prompts_digest()


def test_missing_dir_gives_empty_hash(tmp_path: Path) -> None:
    assert prompts_digest(tmp_path / "absent") == cases_hash([])
    assert pipeline_digest(tmp_path / "absent") == cases_hash([])


def test_digest_depends_on_content_and_path(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    d1 = pipeline_digest(tmp_path)
    (tmp_path / "a.py").write_text("x = 2\n")
    d2 = pipeline_digest(tmp_path)
    (tmp_path / "a.py").rename(tmp_path / "b.py")
    (tmp_path / "b.py").write_text("x = 1\n")
    d3 = pipeline_digest(tmp_path)
    assert len({d1, d2, d3}) == 3


def test_pipeline_digest_ignores_prompts_and_pycache(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    d1 = pipeline_digest(tmp_path)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.md").write_text("prompt\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.cpython-313.py").write_text("junk\n")
    assert pipeline_digest(tmp_path) == d1
    assert prompts_digest(tmp_path / "prompts") != cases_hash([])


def test_cases_hash_order_independent(tmp_path: Path) -> None:
    (tmp_path / "guide").mkdir()
    p1 = tmp_path / "guide" / "c1.yaml"
    p2 = tmp_path / "guide" / "c2.yaml"
    p1.write_text("id: c1\n")
    p2.write_text("id: c2\n")
    assert cases_hash([p1, p2]) == cases_hash([p2, p1])
    assert cases_hash([p1]) != cases_hash([p1, p2])
    assert cases_hash([p1], root=tmp_path) != cases_hash([p1], root=tmp_path / "guide")
    assert digests.APP_DIR.name == "app"


def test_non_files_ignored_and_paths_resolved(tmp_path: Path) -> None:
    f = tmp_path / "a.yaml"
    f.write_text("a\n")
    (tmp_path / "dir").mkdir()
    assert cases_hash([f, tmp_path / "dir", tmp_path / "absent.yaml"]) == cases_hash([f])
    assert cases_hash([tmp_path / "dir"]) == cases_hash([])
    assert cases_hash([tmp_path / "dir" / ".." / "a.yaml"]) == cases_hash([f])
    assert cases_hash([f, f]) == cases_hash([f])


def test_path_outside_root_uses_absolute_key(tmp_path: Path) -> None:
    outside = tmp_path / "out" / "c.yaml"
    outside.parent.mkdir()
    outside.write_text("c\n")
    root = tmp_path / "root"
    root.mkdir()
    h = digest_paths([outside], root)
    other = tmp_path / "out2" / "c.yaml"
    other.parent.mkdir()
    other.write_text("c\n")
    assert h != digest_paths([other], root)  # même nom, chemin absolu différent
    assert h == digest_paths([outside], root)
