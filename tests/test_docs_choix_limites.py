"""Contrat documentaire de la synthèse « Choix et limites », sans réseau."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


DOC = Path(__file__).resolve().parents[1] / "docs" / "choix-et-limites.md"
DETAILS = "\n<details>\n"
ANNEXE_TITLE = "# Choix et limites mesurées\n"
# Bougé le 02/09/2026 : le registre gagne la section « un amendement d'AD-16, écrit plutôt que
# glissé ». L'empreinte est là pour qu'un ajout au registre soit un acte, pas une dérive.
# Bougé le 03/09/2026 : le registre gagne « le dictionnaire d'un contrat par article ». La synthèse
# est bornée à environ deux pages et suit les jalons de la story ; des mesures de campagne
# d'ingestion — 751 → 71 requêtes, majorants par palier — n'y ont pas leur place, et le registre est
# précisément l'endroit où elles se consignent. Rien n'a été retiré ni réécrit : la section du
# 27/08/2026 sur le dictionnaire Baloise reste telle quelle, et la nouvelle dit en quoi ses 35
# unités ne sont plus atteignables.
# Bougé le 03/09/2026 : le registre gagne « un terme s'élargit à un seul groupe », l'amendement de
# la revue Codex 2.1 (I1) après génération du dictionnaire AXA. La décision de I1 avait été mesurée
# sur le dictionnaire du guide, dont les groupes sont des catégories ; sur un dictionnaire de contrat
# généré, dont certains groupes sont des énumérations, la même règle élargissait « fumée » vers
# « explosion » et « déplacement du sol ». Rien n'a été retiré ni réécrit : la section de I1 reste
# telle quelle, et la nouvelle dit en quoi son domaine de validité s'arrête au guide.
# Bougé le 03/09/2026 (story 5.6, T5) : le registre gagne « deux caches pour la facture ». La
# synthèse est bornée à environ deux pages et suit les jalons de la story ; les deux chiffres qui ont
# décidé les caches (0,28 € d'écriture de préfixe contre 0,015 € à chaud) et la limite volontaire du
# cache de réponses — clé exacte, jamais sémantique — se consignent ici. Rien n'a été retiré ni
# réécrit.
# Bougé le 03/09/2026 (story 5.6, T2) : le registre gagne « retirer les passes qui choisissaient,
# garder les outils ». La synthèse est bornée à environ deux pages et suit les jalons de la story ;
# le détail de ce qui a été supprimé — les deux variantes de *retrouver*, la réservation par
# sous-question, l'attribution lexicale, `couvrir_facettes`, l'attachement automatique des
# définitions, les seuils orphelins — et surtout **ce que le retrait coûte** (dix témoins live sans
# preuve réelle jusqu'au prochain enregistrement, la portée du profil qui n'atteint plus la lecture)
# se consignent ici. Rien n'a été retiré ni réécrit.
ANNEXE_SHA256 = "0a555a9552d2c6b74b7ba2bb28b2b3c5d69352bf8aeca81f71cdadc16a419db9"


def _document() -> str:
    return DOC.read_text("utf-8")


def _synthese() -> str:
    synthese, separateur, _ = _document().partition(DETAILS)
    assert separateur, "le registre historique doit être placé dans une annexe repliable"
    return synthese


def _normalise(texte: str) -> str:
    return " ".join(texte.replace("**", "").split()).lower()


def test_le_parcours_principal_est_borne_et_suit_l_ordre_de_la_story() -> None:
    synthese = _synthese()
    mots = re.findall(r"\b[\wÀ-ÿ]+(?:['’][\wÀ-ÿ]+)?\b", synthese)
    assert 1_200 <= len(mots) <= 1_900, f"la synthèse doit rester lisible comme environ deux pages, pas {len(mots)} mots"

    jalons = [
        "## Le cas météo : trouver des mots ne suffit pas",
        "### Sujet 1 —",
        "### Sujet 2 —",
        "### Sujet 3 —",
        "## Ce qui est tombé de la ligne de coupe",
        "## Confidentialité et rétention",
        "## Baseline et état courant",
        "## Limites honnêtes",
        "## Solo, équipe et IA",
        "## Chez Foyer, avec mails et pièces de sinistre, par où je commencerais",
    ]
    for jalon in jalons:
        assert synthese.count(jalon) == 1, f"jalon absent ou dupliqué : {jalon}"
    positions = [synthese.index(jalon) for jalon in jalons]
    assert positions == sorted(positions)


def test_les_choix_et_alternatives_des_trois_sujets_sont_explicites() -> None:
    synthese = _normalise(_synthese())
    attendus = (
        "présence de mots ≠ pertinence",
        "sur les deux surfaces servies — guide et sinistre",
        "le troisième sujet reste un principe de conception",
        "classeur à blocs",
        "ids stables",
        "chaque affirmation factuelle retenue cite un bloc relu",
        "tout mettre dans le contexte",
        "base vectorielle",
        "embeddings restent donc une voix possible, pas la décision",
        "le modèle propose, le code vérifie",
        "parsing niveau 1",
        "ocr systématique",
        "comparaison multi-assureurs",
        "table → base sql",
        "n'accepterait qu'un `select` sur les tables autorisées",
        "avec `limit`, timeout et connexion en lecture seule",
        "après le filtre structuré",
        "pas une capacité du produit servi",
        "plusieurs fournisseurs réduiraient le point de panne par un repli",
        "claude seul a été retenu",
    )
    for attendu in attendus:
        assert attendu in synthese


def test_la_mesure_historique_4_2d_est_rattachee_au_sinistre() -> None:
    synthese = _normalise(_synthese())
    sujet_1 = synthese.split("### sujet 1 —", 1)[1].split("### sujet 2 —", 1)[0]
    sujet_2 = synthese.split("### sujet 2 —", 1)[1].split("### sujet 3 —", 1)[0]

    assert "aucune baseline 4.4 n'est conservée" in sujet_1
    assert "story 4.2d" not in sujet_1
    assert "mesure historique de story 4.2d, sur un seul sinistre" in sujet_2
    assert "recall `2/3` des deux côtés" in sujet_2


def test_les_mesures_courantes_restent_rouges_partielles_et_non_promouvables() -> None:
    synthese = _normalise(_synthese())
    for attendu in (
        "aucune baseline 4.4 n'est conservée",
        "si la baseline gagne, elle devient la variante par défaut",
        "gate 4.5 du candidat `cf5c1ba…`",
        "rouge et non promouvable",
        # La campagne 4.5 reste publiée **datée** : c'est une mesure, elle ne s'efface pas.
        "le 30/08/2026",
        "c'est une mesure datée, pas l'état courant",
        # …et ce qui change ne se fige plus ici : la révision servie se lit sur l'API.
        "le gate et le verdict courants sont en cours de renouvellement",
        "l'état servi se lit sur `get /api/v1/sante`",
    ):
        assert attendu in synthese
    # **Aucune révision servie affirmée au présent.** Le fichier portait « le dernier produit servi
    # reste `6abd3d0…` » longtemps après que ce ne fût plus vrai. Une révision servie est un fait
    # qui change ; un document qui la fige ment dès la promotion suivante.
    assert "dernier produit servi reste" not in synthese

    assert (
        "le gate 4.5 du candidat `cf5c1ba…` s'est arrêté en incident, le 30/08/2026 : "
        "axa est partiel à `25/42`, le guide est partiel à `53/102`, "
        "baloise est indisponible, pour un coût fournisseur cumulé réel de `2,1127 eur`"
    ) in synthese

    metrique = r"(?:\d+/\d+|\d+(?:[,.]\d+)?\s*(?:%|eur|ms))"
    assert not re.search(rf"baseline 4\.4[^.!?\n]*{metrique}", synthese)
    assert not re.search(rf"{metrique}[^.!?\n]*baseline 4\.4", synthese)

    # Les nombres 4.2d sont permis seulement avec leur qualification historique et rouge.
    assert "mesure historique" in synthese
    assert "les deux séries étaient rouges et instables" in synthese
    promesses_interdites = (
        r"\b(?:gate\s+)?4\.[45](?:\s+du candidat)?\s+"
        r"(?:est|reste|devient|a été|serait|est déclaré)\s+"
        r"(?:vert(?:e)?|validé(?:e)?|promouvable)\b",
        r"\b(?:gate\s+)?4\.[45](?:\s+du candidat)?\s*(?::|—)\s*"
        r"(?:vert(?:e)?|validé(?:e)?|promouvable)\b",
        r"\b(?:gate\s+)?4\.[45](?:\s+du candidat)?\s+"
        r"(?:vert(?:e)?|validé(?:e)?|promouvable)\b",
        r"\bbaseline 4\.4\s+(?:gagnante|validée|promouvable)\b",
        r"(?<!pas )(?<!non )(?<!jamais )\bvalid(?:é|ée|és|ées)\s+par\s+"
        r"(?:(?:un|une)\s+|l['’])?expert",
        r"(?<!aucune )\bvalidation\s+par\s+(?:(?:un|une)\s+|l['’])?expert",
        r"(?<!pas )(?<!non )(?<!jamais )\bprêt(?:e|s|es)?\s+(?:pour|en|à)\s+(?:la\s+)?production\b",
    )
    for promesse_interdite in promesses_interdites:
        assert not re.search(promesse_interdite, synthese), f"promesse interdite détectée : {promesse_interdite}"


def test_les_articles_et_la_correspondance_des_assureurs_ne_sont_pas_reveles() -> None:
    synthese = _normalise(_synthese())
    references_articles = (
        r"\barticles?\s+(?:de|d'|d’|par)\s+(?:kezhan|angela)(?:\s+shi)?\b",
        r"\b(?:selon|d'après|d’après)\s+(?:kezhan|angela)(?:\s+shi)?\b",
        r"\b(?:kezhan|angela)(?:\s+shi)?(?:'s|’s)?\s+articles?\b",
        r"\[[^\]\n]*(?:kezhan|angela)(?:\s+shi)?[^\]\n]*\]\(https?://",
        r"\barticle\s+[0-9]+\b",
        r"\bpattern\s+[0-9]+\b",
        r"\bgenial-agent\b",
    )
    correspondances = (
        r"\b(?:mapping|mise en correspondance|correspondance|appariement|association|équivalence|attribution)\s+"
        r"(?:des|entre les|d['’])?\s*assureurs?\b",
        r"\bidentit(?:é|és)\s+des\s+assureurs?\b",
        r"\b(?:contrat|assureur)\s+[a-d]\s*(?:=|→|correspond\s+à)\b",
        r"\bassureurs?\s+[a-d]\s*(?:=|→|correspond\s+à)\b",
        r"\b(?:correspondance|équivalence|association)\s+(?:entre\s+)?(?:les\s+)?"
        r"(?:contrats?\s+)?[a-d](?:\s*[–—-]\s*[a-d])?[^.!?\n]{0,40}\bassureurs?\b",
        r"\bassureur\s+(?:derrière|associé à|correspondant à)\s+(?:le\s+)?(?:contrat\s+)?[a-d]\b",
        r"\btable\s+de\s+correspondance\b",
        r"\b[a-d]\s*(?:=|→|correspond\s+à|désigne)\s+[a-zà-ÿ]",
        r"\b[a-zà-ÿ][\w-]*\s+correspond\s+(?:au\s+contrat|à\s+l['’]assureur)\s+[a-d]\b",
    )
    for interdit in (*references_articles, *correspondances):
        assert not re.search(interdit, synthese), f"référence interdite détectée : {interdit}"


def test_la_politique_de_retention_est_liee_dans_sa_section() -> None:
    synthese = _synthese()
    confidentialite = synthese.split("## Confidentialité et rétention", 1)[1].split("## Baseline et état courant", 1)[0]
    assert re.search(
        r"\[[^\]\n]+\]\(https://privacy\.claude\.com/en/articles/"
        r"7996866-how-long-do-you-store-my-organization-s-data\)",
        confidentialite,
    )


def test_confidentialite_retenue_et_limites_obligatoires_sont_dites() -> None:
    synthese = _normalise(_synthese())
    attendus = (
        "vérifiée le 25/08/2026",
        "sous 30 jours",
        "service à rétention plus longue",
        "accord de rétention différent",
        "politique d'usage",
        "obligation légale",
        "jusqu'à deux ans",
        "le navigateur ne persiste pas les conversations non plus",
        "son stockage local conserve en revanche des données de reprise et de configuration",
        "profil et préférences d'affichage, avancement du parcours",
        "adresses et coordonnées de comparaison",
        "sélections de sinistres et paramètres de simulation",
        "aucune donnée utilisateur n'entre dans `data/`",
        "réduit l'injection d'instructions, sans l'éliminer",
        "best-effort",
        "fichiers versionnés",
        "ne tient pas à l'échelle",
        "mémoire n'a été mesurée que sur deux documents",
        "pas validés par un expert assurance",
        "ni une décision de garantie ni un conseil",
        "sans authentification ni cadre contractuel",
        "couverture de parsing ne prouve pas l'intégrité juridique",
        "ne sont pas redistribués dans le dépôt",
        "unique fournisseur de modèles",
        "ni slo",
        "ni supervision",
        "ni reprise automatique",
        "ni circuit breaker",
        "ni test de charge",
        "pdf.js",
        "observabilité",
        "rester au centre, garder les décisions",
        "pas eu de réécriture humaine intermédiaire",
    )
    for attendu in attendus:
        assert attendu in synthese

    assert "le navigateur garde seulement le profil et les préférences d'affichage" not in synthese


def test_l_annexe_technique_historique_est_repliable_et_byte_identique() -> None:
    document = _document()
    assert document.count("<details>") == document.count("</details>") == 1
    assert document.endswith("</details>\n"), "rien ne doit suivre l'annexe repliable"
    assert "<summary>Annexe technique — registre historique conservé à l'identique</summary>" in document

    debut = document.index(ANNEXE_TITLE)
    fin = document.rindex("\n</details>\n")
    annexe = document[debut:fin].encode("utf-8")
    assert hashlib.sha256(annexe).hexdigest() == ANNEXE_SHA256
    for marqueur in (
        "COLUMN_GUTTER_MIN_PT=18.0",
        "STRUCTURE_MAX_COST_EUR=8.0",
        # Les déclarations `empreinte-committee-perimee: <doc>` sont dynamiques par construction
        # (la garde exige leur retrait dès que la réingestion rétablit l'égalité) : le registre
        # doit nommer le mécanisme, jamais épingler une déclaration active.
        "assert_empreinte_committee_declaree",
    ):
        assert marqueur in annexe.decode("utf-8")
