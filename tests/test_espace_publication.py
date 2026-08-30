"""L'espace de publication : disposition statique, bundle immuable, unique pointeur atomique.

Story 4.5, B7. Les contre-sondes de l'invariant lui-même vivent dans `tests/test_publication_evals.py`,
là où vivaient déjà celles de la story. Ce fichier éprouve ce qui les rend possibles : la
disposition, ses refus, son report en avant, et le fait qu'elle soit réellement posée **dans le
dépôt** — parce qu'une disposition qu'un commit pourrait défaire en silence ne garantit rien.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from server.app.config import REPO_ROOT
from server.evals import run as runner
from server.evals.espace import (GENERATIONS, EspaceIllisible, EspaceNonInstalle, EspacePublie,
                                 LotHorsEspace, cibles_du_depot)
from server.evals.revision import sorties_du_run

# Les **surfaces de racine** que le dépôt doit porter en liens statiques committés. Ce n'est
# volontairement pas la liste complète de ce que la racine couvre : les artefacts de document sont
# découverts structurellement, et c'est
# `test_la_racine_couvre_tout_ce_quun_ecrivain_de_production_publie` — et lui seul — qui les
# éprouve, en dérivant sa liste de `cibles_du_depot`. Écrire ici une liste qui se voudrait complète
# reviendrait à recopier à la main ce que l'énumération structurelle rend, et à la laisser diverger.
# `.evals/` n'y est pas non plus : il est ignoré par git, et la CI pose ses liens elle-même
# (`.github/workflows/ci.yml`).
CIBLES_COMMITTEES = ("data/manifest.json", "data/evals-latest.json",
                     "docs/evals/latest.md", "docs/evals/campagnes")


def test_la_disposition_du_depot_est_posee_et_resolue_par_le_pointeur() -> None:
    """La disposition est **statique et committée** — c'est ce qui rend la bascule légitime.

    Si elle ne l'était pas, il faudrait la poser au runtime, cible par cible, avant la bascule :
    exactement la substitution que l'AC interdit, et celle pour laquelle une session de build a été
    écartée. Ce test est donc le garde-fou du garde-fou.
    """
    espace = EspacePublie(REPO_ROOT)
    assert espace.installe(), "l'espace de publication du dépôt n'est pas posé"
    assert espace.generation() in GENERATIONS
    for relatif in CIBLES_COMMITTEES:
        cible = REPO_ROOT / relatif
        assert cible.is_symlink(), f"{relatif} doit être un lien statique de l'espace"
        assert espace.resolue_dans_lespace(cible), f"{relatif} ne passe pas par le pointeur"


def test_la_racine_couvre_tout_ce_quun_ecrivain_de_production_publie() -> None:
    """Tour de racine unique : une racine n'a d'autorité que sur ce qu'elle couvre.

    Le tour précédent ne couvrait que le manifest et les surfaces d'évals. Les artefacts qu'une
    ingestion publie — document, sommaire, rapport, proposition de structure, overlay, dictionnaire
    — restaient dehors, si bien que le lot d'un `kb_to_blocks`, d'un `pdf_to_blocks` ou d'un
    `type_clauses` était moitié dedans, moitié dehors : il n'existait aucun geste unique qui le
    publiait, et son échec laissait un artefact neuf devant un manifest périmé.

    L'énumération est **structurelle** (`cibles_du_depot` liste `data/`, aucun `doc_id` n'est écrit
    nulle part) : ce test échouerait à l'ingestion d'un document neuf dont la disposition n'aurait
    pas été posée, ce qui est exactement le signal voulu.
    """
    espace = EspacePublie(REPO_ROOT)
    manquantes = [str(relatif) for relatif in cibles_du_depot(REPO_ROOT)
                  if not espace.resolue_dans_lespace(REPO_ROOT / relatif)]
    assert manquantes == [], (
        "ces cibles de production ne passent pas par le pointeur — les publier serait un rang "
        f"hors de l'atome : {manquantes}")


def test_le_manifest_servi_est_toujours_lisible_a_son_chemin_historique() -> None:
    """Le lecteur ne change pas de chemin : `data/manifest.json` se lit là où il se lisait.

    C'est la contrainte de conception que la spec pose — « tout lecteur existant d'une cible du lot
    doit continuer à la lire là où il la lit aujourd'hui ». Le loader, l'ingestion et l'image ouvrent
    toujours `data/manifest.json` ; seule sa résolution passe désormais par le pointeur.
    """
    brut = json.loads((REPO_ROOT / "data" / "manifest.json").read_text(encoding="utf-8"))
    assert brut, "le manifest servi est vide"
    assert all(isinstance(entree, dict) and "status" in entree for entree in brut.values())


def test_lespace_est_exclu_du_controle_darbre_sinon_le_second_gate_serait_refuse() -> None:
    """`revision_executee` refuse un arbre sale : les sorties du run doivent y être exclues.

    Sans `data/.publie/`, le **deuxième** gate d'une campagne verrait l'arbre sali par le premier —
    le pointeur retourné et la génération réécrite — et refuserait. C'est le seul endroit où la
    nouvelle disposition change ce que `git status` montre après une publication.
    """
    assert "data/.publie/" in sorties_du_run()


def test_une_cible_hors_racine_na_pas_de_pointeur_commun(tmp_path: Path) -> None:
    """Un lot ne peut pas s'étendre hors de la racine : il n'y aurait pas d'atome pour le couvrir."""
    espace = EspacePublie(tmp_path)
    espace.installer([Path("a.md")])
    with pytest.raises(LotHorsEspace, match="hors de la racine"):
        espace.basculer([(Path("/tmp/ailleurs.md"), "x")])


def test_deux_cibles_ne_peuvent_pas_partager_un_slot(tmp_path: Path) -> None:
    """Un lot ne publie pas deux contenus au même chemin : le refus est dit, pas arbitré par l'ordre."""
    espace = EspacePublie(tmp_path)
    espace.installer([Path("a.md")])
    with pytest.raises(LotHorsEspace, match="partagent un slot"):
        espace.basculer([(tmp_path / "a.md", "un"), (tmp_path / "a.md", "deux")])


def test_linstallation_ne_migre_jamais_sans_quon_le_lui_demande(tmp_path: Path) -> None:
    """Une migration silencieuse est précisément ce qu'une bascule n'a pas le droit de faire."""
    cible = tmp_path / "deja.md"
    cible.write_text("contenu d'avant", encoding="utf-8")
    espace = EspacePublie(tmp_path)
    with pytest.raises(EspaceNonInstalle, match="existe déjà hors de l'espace"):
        espace.installer([Path("deja.md")])
    assert cible.read_text(encoding="utf-8") == "contenu d'avant"
    assert not cible.is_symlink(), "un refus d'installation ne change pas le type de la cible"
    # En mode opérateur, la migration a lieu et conserve le contenu **à l'octet**.
    espace.installer([Path("deja.md")], migrer=True)
    assert cible.is_symlink() and cible.read_text(encoding="utf-8") == "contenu d'avant"


def test_linstallation_est_idempotente(tmp_path: Path) -> None:
    """La reposer ne retourne pas le pointeur et ne perd pas ce qui est publié."""
    espace = EspacePublie(tmp_path)
    espace.installer([Path("a.md")])
    espace.basculer([(tmp_path / "a.md", "publié")])
    generation = espace.generation()
    espace.installer([Path("a.md")])
    assert espace.generation() == generation
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "publié"


def test_un_lien_pendant_est_une_absence_et_rien_dautre(tmp_path: Path) -> None:
    """Avant toute publication, une cible installée est **absente** pour tout lecteur.

    C'est la fermeture B6 conservée : `FileNotFoundError` seule signifie l'absence. Un lien pendant
    la rend exactement, sans qu'aucun lecteur ait à connaître l'espace.
    """
    espace = EspacePublie(tmp_path)
    espace.installer([Path("jamais.md")])
    cible = tmp_path / "jamais.md"
    assert not cible.exists() and not cible.is_file()
    with pytest.raises(FileNotFoundError):
        cible.read_text(encoding="utf-8")


def test_une_surface_hors_du_lot_garde_son_contenu_a_travers_la_bascule(tmp_path: Path) -> None:
    """Le report en avant : une bascule ne publie pas l'oubli des surfaces qu'elle ne réécrit pas.

    La génération inactive est d'abord un miroir en liens durs de la génération courante. Sans ce
    report, publier `latest.md` seul effacerait le manifest — un lot partiel obtenu par omission.
    """
    espace = EspacePublie(tmp_path)
    espace.installer([Path("a.md"), Path("b.md")])
    espace.basculer([(tmp_path / "a.md", "a1"), (tmp_path / "b.md", "b1")])
    espace.basculer([(tmp_path / "a.md", "a2")])
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "a2"
    assert (tmp_path / "b.md").read_text(encoding="utf-8") == "b1"


def test_ecrire_dans_la_generation_inactive_ne_touche_pas_la_generation_publiee(
        tmp_path: Path) -> None:
    """Le miroir est en liens **durs** : écrire un slot doit remplacer l'entrée, jamais écrire à travers.

    Si l'écriture passait à travers le lien dur, la génération encore publiée changerait sous les
    pieds des lecteurs — une modification de cible avant l'atome, donc l'invariant rompu.
    """
    espace = EspacePublie(tmp_path)
    espace.installer([Path("a.md")])
    espace.basculer([(tmp_path / "a.md", "a1")])
    publiee = espace.chemin / espace.generation() / "a.md"
    inode_publie = os.stat(publiee).st_ino
    espace.basculer([(tmp_path / "a.md", "a2")])
    # L'ancienne génération existe toujours et porte encore son contenu d'avant.
    ancienne = [g for g in GENERATIONS if g != espace.generation()][0]
    assert (espace.chemin / ancienne / "a.md").read_text(encoding="utf-8") == "a1"
    assert os.stat(espace.chemin / ancienne / "a.md").st_ino == inode_publie


def test_un_pointeur_hors_vocabulaire_ferme_au_lieu_de_deviner(tmp_path: Path) -> None:
    """Un garde-fou qui ne peut pas conclure refuse. Il ne choisit pas une génération au hasard."""
    espace = EspacePublie(tmp_path)
    espace.installer([Path("a.md")])
    espace.pointeur.unlink()
    os.symlink("genereation-inventee", espace.pointeur)
    with pytest.raises(EspaceIllisible, match="hors de"):
        espace.generation()


def test_lautorite_du_couple_racine_et_bundle_est_unique() -> None:
    """`espace_du_data_dir` est la seule dérivation : deux auraient fini par désigner deux espaces."""
    espace = runner.espace_du_data_dir(Path("/quelque/part/data"))
    assert espace.racine == Path("/quelque/part")
    assert espace.chemin == Path("/quelque/part/data/.publie")


def test_les_liens_servis_ne_sortent_jamais_de_ce_que_limage_copie() -> None:
    """`Dockerfile` copie `server data web tools` : un lien servi qui sort de `data/` serait pendant.

    `COPY data ./data` recopie les liens tels quels, sans les déréférencer. Un `data/manifest.json`
    pointant hors de `data/` produirait donc, **dans l'image seulement**, un lien pendant : la route
    servie rendrait `publie: false` en production et le loader ne trouverait plus le manifest — un
    défaut invisible hors conteneur. La propriété se contrôle statiquement, sans construire d'image.
    """
    for relatif in ("data/manifest.json", "data/evals-latest.json"):
        lien = REPO_ROOT / relatif
        cible = Path(os.readlink(lien))
        assert not cible.is_absolute(), f"{relatif} : un lien absolu ne survit pas à l'image"
        resolu = Path(os.path.normpath(lien.parent / cible))
        assert resolu.is_relative_to(REPO_ROOT / "data"), (
            f"{relatif} se résout hors de `data/`, que l'image copie")


def test_lingestion_ecrit_a_travers_le_lien_et_ne_le_remplace_pas(tmp_path: Path) -> None:
    """Le chemin frère le plus dangereux : `write_atomic` détruisait le lien du manifest.

    `tmp.replace(path)` remplace un lien symbolique par un fichier ordinaire. Une seule ingestion
    aurait suffi : la bascule suivante aurait déplacé le pointeur sans que `data/manifest.json` en
    voie rien, et le run aurait publié quatre surfaces sur cinq en annonçant un succès — un faux
    vert. L'écriture passe donc par l'espace quand la cible en relève.
    """
    from server.ingest.artifacts import write_atomic

    espace = EspacePublie(tmp_path)
    espace.installer([Path("data") / "manifest.json"])
    manifest = tmp_path / "data" / "manifest.json"
    espace.basculer([(manifest, "{}\n")])
    write_atomic(manifest, '{"doc": 1}\n')
    assert manifest.is_symlink(), "l'ingestion a remplacé le lien par un fichier ordinaire"
    assert espace.resolue_dans_lespace(manifest), "le manifest est sorti de l'espace"
    assert json.loads(manifest.read_text(encoding="utf-8")) == {"doc": 1}


# --- B7, tour correctif 3/3 : immutabilité du bundle et concurrence -------------------------------
#
# Mesuré sur une copie figée de `824e509` : `write_atomic` résolvait le lien puis renommait **dans la
# génération active**, hors du verrou. `'ancien-manifest.json'` devenait `'ecrit-par-l-ingestion'`
# dans la génération que le pointeur publie, sans que le pointeur bouge. Le bundle n'était donc pas
# immuable, et deux écrivains — une ingestion et la reconstruction d'un run — couraient sur les deux
# mêmes générations sans se voir.

def test_lingestion_du_manifest_ne_mute_jamais_la_generation_publiee(tmp_path: Path) -> None:
    """Le bundle est **immuable** : on ne récrit pas une génération, on en publie une autre.

    La sonde compare la génération active **d'avant** : ses octets et son inode doivent être
    intacts après l'écriture, et le pointeur doit avoir bougé. C'est la différence exacte entre
    « écrire à travers le lien » (ce que faisait `824e509`) et « passer par le protocole ».
    """
    from server.ingest.artifacts import write_atomic

    espace = EspacePublie(tmp_path)
    espace.installer([Path("data") / "manifest.json", Path("docs") / "evals" / "latest.md"])
    manifest = tmp_path / "data" / "manifest.json"
    latest = tmp_path / "docs" / "evals" / "latest.md"
    espace.basculer([(manifest, '{"doc": 0}\n'), (latest, "publié par le run\n")])

    generation_avant = espace.generation()
    slot_avant = espace.chemin / generation_avant / "data" / "manifest.json"
    octets_avant, inode_avant = slot_avant.read_bytes(), os.stat(slot_avant).st_ino

    write_atomic(manifest, '{"doc": 1}\n')

    assert espace.generation() != generation_avant, (
        "l'ingestion doit publier une génération, jamais muter celle que le pointeur sert")
    assert slot_avant.read_bytes() == octets_avant, "la génération publiée a été mutée en place"
    assert os.stat(slot_avant).st_ino == inode_avant
    assert json.loads(manifest.read_text(encoding="utf-8")) == {"doc": 1}
    # Le report en avant vaut aussi pour l'ingestion : ce qu'elle ne réécrit pas reste publié.
    assert latest.read_text(encoding="utf-8") == "publié par le run\n"
    assert espace.residus() == []


def test_lingestion_du_manifest_prend_le_meme_verrou_que_la_bascule(tmp_path: Path) -> None:
    """Un second écrivain ne peut ni voir ni produire un bundle partiel : il **attend**.

    Le verrou de l'espace était pris par la bascule d'un run et par elle seule ; l'ingestion écrivait
    à côté. Le ping-pong à deux générations n'est sûr que si **tous** ses écrivains le prennent —
    sinon deux d'entre eux choisissent la même génération inactive et le pointeur publie un mélange.

    La preuve est une vraie contention : le verrou est tenu, l'ingestion est lancée dans un fil, et
    on vérifie qu'elle **n'a pas** abouti tant que le verrou n'est pas rendu. `flock` associe le
    verrou à la description de fichier ouverte, donc deux `open` du même processus se bloquent bien
    l'un l'autre.
    """
    import threading

    from server.evals.espace import _verrou
    from server.ingest.artifacts import write_atomic

    espace = EspacePublie(tmp_path)
    espace.installer([Path("data") / "manifest.json"])
    manifest = tmp_path / "data" / "manifest.json"
    espace.basculer([(manifest, '{"doc": 0}\n')])

    fini = threading.Event()

    def _ecrire() -> None:
        write_atomic(manifest, '{"doc": 1}\n')
        fini.set()

    fil = threading.Thread(target=_ecrire)
    with _verrou(espace.chemin):
        fil.start()
        assert not fini.wait(0.3), (
            "l'ingestion a écrit sans attendre le verrou de l'espace : deux écrivains courent")
        assert json.loads(manifest.read_text(encoding="utf-8")) == {"doc": 0}
    fil.join(10)
    assert fini.is_set(), "l'ingestion n'a jamais abouti après la libération du verrou"
    assert json.loads(manifest.read_text(encoding="utf-8")) == {"doc": 1}


def test_une_racine_atteinte_par_un_prefixe_lie_est_reconnue_et_basculee(tmp_path: Path) -> None:
    """Le texte promettait un repli là où le code levait — trouvé à la vérification indépendante.

    `espace_couvrant` dérivait la racine de `os.path.realpath`, puis interrogeait
    `resolue_dans_lespace` avec le chemin **non résolu**. `slot()` compare par `os.path.abspath`,
    qui ne résout pas les liens : toute cible atteinte par un préfixe lié — `/tmp` en est un sur
    macOS — faisait donc lever `LotHorsEspace` là où `write_atomic` annonçait « elle garde
    l'écriture atomique d'avant », et `basculer` aurait levé de la même façon.

    Le `tmp_path` de pytest est déjà résolu (`/private/var/…`), ce qui rendait toute la suite aveugle
    à ce chemin. La sonde construit donc son propre préfixe lié, et éprouve les **deux** cas :

    1. une cible **couverte** doit être reconnue et basculée — attraper `LotHorsEspace` pour rendre
       `None` la ferait retomber sur l'écriture à travers le lien, c'est-à-dire la réouverture du
       défaut d'immutabilité que ce tour ferme ;
    2. une cible **ordinaire** doit rendre `None` et garder son écriture d'avant.
    """
    from server.evals.espace import espace_couvrant
    from server.ingest.artifacts import write_atomic

    reel = tmp_path / "reel"
    reel.mkdir()
    racine = tmp_path / "lien"
    os.symlink(reel.name, racine)
    assert Path(os.path.realpath(racine)) != racine, "le préfixe doit être un lien"

    espace = EspacePublie(racine)
    espace.installer([Path("data") / "manifest.json"])
    manifest = racine / "data" / "manifest.json"
    espace.basculer([(manifest, '{"doc": 0}\n')])

    # 1. La cible couverte est reconnue, **dans le repère de l'appelant**, et passe par le protocole.
    couvrant = espace_couvrant(manifest)
    assert couvrant is not None, "une cible couverte atteinte par un préfixe lié doit l'être encore"
    assert couvrant.racine == racine and couvrant.data_dir == racine / "data"
    generation_avant = espace.generation()
    slot_avant = espace.chemin / generation_avant / "data" / "manifest.json"
    octets_avant, inode_avant = slot_avant.read_bytes(), os.stat(slot_avant).st_ino

    write_atomic(manifest, '{"doc": 1}\n')

    assert espace.generation() != generation_avant, "l'écriture doit publier, jamais muter"
    assert slot_avant.read_bytes() == octets_avant, "la génération publiée a été mutée en place"
    assert os.stat(slot_avant).st_ino == inode_avant
    assert manifest.is_symlink() and json.loads(manifest.read_text(encoding="utf-8")) == {"doc": 1}

    # 2. La cible ordinaire, sous la même racine liée, garde son écriture d'avant.
    ordinaire = racine / "structure.json"
    assert espace_couvrant(ordinaire) is None
    write_atomic(ordinaire, '{"b": 2}\n')
    assert not ordinaire.is_symlink()
    assert json.loads(ordinaire.read_text(encoding="utf-8")) == {"b": 2}
    assert espace.residus() == []


def test_une_cible_hors_espace_garde_son_ecriture_atomique_ordinaire(tmp_path: Path) -> None:
    """Tout n'est pas dans un bundle : `document.json` et consorts gardent leur chemin d'écriture.

    `espace_couvrant` reconnaît la couverture **structurellement**, par le chemin résolu de la
    cible, jamais par son nom : un fichier ordinaire, un lien qui ne mène à aucun espace et un
    manifest de test posé hors bundle rendent tous `None`.
    """
    from server.evals.espace import espace_couvrant
    from server.ingest.artifacts import write_atomic

    ordinaire = tmp_path / "document.json"
    write_atomic(ordinaire, '{"a": 1}\n')
    assert espace_couvrant(ordinaire) is None
    assert json.loads(ordinaire.read_text(encoding="utf-8")) == {"a": 1}
    assert not ordinaire.is_symlink()

    ailleurs = tmp_path / "cible-liee.json"
    reel = tmp_path / "reel.json"
    reel.write_text("{}\n", encoding="utf-8")
    os.symlink(reel.name, ailleurs)
    assert espace_couvrant(ailleurs) is None
    write_atomic(ailleurs, '{"b": 2}\n')
    assert ailleurs.is_symlink(), "un lien hors espace reste écrit à travers, pas remplacé"
    assert json.loads(reel.read_text(encoding="utf-8")) == {"b": 2}
    assert sorted(p.name for p in tmp_path.rglob("*") if p.name.endswith(".tmp")) == []


# --- B7, tour de racine unique : les quatre faits du verdict, contre-sondés ------------------------
#
# Le tour 3/3 avait rendu l'opération de production du runner tout-ou-rien, mais une racine n'a
# d'autorité que sur les écrivains qui passent par elle. Quatre voies lui échappaient : une écriture
# groupée qui remplaçait la cible sans jamais la voir, un read-modify-write dont la lecture précédait
# le verrou, une sortie de section critique qui pouvait lever après le commit, et un brouillon qui
# devenait invisible dès que le pointeur était indécidable.


def _etat_observable(cibles: list[Path]) -> dict[str, tuple[bool, str | None, str | None, bytes | None]]:
    """Les quatre dimensions de l'AC : présence, type `lstat`, cible de lien, contenu.

    Comparer les octets seuls laisserait passer un lien couvert remplacé par un fichier ordinaire de
    même contenu — le fait 1 exactement. Aucune cible n'est retirée de la comparaison.
    """
    etat: dict[str, tuple[bool, str | None, str | None, bytes | None]] = {}
    for cible in cibles:
        present = cible.is_symlink() or cible.exists()
        if cible.is_symlink():
            type_entree: str | None = "lien"
            lien: str | None = os.readlink(cible)
        elif cible.is_dir():
            type_entree, lien = "repertoire", None
        elif cible.exists():
            type_entree, lien = "fichier", None
        else:
            type_entree, lien = None, None
        try:
            contenu: bytes | None = cible.read_bytes()
        except OSError:
            contenu = None
        etat[str(cible)] = (present, type_entree, lien, contenu)
    return etat


def _espace_pose(tmp_path: Path, relatifs: tuple[str, ...], lot: list[tuple[str, str]]) -> EspacePublie:
    espace = EspacePublie(tmp_path)
    espace.installer([Path(relatif) for relatif in relatifs])
    espace.basculer([(tmp_path / relatif, contenu) for relatif, contenu in lot])
    return espace


def test_deux_fusions_concurrentes_du_manifest_ne_perdent_aucune_entree(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fait 2 : la **lecture** doit vivre sous le verrou, pas seulement le commit.

    `merge_manifest` recevait un `raw` que l'appelant avait lu au début de son traitement — des
    minutes plus tôt pour un typage — et ne prenait le verrou qu'à l'écriture finale. Deux fusions
    concurrentes partaient donc du même état : la seconde publiait un manifest où l'entrée de la
    première n'avait jamais existé. Sérialiser les commits n'y changeait rien, puisque c'est la
    lecture qui était dehors.

    La preuve est une **contention réelle**, pas un ordonnancement supposé : la première fusion est
    retenue à l'intérieur de sa section critique jusqu'à ce que la seconde ait démarré, de sorte
    qu'une lecture faite hors verrou tomberait forcément sur l'état d'avant.
    """
    import threading

    from server.app.domain import ManifestEntry
    from server.ingest import artifacts

    espace = _espace_pose(tmp_path, ("data/manifest.json",), [("data/manifest.json", "{}\n")])
    manifest = tmp_path / "data" / "manifest.json"

    a_lu = threading.Event()
    b_lancee = threading.Event()
    vraie_fusion = artifacts.merged_manifest

    def _fusion_retenue(raw: dict[str, object], doc_id: str, entry: ManifestEntry) -> object:
        if doc_id == "doc-a":
            a_lu.set()
            assert b_lancee.wait(10), "la seconde fusion n'a jamais démarré"
        return vraie_fusion(raw, doc_id, entry)  # type: ignore[arg-type]

    monkeypatch.setattr(artifacts, "merged_manifest", _fusion_retenue)

    def _entree(edition: str) -> ManifestEntry:
        return ManifestEntry(status="servi", source_hash="s" * 64, ingest_fingerprint="f" * 64,
                             document_hash="d" * 64, edition=edition, gate=None)

    erreurs: list[BaseException] = []

    def _fusionner(doc_id: str) -> None:
        try:
            artifacts.merge_manifest(manifest, doc_id, _entree(doc_id))
        except BaseException as exc:  # noqa: BLE001 — remontée au fil principal
            erreurs.append(exc)

    fil_a = threading.Thread(target=_fusionner, args=("doc-a",))
    fil_a.start()
    assert a_lu.wait(10), "la première fusion n'a jamais atteint sa section critique"
    fil_b = threading.Thread(target=_fusionner, args=("doc-b",))
    fil_b.start()
    # Le temps que la seconde fusion lise — hors verrou, elle lirait l'état d'avant et l'écraserait.
    fil_b.join(0.3)
    b_lancee.set()
    fil_a.join(10)
    fil_b.join(10)

    assert erreurs == []
    publie = json.loads(manifest.read_text(encoding="utf-8"))
    assert sorted(publie) == ["doc-a", "doc-b"], (
        f"une fusion a écrasé l'autre : mise à jour perdue ({sorted(publie)})")
    assert manifest.is_symlink() and espace.resolue_dans_lespace(manifest)
    assert espace.residus() == []


def test_la_generation_courante_est_lue_sous_le_verrou(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le même défaut que le fait 2, au cœur de l'espace, et plus grave.

    Lire le pointeur **avant** de prendre le verrou, c'est prendre pour « inactive » la génération
    qu'un concurrent vient de publier : la bascule la reconstruit alors de zéro depuis un état
    périmé et la republie. Ce n'est pas seulement une mise à jour perdue, c'est une **mutation de la
    génération active** — exactement les deux propriétés que ce tour ferme.

    La contre-sonde fait basculer un concurrent entre le préflight et la prise du verrou, puis
    vérifie qu'une surface **hors du lot** du second écrivain porte bien ce que le concurrent a
    publié, et non ce qui la précédait.
    """
    from server.evals import espace as espace_module

    espace = _espace_pose(tmp_path, ("data/manifest.json", "docs/evals/latest.md"),
                          [("data/manifest.json", "v0"), ("docs/evals/latest.md", "l0")])
    manifest = tmp_path / "data" / "manifest.json"
    latest = tmp_path / "docs" / "evals" / "latest.md"

    concurrent_fait = {"oui": False}
    vrai_enter = espace_module._verrou.__enter__

    def _basculer_un_concurrent_puis_verrouiller(self: object) -> object:
        if not concurrent_fait["oui"]:
            concurrent_fait["oui"] = True
            EspacePublie(tmp_path).basculer([(manifest, "v1"), (latest, "l1")])
        return vrai_enter(self)  # type: ignore[arg-type]

    monkeypatch.setattr(espace_module._verrou, "__enter__", _basculer_un_concurrent_puis_verrouiller)

    espace.basculer([(manifest, "v2")])

    assert concurrent_fait["oui"], "la sonde n'a pas injecté de concurrent"
    assert manifest.read_text(encoding="utf-8") == "v2"
    assert latest.read_text(encoding="utf-8") == "l1", (
        "la publication du concurrent a été effacée : la génération courante a été lue hors verrou")
    assert espace.residus() == []


def test_un_deverrouillage_qui_leve_apres_le_commit_ne_propage_rien(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fait 3 : la frontière post-commit ne s'arrête pas au `return` de la bascule.

    `flock(LOCK_UN)` et `close` sont des appels système : ils peuvent lever. Appelés **après** un
    pointeur effectivement remplacé, ils feraient remonter une exception avec le lot déjà publié —
    « publié » et « échoué » vrais en même temps. Le verrou est donc rendu du bon côté de la
    frontière, et sa libération reste effective : un écrivain suivant ne doit pas attendre pour
    toujours.
    """
    import threading

    from server.evals import espace as espace_module

    espace = _espace_pose(tmp_path, ("data/manifest.json",), [("data/manifest.json", "v0")])
    manifest = tmp_path / "data" / "manifest.json"
    vrai_flock = espace_module.fcntl.flock

    def _flock_qui_leve_au_deverrouillage(fd: int, operation: int) -> None:
        if operation == espace_module.fcntl.LOCK_UN:
            raise OSError("EIO pendant flock(LOCK_UN)")
        vrai_flock(fd, operation)

    monkeypatch.setattr(espace_module.fcntl, "flock", _flock_qui_leve_au_deverrouillage)

    espace.basculer([(manifest, "v1")])  # aucune exception : le commit est acquis

    assert manifest.read_text(encoding="utf-8") == "v1"
    assert manifest.is_symlink() and espace.resolue_dans_lespace(manifest)

    # Le verrou a bien été rendu : fermer le descripteur suffit, et le filet le fait.
    fini = threading.Event()

    def _suivant() -> None:
        espace.basculer([(manifest, "v2")])
        fini.set()

    fil = threading.Thread(target=_suivant, daemon=True)
    fil.start()
    assert fini.wait(10), "le verrou n'a pas été rendu : l'écrivain suivant attend indéfiniment"
    assert manifest.read_text(encoding="utf-8") == "v2"
    assert espace.residus() == []


def test_une_interruption_a_la_sortie_de_la_section_critique_ne_propage_rien(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fait 3, variante `BaseException` : toute sortie de contexte postérieure au commit est absorbée.

    Un `KeyboardInterrupt` tombant dans `_verrou.__exit__` après le commit remontait avec le lot
    déjà basculé. La garantie porte sur **toute** la pile qui suit le point de commit, pas sur les
    seules `OSError`.
    """
    import threading

    from server.evals import espace as espace_module

    espace = _espace_pose(tmp_path, ("data/manifest.json",), [("data/manifest.json", "v0")])
    manifest = tmp_path / "data" / "manifest.json"

    def _sortie_interrompue(self: object, *_exc: object) -> None:
        raise KeyboardInterrupt("interruption à la sortie de la section critique")

    monkeypatch.setattr(espace_module._verrou, "__exit__", _sortie_interrompue)

    espace.basculer([(manifest, "v1")])  # aucune exception, `BaseException` comprise

    assert manifest.read_text(encoding="utf-8") == "v1"
    fini = threading.Event()

    def _suivant() -> None:
        espace.basculer([(manifest, "v2")])
        fini.set()

    fil = threading.Thread(target=_suivant, daemon=True)
    fil.start()
    assert fini.wait(10), "le filet n'a pas rendu la section critique"
    assert manifest.read_text(encoding="utf-8") == "v2"
    assert espace.residus() == []


def test_une_exception_de_verrou_avant_le_commit_remonte_sans_rien_modifier(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """La symétrie du fait 3 : avant le commit, une exception de verrou **doit** remonter.

    Absorber les sorties de section critique après le commit ne doit pas devenir « absorber les
    exceptions de verrou ». Avant le commit il n'y a rien de publié qu'une exception contredirait :
    elle remonte, et zéro cible du lot n'est modifiée sur les quatre dimensions.
    """
    from server.evals import espace as espace_module

    espace = _espace_pose(tmp_path, ("data/manifest.json", "docs/evals/latest.md"),
                          [("data/manifest.json", "v0"), ("docs/evals/latest.md", "l0")])
    cibles = [tmp_path / "data" / "manifest.json", tmp_path / "docs" / "evals" / "latest.md"]
    avant = _etat_observable(cibles)
    vrai_flock = espace_module.fcntl.flock

    def _flock_indisponible(fd: int, operation: int) -> None:
        if operation == espace_module.fcntl.LOCK_EX:
            raise OSError("verrou indisponible")
        vrai_flock(fd, operation)

    monkeypatch.setattr(espace_module.fcntl, "flock", _flock_indisponible)

    # Le verrou est pris **avant** le `try/finally` de la transaction — c'est ce qui permet de
    # refuser sans rien créer. Son descripteur doit donc être rendu par la prise elle-même, sinon
    # chaque refus fuit un descripteur, et un processus qui en refuse assez finit en `EMFILE`.
    ouverts_avant = len(os.listdir("/dev/fd"))

    with pytest.raises(OSError, match="verrou indisponible"):
        espace.basculer([(cibles[0], "v1"), (cibles[1], "l1")])

    assert len(os.listdir("/dev/fd")) <= ouverts_avant, "le descripteur du verrou non acquis a fui"
    assert _etat_observable(cibles) == avant
    assert espace.residus() == []


def test_un_brouillon_abandonne_pointeur_indecidable_est_vu_par_la_sonde(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fait 4 : le renoncement prudent est juste, mais il ne doit pas être silencieux.

    Quand le pointeur est **indécidable** au moment de l'abandon, `_abandonner` ne détruit rien — une
    génération publiée effacée rendrait toutes les cibles pendantes. Mais il laissait alors la
    génération inactive, peut-être à moitié écrite, sous son nom `a`/`b`, que `residus()` ne
    regardait pas : un brouillon partiel devenait indiscernable d'un bundle complet, pour la sonde
    comme pour un opérateur.
    """
    from server.evals import espace as espace_module

    espace = _espace_pose(tmp_path, ("data/manifest.json", "docs/evals/latest.md"),
                          [("data/manifest.json", "v0"), ("docs/evals/latest.md", "l0")])
    cibles = [tmp_path / "data" / "manifest.json", tmp_path / "docs" / "evals" / "latest.md"]
    avant = _etat_observable(cibles)
    active = espace.generation()
    empreinte_active = {str(p.relative_to(espace.chemin)): p.read_bytes()
                        for p in (espace.chemin / active).rglob("*") if p.is_file()}

    ecrits = {"n": 0}
    vrai_ecrire = espace_module._ecrire_dans_bundle

    def _echouer_au_second_slot(chemin: Path, contenu: str) -> None:
        ecrits["n"] += 1
        if ecrits["n"] == 2:
            raise OSError("panne d'écriture du brouillon")
        vrai_ecrire(chemin, contenu)

    monkeypatch.setattr(espace_module, "_ecrire_dans_bundle", _echouer_au_second_slot)
    # Le pointeur devient indécidable au moment de conclure : `_abandonner` ne peut plus savoir
    # quelle génération est publiée, donc il ne détruit rien — et doit laisser une trace.
    monkeypatch.setattr(EspacePublie, "_generation_publiee", lambda self: None)

    with pytest.raises(OSError, match="panne d'écriture du brouillon"):
        espace.basculer([(cibles[0], "v1"), (cibles[1], "l1")])

    monkeypatch.undo()
    assert _etat_observable(cibles) == avant, "une cible du lot a bougé"
    assert espace.generation() == active, "la génération active a changé"
    assert {str(p.relative_to(espace.chemin)): p.read_bytes()
            for p in (espace.chemin / active).rglob("*") if p.is_file()} == empreinte_active, (
        "la génération active a été touchée par l'abandon")
    assert espace.residus() != [], (
        "la génération inactive est restée partielle sans qu'aucune sonde ne la voie")


def test_un_lot_sans_racine_dont_un_rang_echoue_ne_modifie_aucune_cible(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reprise du tour : un arbre sans pointeur tient la **même propriété observable**.

    Une version de `publier_artefacts` préparait ses temporaires puis enchaînait les `rename` sans
    rien défaire, au motif qu'« un arbre sans racine n'a pas la propriété ». C'était l'affaiblissement
    que la contre-sonde historique du typage dit depuis toujours : un `rename` réussi au rang 1 suivi
    d'un échec au rang 2 laisse la première cible publiée.

    Ici l'échec tombe au **dernier** rang écrit, et la sonde couvre aussi une cible qui n'existait
    pas — la rétablir, c'est la faire redisparaître, pas y écrire un vide.
    """
    from server.ingest import artifacts

    existantes = [tmp_path / nom for nom in ("document.json", "report.json", "manifest.json")]
    for index, cible in enumerate(existantes):
        cible.write_text(f"avant-{index}", encoding="utf-8")
    neuve = tmp_path / "structure.json"
    cibles = [*existantes, neuve]
    avant = _etat_observable(cibles)

    appels = {"n": 0}
    vrai_replace = artifacts.os.replace

    def _echouer_au_troisieme(source: object, cible: object) -> None:
        appels["n"] += 1
        if appels["n"] == 3:
            raise OSError("panne simulée")
        vrai_replace(source, cible)  # type: ignore[arg-type]

    monkeypatch.setattr(artifacts.os, "replace", _echouer_au_troisieme)

    with pytest.raises(OSError, match="panne simulée"):
        artifacts.publier_artefacts([(cible, f"après-{index}") for index, cible in enumerate(cibles)])

    monkeypatch.undo()
    assert _etat_observable(cibles) == avant
    assert sorted(p.name for p in tmp_path.rglob("*") if p.name.endswith(".tmp")) == []


def test_un_brouillon_complet_dont_le_sort_est_indecidable_est_vu_aussi(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fait 4, second volet : la marque tombe quand le brouillon est complet — elle doit revenir.

    Un brouillon complet et `fsync`é n'est plus un résidu : sa marque est retirée, et c'est juste,
    puisque la bascule suivante s'en sert comme miroir. Mais si l'échec tombe **entre** ce retrait et
    la conclusion, et que le pointeur est alors indécidable, on ne sait plus si cette génération est
    celle que le pointeur publie : `_abandonner` ne détruit rien — à raison — et doit **reposer** la
    marque, faute de quoi une génération dont le sort est inconnu redevient silencieuse.
    """
    from server.evals import espace as espace_module

    espace = _espace_pose(tmp_path, ("data/manifest.json", "docs/evals/latest.md"),
                          [("data/manifest.json", "v0"), ("docs/evals/latest.md", "l0")])
    cibles = [tmp_path / "data" / "manifest.json", tmp_path / "docs" / "evals" / "latest.md"]
    avant = _etat_observable(cibles)
    active = espace.generation()

    def _lien_temporaire_impossible(*_args: object, **_kw: object) -> None:
        raise OSError("panne juste avant l'atome")

    # `os.symlink` n'est appelé qu'après le `fsync` du brouillon et le retrait de sa marque : l'échec
    # tombe donc sur un brouillon **complet**, à un cheveu du point de commit.
    monkeypatch.setattr(espace_module.os, "symlink", _lien_temporaire_impossible)
    monkeypatch.setattr(EspacePublie, "_generation_publiee", lambda self: None)

    with pytest.raises(OSError, match="panne juste avant l'atome"):
        espace.basculer([(cibles[0], "v1"), (cibles[1], "l1")])

    monkeypatch.undo()
    assert _etat_observable(cibles) == avant
    assert espace.generation() == active, "la génération active a changé"
    assert espace.residus() != [], (
        "une génération dont le sort est indécidable est restée indiscernable d'un bundle complet")


# --- Revue du tour de racine unique : les dix constats, contre-sondés -------------------------------


def _verrou_tenu(espace: EspacePublie) -> bool:
    """Le `flock` de l'espace est-il pris ? — sondé depuis une **autre** description de fichier.

    `flock` associe le verrou à la description ouverte, pas au processus : deux `open` du même
    processus se bloquent bien l'un l'autre. Un `LOCK_EX | LOCK_NB` qui échoue prouve donc que la
    section critique est tenue au moment où on le demande, ce qui est exactement la question
    « cette lecture se fait-elle sous le verrou ? ».
    """
    import fcntl

    fd = os.open(espace.chemin / ".verrou", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def test_le_gate_relit_le_manifest_sous_le_verrou_et_ne_perd_aucune_entree(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Constat 1 : le gate est le **quatrième** écrivain du manifest, et il lisait dehors.

    `preparer_gate` relisait `data/manifest.json` entier hors verrou, puis remettait sa
    sérialisation à la bascule. Une ingestion publiée entre les deux disparaissait du manifest que
    le gate republiait — le fait 2 du tour, sur le writer que le tour n'avait pas fermé, alors que
    son invariant dit « tout read-modify-write du manifest ».

    La preuve est une **contention réelle** : le gate est retenu entre sa lecture et sa
    publication, une ingestion démarre pendant ce temps, et l'entrée qu'elle publie doit survivre.
    """
    import threading

    from server.app.domain.ingest import Gate, ManifestEntry
    from server.evals import run as runner
    from server.ingest import artifacts

    espace = _espace_pose(tmp_path, ("data/manifest.json",), [("data/manifest.json", "{}\n")])
    manifest = tmp_path / "data" / "manifest.json"

    def _entree(edition: str) -> ManifestEntry:
        return ManifestEntry(status="servi", source_hash="s" * 64, ingest_fingerprint="f" * 64,
                             document_hash="d" * 64, edition=edition, gate=None)

    espace.basculer([(manifest, json.dumps({"doc-gate": _entree("v0").model_dump(mode="json")},
                                           indent=2) + "\n")])

    gate = Gate(profile="vertical", source_hash="s" * 64, ingest_fingerprint="f" * 64,
                cases_hash="c" * 64, pipeline_digest="p" * 64, prompts_digest="q" * 64,
                model_ids={"reason": "m"}, evals_ok=True, date="2026-08-30T00:00:00Z",
                cases=1, countersigned=False)

    a_lu = threading.Event()
    b_lancee = threading.Event()
    vrai_preparer = runner.preparer_gate
    sous_verrou: list[bool] = []

    def _preparer_retenu(manifest_path: Path, doc_id: str, g: Any, **kw: Any) -> Any:
        sous_verrou.append(_verrou_tenu(espace))
        resultat = vrai_preparer(manifest_path, doc_id, g, **kw)
        a_lu.set()
        assert b_lancee.wait(10), "l'ingestion concurrente n'a jamais démarré"
        return resultat

    monkeypatch.setattr(runner, "preparer_gate", _preparer_retenu)
    erreurs: list[BaseException] = []

    def _ecrire_le_gate() -> None:
        try:
            runner.ecrire_gate(manifest, "doc-gate", gate)
        except BaseException as exc:  # noqa: BLE001 — remontée au fil principal
            erreurs.append(exc)

    def _ingerer() -> None:
        try:
            artifacts.merge_manifest(manifest, "doc-ingere", _entree("v1"))
        except BaseException as exc:  # noqa: BLE001
            erreurs.append(exc)

    fil_gate = threading.Thread(target=_ecrire_le_gate)
    fil_gate.start()
    assert a_lu.wait(10), "le gate n'a jamais atteint sa lecture"
    fil_ingestion = threading.Thread(target=_ingerer)
    fil_ingestion.start()
    fil_ingestion.join(0.3)
    b_lancee.set()
    fil_gate.join(10)
    fil_ingestion.join(10)

    assert erreurs == []
    assert sous_verrou == [True], (
        "le gate a lu le manifest hors du verrou : une publication concurrente peut se glisser "
        "entre sa lecture et son commit")
    publie = json.loads(manifest.read_text(encoding="utf-8"))
    assert sorted(publie) == ["doc-gate", "doc-ingere"], (
        f"le gate a écrasé l'entrée de l'ingestion : mise à jour perdue ({sorted(publie)})")
    assert publie["doc-gate"]["gate"] is not None
    assert espace.residus() == []


def test_larchive_du_rendu_precedent_se_decide_dans_le_repere_de_la_transaction(
        tmp_path: Path) -> None:
    """Constat 1, second volet : `docs/evals/latest.md` est une cible **couverte**.

    Décider son archivage sur une lecture faite à travers son lien, hors verrou, c'est décider sur
    un état qu'une bascule concurrente peut avoir remplacé entre la décision et le commit : le rendu
    publié entre-temps serait écrasé **sans avoir été archivé** — le défaut même que l'archivage
    existe pour fermer, déplacé d'un cran.

    La sonde donne un repère explicite et vérifie que c'est **celui-là** qui est lu : si `resoudre`
    est ignoré, l'archive porte les octets du lien et non ceux du slot publié.
    """
    from server.evals.publication import _archive_a_ecrire

    latest = tmp_path / "docs" / "evals" / "latest.md"
    latest.parent.mkdir(parents=True)
    latest.write_text("ce que le lien montre\n", encoding="utf-8")
    slot = tmp_path / "slot-publie.md"
    slot.write_text("ce que la transaction publie\n", encoding="utf-8")

    par_defaut = _archive_a_ecrire(latest, repo_root=tmp_path)
    assert par_defaut is not None and par_defaut[1] == "ce que le lien montre\n"

    par_la_transaction = _archive_a_ecrire(latest, repo_root=tmp_path, resoudre=lambda _c: slot)
    assert par_la_transaction is not None, "le repère de la transaction doit être lu"
    assert par_la_transaction[1] == "ce que la transaction publie\n", (
        "l'archivage a été décidé sur le lien plutôt que sur le slot publié")
    assert par_la_transaction[0] != par_defaut[0], (
        "le nom de l'archive dérive du contenu remplacé : deux contenus, deux archives")


def test_un_abandon_qui_ne_peut_pas_sortir_le_brouillon_garde_sa_marque(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Constat 2 : la marque tombait même quand le brouillon n'était pas sorti.

    `mkdtemp` et le `rename` qui sortent le brouillon de son emplacement de génération sont sous
    `except OSError: pass` — à raison, on ne veut rien détruire —, mais le retrait de la marque
    s'exécutait ensuite **inconditionnellement**. Quand la sortie échoue, la génération inactive
    reste donc mêlée sous son nom `a`/`b` — mi-ancien miroir, mi-nouveau lot — et sans marque plus
    rien ne la distingue d'un bundle complet : la garantie « une génération inactive laissée
    partielle est toujours vue » était fausse sur cette branche.
    """
    import tempfile as tempfile_module

    from server.evals import espace as espace_module

    espace = _espace_pose(tmp_path, ("data/manifest.json", "docs/evals/latest.md"),
                          [("data/manifest.json", "v0"), ("docs/evals/latest.md", "l0")])
    cibles = [tmp_path / "data" / "manifest.json", tmp_path / "docs" / "evals" / "latest.md"]
    avant = _etat_observable(cibles)
    active = espace.generation()

    ecrits = {"n": 0}
    vrai_ecrire = espace_module._ecrire_dans_bundle

    def _echouer_au_second_slot(chemin: Path, contenu: str) -> None:
        ecrits["n"] += 1
        if ecrits["n"] == 2:
            raise OSError("panne d'écriture du brouillon")
        vrai_ecrire(chemin, contenu)

    def _mkdtemp_impossible(*_args: object, **_kw: object) -> str:
        raise OSError("pas de place pour la poubelle")

    monkeypatch.setattr(espace_module, "_ecrire_dans_bundle", _echouer_au_second_slot)
    monkeypatch.setattr(tempfile_module, "mkdtemp", _mkdtemp_impossible)

    with pytest.raises(OSError, match="panne d'écriture du brouillon"):
        espace.basculer([(cibles[0], "v1"), (cibles[1], "l1")])

    monkeypatch.undo()
    assert _etat_observable(cibles) == avant
    assert espace.generation() == active
    assert espace.residus() != [], (
        "le brouillon n'a pas pu être sorti et sa marque a quand même été retirée : la génération "
        "inactive mêlée est redevenue indiscernable d'un bundle complet")


def test_une_transaction_ne_publie_quune_fois(tmp_path: Path) -> None:
    """Constat 4 : un second `publier` défaisait le premier commit, en silence.

    `courante` et `suivante` sont figées à la construction de la transaction. Après un commit
    acquis, le pointeur désigne `suivante` — donc un second `publier` reconstruirait cette
    génération-là : `rmtree` sur ce que le pointeur publie (toutes les cibles pendantes le temps du
    miroir), puis republication d'un état bâti sur `courante`, c'est-à-dire un retour à l'état
    d'avant le premier commit, sans la moindre erreur.
    """
    espace = _espace_pose(tmp_path, ("data/manifest.json", "docs/evals/latest.md"),
                          [("data/manifest.json", "v0"), ("docs/evals/latest.md", "l0")])
    manifest = tmp_path / "data" / "manifest.json"
    latest = tmp_path / "docs" / "evals" / "latest.md"

    with espace.transaction() as transaction:
        transaction.publier([(manifest, "v1")])
        with pytest.raises(LotHorsEspace, match="déjà publié"):
            transaction.publier([(latest, "l2")])

    # Le premier commit tient, et le refus n'a rien touché.
    assert manifest.read_text(encoding="utf-8") == "v1"
    assert latest.read_text(encoding="utf-8") == "l0"
    assert manifest.is_symlink() and espace.resolue_dans_lespace(manifest)
    assert espace.residus() == []


def test_la_disposition_suit_le_data_dir_du_run_et_refuse_hors_racine(tmp_path: Path) -> None:
    """Constat 5 : `cibles_du_depot` mêlait deux conventions de chemin.

    Les surfaces de racine portaient un préfixe `data/` **codé en dur** alors que les artefacts de
    document étaient bâtis sur le `data_dir` donné. Sous `--data-dir <autre>`, `--depot` posait donc
    un `<racine>/data/manifest.json` sans rapport avec le run et n'installait jamais
    `<autre>/manifest.json` : l'ingestion suivante refusait en lot mixte, en désignant une cible que
    personne n'écrit.
    """
    autre = tmp_path / "donnees"
    (autre / "contrat").mkdir(parents=True)
    (autre / "contrat" / "source.js").write_text("{}", encoding="utf-8")

    cibles = [str(c) for c in cibles_du_depot(tmp_path, autre)]
    assert "donnees/manifest.json" in cibles and "data/manifest.json" not in cibles
    assert "donnees/dictionary.json" in cibles and "donnees/evals-latest.json" in cibles
    assert "donnees/contrat/document.json" in cibles
    # Les surfaces hors `data/` gardent leur repère : elles ne suivent pas le `--data-dir`.
    assert "docs/evals/latest.md" in cibles

    espace = EspacePublie(tmp_path, autre)
    espace.installer([Path(c) for c in cibles])
    for relatif in cibles:
        assert espace.resolue_dans_lespace(tmp_path / relatif), f"{relatif} hors du pointeur"

    # Un `data_dir` hors de la racine n'a pas de disposition possible : aucun pointeur ne peut
    # couvrir à la fois ses surfaces et celles de `docs/`. C'est dit, pas deviné.
    dehors = tmp_path.parent / f"{tmp_path.name}-dehors"
    dehors.mkdir()
    with pytest.raises(LotHorsEspace, match="hors de la racine"):
        cibles_du_depot(tmp_path, dehors)


def test_un_lot_mixte_et_deux_racines_sont_refuses_avant_toute_ecriture(tmp_path: Path) -> None:
    """Constat 8 : les deux branches de refus de `_espace_du_lot` n'étaient testées nulle part.

    La documentation opérateur affirme que ce refus « se dit avant la production » ; encore
    faut-il qu'il se dise, qu'il nomme la cible manquante et la commande qui la pose, et surtout
    qu'il **ne touche à rien** — un refus qui aurait déjà écrit la moitié couverte du lot serait
    précisément l'état mêlé qu'il existe pour empêcher.
    """
    from server.evals.espace import EspaceNonInstalle
    from server.ingest import artifacts

    espace = _espace_pose(tmp_path, ("data/manifest.json",), [("data/manifest.json", "v0")])
    manifest = tmp_path / "data" / "manifest.json"
    ordinaire = tmp_path / "data" / "contrat" / "document.json"
    ordinaire.parent.mkdir(parents=True)
    ordinaire.write_text("doc-avant", encoding="utf-8")
    avant = _etat_observable([manifest, ordinaire])

    with pytest.raises(EspaceNonInstalle, match="lot mixte") as refus:
        artifacts.publier_artefacts([(manifest, "v1"), (ordinaire, "doc-après")])
    assert "--depot" in str(refus.value), (
        "le refus doit pointer la commande que la documentation opérateur donne")
    assert str(ordinaire) in str(refus.value)
    assert _etat_observable([manifest, ordinaire]) == avant, "le refus a touché une cible"

    # Deux racines distinctes dans un même lot : il n'y a pas de pointeur commun.
    seconde = tmp_path / "ailleurs"
    seconde.mkdir()
    espace2 = _espace_pose(seconde, ("data/manifest.json",), [("data/manifest.json", "w0")])
    manifest2 = seconde / "data" / "manifest.json"
    avant2 = _etat_observable([manifest, manifest2])
    with pytest.raises(LotHorsEspace, match="racines différentes"):
        artifacts.publier_artefacts([(manifest, "v1"), (manifest2, "w1")])
    assert _etat_observable([manifest, manifest2]) == avant2
    assert espace.residus() == [] and espace2.residus() == []


def test_une_interruption_juste_apres_un_rename_est_rattrapee_par_le_retablissement(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Constat 3 : le rang était noté **après** l'appel système, donc parfois jamais.

    `tmp.replace(chemin)` puis `faites.append(chemin)` : `KeyboardInterrupt` peut tomber à cette
    frontière d'instruction précise. La cible est alors publiée et absente de la liste des rangs à
    rétablir — une cible modifiée après une exception propagée, ce que l'AC interdit littéralement,
    `BaseException` comprise.

    La sonde injecte exactement cela : le `rename` **réussit**, puis l'interruption est levée. Noter
    le rang avant l'appel est ce qui la rattrape ; l'asymétrie le justifie — rétablir une cible qui
    n'a pas bougé lui réécrit ses propres octets, l'inverse la laisse publiée.
    """
    from server.ingest import artifacts

    cibles = [tmp_path / nom for nom in ("document.json", "report.json", "manifest.json")]
    for index, cible in enumerate(cibles):
        cible.write_text(f"avant-{index}", encoding="utf-8")
    avant = _etat_observable(cibles)

    appels = {"n": 0}
    vrai_replace = artifacts.os.replace

    def _reussir_puis_interrompre(source: object, cible: object) -> None:
        appels["n"] += 1
        vrai_replace(source, cible)  # type: ignore[arg-type]
        if appels["n"] == 1:
            # L'interruption tombe **après** le renommage réussi : la cible est publiée, et c'est
            # tout l'enjeu — le rang doit déjà être noté.
            raise KeyboardInterrupt("interruption juste après le rename")

    monkeypatch.setattr(artifacts.os, "replace", _reussir_puis_interrompre)

    with pytest.raises(KeyboardInterrupt, match="juste après le rename"):
        artifacts.publier_artefacts([(cible, f"après-{index}")
                                     for index, cible in enumerate(cibles)])

    monkeypatch.undo()
    assert _etat_observable(cibles) == avant, (
        "une cible est restée publiée après une exception propagée : le rang n'était pas noté")
    assert sorted(p.name for p in tmp_path.rglob("*") if p.name.endswith(".tmp")) == []


# --- N1 : une lecture pince **une** génération, et lit tout à travers elle ------------------------

def test_un_repere_de_lecture_ne_resout_courant_quune_seule_fois(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """N1 : le repère résout `courant` **une fois**, puis ne le relit plus jamais.

    Avant ce tour, il n'existait aucune API de lecture : toute voie relisait le pointeur à *chaque*
    appel système — un `readlink` par cible. Une bascule tombant entre deux de ces résolutions
    rendait un état composé de deux générations. Ici on compte les résolutions : une seule, quel que
    soit le nombre de cibles lues.
    """
    from server.app.corpus import racine as rac

    espace = _espace_pose(tmp_path, ("a.md", "b.md"), [("a.md", "un"), ("b.md", "deux")])
    assert espace.generation() in GENERATIONS

    lectures = {"n": 0}
    vrai = rac.lire_pointeur

    def _compter(chemin: Path) -> str:
        lectures["n"] += 1
        return vrai(chemin)

    monkeypatch.setattr(rac, "lire_pointeur", _compter)
    repere = rac.lecture_de(espace.data_dir)
    try:
        assert repere.texte(tmp_path / "a.md") == "un"
        assert repere.texte(tmp_path / "b.md") == "deux"
        assert repere.fichier(tmp_path / "a.md")
    finally:
        repere.fermer()
    assert lectures["n"] == 1, (
        f"{lectures['n']} résolutions de `courant` pour une seule passe de lecture — une "
        "opération de lecture ne doit en faire qu'une")


def test_un_repere_pince_survit_a_une_bascule_concurrente(tmp_path: Path) -> None:
    """N1 : la génération pincée reste lisible pendant qu'une autre est publiée.

    Deux générations alternent : celle qu'un lecteur a pincée devient inactive à la bascule
    suivante, mais elle n'est reconstruite qu'à la **seconde**. Un lecteur pincé traverse donc
    entièrement une bascule, et rend un état d'une seule génération — jamais un mélange.
    """
    from server.app.corpus import racine as rac

    espace = _espace_pose(tmp_path, ("a.md", "b.md"), [("a.md", "v1"), ("b.md", "v1")])
    repere = rac.lecture_de(espace.data_dir)
    try:
        assert repere.texte(tmp_path / "a.md") == "v1"
        espace.basculer([(tmp_path / "a.md", "v2"), (tmp_path / "b.md", "v2")])
        # Le lien vivant montre déjà `v2` ; le repère, lui, tient sa génération.
        assert (tmp_path / "a.md").read_text("utf-8") == "v2"
        assert repere.texte(tmp_path / "b.md") == "v1", (
            "le repère a suivi la bascule : la passe mêlerait deux générations")
        assert not repere.perimee()
        # Seconde bascule : la génération pincée est reconstruite, et le repère le **dit**.
        espace.basculer([(tmp_path / "a.md", "v3"), (tmp_path / "b.md", "v3")])
        assert repere.perimee(), "une génération reconstruite sous le repère doit être détectée"
    finally:
        repere.fermer()


def test_relire_rejoue_la_passe_quand_le_repere_a_ete_perime(tmp_path: Path) -> None:
    """N1 : deux bascules pendant une passe ⇒ la passe est **rejouée**, jamais rendue mêlée."""
    from server.app.corpus import racine as rac

    espace = _espace_pose(tmp_path, ("a.md",), [("a.md", "v1")])
    passes: list[str | None] = []

    def _passe(lecture: rac.Lecture) -> str | None:
        valeur = lecture.texte(tmp_path / "a.md")
        passes.append(valeur)
        if len(passes) == 1:  # deux bascules pendant la première passe : le repère est périmé
            espace.basculer([(tmp_path / "a.md", "v2")])
            espace.basculer([(tmp_path / "a.md", "v3")])
        return valeur

    assert rac.relire(espace.data_dir, _passe) == "v3"
    assert passes == ["v1", "v3"], passes


def test_le_repere_ne_touche_le_pointeur_quune_fois_quel_que_soit_le_nombre_de_cibles(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """N1, mesuré sur les **appels système**, pas sur une fonction de commodité.

    Revue du tour N1–N3, constat 2. La sonde voisine compte les appels à `rac.lire_pointeur` ; c'est
    une preuve trop faible, parce qu'elle ne voit pas les résolutions qui passent **à côté** de cette
    fonction. Et il y en avait : `couverte` faisait un `os.path.realpath` par cible et par
    génération, chacun retraversant `courant`, si bien qu'un repère « qui résout `courant` une seule
    fois » le résolvait une fois **par lecture** — mesuré 4 pour 3 lectures. Une sonde qui ne peut
    pas échouer pour la raison qu'elle énonce ne prouve rien.

    Ici on compte toute traversée du pointeur, quelle que soit la primitive employée : `os.readlink`,
    `os.path.realpath` et `os.stat` visant `<espace>/courant`. Le nombre attendu est **1**, celui du
    pincement, et il ne dépend pas du nombre de cibles lues.
    """
    from server.app.corpus import racine as rac

    espace = _espace_pose(tmp_path, ("a.md", "b.md", "c.md"),
                          [("a.md", "un"), ("b.md", "deux"), ("c.md", "trois")])
    pointeur = str(espace.chemin / "courant")
    touches: list[str] = []

    def _mouchard(nom: str, vrai: Any) -> Any:
        def _appel(chemin: Any, *args: Any, **kwargs: Any) -> Any:
            if str(chemin) == pointeur:
                touches.append(nom)
            return vrai(chemin, *args, **kwargs)
        return _appel

    for module, nom in ((os, "readlink"), (os, "stat"), (os.path, "realpath")):
        monkeypatch.setattr(module, nom, _mouchard(nom, getattr(module, nom)))

    repere = rac.lecture_de(espace.data_dir)
    try:
        assert repere.texte(tmp_path / "a.md") == "un"
        assert repere.texte(tmp_path / "b.md") == "deux"
        assert repere.texte(tmp_path / "c.md") == "trois"
        assert repere.fichier(tmp_path / "a.md")
    finally:
        repere.fermer()
    monkeypatch.undo()

    assert touches == ["readlink"], (
        f"{len(touches)} traversées de `courant` ({touches}) pour une seule passe : le repère "
        "doit résoudre le pointeur au pincement et ne plus jamais y toucher")


def test_une_cible_couverte_dont_le_lien_est_casse_refuse_au_lieu_de_lire_le_lien_vivant(
        tmp_path: Path) -> None:
    """N1 : un lecteur qui ne peut pas conclure **refuse** ; il ne retombe pas sur le chemin brut.

    Revue du tour N1–N3, constat 2, second volet. Quand la résolution d'une cible couverte échouait
    — lien remplacé par un fichier ordinaire, disposition reposée pendant la passe —, `reel` rendait
    le **chemin brut**, c'est-à-dire une lecture à travers le lien vivant, hors de la génération
    pincée : exactement le mélange que le repère existe pour interdire, et en silence. Le slot
    existe pourtant dans la génération pincée : la racine *connaît* ce chemin, donc son absence de
    couverture est une disposition cassée, pas un artefact hors bundle.
    """
    from server.app.corpus import racine as rac

    espace = _espace_pose(tmp_path, ("a.md",), [("a.md", "publie")])
    repere = rac.lecture_de(espace.data_dir)
    try:
        assert repere.texte(tmp_path / "a.md") == "publie"
        # La disposition est cassée sous le lecteur : le lien devient un fichier ordinaire.
        (tmp_path / "a.md").unlink()
        (tmp_path / "a.md").write_text("hors génération", "utf-8")
        with pytest.raises(rac.LectureHorsGeneration, match="ne passe plus par le pointeur"):
            repere.texte(tmp_path / "a.md")
    finally:
        repere.fermer()


def test_un_courant_designant_une_generation_absente_est_illisible_pas_vide(
        tmp_path: Path) -> None:
    """Un espace qu'on ne sait pas lire **se dit** — il ne se lit pas comme un espace vide.

    Revue du tour N1–N3, constat 6. Quand `courant` nommait une génération valide dont le répertoire
    était absent ou illisible, chaque slot était vu absent : `load_corpus`, le smoke et le typage
    rendaient alors un état **vide sans refuser**. Un corpus vide et un corpus illisible ne sont pas
    le même fait, et seul le second doit fermer.
    """
    from server.app.corpus import racine as rac

    espace = _espace_pose(tmp_path, ("a.md",), [("a.md", "v1")])
    generation = espace.generation()
    shutil.rmtree(espace.chemin / generation)

    with pytest.raises(rac.EspaceIllisible, match="répertoire est absent ou illisible"):
        rac.lecture_de(espace.data_dir)


def test_relire_epuise_ses_essais_et_dit_le_refus(tmp_path: Path) -> None:
    """N1 : après épuisement des tentatives, le refus est **dit**, jamais un état rendu quand même.

    C'est la contrepartie du rejeu : sous une production qui bascule assez vite pour périmer chaque
    passe, rendre un état serait affirmer une cohérence qu'aucune génération ne porte. Le message de
    ce refus n'était exercé par aucune sonde.
    """
    from server.app.corpus import racine as rac

    espace = _espace_pose(tmp_path, ("a.md",), [("a.md", "v1")])
    passes: list[str | None] = []

    def _passe(lecture: rac.Lecture) -> str | None:
        valeur = lecture.texte(tmp_path / "a.md")
        passes.append(valeur)
        # Deux bascules à **chaque** passe : le repère est périmé quoi qu'il arrive.
        espace.basculer([(tmp_path / "a.md", f"v{len(passes)}a")])
        espace.basculer([(tmp_path / "a.md", f"v{len(passes)}b")])
        return valeur

    with pytest.raises(rac.LecturePerimee, match="chacune des 3 tentatives"):
        rac.relire(espace.data_dir, _passe)
    assert len(passes) == rac.ESSAIS_DE_LECTURE, passes


# --- N2 : la marque du brouillon, dans les deux sens ----------------------------------------------

def test_une_marque_impossible_a_poser_refuse_avant_toute_mutation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """N2 : la marque n'est plus best-effort **à la pose**.

    `marquer()` était intégralement enveloppée dans `contextlib.suppress(OSError)` et ne rendait
    rien : `publier` enchaînait sur `_reconstruire` sans jamais savoir si la marque existait. Un
    `ENOSPC` avalé, puis un `rmtree`/`mkdir` de la génération inactive, puis une exception au rang N
    laissaient un `a`/`b` **partiel sous son nom canonique**, sans marque, sans nom en `.tmp` — et
    `residus()` rendait `[]`.
    """
    from server.evals import espace as esp

    espace = _espace_pose(tmp_path, ("a.md",), [("a.md", "v1")])
    generation = espace.generation()
    inactive = espace.chemin / (GENERATIONS[1] if generation == GENERATIONS[0] else GENERATIONS[0])
    inactive.mkdir(exist_ok=True)
    (inactive / "temoin.txt").write_text("intact", encoding="utf-8")

    vrai_open = esp.os.open

    def _refuser_la_marque(chemin: Any, *a: object, **k: object) -> int:
        # **Seule la marque** échoue : le verrou, lui, s'ouvre normalement. Sans cette précision, la
        # sonde passerait pour la mauvaise raison — un `flock` impossible à prendre refuse déjà.
        if ".brouillon." in str(chemin):
            raise OSError(28, "No space left on device")
        return vrai_open(chemin, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(esp.os, "open", _refuser_la_marque)
    with pytest.raises(OSError, match="No space left"):
        espace.basculer([(tmp_path / "a.md", "v2")])
    monkeypatch.undo()

    assert (tmp_path / "a.md").read_text("utf-8") == "v1", "la cible a bougé malgré le refus"
    assert (inactive / "temoin.txt").read_text("utf-8") == "intact", (
        "la génération inactive a été mutée alors que la marque n'a pas pu être posée")


def test_un_brouillon_reste_marque_jusquau_commit_etabli(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """N2 : la marque tombait **entre** le `fsync` du brouillon et l'atome.

    Une exception dans cette fenêtre — préparation du lien temporaire, `symlink`, interruption —
    laissait la génération inactive écrite, complète pour un lot mais périmée pour le pointeur, et
    **sans marque**. La sonde de résidus ne voyait alors rien.
    """
    from server.evals import espace as esp

    espace = _espace_pose(tmp_path, ("a.md",), [("a.md", "v1")])
    vrai_open = esp.os.open
    poses = {"n": 0}

    def _une_seule_pose(chemin: Any, *a: object, **k: object) -> int:
        # La **première** pose réussit (celle d'avant la première mutation) ; toute pose ultérieure
        # échoue. C'est la conjonction exacte que la cartographie a mesurée : une marque retirée
        # trop tôt, puis une repose best-effort qui échoue en silence, laissait une génération
        # inactive partielle **sous son nom canonique**, sans marque et sans nom en `.tmp`.
        if ".brouillon." in str(chemin):
            poses["n"] += 1
            if poses["n"] > 1:
                raise OSError(13, "Permission denied")
        return vrai_open(chemin, *a, **k)  # type: ignore[arg-type]

    def _echouer(*_a: object, **_k: object) -> None:
        raise RuntimeError("panne juste avant l'atome")

    monkeypatch.setattr(esp.os, "open", _une_seule_pose)
    monkeypatch.setattr(esp.os, "symlink", _echouer)
    # Le pointeur est rendu indécidable pour que l'abandon prudent renonce à détruire : c'est le cas
    # exact du fait 4, et c'est là que la marque doit encore être présente.
    monkeypatch.setattr(esp.EspacePublie, "_generation_publiee", lambda self: None)
    with pytest.raises(RuntimeError, match="juste avant l'atome"):
        espace.basculer([(tmp_path / "a.md", "v2")])
    monkeypatch.undo()

    assert (tmp_path / "a.md").read_text("utf-8") == "v1"
    restes = espace.residus()
    assert any(".brouillon." in reste for reste in restes), (
        f"le brouillon laissé avant l'atome n'est pas vu par la sonde : {restes}")


def test_une_marque_perimee_cesse_detre_signalee_apres_des_bascules_saines(
        tmp_path: Path) -> None:
    """N2, **dans l'autre sens** : un bundle complet n'est jamais un brouillon.

    La marque est nommée par pid et n'était retirée que par la transaction qui l'avait posée : un
    processus tué en cours de brouillon en laissait une que **rien** ne moissonnait. Sondé, après
    deux bascules parfaitement saines — qui `rmtree`ent et republient la génération qu'elle
    nomme —, `residus()` la rendait encore, et désignait comme « brouillon en cours » la génération
    que le pointeur publie. Fermer le faux négatif en ouvrant un faux positif permanent n'aurait
    rien fermé.
    """
    espace = _espace_pose(tmp_path, ("a.md",), [("a.md", "v1")])
    inactive = GENERATIONS[1] if espace.generation() == GENERATIONS[0] else GENERATIONS[0]
    crash = espace.chemin / f".{inactive}.brouillon.99999.tmp"
    crash.write_text("", encoding="utf-8")
    assert espace.residus() == [crash.name], "la marque d'un processus disparu doit d'abord se voir"

    espace.basculer([(tmp_path / "a.md", "v2")])
    espace.basculer([(tmp_path / "a.md", "v3")])

    assert espace.residus() == [], (
        "une marque a survécu à la reconstruction complète de la génération qu'elle nomme : elle "
        "désigne désormais un bundle publié comme un brouillon en cours")
    assert (tmp_path / "a.md").read_text("utf-8") == "v3"


# --- Patch croisé 1/3 : absorber n'est pas taire -------------------------------------------------

def test_un_assainissement_impossible_refuse_avant_le_commit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`N2-NETTOYAGE-MUET`, volet **avant** commit : lever laisse zéro cible modifiée.

    Le moissonnage des marques périmées absorbait l'itération **et** chaque `unlink`, puis la
    transaction continuait. Une marque qu'on n'a pas pu retirer survivait alors à la reconstruction
    de sa génération et, une fois celle-ci publiée, `residus()` la filtrait — ni dite, ni
    observable. Avant le commit rien n'est basculé : le refus est donc gratuit, et c'est exactement
    ce que l'AC demande d'une exception d'avant commit.
    """
    from server.evals import espace as esp

    espace = _espace_pose(tmp_path, ("a.md",), [("a.md", "v1")])
    avant = _etat_observable([tmp_path / "a.md"])
    inactive = GENERATIONS[1] if espace.generation() == GENERATIONS[0] else GENERATIONS[0]
    perimee = espace.chemin / f".{inactive}.brouillon.99999.tmp"
    perimee.write_text("marque d'un processus disparu", "utf-8")

    vrai_unlink = Path.unlink

    def _unlink_impossible(self: Path, **kw: object) -> None:
        if self.name.endswith(".brouillon.99999.tmp"):
            raise PermissionError("marque périmée verrouillée")
        return vrai_unlink(self, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", _unlink_impossible)
    with pytest.raises(esp.EspaceIllisible, match="impossible à retirer"):
        espace.basculer([(tmp_path / "a.md", "v2")])
    monkeypatch.undo()

    assert _etat_observable([tmp_path / "a.md"]) == avant, (
        "une cible a bougé alors que l'assainissement avait refusé avant le commit")
    assert perimee.exists(), "la marque qu'on n'a pas su retirer doit rester détectable"
    assert any("brouillon.99999" in reste for reste in espace.residus()), espace.residus()


def test_un_nettoyage_impossible_apres_le_commit_est_dit_et_reste_observable(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """`N2-NETTOYAGE-MUET`, volet **après** commit : rien ne remonte, mais rien n'est tu.

    Après le commit, l'AC interdit de propager quoi que ce soit — le lot est publié. Mais absorber
    n'est pas taire : la marque qui subsiste nomme désormais la génération que le pointeur
    **publie**, donc `residus()` la filtre à raison (un bundle publié n'est pas un brouillon), et
    l'impossibilité devenait invisible. Elle laisse maintenant une trace d'un **autre nom**, que la
    sonde de résidus ne filtre jamais, et un mot sur `stderr`.
    """
    espace = _espace_pose(tmp_path, ("a.md",), [("a.md", "v1")])
    vrai_unlink = Path.unlink

    def _retrait_impossible(self: Path, **kw: object) -> None:
        if ".brouillon." in self.name:
            raise PermissionError("marque impossible à retirer après le commit")
        return vrai_unlink(self, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", _retrait_impossible)
    espace.basculer([(tmp_path / "a.md", "v2")])  # ne lève pas : le lot est publié
    monkeypatch.undo()

    assert (tmp_path / "a.md").read_text("utf-8") == "v2", "le lot devait être publié"
    erreur = capsys.readouterr().err
    assert "nettoyage impossible après le commit" in erreur, erreur
    traces = [reste for reste in espace.residus() if "nettoyage-impossible" in reste]
    assert traces, (
        f"l'impossibilité de nettoyage n'est pas observable : residus() = {espace.residus()}")
