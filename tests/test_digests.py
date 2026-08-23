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


def _write(app: Path, rel: str, content: str) -> None:
    (app / rel).parent.mkdir(parents=True, exist_ok=True)
    (app / rel).write_text(content)


def test_digest_depends_on_content_and_path(tmp_path: Path) -> None:
    _write(tmp_path, "steps/a.py", "x = 1\n")
    d1 = pipeline_digest(tmp_path)
    _write(tmp_path, "steps/a.py", "x = 2\n")
    d2 = pipeline_digest(tmp_path)
    (tmp_path / "steps" / "a.py").rename(tmp_path / "steps" / "b.py")
    _write(tmp_path, "steps/b.py", "x = 1\n")
    d3 = pipeline_digest(tmp_path)
    assert len({d1, d2, d3}) == 3


def test_pipeline_digest_covers_exactly_the_four_layers(tmp_path: Path) -> None:
    for layer in ("steps", "pipelines", "corpus", "llm"):
        _write(tmp_path, f"{layer}/m.py", "x = 1\n")
    _write(tmp_path, "config.py", "a = 1\n")
    _write(tmp_path, "digests.py", "a = 1\n")
    _write(tmp_path, "domain/d.py", "a = 1\n")
    _write(tmp_path, "api/r.py", "a = 1\n")
    _write(tmp_path, "llm/prompts/p.md", "prompt\n")
    _write(tmp_path, "llm/__pycache__/m.cpython-313.py", "junk\n")
    base_p, base_q = pipeline_digest(tmp_path), prompts_digest(tmp_path / "llm" / "prompts")
    _write(tmp_path, "config.py", "a = 2\n")
    _write(tmp_path, "domain/d.py", "a = 2\n")
    _write(tmp_path, "api/r.py", "a = 2\n")
    assert (pipeline_digest(tmp_path), prompts_digest(tmp_path / "llm" / "prompts")) == (base_p, base_q)
    _write(tmp_path, "llm/prompts/p.md", "prompt 2\n")
    assert pipeline_digest(tmp_path) == base_p
    assert prompts_digest(tmp_path / "llm" / "prompts") != base_q
    for layer in ("steps", "pipelines", "corpus", "llm"):
        before = pipeline_digest(tmp_path)
        _write(tmp_path, f"{layer}/m.py", f"x = '{layer}'\n")
        assert pipeline_digest(tmp_path) != before, layer


def test_prompts_dir_is_under_llm() -> None:
    assert digests.PROMPTS_DIR == digests.APP_DIR / "llm" / "prompts"


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
