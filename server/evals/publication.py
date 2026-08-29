"""FR41 / FR42 — Construire, rendre et écrire l'artefact **unique** des résultats d'évals.

Quatre fonctions, et la frontière entre elles est le point de la story :

0. `valider_rapport_publiable(...)` — **la** lecture du rapport, appelée par tous les chemins qui
   alimentent les quatre surfaces. Une clé absente, nulle, mal typée ou hors domaine est un refus
   dit (`RapportInexploitable`) qui nomme la clé, jamais un chiffre fabriqué ni une erreur nue.
1. `construire_publication(...)` — projette un rapport de run, son gate et les réserves du dépôt dans
   `PublicationEvals` (`server/app/domain/evals.py`). Les **limites** y sont *dérivées* du run —
   décisions rouges chiffrées, réserves à `false`, exécutions manquantes, écarts de parsing, état
   incomplet — et jamais rédigées : une phrase qu'aucun chiffre ne produit est une phrase qu'aucun
   chiffre ne peut démentir.
2. `rendre_publication_markdown(...)` — **un seul** rendu Markdown, réutilisé tel quel par
   `docs/evals/latest.md` et par le résumé que la CI concatène dans `$GITHUB_STEP_SUMMARY`. Deux
   rendus séparés auraient divergé au premier arrondi, et l'AC compare les quatre surfaces « à
   l'octet des chiffres près ».
3. `preparer_publication(...)` — **l'unique** écrivain : il prépare toutes les sorties dans des
   temporaires de leurs répertoires cibles et rend `[(tmp, cible)]` à basculer avec l'entrée de
   manifest. Il n'y a pas de second écrivain « plus simple » (revue R6) : une seconde plomberie
   sans la garantie tout-ou-rien finit par laisser une surface affirmer un verdict que l'autre ne
   porte pas — c'est précisément ce que les revues B3 et B7 ont eu à fermer sur celle-ci.

**La publication est inconditionnelle.** Un run rouge est publié avec ses limites : publier ne promeut
rien, et seul `gate.evals_ok` décide de ce qui est servi (AD-8). Un artefact qui n'apparaîtrait que
lorsque tout est vert ferait de l'absence de nouvelle une bonne nouvelle.
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from typing import Any

from server.app.config import EVALS_PUBLICATION_FILE
from server.app.domain.evals import (LABELS, CoutPublie, LatencePubliee, PublicationEvals,
                                     ReservesPubliees, SecondeLecturePubliee, StabilitePubliee)
from server.app.domain.ingest import Gate, GateDecision

class RapportInexploitable(Exception):
    """Le rapport n'a pas les structures qu'une publication exige : elle ne peut pas être fabriquée.

    Story 4.5, revue B5 (chemins frères). Les lectures `rapport.get(x) or defaut` faisaient dire
    « il n'y en avait pas » à « la clé n'est pas là », et publiaient alors des **chiffres fabriqués**
    sur les quatre surfaces : un `metrics` absent devenait `recall=0.0` et des latences à zéro
    présentées comme des mesures ; un `decisions` absent devenait « aucune décision rouge » ; un
    `stability` absent devenait « 0/0 (N=1) ». C'est l'invariant « aucun chiffre inventé » de la
    story, violé à sa propre surface.
    """


def _exiger(rapport: dict[str, Any], cle: str, types: tuple[type, ...]) -> Any:
    """La valeur sous `cle` — **clé exigée, type exigé**, jamais un défaut silencieux.

    Même forme que `plancher.verifier_identite_externe` : exiger la clé, exiger le type, puis lire.
    """
    if cle not in rapport:
        raise RapportInexploitable(
            f"rapport : la clé obligatoire {cle!r} est absente — une publication bâtie dessus "
            "publierait des chiffres que personne n'a mesurés")
    valeur = rapport[cle]
    if not isinstance(valeur, types):
        raise RapportInexploitable(
            f"rapport : {cle!r} doit être {' ou '.join(t.__name__ for t in types)} "
            f"({type(valeur).__name__} reçu)")
    return valeur


# Les mesures que la publication rend en **réels** et celles qu'elle rend en **entiers**. La
# distinction n'est pas cosmétique : `int(14243.7)` tronquerait une latence, c'est-à-dire
# publierait un chiffre que personne n'a mesuré — la faute même que ce module refuse.
MESURES_REELLES: tuple[str, ...] = (
    "recall", "average_cost_eur", "cost_p95_eur", "ne_tranche_pas_rate",
)
MESURES_ENTIERES: tuple[str, ...] = ("latency_p50_ms", "latency_p95_ms")
# Les deux tables de comptage : jamais `None` rabattu sur `{}`, sous peine de publier « aucun label
# observé » pour un rapport qui n'a simplement pas su les écrire.
TABLES_METRICS: tuple[str, ...] = ("labels", "variants")
# Le **domaine** de chaque mesure, et pas seulement son type (revue R7). `PublicationEvals` borne
# déjà `recall` et `ne_tranche_pas_rate` à [0, 1] et les coûts/latences à >= 0, mais il le fait par
# une `ValidationError` pydantic qui ne nomme ni la clé du rapport ni sa raison — le refus dit
# s'arrêtait donc au type. `None` veut dire « aucune borne de ce côté ».
DOMAINES_MESURES: dict[str, tuple[float | None, float | None]] = {
    "recall": (0.0, 1.0),
    "ne_tranche_pas_rate": (0.0, 1.0),
    "average_cost_eur": (0.0, None),
    "cost_p95_eur": (0.0, None),
    "cost_eur": (0.0, None),
    "latency_p50_ms": (0, None),
    "latency_p95_ms": (0, None),
}
# Les clés que **chaque exécution** du journal de run indexe (`run.rendre_markdown`), avec le type
# qu'elle doit porter. Une clé manquante y levait un `KeyError` nu (revue R1).
CHAMPS_RESULTAT: tuple[tuple[str, str], ...] = (
    ("id", "texte"), ("suite", "texte"), ("variant", "texte"), ("label", "texte"),
    ("cost_eur", "reel"), ("cost_eur_original", "reel"), ("latency_ms", "entier"),
)
# Les clés que le **journal** indexe à la racine, hors mesures (revue R1). `stop_reason` est dans la
# liste : elle peut valoir `None`, mais elle doit **être là** — c'est la distinction que tout ce
# cycle défend, « la clé n'est pas là » n'est pas « il n'y a rien ».
CHAMPS_JOURNAL: tuple[tuple[str, type | None], ...] = (
    ("cases_hash", str), ("cases_planned", int), ("cases_completed", int), ("stop_reason", None),
)


def _mesure(conteneur: dict[str, Any], cle: str, *, chemin: str, entier: bool) -> int | float:
    """Une mesure : **présente, non booléenne, du bon type, finie** — sinon un refus qui la nomme.

    Story 4.5, cycle de récupération, revue B5. Le tour précédent avait fermé la *présence* de la
    clé et laissé sa *valeur* libre : `metrics.recall = None` passait le contrôle, puis
    `float(metrics.get("recall") or 0.0)` le publiait à `0.0000` — un rappel nul, présenté comme une
    mesure, sur les quatre surfaces. `True` passait aussi, et se publiait `1.0000`.

    Le booléen est exclu explicitement parce que Python en fait un entier : sans ce test,
    `isinstance(True, int)` suffit à faire d'un drapeau une latence.
    """
    if cle not in conteneur:
        raise RapportInexploitable(
            f"rapport : {chemin}.{cle} est absent — le publier à zéro serait présenter une absence "
            "de mesure comme une mesure")
    valeur = conteneur[cle]
    if valeur is None:
        raise RapportInexploitable(
            f"rapport : {chemin}.{cle} vaut None — « pas de mesure » n'est pas « une mesure à "
            "zéro », et les quatre surfaces publieraient le second")
    if isinstance(valeur, bool):
        raise RapportInexploitable(
            f"rapport : {chemin}.{cle} est un booléen — ce n'est pas une mesure")
    if entier:
        if not isinstance(valeur, int):
            raise RapportInexploitable(
                f"rapport : {chemin}.{cle} doit être un entier ({type(valeur).__name__} reçu) — "
                "arrondir une mesure pour la publier reviendrait à en inventer une")
        return valeur
    if not isinstance(valeur, (int, float)) or not math.isfinite(valeur):
        raise RapportInexploitable(
            f"rapport : {chemin}.{cle} doit être un nombre fini ({valeur!r} reçu)")
    return valeur


def _dans_le_domaine(valeur: int | float, cle: str, *, chemin: str) -> None:
    """La mesure est-elle dans les bornes que la publication garantit ? (revue R7).

    Refuser par le modèle publié aurait rendu une `ValidationError` pydantic : un refus, oui, mais
    qui ne nomme ni la clé du rapport ni sa raison typée — c'est-à-dire pas le refus que ce module
    promet, ni celui que `docs/evals/harness.md` décrit.
    """
    bornes = DOMAINES_MESURES.get(cle)
    if bornes is None:
        return
    bas, haut = bornes
    if bas is not None and valeur < bas:
        raise RapportInexploitable(
            f"rapport : {chemin}.{cle} vaut {valeur!r}, hors du domaine (>= {bas}) — une mesure "
            "hors bornes n'est pas une mesure prudente, c'est une mesure fausse")
    if haut is not None and valeur > haut:
        raise RapportInexploitable(
            f"rapport : {chemin}.{cle} vaut {valeur!r}, hors du domaine (<= {haut})")


def valider_rapport_publiable(rapport: Any) -> dict[str, Any]:
    """**La** validation canonique d'un rapport avant publication — un seul contrôle, quatre surfaces.

    Story 4.5, cycle de récupération, revue B5. Le défaut fermé ici n'est pas un oubli local : c'est
    une politique de lecture. Chaque surface lisait le rapport à sa façon, avec ses propres replis
    (`or 0.0`, `or {}`, `or 0`), si bien qu'une donnée invalide ne produisait jamais un refus mais
    toujours un **chiffre plausible** — `recall` nul, tables vides, `0/0` de stabilité — que rien ne
    distinguait ensuite d'une mesure réelle.
    Cette fonction est donc appelée par **tous** les chemins qui alimentent les quatre surfaces
    (`construire_publication`, `stabilite_du_rapport`, `limites_du_rapport`, et
    `run.rendre_markdown` pour le résumé que la CI concatène), et elle refuse **avant** qu'aucune
    surface n'affiche quoi que ce soit.

    Ce qui peut légitimement manquer reste distinct de ce qui manque à tort, et c'est tout l'objet
    de la nuance :

    - `stability` **absent** est normal (il n'est écrit que sous `repeat > 1`) — mais `stability`
      présent *sans* `cases` ne l'est pas : il produirait un `0/0` fabriqué ;
    - `reserves` absentes sont normales sur un diagnostic — le rapport ne les établit pas ;
    - `decisions` vides sont normales sans plancher — mais des décisions **non vides** sans
      `plancher_digest` racine ne le sont pas : elles ne diraient contre quels seuils elles ont été
      prises.

    Rend le rapport tel quel (pour chaîner), ou lève `RapportInexploitable` en nommant la clé
    fautive et sa raison typée.
    """
    if not isinstance(rapport, dict):
        raise RapportInexploitable(
            f"rapport : un objet est attendu ({type(rapport).__name__} reçu)")
    profil = _exiger(rapport, "profile", (str,))
    if not profil:
        raise RapportInexploitable(
            "rapport : 'profile' est vide — un run qui ne dit pas ce qu'il a mesuré ne se publie pas")
    _exiger(rapport, "complete", (bool,))
    _exiger(rapport, "unexecuted_cases", (list,))
    _exiger(rapport, "identity", (dict,))
    _exiger(rapport, "results", (list,))
    # **Les clés que le journal de run indexe**, exigées ici et non découvertes à la ligne de rendu
    # (revue R1) : `rapport['cases_completed']` sur un rapport amputé levait un `KeyError` nu, qui
    # n'est pas une `ValueError` et tombait donc dans le dernier `except Exception` de `run._main` —
    # « incident de gate », code 3. Un défaut de données étiqueté panne technique, exactement la
    # ligne de partage des codes de sortie que la spec interdit de franchir.
    for champ, type_attendu in CHAMPS_JOURNAL:
        if champ not in rapport:
            raise RapportInexploitable(
                f"rapport : la clé obligatoire {champ!r} est absente — le journal du run l'indexe, "
                "et son absence sortirait en panne technique au lieu d'un refus dit")
        if type_attendu is not None and not isinstance(rapport[champ], type_attendu):
            raise RapportInexploitable(
                f"rapport : {champ!r} doit être {type_attendu.__name__} "
                f"({type(rapport[champ]).__name__} reçu)")
    # Le coût **froid** du run : c'est le chiffre que la publication présente comme « ce qu'une
    # campagne paie réellement ». `float(rapport.get("cost_eur") or 0.0)` en faisait un run gratuit.
    _dans_le_domaine(_mesure(rapport, "cost_eur", chemin="rapport", entier=False),
                     "cost_eur", chemin="rapport")
    metrics = _exiger(rapport, "metrics", (dict,))
    for champ in MESURES_REELLES:
        _dans_le_domaine(_mesure(metrics, champ, chemin="metrics", entier=False),
                         champ, chemin="metrics")
    for champ in MESURES_ENTIERES:
        _dans_le_domaine(_mesure(metrics, champ, chemin="metrics", entier=True),
                         champ, chemin="metrics")
    for champ in TABLES_METRICS:
        if champ not in metrics:
            raise RapportInexploitable(
                f"rapport : metrics.{champ} est absent — publier une table vide dirait « rien "
                "n'a été observé » là où le rapport n'a rien su écrire")
        if not isinstance(metrics[champ], dict):
            raise RapportInexploitable(
                f"rapport : metrics.{champ} doit être un objet "
                f"({type(metrics[champ]).__name__} reçu)")
        for nom, compte in metrics[champ].items():
            if isinstance(compte, bool) or not isinstance(compte, int) or compte < 0:
                raise RapportInexploitable(
                    f"rapport : metrics.{champ}[{nom!r}] doit être un entier >= 0 "
                    f"({compte!r} reçu)")
    # **Le vocabulaire fixe d'AD-14 est complet**, ou ce n'est pas la table que les surfaces
    # publient : le journal de CI indexe `metrics.labels[label]` sur **les sept**, et un label
    # manquant y levait un `KeyError` nu.
    manquants = [label for label in LABELS if label not in metrics["labels"]]
    if manquants:
        raise RapportInexploitable(
            f"rapport : metrics.labels n'est pas le vocabulaire fixe d'AD-14 (manquants : "
            f"{manquants}) — un zéro absent n'est pas un zéro observé")
    _valider_resultats(rapport["results"])
    _valider_stabilite(rapport)
    _valider_decisions(rapport)
    _valider_reserves(rapport)
    return rapport


def _valider_resultats(resultats: list[Any]) -> None:
    """Chaque exécution porte les clés que le journal du run indexe (revue R1)."""
    for rang, resultat in enumerate(resultats):
        if not isinstance(resultat, dict):
            raise RapportInexploitable(
                f"rapport : results[{rang}] doit être un objet "
                f"({type(resultat).__name__} reçu)")
        for champ, forme in CHAMPS_RESULTAT:
            if champ not in resultat:
                raise RapportInexploitable(
                    f"rapport : results[{rang}].{champ} est absent — le journal du run l'indexe")
            valeur = resultat[champ]
            if forme == "texte":
                if not isinstance(valeur, str):
                    raise RapportInexploitable(
                        f"rapport : results[{rang}].{champ} doit être une chaîne "
                        f"({type(valeur).__name__} reçu)")
            else:
                _mesure(resultat, champ, chemin=f"results[{rang}]", entier=(forme == "entier"))


def _valider_stabilite(rapport: dict[str, Any]) -> None:
    """`stability` absent est légitime ; `stability` présent et creux ne l'est pas (revue B5)."""
    agregat = rapport.get("stability")
    if agregat is None and "stability" not in rapport:
        # Un run sans répétition n'écrit pas d'agrégat : le `repeat` du rapport donne alors le N,
        # et il est **exigé** plutôt que fabriqué à 1.
        repeat = _exiger(rapport, "repeat", (int,))
        if isinstance(repeat, bool) or repeat < 1:
            raise RapportInexploitable(
                f"rapport : 'repeat' doit être un entier >= 1 ({repeat!r} reçu)")
        return
    if not isinstance(agregat, dict):
        raise RapportInexploitable(
            f"rapport : 'stability' doit être un objet ({type(agregat).__name__} reçu)")
    if "cases" not in agregat:
        raise RapportInexploitable(
            "rapport : 'stability' est présent sans 'cases' — publier « 0/0 » dirait « aucun cas "
            "n'était stable » là où le rapport ne dit rien du tout")
    cases = agregat["cases"]
    if not isinstance(cases, dict):
        raise RapportInexploitable(
            f"rapport : 'stability.cases' doit être un objet ({type(cases).__name__} reçu)")
    if any(not isinstance(v, dict) for v in cases.values()):
        raise RapportInexploitable(
            "rapport : chaque entrée de 'stability.cases' doit être un objet")
    n = agregat.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise RapportInexploitable(
            f"rapport : 'stability.n' doit être un entier >= 1 ({n!r} reçu) — le N d'une stabilité "
            "ne se devine pas")


def _valider_decisions(rapport: dict[str, Any]) -> None:
    """Des décisions non vides disent contre quels seuils elles ont été prises (revue B5).

    Et **chaque entrée est un objet** (revue R7) : `decisions=[42]` passait la validation canonique
    et n'était refusé qu'en `ValidationError` pydantic nue, à la construction de `GateDecision` —
    un refus, mais qui ne nomme ni la clé du rapport ni sa raison typée.
    """
    if rapport.get("plancher_digest") is not None:
        decisions = _exiger(rapport, "decisions", (list,))
    else:
        decisions = rapport.get("decisions")
        if decisions is None:
            return
        if not isinstance(decisions, list):
            raise RapportInexploitable(
                f"rapport : 'decisions' doit être une liste ({type(decisions).__name__} reçu)")
        if decisions:
            raise RapportInexploitable(
                "rapport : 'decisions' est non vide alors que 'plancher_digest' est absent de la "
                "racine — une décision qui ne nomme pas son protocole ne dit pas contre quel seuil "
                "elle a été prise, et la publier la présenterait comme opposable")
    for rang, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            raise RapportInexploitable(
                f"rapport : decisions[{rang}] doit être un objet "
                f"({type(decision).__name__} reçu)")


class ArchivePrecedenteIllisible(Exception):
    """Le rendu précédent existe mais ne se lit pas : l'écraser le perdrait sans archive."""


# **Une seule autorité** pour le nom du fichier servi : `config.EVALS_PUBLICATION_FILE`, que le
# lecteur (`api/etat.py`) lit aussi. Deux littéraux auraient pu diverger sans bruit.
PUBLICATION_JSON = EVALS_PUBLICATION_FILE
DOCS_LATEST = ("docs", "evals", "latest.md")
# Où le rendu lisible **précédent** est archivé avant d'être remplacé (revue 4.5, P7).
DOCS_ARCHIVES = ("docs", "evals", "campagnes")

# La réserve d'AD-14, écrite **avant** tout chiffre et sur les quatre surfaces. Elle n'est pas une
# formule de politesse : ce dépôt produit des verdicts d'assurance qu'aucun expert n'a validés, et la
# première chose qu'un lecteur doit savoir est celle-là.
RESERVE_NON_EXPERTE = (
    "Avertissement non expert : aucun verdict, aucune vérité de référence et aucune limite publiée "
    "ici n'est validée par un expert assurance.")


# **Le** formatage des nombres publiés, partagé par toutes les surfaces (revue 4.5, P5).
#
# Sans lui, chaque surface formatait à sa façon et les chiffres divergeaient dès qu'une valeur ne
# tombait pas juste : `recall=1.0` s'écrivait `1.0000` dans le Markdown et `1` sur la page,
# `0.055 €` devenait `0.0550 €` d'un côté et `0.055 €` de l'autre. Les valeurs viennent toutes d'un
# `round(..., 4)` du runner : quatre décimales les rendent **toutes** sans en inventer aucune, et
# `tools/accueil/accueil.js::nombre4` applique exactement la même règle côté page.
DECIMALES = 4


_HEX = frozenset("0123456789abcdef")


def _empreinte(valeur: Any, longueur: int = 64) -> str | None:
    """La valeur si c'est bien une empreinte, `None` sinon — **jamais** une chaîne recopiée.

    Le rapport et le gate sont des entrées : rien ne garantit qu'un `cases_hash` bricolé à la main y
    ressemble à une empreinte. Le publier tel quel ferait afficher aux quatre surfaces une identité
    qui n'en est pas ; refuser la publication entière serait pire — un rapport lisible cesserait
    d'être publiable. « Ce champ n'est pas une empreinte » se dit donc par son absence.
    """
    texte = "" if valeur is None else str(valeur)
    return texte if len(texte) == longueur and all(c in _HEX for c in texte) else None


def nombre(valeur: float) -> str:
    """Un taux ou un montant, rendu identiquement sur les quatre surfaces."""
    return f"{valeur:.{DECIMALES}f}"


CHAMPS_RESERVES: tuple[str, ...] = (
    "countersigned", "validated_by_expert", "dictionary_validated",
)


def _reserves_du_rapport(rapport: dict[str, Any]) -> ReservesPubliees | None:
    """Les réserves telles que le **rapport** les porte, ou `None` s'il ne les établit pas.

    Un run de diagnostic n'établit ni contresignature, ni validation, ni signature de dictionnaire :
    inventer leur état serait pire que de ne rien en dire (AD-16).

    **Absent et illisible se distinguent ici comme partout ailleurs** (revue R3). La version
    précédente rabattait sur `None` des réserves *présentes mais mal formées* : un
    `{"countersigned": "oui", …}` faisait publier « ce run est un diagnostic : il n'établit ni
    contresignature, ni validation par un expert, ni signature du dictionnaire » sur un run qui *a*
    établi ses réserves — et les trois limites correspondantes disparaissaient des limites publiées.
    C'est la faute exacte que ce cycle ferme partout : dire « il n'y en avait pas » là où la vraie
    réponse est « je n'ai pas su la lire ». La clé absente reste un diagnostic ; la clé présente et
    mal formée est un refus qui nomme le champ.
    """
    _valider_reserves(rapport)
    if "reserves" not in rapport:
        return None
    return ReservesPubliees.model_validate(rapport["reserves"])


def _valider_reserves(rapport: dict[str, Any]) -> None:
    """Le contrôle, séparé de la lecture, pour que **tous** les chemins des quatre surfaces l'aient."""
    if "reserves" not in rapport:
        return
    brut = rapport["reserves"]
    if not isinstance(brut, dict):
        raise RapportInexploitable(
            f"rapport : 'reserves' doit être un objet ({type(brut).__name__} reçu) — une réserve "
            "illisible n'est pas une réserve absente")
    for champ in CHAMPS_RESERVES:
        if champ not in brut:
            raise RapportInexploitable(
                f"rapport : reserves.{champ} est absent — des réserves partielles publieraient "
                "« diagnostic » sur un run qui a bien établi les autres")
        if not isinstance(brut[champ], bool):
            raise RapportInexploitable(
                f"rapport : reserves.{champ} doit être un booléen ({brut[champ]!r} reçu)")


def stabilite_du_rapport(rapport: dict[str, Any]) -> StabilitePubliee:
    """N/N depuis l'agrégat du run ; les cas `parsing` restent hors comptage, comme au plancher.

    `stability` n'est écrit que sous `repeat > 1` : **son absence est légitime** et se lit alors
    « aucun cas comptabilisé », avec le `repeat` du rapport pour N. Ce qui ne l'est pas, c'est de
    fabriquer `N=1` quand le rapport ne dit même pas combien de répétitions il a planifiées, ni de
    publier `0/0` pour un `stability` présent mais sans `cases` : les deux passent désormais par
    `valider_rapport_publiable` (revue B5), et se refusent avant toute surface.
    """
    valider_rapport_publiable(rapport)
    agregat = rapport.get("stability")
    if not isinstance(agregat, dict):
        return StabilitePubliee(n=int(rapport["repeat"]), cas_stables=0, cas_comptabilises=0)
    comptabilises = [v for v in agregat["cases"].values() if v.get("comptabilise")]
    return StabilitePubliee(
        n=int(agregat["n"]),
        cas_stables=sum(1 for v in comptabilises if v.get("stable")),
        cas_comptabilises=len(comptabilises))


def limites_du_rapport(rapport: dict[str, Any], decisions: list[GateDecision],
                       reserves: ReservesPubliees | None = None,
                       seconde_lecture: SecondeLecturePubliee | None = None) -> list[str]:
    """Les limites du run, **dérivées** — cinq sources, aucune prose.

    L'ordre est celui de la gravité décroissante pour qui lit : ce qui a été mesuré rouge, ce qui n'a
    pas été exécuté, ce que l'extraction n'a pas rendu, ce que personne n'a signé, ce qui reste dû.

    C'est **l'unique** dérivation : la publication l'appelle, et `run.rendre_markdown` l'appelle
    aussi pour le rapport que la CI concatène. Deux listes de limites calculées séparément auraient
    divergé au premier cas particulier — et l'AC 4 compare précisément ces deux surfaces.

    `reserves` et `seconde_lecture` sont facultatifs : un rapport de diagnostic (`--profile full`
    sans `--gate`) n'établit ni contresignature ni seconde lecture, et inventer leur état serait pire
    que de ne rien en dire.
    """
    valider_rapport_publiable(rapport)
    limites: list[str] = []
    for d in decisions:
        if d.status != "green":
            if d.reason:
                # Une décision rouge « producteur non probant » ou « sous-échantillonné » a une
                # valeur qui **tient** le plancher : publier `1.0000 < plancher 1.0000` aurait écrit
                # une inégalité fausse, et fait chercher un défaut de mesure là où il n'y en a pas.
                # Quand la décision porte sa raison, c'est elle qui explique le rouge.
                limites.append(
                    f"décision rouge {d.metric} : {d.reason} "
                    f"(valeur {nombre(d.value)}, plancher {nombre(d.threshold)}, n={d.n}, "
                    f"scope {d.scope}, producteur {d.producer})")
            else:
                limites.append(
                    f"décision rouge {d.metric} : {nombre(d.value)} < plancher "
                    f"{nombre(d.threshold)} (n={d.n}, scope {d.scope}, producteur {d.producer})")
    if not rapport["complete"]:
        limites.append(
            "run incomplet : " + str(rapport.get("stop_reason") or "interruption non qualifiée"))
    non_executes = list(rapport["unexecuted_cases"])
    if non_executes:
        limites.append(
            f"{len(non_executes)} exécution(s) planifiée(s) non exécutée(s), rouges au "
            f"dénominateur : {', '.join(non_executes)}")
    ecarts_parsing = sorted({str(r["id"]) for r in rapport["results"]
                             if r.get("label") == "parsing"})
    if ecarts_parsing:
        limites.append(
            "écart de parsing (le texte extrait diverge de la lecture visuelle) sur : "
            + ", ".join(ecarts_parsing))
    if reserves is not None:
        if not reserves.countersigned:
            limites.append(
                "contresignature humaine des cas relus : due — la relecture qui fonde ce gate est "
                "celle de la boucle autonome")
        if not reserves.validated_by_expert:
            limites.append(
                "aucun verdict n'est validé par un expert assurance (AD-14 : `validated_by_expert` "
                "est faux pour tout ce que ce projet produit)")
        if not reserves.dictionary_validated:
            limites.append(
                "dictionnaire des variantes non validé : le refus « zéro hit » d'AD-5 est désarmé")
    if seconde_lecture is not None and seconde_lecture.statut != "concordante":
        improjetables = (f", dont {seconde_lecture.blocs_non_projetables} clé(s) attendue(s) "
                         "impossibles à projeter en image"
                         if seconde_lecture.blocs_non_projetables else "")
        limites.append(
            f"seconde lecture sur images de pages : {seconde_lecture.statut} "
            f"({seconde_lecture.blocs_verifies}/{seconde_lecture.blocs_planifies} bloc(s) "
            f"relu(s){improjetables})")
    return limites


def construire_publication(rapport: dict[str, Any], gate: Gate | None = None, *,
                           reserves: ReservesPubliees | None = None,
                           relecture: SecondeLecturePubliee | None = None,
                           report_digest: str | None = None,
                           candidate_revision: str | None = None) -> PublicationEvals:
    """L'objet publié : les chiffres du rapport, l'identité du gate, les limites dérivées.

    **`gate` est facultatif** (correctif P6 du tour de revue précédent). Un `--profile full` sans `--gate` — ce que la CI lance à
    chaque PR — produit un rapport sans verdict : la publication se construit quand même, depuis le
    seul rapport, et les champs liés au gate restent **absents**. C'est ce qui permet au Markdown que
    la CI concatène dans `$GITHUB_STEP_SUMMARY` d'être rendu par **le même** renderer que
    `docs/evals/latest.md` — un second renderer aurait divergé, et c'est exactement ce que l'AC 4
    interdit.

    `report_digest` est l'empreinte des **octets réellement écrits** du rapport JSON — pas un hash
    recalculé sur une re-sérialisation, qui pourrait différer d'un espace et rendre invérifiable ce
    que la publication prétend résumer. Il est `None` tant que le rapport n'est pas figé.
    """
    # **La validation canonique d'abord**, et elle est la seule (revue B5). Tout ce qui suit lit le
    # rapport par indexation directe : plus aucun `or 0.0`, `or {}` ni `or 0` ne peut transformer
    # une donnée absente, nulle ou mal typée en un chiffre plausible sur les quatre surfaces.
    valider_rapport_publiable(rapport)
    reserves = reserves if reserves is not None else _reserves_du_rapport(rapport)
    metrics = rapport["metrics"]
    decisions = [GateDecision.model_validate(d) for d in (rapport.get("decisions") or [])]
    limites = limites_du_rapport(rapport, decisions, reserves, relecture)
    identite = rapport["identity"]
    return PublicationEvals(
        profile=str(rapport["profile"]),
        candidate_revision=_empreinte(
            candidate_revision or (gate.candidate_revision if gate else None)
            or identite.get("candidate_revision"), 40),
        run_digest=_empreinte(identite.get("run_digest")),
        report_digest=_empreinte(report_digest),
        plancher_digest=_empreinte(rapport.get("plancher_digest")
                                   or (gate.plancher_digest if gate else None)),
        cases_hash=_empreinte(rapport.get("cases_hash") or (gate.cases_hash if gate else None)),
        date=str(gate.date if gate else (rapport.get("generated_at") or "")),
        evals_ok=(gate.evals_ok if gate else None),
        variantes=dict(metrics["variants"]),
        labels=dict(metrics["labels"]),
        recall=float(metrics["recall"]),
        stabilite=stabilite_du_rapport(rapport),
        cout=CoutPublie(
            # Le gate désarme le cache sous `--repeat` : le coût du run **est** le coût froid.
            froid_eur=float(rapport["cost_eur"]),
            moyen_eur=float(metrics["average_cost_eur"]),
            p95_eur=float(metrics["cost_p95_eur"])),
        latence=LatencePubliee(p50_ms=int(metrics["latency_p50_ms"]),
                               p95_ms=int(metrics["latency_p95_ms"])),
        ne_tranche_pas_rate=float(metrics["ne_tranche_pas_rate"]),
        reserves=reserves,
        decisions=decisions,
        limites=limites,
        seconde_lecture=relecture)


def rendre_publication_markdown(pub: PublicationEvals,
                                *, valeur: Any = None, code: Any = None) -> str:
    """Le rendu **unique**, partagé par `docs/evals/latest.md` et le résumé de CI.

    `valeur`/`code` sont les deux échappements durcis du runner (`_markdown_value`,
    `_markdown_code`) : ils sont passés en paramètre plutôt qu'importés pour que ce module reste
    lisible sans `run.py` (et testable seul), tout en garantissant qu'une valeur dynamique ne peut ni
    ouvrir du code, ni casser une cellule ou une ligne. Sans eux, un repli inoffensif est utilisé.
    """
    if valeur is None or code is None:  # pragma: no cover — le runner passe toujours les deux
        from server.evals.run import _markdown_code, _markdown_value
        valeur = valeur or _markdown_value
        code = code or _markdown_code
    verdict = "diagnostic (aucun gate)" if pub.evals_ok is None else (
        "vert" if pub.evals_ok else "rouge")
    lignes = [
        "# Résultats des questions-témoins — dernier run publié",
        "",
        f"> **{RESERVE_NON_EXPERTE}**",
        "",
        "> **Publié, jamais promu.** Ce document est écrit à chaque run de gate, rouge compris "
        "(FR41). Il ne promeut rien : seul `gate.evals_ok` décide de ce qui est servi (AD-8).",
        "",
        f"Gate **{verdict}** — profil {code(pub.profile)}, "
        f"révision candidate {code(pub.candidate_revision or '—')}, {valeur(pub.date)}.",
        "",
        "## Identité",
        "",
        "| run_digest | report_digest | plancher_digest | cases_hash |",
        "|---|---|---|---|",
        f"| {code(pub.run_digest or '—')} | {code(pub.report_digest or '—')} | "
        f"{code(pub.plancher_digest or '—')} | {code(pub.cases_hash or '—')} |",
        "",
        "## Chiffres",
        "",
        "| recall | stabilité | coût froid (€) | coût moyen (€) | coût p95 (€) | latence p50 (ms) "
        "| latence p95 (ms) | ne_tranche_pas |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| {nombre(pub.recall)} | {pub.stabilite.cas_stables}/{pub.stabilite.cas_comptabilises} "
        f"(N={pub.stabilite.n}) | {nombre(pub.cout.froid_eur)} | {nombre(pub.cout.moyen_eur)} | "
        f"{nombre(pub.cout.p95_eur)} | {pub.latence.p50_ms} | {pub.latence.p95_ms} | "
        f"{nombre(pub.ne_tranche_pas_rate)} |",
        "",
        "| Label | Nombre |",
        "|---|---:|",
    ]
    lignes += [f"| {code(label)} | {nombre} |" for label, nombre in sorted(pub.labels.items())]
    lignes += ["", "| Variante | Nombre |", "|---|---:|"]
    lignes += [f"| {code(variante)} | {nombre} |"
               for variante, nombre in sorted(pub.variantes.items())]
    lignes += ["", "## Décisions du plancher", ""]
    if not pub.decisions:
        # **Aucune ligne fabriquée** (revue B5). Une ligne `| — | — | — | 0 | 0.0000 | 0.0000 | — |`
        # publiait un `n` et deux seuils qu'aucune décision n'avait produits : « il n'y a pas de
        # décision » se dit en toutes lettres, pas avec des zéros dans un tableau de décisions.
        lignes += ["Ce run n'a pris aucune décision de plancher.", ""]
    else:
        lignes += [
            "| metric | producteur | scope | n | valeur | plancher | statut |",
            "|---|---|---|---:|---:|---:|---|",
        ]
        lignes += [
            f"| {code(d.metric)} | {code(d.producer)} | {code(d.scope)} | {d.n} | "
            f"{nombre(d.value)} | {nombre(d.threshold)} | {code(d.status)} |"
            for d in sorted(pub.decisions, key=lambda d: d.metric)
        ]
    lignes += ["", "## Réserves", ""]
    if pub.reserves is None:
        # Un diagnostic n'établit aucune réserve : le dire vaut mieux que de fabriquer trois `false`.
        lignes += ["Ce run est un diagnostic : il n'établit ni contresignature, ni validation par un "
                   "expert, ni signature du dictionnaire.", ""]
    else:
        lignes += [
            "| contresignature humaine | validé par un expert | dictionnaire validé |",
            "|---|---|---|",
            f"| {code(pub.reserves.countersigned)} | {code(pub.reserves.validated_by_expert)} | "
            f"{code(pub.reserves.dictionary_validated)} |",
            "",
        ]
    if pub.seconde_lecture is not None:
        lignes += [
            f"Seconde lecture sur images de pages : {code(pub.seconde_lecture.statut)} — "
            f"{pub.seconde_lecture.blocs_verifies}/{pub.seconde_lecture.blocs_planifies} bloc(s).",
            "",
        ]
    lignes += ["## Limites", ""]
    lignes += [f"- {valeur(limite)}" for limite in pub.limites] or [
        "- aucune limite dérivée de ce run."]
    return "\n".join(lignes).rstrip() + "\n"


def preparer_publication(pub: PublicationEvals, *, data_dir: Path, repo_root: Path,
                         preparer: Any, nom: str = PUBLICATION_JSON,
                         markdown_run: str | None = None,
                         chemin_run: Path | None = None,
                         valeur: Any = None, code: Any = None) -> list[tuple[Path, Path]]:
    """Écrit **toutes** les sorties de publication dans des temporaires, et rend `[(tmp, cible)]`.

    Story 4.5, revue B3. Le gate était persisté **avant** la publication : un échec d'écriture
    laissait un `evals_ok: true` déjà promu et immédiatement servable, avec des surfaces
    divergentes — le manifest disait « vert », et personne ne pouvait lire sur quoi.

    La séquence est donc celle-ci, et c'est la seule qui tienne (revue A) : tout est écrit et vidé
    sur disque ici, **puis** l'entrée de manifest est préparée de la même façon, **puis** chaque
    temporaire bascule par `os.replace` (atomique, même système de fichiers, sur un fichier déjà
    écrit). Tout ce qui peut échouer survient ainsi avant la **première** bascule : le manifest reste
    byte-identique, aucune publication partielle n'est visible, et il ne subsiste aucun état où les
    surfaces affirment un verdict que le manifest ne porte pas.

    `preparer(cible, contenu) -> tmp` est la recette d'écriture temporaire de l'appelant ; une
    seconde recette ici laisserait un fichier à moitié écrit le jour où le disque est plein.
    """
    import json

    # **Tout ce qui peut lever est lu et décidé avant le premier temporaire** (revue B7). La lecture
    # de l'archive précédente venait après la création du temporaire de `data/evals-latest.json` :
    # quand elle levait `ArchivePrecedenteIllisible`, la fonction sortait sans rendre `a_basculer`,
    # l'appelant gardait une liste vide, et le temporaire échappait à son nettoyage — les refus
    # répétés polluaient `data/` jusqu'à remplir le disque.
    # **Le couple `markdown_run` / `chemin_run` est indivisible** (revue R11) : n'en fournir qu'un
    # faisait disparaître silencieusement de la bascule la surface que la CI concatène, sans qu'un
    # seul appelant l'apprenne. Une surface qu'on croit publier et qui ne l'est pas est pire qu'une
    # surface qu'on sait absente.
    if (markdown_run is None) != (chemin_run is None):
        raise ValueError(
            "preparer_publication : `markdown_run` et `chemin_run` vont ensemble — n'en donner "
            "qu'un retirerait de la bascule la surface que la CI concatène, en silence")
    rendu = rendre_publication_markdown(pub, valeur=valeur, code=code)
    markdown_path = repo_root.joinpath(*DOCS_LATEST)
    archive = _archive_a_ecrire(markdown_path, repo_root=repo_root)
    contenu_json = json.dumps(pub.model_dump(mode="json"), indent=2, ensure_ascii=False,
                              sort_keys=True) + "\n"
    a_basculer: list[tuple[Path, Path]] = []
    # **Et un rollback local par-dessus**, parce que « lire avant d'écrire » ferme la cause connue
    # mais pas la classe : `preparer` peut échouer à n'importe quel **rang** (disque plein sur le
    # deuxième, permission sur le dernier) et laisserait alors derrière lui les temporaires déjà
    # accumulés. La garantie tenue est donc : sur **toute** exception de préparation, zéro
    # temporaire résiduel et zéro cible modifiée.
    try:
        a_basculer.append((preparer(data_dir / nom, contenu_json), data_dir / nom))
        if archive is not None:
            chemin_archive, contenu_archive = archive
            a_basculer.append((preparer(chemin_archive, contenu_archive), chemin_archive))
        a_basculer.append((preparer(markdown_path, rendu), markdown_path))
        if markdown_run is not None and chemin_run is not None:
            # Le journal du run **et** l'artefact publié, dans le fichier que la CI concatène : un
            # seul renderer, une seule bascule.
            a_basculer.append((preparer(chemin_run, markdown_run), chemin_run))
    except BaseException:
        supprimer_temporaires(a_basculer)
        raise
    return a_basculer


def supprimer_temporaires(prepares: list[tuple[Path, Path]]) -> None:
    """Supprime des temporaires préparés mais jamais basculés — aucune cible n'est touchée.

    Le pendant local de `run._abandonner`, présent ici pour que `preparer_publication` puisse
    défaire **sa propre** préparation sans dépendre de son appelant : c'est précisément parce que le
    nettoyage était délégué à l'appelant, qui n'avait encore rien reçu, que le temporaire de la
    revue B7 devenait inaccessible.
    """
    for temporaire, _cible in prepares:
        try:
            os.unlink(temporaire)
        except OSError:
            continue


def _archive_a_ecrire(markdown_path: Path, *, repo_root: Path) -> tuple[Path, str] | None:
    """Décide **sans rien écrire** ce que l'archivage du `latest.md` existant doit produire.

    Rend `(chemin de l'archive, contenu)`, ou `None` s'il n'y a rien à archiver — fichier absent,
    vide, ou déjà archivé à l'identique. Lève `ArchivePrecedenteIllisible` si le rendu précédent
    existe mais ne se lit pas.

    C'est **l'unique** autorité de cette décision : `preparer_publication` et `archiver_latest` en
    dérivent tous deux, et la séparer de l'écriture est ce qui permet à la première de lever avant
    d'avoir créé le moindre temporaire (revue B7).

    Story 4.5, revue B3/B6 (chemin frère, et plus grave que le défaut lui-même) : `except OSError:
    return None` disait « rien à archiver » quand la vraie réponse était « je n'ai pas pu lire ». Le
    rendu précédent n'était alors pas archivé **puis écrasé** par le nouveau — et ce chemin détruit
    même lorsque la bascule réussit. Seule l'absence est une absence.
    """
    import datetime

    try:
        octets = markdown_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ArchivePrecedenteIllisible(
            f"{markdown_path} : le rendu précédent n'a pas pu être lu ({type(exc).__name__}) — il "
            "serait écrasé sans avoir été archivé") from exc
    if not octets.strip():
        return None
    try:
        horodatage = datetime.datetime.fromtimestamp(
            markdown_path.stat().st_mtime, tz=datetime.UTC).strftime("%Y%m%d")
    except OSError as exc:
        raise ArchivePrecedenteIllisible(
            f"{markdown_path} : l'horodatage du rendu précédent n'a pas pu être lu "
            f"({type(exc).__name__}) — son archive ne peut pas être nommée") from exc
    empreinte = hashlib.sha256(octets).hexdigest()[:12]
    archive = repo_root.joinpath(*DOCS_ARCHIVES) / f"{horodatage}-{empreinte}.md"
    if archive.is_file():
        return None
    return archive, octets.decode("utf-8", errors="replace")


# `ecrire_publication` a été **supprimée** (revue R6). Elle n'avait aucun appelant de production —
# seulement deux tests —, mais l'en-tête du module la présentait comme « l'étape 3 » et elle
# enchaînait trois écritures indépendantes sans rollback : un échec sur la troisième laissait
# `data/evals-latest.json` porter le nouveau verdict pendant que `docs/evals/latest.md` restait sur
# l'ancien rendu — « une surface affirme un verdict que l'autre ne porte pas », le défaut même que
# les revues B3 et B7 ont fermé sur `preparer_publication`. La spec interdit d'ouvrir une seconde
# plomberie ; les tests qui l'employaient passent désormais par l'écrivain de production.


def archiver_latest(markdown_path: Path, *, repo_root: Path, ecrire: Any) -> Path | None:
    """Archive le `latest.md` **existant** avant de le remplacer.

    Rend le chemin de l'archive **écrite**, ou `None` quand il n'y avait rien à écrire : fichier
    absent, vide, ou déjà archivé à l'identique.

    Sans cela, le premier gate `full` écrasait sans retour le registre manuel de la campagne 4.2d —
    celui que la story 4.4 référence, et qui contient des mesures live que personne ne peut
    reproduire sans repayer. « `latest` » veut dire « le dernier », pas « le seul » : un journal de
    campagnes qui perd les précédentes ne prouve plus rien sur la durée.

    Le nom de l'archive est dérivé du **contenu remplacé**, jamais d'une horloge : sa date de
    modification et l'empreinte courte de ses octets. Deux archivages du même contenu retombent donc
    sur le même fichier au lieu d'en accumuler des copies, et l'ordre chronologique reste lisible
    dans le nom. Rien n'est archivé si le fichier n'existe pas, ou s'il est déjà archivé à
    l'identique.

    La décision — quoi archiver, sous quel nom — vient de `_archive_a_ecrire`, la **même** autorité
    que `preparer_publication` : deux recettes de nommage auraient fini par diverger, et l'une des
    deux aurait alors écrasé ce que l'autre croyait avoir archivé.
    """
    a_ecrire = _archive_a_ecrire(markdown_path, repo_root=repo_root)
    if a_ecrire is None:
        return None
    archive, contenu = a_ecrire
    ecrire(archive, contenu)
    return archive


def digest_octets(path: Path) -> str:
    """sha256 des octets **réellement écrits** — l'empreinte que la publication référence."""
    return hashlib.sha256(path.read_bytes()).hexdigest()
