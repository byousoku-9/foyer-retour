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
3. `preparer_publication(...)` — **l'unique** constructeur du lot : il rend `[(cible, contenu)]` et
   **n'écrit rien**. C'est `server/evals/espace.py` qui l'écrit, avec l'entrée de manifest, dans une
   génération inactive, puis publie le tout par un unique `os.replace` du pointeur (story 4.5, B7).
   Il n'y a pas de second constructeur « plus simple » (revue R6) : une seconde plomberie sans la
   garantie tout-ou-rien finit par laisser une surface affirmer un verdict que l'autre ne porte
   pas — c'est précisément ce que les revues B3 et B7 ont eu à fermer sur celle-ci.

**La publication est inconditionnelle.** Un run rouge est publié avec ses limites : publier ne promeut
rien, et seul `gate.evals_ok` décide de ce qui est servi (AD-8). Un artefact qui n'apparaîtrait que
lorsque tout est vert ferait de l'absence de nouvelle une bonne nouvelle.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any

from server.app.config import EVALS_PUBLICATION_FILE
from server.app.domain.evals import (LABELS, PROFILS_LIVRES, SUITES, CoutPublie, LatencePubliee,
                                     PublicationEvals, ReservesPubliees, SecondeLecturePubliee,
                                     StabilitePubliee)
from server.app.domain.ingest import Gate, GateDecision
from server.evals.plancher import PreuveExterneVerifiee

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
    "cost_eur_original": (0.0, None),
    "latency_p50_ms": (0, None),
    "latency_p95_ms": (0, None),
    "latency_ms": (0, None),
    # Une décision se lit contre son plancher : les deux vivent dans `[0, 1]` comme les témoins.
    "threshold": (0.0, 1.0),
    "value": (0.0, 1.0),
    # `n` était le **seul** champ de `CHAMPS_DECISION` sans domaine (revue P3) : `n = -5` passait la
    # validation canonique, puis sortait en `ValidationError` pydantic nue à la construction de
    # `GateDecision` — donc en « incident de rapport », code 3, alors que le défaut est dans les
    # données. C'est la ligne de partage d'AD-8, franchie dans le sens que la spec interdit.
    "n": (0, None),
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
# Les huit champs de `GateDecision` que les rendus lisent, et la forme exigée avant que pydantic
# n'ait la moindre occasion de **coercer** (revue B1/B5, tour correctif 1/3) : `threshold`, `n` et
# `value` en chaînes étaient convertis en silence, et le refus dit s'arrêtait donc au type de
# l'entrée. `reason` est facultatif (`None` quand le statut se lit sur la valeur).
CHAMPS_DECISION: tuple[tuple[str, str], ...] = (
    ("metric", "texte"), ("producer", "texte"), ("scope", "texte"),
    ("threshold", "reel"), ("value", "reel"), ("n", "entier"),
    ("run_digest", "empreinte"), ("status", "texte"),
)
STATUTS_DECISION: frozenset[str] = frozenset({"green", "red"})
# Les clés qu'une décision peut porter, **exactement** : les huit obligatoires plus `reason`, qui
# vaut `None` quand le statut se lit sur la valeur. `GateDecision` est `extra="forbid"`, si bien
# qu'une clé en trop y levait une `ValidationError` nue — donc un code 3 (revue P3). `decisions[i]`
# était la seule structure fermée du module contrôlée sans égalité d'ensemble, alors que
# `metrics.labels`, `CLES_PREUVE_TRUSTED`, `CandidatClassement` et `Configuration` le sont tous.
CLES_DECISION: frozenset[str] = frozenset({champ for champ, _forme in CHAMPS_DECISION} | {"reason"})
# Le vocabulaire de `--producer` : la règle trusted ne reconnaît que l'orchestrateur comme
# producteur de preuve, un run de builder est un diagnostic. Il n'y en a pas de troisième.
PRODUCTEURS_DECISION: frozenset[str] = frozenset({"builder", "orchestrator"})


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


def valider_rapport_publiable(rapport: Any, *,
                              preuve_externe: PreuveExterneVerifiee | None) -> dict[str, Any]:
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

    `preuve_externe` est **obligatoire à l'appel** et n'a pas de valeur par défaut (revue B5, tour
    correctif 2/3). C'est la seule chose qui rende publiable une décision portant l'empreinte d'un
    *autre* run : celle que `plancher.verifier_liaison_preuve` a réellement établie sur des octets.
    `None` ne veut pas dire « pas de contrainte » mais « aucune preuve externe n'a été vérifiée,
    donc aucune empreinte étrangère n'est publiable » — un chemin de diagnostic qui n'a légitimement
    aucune preuve externe n'a pas non plus de décision externe à publier. Un paramètre par défaut
    vide aurait replacé l'auto-déclaration un niveau plus haut, ce que la spec interdit nommément.

    Rend le rapport tel quel (pour chaîner), ou lève `RapportInexploitable` en nommant la clé
    fautive et sa raison typée.
    """
    if not isinstance(rapport, dict):
        raise RapportInexploitable(
            f"rapport : un objet est attendu ({type(rapport).__name__} reçu)")
    # **Le vocabulaire autoritaire, pas « une chaîne non vide »** (revue B5, tour correctif 2/3) :
    # `profile='hors-domaine'` était publié tel quel sur les quatre surfaces. `PROFILS_LIVRES` vit
    # dans `domain`, d'où le runner le lit aussi — recopier le littéral ici aurait fait deux
    # autorités.
    _texte(rapport, "profile", chemin="rapport", vocabulaire=PROFILS_LIVRES)
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
        if type_attendu is not None and (isinstance(rapport[champ], bool)
                                         or not isinstance(rapport[champ], type_attendu)):
            raise RapportInexploitable(
                f"rapport : {champ!r} doit être {type_attendu.__name__} "
                f"({type(rapport[champ]).__name__} reçu)")
    # **Les compteurs racine sont dans leur domaine et cohérents entre eux** (revue B5, tour
    # correctif 1/3). `cases_completed > cases_planned` décrirait un run ayant terminé plus de cas
    # qu'il n'en a planifié : le journal l'imprimait tel quel, `2/1`, sans que rien ne le voie.
    for champ in ("cases_planned", "cases_completed"):
        if rapport[champ] < 0:
            raise RapportInexploitable(
                f"rapport : {champ!r} vaut {rapport[champ]}, hors du domaine (>= 0)")
    if rapport["cases_completed"] > rapport["cases_planned"]:
        raise RapportInexploitable(
            f"rapport : 'cases_completed' ({rapport['cases_completed']}) dépasse 'cases_planned' "
            f"({rapport['cases_planned']}) — un run ne termine pas plus de cas qu'il n'en planifie")
    if not _est_empreinte(rapport["cases_hash"], 64):
        raise RapportInexploitable(
            f"rapport : 'cases_hash' doit être 64 caractères hexadécimaux "
            f"({rapport['cases_hash']!r} reçu) — c'est l'identité du lot mesuré, pas une étiquette")
    if rapport["stop_reason"] is not None and not (isinstance(rapport["stop_reason"], str)
                                                   and rapport["stop_reason"]):
        raise RapportInexploitable(
            f"rapport : 'stop_reason' doit être une chaîne non vide ou null "
            f"({rapport['stop_reason']!r} reçu)")
    for rang, execution in enumerate(rapport["unexecuted_cases"]):
        if not isinstance(execution, str) or not execution:
            raise RapportInexploitable(
                f"rapport : unexecuted_cases[{rang}] doit être un identifiant non vide "
                f"({execution!r} reçu) — les limites publiées les énumèrent")
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
            if not isinstance(nom, str) or not nom:
                raise RapportInexploitable(
                    f"rapport : metrics.{champ} porte une clé vide ou non textuelle ({nom!r})")
            if isinstance(compte, bool) or not isinstance(compte, int) or compte < 0:
                raise RapportInexploitable(
                    f"rapport : metrics.{champ}[{nom!r}] doit être un entier >= 0 "
                    f"({compte!r} reçu)")
    # **Le vocabulaire fixe d'AD-14 est la table entière**, ni plus ni moins : le journal de CI
    # indexe `metrics.labels[label]` sur **les sept** — un label manquant y levait un `KeyError` nu
    # —, et une clé **en trop** serait un huitième label publié sans en être un (AD-14 : « labels
    # fixes »). Un vocabulaire fermé se contrôle par égalité, comme `Temoin` et les clés de preuve.
    if set(metrics["labels"]) != set(LABELS):
        manquants = sorted(set(LABELS) - set(metrics["labels"]))
        superflus = sorted(set(metrics["labels"]) - set(LABELS))
        raise RapportInexploitable(
            "rapport : metrics.labels n'est pas le vocabulaire fixe d'AD-14"
            + (f" — manquants : {manquants}" if manquants else "")
            + (f" — en trop : {superflus}" if superflus else "")
            + " ; un zéro absent n'est pas un zéro observé, et un label inconnu n'est pas un label")
    _valider_resultats(rapport["results"])
    _valider_stabilite(rapport)
    _valider_decisions(rapport, preuve_externe)
    _valider_reserves(rapport)
    return rapport


def _texte(conteneur: dict[str, Any], cle: str, *, chemin: str,
           vocabulaire: frozenset[str] | tuple[str, ...] | None = None) -> str:
    """Une chaîne **présente, non vide, et dans son vocabulaire quand il est fermé**.

    Story 4.5, tour correctif 1/3, revue B5. Le tour précédent avait fermé le *type* — « c'est bien
    une chaîne » — et laissé le **domaine** libre : un `label` hors des sept d'AD-14 et un `suite`
    hors des trois livrées passaient, et le journal que la CI concatène les imprimait littéralement.
    Un domaine de valeur n'est pas un type, et une table de comptage indexée sur un vocabulaire fixe
    ne se lit pas avec des valeurs qui n'en font pas partie.
    """
    if cle not in conteneur:
        raise RapportInexploitable(
            f"rapport : {chemin}.{cle} est absent — le journal du run l'indexe")
    valeur = conteneur[cle]
    if not isinstance(valeur, str):
        raise RapportInexploitable(
            f"rapport : {chemin}.{cle} doit être une chaîne ({type(valeur).__name__} reçu)")
    if not valeur:
        raise RapportInexploitable(
            f"rapport : {chemin}.{cle} est vide — une identité vide ne désigne rien")
    if vocabulaire is not None and valeur not in vocabulaire:
        raise RapportInexploitable(
            f"rapport : {chemin}.{cle} vaut {valeur!r}, hors du vocabulaire fixe "
            f"{sorted(vocabulaire)} — publier une valeur inconnue la présenterait comme mesurée")
    return valeur


def _booleen(conteneur: dict[str, Any], cle: str, *, chemin: str) -> bool:
    """Un booléen **présent et vraiment booléen** — `"oui"` n'est pas `True`.

    Revue B5, tour correctif 1/3 : `stability.cases[x] = {"stable": "oui", "comptabilise": "oui"}`
    produisait une stabilité **1/1**, parce que la vérité de Python sur une chaîne non vide n'est
    pas une mesure. Une stabilité fabriquée est le pire des chiffres publiés : elle dit « ce run est
    reproductible » sans que rien ne l'ait été.
    """
    if cle not in conteneur:
        raise RapportInexploitable(
            f"rapport : {chemin}.{cle} est absent — un cas dont on ne sait pas s'il est stable ne "
            "se compte ni au numérateur ni au dénominateur")
    valeur = conteneur[cle]
    if not isinstance(valeur, bool):
        raise RapportInexploitable(
            f"rapport : {chemin}.{cle} doit être un booléen ({valeur!r} reçu) — une chaîne non vide "
            "est vraie en Python, jamais dans une mesure")
    return valeur


def _empreinte_exigee(conteneur: dict[str, Any], cle: str, *, chemin: str, longueur: int) -> str:
    """Une empreinte **présente et bien formée** — sinon un refus qui la nomme."""
    valeur = conteneur.get(cle)
    if not _est_empreinte(valeur, longueur):
        raise RapportInexploitable(
            f"rapport : {chemin}.{cle} doit être {longueur} caractères hexadécimaux "
            f"({valeur!r} reçu) — une chaîne qui ressemble à une empreinte n'en est pas une")
    return str(valeur)


def _valider_resultats(resultats: list[Any]) -> None:
    """Chaque exécution porte les clés que le journal du run indexe, **dans leur domaine**.

    Revue R1 pour la présence des sept clés ; revue B5 du tour correctif 1/3 pour leur **valeur** :
    `suite` et `label` dans leurs vocabulaires littéraux, `id` et `variant` non vides, coûts finis
    `>= 0` et latence entière `>= 0`. Sans ce dernier point, le journal de CI publiait littéralement
    `| cas-1 | vertical | v | LABEL_INCONNU | -12.5000 | -9.0000 | -7 |` — quatre valeurs
    impossibles, rendues comme des mesures.
    """
    vocabulaires: dict[str, tuple[str, ...]] = {"suite": SUITES, "label": LABELS}
    for rang, resultat in enumerate(resultats):
        chemin = f"results[{rang}]"
        if not isinstance(resultat, dict):
            raise RapportInexploitable(
                f"rapport : {chemin} doit être un objet ({type(resultat).__name__} reçu)")
        for champ, forme in CHAMPS_RESULTAT:
            if forme == "texte":
                _texte(resultat, champ, chemin=chemin, vocabulaire=vocabulaires.get(champ))
            else:
                _dans_le_domaine(
                    _mesure(resultat, champ, chemin=chemin, entier=(forme == "entier")),
                    champ, chemin=chemin)


def _valider_stabilite(rapport: dict[str, Any]) -> None:
    """`stability` absent est légitime ; `stability` présent et creux ne l'est pas (revue B5).

    Le `repeat` du rapport est exigé **dans les deux cas** : c'est le N d'un run sans agrégat, et
    c'est le dénominateur qu'un agrégat doit retrouver. `stability.n != repeat` est un rapport qui
    se contredit lui-même — la stabilité y serait mesurée sur un nombre de répétitions que le run
    n'a pas planifié.
    """
    repeat = _exiger(rapport, "repeat", (int,))
    if isinstance(repeat, bool) or repeat < 1:
        raise RapportInexploitable(
            f"rapport : 'repeat' doit être un entier >= 1 ({repeat!r} reçu)")
    agregat = rapport.get("stability")
    if agregat is None and "stability" not in rapport:
        # Un run sans répétition n'écrit pas d'agrégat : le `repeat` du rapport donne alors le N.
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
    if not cases:
        # Le tour précédent avait fermé l'**absence** de `cases` et laissé le **vide** ouvert
        # (revue P4) : `{"n": 3, "cases": {}}` publiait `0/0`, le chiffre fabriqué exact que ce
        # module refuse. Un agrégat de stabilité n'est écrit que sous `repeat > 1`, et
        # `agreger_stabilite` y range une entrée par cas : sans aucune entrée, il n'y a pas
        # d'agrégat, il y a un agrégat qu'on n'a pas su écrire.
        raise RapportInexploitable(
            "rapport : 'stability.cases' est vide — publier « 0/0 » dirait « aucun cas n'était "
            "stable » là où le rapport n'a mesuré aucun cas du tout")
    for case_id, detail in cases.items():
        if not isinstance(detail, dict):
            raise RapportInexploitable(
                f"rapport : stability.cases[{case_id!r}] doit être un objet "
                f"({type(detail).__name__} reçu)")
        # **Les deux drapeaux que le numérateur et le dénominateur lisent**, en booléens vrais.
        _booleen(detail, "stable", chemin=f"stability.cases[{case_id!r}]")
        _booleen(detail, "comptabilise", chemin=f"stability.cases[{case_id!r}]")
    n = agregat.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise RapportInexploitable(
            f"rapport : 'stability.n' doit être un entier >= 1 ({n!r} reçu) — le N d'une stabilité "
            "ne se devine pas")
    if n != repeat:
        raise RapportInexploitable(
            f"rapport : 'stability.n' vaut {n} alors que 'repeat' vaut {repeat} — la stabilité "
            "serait publiée sur un nombre de répétitions que le run n'a pas planifié")


def _valider_decisions(rapport: dict[str, Any],
                       preuve_externe: PreuveExterneVerifiee | None) -> None:
    """Des décisions non vides disent contre quels seuils elles ont été prises, et **avec quoi**.

    Revue B5 pour le `plancher_digest` racine ; revue R7 pour « chaque entrée est un objet » ; revue
    B5 du tour correctif 1/3 pour le reste : `threshold`, `n` et `value` en chaînes étaient coercés
    par pydantic à la construction de `GateDecision`, et le `run_digest` d'une décision n'était
    opposé à rien.

    Sur le `run_digest` d'une décision, l'ancrage vient **d'ailleurs que du rapport** (revue B5,
    tour correctif 2/3). Le tour précédent admettait une empreinte étrangère dès que le rapport
    l'inscrivait lui-même dans `external_run_digests` avec `producer='orchestrator'` : la liste des
    empreintes « légitimes » était donc lue dans l'entrée non fiable qu'on est en train de valider,
    et `'f'*64` passait. Une liste auto-déclarée par ce qu'on valide ne peut jamais être son propre
    ancrage de confiance — c'était le volet initial « run_digest arbitraire non opposé », déplacé
    d'un cran.

    L'ancrage est donc `preuve_externe`, l'objet que `plancher.verifier_liaison_preuve` a établi sur
    les **octets** de la preuve et du rapport qu'elle référence, avant qu'aucune décision externe
    n'existe. Les règles :

    - une décision porte l'empreinte de ce run, ou celle qu'une **preuve vérifiée** établit ;
    - sans preuve vérifiée, aucune empreinte étrangère n'est publiable — un chemin de diagnostic
      n'ayant légitimement aucune preuve externe n'a pas non plus de décision externe à publier ;
    - une empreinte étrangère n'est admise que sur une décision `producer="orchestrator"` : le
      runner n'écrit jamais autre chose que sa propre empreinte pour ses propres mesures ;
    - `external_run_digests`, que le rapport écrit pour rester lisible hors contexte, doit
      **concorder** avec la preuve : ce que le rapport déclare et ce que la preuve établit sont deux
      choses, et leur écart est un refus. Le rapport ne décide plus, il est confronté.
    """
    identite = rapport.get("identity")
    propre = (identite or {}).get("run_digest") if isinstance(identite, dict) else None
    # **Validé inconditionnellement** (revue P5, chemin frère) : le retour anticipé « pas de
    # décisions » sautait ce contrôle, si bien qu'un `external_run_digests` mal formé passait dès
    # que le rapport n'avait aucune décision.
    declarees = rapport.get("external_run_digests", [])
    if not isinstance(declarees, list) or any(not _est_empreinte(d, 64) for d in declarees):
        raise RapportInexploitable(
            f"rapport : 'external_run_digests' doit être une liste d'empreintes 64 hexadécimales "
            f"({declarees!r} reçu)")
    # **Les empreintes admises viennent de la preuve, pas du rapport.** La déclaration du rapport
    # n'est plus qu'une affirmation à confronter.
    externes = preuve_externe.run_digests if preuve_externe is not None else frozenset()
    ecart = sorted(set(map(str, declarees)) - externes)
    if ecart:
        raise RapportInexploitable(
            f"rapport : 'external_run_digests' déclare {ecart} qu'aucune preuve externe vérifiée "
            "n'établit — une liste écrite par le rapport qu'on valide ne peut pas être son propre "
            "ancrage de confiance")
    if rapport.get("plancher_digest") is not None:
        decisions = _exiger(rapport, "decisions", (list,))
    else:
        decisions = rapport.get("decisions")
        if decisions is None:
            decisions = []
        if not isinstance(decisions, list):
            raise RapportInexploitable(
                f"rapport : 'decisions' doit être une liste ({type(decisions).__name__} reçu)")
        if decisions:
            raise RapportInexploitable(
                "rapport : 'decisions' est non vide alors que 'plancher_digest' est absent de la "
                "racine — une décision qui ne nomme pas son protocole ne dit pas contre quel seuil "
                "elle a été prise, et la publier la présenterait comme opposable")
    if decisions and not _est_empreinte(rapport.get("plancher_digest"), 64):
        raise RapportInexploitable(
            f"rapport : 'plancher_digest' doit être 64 caractères hexadécimaux "
            f"({rapport.get('plancher_digest')!r} reçu)")
    etrangeres_portees: set[str] = set()
    for rang, decision in enumerate(decisions):
        chemin = f"decisions[{rang}]"
        if not isinstance(decision, dict):
            raise RapportInexploitable(
                f"rapport : {chemin} doit être un objet ({type(decision).__name__} reçu)")
        superflues = sorted(set(decision) - CLES_DECISION)
        if superflues:
            raise RapportInexploitable(
                f"rapport : {chemin} porte des clés inconnues {superflues} — le vocabulaire d'une "
                f"décision est fermé ({sorted(CLES_DECISION)}), et une clé en trop sortirait en "
                "ValidationError nue plutôt qu'en refus dit")
        for champ, forme in CHAMPS_DECISION:
            if forme == "texte":
                vocabulaire = {"status": STATUTS_DECISION,
                               "producer": PRODUCTEURS_DECISION}.get(champ)
                _texte(decision, champ, chemin=chemin, vocabulaire=vocabulaire)
            elif forme == "empreinte":
                _empreinte_exigee(decision, champ, chemin=chemin, longueur=64)
            else:
                _dans_le_domaine(
                    _mesure(decision, champ, chemin=chemin, entier=(forme == "entier")),
                    champ, chemin=chemin)
        if "reason" in decision and not isinstance(decision["reason"], (str, type(None))):
            raise RapportInexploitable(
                f"rapport : {chemin}.reason doit être une chaîne ou null "
                f"({type(decision['reason']).__name__} reçu)")
        if decision["run_digest"] == propre:
            continue
        if decision["run_digest"] not in externes:
            raise RapportInexploitable(
                f"rapport : {chemin}.run_digest vaut {decision['run_digest']!r} — ce n'est ni "
                f"l'empreinte de ce run ({propre!r}), ni celle qu'une preuve externe vérifiée "
                "établit : cette décision n'est opposée à aucun run")
        if decision["producer"] != "orchestrator":
            raise RapportInexploitable(
                f"rapport : {chemin} porte l'empreinte d'un autre run alors que son producteur est "
                f"{decision['producer']!r} — seule une mesure venue d'une preuve orchestrateur peut "
                "avoir été prise ailleurs que dans ce run")
        etrangeres_portees.add(str(decision["run_digest"]))
    orphelines = sorted(set(map(str, declarees)) - etrangeres_portees)
    if orphelines:
        raise RapportInexploitable(
            f"rapport : 'external_run_digests' déclare {orphelines} qu'aucune décision ne porte — "
            "une empreinte déclarée sans usage n'est pas une donnée, c'est une porte ouverte")


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


def _est_empreinte(valeur: Any, longueur: int = 64) -> bool:
    """`valeur` est-elle exactement `longueur` caractères hexadécimaux minuscules ?"""
    return (isinstance(valeur, str) and len(valeur) == longueur
            and all(c in _HEX for c in valeur))


def _empreinte(valeur: Any, longueur: int = 64) -> str | None:
    """L'empreinte, ou `None` **si et seulement si** elle est absente — jamais un repli silencieux.

    Story 4.5, tour correctif 1/3, revue B5. La version précédente rabattait sur `None` une valeur
    *présente mais invalide* : un `cases_hash` bricolé à la main s'affichait `—` sur les quatre
    surfaces, c'est-à-dire exactement comme un rapport qui n'en porte pas. C'est la faute que tout
    ce cycle ferme, sous sa dernière forme : « je n'ai pas su la lire » rendu comme « il n'y en
    avait pas ».

    Absent (`None`, ou chaîne vide côté gate) reste une absence — un diagnostic n'a ni révision
    candidate ni rapport certifié, et l'inventer serait pire. Présent et mal formé est un
    `RapportInexploitable` qui nomme le champ.
    """
    if valeur is None or valeur == "":
        return None
    if not _est_empreinte(valeur, longueur):
        raise RapportInexploitable(
            f"publication : {valeur!r} n'est pas une empreinte de {longueur} caractères "
            "hexadécimaux — la publier telle quelle afficherait sur les quatre surfaces une "
            "identité qui n'en est pas une")
    return str(valeur)


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


def stabilite_du_rapport(rapport: dict[str, Any], *,
                         preuve_externe: PreuveExterneVerifiee | None) -> StabilitePubliee:
    """N/N depuis l'agrégat du run ; les cas `parsing` restent hors comptage, comme au plancher.

    `stability` n'est écrit que sous `repeat > 1` : **son absence est légitime** et se lit alors
    « aucun cas comptabilisé », avec le `repeat` du rapport pour N. Ce qui ne l'est pas, c'est de
    fabriquer `N=1` quand le rapport ne dit même pas combien de répétitions il a planifiées, ni de
    publier `0/0` pour un `stability` présent mais sans `cases` : les deux passent désormais par
    `valider_rapport_publiable` (revue B5), et se refusent avant toute surface.
    """
    valider_rapport_publiable(rapport, preuve_externe=preuve_externe)
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
                       seconde_lecture: SecondeLecturePubliee | None = None, *,
                       preuve_externe: PreuveExterneVerifiee | None) -> list[str]:
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
    valider_rapport_publiable(rapport, preuve_externe=preuve_externe)
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
                           candidate_revision: str | None = None,
                           preuve_externe: PreuveExterneVerifiee | None) -> PublicationEvals:
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
    valider_rapport_publiable(rapport, preuve_externe=preuve_externe)
    reserves = reserves if reserves is not None else _reserves_du_rapport(rapport)
    metrics = rapport["metrics"]
    decisions = [GateDecision.model_validate(d) for d in (rapport.get("decisions") or [])]
    limites = limites_du_rapport(rapport, decisions, reserves, relecture,
                                 preuve_externe=preuve_externe)
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
        stabilite=stabilite_du_rapport(rapport, preuve_externe=preuve_externe),
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
                         nom: str = PUBLICATION_JSON,
                         markdown_run: str | None = None,
                         chemin_run: Path | None = None,
                         valeur: Any = None, code: Any = None) -> list[tuple[Path, str]]:
    """Rend le lot de publication : `[(cible, contenu)]`, **sans écrire un seul octet**.

    Story 4.5, revue B3. Le gate était persisté **avant** la publication : un échec d'écriture
    laissait un `evals_ok: true` déjà promu et immédiatement servable, avec des surfaces
    divergentes — le manifest disait « vert », et personne ne pouvait lire sur quoi.

    Story 4.5, B7 : cette fonction **ne touche plus le disque du tout**. Elle rend le lot complet —
    l'artefact servi, l'archive de campagne s'il y en a une, le rendu lisible du dépôt, et le
    journal que la CI concatène — et c'est `EspacePublie.basculer` qui l'écrit dans une génération
    inactive puis le publie par un unique `os.replace`. Il n'y a donc plus ni temporaire à nettoyer,
    ni rang de préparation où un résidu pourrait rester : une exception ici n'a rien écrit.

    Tout ce qui peut lever — validation canonique du rapport, rendu, décision d'archivage — se
    produit **avant** que l'appelant ne remette le lot à la bascule, donc avant que la moindre
    surface ne bouge. C'était déjà l'intention ; c'est maintenant une propriété du type de retour.
    """
    import json

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
    lot: list[tuple[Path, str]] = [(data_dir / nom, contenu_json)]
    if archive is not None:
        lot.append(archive)
    lot.append((markdown_path, rendu))
    if markdown_run is not None and chemin_run is not None:
        # Le journal du run **et** l'artefact publié, dans le fichier que la CI concatène : un
        # seul renderer, une seule bascule.
        lot.append((chemin_run, markdown_run))
    return lot


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
