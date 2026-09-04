from __future__ import annotations

from pathlib import Path

import pytest

from server.app.llm import models

ROOT = Path(__file__).resolve().parents[1]


def test_tiers_and_step_assignment() -> None:
    assert set(models.TIERS) == {"ingest", "reason", "micro"}
    assert models.TIERS["ingest"].startswith("claude-opus-5")
    assert models.TIERS["reason"].startswith("claude-sonnet-5")
    assert models.TIERS["micro"].startswith("claude-haiku-4-5")
    assert models.STEP_TIERS == {"comprendre": "reason", "retrouver": "reason",
                                 "rediger": "reason", "verifier": "reason",
                                 "restituer": None, "ingest": "ingest"}


def test_documentation_active_aligne_le_plancher_sonnet_des_choix_semantiques() -> None:
    readme = (ROOT / "README.md").read_text("utf-8")
    architecture = (ROOT / "docs" / "architecture.md").read_text("utf-8")

    assert "Sonnet au minimum pour tout choix sémantique" in readme
    assert "Sonnet au minimum pour tout choix sémantique" in architecture
    assert "Un seul appel `reason`, toujours." in readme
    assert "Un seul appel `micro`, toujours." not in readme
    assert "`micro` pour comprendre et vérifier" not in architecture


# Formulations qui attribuent un appel **servi** de *comprendre* ou de *vérifier* au tier `micro`.
# AD-9 amendé (02/09/2026) : Sonnet `reason` est le plancher de tout choix sémantique servi, et
# `config.py` refuse la descente sans `baseline_tiers=true` hors production. `micro` reste un axe
# d'évaluation légitime — c'est pourquoi la garde porte sur ces tournures-là, et non sur le mot seul :
# « matrice baseline (`micro`/`reason`) », « `micro` reste expérimental » ou un `StepTrace` synthétique
# restent vrais. Ce qui ne peut plus revenir, c'est l'attribution du chemin servi.
FORMULATIONS_PERIMEES = ("appel micro", "appels micro", "appel est micro",
                         "contrôle micro", "micro groupé", "micro_call")

# Journaux de mesures datées (`docs/tests-live.md`, `.memlog.md`, baselines d'évals) et tests
# `*live.py` sont hors garde : ils consignent ce qui a réellement tourné, jamais ce que le produit
# sert aujourd'hui. `web/` a sa propre surface et son propre cycle.
# `README.md` est hors balayage pour une raison de fond, pas par oubli : sa ligne 50 dit « leur
# contrôle `micro` par retraduction », et ce contrôle-là **est** un appel `micro`, distinct des
# étapes servies. Y appliquer la garde rendrait rouge une phrase vraie. Ce que le README affirme du
# plancher servi est déjà verrouillé au-dessus, par
# `test_documentation_active_aligne_le_plancher_sonnet_des_choix_semantiques`.
SURFACES_ACTIVES_SUFFIXES = {".py", ".md", ".yaml", ".yml"}

# Deux artefacts portent la formulation périmée **et** un verrou d'identité à l'octet ; les réécrire
# rendrait rouge une porte réservée, pas une dette de lecture :
#   - `server/evals/cases/guide/g-luxtrust-prix.yaml` consigne une mesure datée du 2026-08-24 et ses
#     octets sont figés par `test_evals_run.py::test_les_cinq_verticaux_restent_byte_identiques`;
#   - l'annexe de `docs/choix-et-limites.md`, sous `<details>`, s'annonce « registre historique
#     conservé à l'identique » et son SHA-256 est figé par
#     `test_docs_choix_limites.py::test_l_annexe_technique_historique_est_repliable_et_byte_identique`.
# La garde s'arrête donc à la frontière que ces documents déclarent eux-mêmes.
ARTEFACTS_FIGES_A_L_OCTET = (Path("server") / "evals" / "cases" / "guide" / "g-luxtrust-prix.yaml",)


def _surfaces_actives() -> list[tuple[Path, str]]:
    """Les surfaces où une mention du chemin servi engage le produit d'aujourd'hui."""
    chemins = [p for p in sorted((ROOT / "server").rglob("*"))
               if p.is_file() and p.suffix in SURFACES_ACTIVES_SUFFIXES
               and p.relative_to(ROOT) not in ARTEFACTS_FIGES_A_L_OCTET]
    chemins += [ROOT / "docs" / "architecture.md"]
    chemins += [p for p in sorted((ROOT / "tests").glob("test_*.py"))
                if not p.name.endswith("live.py") and p.name != Path(__file__).name]
    surfaces = [(p, p.read_text("utf-8")) for p in chemins]

    # `choix-et-limites.md` déclare lui-même sa frontière : la synthèse active, puis l'annexe figée.
    limites = (ROOT / "docs" / "choix-et-limites.md").read_text("utf-8")
    assert "<details>" in limites, "l'annexe historique doit rester repliable et identifiable"
    surfaces.append((ROOT / "docs" / "choix-et-limites.md", limites[:limites.index("<details>")]))
    return surfaces


def test_aucune_surface_active_nattribue_un_appel_servi_au_tier_micro() -> None:
    """Le verrou documentaire du plancher Sonnet, étendu aux mentions du chemin servi.

    Une phrase qui décrit *comprendre* ou *vérifier* comme un appel `micro` est fausse depuis
    l'amendement AD-9 : elle coûte une lecture de plus à qui doit décider sur pièce.
    """
    surfaces = _surfaces_actives()
    assert len(surfaces) > 50  # la garde balaie bien les surfaces actives, pas une liste vide

    fautes = []
    for chemin, contenu in surfaces:
        texte = contenu.replace("`", "").replace("*", "").lower()
        for numero, ligne in enumerate(texte.split("\n"), 1):
            fautes += [f"{chemin.relative_to(ROOT)}:{numero} — {formulation}"
                       for formulation in FORMULATIONS_PERIMEES if formulation in ligne]
    assert fautes == []


def test_effort_par_prompt_publie_la_derogation_sinistre() -> None:
    # Deux dérogations depuis T10 (03/09/2026) : la rédaction sinistre transcrit des clauses déjà
    # retrouvées, et le vérificateur sinistre est **redescendu à `low`** — à `medium`, la mesure
    # non censurée d'A16 sur `28366ad` montre une réflexion qui sature son plafond de 6 144 sans
    # rendre de JSON, pour 120 s et 0,18 € par vérification (voir `llm/models.py`). Le mode d'échec
    # qui avait fait remonter l'effort à T1c est désormais constaté par le code
    # (`hors_objet_incoherent`, T1f), pas acheté en profondeur de réflexion.
    #
    # **Trois depuis L1l (04/09/2026)** : le vérificateur du **guide** rejoint les deux autres, sur
    # exactement la même mesure. Rejeu L1j, `.audit/llm-calls.jsonl` run `b8fe51e1` — l'appel rend
    # `output_tokens = 6 144` **dont `thinking_tokens = 6 144`**, `stop_reason = max_tokens`, zéro
    # caractère de JSON, puis expire à la relance. Ce n'était pas la taille de l'entrée : c'était
    # `medium`, la troisième fois.
    assert getattr(models, "EFFORT_PAR_PROMPT", None) == {"rediger_sinistre": "low",
                                                         "verifier_sinistre": "low",
                                                         "verifier": "low"}
    # La conséquence, épinglée ici parce que c'est elle qui compte : l'appel ne part **pas** à
    # l'effort du tier, qui reste `medium` pour les autres prompts du palier.
    assert models.EFFORT[models.STEP_TIERS["verifier"]] == "medium"
    assert models.EFFORT_PAR_PROMPT["verifier"] != models.EFFORT[models.STEP_TIERS["verifier"]]


def _settings(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    from server.app import config

    monkeypatch.setattr(config, "get_settings", lambda: config.Settings(_env_file=None, anthropic_api_key=key))


def test_check_without_key_exits_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    _settings(monkeypatch, "")
    assert models.main(["--check"]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_check_exit_codes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    _settings(monkeypatch, "k")

    async def all_ok(api_key: str) -> dict[str, bool]:
        return {m: True for m in models.TIERS.values()}

    async def one_missing(api_key: str) -> dict[str, bool]:
        return {m: m != models.TIERS["micro"] for m in models.TIERS.values()}

    async def boom(api_key: str) -> dict[str, bool]:
        raise RuntimeError("401 authentication_error")

    monkeypatch.setattr(models, "check_models", all_ok)
    assert models.main(["--check"]) == 0
    assert capsys.readouterr().out.count("OK") == 3
    monkeypatch.setattr(models, "check_models", one_missing)
    assert models.main(["--check"]) == 1
    assert "ABSENT" in capsys.readouterr().out
    monkeypatch.setattr(models, "check_models", boom)
    assert models.main(["--check"]) == 3
    assert "401" in capsys.readouterr().err
