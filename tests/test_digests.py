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


def test_le_cases_hash_du_gate_est_celui_du_golden_set_livre() -> None:
    """AD-14 : « deux runs ne sont comparables qu'à hash égal » — vérifié contre `data/manifest.json`.

    Le gate écrit par `evals run --gate` se réclame d'une suite précise. Ce test recalcule le hash de
    cette suite depuis les fichiers du dépôt et le compare à celui que le manifest porte : une
    question-témoin modifiée, ajoutée ou renommée sans relancer le gate se voit ici, et non le jour
    où quelqu'un croira que le gate valide les cas qu'il lit.
    """
    import json

    from server.app.config import REPO_ROOT, Settings
    from server.evals.run import CASES_DIR, charger_cas, suite_du_document

    reglages = Settings(_env_file=None)
    manifest = json.loads((REPO_ROOT / "data" / "manifest.json").read_text("utf-8"))
    for doc_id in (reglages.guide_doc_id, reglages.sinistre_doc_id):
        suite = suite_du_document(reglages, doc_id)
        gate = manifest[doc_id]["gate"]
        assert gate is not None, f"{doc_id} n'a pas de gate : relancer `evals run --gate {doc_id}`"
        # **Le même filtre que `main()`**, sinon les deux calculs de `cases_hash` divergeraient dès
        # qu'un cas d'un autre profil existerait (4.1) : le gate est écrit sur les cas du profil
        # demandé, ce test recalculerait sur toute la suite, et il rougirait sans qu'il y ait de
        # défaut. `refuser_ce_qui_nest_pas_livre` empêche aujourd'hui la cohabitation ; ce filtre est
        # ce qui rendra le contrôle juste le jour où elle sera permise.
        cas = [c for c in charger_cas(CASES_DIR, suites=(suite,)) if c.profile == gate["profile"]]
        assert cas, f"aucun cas au profil {gate['profile']} pour la suite {suite}"
        fichiers = [CASES_DIR / c.suite / f"{c.id}.yaml" for c in cas]
        assert gate["cases_hash"] == cases_hash(fichiers, CASES_DIR), (
            f"le golden set de la suite {suite} a changé depuis le gate de {doc_id} : "
            f"relancer `uv run python -m server.evals.run --gate {doc_id} --profile vertical`")
        assert gate["cases"] == len(cas)


def test_les_gates_du_depot_sont_ceux_de_limage_courante() -> None:
    """AD-7 : un `pipeline_digest`/`prompts_digest`/`model_ids` ≠ l'image ⇒ alerte `gate_perime`.

    L'alerte laisse le document **servi** — c'est ce qu'AD-7 veut — et rien, hors ligne, ne disait
    qu'elle allait se lever : `test_repo_data_loads` charge sans `current=`, et les tests de `/sante`
    n'assertent que des valeurs que `gate_perime` ne change pas. Concrètement, la première story qui
    touche `steps/`, `pipelines/`, `corpus/`, `llm/` ou un prompt rendait les deux gates périmés
    pendant que l'accueil continuait d'annoncer « vertical — 2 cas relus à la main », sans qu'un seul
    test rougisse.

    Ce test est ce rappel, et son message est la procédure : relancer les deux gates. C'est le pendant
    exact du garde-fou d'`ALLOW_UNGATED` posé en 1.6 — un commentaire ne garantit rien, un test si.
    """
    import json

    from server.app.config import REPO_ROOT, Settings
    from server.app.llm.models import TIERS

    reglages = Settings(_env_file=None)
    manifest = json.loads((REPO_ROOT / "data" / "manifest.json").read_text("utf-8"))
    attendus = {"pipeline_digest": pipeline_digest(), "prompts_digest": prompts_digest(),
                "model_ids": dict(TIERS)}
    for doc_id in (reglages.guide_doc_id, reglages.sinistre_doc_id):
        gate = manifest[doc_id]["gate"]
        assert gate is not None, f"{doc_id} n'a pas de gate"
        for champ, attendu in attendus.items():
            assert gate[champ] == attendu, (
                f"{doc_id} : le gate porte un {champ} qui n'est plus celui de l'image — le serveur "
                f"le servira avec l'alerte `gate_perime` pendant que `/` annoncera son profil. "
                f"Relancer : `uv run python -m server.evals.run --gate {doc_id} --profile vertical` "
                f"(≈ 0,05 € par document, clé requise).")
