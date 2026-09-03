"""Matrice d'E/S du runner de questions-témoins (spec 1.10) : AD-14, AD-7, AD-8, AD-9, D2, D4, D5.

Aucun réseau : le pipeline est **doublé**, le corpus est miniature et écrit dans `tmp_path`, et la
seule chose qui touche `data/` est l'écriture du gate — sur une copie. Une ligne de la matrice d'E/S
sans test est une règle non tenue.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from server.app.config import Settings
from server.app.llm.pricing import estimate_run_majorant
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus, load_corpus
from server.app.corpus.text import normalize
from server.app.domain.answer import (
    AbsenceProof,
    Answer,
    AnswerSegment,
    ClaimStatus,
    LecturePartielle,
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
from server.app.domain.trace import LLMCall, StepTrace, Trace, Usage
from server.app.domain.verdict import Verdict
from server.evals import baselines
from server.evals import run as runner
from server.evals.espace import GENERATIONS, REPERTOIRE_ESPACE
from server.evals.plancher import charger_plancher
from server.evals.espace import EspacePublie
from tests.helpers_espace import CIBLES_STANDARD, poser_espace

GUIDE = "mini-guide"
CONTRAT = "mini-contrat"
TROISIEME = "troisieme-contrat"
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


def _document(doc_id: str, kind: str, texte: str, loc: str, *,
              block_kind: str = "para", kind_source: str | None = None) -> Document:
    doc = Document(
        doc_id=doc_id, kind=kind, title=f"Doc {doc_id}", edition="2020",
        source_hash="s", ingest_fingerprint="f",
        nodes=[Node(node_id=f"{doc_id}:n1", level=1, title="N1",
                    items=[{"block_id": f"{doc_id}:{loc}:1"}])],
        blocks=[{"block_id": f"{doc_id}:{loc}:1", "loc": loc, "seq": 1, "kind": block_kind,
                 "kind_source": kind_source, "text": texte}])
    for b in doc.blocks:
        b.text_norm = normalize(b.text)
    return doc


def _corpus() -> tuple[Corpus, Index]:
    docs = {GUIDE: _document(GUIDE, "guide", TEXTE_GUIDE, "ffiche"),
            CONTRAT: _document(CONTRAT, "contrat", TEXTE_CONTRAT, "p34",
                               block_kind="garantie", kind_source="manual")}
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


def _claim(quote: VerifiedQuote, claim_id: str = "c1", *,
           applicable: str | None = None) -> VerifiedClaim:
    return VerifiedClaim(claim_id=claim_id, text="Une affirmation.", quotes=[quote],
                         status=ClaimStatus(retrouvee=True, pertinente=True, applicable=applicable,
                                            edition="2020"))


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


def _trace(pipeline: str = "guide", *, variant: str | None = None,
           cost_eur_original: float = 0.0) -> Trace:
    calls = ([LLMCall(model="modele-test", usage=Usage(
        cost_eur=cost_eur_original, cost_eur_original=cost_eur_original))]
        if cost_eur_original else [])
    # La variante par défaut de la suite, lue sur le runner : depuis la story 4.2d le sinistre navigue
    # lui aussi par outils, et un double qui trace `deterministe` mentirait à la garde de cohérence.
    variant = variant or runner.DEFAUT_PAR_SUITE[pipeline]
    return Trace(request_id="eval", pipeline=pipeline, variant=variant, total_cost_eur=0.01,
                 steps=[StepTrace(name="comprendre", tier="micro", calls=calls)])


class DoublePipeline:
    """Double de `repondre_guide` / `sinistre.run` : rend ou lève ce qu'on lui donne, et note tout."""

    def __init__(self, resultats: list[Any], *, cout: float = 0.02) -> None:
        self.resultats = list(resultats)
        self.cout = cout
        self.appels: list[dict[str, Any]] = []

    async def __call__(self, *args: Any, **kw: Any) -> tuple[Answer, Trace]:
        self.appels.append({"args": args, "kw": kw})
        budget = kw.get("budget")
        if budget is not None and budget.max_cost_eur <= 0 and self.cout > 0:
            raise BudgetExceeded("aucun budget restant pour un miss cache")
        if budget is not None:
            # Un vrai pipeline consomme du budget ; le double le simule pour que le plafond de run
            # se mesure sur autre chose qu'un compteur nul.
            budget.cost_eur = round(budget.cost_eur + self.cout, 4)
        if not self.resultats:
            # Un appel non prévu rendait `[]`, que l'appelant dépaquetait en `answer, trace` — une
            # `ValueError` obscure à cent lignes de la cause. Le double dit ce qui s'est passé.
            raise AssertionError(f"appel n° {len(self.appels)} non prévu par ce double "
                                 f"(args={args!r}, doc_id={kw.get('doc_id')!r})")
        suivant = self.resultats.pop(0)
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
scenario: "parcours miniature"
famille: parcours
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
        reference = racine.parent / "reference"
        reference.mkdir(exist_ok=True)
        (reference / "utilite.yaml").write_text(
            "kind: utilite_guide\nversion: 1\nreferences:\n"
            "  - case_id: g-luxtrust\n"
            "    ordre_juste: [Lire la fiche]\n"
            "    documents_cites: [Fiche miniature]\n"
            "    interlocuteur: LuxTrust\n"
            "    provenance: fixture locale\n"
            "    countersigned_by: null\n", encoding="utf-8")
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
    """Le golden set 4.2 tient ses volumes, familles, provenances et réserves humaines."""
    cas = runner.charger_cas(runner.CASES_DIR)
    guide = [c for c in cas if c.suite == "guide"]
    sinistre = [c for c in cas if c.suite == "sinistre"]
    parsing = [c for c in cas if c.suite == "parsing"]
    # 36 depuis les deux trous de couverture comblés : `g-emploi-adem-fr` et `g-ecole-inscription-fr`.
    assert len(guide) == 36
    assert len(sinistre) == 16
    assert len(parsing) == 11
    assert sum(c.profile == "vertical" for c in cas) == 5
    assert {c.famille for c in guide if c.profile == "full"} == {
        "parcours", "meteo", "suivi", "multilingue", "trois_fiches", "hors_guide",
    }
    assert {c.famille for c in sinistre if c.profile == "full"} == {
        "absurde", "multiple", "hors_habitation", "vide", "contradictoire",
        "clairement_couvert", "sejour_temporaire",
        "telephone_vacances", "exclusion_animale", "perte_exploitation",
        "voies_garantie", "acte_volontaire",
    }
    meteo = [c for c in guide if c.famille == "meteo"]
    assert {c.lang for c in meteo} == {"fr", "en", "de"}
    # **Deux fiches n'étaient interrogées que dans une langue étrangère.** `femploi` ne l'était
    # qu'en allemand (`g-lang-de-adem`) et `fecole` qu'en anglais et en portugais
    # (`g-lang-en-ecole`, `g-lang-pt-ecole`) : le français, la langue de travail du produit et
    # celle de la démonstration, n'avait aucun cas sur ni l'une ni l'autre. Un cas multilingue
    # mesure la retraduction, pas le rappel en français — les deux ne se remplacent pas.
    for fiche in ("lux-guide:femploi", "lux-guide:fecole"):
        en_francais = [c for c in guide
                       if c.lang == "fr" and fiche in (c.expected.fiche_ids or [])]
        assert en_francais, fiche
    assert any(c.famille == "telephone_vacances" for c in sinistre)
    assert any("non_couvert" in c.expected.verdict for c in sinistre)
    assert {c.doc_id for c in parsing} == {
        "axa-lu-optihome-2017", "baloise-lu-home-2-2024",
    }
    baloise = [c for c in cas if c.id.startswith("b-")]
    assert {c.doc_id for c in baloise} == {"baloise-lu-home-2-2024"}
    for c in cas:
        assert c.truth.validated_by_expert is False
        signature = c.truth.countersigned_by
        assert signature is None or (isinstance(signature, str) and bool(signature.strip()))
        assert c.truth.note.strip()
        assert c.mode_attendu in runner.LABELS
    references = runner.charger_references(cas, runner.REFERENCE_DIR)
    assert len(references.files) == 2 and len(references.digest) == 64
    brut_retraductions = yaml.safe_load((runner.REFERENCE_DIR / "retraductions.yaml").read_text("utf-8"))
    controles = runner.FichierRetraduction.model_validate(brut_retraductions).references
    assert all(not hasattr(controle, "retraduction_fr") for controle in controles)
    assert all((runner.REPO_ROOT / controle.fixture).is_file() for controle in controles)
    assert all(controle.resultat == "fidele" and controle.ecarts == []
               and "due" in controle.reserve_signature for controle in controles)


def test_les_cinq_verticaux_restent_byte_identiques() -> None:
    attendus = {
        "guide/g-luxtrust-prix.yaml": "f2e571839b87973ba6507343558a64cd2eb136c357f84f2fbe6e65cec78f58df",
        "sinistre/s-bougie-canape.yaml": "207ebc073d8e32869737d1e609810e17f0b065d8c4c78776b41d4d06c56e78c2",
        "sinistre/baloise-lu-home-2-2024/b-bougie-canape.yaml": "6b98d6906df82d303c8a41c1226b07eebcfb50d84dc2b00144403f7cd92395aa",
        "sinistre/baloise-lu-home-2-2024/b-congelateur.yaml": "89c243e258152d9aded0a33f9de48a5998704384867b15718c9be600b8ecde6f",
        "sinistre/baloise-lu-home-2-2024/b-invite-cigarette.yaml": "a6e19c8872db005e4152594844c45608538660088c16ae4e0e6504208a8e15b8",
    }
    for relatif, attendu in attendus.items():
        assert hashlib.sha256((runner.CASES_DIR / relatif).read_bytes()).hexdigest() == attendu


def test_clarification_ne_change_ni_les_attentes_ni_les_verdicts_verticaux() -> None:
    """Le nouveau champ est absent des cinq témoins historiques et ne les requalifie pas."""
    verticaux = {c.id: c for c in runner.charger_cas(runner.CASES_DIR)
                 if c.profile == "vertical"}
    assert set(verticaux) == {
        "g-luxtrust-prix", "s-bougie-canape", "b-bougie-canape", "b-congelateur",
        "b-invite-cigarette",
    }
    assert all(c.expected.clarification is None for c in verticaux.values())
    assert verticaux["g-luxtrust-prix"].expected.refusal is False
    for case_id in set(verticaux) - {"g-luxtrust-prix"}:
        assert verticaux[case_id].expected.verdict == ["sous_conditions", "ne_tranche_pas"]


def test_les_cinq_repros_differes_sont_materialises_mot_pour_mot() -> None:
    cas = {c.id: c for c in runner.charger_cas(runner.CASES_DIR)}
    assert cas["s-perte-exploitation-domicile"].question == (
        "Ma garantie perte d'exploitation couvre bien les trois semaines où je n'ai pas pu "
        "travailler depuis chez moi ?")
    assert cas["g-nounou-apres-ecole"].question == (
        "Pour la petite, je cherche une nounou après l’école — je tape quoi et je contacte qui ?")
    assert cas["s-tuile-voiture-invite"].question == (
        "Une tuile est tombée de mon toit sur la voiture garée de mon invité pendant l'orage : "
        "habitation, auto ou responsabilité civile ?")
    assert cas["s-ado-baie-volontaire"].question == (
        "Mon ado a cassé exprès la baie vitrée chez sa grand-mère ; c’est ma responsabilité civile "
        "familiale même si c’était volontaire ?")
    assert cas["g-conjoints-arrivee-affiliation"].question == (
        "Ma femme commence son travail un mois avant moi : on doit s’inscrire ensemble à la "
        "commune et affilier les enfants tout de suite ?")
    assert cas["s-perte-exploitation-domicile"].expected.clarification is True
    assert cas["s-ado-baie-volontaire"].expected.block_ids == [
        "axa-lu-optihome-2017:p65:5", "axa-lu-optihome-2017:p22:5"]
    assert cas["s-sejour-ordinateur-pro"].question == (
        "Mon ordinateur professionnel a été volé dans ma chambre d’hôtel : mon contrat habitation "
        "le considère comme contenu ?")


def test_le_cas_clairement_couvert_naccepte_pas_un_verdict_conditionnel() -> None:
    cas = next(c for c in runner.charger_cas(runner.CASES_DIR)
               if c.id == "b-clairement-couvert-degat-eau")
    assert cas.expected.verdict == ["couvert"]


def test_lacte_volontaire_refuse_lexquive_ne_tranche_pas() -> None:
    cas = next(c for c in runner.charger_cas(runner.CASES_DIR)
               if c.id == "s-ado-baie-volontaire")
    assert cas.expected.verdict == ["non_couvert"]
    corpus = load_corpus(runner.DATA_DIR, allow_ungated=True)
    index = Index(corpus)
    claims = [
        _claim(_citation(
            index, "axa-lu-optihome-2017:p65:5",
            "Les présentes conditions spéciales"), "condition-rc"),
        _claim(_citation(
            index, "axa-lu-optihome-2017:p22:5",
            "les dommages occasionnés par la faute intentionnelle"), "exclusion-intention"),
    ]
    answer = _reponse(
        claims, verdict=Verdict(value="ne_tranche_pas", reason="esquive"))
    label, ecarts = runner.juger(
        cas, answer, doc_id="axa-lu-optihome-2017", index=index)
    assert label == "faux_refus"
    assert any("verdict ne_tranche_pas" in ecart for ecart in ecarts)


def test_latest_ouvre_sur_la_reserve_non_experte_sans_inventer_de_run() -> None:
    """`docs/evals/latest.md` : la réserve d'abord, et **aucun chiffre sans son identité de run**.

    Ce test épinglait deux chaînes d'état transitoire — « aucun résultat live » et « ne fabrique donc
    aucun résultat courant ». Elles décrivaient un dépôt qui n'avait encore rien mesuré ; la mesure
    produite en 4.2a-bis puis 4.2d les a légitimement rendues fausses, et le test est resté rouge
    depuis, à épingler une absence que le projet avait le devoir de combler (dette 4.2d, propriété de
    cette story).

    L'invariant **durable** n'est pas « ce document ne contient aucun résultat » : c'est

    1. la **réserve non experte en tête** — la première chose qu'un lecteur voit, avant tout chiffre,
       est qu'aucun verdict n'a été validé par un expert assurance (AD-14) ;
    2. l'**identité de campagne présente** — un chiffre publié dit de quel run il vient ;
    3. **aucun résultat sans `run_digest`** — un document qui affiche un recall, un coût ou une
       stabilité sans l'empreinte du run qui les a produits invite à croire une mesure que personne
       ne peut retrouver. C'est exactement ce que la story interdit, et c'est ce qui est vérifié ici.

    Plus fort que les deux chaînes d'origine, et vrai avant comme après une campagne.
    """
    latest = (runner.REPO_ROOT / "docs" / "evals" / "latest.md").read_text(encoding="utf-8")
    tete = "\n".join(latest.splitlines()[:8]).casefold()
    assert "avertissement non expert" in tete, (
        "la réserve d'AD-14 doit ouvrir le document, avant tout chiffre")
    assert "expert assurance" in tete
    corps = latest.casefold()
    # **Inconditionnel** : un `latest.md` sans le moindre chiffre ne serait pas « prudent », il
    # serait vide — et le rendre acceptable affaiblirait précisément l'invariant que cette
    # réécriture prétend renforcer. Ce document publie un run, et un run se chiffre.
    assert any(mot in corps for mot in ("recall", "rappel", "coût", "stabilité", "latence")), (
        "ce document publie un run : il doit en porter les chiffres")
    assert "run_digest" in corps, (
        "des chiffres sont publiés sans l'empreinte du run qui les a produits")
    assert any(mot in corps for mot in ("campagne", "identité")), (
        "des chiffres sont publiés sans identité de campagne")


def test_quick_porte_les_ids_exacts_et_stables_du_depot() -> None:
    cas = runner.charger_cas(runner.CASES_DIR)
    assert [c.id for c in runner.selection_quick(cas)] == [
        "b-bougie-canape", "g-arrivee-huit-jours", "p-axa-chaleur",
        "p-baloise-acceptation", "s-absurde-chat-lune",
    ]


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


def test_un_cas_full_accepte_la_provenance_codex_sans_contresignature() -> None:
    cas = runner.Cas.model_validate({
        "id": "cas-synthetique-neutre",
        "suite": "guide",
        "profile": "full",
        "question": "Question synthétique neutre",
        "scenario": "Scénario synthétique neutre",
        "famille": "parcours",
        "expected": {"found": False},
        "truth": {
            "source": "codex",
            "countersigned_by": None,
            "validated_by_expert": False,
            "note": "Attente synthétique non contresignée.",
        },
        "mode_attendu": "faux_refus",
    })
    assert cas.truth.source == "codex"
    assert cas.truth.countersigned_by is None


def test_la_provenance_codex_ne_transforme_pas_un_cas_vertical_en_lecture_humaine() -> None:
    with pytest.raises(ValueError, match="lecture_humaine"):
        runner.Cas.model_validate({
            "id": "cas-synthetique-neutre",
            "suite": "guide",
            "profile": "vertical",
            "question": "Question synthétique neutre",
            "expected": {"found": False},
            "truth": {
                "source": "codex",
                "countersigned_by": None,
                "validated_by_expert": False,
                "note": "Attente synthétique non contresignée.",
            },
            "mode_attendu": "faux_refus",
        })


def _lot_holdout_synthetique() -> runner.LotCasesFournis:
    cas = runner.Cas.model_validate({
        "id": "holdout-memoire-neutre",
        "suite": "guide",
        "profile": "full",
        "question": "Question synthétique neutre",
        "scenario": "Scénario synthétique neutre",
        "famille": "parcours",
        "expected": {"found": False},
        "truth": {
            "source": "codex",
            "countersigned_by": None,
            "validated_by_expert": False,
            "note": "Attente synthétique non contresignée.",
        },
        "mode_attendu": "faux_refus",
    })
    return runner.LotCasesFournis(
        (cas,), runner.ReferencesSnapshot("1" * 64), runner.CasesSnapshot("2" * 64))


def test_le_runner_utilise_exclusivement_le_lot_holdout_en_memoire(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        runner, "charger_cas",
        lambda *_args: (_ for _ in ()).throw(AssertionError("lecture des cas disque interdite")),
    )
    assert _main(
        tmp_path, ["--profile", "full", "--dry-run"], monkeypatch,
        lot_cases_fourni=_lot_holdout_synthetique(),
    ) == 0
    sortie = capsys.readouterr().out
    assert "ids=holdout-memoire-neutre" in sortie
    assert "cases_hash" not in sortie


@pytest.mark.parametrize("option", [
    ["--profile", "vertical", "--dry-run"],
    ["--profile", "full", "--quick", "--dry-run"],
    ["--profile", "full", "--suite", "guide", "--dry-run"],
    ["--profile", "full", "--case", "holdout-memoire-neutre", "--dry-run"],
    ["--profile", "full", "--exclude-suite", "parsing", "--dry-run"],
], ids=["profil", "quick", "suite", "case", "exclude"])
def test_le_lot_holdout_refuse_tout_filtre(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: list[str]) -> None:
    assert _main(
        tmp_path, option, monkeypatch, lot_cases_fourni=_lot_holdout_synthetique()) == 2


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


def test_une_suite_documentaire_liee_hors_des_cas_est_refusee(tmp_path: Path) -> None:
    racine = _cases_dir(tmp_path)
    externe = tmp_path / "cas-externes"
    externe.mkdir()
    (externe / "x.yaml").write_text(CAS_SINISTRE.format(id="x"), encoding="utf-8")
    (racine / "sinistre" / "doc-externe").symlink_to(externe, target_is_directory=True)

    with pytest.raises(runner.RefusDeTourner, match="hors de la racine"):
        runner.charger_cas(racine, suites=("sinistre/doc-externe",))


def test_un_yaml_lie_hors_des_cas_ne_peut_pas_entrer_dans_un_gate(tmp_path: Path) -> None:
    racine = _cases_dir(tmp_path)
    dossier = racine / "sinistre" / "doc-lie"
    dossier.mkdir()
    externe = tmp_path / "x.yaml"
    externe.write_text(CAS_SINISTRE.format(id="x"), encoding="utf-8")
    (dossier / "x.yaml").symlink_to(externe)

    with pytest.raises(runner.RefusDeTourner, match="cas YAML hors de la racine"):
        runner.charger_cas(racine, suites=("sinistre/doc-lie",))


# --- ce qui n'est pas livré est refusé, jamais simulé ---------------------

def test_les_profils_vertical_et_full_sont_livres() -> None:
    """4.1 ferme le contrat : les deux profils sont adressables."""
    runner.refuser_ce_qui_nest_pas_livre([], "vertical")
    runner.refuser_ce_qui_nest_pas_livre([], "full")


def test_un_cas_parsing_exige_un_sous_dossier_documentaire(tmp_path: Path) -> None:
    racine = _cases_dir(tmp_path, autres={"parsing/p-page9.yaml": "x: y\n"})
    with pytest.raises(runner.RefusDeTourner, match="parsing/<doc_id>"):
        runner.charger_cas(racine)


def test_un_parsing_invalide_nest_pas_ignore_en_silence(tmp_path: Path) -> None:
    racine = _cases_dir(tmp_path, guide=CAS_GUIDE)
    (racine / "parsing" / GUIDE).mkdir(parents=True)
    (racine / "parsing" / GUIDE / "p-x.yaml").write_text("id: p-x\nsuite: parsing\n", encoding="utf-8")
    with pytest.raises(runner.RefusDeTourner):
        runner.charger_cas(racine)


def test_charger_la_suite_parsing_charge_recursivement_les_documents() -> None:
    cas = runner.charger_cas(runner.CASES_DIR, suites=("parsing",))
    assert len(cas) == 11
    assert {c.doc_id for c in cas} == {"axa-lu-optihome-2017", "baloise-lu-home-2-2024"}


def _cas_parsing(block_id: str, text_norm: str) -> runner.Cas:
    cas = runner.Cas.model_validate({
        "id": "p-test", "suite": "parsing", "profile": "full", "question": "lecture",
        "scenario": "test local", "famille": "definition",
        "expected": {"found": True, "block_ids": [block_id], "text_norm": text_norm},
        "mode_attendu": "bonne_reponse",
        "truth": {"source": "lecture_humaine", "validated_by_expert": False,
                  "countersigned_by": None, "note": "fixture locale"},
    })
    cas._doc_id = GUIDE
    return cas


def test_parsing_exact_divergent_et_bloc_absent_portent_les_trois_labels() -> None:
    ctx = _contexte([])
    block_id = f"{GUIDE}:ffiche:1"
    exact = runner.executer_parsing(_cas_parsing(block_id, normalize(TEXTE_GUIDE)), ctx, doc_id=GUIDE)
    divergent = runner.executer_parsing(
        _cas_parsing(block_id, "transcription visuelle differente"), ctx, doc_id=GUIDE)
    absent = runner.executer_parsing(
        _cas_parsing(f"{GUIDE}:p404:1", "bloc attendu mais absent"), ctx, doc_id=GUIDE)

    assert exact.label == "bonne_reponse" and exact.ok
    assert divergent.label == "parsing" and "index" in divergent.ecarts[0]
    assert absent.label == "citation_introuvable" and "absent" in absent.ecarts[0]
    assert all(r.variant == "local" and r.cost_eur == r.cost_eur_original == 0.0 and r.ms == 0
               for r in (exact, divergent, absent))

    rapport_exact = runner.construire_rapport(
        [exact], [_cas_parsing(block_id, normalize(TEXTE_GUIDE))], cases_dir=Path("/absent"),
        profile="full", max_cost_eur=0.01, complete=True,
        snapshot=runner.CasesSnapshot("miniature-exacte"))
    assert rapport_exact["metrics"]["recall"] == 1.0


def test_parsing_cross_document_est_introuvable_et_found_false() -> None:
    ctx = _contexte([])
    cas = _cas_parsing(f"{CONTRAT}:p34:1", normalize(TEXTE_CONTRAT))
    resultat = runner.executer_parsing(cas, ctx, doc_id=GUIDE)
    assert resultat.label == "citation_introuvable"
    assert resultat.found is False
    assert CONTRAT in resultat.ecarts[0] and GUIDE in resultat.ecarts[0]


def test_parsing_reel_tourne_sans_cle_client_ni_fournisseur(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(runner, "LlmClient", _interdit)
    # Une copie du corpus servi réel (story 4.5, B7) : la suite `parsing` mesure les 11 cas contre
    # les vrais documents ingérés, mais l'écriture des rapports doit rester hors de `data/` du dépôt.
    # `symlinks=True` préserve la disposition de l'espace de publication telle quelle — la suivre la
    # déréférencerait, et `evals-latest.json` n'a pas encore de cible tant qu'aucun run ne l'a publié.
    data = tmp_path / "data"
    shutil.copytree(runner.DATA_DIR, data, symlinks=True)
    poser_espace(tmp_path, data_dir=data, cibles=(Path("parsing.json"), Path("parsing.md")))
    sortie = tmp_path / "parsing.json"
    code = runner.main([
        "--suite", "parsing", "--profile", "full", "--max-cost", "0.01",
        "--data-dir", str(data),
        "--output-json", str(sortie), "--output-markdown", str(tmp_path / "parsing.md"),
    ])
    assert code == 0
    rapport = json.loads(sortie.read_text(encoding="utf-8"))
    assert rapport["cases_completed"] == 11
    assert rapport["cost_eur"] == rapport["cost_eur_original"] == 0.0
    assert rapport["metrics"]["variants"] == {"local": 11}
    assert rapport["metrics"]["labels"]["bonne_reponse"] == 11
    assert rapport["metrics"]["labels"]["parsing"] == 0
    assert rapport["metrics"]["recall"] == 1.0
    assert all(document["dictionary_fingerprint"] is None
               for document in rapport["identity"]["documents"].values())
    assert rapport["identity"]["scope"]["references_digest"] is None
    assert {r["id"] for r in rapport["results"] if r["label"] == "parsing"} == set()


def test_les_compagnons_ont_un_digest_distinct_et_sont_figes_pendant_le_run(tmp_path: Path) -> None:
    cas = runner.charger_cas(runner.CASES_DIR, suites=("guide",))[:1]
    reference = tmp_path / "utilite.yaml"
    reference.write_text("version: 1\n", encoding="utf-8")
    contenu = reference.read_bytes()
    references = runner.ReferencesSnapshot(
        "digest", tmp_path, {reference: contenu}, (reference.name,))
    avant = runner.snapshot_cas(cas, runner.CASES_DIR, references)
    gate = runner.construire_gate(
        ManifestEntry(status="servi", source_hash="s", ingest_fingerprint="i",
                      document_hash="d", edition="2026"),
        _contexte([]), profil="full", cas=cas, cases_dir=runner.CASES_DIR,
        evals_ok=True, snapshot=avant,
        # Story 4.5 : un gate `full` porte son protocole, sa révision et son rapport.
        plancher_digest="a" * 64, candidate_revision="b" * 40, report_digest="c" * 64)
    from server.app.digests import cases_hash
    assert gate.cases_hash == avant.cases_hash == cases_hash(
        [c.case_path for c in cas if c.case_path is not None], runner.CASES_DIR)
    reference.write_text("version: 2\n", encoding="utf-8")
    contenu_apres = reference.read_bytes()
    apres = runner.snapshot_cas(cas, runner.CASES_DIR, runner.ReferencesSnapshot(
        "digest-2", tmp_path, {reference: contenu_apres}, (reference.name,)))
    assert avant.cases_hash == apres.cases_hash
    assert references.digest != "digest-2"
    with pytest.raises(runner.IncidentTechnique, match="modifiés pendant le run"):
        runner.verifier_snapshot_cas(avant)


def test_une_apparition_dans_un_dossier_snapshotte_est_detectee(tmp_path: Path) -> None:
    cases = _cases_dir(tmp_path, guide=CAS_GUIDE)
    cas = runner.charger_cas(cases, suites=("guide",))
    snapshot = runner.snapshot_cas(cas, cases)
    (cases / "guide" / "nouveau.yaml").write_text("invalide: vrai\n", encoding="utf-8")
    with pytest.raises(runner.IncidentTechnique, match="contenu de dossiers modifié"):
        runner.verifier_snapshot_cas(snapshot)


def test_references_absentes_symlink_hors_racine_et_items_blancs_sont_refuses(
        tmp_path: Path) -> None:
    cas = runner.charger_cas(runner.CASES_DIR)
    with pytest.raises(runner.RefusDeTourner, match="dossier de références absent"):
        runner.charger_references(cas, tmp_path / "absent")

    reference_dir = tmp_path / "reference"
    reference_dir.mkdir()
    dehors = tmp_path / "dehors.yaml"
    dehors.write_text("kind: utilite_guide\nversion: 1\nreferences: []\n", encoding="utf-8")
    (reference_dir / "utilite.yaml").symlink_to(dehors)
    with pytest.raises(runner.RefusDeTourner, match="compagnon invalide"):
        runner.charger_references(cas, reference_dir)

    with pytest.raises(runner.ValidationError):
        runner.ReferenceUtilite.model_validate({
            "case_id": "g", "ordre_juste": ["  "], "documents_cites": ["doc"],
            "interlocuteur": "x", "provenance": "x", "countersigned_by": None,
        })
    valide = runner.ReferenceUtilite.model_validate({
        "case_id": "g", "ordre_juste": ["étape"], "documents_cites": ["doc"],
        "interlocuteur": "x", "provenance": "x", "countersigned_by": "L. Oudin — 2026-08-27",
    })
    assert valide.countersigned_by is not None
    with pytest.raises(runner.ValidationError):
        runner.ReferenceUtilite.model_validate({
            "case_id": "g", "ordre_juste": ["étape"], "documents_cites": ["doc"],
            "interlocuteur": "x", "provenance": "x", "countersigned_by": "  ",
        })
    base = {
        "case_id": "g", "langue": "en", "fixture": "f", "test_id": "t", "journal": "j",
        "journal_section": "s", "reserve_signature": "due", "countersigned_by": None,
    }
    assert runner.ControleRetraduction.model_validate(
        {**base, "resultat": "fidele", "ecarts": []}).resultat == "fidele"
    assert runner.ControleRetraduction.model_validate(
        {**base, "resultat": "ecart", "ecarts": ["nuance perdue"]}).resultat == "ecart"
    for resultat, ecarts in (("fidele", ["écart"]), ("ecart", [])):
        with pytest.raises(runner.ValidationError):
            runner.ControleRetraduction.model_validate(
                {**base, "resultat": resultat, "ecarts": ecarts})


def test_les_references_ne_peuvent_pas_cibler_une_mauvaise_suite(tmp_path: Path) -> None:
    cas = runner.charger_cas(runner.CASES_DIR)
    reference_dir = tmp_path / "reference"
    reference_dir.mkdir()
    utilite = (runner.REFERENCE_DIR / "utilite-guide.yaml").read_text("utf-8")
    utilite = utilite.replace("case_id: g-luxtrust-prix", "case_id: s-bougie-canape", 1)
    (reference_dir / "utilite.yaml").write_text(utilite, encoding="utf-8")
    (reference_dir / "retraductions.yaml").write_bytes(
        (runner.REFERENCE_DIR / "retraductions.yaml").read_bytes())
    with pytest.raises(runner.RefusDeTourner, match="cible doit être guide"):
        runner.charger_references(cas, reference_dir)

    (reference_dir / "utilite.yaml").write_bytes(
        (runner.REFERENCE_DIR / "utilite-guide.yaml").read_bytes())
    retraductions = (runner.REFERENCE_DIR / "retraductions.yaml").read_text("utf-8")
    (reference_dir / "retraductions.yaml").write_text(
        retraductions.replace("case_id: g-lang-en-arrivee", "case_id: g-meteo-demain", 1),
        encoding="utf-8")
    with pytest.raises(runner.RefusDeTourner, match="guide/full/multilingue"):
        runner.charger_references(cas, reference_dir)


@pytest.mark.parametrize(("nom", "erreur", "motif"), [
    ("test_langues_live.py", OSError("disque"), "test de retraduction illisible"),
    ("tests-live.md", UnicodeDecodeError("utf-8", b"\xff", 0, 1, "octet"), "journal de retraduction illisible"),
])
def test_les_preuves_de_retraduction_illisibles_sont_un_refus_controle(
        nom: str, erreur: Exception, motif: str, monkeypatch: pytest.MonkeyPatch) -> None:
    cas = runner.charger_cas(runner.CASES_DIR)
    original = Path.read_text

    def lire(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.name == nom:
            raise erreur
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", lire)
    with pytest.raises(runner.RefusDeTourner, match=motif):
        runner.charger_references(cas, runner.REFERENCE_DIR)


def test_une_famille_full_absente_non_trimee_ou_inconnue_est_refusee() -> None:
    cas = next(c for c in runner.charger_cas(runner.CASES_DIR)
               if c.suite == "guide" and c.profile == "full")
    brut = cas.model_dump(mode="json")
    for famille in ("", " parcours ", "echappatoire"):
        brut_mute = {**brut, "famille": famille}
        with pytest.raises(runner.ValidationError, match="famille"):
            runner.Cas.model_validate(brut_mute)

    parsing = next(c for c in runner.charger_cas(runner.CASES_DIR)
                   if c.suite == "parsing")
    brut_parsing = parsing.model_dump(mode="json")
    for famille in ("", " definition ", "echappatoire"):
        with pytest.raises(runner.ValidationError, match="famille"):
            runner.Cas.model_validate({**brut_parsing, "famille": famille})


# --- la clé (AD-14) ------------------------------------------------------

def test_sans_cle_le_runner_refuse_avant_tout_chargement(monkeypatch: pytest.MonkeyPatch,
                                                         tmp_path: Path) -> None:
    """Matrice : « refus **avant** tout chargement de corpus, code 2, “les évals exigent une clé” »."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(runner, "construire_contexte", _interdit)
    code = runner.main(["--suite", "guide", "--cases-dir", str(tmp_path), "--data-dir", str(tmp_path)])
    assert code == 2


def test_dry_run_prepare_sans_cle_client_ni_ecriture(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Story 3.7 : le jalon superviseur se prépare sans franchir la frontière payante."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(runner, "construire_contexte", _interdit)
    code = runner.main(["--gate", "lux-guide", "--profile", "vertical", "--dry-run"])
    assert code == 0
    sortie = capsys.readouterr().out
    assert "aucun appel, aucun client et aucune écriture" in sortie
    assert "gate=lux-guide" in sortie and "cas=1" in sortie


def test_un_dry_run_vertical_refuse_une_racine_incomplete_sans_succes_ni_rapport(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "manifest.json").write_text("{}\n", "utf-8")
    poser_espace(tmp_path, data_dir=data)
    (data / "dictionary.json").unlink()

    code = runner.main(["--profile", "vertical", "--dry-run", "--data-dir", str(data)])
    capture = capsys.readouterr()
    assert code == 2
    assert "dry-run :" not in capture.out and "rapports écrits" not in capture.out
    assert "disposition est incomplète" in capture.err


def test_la_racine_incomplete_precede_le_refus_de_cle(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    data = tmp_path / "data"

    assert runner.main(["--suite", "guide", "--data-dir", str(data)]) == 2
    erreur = capsys.readouterr().err
    assert "espace de publication" in erreur and "les évals exigent une clé" not in erreur


def test_main_full_quick_planifie_seulement_les_ids_stables(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Preuve de couture : ``main`` applique réellement quick au lot full avant exécution."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(runner, "construire_contexte", _interdit)

    assert runner.main(["--profile", "full", "--quick", "--dry-run"]) == 0

    sortie = capsys.readouterr().out
    assert "cas=5" in sortie
    assert ("ids=b-bougie-canape,g-arrivee-huit-jours,p-axa-chaleur,"
            "p-baloise-acceptation,s-absurde-chat-lune") in sortie


@pytest.mark.parametrize("fin", [["--dry-run"], ["--max-cost", "0.0001"]],
                         ids=["dry-run", "refus-budgetaire"])
def test_la_composition_full_precede_dry_run_et_budget(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fin: list[str]) -> None:
    revision = "1" * 40
    monkeypatch.setattr(runner, "revision_executee", lambda *_a, **_k: (revision, []))
    monkeypatch.setattr(runner, "estimate_run_majorant", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("le budget a été calculé avant la composition")))
    monkeypatch.setattr(
        runner, "verifier_composition_gate_full",
        lambda *_a, **_k: (_ for _ in ()).throw(runner.RefusDeTourner("lot full mal composé")))

    code = _main(
        tmp_path,
        ["--gate", GUIDE, "--profile", "full", "--repeat", "3",
         "--candidate-revision", revision, *fin],
        monkeypatch,
    )
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


@pytest.mark.parametrize("doc_id", ["/tmp/contrat", "../contrat", "a/b", r"a\b", "a" * 65])
def test_un_doc_id_de_gate_invalide_est_refuse_avant_tout_chemin_ou_cas(
        doc_id: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(
        runner, "Settings",
        lambda: _settings(live_campaign_id=os.environ.get("LIVE_CAMPAIGN_ID")))
    monkeypatch.setattr(runner, "suite_du_document", _interdit)
    monkeypatch.setattr(runner, "charger_cas", _interdit)

    assert runner.main(["--gate", doc_id, "--cases-dir", str(tmp_path)]) == 2


# --- le jugement (D2) ----------------------------------------------------

def _cas(**kw: Any) -> runner.Cas:
    base: dict[str, Any] = {
        "id": "c", "suite": "guide", "profile": "vertical", "question": "q",
        "expected": {"found": True}, "mode_attendu": "bonne_reponse",
        "truth": {"source": "lecture_humaine", "note": "relu"},
    }
    base.update(kw)
    return runner.Cas.model_validate(base)


def test_expected_decision_claim_est_un_booleen_strict_et_reserve_au_sinistre() -> None:
    with pytest.raises(runner.ValidationError, match="decision_claim"):
        _cas(suite="sinistre", faits={"description": "x"},
             expected={"found": True, "decision_claim": "true"})
    with pytest.raises(runner.ValidationError, match="suite `sinistre`"):
        _cas(expected={"found": True, "decision_claim": True})


def test_une_reponse_conforme_est_une_bonne_reponse() -> None:
    _corpus_, index = _corpus()
    answer = _reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))])
    label, ecarts = runner.juger(_cas(expected={"found": True, "fiche_ids": [f"{GUIDE}:n1"]}),
                                 answer, doc_id=GUIDE, index=index)
    assert (label, ecarts) == ("bonne_reponse", [])


def test_le_predicat_decision_claim_exige_kind_confirme_et_applicabilite_calculee() -> None:
    _corpus_, index = _corpus()
    citation = _citation(index, f"{CONTRAT}:p34:1", "mobilier assuré")
    cas = _cas(suite="sinistre", faits={"description": "x"},
               expected={"found": True, "decision_claim": True})

    sans_applicabilite = _reponse([_claim(citation)])
    _label, ecarts = runner.juger(cas, sans_applicabilite, doc_id=CONTRAT, index=index)
    assert any("claim décisionnelle confirmée" in ecart for ecart in ecarts)

    decisionnelle = _reponse([_claim(citation, applicable="humain")])
    label, ecarts = runner.juger(cas, decisionnelle, doc_id=CONTRAT, index=index)
    assert (label, ecarts) == ("bonne_reponse", [])

    bloc = index.corpus.documents[CONTRAT].block(f"{CONTRAT}:p34:1")
    bloc.kind_source = None
    _label, ecarts = runner.juger(cas, decisionnelle, doc_id=CONTRAT, index=index)
    assert any("claim décisionnelle confirmée" in ecart for ecart in ecarts)


def test_la_definition_confirmee_ne_satisfait_jamais_le_predicat_decision_claim() -> None:
    """Miroir runner du smoke : un bloc confirmé mais non décisionnel ne fonde rien.

    Le kind-set du prédicat est `KINDS_FONDATEURS` — élargir le set à `definition` dans `juger`
    doit faire rougir ce test : une définition confirmée, citée avec une applicabilité calculée,
    ne satisfait jamais `expected.decision_claim: true`.
    """
    docs = {GUIDE: _document(GUIDE, "guide", TEXTE_GUIDE, "ffiche"),
            CONTRAT: _document(CONTRAT, "contrat", TEXTE_CONTRAT, "p34",
                               block_kind="definition", kind_source="manual")}
    manifest = {d: ManifestEntry(status="servi", source_hash="s", ingest_fingerprint="f",
                                 document_hash="d", edition="2020") for d in docs}
    corpus = Corpus(documents=docs, manifest=manifest,
                    summaries={d: f"# {d}" for d in docs}, alerts={d: [] for d in docs})
    index = Index(corpus)
    assert index.corpus.documents[CONTRAT].block(f"{CONTRAT}:p34:1").kind_confirmed
    cas = _cas(suite="sinistre", faits={"description": "x"},
               expected={"found": True, "decision_claim": True})
    reponse = _reponse([_claim(_citation(index, f"{CONTRAT}:p34:1", "mobilier assuré"),
                               applicable="humain")])
    _label, ecarts = runner.juger(cas, reponse, doc_id=CONTRAT, index=index)
    assert any("claim décisionnelle confirmée" in ecart for ecart in ecarts)


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


# --- story 4.2f : le 200 typé d'une lecture partielle, dans le vocabulaire du harness -------------

def _lecture_partielle() -> Answer:
    """Ce que le pipeline rend désormais là où il levait `TruncatedRead` : un 200 sans absence."""
    return Answer(found=False, complete=False, texte="Ma lecture s'est arrêtée avant de conclure.",
                  lecture_partielle=LecturePartielle(nodes_read=2, blocks_read=6,
                                                     documents=[GUIDE]),
                  unknown=["Je n'ai pas pu lire tout ce qui pouvait concerner la question."])


def test_une_lecture_partielle_est_un_claim_non_soutenu_jamais_une_bonne_reponse() -> None:
    """La bascule du code HTTP ne doit pas verdir la mesure.

    L'ancienne branche d'exception classait la lecture bornée en `claim_non_soutenu` ; le 200 qui la
    remplace doit produire **le même** label, à la même précédence — avant `faux_refus`, que le
    `found=False` déclencherait sinon, et loin de `bonne_reponse`, qui est le défaut de `juger()`.
    """
    _corpus_, index = _corpus()
    label, ecarts = runner.juger(_cas(), _lecture_partielle(), doc_id=GUIDE, index=index)
    assert label == "claim_non_soutenu"
    assert any("lecture partielle" in e and "2 nœud(s) lu(s)" in e for e in ecarts)
    # Même sur un cas qui attend un refus, ce n'est pas une bonne réponse : rien n'a été prouvé.
    attendu_refus = _cas(expected={"found": False, "refusal": True})
    label_refus, _ = runner.juger(attendu_refus, _lecture_partielle(), doc_id=GUIDE, index=index)
    assert label_refus == "claim_non_soutenu"


def test_le_reason_kind_dune_lecture_partielle_reste_lecture_tronquee() -> None:
    """Deux campagnes ne restent comparables que si le vocabulaire du harness ne change pas avec le
    code HTTP : `lecture_tronquee` est le mot que la branche `TruncatedRead` posait déjà.

    Le `Resultat` est produit par **l'exécution** (`runner.executer`), jamais construit ici : c'est
    la seule façon que ce test rougisse si la branche de production disparaît. Un test qui recopie
    l'expression du runner dans son propre corps assert sur lui-même et reste vert quel que soit le
    code — le défaut exact que cette réécriture ferme.
    """
    ctx = _armer(_contexte([(_lecture_partielle(), _trace())]))
    cas = _cas(id="g-lecture-partielle", expected={"found": True})

    resultats, _sortie = _executer(ctx, [cas])

    assert len(resultats) == 1
    resultat = resultats[0]
    assert resultat.reason_kind == "lecture_tronquee"
    assert resultat.label == "claim_non_soutenu"
    # Un 200, et pas une erreur : c'est la bascule que la story opère, vue depuis le harness.
    assert resultat.http == 200 and resultat.found is False and resultat.complete is False
    assert resultat.ok is False  # jamais confondu avec une bonne réponse


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
    assert runner.Resultat(id="c", suite="guide", label=label,
                           variant=runner.DEFAUT_PAR_SUITE["guide"], ecarts=ecarts).ok is False


def test_un_refus_attendu_et_obtenu_na_aucun_ecart() -> None:
    """Un cas qui attend un refus **justifié** et l'obtient est `bonne_reponse` sans écart."""
    _corpus_, index = _corpus()
    cas = _cas(expected={"found": False, "refusal": True}, mode_attendu="bonne_reponse")
    label, ecarts = runner.juger(cas, _refus(), doc_id=GUIDE, index=index)
    assert label == "bonne_reponse" and ecarts == []


def test_une_clarification_attendue_nest_pas_assimilee_a_un_refus() -> None:
    _corpus_, index = _corpus()
    answer = Answer(
        found=False, complete=False, texte="",
        reason=AbsenceProof(kind="clarification_requise"),
        clarification="De quel sujet parlez-vous ?",
    )
    clarification = _cas(expected={"found": False, "clarification": True})
    label, ecarts = runner.juger(clarification, answer, doc_id=GUIDE, index=index)
    assert label == "bonne_reponse" and ecarts == []

    refus = _cas(expected={"found": False, "refusal": True})
    _label, ecarts_refus = runner.juger(refus, answer, doc_id=GUIDE, index=index)
    assert "refus justifié=False (attendu True)" in ecarts_refus


def test_un_label_different_du_mode_attendu_est_un_ecart() -> None:
    """La branche que la matrice exige : le label obtenu ≠ `mode_attendu` ⇒ le cas n'est pas `ok`.

    Sans elle, un cas qui déclare attendre un `faux_refus` et reçoit une bonne réponse passerait le
    gate — le golden set cesserait de mesurer ce qu'il dit mesurer.
    """
    _corpus_, index = _corpus()
    cas = _cas(expected={"found": False, "refusal": True}, mode_attendu="faux_refus")
    label, ecarts = runner.juger(cas, _refus(), doc_id=GUIDE, index=index)
    assert label == "bonne_reponse"
    assert ecarts == ["label bonne_reponse (mode_attendu faux_refus)"]
    assert runner.Resultat(id="c", suite="guide", label=label,
                           variant=runner.DEFAUT_PAR_SUITE["guide"], ecarts=ecarts).ok is False


# --- exécution : la matrice d'E/S ----------------------------------------

def _executer(ctx: runner.Contexte, cas: list[runner.Cas], *, max_cost: float = 1.0,
              variant: str | None = None) -> Any:
    import asyncio
    sortie = io.StringIO()
    _armer(ctx)
    return (asyncio.run(runner.executer(
        cas, ctx, max_cost_eur=max_cost, sortie=sortie, variant=variant)), sortie)


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


def test_variante_answer_trace_et_mesures_sont_projetees_jusquau_json(tmp_path: Path) -> None:
    _corpus_, index = _corpus()
    block_id = f"{GUIDE}:ffiche:1"
    answer = _reponse([_claim(_citation(index, block_id, "LuxTrust"))])
    # `full_context` : la variante de comparaison, la seule qui ne soit pas le défaut de la suite
    # depuis que la tâche T2 de la story 5.6 a supprimé `deterministe` et `outils`. Ce qui est mesuré
    # ici est la **projection** d'une variante explicite jusqu'au JSON, quelle qu'elle soit.
    ctx = _armer(_contexte([(answer, _trace(
        variant="full_context", cost_eur_original=0.0372))]))
    cas = _cas(id="g-projection", expected={"found": True, "block_ids": [block_id]})

    resultats, _ = _executer(ctx, [cas], variant="full_context")
    rapport = runner.construire_rapport(
        resultats, [cas], cases_dir=tmp_path, profile="vertical", max_cost_eur=1.0,
        complete=True)

    assert ctx._guide.appels[0]["kw"]["variant"] == "full_context"  # type: ignore[attr-defined]
    assert resultats[0].variant == "full_context"
    assert rapport["metrics"]["variants"] == {"full_context": 1}
    assert rapport["metrics"]["recall"] == 1.0
    projection = rapport["results"][0]
    assert projection["claims"][0]["quotes"][0]["block_id"] == block_id
    assert projection["cost_eur_original"] == 0.0372
    assert isinstance(projection["latency_ms"], int) and projection["latency_ms"] >= 0


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


@pytest.mark.parametrize("erreur", [Timeout("deadline"), LlmUnavailable("529")])
def test_un_incident_technique_est_un_incident_pas_un_verdict(erreur: Exception) -> None:
    """D4 : `Timeout`, `LlmUnavailable` — `IncidentTechnique`, donc code 3 et manifest intact.

    (Que le manifest reste intact est vérifié de bout en bout par
    `test_gate_en_echec_technique_ne_modifie_pas_le_manifest`, qui compare le fichier avant/après.)
    """
    ctx = _armer(_contexte([erreur]))
    with pytest.raises(runner.IncidentTechnique) as exc:
        _executer(ctx, [_cas(id="g-luxtrust")])
    assert erreur.code.value in str(exc.value)


def test_un_incident_conserve_le_cout_deja_engage_dans_le_budget_et_la_trace() -> None:
    """B10 : un appel facturé avant `PipelineError` ne redevient jamais artificiellement gratuit."""
    erreur = Timeout("réponse tronquée")
    erreur.trace = _trace()
    ctx = _armer(_contexte([erreur], cout=0.0372))
    with pytest.raises(runner.IncidentTechnique) as exc:
        _executer(ctx, [_cas(id="g-luxtrust")])
    assert "coût engagé 0.0372 €" in str(exc.value)
    assert erreur.trace.total_cost_eur == 0.0372


def test_un_incident_sans_trace_conserve_aussi_le_cout_du_budget() -> None:
    erreur = Timeout("réponse interrompue sans trace assemblée")
    ctx = _armer(_contexte([erreur], cout=0.0413))
    with pytest.raises(runner.IncidentTechnique) as exc:
        _executer(ctx, [_cas(id="g-sans-trace")])
    assert "coût engagé 0.0413 €" in str(exc.value)


def test_le_plafond_atteint_pendant_un_cas_est_dit_pour_ce_quil_est() -> None:
    """Le budget d'un cas est le reste du run (AD-9) : `BudgetExceeded` y veut dire « ce cas
    déborderait le plafond », pas « le fournisseur est en panne ». Même arrêt, une étape plus tard."""
    ctx = _armer(_contexte([BudgetExceeded("majorant 0,12 € > reste 0,03 €")]))
    with pytest.raises(runner.IncidentTechnique) as exc:
        _executer(ctx, [_cas(id="g-luxtrust")], max_cost=0.03)
    message = str(exc.value)
    assert "plafond de run atteint pendant le cas g-luxtrust" in message
    assert "0.0200 € engagés sur 0.0300 €" in message


def test_un_cas_hors_bornes_du_pipeline_est_un_refus_pas_un_incident() -> None:
    """`invalid_request` reste un refus, mais un appel déjà facturé n'est pas présenté gratuit."""
    ctx = _armer(_contexte([InvalidRequest("historique de 12 tours")], cout=0.0187))
    with pytest.raises(runner.RefusDeTourner) as exc:
        _executer(ctx, [_cas(id="g-luxtrust")])
    assert "coût engagé 0.0187 €" in str(exc.value)


def test_une_exception_inattendue_apres_un_appel_devient_un_incident_facture() -> None:
    ctx = _armer(_contexte([RuntimeError("secret interne")], cout=0.0291))
    with pytest.raises(runner.IncidentTechnique) as exc:
        _executer(ctx, [_cas(id="g-interne")])
    message = str(exc.value)
    assert "internal (RuntimeError)" in message
    assert "coût engagé 0.0291 €" in message
    assert "secret interne" not in message


def test_le_plafond_de_run_arrete_avant_le_cas_suivant() -> None:
    """Matrice : « arrêt avant le cas suivant, manifest non modifié, code 3, message avec le coût »."""
    corpus, index = _corpus()
    bonne = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())
    ctx = _armer(_contexte([bonne, bonne, bonne], cout=0.05))
    with pytest.raises(runner.IncidentTechnique) as exc:
        _executer(ctx, [_cas(id="a"), _cas(id="b"), _cas(id="c")], max_cost=0.05)
    message = str(exc.value)
    assert "plafond de run" in message and "0.0500" in message
    # Story 4.2b : le plan se compte en **exécutions** (cas × répétitions), et l'incident emporte
    # les acquis pour que le rapport partiel les publie.
    assert "2 exécutions non exécutées" in message
    assert exc.value.non_executes == ["b", "c"]
    # Un seul cas a démarré : l'arrêt est **avant** le suivant, pas au milieu.
    assert len(ctx._guide.appels) == 1   # type: ignore[attr-defined]


def test_le_plafond_exact_laisse_un_cas_suivant_tenter_un_hit_cache(tmp_path: Path) -> None:
    """Un plafond épuisé interdit un appel, pas une lecture locale à coût nul."""
    _corpus_, index = _corpus()
    bonne = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())

    class PipelineCacheAware(DoublePipeline):
        async def __call__(self, *args: Any, **kw: Any) -> tuple[Answer, Trace]:
            if kw["budget"].max_cost_eur == 0:
                cout = self.cout
                self.cout = 0.0
                try:
                    return await super().__call__(*args, **kw)
                finally:
                    self.cout = cout
            return await super().__call__(*args, **kw)

    ctx = _contexte([], cout=0.05)
    ctx._guide = PipelineCacheAware([bonne, bonne], cout=0.05)  # type: ignore[attr-defined]
    ctx.response_cache = runner.PersistentResponseCache(tmp_path / "cache")
    resultats, _ = _executer(ctx, [_cas(id="a"), _cas(id="b")], max_cost=0.05)

    assert [r.id for r in resultats] == ["a", "b"]
    assert ctx._guide.appels[1]["kw"]["budget"].max_cost_eur == 0  # type: ignore[attr-defined]


def test_une_trace_de_variante_differente_est_un_incident_et_purge_sa_namespace(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _corpus_, index = _corpus()
    answer = _reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))])
    # Deux variantes **servies par le harness** et distinctes : la trace dit `navigation`, le run
    # demandait `full_context`. Ce que le témoin mesure est l'écart, pas l'identité des deux noms.
    ctx = _contexte([(answer, _trace(variant="navigation"))])
    cache = runner.PersistentResponseCache(tmp_path / "cache")
    ctx.response_cache = cache
    purges: list[bool] = []
    original = cache.discard_namespace

    def purge() -> None:
        purges.append(True)
        original()

    monkeypatch.setattr(cache, "discard_namespace", purge)
    with pytest.raises(runner.IncidentTechnique, match="TraceVariantMismatch"):
        _executer(ctx, [_cas(id="g-mismatch")], variant="full_context")
    assert purges == [True]


@pytest.mark.parametrize("suite", ["guide", "sinistre"])
def test_executer_cas_sans_variante_attend_le_defaut_du_pipeline_de_la_suite(suite: str) -> None:
    """Revue 4.2d : la garde de cohérence lit `DEFAUT_PAR_SUITE`, jamais une table recopiée.

    `executer_cas` est une coroutine publique dont la signature annonce `variant: str | None = None`.
    Appelée sans variante, elle comparait la trace à une table locale qui affirmait `deterministe`
    pour le sinistre : un faux `TraceVariantMismatch` **après** les appels facturés, qui jetait de
    surcroît la namespace de cache d'une trace pourtant conforme. Le cas guide est là pour prouver
    que le correctif n'a pas déplacé le défaut de l'autre suite.
    """
    import asyncio

    _corpus_, index = _corpus()
    bloc = f"{GUIDE}:ffiche:1" if suite == "guide" else f"{CONTRAT}:p34:1"
    citation = "LuxTrust" if suite == "guide" else "mobilier assuré"
    answer = _reponse([_claim(_citation(index, bloc, citation))])
    reponse = (answer, _trace(suite))  # trace étiquetée du défaut de la suite
    ctx = _armer(_contexte([reponse] if suite == "guide" else [],
                           [reponse] if suite == "sinistre" else None))
    cas = _cas(id=f"{suite[0]}-defaut", suite=suite,
               **({"faits": {"description": "x"}} if suite == "sinistre" else {}))
    doc_id = GUIDE if suite == "guide" else CONTRAT

    _answer, trace, _cout = asyncio.run(runner.executer_cas(
        cas, ctx, doc_id=doc_id, budget_restant_eur=1.0))  # aucune variante passée

    assert trace.variant == runner.DEFAUT_PAR_SUITE[suite] == "navigation"


def test_guide_run_without_variant_uses_the_runtime_versioned_setting() -> None:
    settings = _settings(retrieval_variant="full_context")
    cas = _cas(id="g-runtime-default")
    assert runner.variante_du_cas(cas, None, settings=settings) == "full_context"

    _corpus_, index = _corpus()
    answer = _reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))])
    ctx = _armer(_contexte(
        [(answer, _trace(variant="full_context"))], settings=settings))
    _answer, trace, _cost = asyncio.run(runner.executer_cas(
        cas, ctx, doc_id=GUIDE, budget_restant_eur=1.0))
    assert trace.variant == "full_context"
    assert ctx._guide.appels[0]["kw"]["variant"] == "full_context"  # type: ignore[attr-defined]


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
    # Story 4.5, B7 : `ecrire_gate` bascule par l'espace de publication, qui refuse une cible que le
    # pointeur unique ne résout pas — la disposition est posée ici, hors de tout run.
    poser_espace(tmp_path, data_dir=racine)
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


def test_la_suite_dun_gate_est_celle_qui_sert_le_document(tmp_path: Path) -> None:
    """D5 : `--gate lux-guide` → suite `guide` ; `--gate {contrat}` → suite `sinistre`."""
    s = _settings()
    assert runner.suite_du_document(s, GUIDE) == "guide"
    assert runner.suite_du_document(s, CONTRAT) == "sinistre"
    dossier = tmp_path / "sinistre" / "baloise-lu-home-2-2024"
    dossier.mkdir(parents=True)
    assert runner.suite_du_document(
        s, "baloise-lu-home-2-2024", cases_dir=tmp_path
    ) == "sinistre/baloise-lu-home-2-2024"
    assert runner.document_de_la_suite(
        s, "sinistre/baloise-lu-home-2-2024"
    ) == "baloise-lu-home-2-2024"
    with pytest.raises(runner.RefusDeTourner) as exc:
        runner.suite_du_document(s, "un-autre-contrat", cases_dir=tmp_path)
    assert "aucune suite" in str(exc.value)


def test_les_hashes_de_cas_sinistre_sont_isoles_par_document(tmp_path: Path) -> None:
    racine = _cases_dir(
        tmp_path,
        sinistre=CAS_SINISTRE,
        autres={"sinistre/doc-b/b-temoin.yaml": CAS_SINISTRE.format(id="b-temoin")},
    )
    axa = runner.charger_cas(racine, suites=("sinistre",))
    doc_b = runner.charger_cas(racine, suites=("sinistre/doc-b",))
    assert {c.id for c in axa} == {"s-bougie"}
    assert [c.id for c in doc_b] == ["b-temoin"] and doc_b[0].doc_id == "doc-b"
    entry = ManifestEntry(status="servi", source_hash="s", ingest_fingerprint="f",
                          document_hash="d", edition="2020")
    ctx = _contexte([])
    gate_axa = runner.construire_gate(
        entry, ctx, profil="vertical", cas=axa,
        cases_dir=racine, evals_ok=True)
    gate_b = runner.construire_gate(
        entry, ctx, profil="vertical", cas=doc_b, cases_dir=racine, evals_ok=True)
    assert gate_axa.cases == 1 and gate_b.cases == 1
    assert gate_axa.cases_hash != gate_b.cases_hash


def test_un_cas_documentaire_sans_chemin_ne_peut_pas_certifier_un_hash(tmp_path: Path) -> None:
    racine = _cases_dir(
        tmp_path, autres={"sinistre/doc-b/b-temoin.yaml": CAS_SINISTRE.format(id="b-temoin")})
    cas = runner.charger_cas(racine, suites=("sinistre/doc-b",))[0]
    cas._case_path = None
    entry = ManifestEntry(status="servi", source_hash="s", ingest_fingerprint="f",
                          document_hash="d", edition="2020")

    with pytest.raises(runner.RefusDeTourner, match="sans case_path"):
        runner.construire_gate(entry, _contexte([]), profil="vertical", cas=[cas],
                               cases_dir=racine, evals_ok=True)


def test_le_fallback_de_chemin_reste_disponible_pour_un_cas_plat(tmp_path: Path) -> None:
    racine = _cases_dir(tmp_path, guide=CAS_GUIDE)
    cas = runner.charger_cas(racine, suites=("guide",))[0]
    cas._case_path = None
    entry = ManifestEntry(status="servi", source_hash="s", ingest_fingerprint="f",
                          document_hash="d", edition="2020")

    gate = runner.construire_gate(entry, _contexte([]), profil="vertical", cas=[cas],
                                  cases_dir=racine, evals_ok=True)
    from server.app.digests import cases_hash
    assert gate.cases_hash == cases_hash([racine / "guide" / "g-luxtrust.yaml"], racine)


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
    # Story 4.5 : l'entrée réécrite se **normalise** au schéma en vigueur, `structure_hash` compris
    # (`null` tant qu'aucune `structure.json` n'a été ingérée). Aucune **valeur** existante ne
    # bouge — c'est ce que ce test protège : un `--gate` n'a le droit de toucher qu'au gate.
    assert apres[GUIDE].get("structure_hash") is None
    assert {k: v for k, v in apres[GUIDE].items() if k not in ("gate", "structure_hash")} == \
        {k: v for k, v in avant[GUIDE].items() if k not in ("gate", "structure_hash")}
    # Le fichier reste lisible par le loader.
    assert ManifestEntry.model_validate(apres[GUIDE]).gate is not None


# --- revue Codex 1.10 tour 2 (B2) : la contresignature humaine de la relecture -----------------

CAS_CONTRESIGNE = """
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
  countersigned_by: "Lancelot Oudin, 2026-08-25"
  validated_by_expert: false
  note: "relu à la main"
"""


def test_le_gate_dit_si_la_relecture_est_contresignee(tmp_path: Path) -> None:
    """AD-14 définit `vertical` par une relecture **à la main**, « affichée comme tel » sur `/`.

    Revue Codex 1.10 tour 2, B2 : `truth.source: lecture_humaine` dit comment l'attente a été
    établie, pas par qui. Tant que `countersigned_by` est `null`, la relecture est celle de la boucle
    autonome, et `/` ne doit pas écrire « relus à la main ». Le gate porte donc la conjonction sur
    les cas exécutés — c'est une propriété du run, pas un littéral de la page.
    """
    entry = ManifestEntry(status="servi", source_hash="s", ingest_fingerprint="f",
                          document_hash="d", edition="2020")
    ctx = _contexte([])

    du = _cases_dir(tmp_path / "du", guide=CAS_GUIDE)
    gate = runner.construire_gate(entry, ctx, profil="vertical",
                                  cas=runner.charger_cas(du, suites=("guide",)),
                                  cases_dir=du, evals_ok=True)
    assert gate.countersigned is False

    fait = _cases_dir(tmp_path / "fait", guide=CAS_CONTRESIGNE)
    gate = runner.construire_gate(entry, ctx, profil="vertical",
                                  cas=runner.charger_cas(fait, suites=("guide",)),
                                  cases_dir=fait, evals_ok=True)
    assert gate.countersigned is True

    # Un seul cas non contresigné suffit : « 2 cas relus à la main » serait faux dès que l'un des
    # deux ne l'est pas. La conjonction est donc sur **tous** les cas du run.
    melange = _cases_dir(tmp_path / "melange", guide=CAS_CONTRESIGNE)
    (melange / "guide" / "g-autre.yaml").write_text(
        CAS_GUIDE.format(id="g-autre", profile="vertical", fiche=f"{GUIDE}:n1"), encoding="utf-8")
    gate = runner.construire_gate(entry, ctx, profil="vertical",
                                  cas=runner.charger_cas(melange, suites=("guide",)),
                                  cases_dir=melange, evals_ok=True)
    assert gate.countersigned is False

    # Le contrat accepte les deux états réels : signature encore due, ou nom/date non blanc.
    signatures = [
        runner.charger_cas(du, suites=("guide",))[0].truth.countersigned_by,
        runner.charger_cas(fait, suites=("guide",))[0].truth.countersigned_by,
    ]
    assert any(signature is None for signature in signatures)
    assert any(isinstance(signature, str) and bool(signature.strip())
               for signature in signatures)


def test_une_contresignature_doit_nommer_quelquun(tmp_path: Path) -> None:
    """`countersigned_by: ""` serait une contresignature par personne — et ferait basculer la page."""
    racine = _cases_dir(tmp_path, guide=CAS_CONTRESIGNE.replace(
        '"Lancelot Oudin, 2026-08-25"', '"   "'))
    with pytest.raises(runner.RefusDeTourner) as exc:
        runner.charger_cas(racine, suites=("guide",))
    assert "countersigned_by" in str(exc.value)


def test_un_gate_hors_schema_ne_bloque_pas_lecriture_du_suivant(tmp_path: Path) -> None:
    """Le même cul-de-sac que le gate rouge, par une autre porte (revue Codex 1.10 tour 2).

    Quand `Gate` gagne un champ obligatoire, tous les gates déjà écrits deviennent des entrées de
    manifest invalides. `ecrire_gate` validait le manifest **entier** avant d'écrire : refaire le
    gate du premier document devenait impossible tant que le second n'était pas refait, et
    réciproquement. Les autres entrées sont recopiées telles quelles — rien n'est réparé en douce —,
    et un manifest réellement abîmé arrête toujours tout.
    """
    racine = _data_dir(tmp_path)
    chemin = racine / "manifest.json"
    brut = json.loads(chemin.read_text(encoding="utf-8"))
    perime = {"profile": "vertical", "source_hash": "s", "ingest_fingerprint": "f",
              "cases_hash": "c", "pipeline_digest": "p", "prompts_digest": "q", "model_ids": {},
              "evals_ok": True, "date": "2026-08-23", "overlay_hash": None, "cases": 1}
    brut[GUIDE]["gate"] = dict(perime)
    brut[CONTRAT]["gate"] = dict(perime)
    chemin.write_text(json.dumps(brut, indent=2) + "\n", encoding="utf-8")

    racine_cas = _cases_dir(tmp_path, guide=CAS_GUIDE)
    entry = ManifestEntry.model_validate({**brut[GUIDE], "gate": None})
    gate = runner.construire_gate(entry, _contexte([]), profil="vertical",
                                  cas=runner.charger_cas(racine_cas, suites=("guide",)),
                                  cases_dir=racine_cas, evals_ok=True)
    runner.ecrire_gate(chemin, GUIDE, gate)
    apres = json.loads(chemin.read_text(encoding="utf-8"))
    assert apres[GUIDE]["gate"]["countersigned"] is False
    # L'autre document garde son gate périmé **mot pour mot** : son propre run le refera.
    assert apres[CONTRAT]["gate"] == perime


def test_un_gate_hors_schema_se_reprend_comme_un_gate_rouge(tmp_path: Path) -> None:
    """`construire_contexte(regate=...)` : un gate que l'image ne sait pas lire n'est pas un gate.

    Sans cela, le document part en quarantaine « entrée de manifest invalide » et `--gate` refuse en
    code 2 « document non servi » — alors que réécrire ce gate est exactement ce que la commande
    demande (revue Codex 1.10 tour 2).
    """
    racine = tmp_path / "data"
    racine.mkdir()
    _corpus_sur_disque(racine)
    chemin = racine / "manifest.json"
    brut = json.loads(chemin.read_text(encoding="utf-8"))
    brut[GUIDE]["gate"] = {"profile": "vertical", "source_hash": brut[GUIDE]["source_hash"],
                           "ingest_fingerprint": brut[GUIDE]["ingest_fingerprint"],
                           "cases_hash": "c", "pipeline_digest": "p", "prompts_digest": "q",
                           "model_ids": {}, "evals_ok": True, "date": "2026-08-23",
                           "overlay_hash": None, "cases": 1}
    brut[CONTRAT]["gate"] = {
        "profile": "vertical", "source_hash": brut[CONTRAT]["source_hash"],
        "ingest_fingerprint": brut[CONTRAT]["ingest_fingerprint"], "cases_hash": "autre-cas",
        "pipeline_digest": "autre-pipeline", "prompts_digest": "autre-prompt",
        "model_ids": {}, "evals_ok": True, "date": "2026-08-23", "overlay_hash": None,
        "cases": 1, "countersigned": False,
    }
    chemin.write_text(json.dumps(brut, indent=2) + "\n", encoding="utf-8")

    poser_espace(tmp_path, data_dir=racine)
    avant = chemin.read_bytes()
    from server.app.corpus.racine import lecture_pincee_regate

    with lecture_pincee_regate(racine, GUIDE) as capacite:
        ferme = load_corpus(
            racine, allow_ungated=True, lecture=capacite.lecture,
            regate=GUIDE, capacite_regate=capacite)
        assert GUIDE not in ferme.served, "la neutralisation doit rester fail-closed par défaut"
        corpus = load_corpus(
            racine, allow_ungated=True, lecture=capacite.lecture,
            regate=GUIDE, capacite_regate=capacite, neutraliser_regate=True)
        assert GUIDE in corpus.served
        assert capacite.lecture.generation is not None
        assert capacite.lecture.racine is not None
        assert capacite.lecture.racine.data_dir == racine
    # Le manifest validé est seulement neutralisé en mémoire ; l'autre entrée est inchangée.
    assert chemin.read_bytes() == avant
    assert json.loads(chemin.read_text(encoding="utf-8"))[CONTRAT]["gate"] == brut[CONTRAT]["gate"]


def test_la_vue_de_reprise_ne_materialise_pas_un_lien_hors_data(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nom historique : la reprise ne matérialise plus aucune vue ni aucun lien."""
    racine = tmp_path / "data"
    racine.mkdir()
    _corpus_sur_disque(racine)
    externe = tmp_path / "hors-data"
    externe.mkdir()
    (externe / "secret.txt").write_text("ne pas lire", encoding="utf-8")
    (racine / "lien-hors-data").symlink_to(externe, target_is_directory=True)

    poser_espace(tmp_path, data_dir=racine)
    from server.app.corpus.racine import lecture_pincee_regate

    lectures_externes: list[Path] = []
    vrai_read_bytes = Path.read_bytes
    vrai_open = Path.open

    def open_garde(chemin: Path, *args: Any, **kwargs: Any) -> Any:
        mode = args[0] if args else kwargs.get("mode", "r")
        resolu = Path(os.path.realpath(chemin))
        if "r" in mode and resolu.is_relative_to(externe.resolve()):
            lectures_externes.append(resolu)
            raise AssertionError(f"lecture externe interdite : {resolu}")
        return vrai_open(chemin, *args, **kwargs)

    def read_bytes_garde(chemin: Path) -> bytes:
        resolu = Path(os.path.realpath(chemin))
        if resolu.is_relative_to(externe.resolve()):
            lectures_externes.append(resolu)
            raise AssertionError(f"lecture externe interdite : {resolu}")
        return vrai_read_bytes(chemin)

    monkeypatch.setattr(Path, "open", open_garde)
    monkeypatch.setattr(Path, "read_bytes", read_bytes_garde)
    entrees_avant = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    with lecture_pincee_regate(racine, GUIDE) as capacite:
        corpus = load_corpus(
            racine, allow_ungated=True, lecture=capacite.lecture,
            regate=GUIDE, capacite_regate=capacite, neutraliser_regate=True)
        assert GUIDE in corpus.served
    assert lectures_externes == []
    assert {p.relative_to(tmp_path) for p in tmp_path.rglob("*")} == entrees_avant
    assert (racine / "lien-hors-data").resolve() == externe.resolve()


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
          reponses_sinistre: list[Any] | None = None,
          lot_cases_fourni: runner.LotCasesFournis | None = None) -> int:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    if not (data / "manifest.json").is_file():
        _corpus_sur_disque(data)
    # Story 4.5, B7 : le runner bascule ses sorties (rapports, gate) par l'espace de publication,
    # qui refuse une cible non résolue par le pointeur unique — posée ici, hors de tout run, et
    # idempotente pour les appels successifs de `_main` sur le même `tmp_path`. Un `--output-json`
    # / `--output-markdown` explicite ajoute sa propre cible au lot standard.
    cibles_supplementaires = []
    for drapeau in ("--output-json", "--output-markdown"):
        if drapeau in argv:
            valeur = Path(argv[argv.index(drapeau) + 1])
            cibles_supplementaires.append(
                valeur.relative_to(tmp_path) if valeur.is_absolute() else valeur)
    poser_espace(tmp_path, data_dir=data, cibles=cibles_supplementaires)
    cases = _cases_dir(tmp_path, guide=CAS_GUIDE, sinistre=CAS_SINISTRE)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(runner, "Settings", lambda: _settings())
    _COURANT["guide"] = DoublePipeline(reponses_guide or [])
    _COURANT["sinistre"] = DoublePipeline(reponses_sinistre or [])
    return runner.main(
        argv + ["--cases-dir", str(cases), "--data-dir", str(data)],
        lot_cases_fourni=lot_cases_fourni,
    )


def test_commande_matrice_sans_sorties_traverse_chaque_cellule_et_ecrit_les_canoniques(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    cases = _cases_dir(tmp_path, guide=CAS_GUIDE)
    _corpus_, index = _corpus()
    answer = _reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))])
    variants = list(baselines.VARIANTS)  # lues sur la matrice, jamais recopiées
    double = DoublePipeline([(answer, _trace(variant=variant)) for variant in variants])
    _COURANT["guide"] = double
    _COURANT["sinistre"] = DoublePipeline([])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")

    def matrix_settings() -> Settings:
        return _settings(
            retrouver_outils_tier=os.environ.get("RETROUVER_OUTILS_TIER", "reason"),
            retrieval_prompt_cache=(
                os.environ.get("RETRIEVAL_PROMPT_CACHE", "true").casefold() == "true"))

    monkeypatch.setattr(runner, "Settings", matrix_settings)
    # La commande ne reçoit aucun --output-* : seul le root canonique est redirigé vers tmp_path.
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    poser_espace(
        tmp_path,
        data_dir=data,
        cibles=(Path("docs/evals/baselines.json"), Path("docs/evals/baselines.md")),
    )
    code = runner.main([
        "--suite", "guide", "--compare", ",".join(variants),
        "--tiers", "reason", "--max-cost", "1.0",
        "--cases-dir", str(cases), "--data-dir", str(data),
    ])

    assert code == 0
    assert len(double.appels) == len(variants)
    assert [call["kw"]["variant"] for call in double.appels] == variants
    json_path = tmp_path / "docs" / "evals" / "baselines.json"
    markdown_path = tmp_path / "docs" / "evals" / "baselines.md"
    assert json_path.is_file() and markdown_path.is_file()
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert [cell["key"] for cell in report["cells"]] == [f"{v}/reason" for v in variants]
    assert all(cell["complete"] for cell in report["cells"])


def test_une_reconstruction_apres_execution_refuse_avant_tout_rapport_ou_gate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    _corpus_, index = _corpus()
    guide = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())
    vrai_executer = runner._executer_puis_fermer
    json_path = tmp_path / "ne-doit-pas-exister.json"
    md_path = tmp_path / "ne-doit-pas-exister.md"

    async def reconstruire_apres(*args: Any, **kwargs: Any) -> Any:
        resultats = await vrai_executer(*args, **kwargs)
        data = tmp_path / "data"
        espace = EspacePublie(tmp_path, data)
        manifest = (data / "manifest.json").read_bytes()
        espace.basculer([(data / "manifest.json", manifest)])
        espace.basculer([(data / "manifest.json", manifest)])
        return resultats

    monkeypatch.setattr(runner, "_executer_puis_fermer", reconstruire_apres)
    code = _main(
        tmp_path,
        ["--suite", "guide", "--output-json", str(json_path),
         "--output-markdown", str(md_path)],
        monkeypatch,
        reponses_guide=[guide],
    )
    capture = capsys.readouterr()
    assert code == 2 and "reconstruite" in capture.err
    assert not json_path.exists() and not md_path.exists()
    assert "rapports écrits" not in capture.out


def test_gate_builder_ecrit_deux_diagnostics_rouges(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Les suites tournent, mais la provenance builder ne peut produire aucune preuve verte."""
    _corpus_, index = _corpus()
    guide = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())
    sinistre = (_reponse([_claim(_citation(index, f"{CONTRAT}:p34:1", "mobilier assuré"))],
                         verdict=Verdict(value="sous_conditions", reason="r")), _trace("sinistre"))
    assert _main(tmp_path, ["--gate", GUIDE], monkeypatch, reponses_guide=[guide]) == 1
    assert _main(tmp_path, ["--gate", CONTRAT], monkeypatch, reponses_sinistre=[sinistre]) == 1
    manifest = json.loads((tmp_path / "data" / "manifest.json").read_text(encoding="utf-8"))
    dernier_rapport = json.loads((tmp_path / "eval-results.json").read_text(encoding="utf-8"))
    for doc_id in (GUIDE, CONTRAT):
        gate = manifest[doc_id]["gate"]
        assert gate["evals_ok"] is False and gate["profile"] == "vertical" and gate["cases"] == 1
        assert set(gate) >= {"profile", "source_hash", "ingest_fingerprint", "cases_hash",
                             "pipeline_digest", "prompts_digest", "model_ids", "evals_ok", "date",
                             "overlay_hash", "cases", "countersigned"}
    assert dernier_rapport["cases_hash"] == manifest[CONTRAT]["gate"]["cases_hash"]
    assert dernier_rapport["identity"]["image"]["pipeline_digest"]
    assert dernier_rapport["identity"]["scope"]["case_ids"] == ["s-bougie"]
    assert CONTRAT in dernier_rapport["identity"]["documents"]
    # Le corpus refuse honnêtement ces diagnostics non probants.
    from server.app.domain.ingest import GateContext
    contexte = GateContext(pipeline_digest=manifest[GUIDE]["gate"]["pipeline_digest"],
                           prompts_digest=manifest[GUIDE]["gate"]["prompts_digest"],
                           model_ids=manifest[GUIDE]["gate"]["model_ids"])
    corpus = load_corpus(tmp_path / "data", allow_ungated=False, current=contexte)
    assert corpus.documents == {}
    assert corpus.quarantine == {GUIDE: "gate_echoue", CONTRAT: "gate_echoue"}


def test_gate_orchestrateur_fusionne_la_preuve_externe_et_peut_devenir_vert(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """La provenance ne suffit pas : le témoin repo externe doit être fourni et recalculé.

    Story 4.5 (M2), durci par la revue B1 : la preuve porte la **révision candidate** et l'empreinte
    du rapport dont elle dérive, et le rapport référencé doit **se reconnaître** dans la preuve —
    même `run_digest`, recalculé depuis son identité, même révision, même plancher, même image. La
    preuve construite ici est donc réellement valide de bout en bout : c'est la seule façon
    d'exercer le chemin vert sans désarmer un contrôle. Les cinq façons de ne pas concorder sont
    couvertes par `tests/test_plancher.py -k candidate_revision`.
    """
    charge = charger_plancher()
    revision = "1" * 40
    # L'identité d'un run mesuré par l'orchestrateur, telle que `identite_run` la construit :
    # `run_digest` est l'empreinte canonique de l'identité **privée de sa propre clé**.
    identite: dict[str, Any] = {
        "candidate_revision": revision,
        # **Les cinq champs** que `identite_run` publie : depuis la revue B1, une identité d'image
        # incomplète ferme au lieu de laisser les champs manquants hors comparaison. C'est
        # `image_du_run` qui en est l'autorité, des deux côtés.
        "image": runner.image_du_run(charge.digest),
        "scope": {"profile": "full", "repeat": 3},
    }
    identite["run_digest"] = runner.empreinte_canonique(identite)
    rapport_source = tmp_path / "rapport-orchestrateur.json"
    rapport_source.write_text(json.dumps({
        "schema_version": 3, "plancher_digest": charge.digest, "identity": identite,
    }) + "\n", encoding="utf-8")
    preuve = tmp_path / "preuve-orchestrateur.json"
    preuve.write_text(json.dumps({
        "plancher_digest": charge.digest,
        "candidate_revision": revision,
        "report_digest": hashlib.sha256(rapport_source.read_bytes()).hexdigest(),
        "run_digest": identite["run_digest"],
        "decisions": [{
            "metric": "offline_tests_pass_rate", "n": 3, "value": 1.0,
            "run_digest": identite["run_digest"],
        }],
    }), encoding="utf-8")
    _corpus_, index = _corpus()
    guide = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())
    monkeypatch.setenv("LIVE_CAMPAIGN_ID", "test-gate-orchestrateur")
    code = _main(
        tmp_path,
        ["--gate", GUIDE, "--repeat", "3", "--producer", "orchestrator",
         # Le plafond de run suit la configuration : « 1.0 » valait pour un plafond par
         # requête de 0,18 €, et le relèvement des budgets Sonnet (02/09/2026) faisait
         # refuser ce run avant son premier appel, pour une raison étrangère au test.
         "--series-kind", "final", "--series-id", "final-guide",
         "--max-cost", str(estimate_run_majorant(3, _settings())),
         "--candidate-revision", revision,
         "--orchestrator-evidence", str(preuve),
         "--orchestrator-report", str(rapport_source)],
        monkeypatch, reponses_guide=[guide, guide, guide])
    assert code == 0
    manifest = json.loads((tmp_path / "data" / "manifest.json").read_text(encoding="utf-8"))
    gate = manifest[GUIDE]["gate"]
    assert gate["evals_ok"] is True
    assert {d["metric"] for d in gate["decisions"]} >= {
        "offline_tests_pass_rate", "cases_ok_rate", "stabilite_guide", "executions_completes"}


def test_un_troisieme_contrat_execute_sa_suite_son_dictionnaire_et_son_gate_de_bout_en_bout(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    texte = "Le sofa confié à un visiteur fait partie du mobilier assuré."
    document = _document(TROISIEME, "contrat", texte, "p7")
    octets = json.dumps(document.model_dump(mode="json", exclude_defaults=True),
                        ensure_ascii=False, sort_keys=True).encode("utf-8")
    dossier = data / TROISIEME
    dossier.mkdir()
    (dossier / "document.json").write_bytes(octets)
    (dossier / "summary.md").write_text(f"# {TROISIEME}", encoding="utf-8")
    (dossier / "dictionary.json").write_text(json.dumps({
        "schema_version": "1", "corpus_source_hashes": {TROISIEME: "s"},
        "corpus": {"mobilier": ["sofa visiteur"]}, "intents": {},
        "candidate_questions": {}, "validated": False,
        "validated_by": None, "validated_at": None,
    }, ensure_ascii=False), encoding="utf-8")
    manifest_path = data / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest[TROISIEME] = {
        "status": "servi", "source_hash": "s", "ingest_fingerprint": "f",
        "document_hash": hashlib.sha256(octets).hexdigest(), "edition": "2020",
        "overlay_hash": None, "gate": None,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    poser_espace(tmp_path, data_dir=data)
    autres_avant = {doc_id: entree for doc_id, entree in manifest.items() if doc_id != TROISIEME}

    cases = _cases_dir(tmp_path, autres={
        f"sinistre/{TROISIEME}/t-temoin.yaml": CAS_SINISTRE.format(id="t-temoin")})
    corpus = load_corpus(data, allow_ungated=True)
    index = Index(corpus)
    reponse = _reponse([
        _claim(_citation(index, f"{TROISIEME}:p7:1", "mobilier assuré"))],
        verdict=Verdict(value="sous_conditions", reason="r"))
    double = DoublePipeline([(reponse, _trace("sinistre"))])
    _COURANT["guide"] = DoublePipeline([])
    _COURANT["sinistre"] = double
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(runner, "Settings", lambda: _settings())

    code = runner.main(["--gate", TROISIEME, "--cases-dir", str(cases),
                        "--data-dir", str(data)])
    assert code == 1 and len(double.appels) == 1
    appel = double.appels[0]
    assert appel["args"][0] == TROISIEME
    dictionnaire = appel["kw"]["dictionnaire"]
    assert dictionnaire.doc_id == TROISIEME and dictionnaire.charge and dictionnaire.corpus_ok
    assert dictionnaire.expand(["sofa visiteur"])["sofa visiteur"] == ["mobilier"]

    apres = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert {doc_id: entree for doc_id, entree in apres.items() if doc_id != TROISIEME} == autres_avant
    gate = apres[TROISIEME]["gate"]
    cas_charge = runner.charger_cas(cases, suites=(f"sinistre/{TROISIEME}",))
    fichiers = [cas.case_path for cas in cas_charge if cas.case_path is not None]
    from server.app.digests import cases_hash
    assert gate["cases"] == 1 and gate["evals_ok"] is False
    assert gate["cases_hash"] == cases_hash(fichiers, cases)


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


def test_un_sinistre_rouge_fait_echouer_un_run_sans_gate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code = _main(
        tmp_path, ["--suite", "sinistre"], monkeypatch,
        reponses_sinistre=[(_refus(), _trace("sinistre"))])
    assert code == 1


def test_gate_en_echec_technique_ne_modifie_pas_le_manifest(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Matrice : « manifest non modifié, code 3 — un incident n'est pas un verdict »."""
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    avant = (data / "manifest.json").read_text(encoding="utf-8")
    json_path, md_path = tmp_path / "timeout.json", tmp_path / "timeout.md"
    code = _main(tmp_path, ["--gate", GUIDE, "--output-json", str(json_path),
                            "--output-markdown", str(md_path)], monkeypatch,
                 reponses_guide=[Timeout("deadline")])
    assert code == 3
    assert (data / "manifest.json").read_text(encoding="utf-8") == avant
    assert json_path.is_file() and md_path.is_file()
    rapport = json.loads(json_path.read_text(encoding="utf-8"))
    assert rapport["complete"] is False
    assert rapport["unexecuted_cases"] == ["g-luxtrust"]
    assert rapport["stop_http"] == 503
    assert rapport["decisions"] and all(d["status"] == "red" for d in rapport["decisions"])


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


def test_gate_refuse_une_variante_non_servie_avant_pipeline(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `full_context` est une variante **connue** du guide et pourtant non servie : c'est exactement
    # ce que le gate doit refuser — il mesure l'image servie, pas une variante de l'image.
    assert _main(tmp_path, ["--gate", GUIDE, "--variant", "full_context"], monkeypatch) == 2
    assert _COURANT["guide"].appels == []


def test_gate_et_quick_sont_exclusifs_avant_pipeline(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _main(tmp_path, ["--gate", GUIDE, "--quick"], monkeypatch) == 2
    assert _COURANT["guide"].appels == []


@pytest.mark.parametrize("collision", [
    "sorties-identiques", "sortie-dans-cases", "sortie-ancetre-data",
    "cache-dans-data", "cases-dans-data", "cache-egale-cases", "sortie-dans-reference",
])
def test_tous_les_chevauchements_de_chemins_sont_refuses(
        collision: str, tmp_path: Path) -> None:
    cases = tmp_path / "cases"
    data = tmp_path / "data"
    cases.mkdir()
    data.mkdir()
    valeurs = {
        "output_json": tmp_path / "r.json",
        "output_markdown": tmp_path / "r.md",
        "cases_dir": cases,
        "reference_dir": tmp_path / "reference",
        "data_dir": data,
        "cache_dir": tmp_path / "cache",
    }
    if collision == "sorties-identiques":
        valeurs["output_markdown"] = valeurs["output_json"]
    elif collision == "sortie-dans-cases":
        valeurs["output_json"] = cases / "r.json"
    elif collision == "sortie-ancetre-data":
        valeurs["output_json"] = tmp_path
    elif collision == "cache-dans-data":
        valeurs["cache_dir"] = data / "cache"
    elif collision == "cases-dans-data":
        valeurs["cases_dir"] = data / "cases"
    elif collision == "cache-egale-cases":
        valeurs["cache_dir"] = cases
    elif collision == "sortie-dans-reference":
        valeurs["output_json"] = valeurs["reference_dir"] / "r.json"
    with pytest.raises(runner.RefusDeTourner, match="collision"):
        runner.valider_chemins(**valeurs)


@pytest.mark.parametrize("sortie", ["output_json", "output_markdown"])
def test_une_sortie_existante_comme_repertoire_est_refusee(
        sortie: str, tmp_path: Path) -> None:
    cases, data, cache = tmp_path / "cases", tmp_path / "data", tmp_path / "cache"
    cases.mkdir()
    data.mkdir()
    repertoire = tmp_path / "sortie"
    repertoire.mkdir()
    valeurs = {
        "output_json": tmp_path / "r.json", "output_markdown": tmp_path / "r.md",
        "cases_dir": cases, "data_dir": data, "cache_dir": cache,
        "reference_dir": tmp_path / "reference",
    }
    valeurs[sortie] = repertoire
    with pytest.raises(runner.RefusDeTourner, match="répertoire"):
        runner.valider_chemins(**valeurs)


def test_profil_full_est_adressable_en_dry_run_sans_rien_charger(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _main(tmp_path, ["--profile", "full", "--dry-run"], monkeypatch) == 0


def test_un_plafond_nul_est_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLAUDE.md : « les évals tournent seulement avec la clé **et un plafond** »."""
    assert _main(tmp_path, ["--suite", "guide", "--max-cost", "0"], monkeypatch) == 2


def test_arret_budget_ecrit_les_deux_rapports_partiels_sans_faux_label(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Matrice 4.1 : le cas acquis reste publié et le suivant est non exécuté, pas labellisé."""
    _corpus_, index = _corpus()
    guide = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())
    monkeypatch.setattr(runner, "estimate_run_majorant", lambda *_: 0.01)
    json_path, md_path = tmp_path / "partiel.json", tmp_path / "partiel.md"
    code = _main(
        tmp_path,
        ["--max-cost", "0.02", "--output-json", str(json_path),
         "--output-markdown", str(md_path)],
        monkeypatch, reponses_guide=[guide],
    )
    assert code == 3
    rapport = json.loads(json_path.read_text("utf-8"))
    assert rapport["complete"] is False and rapport["cases_completed"] == 1
    assert rapport["results"][0]["label"] == "bonne_reponse"
    assert rapport["unexecuted_cases"] == ["s-bougie"]
    assert "Run **partiel**" in md_path.read_text("utf-8")


def test_arret_budget_pendant_le_premier_cas_ecrit_les_deux_rapports_partiels(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "estimate_run_majorant", lambda *_: 0.01)
    json_path, md_path = tmp_path / "pendant.json", tmp_path / "pendant.md"
    code = _main(
        tmp_path,
        ["--suite", "guide", "--max-cost", "0.03", "--output-json", str(json_path),
         "--output-markdown", str(md_path)],
        monkeypatch, reponses_guide=[BudgetExceeded("majorant > reste")],
    )
    assert code == 3 and json_path.is_file() and md_path.is_file()
    rapport = json.loads(json_path.read_text("utf-8"))
    assert rapport["complete"] is False and rapport["cases_completed"] == 0
    assert rapport["unexecuted_cases"] == ["g-luxtrust"]
    assert "Run **partiel**" in md_path.read_text("utf-8")


@pytest.mark.parametrize("partiel", [False, True])
def test_une_erreur_de_rapport_reste_un_incident_et_ne_touche_pas_le_gate(
        partiel: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    # Story 4.5, N3 : le runner exige une racine **installée** et refuse avant toute
    # mesure sinon. La disposition se pose ici, comme la CI et l'opérateur la posent.
    poser_espace(tmp_path, data_dir=data)
    avant = (data / "manifest.json").read_text("utf-8")
    cases = _cases_dir(tmp_path, guide=CAS_GUIDE, sinistre=CAS_SINISTRE)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(runner, "Settings", lambda: _settings())
    monkeypatch.setattr(runner, "estimate_run_majorant", lambda *_: 0.01)
    monkeypatch.setattr(runner, "preparer_les_rapports", lambda *a, **k: (_ for _ in ()).throw(
        OSError("disque indisponible")))
    _corpus_, index = _corpus()
    reponse: Any = (BudgetExceeded("majorant > reste") if partiel else (
        _reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace()))
    _COURANT["guide"] = DoublePipeline([reponse])
    _COURANT["sinistre"] = DoublePipeline([])

    code = runner.main([
        "--gate", GUIDE, "--max-cost", "0.03", "--cases-dir", str(cases),
        "--data-dir", str(data)])

    assert code == (3 if partiel else 1)
    assert (data / "manifest.json").read_text("utf-8") == avant


def test_un_cas_modifie_pendant_le_run_ne_peut_pas_etre_certifie(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    # Story 4.5, N3 : le runner exige une racine **installée** et refuse avant toute
    # mesure sinon. La disposition se pose ici, comme la CI et l'opérateur la posent.
    poser_espace(tmp_path, data_dir=data)
    avant = (data / "manifest.json").read_text("utf-8")
    cases = _cases_dir(tmp_path, guide=CAS_GUIDE, sinistre=CAS_SINISTRE)
    chemin = cases / "guide" / "g-luxtrust.yaml"
    _corpus_, index = _corpus()
    nominal = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())

    class PipelineQuiModifieLeCas(DoublePipeline):
        async def __call__(self, *args: Any, **kw: Any) -> tuple[Answer, Trace]:
            resultat = await super().__call__(*args, **kw)
            chemin.write_text(chemin.read_text("utf-8") + "\n# mutation pendant le run\n",
                               encoding="utf-8")
            return resultat

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(runner, "Settings", lambda: _settings())
    _COURANT["guide"] = PipelineQuiModifieLeCas([nominal])
    _COURANT["sinistre"] = DoublePipeline([])

    code = runner.main([
        "--gate", GUIDE, "--cases-dir", str(cases), "--data-dir", str(data)])

    assert code == 3
    assert (data / "manifest.json").read_text("utf-8") == avant
    assert not (tmp_path / "eval-results.json").exists()
    assert not (tmp_path / "eval-results.md").exists()


def test_suite_et_gate_contradictoires_sont_refuses(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    assert _main(tmp_path, ["--gate", GUIDE, "--suite", "sinistre"], monkeypatch) == 2


def test_un_cas_inconnu_est_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _main(tmp_path, ["--case", "n-existe-pas"], monkeypatch) == 2


def test_suite_sinistre_selectionne_axa_et_tous_les_documents(
        capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.main(["--suite", "sinistre", "--profile", "full", "--dry-run"]) == 0
    sortie = capsys.readouterr().out
    assert "cas=16" in sortie
    assert "s-telephone-vacances" in sortie
    assert "b-congelateur" in sortie
    assert "s-tuile-voiture-invite" in sortie


def test_quick_fournisseur_exclut_le_parsing_avant_selection(
        capsys: pytest.CaptureFixture[str]) -> None:
    assert runner.main([
        "--profile", "full", "--exclude-suite", "parsing", "--quick", "--dry-run",
    ]) == 0
    sortie = capsys.readouterr().out
    assert "cas=3" in sortie and "suites=guide,sinistre" in sortie
    assert "p-baloise-acceptation" not in sortie


def test_un_case_parsing_sans_suite_resout_avant_la_cle(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(runner, "LlmClient", _interdit)
    # Copie du corpus servi réel (story 4.5, B7) : `p-axa-chaleur` se juge contre le vrai document
    # ingéré, et les rapports doivent basculer par un espace de publication, pas par `data/` du dépôt.
    data = tmp_path / "data"
    shutil.copytree(runner.DATA_DIR, data, symlinks=True)
    poser_espace(tmp_path, data_dir=data, cibles=(Path("p.json"), Path("p.md")))
    code = runner.main([
        "--case", "p-axa-chaleur", "--profile", "full", "--max-cost", "0.01",
        "--data-dir", str(data),
        "--output-json", str(tmp_path / "p.json"),
        "--output-markdown", str(tmp_path / "p.md"),
    ])
    assert code == 0


def test_un_lot_mixte_garde_lexigence_de_cle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(runner, "construire_contexte", _interdit)
    assert runner.main(["--profile", "full", "--max-cost", "0.01"]) == 2


def test_execution_full_mixte_hors_ligne_et_digest_references_reel(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    poser_espace(tmp_path, data_dir=data, cibles=(Path("mixte.json"), Path("mixte.md")))
    cases = _cases_dir(tmp_path, guide=CAS_GUIDE, sinistre=CAS_SINISTRE)
    parsing_dir = cases / "parsing" / GUIDE
    parsing_dir.mkdir(parents=True)
    parsing_dir.joinpath("p-mini.yaml").write_text(
        "id: p-mini\nsuite: parsing\nprofile: full\nquestion: lecture locale\n"
        "scenario: miniature exacte\nfamille: definition\n"
        f"expected:\n  found: true\n  block_ids: [{GUIDE}:ffiche:1]\n"
        f"  text_norm: {normalize(TEXTE_GUIDE)!r}\n"
        "mode_attendu: bonne_reponse\ntruth:\n  source: lecture_humaine\n"
        "  countersigned_by: null\n  validated_by_expert: false\n"
        "  note: La boucle a préparé la miniature locale ; la contresignature humaine reste due.\n",
        encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(runner, "Settings", lambda: _settings())
    _corpus_, index = _corpus()
    guide = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())
    sinistre = (_reponse(
        [_claim(_citation(index, f"{CONTRAT}:p34:1", "mobilier assuré"))],
        verdict=Verdict(value="sous_conditions", reason="r")), _trace("sinistre"))

    def run_once() -> dict[str, Any]:
        _COURANT["guide"] = DoublePipeline([guide])
        _COURANT["sinistre"] = DoublePipeline([sinistre])
        assert runner.main([
            # Deux exécutions payantes : le plafond de run suit le majorant par requête du produit.
            "--profile", "full", "--max-cost", f"{2 * _settings().max_cost_eur_per_request + 0.1:.2f}",
            "--cases-dir", str(cases),
            "--data-dir", str(data), "--output-json", str(tmp_path / "mixte.json"),
            "--output-markdown", str(tmp_path / "mixte.md"),
        ]) == 0
        return json.loads((tmp_path / "mixte.json").read_text("utf-8"))

    premier = run_once()
    assert {r["suite"] for r in premier["results"]} == {"guide", "sinistre", "parsing"}
    from server.app.digests import cases_hash
    chemins = [c.case_path for c in runner.charger_cas(cases) if c.case_path is not None]
    assert premier["cases_hash"] == cases_hash(chemins, cases)
    digest_1 = premier["identity"]["scope"]["references_digest"]
    assert digest_1 == runner.charger_references(
        runner.charger_cas(cases), tmp_path / "reference").digest
    utilite = tmp_path / "reference" / "utilite.yaml"
    utilite.write_text(utilite.read_text("utf-8").replace(
        "fixture locale", "fixture locale relue"), encoding="utf-8")
    second = run_once()
    assert second["identity"]["scope"]["references_digest"] != digest_1
    assert second["cases_hash"] == premier["cases_hash"]


# --- convention Couches ---------------------------------------------------

def test_le_seuil_du_plafond_de_run_vit_dans_config() -> None:
    """Convention Seuils : aucun nombre en dur dans `run.py`."""
    s = Settings(_env_file=None)
    assert s.evals_max_cost_eur > 0
    assert "evals_max_cost_eur" in s.thresholds()
    source = (Path(runner.__file__)).read_text(encoding="utf-8")
    assert "evals_max_cost_eur" in source


def test_le_runner_appelle_les_pipelines_avec_leur_vraie_signature() -> None:
    """Le seul point d'intégration du runner est doublé partout ailleurs dans ce module.

    `DoublePipeline.__call__(*args, **kw)` accepte n'importe quel nom d'argument : renommer
    `pipeline_digest_hex` dans `pipelines/guide.py` laisserait ces 47 tests verts, et
    `evals run --gate` — l'unique écrivain de `manifest.gate` (AD-7) — casserait sur un `TypeError`
    au premier run **payé**. Ce test lie l'appel aux vraies signatures, sans réseau et sans appel.
    """
    import inspect

    from server.app.pipelines.guide import repondre_guide as vrai_guide
    from server.app.pipelines.sinistre import run as vrai_sinistre

    commun = dict(corpus=object(), index=object(), client=object(), settings=_settings(),
                  request_id="eval-x", lang="fr", budget=object(),
                  pipeline_digest_hex="pd", prompts_digest_hex="pp")
    # Exactement ce que `executer_cas` construit, pour les deux suites.
    inspect.signature(vrai_guide).bind("question", [], None, doc_id=GUIDE, **commun)
    inspect.signature(vrai_sinistre).bind(CONTRAT, "question", None, **commun)


def test_un_run_sans_gate_ne_touche_jamais_le_manifest(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """`--gate` est le **seul** chemin d'écriture : mesurer ne doit rien changer à ce qui est servi."""
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    avant = (data / "manifest.json").read_text(encoding="utf-8")
    _corpus_, index = _corpus()
    bonne = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())
    assert _main(tmp_path, ["--suite", "guide"], monkeypatch, reponses_guide=[bonne]) == 0
    assert (data / "manifest.json").read_text(encoding="utf-8") == avant


def test_un_gate_rouge_peut_etre_repris(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un verdict d'éval ne doit pas rendre l'éval impossible.

    `loader._gate_alerts` met en quarantaine, **sans dérogation**, tout document dont le gate porte
    `evals_ok: false` (AD-8 : « jamais servi »). C'est juste au service, et c'était un cul-de-sac
    pour la mesure : après un run rouge, `--gate {doc_id}` refusait éternellement « document non
    servi », et le seul chemin de sortie était une édition à la main de `data/manifest.json` — que la
    spec interdit (« les gates écrits **par le runner**, jamais à la main »).
    """
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    # 1. un run rouge écrit `evals_ok: false` et le document part en quarantaine.
    assert _main(tmp_path, ["--gate", GUIDE], monkeypatch, reponses_guide=[(_refus(), _trace())]) == 1
    assert _main(
        tmp_path, ["--gate", CONTRAT], monkeypatch,
        reponses_sinistre=[(_refus(), _trace("sinistre"))]) == 1
    avant = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    gate_autre = avant[CONTRAT]["gate"]
    assert load_corpus(data, allow_ungated=True).quarantine == {
        GUIDE: "gate_echoue", CONTRAT: "gate_echoue"}

    # 2. le run suivant peut mesurer la cible sans neutraliser l'autre gate rouge dans le Corpus.
    vrai_load_corpus = runner.load_corpus
    corpus_du_run: list[Corpus] = []

    def observer_corpus(*args: Any, **kwargs: Any) -> Corpus:
        corpus = vrai_load_corpus(*args, **kwargs)
        corpus_du_run.append(corpus)
        return corpus

    monkeypatch.setattr(runner, "load_corpus", observer_corpus)
    _corpus_, index = _corpus()
    bonne = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())
    assert _main(tmp_path, ["--gate", GUIDE], monkeypatch, reponses_guide=[bonne]) == 1
    assert corpus_du_run and GUIDE in corpus_du_run[-1].served
    assert corpus_du_run[-1].quarantine[CONTRAT] == "gate_echoue"
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    assert manifest[GUIDE]["gate"]["evals_ok"] is False
    assert manifest[CONTRAT]["gate"] == gate_autre
    # La reprise a bien exécuté le document sans élargir la dérogation, puis le service reste fermé.
    corpus = load_corpus(data, allow_ungated=False)
    assert corpus.served == [] and corpus.quarantine == {
        GUIDE: "gate_echoue", CONTRAT: "gate_echoue"}


@pytest.mark.parametrize("forme", ["scalaire", "liste", "full-preprotocole"])
def test_le_vrai_cli_reprend_un_gate_hors_schema_sur_le_custom_installe(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forme: str) -> None:
    """La reprise hors schéma passe par `main`, le data-dir original et son repère pincé."""
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    manifest_path = data / "manifest.json"
    brut = json.loads(manifest_path.read_text("utf-8"))
    if forme == "scalaire":
        gate_existant: Any = 7
    elif forme == "liste":
        gate_existant = ["gate", "historique"]
    else:
        gate_existant = {
            "profile": "full", "source_hash": brut[GUIDE]["source_hash"],
            "ingest_fingerprint": brut[GUIDE]["ingest_fingerprint"], "cases_hash": "c",
            "pipeline_digest": "p", "prompts_digest": "q", "model_ids": {},
            "evals_ok": True, "date": "2026-08-23", "overlay_hash": None, "cases": 1,
            "countersigned": False, "decisions": [], "run_digest": None,
            "plancher_digest": "a" * 64, "candidate_revision": "b" * 40,
            "report_digest": "c" * 64,
        }
    brut[GUIDE]["gate"] = gate_existant
    manifest_path.write_text(json.dumps(brut, indent=2) + "\n", encoding="utf-8")
    _corpus_, index = _corpus()
    bonne = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())
    vrai_load_corpus = runner.load_corpus
    observations: list[tuple[int, int, str]] = []

    def observer_load_corpus(data_dir: Path | str, **kwargs: Any) -> Corpus:
        lecture = kwargs.get("lecture")
        capacite = kwargs.get("capacite_regate")
        assert Path(data_dir) == data, "le CLI doit garder le data-dir custom original"
        assert lecture is not None and lecture.generation is not None
        assert lecture.racine is not None and lecture.racine.data_dir == data
        assert capacite is not None and capacite.lecture is lecture
        assert capacite.cible == GUIDE and kwargs.get("regate") == GUIDE
        assert kwargs.get("neutraliser_regate") is True
        observations.append((id(lecture), id(capacite), lecture.generation))
        return vrai_load_corpus(data_dir, **kwargs)

    monkeypatch.setattr(runner, "load_corpus", observer_load_corpus)

    code = _main(tmp_path, ["--gate", GUIDE], monkeypatch, reponses_guide=[bonne])

    assert code == 1
    gate = json.loads(manifest_path.read_text("utf-8"))[GUIDE]["gate"]
    assert len(observations) == 1
    assert not any(p.is_dir() and p.name.startswith("evals-regate") for p in tmp_path.rglob("*"))
    assert len(_COURANT["guide"].appels) == 1, "le vrai CLI n'a pas mesuré le document repris"
    assert gate == gate_existant, (
        "le candidat builder rouge ne doit pas écraser le gate inconnu ou préprotocole")


@pytest.mark.parametrize("fraiche", [False, True])
def test_le_regate_neutralise_un_gate_perime_mais_pas_un_gate_vert_frais(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fraiche: bool) -> None:
    """La qualification précède la capacité : un gate vert/frais garde son jugement."""
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    assert _main(
        tmp_path, ["--gate", GUIDE], monkeypatch,
        reponses_guide=[(_refus(), _trace())]) == 1
    manifest_path = data / "manifest.json"
    brut = json.loads(manifest_path.read_text("utf-8"))
    gate = brut[GUIDE]["gate"]
    gate["evals_ok"] = True
    gate["decisions"] = []
    gate["pipeline_digest"] = runner.pipeline_digest() if fraiche else "pipeline-perime"
    gate["prompts_digest"] = runner.prompts_digest()
    gate["model_ids"] = dict(runner.TIERS)
    gate["pipeline_settings"] = _settings().thresholds()
    EspacePublie(tmp_path, data).basculer([
        (manifest_path, json.dumps(brut, indent=2, ensure_ascii=False) + "\n")])
    from server.app.corpus.racine import lecture_pincee_regate

    with lecture_pincee_regate(data, GUIDE) as capacite:
        ctx = runner.construire_contexte(
            _settings(), data, regate=GUIDE, lecture=capacite.lecture,
            capacite_regate=capacite)
        alertes = ctx.index.corpus.alerts[GUIDE]
        asyncio.run(ctx.client.aclose())

    if fraiche:
        assert "sans_gate" not in alertes and "gate_perime" not in alertes
    else:
        assert "sans_gate" in alertes and "gate_perime" not in alertes


def test_aucune_production_natteint_la_lecture_rootless() -> None:
    """Garde structurelle N3 : une définition privée, aucun import ni appel dans `server/`."""
    serveur = Path(runner.__file__).resolve().parents[1]
    occurrences = [
        (chemin.relative_to(serveur), no, ligne.strip())
        for chemin in serveur.rglob("*.py")
        for no, ligne in enumerate(chemin.read_text("utf-8").splitlines(), 1)
        if "_lecture_interne_sans_racine" in ligne
    ]
    assert occurrences, "la garde privée rootless doit rester définie pour les sondes historiques"
    assert occurrences == [
        (Path("app/corpus/racine.py"), occurrences[0][1],
         "def _lecture_interne_sans_racine(data_dir: Path | str) -> Lecture:")
    ]


def test_un_document_en_quarantaine_pour_autre_chose_reste_refuse(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """La reprise d'un gate rouge ne déroge qu'au gate rouge : le reste d'AD-7 s'applique."""
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    brut = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    brut[GUIDE]["gate"] = {"profile": "vertical", "source_hash": "s", "ingest_fingerprint": "f",
                           "cases_hash": "c", "pipeline_digest": "p", "prompts_digest": "q",
                           "model_ids": {}, "evals_ok": False, "date": "2026-08-24",
                           "overlay_hash": None, "cases": 1, "countersigned": False}
    brut[GUIDE]["document_hash"] = "un-hash-qui-ne-correspond-plus"
    (data / "manifest.json").write_text(json.dumps(brut, indent=2) + "\n", encoding="utf-8")
    assert _main(tmp_path, ["--gate", GUIDE], monkeypatch) == 2


def test_gate_et_case_sont_exclusifs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un gate se réclame de la suite qui sert le document, jamais d'un cas choisi à la main (D5)."""
    assert _main(tmp_path, ["--gate", GUIDE, "--case", "g-luxtrust"], monkeypatch) == 2


@pytest.mark.parametrize("valeur", ["inf", "nan", "-1"])
def test_un_plafond_non_fini_est_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        valeur: str) -> None:
    """`argparse(type=float)` accepte « inf » et « nan », et `nan <= 0` est **faux**.

    Sans `math.isfinite`, `--max-cost inf` neutralisait le plafond et le run partait sans borne —
    contre AD-9 et contre CLAUDE.md (« les évals tournent seulement avec la clé **et un plafond** »).
    """
    assert _main(tmp_path, ["--suite", "guide", "--max-cost", valeur], monkeypatch) == 2


def test_un_cas_depose_sous_une_autre_extension_nest_pas_ignore(tmp_path: Path) -> None:
    """Un `.yml` glissé dans une suite serait ignoré en silence, et le gate amputé sans un mot."""
    racine = _cases_dir(tmp_path, guide=CAS_GUIDE)
    (racine / "guide" / "g-oublie.yml").write_text("id: g-oublie\n", encoding="utf-8")
    with pytest.raises(runner.RefusDeTourner) as exc:
        runner.charger_cas(racine)
    assert "g-oublie.yml" in str(exc.value)


def test_une_exception_inattendue_est_un_incident_pas_un_verdict(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """Code 3, pas 1 : un bug du runner ne doit pas se lire comme « un cas a rendu un mauvais label ».

    Un `TypeError` — une signature de pipeline qui a bougé — sortait en code 1 par le défaut de
    Python. Un appelant, ou la CI, aurait lu un bug comme un verdict d'éval.
    """
    class Casse:
        async def __call__(self, *a: Any, **k: Any) -> Any:
            raise TypeError("repondre_guide() got an unexpected keyword argument")

    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    # Story 4.5, N3 : le runner exige une racine **installée** et refuse avant toute
    # mesure sinon. La disposition se pose ici, comme la CI et l'opérateur la posent.
    poser_espace(tmp_path, data_dir=data)
    avant = (data / "manifest.json").read_text(encoding="utf-8")
    cases = _cases_dir(tmp_path, guide=CAS_GUIDE, sinistre=CAS_SINISTRE)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(runner, "Settings", lambda: _settings())
    _COURANT["guide"] = Casse()
    _COURANT["sinistre"] = DoublePipeline([])
    code = runner.main(["--gate", GUIDE, "--cases-dir", str(cases), "--data-dir", str(data)])
    assert code == 3
    assert (data / "manifest.json").read_text(encoding="utf-8") == avant


def test_un_cas_full_est_accepte_par_le_contrat_4_1(tmp_path: Path) -> None:
    racine = _cases_dir(tmp_path, guide=CAS_GUIDE)
    (racine / "guide" / "g-plus-tard.yaml").write_text(
        CAS_GUIDE.format(id="g-plus-tard", profile="full", fiche=f"{GUIDE}:n1"), encoding="utf-8")
    cas = runner.charger_cas(racine)          # la lecture, elle, reste permissive : le schéma le permet
    runner.refuser_ce_qui_nest_pas_livre(cas, "full")


def test_le_profil_full_inclut_vertical_et_full_en_dry_run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    avant = (data / "manifest.json").read_text(encoding="utf-8")
    cases = _cases_dir(tmp_path, guide=CAS_GUIDE, sinistre=CAS_SINISTRE)
    (cases / "guide" / "g-plus-tard.yaml").write_text(
        CAS_GUIDE.format(id="g-plus-tard", profile="full", fiche=f"{GUIDE}:n1"), encoding="utf-8")
    utilite = tmp_path / "reference" / "utilite.yaml"
    utilite.write_text(utilite.read_text("utf-8") + (
        "  - case_id: g-plus-tard\n"
        "    ordre_juste: [Lire la fiche]\n"
        "    documents_cites: [Fiche miniature]\n"
        "    interlocuteur: LuxTrust\n"
        "    provenance: fixture locale\n"
        "    countersigned_by: null\n"), encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(runner, "Settings", lambda: _settings())
    _COURANT["guide"] = DoublePipeline([])
    _COURANT["sinistre"] = DoublePipeline([])
    poser_espace(tmp_path, data_dir=data)
    code = runner.main(["--profile", "full", "--dry-run", "--cases-dir", str(cases),
                        "--data-dir", str(data)])
    assert code == 0
    assert (data / "manifest.json").read_text(encoding="utf-8") == avant

    selection = runner.selection_profil(runner.charger_cas(cases), "full")
    assert {c.id for c in selection} >= {"g-luxtrust", "g-plus-tard"}


def test_une_attente_de_la_suite_parsing_est_refusee_ailleurs(tmp_path: Path) -> None:
    """`expected.text_norm` n'est lue par `juger()` que dans la suite `parsing` (story 4.2).

    Toutes les autres attentes produisent un écart quand elles ne sont pas tenues ; celle-là était
    acceptée sur n'importe quel cas et ignorée en silence — un cas la portant serait passé au vert
    sur une attente que personne ne vérifie.
    """
    racine = _cases_dir(tmp_path, guide=CAS_GUIDE.replace(
        "  fiche_ids:", '  text_norm: "le texte de la page"\n  fiche_ids:'))
    with pytest.raises(runner.RefusDeTourner) as exc:
        runner.charger_cas(racine)
    assert "text_norm" in str(exc.value) and "4.2" in str(exc.value)


def test_un_cas_range_dans_un_sous_dossier_nest_pas_ignore(tmp_path: Path) -> None:
    """`glob("*.yaml")` n'est pas récursif : un `cases/guide/archive/g-x.yaml` disparaîtrait."""
    racine = _cases_dir(tmp_path, guide=CAS_GUIDE)
    (racine / "guide" / "archive").mkdir()
    (racine / "guide" / "archive" / "g-x.yaml").write_text(
        CAS_GUIDE.format(id="g-x", profile="vertical", fiche=f"{GUIDE}:n1"), encoding="utf-8")
    with pytest.raises(runner.RefusDeTourner) as exc:
        runner.charger_cas(racine)
    assert "archive/" in str(exc.value)


def test_lecriture_du_gate_ne_laisse_pas_de_temporaire_ni_de_nom_partage(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue Codex 1.10, M1 : un `manifest.json.tmp` fixe est partagé par deux écrivains.

    Deux `--gate` concurrents écrivaient le même fichier temporaire ; le second pouvait déplacer un
    fichier que le premier avait déjà déplacé (`replace` en échec, `FileNotFoundError`). Le nom est
    désormais unique et créé dans le répertoire du fichier qu'il remplace — le `replace` reste
    atomique.

    Story 4.5, B7 : le fichier remplacé n'est plus `data/manifest.json` lui-même (un lien vers le
    bundle), mais son slot dans la génération inactive de l'espace de publication
    (`data/.publie/<a|b>/data/manifest.json`, `EspacePublie._ecrire_dans_bundle`) — et son préfixe
    porte désormais un point (fichier caché), comme les autres temporaires du bundle. Le nom n'en
    reste pas moins non dérivable, unique par écriture, et créé dans le répertoire exact du fichier
    qu'il remplace : c'est la même propriété, à l'endroit où elle vit désormais.
    """
    _corpus_, index = _corpus()
    guide = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())
    assert _main(tmp_path, ["--gate", GUIDE], monkeypatch, reponses_guide=[guide]) == 1
    data = tmp_path / "data"
    restes = sorted(p.name for p in data.iterdir() if p.name.startswith("manifest.json."))
    assert restes == [], restes

    # Le nom n'est pas dérivable : deux écritures successives n'ont pas le même temporaire.
    vus: list[str] = []
    vrai = runner.tempfile.mkstemp

    def espion(*a: Any, **kw: Any) -> Any:
        fd, nom = vrai(*a, **kw)
        vus.append(nom)
        return fd, nom

    monkeypatch.setattr(runner.tempfile, "mkstemp", espion)
    assert _main(tmp_path, ["--gate", GUIDE], monkeypatch, reponses_guide=[guide]) == 1
    assert _main(tmp_path, ["--gate", GUIDE], monkeypatch, reponses_guide=[guide]) == 1
    ecritures = [n for n in vus if Path(n).name.startswith(".manifest.json.") and n.endswith(".tmp")]
    assert len(ecritures) == 2 and len(set(ecritures)) == 2, ecritures
    generations = {data / REPERTOIRE_ESPACE / gen / "data" for gen in GENERATIONS}
    assert all(Path(n).parent in generations for n in ecritures), ecritures


class ClientLieASaBoucle:
    """Un client dont la fermeture ne vaut que sur la boucle qui l'a servi — comme le vrai.

    Le client réel ouvre une connexion TLS vers `api.anthropic.com` ; anyio la referme en repassant
    par le transport asyncio, donc par la boucle qui l'a ouverte. Depuis une autre boucle, ou depuis
    une boucle close, c'est `RuntimeError: Event loop is closed`. `call_soon` sur la boucle capturée
    lève exactement cette erreur, sans socket ni réseau.
    """

    def __init__(self) -> None:
        self.boucle: Any = None
        self.fermetures = 0

    def utiliser(self) -> None:
        self.boucle = asyncio.get_running_loop()

    async def aclose(self) -> None:
        self.fermetures += 1
        if self.boucle is not None:
            self.boucle.call_soon(lambda: None)


def test_le_client_se_ferme_sur_la_boucle_qui_la_servi(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Le run réel sortait en code 3, sans gate, après avoir payé les appels.

    `main()` exécutait les cas dans un premier `asyncio.run` — qui ferme sa boucle en sortant — puis
    fermait le client depuis l'`ExitStack`, dans un **second**. Le pool TLS appartenant à la première,
    `httpx`/anyio levait `Event loop is closed` ; le garde-fou « incident » de `main()` l'attrapait,
    rendait 3 et n'écrivait aucun gate. Les doubles des autres tests n'ont pas de pool : ils ne
    pouvaient pas le voir.
    """
    client = ClientLieASaBoucle()

    class PipelineQuiUtiliseLeClient(DoublePipeline):
        async def __call__(self, *args: Any, **kw: Any) -> Any:
            kw["client"].utiliser()
            return await super().__call__(*args, **kw)

    _corpus_, index = _corpus()
    reponse = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    poser_espace(tmp_path, data_dir=data)
    cases = _cases_dir(tmp_path, guide=CAS_GUIDE, sinistre=CAS_SINISTRE)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(runner, "Settings", lambda: _settings())
    monkeypatch.setattr(runner, "LlmClient", lambda *a, **k: client)
    _COURANT["guide"] = PipelineQuiUtiliseLeClient([reponse])
    _COURANT["sinistre"] = DoublePipeline([])

    code = runner.main(["--gate", GUIDE, "--cases-dir", str(cases), "--data-dir", str(data)])

    assert code == 1, "le rouge de provenance ne doit pas masquer la fermeture correcte du client"
    assert client.fermetures == 1
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    assert manifest[GUIDE]["gate"] is not None and manifest[GUIDE]["gate"]["evals_ok"] is False


def test_le_client_est_ferme_meme_quand_le_runner_refuse(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """`construire_contexte` ouvre un pool httpx ; les refus sortent avant toute exécution.

    Le `finally` d'origine entourait l'exécution : `--gate` sur un document non servi construisait le
    client puis quittait sans le fermer.
    """
    ferme: list[bool] = []

    class ClientDouble:
        async def aclose(self) -> None:
            ferme.append(True)

    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    # Story 4.5, N3 : le runner exige une racine **installée** et refuse avant toute
    # mesure sinon. La disposition se pose ici, comme la CI et l'opérateur la posent.
    poser_espace(tmp_path, data_dir=data)
    cases = _cases_dir(tmp_path, guide=CAS_GUIDE, sinistre=CAS_SINISTRE)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(runner, "Settings", lambda: _settings())
    monkeypatch.setattr(runner, "LlmClient", lambda *a, **k: ClientDouble())
    _COURANT["guide"] = DoublePipeline([])
    _COURANT["sinistre"] = DoublePipeline([])

    # Chemin de refus : le document n'est pas servi.
    assert runner.main(["--gate", "document-inconnu", "--cases-dir", str(cases),
                        "--data-dir", str(data)]) == 2
    assert ferme == [], "ce refus-là est levé avant même de construire le contexte"

    brut = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    brut[GUIDE]["status"] = "quarantaine"
    (data / "manifest.json").write_text(json.dumps(brut, indent=2) + "\n", encoding="utf-8")
    assert runner.main(["--gate", GUIDE, "--cases-dir", str(cases), "--data-dir", str(data)]) == 2
    assert ferme == [True], "le pool est resté ouvert sur le chemin de refus"


# --- story 2.1 : le gate mesure l'image, dictionnaire compris ---------------

def test_le_contexte_porte_le_dictionnaire_comme_api_etat(tmp_path: Path) -> None:
    """`Contexte` est « exactement ce qu'`api/etat.py` construit pour les routes ».

    Sans le dictionnaire, le gate mesurerait un pipeline **sans** variantes et sans le court-circuit
    d'AD-5, alors que la production les a : le gate juge l'image, pas une variante de l'image.
    """
    racine = _data_dir(tmp_path)
    ctx = runner.construire_contexte(_settings(), racine)
    assert isinstance(ctx.dictionnaire, runner.Dictionnaire)
    # Aucun `dictionary.json` ici : l'objet est inerte, et rien ne lève au chargement (AD-7).
    assert ctx.dictionnaire.charge is False and ctx.dictionnaire.court_circuit_actif is False


def test_le_meme_cache_est_cable_au_client_et_au_runner(tmp_path: Path) -> None:
    racine = _data_dir(tmp_path)
    ctx = runner.construire_contexte(_settings(), racine, cache_dir=tmp_path / "cache")
    assert ctx.response_cache is not None
    assert ctx.client._cache is ctx.response_cache


def test_le_runner_arme_la_namespace_normative_avant_le_pipeline(tmp_path: Path) -> None:
    _corpus_, index = _corpus()
    answer = _reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))])
    ctx = _contexte([(answer, _trace(variant="navigation"))])
    cache = runner.PersistentResponseCache(tmp_path / "cache")
    ctx.response_cache = cache
    attendue = runner.namespace_cache(
        _cas(id="g-namespace"), ctx, doc_id=GUIDE, variant="navigation")

    _executer(ctx, [_cas(id="g-namespace")], variant="navigation")

    assert cache.namespace_digest == runner.empreinte_canonique(attendue)


def test_le_dictionnaire_du_contexte_part_au_pipeline_du_guide() -> None:
    """Le passer au `Contexte` sans le passer au pipeline n'aurait rien mesuré du tout."""
    _corpus_, index = _corpus()
    ctx = _armer(_contexte([(_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]),
                             _trace())]))
    _executer(ctx, [_cas(id="g-luxtrust", expected={"found": True})])
    kw = ctx._guide.appels[0]["kw"]      # type: ignore[attr-defined]
    assert kw["dictionnaire"] is ctx.dictionnaire


# --- story 4.2e : la namespace de cache épingle les tiers, et deux réglages ne se confondent pas ---

def test_deux_configurations_de_tiers_ne_partagent_jamais_une_namespace_de_cache() -> None:
    """AC : « deux configurations de tiers produisent deux namespaces distincts ».

    La `namespace_cache` est **lue** ici, jamais modifiée : elle épingle déjà les tiers deux fois —
    la table `models` (tier → modèle servi) et les seuils actifs, qui portent chaque surcharge par
    étape. Ce test le prouve plutôt que de le supposer : sans lui, une campagne mesurée sous un
    réglage pourrait resservir les réponses d'un autre, et le rapport comparerait deux images.
    """
    cas = _cas(id="g-tiers")
    micro = _contexte([], settings=_settings(rediger_tier="micro"))
    reason = _contexte([], settings=_settings(rediger_tier="reason"))

    espace_micro = runner.namespace_cache(cas, micro, doc_id=GUIDE, variant="outils")
    espace_reason = runner.namespace_cache(cas, reason, doc_id=GUIDE, variant="outils")

    assert espace_micro != espace_reason
    assert (runner.empreinte_canonique(espace_micro)
            != runner.empreinte_canonique(espace_reason))
    # La surcharge se lit dans les seuils actifs, et la table des modèles servis est épinglée aussi.
    assert espace_micro["parameters"]["thresholds"]["rediger_tier_reason"] == 0
    assert espace_reason["parameters"]["thresholds"]["rediger_tier_reason"] == 1
    assert espace_micro["models"] == dict(runner.TIERS)

    # La navigation n'accepte plus le tier micro : Sonnet reason est le plancher contractuel.
    with pytest.raises(ValidationError):
        _settings(retrouver_outils_tier="micro")

    # Et à réglages identiques, la namespace est stable : le cache reste utile.
    jumeau = _contexte([], settings=_settings(rediger_tier="micro"))
    assert runner.empreinte_canonique(runner.namespace_cache(
        cas, jumeau, doc_id=GUIDE, variant="outils")) == runner.empreinte_canonique(espace_micro)


# --- R1 : un rapport que la validation canonique refuse est un **résultat**, pas un incident -------

def test_un_rapport_inexploitable_sort_en_un_et_non_en_trois(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """La ligne de partage d'AD-8, éprouvée de bout en bout par `main()`.

    Le journal du run indexait des clés que rien n'exigeait — `cases_completed`, `cases_hash`, les
    sept labels d'AD-14, les sept champs de chaque exécution. Un rapport amputé y levait un
    `KeyError`, qui n'est pas une `ValueError` : il traversait tous les handlers nommés et
    ressortait par le dernier `except Exception` en « incident », **code 3**. Un défaut de données
    étiqueté panne technique — et code 3 promet « manifest non modifié » pour une raison qui n'est
    pas la bonne, ce qui envoie chercher un problème de réseau là où une clé manque.

    Ce test tient la promesse dans les deux sens : le code **et** la cause nommée sur `stderr`.
    """
    from server.evals.publication import RapportInexploitable

    _corpus_, index = _corpus()
    bonne = (_reponse([_claim(_citation(index, f"{GUIDE}:ffiche:1", "LuxTrust"))]), _trace())
    vrai_construire = runner.construire_rapport

    def rapport_ampute(*a: Any, **k: Any) -> dict[str, Any]:
        rapport = vrai_construire(*a, **k)
        # Le rapport est complet et cohérent, à une clé près — celle que le journal indexe.
        return {cle: valeur for cle, valeur in rapport.items() if cle != "cases_completed"}

    monkeypatch.setattr(runner, "construire_rapport", rapport_ampute)
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    avant = (data / "manifest.json").read_bytes()

    code = _main(tmp_path, ["--suite", "guide"], monkeypatch, reponses_guide=[bonne])

    assert code == 1, "un défaut de données est un résultat (1), jamais un incident technique (3)"
    err = capsys.readouterr().err
    assert "refus d'écrire les rapports" in err
    assert "cases_completed" in err, "le refus doit nommer la clé fautive"
    assert "manifest non modifié" in err
    # Et il dit vrai : le manifest est byte-identique, rien n'a été écrit.
    assert (data / "manifest.json").read_bytes() == avant
    assert not [p.name for p in tmp_path.rglob("*.tmp")]
    # Le refus vient bien du contrôle canonique, nommé : `RapportInexploitable` est le seul chemin
    # par lequel ce message peut sortir (les autres handlers nomment d'autres causes).
    assert issubclass(RapportInexploitable, Exception)
    assert not issubclass(RapportInexploitable, ValueError), (
        "si c'était une ValueError, elle serait absorbée par le handler d'échec de publication "
        "du chemin gate, et la cause nommée ici disparaîtrait")


# --- N3 : le runner d'évals exige une racine installée, **avant** toute mesure --------------------

def test_le_runner_refuse_un_data_dir_non_installe_avant_toute_mesure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """N3 : le runner n'avait **aucun préflight d'espace**.

    Dans `run._main`, les seules occurrences d'`espace` étaient une construction pure — qui ne
    vérifie rien — puis quatre usages **tous postérieurs** à `_executer_puis_fermer` : sur un
    `--data-dir` non installé, la campagne entière était payée, puis le refus tombait et le rapport
    était perdu. `docs/evals/harness.md` affirmait pourtant, mot pour mot, que « le run refuse avant
    toute mesure ». Il le fait maintenant, et aucun pipeline n'est appelé.
    """
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    cases = _cases_dir(tmp_path, guide=CAS_GUIDE, sinistre=CAS_SINISTRE)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(runner, "Settings", lambda: _settings())
    appels: list[str] = []

    class PipelineSentinelle:
        async def __call__(self, *a: Any, **k: Any) -> Any:
            appels.append("appel")
            raise AssertionError("aucune mesure ne doit avoir lieu sans racine installée")

    _COURANT["guide"] = PipelineSentinelle()
    _COURANT["sinistre"] = PipelineSentinelle()

    code = runner.main(["--gate", GUIDE, "--cases-dir", str(cases), "--data-dir", str(data)])

    assert code == 2, "un refus d'avant appel est un code 2, comme les autres"
    erreur = capsys.readouterr().err
    assert "espace de publication" in erreur and "rien n'a été mesuré" in erreur
    assert appels == []
    assert not (tmp_path / "eval-results.json").exists()
    assert not (tmp_path / "eval-results.md").exists()


# --- Revue du tour N1–N3 : la couverture du lot de gate, et le repère partagé de la décision ------

def test_une_disposition_sans_le_lien_des_campagnes_refuse_avant_toute_mesure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """Revue du tour N1–N3, constat 5 : `docs/evals/campagnes` est une cible du lot comme les autres.

    Le préflight écartait l'archive de campagne au motif qu'« l'inclure nommément exigerait un
    horodatage qui n'existe pas encore ». Le motif vaut pour le **fichier**, jamais pour le
    **répertoire** qui porte la couverture. Une disposition où ce seul lien manque laissait donc la
    campagne entière être payée avant que la publication ne refuse — précisément le mode de
    défaillance que N3 ferme pour toutes les autres cibles.
    """
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    cases = _cases_dir(tmp_path, guide=CAS_GUIDE, sinistre=CAS_SINISTRE)
    # Toutes les cibles du lot **sauf** le répertoire d'archives.
    espace = EspacePublie(tmp_path, data)
    espace.installer([cible for cible in CIBLES_STANDARD
                      if cible != Path("docs") / "evals" / "campagnes"], migrer=True)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(runner, "Settings", lambda: _settings())
    appels: list[str] = []

    class PipelineSentinelle:
        async def __call__(self, *a: Any, **k: Any) -> Any:
            appels.append("appel")
            raise AssertionError("aucune mesure ne doit avoir lieu sous une disposition incomplète")

    _COURANT["guide"] = PipelineSentinelle()
    _COURANT["sinistre"] = PipelineSentinelle()

    code = runner.main(["--gate", GUIDE, "--cases-dir", str(cases), "--data-dir", str(data)])

    assert code == 2, "un refus d'avant appel est un code 2, comme les autres"
    assert "espace de publication" in capsys.readouterr().err
    assert appels == [], "la campagne a été payée avant que la couverture ne soit vérifiée"


def test_les_arguments_de_la_ci_passent_le_preflight_de_racine(tmp_path: Path) -> None:
    """Le refus neuf du runner ne referme pas le chemin que l'Always du contrat protège.

    Revue du tour N1–N3, constat 19. Le préflight de racine s'arme sur **tout** run hors
    `--dry-run`, gate ou non : il fallait établir qu'un `--profile full` **sans** `--gate`, celui que
    la CI lance, continue de tourner à l'identique. La CI pose ses liens `.evals/` avant l'étape
    d'évals (`.github/workflows/ci.yml`), et son lot hors gate est le seul couple
    `(rapport JSON, table Markdown)` : c'est cette forme-là que la sonde éprouve, sans aucun appel.
    """
    from server.ingest.artifacts import exiger_espace_installe

    sorties = tmp_path / ".evals"
    sorties.mkdir()
    espace = EspacePublie(tmp_path, tmp_path / "data")
    espace.installer([Path(".evals") / "results.json", Path(".evals") / "results.md"])

    lot = runner.cibles_publiees_du_run(tmp_path / "data", sorties / "results.json",
                                        sorties / "results.md", gate=False)
    assert lot == [sorties / "results.json", sorties / "results.md"], (
        "le lot d'un run sans gate est le seul couple rapport/table")
    exiger_espace_installe(lot)  # ne lève pas : la disposition de la CI suffit au diagnostic


# --- Patch croisé 1/3 : la fraîcheur du repère vaut pour tout run, et le regate sous racine -------

def test_un_run_sans_gate_verifie_aussi_la_fraicheur_de_son_repere(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """`N1-RUN-FRESHNESS` : la péremption est une propriété de la passe, pas du profil.

    `lecture_run.verifier()` vivait sous `if exigences_full:`. Un gate `vertical` et le run CI
    `--profile full` **sans** `--gate` sortaient donc de leur passe de lecture sans vérification
    finale : `Lecture.reel` contrôle avant de *rendre* un chemin, et l'ouverture arrive séparément,
    si bien qu'une reconstruction entre les deux fournissait les nouveaux octets sans rejeu ni
    refus. La sonde prend le chemin **sans gate**, celui que la CI lance.
    """
    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    cases = _cases_dir(tmp_path, guide=CAS_GUIDE, sinistre=CAS_SINISTRE)
    espace = poser_espace(tmp_path, data_dir=data)
    manifest = (data / "manifest.json").read_text("utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-de-test")
    monkeypatch.setattr(runner, "Settings", lambda: _settings())

    appels: list[str] = []

    class PipelineSentinelle:
        async def __call__(self, *a: Any, **k: Any) -> Any:
            appels.append("appel")
            raise AssertionError("aucune mesure ne doit suivre un repère périmé")

    _COURANT["guide"] = PipelineSentinelle()
    _COURANT["sinistre"] = PipelineSentinelle()

    # La génération pincée est reconstruite pendant la construction du contexte : deux bascules.
    vrai_contexte = runner.construire_contexte

    def _reconstruire_puis_rendre(*a: Any, **k: Any) -> Any:
        ctx = vrai_contexte(*a, **k)
        espace.basculer([(data / "manifest.json", manifest)])
        espace.basculer([(data / "manifest.json", manifest)])
        return ctx

    monkeypatch.setattr(runner, "construire_contexte", _reconstruire_puis_rendre)
    code = runner.main(["--suite", "guide", "--cases-dir", str(cases), "--data-dir", str(data)])
    monkeypatch.undo()

    assert code == 2, "un repère périmé se refuse avant tout appel, comme les autres refus"
    assert "rien n'a été mesuré" in capsys.readouterr().err
    assert appels == [], "un appel payant a suivi une génération reconstruite sous la passe"


def test_le_regate_traverse_une_disposition_installee_sans_la_prendre_pour_cassee(
        tmp_path: Path) -> None:
    """Nom historique : le regate lit le custom installé, sans arbre secondaire."""

    data = tmp_path / "data"
    data.mkdir()
    _corpus_sur_disque(data)
    poser_espace(tmp_path, data_dir=data,
                 cibles=[Path("data") / GUIDE / nom for nom in
                         ("document.json", "summary.md", "report.json")])
    from server.app.corpus.racine import lecture_pincee_regate

    with lecture_pincee_regate(data, GUIDE) as capacite:
        corpus = load_corpus(
            data, allow_ungated=True, lecture=capacite.lecture,
            regate=GUIDE, capacite_regate=capacite, neutraliser_regate=True)
        assert GUIDE in corpus.served
        assert capacite.lecture.reel(data / GUIDE / "document.json").is_file()
    assert data.is_dir() and (data / GUIDE).is_dir()
