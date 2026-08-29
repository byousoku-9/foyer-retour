"""FR47 — La seconde lecture : le **plan** déterministe et le **contrôle** du verdict rempli.

Ce que ce module fait, et ce qu'il refuse délibérément de faire.

*Il fait* deux choses, toutes deux hors réseau et sans clé :

1. `plan_de_relecture(...)` — depuis le corpus **servi**, pour une liste de blocs clés, il produit un
   plan : par bloc, `{doc_id, block_id, page, bbox, text_norm, image_url}`. `image_url` est la route
   de rendu de page livrée par la story 3.4 (`GET /api/v1/documents/{doc_id}/pages/{page}.png?
   blocks={block_id}`), avec son surlignage sur les `Line.bbox` exactes.
2. `valider_verdict(...)` — il **contrôle** le verdict que le second lecteur a rempli : le schéma
   exact, la couverture du plan bloc à bloc, l'empreinte du plan (`plan_digest`), la révision
   candidate, et un `image_sha256` par bloc relu. Un verdict qui ne concorde pas est refusé, jamais
   arrondi.

*Il ne fait pas* la rasterisation. Aucun `source.pdf` n'est présent dans ce worktree, la route de
3.4 existe déjà avec sa vérification de source, et dupliquer PyMuPDF sous `server/evals/` ajouterait
une seconde recette de rendu — non testable sur le corpus réel — tout en faisant dépendre `evals` de
la couche `api`, ce que la table du spine interdit. Il n'appelle non plus aucun modèle : le second
lecteur est humain (ou orchestré), et son verdict entre ici par un fichier.

La division est celle de la règle trusted : le builder livre le **format** et son **contrôle** ;
l'orchestrateur produit le **verdict**.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from server.app.domain.evals import REVISION, SHA256
from server.evals.cache import empreinte_canonique

ROUTE_PAGE = "/api/v1/documents/{doc_id}/pages/{page}.png?blocks={block_id}"


class RelectureInvalide(Exception):
    """Le plan ou le verdict ne tient pas : refus, jamais une approximation."""


class BlocRelu(BaseModel):
    """Une entrée du plan : tout ce dont le second lecteur a besoin, et rien de plus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: str = Field(min_length=1)
    block_id: str = Field(min_length=1)
    page: int = Field(ge=1)
    bbox: list[float] = Field(min_length=4, max_length=4)
    text_norm: str
    image_url: str = Field(min_length=1)


class PlanRelecture(BaseModel):
    """Le plan complet et son empreinte : le verdict s'y adosse, il ne s'en écarte pas."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    candidate_revision: str = Field(pattern=REVISION)
    blocs: list[BlocRelu] = Field(default_factory=list)

    @property
    def plan_digest(self) -> str:
        return empreinte_canonique(self.model_dump(mode="json"))


class VerdictBloc(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    block_id: str = Field(min_length=1)
    verdict: Literal["concordant", "divergent"]
    image_sha256: str = Field(pattern=SHA256)
    note: str = ""


class VerdictRelecture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    candidate_revision: str = Field(pattern=REVISION)
    plan_digest: str = Field(pattern=SHA256)
    verdicts: list[VerdictBloc] = Field(default_factory=list)

    @property
    def concordant(self) -> bool:
        return bool(self.verdicts) and all(v.verdict == "concordant" for v in self.verdicts)


def blocs_cles_du_rapport(rapport: dict[str, Any]) -> list[str]:
    """Les **blocs clés** d'un run : ceux que ses preuves citent, plus ceux qu'il attendait.

    C'est l'unique définition, partagée par le plan que la CLI écrit et par l'état de seconde lecture
    que la publication chiffre : deux notions séparées auraient produit un plan de N blocs et un
    `blocs_planifies` de M, et personne n'aurait su lequel des deux croire.

    Les blocs attendus non ouverts en font partie : ce sont précisément ceux dont on veut savoir si
    l'extraction les a rendus fidèlement — un bloc attendu que le rappel n'a pas présenté au modèle
    est le premier suspect d'un défaut de parsing.
    """
    blocs: set[str] = set()
    for resultat in rapport.get("results") or []:
        if not isinstance(resultat, dict):
            continue
        for preuve in resultat.get("proofs") or []:
            if isinstance(preuve, dict) and isinstance(preuve.get("block_id"), str):
                blocs.add(preuve["block_id"])
        for block_id in resultat.get("expected_blocks_not_opened") or []:
            if isinstance(block_id, str):
                blocs.add(block_id)
    return sorted(blocs)


def plan_de_relecture(index: Any, block_ids: list[str], *, candidate_revision: str) -> PlanRelecture:
    """Le plan des blocs clés, tel que le corpus servi les décrit — trié, sans doublon.

    Un `block_id` que le corpus ne sert pas, ou dont l'ingestion n'a retenu ni page ni bbox, n'entre
    **pas** dans le plan : on ne peut pas demander à quelqu'un de relire une image qui n'existe pas.
    Ce qui manque se lit à la longueur du plan, pas à une entrée fabriquée.
    """
    vus: dict[str, BlocRelu] = {}
    for block_id in sorted(set(block_ids)):
        try:
            doc_id = index.doc_of(block_id)
            bloc = index.corpus.documents[doc_id].block(block_id)
        except KeyError:
            continue
        if bloc is None or bloc.page is None or bloc.bbox is None:
            continue
        vus[block_id] = BlocRelu(
            doc_id=doc_id, block_id=block_id, page=bloc.page, bbox=[float(v) for v in bloc.bbox],
            text_norm=bloc.text_norm,
            image_url=ROUTE_PAGE.format(doc_id=doc_id, page=bloc.page, block_id=block_id))
    return PlanRelecture(candidate_revision=candidate_revision,
                         blocs=[vus[k] for k in sorted(vus)])


def ecrire_plan(plan: PlanRelecture, path: Path) -> None:
    """Écrit le plan tel quel : c'est lui que le second lecteur reçoit, et lui que le digest couvre."""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def nom_image(block_id: str) -> str:
    """Le nom de fichier local de l'image d'un bloc — déterministe, sans séparateur de chemin.

    Les `block_id` portent des `:` (`{doc_id}:{loc}:{seq}`), que tous les systèmes de fichiers
    n'acceptent pas et qui ouvriraient une composition de chemin. La substitution est fixe et
    documentée : c'est le nom que l'orchestrateur donne à l'image téléchargée depuis la route de la
    story 3.4, et celui que le validateur cherche.
    """
    return block_id.replace(":", "_") + ".png"


def charger_images(images_dir: Path, plan: PlanRelecture) -> dict[str, bytes]:
    """Les octets **réellement regardés**, un fichier par bloc du plan. Une absence n'est pas vide.

    Un bloc dont l'image manque n'entre pas dans la table : `valider_verdict` refusera alors le
    verdict qui prétend l'avoir relu, plutôt que de le croire sur parole.
    """
    octets: dict[str, bytes] = {}
    for bloc in plan.blocs:
        chemin = images_dir / nom_image(bloc.block_id)
        try:
            octets[bloc.block_id] = chemin.read_bytes()
        except OSError:
            continue
    return octets


def valider_verdict(brut: Any, plan: PlanRelecture, *, candidate_revision: str,
                    images: dict[str, bytes] | None = None) -> VerdictRelecture:
    """Contrôle strict du verdict rempli — sinon `RelectureInvalide`, jamais un verdict à moitié lu.

    **Cinq** liens, et les cinq sont exigés : le **schéma** exact (aucune clé en trop), la
    **révision** candidate (un verdict d'un autre commit ne dit rien de celui-ci), l'**empreinte du
    plan** (un plan modifié après coup rendrait le verdict inintelligible), la **couverture** —
    exactement les blocs du plan, chacun une fois — et, depuis la revue, l'**empreinte des images**.

    Ce cinquième lien est celui qui manquait, et il porte tout le poids de FR47 : `image_sha256`
    était recopié du verdict sans jamais être recoupé, si bien qu'un verdict portant une empreinte
    **inventée** était accepté puis publié « concordante » sur les quatre surfaces. Une fausse preuve
    de seconde lecture, affirmée par le service. Le validateur recalcule donc lui-même l'empreinte
    des octets qu'on lui donne (`empreinte_image`) et exige l'égalité ; une page manquante ou une
    empreinte non recoupée est un refus.

    `images=None` **refuse** : sans les octets, il n'y a rien à recouper, et accepter reviendrait à
    rouvrir la porte que ce lien ferme. Un verdict ne se valide qu'avec ce qui a été regardé.
    """
    if not isinstance(brut, dict):
        raise RelectureInvalide("verdict de seconde lecture : un objet JSON est attendu")
    attendues = {"schema_version", "candidate_revision", "plan_digest", "verdicts"}
    if set(brut) != attendues:
        raise RelectureInvalide(
            f"verdict de seconde lecture : clés racine {sorted(set(brut))}, attendu "
            f"{sorted(attendues)}")
    try:
        verdict = VerdictRelecture.model_validate(brut)
    except ValidationError as exc:
        premier = exc.errors()[0]
        champ = ".".join(str(p) for p in premier.get("loc", ())) or "(racine)"
        raise RelectureInvalide(
            f"verdict de seconde lecture : champ {champ} — {premier.get('msg', '')}") from exc
    if verdict.candidate_revision != candidate_revision:
        raise RelectureInvalide(
            f"verdict de seconde lecture : candidate_revision {verdict.candidate_revision} ≠ "
            f"révision du run {candidate_revision}")
    if verdict.plan_digest != plan.plan_digest:
        raise RelectureInvalide(
            f"verdict de seconde lecture : plan_digest {verdict.plan_digest} ≠ plan servi "
            f"{plan.plan_digest}")
    attendus = [b.block_id for b in plan.blocs]
    rendus = [v.block_id for v in verdict.verdicts]
    if sorted(rendus) != sorted(attendus) or len(set(rendus)) != len(rendus):
        raise RelectureInvalide(
            "verdict de seconde lecture : la couverture du plan n'est pas exacte "
            f"(plan {sorted(attendus)}, verdict {sorted(rendus)})")
    if images is None:
        raise RelectureInvalide(
            "verdict de seconde lecture : les octets réellement regardés ne sont pas fournis — "
            "sans eux, `image_sha256` n'est recoupé avec rien")
    for rendu in verdict.verdicts:
        octets = images.get(rendu.block_id)
        if octets is None:
            raise RelectureInvalide(
                f"verdict de seconde lecture : aucune image fournie pour {rendu.block_id} "
                f"(attendu : {nom_image(rendu.block_id)}) — un bloc qu'on n'a pas pu regarder ne "
                "peut pas avoir été relu")
        observe = empreinte_image(octets)
        if observe != rendu.image_sha256:
            raise RelectureInvalide(
                f"verdict de seconde lecture : {rendu.block_id} annonce image_sha256 "
                f"{rendu.image_sha256}, les octets fournis rendent {observe} — le verdict ne porte "
                "pas sur l'image qu'on a lue")
    return verdict


def empreinte_image(octets: bytes) -> str:
    """L'empreinte que le second lecteur reporte pour l'image qu'il a réellement regardée."""
    return hashlib.sha256(octets).hexdigest()


def statut_du_verdict(verdict: VerdictRelecture) -> str:
    """`concordante` ssi **tous** les blocs relus concordent — jamais « concordante par défaut »."""
    return "concordante" if verdict.concordant else "divergente"


# --- le point d'entrée : écrire le plan, hors réseau et sans clé -----------------------------------

def _main(argv: list[str] | None = None) -> int:
    """`python -m server.evals.relecture --report … --candidate-revision … --out …`

    Sans ce point d'entrée, FR47 était livré en bibliothèque : `ecrire_plan` et `valider_verdict`
    n'avaient aucun appelant, l'orchestrateur — à qui `docs/tests-live.md` assigne le verdict — ne
    recevait jamais de plan, et `SecondeLecturePubliee.statut` ne pouvait pas quitter `planifiee`.

    La commande est **déterministe et hors réseau** : elle lit le rapport d'un run et le corpus servi,
    et n'appelle ni modèle ni rasteriseur. L'image de chaque bloc se demande ensuite à la route de la
    story 3.4, dont l'URL est écrite dans le plan.
    """
    import argparse
    import json
    import sys

    from server.app.config import REPO_ROOT, Settings
    from server.app.corpus.index import Index
    from server.app.corpus.loader import load_corpus

    parser = argparse.ArgumentParser(
        prog="python -m server.evals.relecture",
        description="FR47 — plan de seconde lecture sur images de pages (déterministe, hors réseau).")
    parser.add_argument("--report", type=Path, required=True,
                        help="rapport JSON du run dont on relit les blocs clés")
    parser.add_argument("--candidate-revision", required=True, metavar="SHA40",
                        help="révision produit mesurée par ce run (40 hexadécimaux)")
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--out", type=Path, help="où écrire le plan (défaut : stdout)")
    args = parser.parse_args(argv)
    try:
        rapport = json.loads(args.report.read_text(encoding="utf-8"))
        if not isinstance(rapport, dict):
            raise ValueError("un objet JSON est attendu")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"refus : rapport illisible ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 2
    reglages = Settings()
    corpus = load_corpus(args.data_dir, allow_ungated=True,
                         perimetre_max_chars=reglages.perimetre_max_chars,
                         raison_max_chars=reglages.raison_publiable_max_chars)
    try:
        plan = plan_de_relecture(Index(corpus), blocs_cles_du_rapport(rapport),
                                 candidate_revision=args.candidate_revision)
    except ValidationError as exc:
        print(f"refus : {exc.errors()[0].get('msg', '')}", file=sys.stderr)
        return 2
    if args.out is not None:
        ecrire_plan(plan, args.out)
        print(f"plan écrit : {args.out} ({len(plan.blocs)} bloc(s), "
              f"plan_digest={plan.plan_digest})")
    else:
        print(json.dumps(plan.model_dump(mode="json"), indent=2, ensure_ascii=False))
    return 0 if plan.blocs else 1


if __name__ == "__main__":  # pragma: no cover — point d'entrée
    raise SystemExit(_main())
