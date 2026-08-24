"""Matrice d'E/S du runner de questions-témoins (spec 1.10) : AD-14, AD-7, AD-8, AD-9, D2, D4, D5.

Aucun réseau : le pipeline est **doublé**, le corpus est miniature et écrit dans `tmp_path`, et la
seule chose qui touche `data/` est l'écriture du gate — sur une copie. Une ligne de la matrice d'E/S
sans test est une règle non tenue.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from server.app.config import Settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus, load_corpus
from server.app.corpus.text import normalize
from server.app.domain.answer import (
    AbsenceProof,
    Answer,
    AnswerSegment,
    ClaimStatus,
    VerifiedClaim,
    VerifiedQuote,
)
from server.app.domain.document import Document, Node
from server.app.domain.errors import (
    BudgetExceeded,
    InvalidRequest,
    LlmUnavailable,
    Timeout,
)
from server.app.domain.ingest import ManifestEntry
from server.app.domain.trace import StepTrace, Trace
from server.app.domain.verdict import Verdict
from server.evals import run as runner

GUIDE = "mini-guide"
CONTRAT = "mini-contrat"
TEXTE_GUIDE = ("LuxTrust s'obtient au meilleur prix par une banque luxembourgeoise, souvent "
               "gratuitement pour ses clients.")
TEXTE_CONTRAT = ("Les dégâts occasionnés au mobilier assuré par un événement soudain sont couverts, "
                 "même sans embrasement.")


# --- fabriques -----------------------------------------------------------

def _settings(**kw: Any) -> Settings:
    defauts: dict[str, Any] = {"anthropic_api_key": "cle-de-test", "guide_doc_id": GUIDE,
                               "sinistre_doc_id": CONTRAT}
    defauts.update(kw)
    return Settings(_env_file=None, **defauts)


def _document(doc_id: str, kind: str, texte: str, loc: str) -> Document:
    doc = Document(
        doc_id=doc_id, kind=kind, title=f"Doc {doc_id}", edition="2020",
        source_hash="s", ingest_fingerprint="f",
        nodes=[Node(node_id=f"{doc_id}:n1", level=1, title="N1",
                    items=[{"block_id": f"{doc_id}:{loc}:1"}])],
        blocks=[{"block_id": f"{doc_id}:{loc}:1", "loc": loc, "seq": 1, "kind": "para",
                 "text": texte}])
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    return doc


def _corpus() -> tuple[Corpus, Index]:
    docs = {GUIDE: _document(GUIDE, "guide", TEXTE_GUIDE, "ffiche"),
            CONTRAT: _document(CONTRAT, "contrat", TEXTE_CONTRAT, "p34")}
    manifest = {d: ManifestEntry(status="servi", source_hash="s", ingest_fingerprint="f",
                                 document_hash="d", edition="2020") for d in docs}
    corpus = Corpus(documents=docs, manifest=manifest,
                    summaries={d: f"# {d}" for d in docs}, alerts={d: [] for d in docs})
    return corpus, Index(corpus)


def _citation(index: Index, block_id: str, extrait: str) -> VerifiedQuote:
    """Une citation **relue du corpus** : `quote == Block.text[text_start:text_end]` (AD-3)."""
    texte = index.corpus.documents[index.doc_of(block_id)].block(block_id).text
    debut = texte.index(extrait)
    return VerifiedQuote(block_id=block_id, quote=extrait, start=0, end=len(extrait),
                         text_start=debut, text_end=debut + len(extrait))


def _claim(quote: VerifiedQuote, claim_id: str = "c1") -> VerifiedClaim:
    return VerifiedClaim(claim_id=claim_id, text="Une affirmation.", quotes=[quote],
                         status=ClaimStatus(retrouvee=True, pertinente=True, edition="2020"))


def _reponse(claims: list[VerifiedClaim], *, verdict: Verdict | None = None,
             segments: list[AnswerSegment] | None = None) -> Answer:
    return Answer(found=True, complete=True, texte="Une affirmation.",
                  segments=segments if segments is not None
                  else [AnswerSegment(text="Une affirmation.", kind="factuel",
                                      claim_ids=[c.claim_id for c in claims])],
                  claims=claims, verdict=verdict)


def _refus() -> Answer:
    return Answer(found=False, complete=False, texte="",
                  reason=AbsenceProof(kind="hors_perimetre"))


def _trace(pipeline: str = "guide") -> Trace:
    return Trace(request_id="eval", pipeline=pipeline, total_cost_eur=0.01,
                 steps=[StepTrace(name="comprendre", tier="micro")])


class DoublePipeline:
    """Double de `repondre_guide` / `sinistre.run` : rend ou lève ce qu'on lui donne, et note tout."""

    def __init__(self, resultats: list[Any], *, cout: float = 0.02) -> None:
        self.resultats = list(resultats)
        self.cout = cout
        self.appels: list[dict[str, Any]] = []

    async def __call__(self, *args: Any, **kw: Any) -> tuple[Answer, Trace]:
        self.appels.append({"args": args, "kw": kw})
        budget = kw.get("budget")
        if budget is not None:
            # Un vrai pipeline consomme du budget ; le double le simule pour que le plafond de run
            # se mesure sur autre chose qu'un compteur nul.
            budget.cost_eur = round(budget.cost_eur + self.cout, 4)
        suivant = self.resultats.pop(0) if self.resultats else self.resultats
        if isinstance(suivant, BaseException):
            raise suivant
        return suivant


def _contexte(reponses_guide: list[Any], reponses_sinistre: list[Any] | None = None,
              *, settings: Settings | None = None, cout: float = 0.02) -> runner.Contexte:
    corpus, index = _corpus()
    ctx = runner.Contexte(settings=settings or _settings(), index=index, client=object(),
                          pipeline_digest_hex="pd", prompts_digest_hex="pp")
    ctx._guide = DoublePipeline(reponses_guide, cout=cout)       # type: ignore[attr-defined]
    ctx._sinistre = DoublePipeline(reponses_sinistre or [], cout=cout)  # type: ignore[attr-defined]
    return ctx


@pytest.fixture(autouse=True)
def _pipelines_doubles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aucun test de ce module n'appelle un vrai pipeline : les deux sont remplacés par le double
    que le `Contexte` porte (`ctx._guide` / `ctx._sinistre`)."""
    async def guide(*args: Any, **kw: Any) -> Any:
        return await _COURANT["guide"](*args, **kw)

    async def sinistre(*args: Any, **kw: Any) -> Any:
        return await _COURANT["sinistre"](*args, **kw)

    monkeypatch.setattr(runner, "repondre_guide", guide)
    monkeypatch.setattr(runner.pipeline_sinistre, "run", sinistre)


_COURANT: dict[str, Any] = {}


def _armer(ctx: runner.Contexte) -> runner.Contexte:
    _COURANT["guide"] = ctx._guide          # type: ignore[attr-defined]
    _COURANT["sinistre"] = ctx._sinistre    # type: ignore[attr-defined]
    return ctx


# --- cas YAML : le schéma d'AD-14 ----------------------------------------

CAS_GUIDE = """
id: {id}
suite: guide
profile: {profile}
question: "Quelle est la façon la moins chère d'obtenir LuxTrust ?"
lang: fr
expected:
  found: true
  fiche_ids: ["{fiche}"]
mode_attendu: bonne_reponse
truth:
  source: lecture_humaine
  validated_by_expert: false
  note: "relu à la main"
"""

CAS_SINISTRE = """
id: {id}
suite: sinistre
profile: vertical
question: "Ce sinistre est-il couvert ?"
faits:
  description: "Une bougie est tombée sur le canapé."
expected:
  found: true
  verdict: [sous_conditions, ne_tranche_pas]
mode_attendu: bonne_reponse
truth:
  source: lecture_humaine
  validated_by_expert: false
  note: "relu à la main"
"""


def _cases_dir(tmp_path: Path, *, guide: str | None = None, sinistre: str | None = None,
               autres: dict[str, str] | None = None) -> Path:
    racine = tmp_path / "cases"
    for suite in ("guide", "sinistre"):
        (racine / suite).mkdir(parents=True, exist_ok=True)
    if guide is not None:
        (racine / "guide" / "g-luxtrust.yaml").write_text(
            guide.format(id="g-luxtrust", profile="vertical", fiche=f"{GUIDE}:n1"), encoding="utf-8")
    if sinistre is not None:
        (racine / "sinistre" / "s-bougie.yaml").write_text(
            sinistre.format(id="s-bougie"), encoding="utf-8")
    for chemin, contenu in (autres or {}).items():
        cible = racine / chemin
        cible.parent.mkdir(parents=True, exist_ok=True)
        cible.write_text(contenu, encoding="utf-8")
    return racine


# --- lecture et validation stricte des cas (AD-14) ------------------------

def test_les_cas_livres_du_depot_sont_valides() -> None:
    """Le golden set réel : deux cas `vertical`, relus à la main, un par suite (AC de la story)."""
    cas = runner.charger_cas(runner.CASES_DIR)
    assert {c.id for c in cas} == {"g-luxtrust-prix", "s-bougie-canape"}
    assert {c.suite for c in cas} == {"guide", "sinistre"}
    for c in cas:
        assert c.profile == "vertical"
        assert c.truth.source == "lecture_humaine"
        assert c.truth.validated_by_expert is False
        assert c.truth.note.strip(), "un cas relu à la main sans note ne dit pas qui a relu quoi"
        assert c.mode_attendu in runner.LABELS


def test_un_champ_inconnu_est_refuse_en_nommant_le_fichier_et_le_champ(tmp_path: Path) -> None:
    """Matrice : « Cas invalide ⇒ refus **avant** tout appel facturé, message nommant le fichier »."""
    racine = _cases_dir(tmp_path, guide=CAS_GUIDE)
    chemin = racine / "guide" / "g-luxtrust.yaml"
    chemin.write_text(chemin.read_text(encoding="utf-8") + "\nvariante: agentique\n", encoding="utf-8")
    with pytest.raises(runner.RefusDeTourner) as exc:
        runner.charger_cas(racine)
    assert "g-luxtrust.yaml" in str(exc.value) and "variante" in str(exc.value)


def test_une_source_de_verite_inconnue_est_refusee(tmp_path: Path) -> None:
    mauvais = CAS_GUIDE.replace("source: lecture_humaine", "source: intuition")
    racine = _cases_dir(tmp_path, guide=mauvais)
    with pytest.raises(runner.RefusDeTourner) as exc:
        runner.charger_cas(racine)
    assert "truth.source" in str(exc.value)


def test_un_cas_vertical_exige_une_lecture_humaine(tmp_path: Path) -> None:
    """AD-14 : `vertical` = « relus à la main ». C'est ce que le profil annonce à qui lit `/sante`."""
    racine = _cases_dir(tmp_path, guide=CAS_GUIDE.replace("source: lecture_humaine", "source: claude"))
    with pytest.raises(runner.RefusDeTourner) as exc:
        runner.charger_cas(racine)
    assert "lecture_humaine" in str(exc.value)


def test_un_expert_ne_peut_pas_etre_declare(tmp_path: Path) -> None:
    """AD-14 : `validated_by_expert: false`, toujours. Aucun verdict n'est validé par un expert."""
    racine = _cases_dir(tmp_path, guide=CAS_GUIDE.replace("validated_by_expert: false",
                                                          "validated_by_expert: true"))
    with pytest.raises(runner.RefusDeTourner):
        runner.charger_cas(racine)


def test_lidentifiant_doit_etre_le_nom_du_fichier(tmp_path: Path) -> None:
    """C'est le nom de fichier qu'agrège `cases_hash` : un `id` qui en diffère rend le gate illisible."""
    racine = _cases_dir(tmp_path, guide=CAS_GUIDE)
    chemin = racine / "guide" / "g-luxtrust.yaml"
    chemin.write_text(chemin.read_text(encoding="utf-8").replace("id: g-luxtrust", "id: autre-chose"),
                      encoding="utf-8")
    with pytest.raises(runner.RefusDeTourner) as exc:
        runner.charger_cas(racine)
    assert "id" in str(exc.value)


def test_un_yaml_illisible_est_refuse(tmp_path: Path) -> None:
    racine = _cases_dir(tmp_path, autres={"guide/g-casse.yaml": "id: [non fermé\n"})
    with pytest.raises(runner.RefusDeTourner) as exc:
        runner.charger_cas(racine)
    assert "g-casse.yaml" in str(exc.value)


def test_un_cas_guide_ne_porte_pas_de_faits_et_un_cas_sinistre_en_exige(tmp_path: Path) -> None:
    racine = _cases_dir(tmp_path, guide=CAS_GUIDE + '\nfaits:\n  description: "x"\n')
    with pytest.raises(runner.RefusDeTourner) as exc:
        runner.charger_cas(racine)
    assert "faits" in str(exc.value)

    racine2 = _cases_dir(tmp_path / "b", sinistre=CAS_SINISTRE.replace(
        'faits:\n  description: "Une bougie est tombée sur le canapé."\n', ""))
    with pytest.raises(runner.RefusDeTourner) as exc:
        runner.charger_cas(racine2)
    assert "faits" in str(exc.value)


# --- ce qui n'est pas livré est refusé, jamais simulé ---------------------

def test_le_profil_full_est_refuse_en_nommant_sa_story() -> None:
    """Matrice : « Profil non livré ⇒ refus immédiat, code 2, “profil `full` : story 4.1” »."""
    with pytest.raises(runner.RefusDeTourner) as exc:
        runner.refuser_ce_qui_nest_pas_livre([], "full")
    assert "full" in str(exc.value) and "4.1" in str(exc.value)


def test_la_suite_parsing_est_refusee_en_nommant_sa_story(tmp_path: Path) -> None:
    """Matrice : « Suite non livrée ⇒ refus immédiat, code 2, “suite `parsing` : story 4.2” »."""
    cas_parsing = CAS_GUIDE.replace("suite: guide", "suite: parsing")
    racine = _cases_dir(tmp_path, autres={"parsing/p-page9.yaml": cas_parsing.format(
        id="p-page9", profile="vertical", fiche=f"{GUIDE}:n1")})
    with pytest.raises(runner.RefusDeTourner) as exc:
        runner.charger_cas(racine)
    assert "parsing" in str(exc.value) and "4.2" in str(exc.value)


def test_une_suite_deposee_hors_des_suites_livrees_nest_pas_ignoree_en_silence(tmp_path: Path) -> None:
    """`charger_cas` balaie **tous** les dossiers : un golden set muet est pire qu'un golden set rouge."""
    racine = _cases_dir(tmp_path, guide=CAS_GUIDE)
    (racine / "parsing").mkdir()
    (racine / "parsing" / "p-x.yaml").write_text(
        CAS_GUIDE.replace("suite: guide", "suite: parsing").format(
            id="p-x", profile="vertical", fiche=f"{GUIDE}:n1"), encoding="utf-8")
    with pytest.raises(runner.RefusDeTourner):
        runner.charger_cas(racine)


# --- la clé (AD-14) ------------------------------------------------------

def test_sans_cle_le_runner_refuse_avant_tout_chargement(monkeypatch: pytest.MonkeyPatch,
                                                         tmp_path: Path) -> None:
    """Matrice : « refus **avant** tout chargement de corpus, code 2, “les évals exigent une clé” »."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(runner, "construire_contexte", _interdit)
    code = runner.main(["--suite", "guide", "--cases-dir", str(tmp_path), "--data-dir", str(tmp_path)])
    assert code == 2


def _interdit(*args: Any, **kw: Any) -> Any:
    raise AssertionError("le corpus a été chargé alors que la clé manque")


def test_une_variable_posee_vide_fait_foi_sur_le_env_du_poste(monkeypatch: pytest.MonkeyPatch) -> None:
    """`env_ignore_empty=True` fait retomber `Settings` sur `.env` : `ANTHROPIC_API_KEY=` aurait tourné."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert runner.cle_absente(_settings()) is True
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-vraie")
    assert runner.cle_absente(_settings(anthropic_api_key="")) is False
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert runner.cle_absente(_settings()) is False
    assert runner.cle_absente(_settings(anthropic_api_key="")) is True


# --- le jugement (D2) ----------------------------------------------------

def _cas(**kw: Any) -> runner.Cas:
    base: dict[str, Any] = {
        "id": "c", "suite": "guide", "profile": "vertical", "question": "q",
        "expected": {"found": True}, "mode_attendu": "bonne_reponse",
        "truth": {"source": "lecture_humaine", "note": "relu"},
    }
    base.update(kw)
    return runner.Cas.model_validate(base)


def test_une_reponse_conforme_est_une_bonne_reponse() -> None:
    _corpus_, index = _corpus()
    answer = _reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))])
    label, ecarts = runner.juger(_cas(expected={"found": True, "fiche_ids": [f"{GUIDE}:n1"]}),
                                 answer, doc_id=GUIDE, index=index)
    assert (label, ecarts) == ("bonne_reponse", [])


def test_un_bloc_attendu_absent_du_corpus_est_citation_introuvable() -> None:
    """FR34, littéralement : « `block_id` attendu disparu ⇒ `citation_introuvable` »."""
    _corpus_, index = _corpus()
    answer = _reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))])
    label, ecarts = runner.juger(_cas(expected={"found": True, "block_ids": [f"{GUIDE}:disparu:9"]}),
                                 answer, doc_id=GUIDE, index=index)
    assert label == "citation_introuvable"
    assert any("absents du corpus" in e for e in ecarts)


def test_une_citation_que_le_corpus_ne_confirme_pas_est_citation_introuvable() -> None:
    """AD-3 : le texte affiché est relu du corpus. Une quote qui n'y correspond pas est un défaut."""
    _corpus_, index = _corpus()
    quote = _citation(index, f"{GUIDE}:ffiche:1", "LuxTrust")
    menteuse = quote.model_copy(update={"quote": "une phrase que le bloc ne porte pas"})
    answer = _reponse([_claim(menteuse)])
    label, _ = runner.juger(_cas(), answer, doc_id=GUIDE, index=index)
    assert label == "citation_introuvable"


def test_un_segment_factuel_sans_affirmation_survivante_est_claim_non_soutenu() -> None:
    """AD-3 : « tout segment `factuel` référence ≥ 1 claim survivante »."""
    _corpus_, index = _corpus()
    claim = _claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))
    answer = _reponse([claim], segments=[
        AnswerSegment(text="Une affirmation.", kind="factuel", claim_ids=[claim.claim_id]),
        AnswerSegment(text="Une phrase orpheline.", kind="factuel", claim_ids=["c-disparue"])])
    label, ecarts = runner.juger(_cas(), answer, doc_id=GUIDE, index=index)
    assert label == "claim_non_soutenu"
    assert any("sans affirmation survivante" in e for e in ecarts)


def test_un_refus_attendu_trouve_est_un_faux_refus() -> None:
    _corpus_, index = _corpus()
    label, ecarts = runner.juger(_cas(), _refus(), doc_id=GUIDE, index=index)
    assert label == "faux_refus"
    assert any("found=False" in e for e in ecarts)


def test_un_ne_tranche_pas_hors_des_valeurs_admissibles_est_un_faux_refus() -> None:
    """D2 : « ou verdict `ne_tranche_pas` hors des valeurs admissibles »."""
    _corpus_, index = _corpus()
    answer = _reponse([_claim(_citation(index, f"{CONTRAT}:p34:1", "mobilier assuré"))],
                      verdict=Verdict(value="ne_tranche_pas", reason="rien de décisif"))
    cas = _cas(suite="sinistre", faits={"description": "x"},
               expected={"found": True, "verdict": ["couvert", "sous_conditions"]})
    label, ecarts = runner.juger(cas, answer, doc_id=CONTRAT, index=index)
    assert label == "faux_refus"
    assert any("hors des valeurs admissibles" in e for e in ecarts)


def test_des_blocs_venus_dun_autre_document_sont_mauvais_doc() -> None:
    _corpus_, index = _corpus()
    answer = _reponse([_claim(_citation(index, f"{CONTRAT}:p34:1", "mobilier assuré"))])
    label, ecarts = runner.juger(_cas(), answer, doc_id=GUIDE, index=index)
    assert label == "mauvais_doc"
    assert any(CONTRAT in e for e in ecarts)


def test_aucune_fiche_attendue_citee_est_doc_manque() -> None:
    _corpus_, index = _corpus()
    answer = _reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))])
    label, ecarts = runner.juger(_cas(expected={"found": True, "fiche_ids": ["mini-guide:absente"]}),
                                 answer, doc_id=GUIDE, index=index)
    assert label == "doc_manque"
    assert any("fiches attendues non citées" in e for e in ecarts)


def test_le_label_reste_dans_le_vocabulaire_fixe_dad14() -> None:
    """AD-14 : « labels fixes ». Sept, pas huit — ce que le vocabulaire ne dit pas est un `ecart`."""
    assert set(runner.LABELS) == {"bonne_reponse", "mauvais_doc", "doc_manque", "claim_non_soutenu",
                                  "faux_refus", "citation_introuvable", "parsing"}


def test_une_attente_inassouvie_sous_un_bon_label_fait_echouer_le_cas() -> None:
    """D2 : « le gate exige `ok`, pas seulement le label »."""
    _corpus_, index = _corpus()
    answer = _reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))])
    cas = _cas(expected={"found": True, "fiche_ids": [f"{GUIDE}:n1"], "complete": False})
    label, ecarts = runner.juger(cas, answer, doc_id=GUIDE, index=index)
    assert label == "bonne_reponse"
    assert ecarts and any("complete=True" in e for e in ecarts)
    assert runner.Resultat(id="c", suite="guide", label=label, ecarts=ecarts).ok is False


def test_un_label_different_du_mode_attendu_est_un_ecart() -> None:
    _corpus_, index = _corpus()
    cas = _cas(expected={"found": False, "refusal": True}, mode_attendu="bonne_reponse")
    label, ecarts = runner.juger(cas, _refus(), doc_id=GUIDE, index=index)
    assert label == "bonne_reponse" and ecarts == []


# --- exécution : la matrice d'E/S ----------------------------------------

def _executer(ctx: runner.Contexte, cas: list[runner.Cas], *, max_cost: float = 1.0) -> Any:
    import asyncio
    sortie = io.StringIO()
    _armer(ctx)
    return asyncio.run(runner.executer(cas, ctx, max_cost_eur=max_cost, sortie=sortie)), sortie


def test_run_nominal_execute_chaque_cas_par_le_pipeline(tmp_path: Path) -> None:
    """Matrice : « chaque cas exécuté par le pipeline, ligne `id label coût ms`, résumé, code 0 »."""
    corpus, index = _corpus()
    ctx = _armer(_contexte([(_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]),
                             _trace())]))
    cas = [_cas(id="g-luxtrust", expected={"found": True, "fiche_ids": [f"{GUIDE}:n1"]})]
    resultats, sortie = _executer(ctx, cas)
    assert [r.label for r in resultats] == ["bonne_reponse"]
    assert resultats[0].ok is True and resultats[0].cost_eur == 0.02
    texte = sortie.getvalue()
    assert "g-luxtrust" in texte and "bonne_reponse" in texte and "ok" in texte
    # Le pipeline a bien reçu corpus, index et client — comme l'API les lui passe.
    kw = ctx._guide.appels[0]["kw"]      # type: ignore[attr-defined]
    assert kw["corpus"] is ctx.index.corpus and kw["index"] is ctx.index
    assert kw["client"] is ctx.client and kw["doc_id"] == GUIDE
    assert kw["pipeline_digest_hex"] == "pd" and kw["prompts_digest_hex"] == "pp"


def test_le_cas_sinistre_passe_par_le_pipeline_sinistre() -> None:
    """AD-1 : aucun dispatch — la suite décide du pipeline, et le document en découle (D5)."""
    corpus, index = _corpus()
    answer = _reponse([_claim(_citation(index, f"{CONTRAT}:p34:1", "mobilier assuré"))],
                      verdict=Verdict(value="sous_conditions", reason="qualité non établie"))
    ctx = _armer(_contexte([], [(answer, _trace("sinistre"))]))
    cas = [_cas(id="s-bougie", suite="sinistre", faits={"description": "x"},
                expected={"found": True, "verdict": ["sous_conditions", "ne_tranche_pas"]})]
    resultats, _ = _executer(ctx, cas)
    assert resultats[0].ok and resultats[0].verdict == "sous_conditions"
    assert ctx._sinistre.appels[0]["args"][0] == CONTRAT   # type: ignore[attr-defined]
    assert not ctx._guide.appels                            # type: ignore[attr-defined]


@pytest.mark.parametrize("erreur", [Timeout("deadline"), LlmUnavailable("529"),
                                    BudgetExceeded("plafond")])
def test_un_incident_technique_ne_touche_pas_le_manifest(erreur: Exception) -> None:
    """D4 : « un incident n'est pas un verdict » — code 3, manifest intact."""
    ctx = _armer(_contexte([erreur]))
    with pytest.raises(runner.IncidentTechnique) as exc:
        _executer(ctx, [_cas(id="g-luxtrust")])
    assert erreur.code.value in str(exc.value)


def test_un_cas_hors_bornes_du_pipeline_est_un_refus_pas_un_incident() -> None:
    """`invalid_request` est une faute d'écriture du cas : ni verdict, ni incident (code 2)."""
    ctx = _armer(_contexte([InvalidRequest("historique de 12 tours")]))
    with pytest.raises(runner.RefusDeTourner):
        _executer(ctx, [_cas(id="g-luxtrust")])


def test_le_plafond_de_run_arrete_avant_le_cas_suivant() -> None:
    """Matrice : « arrêt avant le cas suivant, manifest non modifié, code 3, message avec le coût »."""
    corpus, index = _corpus()
    bonne = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())
    ctx = _armer(_contexte([bonne, bonne, bonne], cout=0.05))
    with pytest.raises(runner.IncidentTechnique) as exc:
        _executer(ctx, [_cas(id="a"), _cas(id="b"), _cas(id="c")], max_cost=0.05)
    message = str(exc.value)
    assert "plafond de run" in message and "0.0500" in message
    assert "2 cas non exécutés" in message
    # Un seul cas a démarré : l'arrêt est **avant** le suivant, pas au milieu.
    assert len(ctx._guide.appels) == 1   # type: ignore[attr-defined]


def test_le_budget_dun_cas_est_le_reste_du_plafond_de_run() -> None:
    """AD-9 : « en évals, [le plafond par requête] est remplacé par un plafond par run »."""
    corpus, index = _corpus()
    bonne = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())
    ctx = _armer(_contexte([bonne, bonne], cout=0.30))
    _executer(ctx, [_cas(id="a"), _cas(id="b")], max_cost=1.0)
    budgets = [a["kw"]["budget"].max_cost_eur for a in ctx._guide.appels]  # type: ignore[attr-defined]
    assert budgets == [1.0, 0.7]
    # …et surtout : pas le plafond **par requête**, qui aurait fait échouer un cas à 0,30 €.
    assert budgets[0] > _settings().max_cost_eur_per_request


# --- le gate (AD-7, D5) ---------------------------------------------------

def _data_dir(tmp_path: Path) -> Path:
    racine = tmp_path / "data"
    racine.mkdir()
    entrees = {d: {"status": "servi", "source_hash": f"s-{d}", "ingest_fingerprint": f"f-{d}",
                   "document_hash": "d", "edition": "2020", "overlay_hash": None, "gate": None}
               for d in (GUIDE, CONTRAT)}
    (racine / "manifest.json").write_text(json.dumps(entrees, indent=2) + "\n", encoding="utf-8")
    return racine


def test_le_gate_porte_les_empreintes_de_lentree_du_manifest(tmp_path: Path) -> None:
    """AD-7 : les hashes du gate sont ceux de **l'entrée**, jamais des valeurs recalculées ici.

    Recalculées, le loader comparerait deux résultats du même calcul : ils seraient toujours
    d'accord, y compris sur un artefact qui a bougé sans réingestion (`_gate_alerts`).
    """
    racine_cas = _cases_dir(tmp_path, guide=CAS_GUIDE)
    cas = runner.charger_cas(racine_cas, suites=("guide",))
    entry = ManifestEntry(status="servi", source_hash="s-du-manifest",
                          ingest_fingerprint="f-du-manifest", document_hash="d", edition="2020",
                          overlay_hash="o-du-manifest")
    ctx = _contexte([])
    gate = runner.construire_gate(entry, ctx, profil="vertical", cas=cas, cases_dir=racine_cas,
                                  evals_ok=True)
    assert (gate.source_hash, gate.ingest_fingerprint, gate.overlay_hash) == (
        "s-du-manifest", "f-du-manifest", "o-du-manifest")
    assert gate.profile == "vertical" and gate.evals_ok is True and gate.cases == 1
    assert gate.pipeline_digest == "pd" and gate.prompts_digest == "pp"
    assert gate.model_ids and gate.date.endswith("Z")
    # `cases_hash` couvre la suite réellement exécutée (D5).
    from server.app.digests import cases_hash
    assert gate.cases_hash == cases_hash([racine_cas / "guide" / "g-luxtrust.yaml"], racine_cas)


def test_le_gate_change_avec_le_contenu_des_cas(tmp_path: Path) -> None:
    """AD-14 : « deux runs ne sont comparables qu'à hash égal »."""
    racine = _cases_dir(tmp_path, guide=CAS_GUIDE)
    entry = ManifestEntry(status="servi", source_hash="s", ingest_fingerprint="f",
                          document_hash="d", edition="2020")
    ctx = _contexte([])
    avant = runner.construire_gate(entry, ctx, profil="vertical",
                                   cas=runner.charger_cas(racine, suites=("guide",)),
                                   cases_dir=racine, evals_ok=True).cases_hash
    chemin = racine / "guide" / "g-luxtrust.yaml"
    chemin.write_text(chemin.read_text(encoding="utf-8").replace("Quelle", "Comment"), encoding="utf-8")
    apres = runner.construire_gate(entry, ctx, profil="vertical",
                                   cas=runner.charger_cas(racine, suites=("guide",)),
                                   cases_dir=racine, evals_ok=True).cases_hash
    assert avant != apres


def test_la_suite_dun_gate_est_celle_qui_sert_le_document() -> None:
    """D5 : `--gate lux-guide` → suite `guide` ; `--gate {contrat}` → suite `sinistre`."""
    s = _settings()
    assert runner.suite_du_document(s, GUIDE) == "guide"
    assert runner.suite_du_document(s, CONTRAT) == "sinistre"
    with pytest.raises(runner.RefusDeTourner) as exc:
        runner.suite_du_document(s, "un-autre-contrat")
    assert "aucune suite" in str(exc.value)


def test_ecrire_le_gate_ne_touche_que_lentree_visee(tmp_path: Path) -> None:
    racine = _data_dir(tmp_path)
    avant = json.loads((racine / "manifest.json").read_text(encoding="utf-8"))
    entry = ManifestEntry.model_validate(avant[GUIDE])
    ctx = _contexte([])
    racine_cas = _cases_dir(tmp_path, guide=CAS_GUIDE)
    gate = runner.construire_gate(entry, ctx, profil="vertical",
                                  cas=runner.charger_cas(racine_cas, suites=("guide",)),
                                  cases_dir=racine_cas, evals_ok=True)
    runner.ecrire_gate(racine / "manifest.json", GUIDE, gate)
    apres = json.loads((racine / "manifest.json").read_text(encoding="utf-8"))
    assert apres[CONTRAT] == avant[CONTRAT]
    assert apres[GUIDE]["gate"]["profile"] == "vertical"
    assert apres[GUIDE]["gate"]["cases"] == 1
    assert {k: v for k, v in apres[GUIDE].items() if k != "gate"} == \
        {k: v for k, v in avant[GUIDE].items() if k != "gate"}
    # Le fichier reste lisible par le loader.
    assert ManifestEntry.model_validate(apres[GUIDE]).gate is not None


def test_un_manifest_invalide_arrete_tout_sans_rien_ecrire(tmp_path: Path) -> None:
    racine = _data_dir(tmp_path)
    chemin = racine / "manifest.json"
    brut = json.loads(chemin.read_text(encoding="utf-8"))
    brut["un-tiers"] = {"status": "inconnu"}
    chemin.write_text(json.dumps(brut), encoding="utf-8")
    avant = chemin.read_text(encoding="utf-8")
    entry = ManifestEntry.model_validate(brut[GUIDE])
    racine_cas = _cases_dir(tmp_path, guide=CAS_GUIDE)
    gate = runner.construire_gate(entry, _contexte([]), profil="vertical",
                                  cas=runner.charger_cas(racine_cas, suites=("guide",)),
                                  cases_dir=racine_cas, evals_ok=True)
    with pytest.raises(runner.RefusDeTourner):
        runner.ecrire_gate(chemin, GUIDE, gate)
    assert chemin.read_text(encoding="utf-8") == avant


# --- bout en bout par `main()` : les codes de sortie ----------------------

def _corpus_sur_disque(racine: Path) -> None:
    """Un `data/` minimal, lisible par `load_corpus` : deux documents servis, aucun gate."""
    import hashlib
    entrees = {}
    for doc_id, kind, texte, loc in ((GUIDE, "guide", TEXTE_GUIDE, "ffiche"),
                                     (CONTRAT, "contrat", TEXTE_CONTRAT, "p34")):
        dossier = racine / doc_id
        dossier.mkdir(parents=True)
        doc = _document(doc_id, kind, texte, loc)
        octets = json.dumps(doc.model_dump(mode="json", exclude_defaults=True),
                            ensure_ascii=False, sort_keys=True).encode("utf-8")
        (dossier / "document.json").write_bytes(octets)
        (dossier / "summary.md").write_text(f"# {doc_id}", encoding="utf-8")
        entrees[doc_id] = {"status": "servi", "source_hash": "s", "ingest_fingerprint": "f",
                           "document_hash": hashlib.sha256(octets).hexdigest(),
                           "edition": "2020", "overlay_hash": None, "gate": None}
    (racine / "manifest.json").write_text(json.dumps(entrees, indent=2) + "\n", encoding="utf-8")


def _main(tmp_path: Path, argv: list[str], monkeypatch: pytest.MonkeyPatch, *,
          reponses_guide: list[Any] | None = None,
          reponses_sinistre: list[Any] | None = None) -> int:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    if not (data / "manifest.json").is_file():
        _corpus_sur_disque(data)
    cases = _cases_dir(tmp_path, guide=CAS_GUIDE, sinistre=CAS_SINISTRE)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(runner, "Settings", lambda: _settings())
    _COURANT["guide"] = DoublePipeline(reponses_guide or [])
    _COURANT["sinistre"] = DoublePipeline(reponses_sinistre or [])
    return runner.main(argv + ["--cases-dir", str(cases), "--data-dir", str(data)])


def test_gate_nominal_ecrit_le_gate_et_rend_zero(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """AC : deux `--gate` successifs ⇒ `manifest.gate` renseigné, code 0."""
    _corpus_, index = _corpus()
    guide = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())
    sinistre = (_reponse([_claim(_citation(index, f"{CONTRAT}:p34:1", "mobilier assuré"))],
                         verdict=Verdict(value="sous_conditions", reason="r")), _trace("sinistre"))
    assert _main(tmp_path, ["--gate", GUIDE], monkeypatch, reponses_guide=[guide]) == 0
    assert _main(tmp_path, ["--gate", CONTRAT], monkeypatch, reponses_sinistre=[sinistre]) == 0
    manifest = json.loads((tmp_path / "data" / "manifest.json").read_text(encoding="utf-8"))
    for doc_id in (GUIDE, CONTRAT):
        gate = manifest[doc_id]["gate"]
        assert gate["evals_ok"] is True and gate["profile"] == "vertical" and gate["cases"] == 1
        assert set(gate) >= {"profile", "source_hash", "ingest_fingerprint", "cases_hash",
                             "pipeline_digest", "prompts_digest", "model_ids", "evals_ok", "date",
                             "overlay_hash", "cases"}
    # …et le corpus se recharge sans `sans_gate` ni `gate_perime` (AC).
    from server.app.domain.ingest import GateContext
    contexte = GateContext(pipeline_digest=manifest[GUIDE]["gate"]["pipeline_digest"],
                           prompts_digest=manifest[GUIDE]["gate"]["prompts_digest"],
                           model_ids=manifest[GUIDE]["gate"]["model_ids"])
    corpus = load_corpus(tmp_path / "data", allow_ungated=False, current=contexte)
    assert sorted(corpus.documents) == sorted([GUIDE, CONTRAT])
    # `allow_ungated=False` : les deux documents ne sont servis que parce que leur gate suffit (AC).
    for doc_id, alertes in corpus.alerts.items():
        assert "sans_gate" not in alertes and "gate_perime" not in alertes, (doc_id, alertes)


def test_gate_en_echec_de_cas_ecrit_evals_ok_false_et_rend_un(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """Matrice : « un cas rend `faux_refus` ⇒ gate écrit avec `evals_ok: false`, code 1 »."""
    code = _main(tmp_path, ["--gate", GUIDE], monkeypatch, reponses_guide=[(_refus(), _trace())])
    assert code == 1
    manifest = json.loads((tmp_path / "data" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest[GUIDE]["gate"]["evals_ok"] is False
    # …et ce document part en quarantaine `gate_echoue` au prochain démarrage (AD-8).
    corpus = load_corpus(tmp_path / "data", allow_ungated=True)
    assert corpus.quarantine.get(GUIDE) == "gate_echoue"


def test_gate_en_echec_technique_ne_modifie_pas_le_manifest(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Matrice : « manifest non modifié, code 3 — un incident n'est pas un verdict »."""
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    avant = (data / "manifest.json").read_text(encoding="utf-8")
    code = _main(tmp_path, ["--gate", GUIDE], monkeypatch, reponses_guide=[Timeout("deadline")])
    assert code == 3
    assert (data / "manifest.json").read_text(encoding="utf-8") == avant


def test_gate_dun_document_non_servi_est_refuse(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Matrice : « `doc_id` absent du corpus ou en quarantaine ⇒ refus, manifest non modifié, code 2 »."""
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    brut = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    brut[GUIDE]["status"] = "quarantaine"
    (data / "manifest.json").write_text(json.dumps(brut, indent=2) + "\n", encoding="utf-8")
    avant = (data / "manifest.json").read_text(encoding="utf-8")
    assert _main(tmp_path, ["--gate", GUIDE], monkeypatch) == 2
    assert (data / "manifest.json").read_text(encoding="utf-8") == avant


def test_gate_dun_document_inconnu_est_refuse(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    assert _main(tmp_path, ["--gate", "document-inconnu"], monkeypatch) == 2


def test_profil_full_rend_deux_sans_rien_charger(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    assert _main(tmp_path, ["--profile", "full"], monkeypatch) == 2


def test_un_plafond_nul_est_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLAUDE.md : « les évals tournent seulement avec la clé **et un plafond** »."""
    assert _main(tmp_path, ["--suite", "guide", "--max-cost", "0"], monkeypatch) == 2


def test_suite_et_gate_contradictoires_sont_refuses(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    assert _main(tmp_path, ["--gate", GUIDE, "--suite", "sinistre"], monkeypatch) == 2


def test_un_cas_inconnu_est_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _main(tmp_path, ["--case", "n-existe-pas"], monkeypatch) == 2


# --- convention Couches ---------------------------------------------------

def test_le_seuil_du_plafond_de_run_vit_dans_config() -> None:
    """Convention Seuils : aucun nombre en dur dans `run.py`."""
    s = Settings(_env_file=None)
    assert s.evals_max_cost_eur > 0
    assert "evals_max_cost_eur" in s.thresholds()
    source = (Path(runner.__file__)).read_text(encoding="utf-8")
    assert "evals_max_cost_eur" in source
