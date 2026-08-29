"""L'espace de publication : disposition statique, bundle immuable, unique pointeur atomique.

Story 4.5, B7. Les contre-sondes de l'invariant lui-même vivent dans `tests/test_publication_evals.py`,
là où vivaient déjà celles de la story. Ce fichier éprouve ce qui les rend possibles : la
disposition, ses refus, son report en avant, et le fait qu'elle soit réellement posée **dans le
dépôt** — parce qu'une disposition qu'un commit pourrait défaire en silence ne garantit rien.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from server.app.config import REPO_ROOT
from server.evals import run as runner
from server.evals.espace import (GENERATIONS, EspaceIllisible, EspaceNonInstalle, EspacePublie,
                                 LotHorsEspace)
from server.evals.revision import sorties_du_run

# Les cibles que le dépôt doit porter en liens statiques committés. `.evals/` n'y est pas : il est
# ignoré par git, et la CI pose ses liens elle-même (`.github/workflows/ci.yml`).
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
