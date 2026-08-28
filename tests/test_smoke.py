"""La matrice des smoke tests d'AD-12, exercée hors ligne sur des charges utiles enregistrées.

`scripts/smoke.py` est la seule chose du dépôt qui décide si un déploiement est promu. Ses règles
tiennent en trois fonctions **pures** — elles reçoivent un corps déjà décodé et rendent la liste des
écarts —, et c'est ce qui les rend vérifiables ici : chaque ligne de la matrice de la story 1.11 a son
cas, sans réseau, sans révision Cloud Run, sans dépense.

Les corps de référence sont ceux des contrats d'AD-11 (`SanteResponse`, `ChatResponse`,
`SinistreResponse`), réduits aux champs que le smoke lit. Ils sont **amarrés** aux schémas du serveur
par `test_les_corps_de_reference_sont_ceux_des_schemas` : un champ renommé dans `schemas.py` ferait
rougir ce fichier plutôt que de laisser le smoke lire une clé qui n'existe plus.
"""

from __future__ import annotations

import json
import re
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from scripts.smoke import (
    SURFACES,
    Attendus,
    CasTemoin,
    ErreurTransport,
    charger_attendus,
    corps_chat,
    corps_sinistre,
    main,
    verifier_chat,
    verifier_sante,
    verifier_sinistre,
    verifier_surfaces,
)
from server.app.config import Settings

REPO = Path(__file__).resolve().parents[1]

DOCUMENTS = ("axa-lu-optihome-2017", "lux-guide")
PROFIL = "vertical"
CASES = 2  # un cas témoin par document servi, sommés comme `etat.py::gate_cases` les somme
VERSION = "1a2b3c4"
HASH_GUIDE = "abe65f06a4789b61b1e19bb53f8546a8464a697ae24d0dcebc026f40fd49e3a5"
HASH_AXA = "6824f9d2bbcb573b0b7c3816ea8a6e5f035b199bd885cf5b777e0978faa4af2c"

CAS_GUIDE = CasTemoin(id="g-luxtrust-prix", doc_id="lux-guide", question="…", lang="fr",
                      found_attendu=True)
CAS_SINISTRE = CasTemoin(id="s-bougie-canape", doc_id="axa-lu-optihome-2017", question="…",
                         lang="fr", faits={"description": "…"}, found_attendu=True,
                         verdicts_admissibles=("sous_conditions", "ne_tranche_pas"),
                         decision_claim_attendue=True)


# La charge utile de référence publie les seuils **du serveur**, lus sur la même autorité que
# `/api/v1/sante` (`Settings.thresholds()`). Deux nombres écrits à la main ici auraient verrouillé
# une patience que le programme n'utilise jamais en production : la suite restait verte pendant que
# le smoke attendait autre chose que ce que le service annonce (revue 1.11).
SEUILS_DU_SERVEUR = {nom: Settings(_env_file=None).thresholds()[nom]
                     for nom in ("deadline_s", "client_abort_margin_s")}
PATIENCE_PIPELINE = SEUILS_DU_SERVEUR["deadline_s"] + SEUILS_DU_SERVEUR["client_abort_margin_s"]


def sante_nominale() -> dict[str, Any]:
    return {
        "ok": True,
        "version": VERSION,
        "documents_servis": ["axa-lu-optihome-2017", "lux-guide"],
        "gate_profile": "vertical",
        "gate_cases": 2,
        "gate_countersigned": False,
        # État **nominal d'un déploiement** (story 2.1) : le dépôt porte un dictionnaire validé, le
        # service publie le même fait, et aucune alerte ne pèse. Le cas où la validation est encore
        # due — celui du dépôt aujourd'hui — a ses propres tests ci-dessous, avec l'attendu explicite
        # `dictionnaire_validated=False` : c'est un **accord** entre le dépôt et le service, et le
        # smoke le laisse passer sans faire de `sante_nominale()` un corps à deux lectures.
        "dictionary": {"validated": True, "corpus_ok": True, "refus_zero_hit_actif": True},
        "alerts": [],
        "thresholds": SEUILS_DU_SERVEUR,
    }


def chat_nominal() -> dict[str, Any]:
    return {
        "texte": "Le chemin le moins coûteux passe par une banque luxembourgeoise.",
        "sources": [{"block_id": "lux-guide:fluxtrust:5", "status": "verifiee"}],
        "fiches": ["lux-guide:fluxtrust"],
        "unknown": [],
        "comparateur": False,
        "via": "api/v1",
        "answer": {
            "found": True,
            "complete": True,
            "claims": [{"id": "c1", "status": {"retrouvee": True, "pertinente": True,
                                               "edition": "git:a8e8593"}}],
            "rejected_claims": [],
            "verdict": None,
        },
        "trace": {"request_id": "r-1", "pipeline": "guide",
                  "source_hash": {"lux-guide": HASH_GUIDE}, "total_cost_eur": 0.03},
    }


def verdict_nominal(value: str = "ne_tranche_pas") -> dict[str, Any]:
    """Le `Verdict` d'AD-6 tel que la route l'a **réellement** rendu, le 24/08/2026, sur le cas bougie.

    C'est un **objet**, pas une chaîne : `answer.verdict.value` porte la valeur que le cas témoin
    borne, et le reste porte la raison, le paquet manquant et les questions à poser. La première
    version du smoke comparait l'objet entier à la liste des valeurs admissibles — elle aurait rendu
    tout déploiement rouge en accusant le système, et c'est le premier passage live qui l'a montré.
    """
    return {
        "value": value,
        "reason": "Aucune règle de la table ne tranche sur les clauses retrouvées "
                  "(au regard des conditions générales seules)",
        "missing": {"conditions_particulieres": True, "options_souscrites": True,
                    "avenants": True, "date_effet": True,
                    "faits": ["caractère soudain de l'événement", "action subite de la chaleur"]},
        "ask_client": ["Quelles options et extensions ont été souscrites ?"],
        "escalate": [],
    }


def sinistre_nominal() -> dict[str, Any]:
    return {
        "via": "api/v1",
        "sources": [{"block_id": "axa-lu-optihome-2017:p34:12", "kind": "garantie",
                     "kind_confirmed": True, "status": "verifiee"}],
        "answer": {
            "found": True,
            "complete": False,
            "verdict": verdict_nominal(),
            "claims": [{"claim_id": "c1", "quotes": [
                {"block_id": "axa-lu-optihome-2017:p34:12"}],
                "status": {"retrouvee": True, "pertinente": True,
                           "applicable": "humain", "edition": "juin 2017"}}],
            "rejected_claims": [],
        },
        "trace": {"request_id": "r-2", "pipeline": "sinistre",
                  "source_hash": {"axa-lu-optihome-2017": HASH_AXA}, "total_cost_eur": 0.05},
    }


def sante(**remplacements: Any) -> dict[str, Any]:
    corps = sante_nominale()
    corps.update(remplacements)
    return corps


def ecarts_sante(corps: Any, *, dictionnaire_validated: bool = True,
                 dictionnaire_corpus_ok: bool = True) -> list[str]:
    return verifier_sante(corps, documents=DOCUMENTS, gate_profile=PROFIL, gate_cases=CASES,
                          version=VERSION, dictionnaire_validated=dictionnaire_validated,
                          dictionnaire_corpus_ok=dictionnaire_corpus_ok)


def ecarts_chat(corps: Any) -> list[str]:
    return verifier_chat(corps, cas=CAS_GUIDE, source_hash=HASH_GUIDE)


def ecarts_sinistre(corps: Any) -> list[str]:
    return verifier_sinistre(corps, cas=CAS_SINISTRE, source_hash=HASH_AXA)


# --- `sante` -------------------------------------------------------------------------------------

def test_la_sante_nominale_ne_produit_aucun_ecart() -> None:
    assert ecarts_sante(sante_nominale()) == []


def test_un_document_manquant_est_nomme() -> None:
    ecarts = ecarts_sante(sante(documents_servis=["lux-guide"]))
    assert len(ecarts) == 1
    assert "axa-lu-optihome-2017" in ecarts[0]


def test_un_profil_de_gate_inattendu_est_un_ecart() -> None:
    ecarts = ecarts_sante(sante(gate_profile="full"))
    assert len(ecarts) == 1 and "'full'" in ecarts[0] and "'vertical'" in ecarts[0]


def test_un_gate_reecrit_sur_moins_de_cas_est_un_ecart() -> None:
    """Le nom du profil survit à un gate qui a perdu la moitié de sa substance ; le compte, non."""
    ecarts = ecarts_sante(sante(gate_cases=1))
    assert len(ecarts) == 1 and "gate_cases=1" in ecarts[0] and "attendu 2" in ecarts[0]


def test_un_gate_cases_absent_est_un_ecart() -> None:
    corps = sante_nominale()
    del corps["gate_cases"]
    ecarts = ecarts_sante(corps)
    assert len(ecarts) == 1 and "gate_cases absent" in ecarts[0]


def test_la_contresignature_nest_pas_exigee_par_le_smoke() -> None:
    """Le dépôt sait qu'elle est fausse (elle est due à Lancelot) : l'exiger refuserait tout
    déploiement, et un smoke qui refuse tout ne vérifie plus rien."""
    assert ecarts_sante(sante(gate_countersigned=False)) == []


def test_un_gate_absent_publie_null_et_reste_un_ecart() -> None:
    """AD-11 : `gate_profile: null` dès qu'un document servi n'a pas de gate. Jamais promu."""
    ecarts = ecarts_sante(sante(gate_profile=None))
    assert len(ecarts) == 1 and "None" in ecarts[0]


def test_une_version_differente_du_sha7_est_un_ecart() -> None:
    """AD-11 : sans ce contrôle, le smoke mesurerait une révision qu'il n'a pas construite."""
    ecarts = ecarts_sante(sante(version="deadbee"))
    assert len(ecarts) == 1 and "deadbee" in ecarts[0] and VERSION in ecarts[0]


def test_la_derogation_armee_sur_le_service_rougit_le_smoke() -> None:
    """La reprise différée de 1.10 : `ALLOW_UNGATED=true` posé sur la configuration du service.

    Aucun test hors ligne ne voyait cette surface — le garde-fou de 1.10 ne lisait que le texte du
    `Dockerfile`. Ici, l'alerte qu'`etat.py::_alerte_ungated` publie interdit la promotion.
    """
    corps = sante(alerts=[{"doc_id": "*", "alerte": "ungated_refuse_en_production",
                           "detail": "ALLOW_UNGATED=true posé avec ENV=prod"}])
    ecarts = ecarts_sante(corps)
    assert len(ecarts) == 1
    assert "ungated_refuse_en_production" in ecarts[0] and "*" in ecarts[0]


@pytest.mark.parametrize("alerte", ["sans_gate", "gate_perime", "source_absente", "quarantaine",
                                    "dictionnaire_corpus_perime"])
def test_toute_alerte_interdit_la_promotion(alerte: str) -> None:
    """Une alerte est un écart (AD-16) — `dictionnaire_corpus_perime` comprise, en toutes circonstances.

    Un dictionnaire dont les empreintes ne sont pas celles du corpus servi décrit un **autre** corpus :
    c'est un défaut de l'image, pas une signature en attente, et aucune tolérance ne le couvre.
    """
    ecarts = ecarts_sante(sante(alerts=[{"doc_id": "lux-guide", "alerte": alerte, "detail": ""}]))
    assert len(ecarts) == 1 and alerte in ecarts[0]


# --- `sante` : l'état du dictionnaire est celui du dépôt (AD-5, story 2.1) -----------------------

def sante_non_validee(**remplacements: Any) -> dict[str, Any]:
    """Le corps que sert un déploiement dont le `data/dictionary.json` committé n'est pas signé."""
    return sante(dictionary={"validated": False, "corpus_ok": True, "refus_zero_hit_actif": False},
                 alerts=[{"doc_id": "*", "alerte": "dictionnaire_non_valide",
                          "detail": "le refus « zéro hit » est désactivé"}],
                 **remplacements)


def test_un_dictionnaire_non_signe_dans_le_depot_ne_bloque_pas_le_deploiement() -> None:
    """AD-5 oblige `/sante` à porter l'alerte tant que la validation est due (`epics.md`, input humain).

    La refuser interdirait tout déploiement jusqu'à une signature que rien, dans la chaîne, ne réclame
    avant la mise en ligne — exactement la raison pour laquelle `gate_countersigned` n'est pas exigé
    non plus. Ce qui est vérifié n'est pas « pas d'alerte » mais l'**accord** entre le dépôt et le
    service.
    """
    assert ecarts_sante(sante_non_validee(), dictionnaire_validated=False) == []


def test_un_service_qui_tait_lalerte_alors_que_le_depot_nest_pas_signe_est_un_ecart() -> None:
    """La tolérance a deux bords : son absence dit que l'image ne sert pas le `data/` de ce commit."""
    corps = sante(dictionary={"validated": False, "corpus_ok": True, "refus_zero_hit_actif": False},
                  alerts=[])
    ecarts = ecarts_sante(corps, dictionnaire_validated=False)
    assert len(ecarts) == 1 and "dictionnaire_non_valide" in ecarts[0]


def test_lalerte_nest_plus_toleree_le_jour_ou_le_depot_porte_un_dictionnaire_signe() -> None:
    """La tolérance s'éteint d'elle-même : elle est dérivée du dépôt, jamais écrite en dur."""
    ecarts = ecarts_sante(sante_non_validee(), dictionnaire_validated=True)
    noms = " ".join(ecarts)
    assert len(ecarts) == 2 and "dictionnaire_non_valide" in noms and "divergé" in noms


def test_un_dictionnaire_signe_sur_le_service_et_pas_dans_le_depot_est_un_ecart() -> None:
    """Le sens inverse : la révision sert un `data/` que ce commit ne contient pas."""
    ecarts = ecarts_sante(sante(), dictionnaire_validated=False)
    noms = " ".join(ecarts)
    assert "dictionary.validated" in noms and "divergé" in noms


@pytest.mark.parametrize("valeur", ["true", 1, None, {}])
def test_un_validated_non_booleen_est_un_ecart(valeur: Any) -> None:
    """`bool("false")` vaut `True` : seul un booléen JSON strict est lu (même règle que le serveur)."""
    corps = sante(dictionary={"validated": valeur, "corpus_ok": True, "refus_zero_hit_actif": False},
                  alerts=[{"doc_id": "*", "alerte": "dictionnaire_non_valide", "detail": ""}])
    ecarts = ecarts_sante(corps, dictionnaire_validated=False)
    assert any("dictionary.validated" in e for e in ecarts)


# --- l'attendu du dépôt : la règle du serveur, pas une approximation --------

DICO_SOURCE_HASH = "sha-source-du-guide"


def _dico(**over: Any) -> str:
    base: dict[str, Any] = {"schema_version": "1",
                            "corpus_source_hashes": {"lux-guide": DICO_SOURCE_HASH},
                            "corpus": {"matricule": ["numéro national"]}, "intents": {},
                            "candidate_questions": {}, "validated": False,
                            "validated_by": None, "validated_at": None}
    return json.dumps(base | over, ensure_ascii=False)


def _signe() -> dict[str, Any]:
    return {"validated": True, "validated_by": "Lancelot Oudin",
            "validated_at": "2026-08-25T10:00:00Z"}


def _ecrire_depot(tmp_path: Any, contenu: str | None, *, source_hash: str = DICO_SOURCE_HASH,
                  autres: dict[str, str] | None = None) -> Any:
    """Un dépôt minimal : `data/dictionary.json` (optionnel) et le `data/manifest.json` qui va avec.

    `autres` ajoute des documents **servis** au manifest : c'est ce qui rend visible le trou de la
    revue Codex 2.1 (B3) — un dictionnaire qui décrit l'un d'eux, et pas le guide.
    """
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    if contenu is not None:
        (data / "dictionary.json").write_text(contenu, encoding="utf-8")
    entrees = {"lux-guide": {"status": "servi", "source_hash": source_hash}}
    for doc_id, empreinte in (autres or {}).items():
        entrees[doc_id] = {"status": "servi", "source_hash": empreinte}
    (data / "manifest.json").write_text(json.dumps(entrees), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("contenu, attendu", [
    (None, False),                                          # absent
    ("{ pas du json", False),                               # illisible
    (_dico(validated="true"), False),                       # chaîne, pas booléen (StrictBool)
    (_dico(validated=False), False),
    (_dico(**_signe()), True),
    # **Ce que l'ancienne version figeait** : `{"validated": true}` seul n'est pas un dictionnaire.
    # Il n'a ni `schema_version`, ni signataire, ni date — `DictionaryFile` le refuse, donc le
    # serveur le refuse, donc l'attendu du dépôt doit le refuser aussi. Le tenir pour `True` faisait
    # arrêter le déploiement avec le diagnostic **faux** « l'image et le dépôt ont divergé ».
    ('{"validated": true}', False),
    # Un « validé par personne » : la contradiction que le schéma nomme.
    (_dico(validated=True), False),
    # Un champ de plus qu'AD-5 n'énumère pas.
    (_dico(gate="oui", **_signe()), False),
    # Une date que rien ne sait relire.
    (_dico(validated=True, validated_by="Nom", validated_at="hier"), False),
])
def test_lattendu_du_dictionnaire_vient_du_depot(tmp_path: Any, contenu: str | None,
                                                 attendu: bool) -> None:
    """`charger_attendus` lit `data/dictionary.json` **par la règle du serveur** (`DictionaryFile`)."""
    from scripts.smoke import _dictionnaire_validated

    racine = _ecrire_depot(tmp_path, contenu)
    assert _dictionnaire_validated(racine) is attendu


def test_le_depot_et_le_serveur_lisent_le_dictionnaire_de_la_meme_facon(tmp_path: Any) -> None:
    """Sur le modèle de `test_les_deux_commandes_qui_exigent_la_cle_appliquent_la_meme_regle`.

    Le smoke compare deux lectures du **même** fait : celle du dépôt et celle publiée par le service.
    Si elles ne suivent pas la même règle, tout désaccord qu'il rapporte peut n'être qu'un désaccord
    entre ses deux lecteurs — et son diagnostic (« l'image et le dépôt ont divergé ») devient faux.
    """
    from server.app.corpus.dictionary import load_dictionary
    from server.app.corpus.loader import Corpus
    from server.app.domain.ingest import ManifestEntry
    from scripts.smoke import _dictionnaire_validated

    corpus = Corpus(documents={"lux-guide": None},  # type: ignore[dict-item]
                    manifest={"lux-guide": ManifestEntry(
                        status="servi", source_hash=DICO_SOURCE_HASH, ingest_fingerprint="fp",
                        document_hash="d", edition="git:test")})
    contenus = [None, "{ pas du json", '{"validated": true}', _dico(), _dico(**_signe()),
                _dico(validated="true"), _dico(validated=True),
                _dico(gate="oui", **_signe()),
                _dico(validated=True, validated_by="Nom", validated_at="hier")]
    for i, contenu in enumerate(contenus):
        racine = _ecrire_depot(tmp_path / f"c{i}", contenu)
        cote_serveur = load_dictionary(racine / "data", corpus, "lux-guide").validated
        assert _dictionnaire_validated(racine) is cote_serveur, contenu


def test_des_octets_non_utf8_sont_un_attendu_faux_et_non_un_traceback(tmp_path: Any) -> None:
    """`read_text("utf-8")` levait `UnicodeDecodeError`, non attrapée : le smoke sortait en traceback
    au lieu de rapporter un écart, sur un dépôt dont le fichier est simplement illisible."""
    from scripts.smoke import _dictionnaire_validated

    racine = _ecrire_depot(tmp_path, None)
    (racine / "data" / "dictionary.json").write_bytes(b"\xff\xfe\x00binaire")
    assert _dictionnaire_validated(racine) is False


@pytest.mark.parametrize("contenu, source_hash, attendu", [
    (_dico(), DICO_SOURCE_HASH, True),
    (None, DICO_SOURCE_HASH, False),                        # aucun dictionnaire dans le dépôt
    (_dico(), "empreinte-dun-autre-corpus", False),         # le corpus a bougé sous le dictionnaire
    (_dico(corpus_source_hashes={}), DICO_SOURCE_HASH, False),
    (_dico(corpus_source_hashes={"inconnu": DICO_SOURCE_HASH}), DICO_SOURCE_HASH, False),
    # Revue Codex 2.1 (B3) : un dictionnaire qui ne décrit qu'un **autre document servi** — l'AXA,
    # empreinte juste, statut `servi`. Il passait ici comme côté serveur, et le smoke aurait promu
    # une image où les variantes du guide viennent d'un contrat d'assurance.
    (_dico(corpus_source_hashes={"axa-lu-optihome-2017": "sha-du-contrat"}), DICO_SOURCE_HASH, False),
])
def test_lattendu_de_corpus_ok_vient_du_depot(tmp_path: Any, contenu: str | None,
                                              source_hash: str, attendu: bool) -> None:
    """Même règle que `corpus/dictionary._corpus_ok`, appliquée au manifest de ce commit."""
    import json as _json

    from scripts.smoke import _dictionnaire_corpus_ok

    racine = _ecrire_depot(tmp_path, contenu, source_hash=source_hash,
                           autres={"axa-lu-optihome-2017": "sha-du-contrat"})
    manifest = _json.loads((racine / "data" / "manifest.json").read_text("utf-8"))
    assert _dictionnaire_corpus_ok(racine, manifest, "lux-guide") is attendu


def test_une_image_sans_dictionnaire_ne_passe_pas_pour_un_depot_qui_en_porte_un() -> None:
    """**Le trou que `corpus_ok` ferme.** Une image construite sans `data/dictionary.json` publie
    `validated: false` et l'alerte `dictionnaire_non_valide` — exactement ce que le dépôt attend
    tant que la signature est due. Elle aurait donc été promue en silence, l'élargissement d'AD-5
    éteint sur toute la démonstration, sans une ligne pour le dire."""
    corps = sante(dictionary={"validated": False, "corpus_ok": False, "refus_zero_hit_actif": False},
                  alerts=[{"doc_id": "*", "alerte": "dictionnaire_non_valide", "detail": ""}])
    # Sans l'attendu `corpus_ok`, ce corps est indiscernable du cas nominal « signature due ».
    assert ecarts_sante(corps, dictionnaire_validated=False, dictionnaire_corpus_ok=False) == []
    ecarts = ecarts_sante(corps, dictionnaire_validated=False, dictionnaire_corpus_ok=True)
    assert len(ecarts) == 1 and "dictionary.corpus_ok" in ecarts[0]


@pytest.mark.parametrize("valeur", ["true", 1, None, {}])
def test_un_corpus_ok_non_booleen_est_un_ecart(valeur: Any) -> None:
    corps = sante(dictionary={"validated": True, "corpus_ok": valeur, "refus_zero_hit_actif": True})
    assert any("dictionary.corpus_ok" in e for e in ecarts_sante(corps))


def test_un_corpus_ok_absent_est_nomme() -> None:
    corps = sante(dictionary={"validated": True, "refus_zero_hit_actif": True})
    assert any("dictionary.corpus_ok" in e for e in ecarts_sante(corps))


def test_ok_faux_est_un_ecart() -> None:
    assert len(ecarts_sante(sante(ok=False))) == 1


def test_un_corps_ampute_ne_passe_pas_par_defaut() -> None:
    """Lecture stricte : une clé absente ne vaut ni `None`, ni `False`, ni `[]`."""
    ecarts = ecarts_sante({})
    # ok, documents_servis, gate_profile, gate_cases, version, thresholds,
    # dictionary.validated, dictionary.corpus_ok, alerts
    assert len(ecarts) == 9
    assert sum("absent de la réponse" in e for e in ecarts) == 8
    assert any("thresholds absent ou vide" in e for e in ecarts)


def test_un_corps_qui_nest_pas_un_objet_est_un_ecart() -> None:
    assert len(ecarts_sante("indisponible")) == 9


def test_des_seuils_absents_sont_un_ecart_et_non_un_repli_silencieux() -> None:
    """La patience du smoke se lit sur `/sante` : la taire, c'est couper avant le serveur.

    `SanteResponse.thresholds` a un `default_factory=dict` : pydantic accepte un corps sans seuils.
    Sans ce contrôle, `_timeout_pipeline` retombait sur `TIMEOUT_SANTE_S` (30 s), c'est-à-dire **sous**
    la deadline du serveur (55 s) : le smoke aurait coupé une requête parfaitement saine et accusé le
    pipeline. Un renommage de `deadline_s` dans `config.py` suffisait à l'armer.
    """
    ecarts = ecarts_sante(sante(thresholds={}))
    assert len(ecarts) == 1 and "thresholds absent ou vide" in ecarts[0]


@pytest.mark.parametrize("seuil", ["deadline_s", "client_abort_margin_s"])
def test_un_seuil_de_patience_manquant_est_nomme(seuil: str) -> None:
    seuils = {k: v for k, v in sante_nominale()["thresholds"].items() if k != seuil}
    ecarts = ecarts_sante(sante(thresholds=seuils))
    assert len(ecarts) == 1 and f"thresholds.{seuil}" in ecarts[0]


def test_un_seuil_de_patience_non_numerique_est_nomme() -> None:
    """`True` est un `int` en Python : le contrôle doit l'exclure explicitement."""
    ecarts = ecarts_sante(sante(thresholds={"deadline_s": True, "client_abort_margin_s": "10"}))
    assert len(ecarts) == 2 and all("thresholds." in e for e in ecarts)


def test_un_document_servi_hors_manifest_est_un_ecart() -> None:
    """Le sens inverse du contrôle : l'image sert un document que ce commit ne connaît pas.

    Un `data/` resté d'une construction antérieure, ou un manifest édité sans réingestion — dans les
    deux cas l'image et le dépôt ont divergé, et le smoke est le seul endroit qui puisse le voir.
    """
    corps = sante(documents_servis=[*DOCUMENTS, "contrat-fantome"])
    ecarts = ecarts_sante(corps)
    assert len(ecarts) == 1 and "contrat-fantome" in ecarts[0] and "divergé" in ecarts[0]


def test_les_ecarts_de_sante_saccumulent() -> None:
    """Le message nomme **chaque** écart : un déploiement rouge se diagnostique en une lecture."""
    ecarts = ecarts_sante(sante(version="deadbee", gate_profile="full", documents_servis=[],
                                alerts=[{"doc_id": "lux-guide", "alerte": "gate_perime"}],
                                thresholds={}))
    assert len(ecarts) == 5


# --- `chat` --------------------------------------------------------------------------------------

def test_le_chat_nominal_ne_produit_aucun_ecart() -> None:
    assert ecarts_chat(chat_nominal()) == []


def test_un_via_faux_est_un_ecart() -> None:
    corps = chat_nominal() | {"via": "local"}
    ecarts = ecarts_chat(corps)
    assert len(ecarts) == 1 and "'local'" in ecarts[0]


def test_un_refus_la_ou_le_cas_attend_une_reponse_est_un_ecart() -> None:
    corps = chat_nominal()
    corps["answer"]["found"] = False
    ecarts = ecarts_chat(corps)
    assert any("answer.found" in e for e in ecarts)


@pytest.mark.parametrize("valeur", ["false", "true", 1, 0, [], ["oui"], None, {}])
def test_un_found_non_booleen_est_un_ecart(valeur: object) -> None:
    """`bool(found)` acceptait tout objet vrai comme un `True` (revue Codex 1.11).

    Le contre-exemple : `answer.found: "false"` — une chaîne non vide, donc vraie — passait le smoke
    d'un cas témoin qui attend `true`, et un service rendant un corps hors contrat était promu. Le
    contrat v1 dit `found: bool` ; le smoke le lit comme tel, et refuse tout le reste, y compris `1`
    et `0` qui valent pourtant `True` et `False` à la comparaison.
    """
    corps = chat_nominal()
    corps["answer"]["found"] = valeur
    ecarts = ecarts_chat(corps)
    assert any("n'est pas un booléen" in e for e in ecarts), ecarts


@pytest.mark.parametrize("valeur", [True, False, "2", 2.0, None, [2]])
def test_un_gate_cases_non_entier_est_un_ecart(valeur: object) -> None:
    """`True == 1` : un `gate_cases: true` valait « un cas » sur un gate qui en attend un.

    Le compte de cas relus est ce que `/` affiche à côté du niveau de validation ; le lire sur un
    booléen, ou sur une chaîne, c'est afficher un chiffre que rien ne tient (revue Codex 1.11).
    """
    ecarts = ecarts_sante(sante(gate_cases=valeur))
    assert any("n'est pas un entier" in e for e in ecarts), ecarts


def test_aucune_claim_retrouvee_est_un_ecart() -> None:
    """AD-3 : une réponse sans citation retrouvée n'est pas une réponse sourcée."""
    corps = chat_nominal()
    corps["answer"]["claims"][0]["status"]["retrouvee"] = False
    ecarts = ecarts_chat(corps)
    assert len(ecarts) == 1 and "aucune claim `retrouvee`" in ecarts[0]


def test_zero_claim_est_un_ecart() -> None:
    corps = chat_nominal()
    corps["answer"]["claims"] = []
    assert len(ecarts_chat(corps)) == 1


def test_un_source_hash_different_est_un_ecart() -> None:
    """La révision sert une autre source que celle du commit : `fetch_source` a ramené autre chose."""
    corps = chat_nominal()
    corps["trace"]["source_hash"]["lux-guide"] = "0" * 64
    ecarts = ecarts_chat(corps)
    assert len(ecarts) == 1 and HASH_GUIDE in ecarts[0]


def test_un_source_hash_absent_pour_le_document_est_un_ecart() -> None:
    corps = chat_nominal()
    corps["trace"]["source_hash"] = {}
    assert len(ecarts_chat(corps)) == 1


def test_un_chat_ampute_produit_un_ecart_par_champ_lu() -> None:
    ecarts = ecarts_chat({})
    assert len(ecarts) == 4 and all("absent de la réponse" in e for e in ecarts)


# --- `sinistre` ----------------------------------------------------------------------------------

def test_le_sinistre_nominal_ne_produit_aucun_ecart() -> None:
    assert ecarts_sinistre(sinistre_nominal()) == []


@pytest.mark.parametrize("verdict", ["sous_conditions", "ne_tranche_pas"])
def test_les_deux_verdicts_admissibles_du_cas_passent(verdict: str) -> None:
    """Le cas témoin borne un intervalle conservateur ; le smoke lit **sa** liste, pas la sienne."""
    corps = sinistre_nominal()
    corps["answer"]["verdict"] = verdict_nominal(verdict)
    assert ecarts_sinistre(corps) == []


@pytest.mark.parametrize("verdict", ["couvert", "non_couvert"])
def test_un_verdict_hors_valeurs_admissibles_est_un_ecart(verdict: str) -> None:
    corps = sinistre_nominal()
    corps["answer"]["verdict"] = verdict_nominal(verdict)
    ecarts = ecarts_sinistre(corps)
    assert len(ecarts) == 1 and "hors des valeurs admissibles" in ecarts[0]


def test_le_verdict_est_lu_dans_lobjet_et_non_compare_entier() -> None:
    """La régression que le premier passage live a révélée, tenue par un test.

    `answer.verdict` est le `Verdict` d'AD-6 — un objet. Comparer l'objet à la liste des valeurs
    admissibles rendait **tout** déploiement rouge, en accusant le système d'un verdict qu'il n'avait
    pas rendu.
    """
    corps = sinistre_nominal()
    assert isinstance(corps["answer"]["verdict"], dict)
    assert ecarts_sinistre(corps) == []


@pytest.mark.parametrize("verdict", [None, "ne_tranche_pas", {}, {"reason": "…"}])
def test_un_verdict_sans_value_est_un_ecart(verdict: Any) -> None:
    """Un verdict `null`, une chaîne nue ou un objet sans `value` : jamais promu par défaut."""
    corps = sinistre_nominal()
    corps["answer"]["verdict"] = verdict
    ecarts = ecarts_sinistre(corps)
    assert len(ecarts) == 1 and "answer.verdict.value" in ecarts[0]


def test_un_verdict_absent_est_un_ecart() -> None:
    corps = sinistre_nominal()
    del corps["answer"]["verdict"]
    ecarts = ecarts_sinistre(corps)
    assert len(ecarts) == 1 and "answer.verdict.value" in ecarts[0]


def test_un_corps_ampute_ne_prouve_jamais_labsence_de_claim_decisionnelle() -> None:
    """4.2a (revue) : sans `answer.claims` ni `sources`, `decisionnelle=False` serait un calcul sur
    un corps amputé — l'écart est nommé avant tout calcul, même quand le cas attend `false`."""
    import dataclasses

    inverse = dataclasses.replace(CAS_SINISTRE, decision_claim_attendue=False)
    corps = sinistre_nominal()
    del corps["answer"]["claims"]
    del corps["sources"]
    ecarts = verifier_sinistre(corps, cas=inverse, source_hash=HASH_AXA)
    assert any("answer.claims" in e for e in ecarts)
    assert any("sources" in e for e in ecarts)


def test_un_corps_mal_forme_ne_prouve_jamais_labsence_de_claim_decisionnelle() -> None:
    """Revue post-4.2a : un champ présent mais qui n'est pas une liste n'est pas plus une preuve
    qu'un champ absent — `decisionnelle=False` passerait par accident quand le cas attend `false`.
    L'écart est nommé avant tout calcul du prédicat."""
    import dataclasses

    inverse = dataclasses.replace(CAS_SINISTRE, decision_claim_attendue=False)
    corps = sinistre_nominal()
    corps["answer"]["claims"] = {"c1": {}}
    corps["sources"] = "axa-lu-optihome-2017:p34:12"
    ecarts = verifier_sinistre(corps, cas=inverse, source_hash=HASH_AXA)
    assert any("answer.claims n'est pas une liste" in e for e in ecarts)
    assert any("sources n'est pas une liste" in e for e in ecarts)
    assert not any("claim décisionnelle confirmée" in e for e in ecarts)


def test_un_cas_sans_verdict_attendu_ne_juge_pas_le_verdict() -> None:
    """Un cas qui ne dit rien du verdict n'en fait pas juger un : `expected.verdict` vide = muet."""
    muet = CasTemoin(id="x", doc_id="axa-lu-optihome-2017", question="…", verdicts_admissibles=())
    corps = sinistre_nominal()
    corps["answer"]["verdict"] = "couvert"
    assert verifier_sinistre(corps, cas=muet, source_hash=HASH_AXA) == []


def test_un_sinistre_sans_claim_retrouvee_est_un_ecart() -> None:
    corps = sinistre_nominal()
    corps["answer"]["claims"] = [{"id": "c1", "status": {"retrouvee": False, "edition": "juin 2017"}}]
    ecarts = ecarts_sinistre(corps)
    assert any("aucune claim `retrouvee`" in ecart for ecart in ecarts)
    assert any("claim décisionnelle confirmée" in ecart for ecart in ecarts)


def test_la_definition_seule_ne_satisfait_jamais_le_predicat_decisionnel() -> None:
    corps = sinistre_nominal()
    corps["sources"][0].update({"kind": "definition", "kind_confirmed": True})
    ecarts = ecarts_sinistre(corps)
    assert len(ecarts) == 1 and "claim décisionnelle confirmée" in ecarts[0]


def test_une_source_non_verifiee_ne_satisfait_pas_le_predicat_decisionnel() -> None:
    corps = sinistre_nominal()
    corps["sources"][0]["status"] = "rejetee"
    ecarts = ecarts_sinistre(corps)
    assert len(ecarts) == 1 and "claim décisionnelle confirmée" in ecarts[0]


def test_une_clause_non_confirmee_ou_sans_applicabilite_est_un_ecart() -> None:
    corps = sinistre_nominal()
    corps["sources"][0]["kind_confirmed"] = False
    assert any("claim décisionnelle confirmée" in ecart for ecart in ecarts_sinistre(corps))
    corps = sinistre_nominal()
    corps["answer"]["claims"][0]["status"]["applicable"] = None
    assert any("claim décisionnelle confirmée" in ecart for ecart in ecarts_sinistre(corps))


# --- les surfaces ---------------------------------------------------------------------------------

def test_les_surfaces_sondees_sont_les_trois_dad12() -> None:
    """L'origine unique d'AD-12 sert trois pages ; le smoke les sonde toutes les trois, pas deux."""
    assert SURFACES == ("/", "/guide/", "/sinistre/")


def test_trois_surfaces_a_200_ne_produisent_aucun_ecart() -> None:
    assert verifier_surfaces(dict.fromkeys(SURFACES, 200)) == []


@pytest.mark.parametrize("code", [301, 404, 500, 502])
def test_une_surface_qui_ne_repond_pas_200_est_un_ecart_nomme(code: int) -> None:
    """Un `COPY web` disparu du Dockerfile : l'API reste parfaite et la page est morte."""
    statuts = dict.fromkeys(SURFACES, 200) | {"/guide/": code}
    ecarts = verifier_surfaces(statuts)
    assert len(ecarts) == 1
    assert "/guide/" in ecarts[0] and str(code) in ecarts[0]


def test_une_surface_non_sondee_est_un_ecart_et_non_un_silence() -> None:
    """Une entrée absente du relevé ne doit pas se lire comme un succès."""
    statuts = dict.fromkeys(SURFACES, 200)
    del statuts["/sinistre/"]
    ecarts = verifier_surfaces(statuts)
    assert len(ecarts) == 1 and "non sondée" in ecarts[0]


def test_les_ecarts_de_surface_saccumulent() -> None:
    assert len(verifier_surfaces({"/": 404, "/guide/": 500, "/sinistre/": 200})) == 2


def test_sonder_rend_le_code_dun_404_au_lieu_de_lever(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un 404 est une information — c'est `verifier_surfaces` qui le nomme, pas le transport."""
    import scripts.smoke as smoke

    def refuser(requete: Any, timeout: float = 0) -> Any:
        raise urllib.error.HTTPError(requete.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(smoke.urllib.request, "urlopen", refuser)
    assert smoke.sonder("https://c/guide/", timeout=1.0) == 404


def test_sonder_leve_quand_le_service_est_injoignable(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.smoke as smoke

    def refuser(requete: Any, timeout: float = 0) -> Any:
        raise urllib.error.URLError("connexion refusée")

    monkeypatch.setattr(smoke.urllib.request, "urlopen", refuser)
    with pytest.raises(ErreurTransport):
        smoke.sonder("https://c/", timeout=1.0)


def test_la_sonde_de_sante_reessaie_puis_renonce(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--min-instances=0` : un démarrage à froid a droit à une seconde chance, bornée."""
    import scripts.smoke as smoke

    essais: list[int] = []

    def refuser(url: str, **_: Any) -> Any:
        essais.append(1)
        raise ErreurTransport(f"{url} injoignable : démarrage")

    monkeypatch.setattr(smoke, "appeler", refuser)
    monkeypatch.setattr(smoke, "_patienter", lambda _s: None)
    with pytest.raises(ErreurTransport):
        smoke.appeler_avec_reprise("https://c/api/v1/sante", timeout=1.0)
    assert len(essais) == smoke.ESSAIS_SANTE


def test_la_sonde_de_sante_rend_la_reponse_du_second_essai(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.smoke as smoke

    appels: list[int] = []

    def repondre(url: str, **_: Any) -> Any:
        appels.append(1)
        if len(appels) == 1:
            raise ErreurTransport("démarrage à froid")
        return sante_nominale()

    monkeypatch.setattr(smoke, "appeler", repondre)
    monkeypatch.setattr(smoke, "_patienter", lambda _s: None)
    assert smoke.appeler_avec_reprise("https://c/api/v1/sante", timeout=1.0)["ok"] is True


def test_les_appels_de_pipeline_nont_droit_a_aucune_reprise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Réessayer un appel modèle qui a échoué, c'est payer deux fois pour cacher un symptôme.

    Le contrôle porte sur le **comportement** de `main`, pas sur la mise en page du fichier : une
    version antérieure découpait le source à `def main(` et comptait des sous-chaînes, ce qu'un
    renommage cassait sans qu'aucun comportement n'ait changé (revue 1.11).
    """
    import scripts.smoke as smoke

    attendus = Attendus(documents=DOCUMENTS, gate_profile=PROFIL, gate_cases=CASES,
                        dictionnaire_validated=True,
                        dictionnaire_corpus_ok=True,
                        source_hash={"lux-guide": HASH_GUIDE, "axa-lu-optihome-2017": HASH_AXA},
                        cas_guide=CAS_GUIDE, cas_sinistre=CAS_SINISTRE)
    monkeypatch.setattr(smoke, "charger_attendus", lambda **_: attendus)
    appels: list[str] = []

    def repondre(url: str, **_: Any) -> Any:
        appels.append(url)
        if url.endswith("/api/v1/sante"):
            return sante_nominale()
        raise ErreurTransport(f"{url} injoignable : le modèle n'a pas répondu")

    monkeypatch.setattr(smoke, "appeler", repondre)
    monkeypatch.setattr(smoke, "sonder", lambda url, *, timeout: 200)
    monkeypatch.setattr(smoke, "_patienter", lambda _s: None)
    assert main(["--base-url", "https://c", "--version", VERSION]) == 1
    # Un seul appel au chat — puis l'arrêt. Ni reprise, ni passage au sinistre.
    assert appels == ["https://c/api/v1/sante", "https://c/api/v1/chat"]



# --- les attendus viennent du dépôt, jamais du smoke ---------------------------------------------

def test_les_attendus_sont_ceux_du_manifest_et_des_cas_du_gate() -> None:
    """Ce que le smoke exige est lu dans `data/manifest.json` et les deux YAML — rien n'est recopié."""
    attendus = charger_attendus()
    manifest = json.loads((REPO / "data" / "manifest.json").read_text("utf-8"))
    # Même définition que le loader de production : un artefact sans gate est `sans_gate`, donc
    # auditable dans le listing mais absent de `documents_servis`.
    servis = tuple(sorted(
        doc_id for doc_id, entry in manifest.items()
        if entry["status"] == "servi" and isinstance(entry.get("gate"), dict)
    ))
    assert attendus.documents == servis
    assert attendus.gate_profile == manifest[servis[0]]["gate"]["profile"]
    assert attendus.source_hash == {d: manifest[d]["source_hash"] for d in servis}
    assert attendus.cas_guide.id == "g-luxtrust-prix"
    assert attendus.cas_sinistre.id == "s-bougie-canape"
    assert attendus.cas_sinistre.verdicts_admissibles == ("sous_conditions", "ne_tranche_pas")
    assert attendus.cas_sinistre.faits["description"].startswith("Une bougie allumée")


def test_les_attendus_refusent_un_depot_sans_document_servi(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "manifest.json").write_text('{"x": {"status": "quarantaine"}}', "utf-8")
    with pytest.raises(ErreurTransport, match="aucun document"):
        charger_attendus(racine=tmp_path)


def test_les_corps_de_reference_sont_ceux_des_schemas() -> None:
    """Un champ renommé dans `schemas.py` doit rougir **ici**, pas en production.

    Le smoke lit des clés par leur nom sur un service distant : rien, à l'exécution, ne le relierait
    au contrat. Ce test est ce lien — il asserte que chaque clé lue existe bien dans le modèle qui la
    publie, et que les corps de référence ci-dessus valident contre ces modèles.
    """
    from server.app.api.schemas import ChatResponse, SanteResponse, SinistreResponse
    from server.app.domain.answer import Answer, ClaimStatus
    from server.app.domain.trace import Trace
    from server.app.domain.verdict import Verdict

    for champ in ("ok", "version", "documents_servis", "gate_profile", "alerts"):
        assert champ in SanteResponse.model_fields
    for champ in ("via", "answer", "trace"):
        assert champ in ChatResponse.model_fields
        assert champ in SinistreResponse.model_fields
    for champ in ("found", "claims", "verdict"):
        assert champ in Answer.model_fields
    assert "source_hash" in Trace.model_fields

    # Les objets de référence sont **validés** contre les modèles qui les publient, et pas seulement
    # comparés champ à champ : c'est ce qui aurait attrapé, hors ligne, le verdict lu comme une chaîne
    # là où AD-6 rend un objet.
    SanteResponse.model_validate(sante_nominale())
    Verdict.model_validate(sinistre_nominal()["answer"]["verdict"])
    for corps in (chat_nominal(), sinistre_nominal()):
        ClaimStatus.model_validate(corps["answer"]["claims"][0]["status"])
    assert Answer.model_fields["verdict"].annotation is not str


def test_les_corps_de_requete_sont_ceux_des_schemas_de_requete() -> None:
    """Le pendant du test ci-dessus, côté **envoi** — et c'est le côté qui casse en silence.

    `ChatRequest` est `extra="ignore"` : un champ renommé dans `schemas.py` ferait poster au smoke un
    corps que la route accepte en retombant sur un profil vide et un historique vide. Le smoke
    resterait **vert** en rejouant un cas témoin amputé de ce qui le définit. `SinistreRequest`, lui,
    est `extra="forbid"` : la même dérive y donne un 400, donc un déploiement rouge après un build
    facturé. Les deux se voient ici, hors ligne (revue 1.11).
    """
    from server.app.api.schemas import ChatRequest, SinistreRequest

    corps_g = corps_chat(CAS_GUIDE)
    corps_s = corps_sinistre(CAS_SINISTRE)
    assert set(corps_g) <= set(ChatRequest.model_fields), (
        "un champ que `ChatRequest` ignore est un champ que le cas témoin perd en silence")
    assert set(corps_s) <= set(SinistreRequest.model_fields)
    ChatRequest.model_validate(corps_g)
    SinistreRequest.model_validate(corps_s)
    # Et les champs qui **portent** le cas sont bien là : sans eux, le smoke rejouerait une question
    # nue, pas le cas que le gate a mesuré.
    assert {"question", "profil", "historique"} <= set(corps_g)
    assert {"doc_id", "question", "faits"} <= set(corps_s)


# --- la lecture stricte du dépôt -------------------------------------------------------------------

def _ecrire_cas(dossier: Path, contenu: str) -> Path:
    dossier.mkdir(parents=True, exist_ok=True)
    fichier = dossier / "cas.yaml"
    fichier.write_text(contenu, encoding="utf-8")
    return fichier


CAS_VALIDE = """
id: c-1
profile: vertical
question: Une question
lang: fr
expected:
  found: true
  verdict: [sous_conditions, ne_tranche_pas]
"""

CAS_SINISTRE_VALIDE = CAS_VALIDE.replace("expected:", "expected:").rstrip() + """
  decision_claim: true
"""


def test_un_cas_valide_est_lu_avec_ses_champs(tmp_path: Path) -> None:
    import scripts.smoke as smoke

    _ecrire_cas(tmp_path, CAS_VALIDE)
    cas = smoke._lire_cas(tmp_path, "lux-guide")
    assert cas.id == "c-1" and cas.doc_id == "lux-guide" and cas.found_attendu is True
    assert cas.verdicts_admissibles == ("sous_conditions", "ne_tranche_pas")
    assert cas.decision_claim_attendue is None


def test_un_cas_sinistre_lit_son_attente_decisionnelle(tmp_path: Path) -> None:
    import scripts.smoke as smoke

    _ecrire_cas(tmp_path, CAS_SINISTRE_VALIDE)
    cas = smoke._lire_cas(tmp_path, "axa-lu-optihome-2017", suite_sinistre=True)
    assert cas.decision_claim_attendue is True


def test_une_attente_decisionnelle_sur_un_cas_guide_est_un_refus(tmp_path: Path) -> None:
    """Même règle que `server/evals/run.py` : `decision_claim` n'a de sens qu'en suite sinistre —
    l'accepter ailleurs en ferait une attente silencieusement ignorée."""
    import scripts.smoke as smoke

    _ecrire_cas(tmp_path, CAS_SINISTRE_VALIDE)
    with pytest.raises(ErreurTransport, match="suite `sinistre`"):
        smoke._lire_cas(tmp_path, "lux-guide")


def test_un_verdict_scalaire_est_refuse_et_non_lu_caractere_par_caractere(tmp_path: Path) -> None:
    """Le contre-exemple qui a motivé la dureté : `tuple("sous_conditions")` = onze caractères."""
    import scripts.smoke as smoke

    _ecrire_cas(tmp_path, CAS_VALIDE.replace("verdict: [sous_conditions, ne_tranche_pas]",
                                             "verdict: sous_conditions"))
    with pytest.raises(ErreurTransport, match="liste"):
        smoke._lire_cas(tmp_path, "lux-guide")


def test_un_found_absent_est_refuse_plutot_que_devine(tmp_path: Path) -> None:
    import scripts.smoke as smoke

    _ecrire_cas(tmp_path, CAS_VALIDE.replace("  found: true\n", ""))
    with pytest.raises(ErreurTransport, match="found"):
        smoke._lire_cas(tmp_path, "lux-guide")


@pytest.mark.parametrize("nombre", [0, 2])
def test_zero_ou_deux_cas_sont_un_refus(tmp_path: Path, nombre: int) -> None:
    """Le smoke rejoue l'unique cas vertical ; il n'en désigne pas un parmi plusieurs."""
    import scripts.smoke as smoke

    tmp_path.mkdir(parents=True, exist_ok=True)
    for i in range(nombre):
        (tmp_path / f"cas-{i}.yaml").write_text(CAS_VALIDE, encoding="utf-8")
    with pytest.raises(ErreurTransport, match=f"contient {nombre} cas vertical"):
        smoke._lire_cas(tmp_path, "lux-guide")


def test_les_cas_full_ne_masquent_pas_le_témoin_vertical(tmp_path: Path) -> None:
    import scripts.smoke as smoke

    _ecrire_cas(tmp_path, CAS_VALIDE)
    (tmp_path / "full.yaml").write_text(
        CAS_VALIDE.replace("id: c-1", "id: c-full").replace(
            "profile: vertical", "profile: full"), encoding="utf-8")
    assert smoke._lire_cas(tmp_path, "lux-guide").id == "c-1"


@pytest.mark.parametrize("profile", ["inconnu", "3"])
def test_un_profile_inconnu_ou_non_chaine_est_refuse(tmp_path: Path, profile: str) -> None:
    import scripts.smoke as smoke

    valeur = profile if profile == "inconnu" else "3"
    brut = CAS_VALIDE.replace("profile: vertical", f"profile: {valeur}")
    _ecrire_cas(tmp_path, brut)
    with pytest.raises(ErreurTransport, match="profile"):
        smoke._lire_cas(tmp_path, "lux-guide")


def test_un_profile_absent_est_refuse(tmp_path: Path) -> None:
    import scripts.smoke as smoke

    _ecrire_cas(tmp_path, CAS_VALIDE.replace("profile: vertical\n", ""))
    with pytest.raises(ErreurTransport, match="profile"):
        smoke._lire_cas(tmp_path, "lux-guide")


def test_un_dossier_de_cas_absent_est_un_refus(tmp_path: Path) -> None:
    import scripts.smoke as smoke

    with pytest.raises(ErreurTransport, match="absent"):
        smoke._lire_cas(tmp_path / "nulle-part", "lux-guide")


@pytest.mark.parametrize("mutation,motif", [
    ("- pas une table", "table YAML"),
    ("id: 42\nquestion: q\nexpected:\n  found: true\n", "`id`"),
    ("id: c\nquestion: '  '\nexpected:\n  found: true\n", "`question`"),
    ("id: c\nquestion: q\nexpected: 3\n", "`expected`"),
    ("id: c\nquestion: q\nlang: 3\nexpected:\n  found: true\n", "`lang`"),
    ("id: c\nquestion: q\nprofil: 3\nexpected:\n  found: true\n", "`profil`"),
    ("id: c\nquestion: q\nfaits: 3\nexpected:\n  found: true\n", "`faits`"),
    ("id: c\nquestion: q\nhistorique: [3]\nexpected:\n  found: true\n", "`historique`"),
])
def test_chaque_champ_mal_forme_est_un_refus_nomme(tmp_path: Path, mutation: str, motif: str) -> None:
    """La docstring de `_lire_cas` promet des refus **nommés** : chacun est exercé ici."""
    import scripts.smoke as smoke

    contenu = mutation if mutation.startswith("-") else "profile: vertical\n" + mutation
    _ecrire_cas(tmp_path, contenu)
    with pytest.raises(ErreurTransport, match=re.escape(motif)):
        smoke._lire_cas(tmp_path, "lux-guide")


def test_un_yaml_illisible_est_un_refus_et_non_une_exception_nue(tmp_path: Path) -> None:
    import scripts.smoke as smoke

    _ecrire_cas(tmp_path, "id: [non fermé\n")
    with pytest.raises(ErreurTransport, match="illisible"):
        smoke._lire_cas(tmp_path, "lux-guide")


def test_un_gate_cases_illisible_dans_le_manifest_est_un_refus(tmp_path: Path) -> None:
    """Le compte de cas ne se devine pas à 0 ni à 1 : ce serait inventer l'attente qu'on lit."""
    depot = tmp_path / "depot"
    (depot / "data").mkdir(parents=True)
    manifest = json.loads((REPO / "data" / "manifest.json").read_text("utf-8"))
    manifest["lux-guide"]["gate"]["cases"] = None
    (depot / "data" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ErreurTransport, match="gate.cases"):
        charger_attendus(racine=depot)


# --- le programme ---------------------------------------------------------------------------------

def test_main_sort_en_1_quand_le_service_est_injoignable(monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    """Serveur injoignable : jamais de promotion, jamais de repli (AD-16)."""
    import scripts.smoke as smoke

    def refuser(url: str, **_: Any) -> Any:
        raise ErreurTransport(f"{url} injoignable : connexion refusée")

    monkeypatch.setattr(smoke, "appeler", refuser)
    monkeypatch.setattr(smoke, "sonder", refuser)
    monkeypatch.setattr(smoke, "_patienter", lambda _s: None)
    assert main(["--base-url", "https://candidat---exemple.run.app", "--version", VERSION]) == 1
    assert "injoignable" in capsys.readouterr().err


def test_main_sort_en_0_sur_les_trois_corps_nominaux(monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    import scripts.smoke as smoke

    attendus = Attendus(documents=DOCUMENTS, gate_profile=PROFIL, gate_cases=CASES,
                        dictionnaire_validated=True,
                        dictionnaire_corpus_ok=True,
                        source_hash={"lux-guide": HASH_GUIDE, "axa-lu-optihome-2017": HASH_AXA},
                        cas_guide=CAS_GUIDE, cas_sinistre=CAS_SINISTRE)
    monkeypatch.setattr(smoke, "charger_attendus", lambda **_: attendus)
    reponses = {"/api/v1/sante": sante_nominale(), "/api/v1/chat": chat_nominal(),
                "/api/v1/sinistre": sinistre_nominal()}
    appels: list[tuple[str, float]] = []

    def repondre(url: str, *, timeout: float, corps: Any = None) -> Any:
        appels.append((url, timeout))
        return reponses[url.removeprefix("https://c")]

    sondes: list[str] = []

    def sonder(url: str, *, timeout: float) -> int:
        sondes.append(url)
        return 200

    monkeypatch.setattr(smoke, "appeler", repondre)
    monkeypatch.setattr(smoke, "sonder", sonder)
    assert main(["--base-url", "https://c/", "--version", VERSION]) == 0
    sortie = capsys.readouterr().out
    assert sortie.count("ok    ·") == 5  # les quatre vérifications, plus la ligne de conclusion
    assert sondes == [f"https://c{chemin}" for chemin in SURFACES]
    # La patience des deux appels de pipeline vient de `/sante` (55 + 5), jamais d'un nombre écrit ici.
    assert [t for _, t in appels] == [smoke.TIMEOUT_SANTE_S, PATIENCE_PIPELINE, PATIENCE_PIPELINE]


def test_main_sort_en_1_et_nomme_lecart(monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    import scripts.smoke as smoke

    attendus = Attendus(documents=DOCUMENTS, gate_profile=PROFIL, gate_cases=CASES,
                        dictionnaire_validated=True,
                        dictionnaire_corpus_ok=True,
                        source_hash={"lux-guide": HASH_GUIDE, "axa-lu-optihome-2017": HASH_AXA},
                        cas_guide=CAS_GUIDE, cas_sinistre=CAS_SINISTRE)
    monkeypatch.setattr(smoke, "charger_attendus", lambda **_: attendus)
    mauvaise = sante_nominale() | {"version": "deadbee"}
    reponses = {"/api/v1/sante": mauvaise, "/api/v1/chat": chat_nominal(),
                "/api/v1/sinistre": sinistre_nominal()}
    appels: list[str] = []

    def repondre(url: str, **_: Any) -> Any:
        appels.append(url)
        return reponses[url.removeprefix("https://c")]

    monkeypatch.setattr(smoke, "appeler", repondre)
    monkeypatch.setattr(smoke, "sonder", lambda url, *, timeout: 200)
    assert main(["--base-url", "https://c", "--version", VERSION]) == 1
    capture = capsys.readouterr()
    assert "ÉCART · sante" in capture.out and "deadbee" in capture.out
    assert "le trafic ne doit pas être promu" in capture.err
    # AD-16 : le déploiement est déjà refusé — les deux appels facturés ne sont pas joués.
    assert appels == ["https://c/api/v1/sante"]
    assert "avant tout appel facturé" in capture.err


def test_main_ne_paie_aucun_appel_de_pipeline_quand_une_surface_est_morte(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Une page morte suffit à refuser le déploiement : chat et sinistre ne sont pas facturés."""
    import scripts.smoke as smoke

    attendus = Attendus(documents=DOCUMENTS, gate_profile=PROFIL, gate_cases=CASES,
                        dictionnaire_validated=True,
                        dictionnaire_corpus_ok=True,
                        source_hash={"lux-guide": HASH_GUIDE, "axa-lu-optihome-2017": HASH_AXA},
                        cas_guide=CAS_GUIDE, cas_sinistre=CAS_SINISTRE)
    monkeypatch.setattr(smoke, "charger_attendus", lambda **_: attendus)
    appels: list[str] = []

    def repondre(url: str, **_: Any) -> Any:
        appels.append(url)
        return sante_nominale()

    monkeypatch.setattr(smoke, "appeler", repondre)
    monkeypatch.setattr(smoke, "sonder",
                        lambda url, *, timeout: 404 if url.endswith("/sinistre/") else 200)
    assert main(["--base-url", "https://c", "--version", VERSION]) == 1
    capture = capsys.readouterr()
    assert "ÉCART · surfaces" in capture.out and "/sinistre/" in capture.out
    assert appels == ["https://c/api/v1/sante"]
    assert "avant tout appel facturé" in capture.err


def test_un_ecart_de_sinistre_est_rapporte_apres_les_quatre_verifications(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Le chemin qui va jusqu'au bout : quatre résultats rapportés, sortie 1, aucune promotion."""
    import scripts.smoke as smoke

    attendus = Attendus(documents=DOCUMENTS, gate_profile=PROFIL, gate_cases=CASES,
                        dictionnaire_validated=True,
                        dictionnaire_corpus_ok=True,
                        source_hash={"lux-guide": HASH_GUIDE, "axa-lu-optihome-2017": HASH_AXA},
                        cas_guide=CAS_GUIDE, cas_sinistre=CAS_SINISTRE)
    monkeypatch.setattr(smoke, "charger_attendus", lambda **_: attendus)
    mauvais = sinistre_nominal()
    mauvais["answer"]["verdict"] = verdict_nominal("couvert")
    reponses = {"/api/v1/sante": sante_nominale(), "/api/v1/chat": chat_nominal(),
                "/api/v1/sinistre": mauvais}
    monkeypatch.setattr(smoke, "appeler",
                        lambda url, **_: reponses[url.removeprefix("https://c")])
    monkeypatch.setattr(smoke, "sonder", lambda url, *, timeout: 200)
    assert main(["--base-url", "https://c", "--version", VERSION]) == 1
    capture = capsys.readouterr()
    assert capture.out.count("ok    ·") == 3  # sante, surfaces, chat — et pas de ligne de conclusion
    assert "ÉCART · sinistre" in capture.out and "couvert" in capture.out
    assert "le trafic ne doit pas être promu" in capture.err
    assert "avant tout appel facturé" not in capture.err
