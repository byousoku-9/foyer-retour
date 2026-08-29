"""FR41 / FR42 — Construire, rendre et écrire l'artefact **unique** des résultats d'évals.

Trois fonctions, et la frontière entre elles est le point de la story :

1. `construire_publication(...)` — projette un rapport de run, son gate et les réserves du dépôt dans
   `PublicationEvals` (`server/app/domain/evals.py`). Les **limites** y sont *dérivées* du run —
   décisions rouges chiffrées, réserves à `false`, exécutions manquantes, écarts de parsing, état
   incomplet — et jamais rédigées : une phrase qu'aucun chiffre ne produit est une phrase qu'aucun
   chiffre ne peut démentir.
2. `rendre_publication_markdown(...)` — **un seul** rendu Markdown, réutilisé tel quel par
   `docs/evals/latest.md` et par le résumé que la CI concatène dans `$GITHUB_STEP_SUMMARY`. Deux
   rendus séparés auraient divergé au premier arrondi, et l'AC compare les quatre surfaces « à
   l'octet des chiffres près ».
3. `ecrire_publication(...)` — l'écriture atomique des deux fichiers : `data/evals-latest.json`
   (servi, copié par l'image Docker) et `docs/evals/latest.md` (lisible, hors image).

**La publication est inconditionnelle.** Un run rouge est publié avec ses limites : publier ne promeut
rien, et seul `gate.evals_ok` décide de ce qui est servi (AD-8). Un artefact qui n'apparaîtrait que
lorsque tout est vert ferait de l'absence de nouvelle une bonne nouvelle.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from server.app.config import EVALS_PUBLICATION_FILE
from server.app.domain.evals import (CoutPublie, LatencePubliee, PublicationEvals, ReservesPubliees,
                                     SecondeLecturePubliee, StabilitePubliee)
from server.app.domain.ingest import Gate, GateDecision

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


def _reserves_du_rapport(rapport: dict[str, Any]) -> ReservesPubliees | None:
    """Les réserves telles que le **rapport** les porte, ou `None` s'il ne les établit pas.

    Un run de diagnostic n'établit ni contresignature, ni validation, ni signature de dictionnaire :
    inventer leur état serait pire que de ne rien en dire (AD-16).
    """
    brut = rapport.get("reserves")
    if not isinstance(brut, dict) or any(
            not isinstance(brut.get(champ), bool)
            for champ in ("countersigned", "validated_by_expert", "dictionary_validated")):
        return None
    return ReservesPubliees.model_validate(brut)


def stabilite_du_rapport(rapport: dict[str, Any]) -> StabilitePubliee:
    """N/N depuis l'agrégat du run ; les cas `parsing` restent hors comptage, comme au plancher."""
    agregat = rapport.get("stability") or {}
    cases = agregat.get("cases") or {}
    comptabilises = [v for v in cases.values() if v.get("comptabilise")]
    return StabilitePubliee(
        n=int(agregat.get("n", rapport.get("repeat", 1)) or 1),
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
    if not rapport.get("complete", False):
        limites.append(
            "run incomplet : " + str(rapport.get("stop_reason") or "interruption non qualifiée"))
    non_executes = list(rapport.get("unexecuted_cases") or [])
    if non_executes:
        limites.append(
            f"{len(non_executes)} exécution(s) planifiée(s) non exécutée(s), rouges au "
            f"dénominateur : {', '.join(non_executes)}")
    ecarts_parsing = sorted({r["id"] for r in rapport.get("results", [])
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
    reserves = reserves if reserves is not None else _reserves_du_rapport(rapport)
    metrics = rapport.get("metrics") or {}
    decisions = [GateDecision.model_validate(d) for d in rapport.get("decisions") or []]
    limites = limites_du_rapport(rapport, decisions, reserves, relecture)
    identite = rapport.get("identity") or {}
    return PublicationEvals(
        profile=str(rapport.get("profile") or (gate.profile if gate else "")),
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
        variantes=dict(metrics.get("variants") or {}),
        labels=dict(metrics.get("labels") or {}),
        recall=float(metrics.get("recall") or 0.0),
        stabilite=stabilite_du_rapport(rapport),
        cout=CoutPublie(
            # Le gate désarme le cache sous `--repeat` : le coût du run **est** le coût froid.
            froid_eur=float(rapport.get("cost_eur") or 0.0),
            moyen_eur=float(metrics.get("average_cost_eur") or 0.0),
            p95_eur=float(metrics.get("cost_p95_eur") or 0.0)),
        latence=LatencePubliee(p50_ms=int(metrics.get("latency_p50_ms") or 0),
                               p95_ms=int(metrics.get("latency_p95_ms") or 0)),
        ne_tranche_pas_rate=float(metrics.get("ne_tranche_pas_rate") or 0.0),
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
    lignes += [
        "",
        "## Décisions du plancher",
        "",
        "| metric | producteur | scope | n | valeur | plancher | statut |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    lignes += [
        f"| {code(d.metric)} | {code(d.producer)} | {code(d.scope)} | {d.n} | "
        f"{nombre(d.value)} | {nombre(d.threshold)} | {code(d.status)} |"
        for d in sorted(pub.decisions, key=lambda d: d.metric)
    ] or [f"| — | — | — | 0 | {nombre(0.0)} | {nombre(0.0)} | — |"]
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

    rendu = rendre_publication_markdown(pub, valeur=valeur, code=code)
    a_basculer: list[tuple[Path, Path]] = [
        (preparer(data_dir / nom,
                  json.dumps(pub.model_dump(mode="json"), indent=2, ensure_ascii=False,
                             sort_keys=True) + "\n"),
         data_dir / nom),
    ]
    markdown_path = repo_root.joinpath(*DOCS_LATEST)
    archive = _preparer_archive(markdown_path, repo_root=repo_root, preparer=preparer)
    if archive is not None:
        a_basculer.append(archive)
    a_basculer.append((preparer(markdown_path, rendu), markdown_path))
    if markdown_run is not None and chemin_run is not None:
        # Le journal du run **et** l'artefact publié, dans le fichier que la CI concatène : un seul
        # renderer, une seule bascule.
        a_basculer.append((preparer(chemin_run, markdown_run), chemin_run))
    return a_basculer


def _preparer_archive(markdown_path: Path, *, repo_root: Path,
                      preparer: Any) -> tuple[Path, Path] | None:
    """Prépare l'archive du `latest.md` existant, ou `None` s'il n'y a rien à archiver."""
    import datetime

    try:
        octets = markdown_path.read_bytes()
    except OSError:
        return None
    if not octets.strip():
        return None
    horodatage = datetime.datetime.fromtimestamp(
        markdown_path.stat().st_mtime, tz=datetime.UTC).strftime("%Y%m%d")
    empreinte = hashlib.sha256(octets).hexdigest()[:12]
    archive = repo_root.joinpath(*DOCS_ARCHIVES) / f"{horodatage}-{empreinte}.md"
    if archive.is_file():
        return None
    return preparer(archive, octets.decode("utf-8", errors="replace")), archive


def ecrire_publication(pub: PublicationEvals, *, data_dir: Path, repo_root: Path,
                       ecrire: Any, nom: str = PUBLICATION_JSON,
                       valeur: Any = None, code: Any = None) -> tuple[Path, Path]:
    """Écrit les deux faces du même objet, atomiquement, et rend leurs chemins.

    `ecrire` est l'écrivain atomique du runner (`_ecrire_atomique`) : une seule recette d'écriture
    dans tout le projet, et pas une seconde qui laisserait un fichier à moitié écrit le jour où le
    disque est plein.

    `nom` est **le réglage que le lecteur lit** (`Settings.evals_publication_file`), pas seulement son
    défaut : un écrivain figé sur la constante et un lecteur sur le réglage auraient pu diverger, et
    la route serait restée `publie: false` pour toujours. `config` valide ce nom par un motif strict
    qui interdit tout séparateur de chemin — il ne peut donc pas désigner un fichier hors de `data/`.
    """
    import json

    json_path = data_dir / nom
    markdown_path = repo_root.joinpath(*DOCS_LATEST)
    archiver_latest(markdown_path, repo_root=repo_root, ecrire=ecrire)
    ecrire(json_path,
           json.dumps(pub.model_dump(mode="json"), indent=2, ensure_ascii=False,
                      sort_keys=True) + "\n")
    ecrire(markdown_path, rendre_publication_markdown(pub, valeur=valeur, code=code))
    return json_path, markdown_path


def archiver_latest(markdown_path: Path, *, repo_root: Path, ecrire: Any) -> Path | None:
    """Archive le `latest.md` **existant** avant de le remplacer, et rend le chemin de l'archive.

    Sans cela, le premier gate `full` écrasait sans retour le registre manuel de la campagne 4.2d —
    celui que la story 4.4 référence, et qui contient des mesures live que personne ne peut
    reproduire sans repayer. « `latest` » veut dire « le dernier », pas « le seul » : un journal de
    campagnes qui perd les précédentes ne prouve plus rien sur la durée.

    Le nom de l'archive est dérivé du **contenu remplacé**, jamais d'une horloge : sa date de
    modification et l'empreinte courte de ses octets. Deux archivages du même contenu retombent donc
    sur le même fichier au lieu d'en accumuler des copies, et l'ordre chronologique reste lisible
    dans le nom. Rien n'est archivé si le fichier n'existe pas, ou s'il est déjà archivé à
    l'identique.
    """
    import datetime

    try:
        octets = markdown_path.read_bytes()
    except OSError:
        return None
    if not octets.strip():
        return None
    horodatage = datetime.datetime.fromtimestamp(
        markdown_path.stat().st_mtime, tz=datetime.UTC).strftime("%Y%m%d")
    empreinte = hashlib.sha256(octets).hexdigest()[:12]
    archive = repo_root.joinpath(*DOCS_ARCHIVES) / f"{horodatage}-{empreinte}.md"
    if archive.is_file():
        return archive
    ecrire(archive, octets.decode("utf-8", errors="replace"))
    return archive


def digest_octets(path: Path) -> str:
    """sha256 des octets **réellement écrits** — l'empreinte que la publication référence."""
    return hashlib.sha256(path.read_bytes()).hexdigest()
