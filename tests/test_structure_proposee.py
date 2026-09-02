"""Story 4.2c — le modèle propose une structure sur des lignes source, le code la prouve ou la refuse.

Trois preuves séparées, aucune n'appelant le réseau :

1. **L'immuabilité** — le registre conserve uid, texte, page, bbox et ordre source, et chaque ligne
   est soit portée par exactement un bloc, soit retirée sous un motif explicite.
2. **La surface** — la charge utile n'offre aucun champ de texte réinscriptible, le schéma n'admet
   ni `kind`, ni portée, ni verdict, et le faux client ne répond qu'à partir des uid reçus.
3. **Le rejet** — chaque famille invalide rend un refus **nommé**, `build_document` lève et
   `run()` met le document en quarantaine avec les artefacts périmés purgés.

Le corpus est synthétique et neutre : `S1…`, `T1…`, aucun assureur, aucun document réel.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.helpers_espace import poser_espace
from pydantic import ValidationError

from server.app.config import Settings, get_settings
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.domain import Document
from server.ingest import pdf_to_blocks as p
from server.ingest import pdf_structure_gate as structure_gate
from server.ingest import structure as s

DOC = "doc-structure"


def _ligne(text: str, y: float, *, x: float = 56.0, uid: str | None = None,
           size: float = 10.0) -> p.PageLine:
    return p.PageLine(text, [x, y, x + 300.0, y + 12.0], size,
                      source_uids=[] if uid is None else [uid])


def _page(page_no: int, textes: list[str], *, depart: float = 100.0) -> p.PageText:
    """Page synthétique **avec son registre** : l'idiome direct, mais registre compris (4.2c)."""
    registre = s and p.SourceRegistry()
    lines = []
    for index, text in enumerate(textes):
        bbox = [56.0, depart + index * 20.0, 356.0, depart + index * 20.0 + 12.0]
        source = registre.add(page=page_no, text=text, bbox=bbox)
        lines.append(p.PageLine(text, bbox, 10.0, source_uids=[source.uid]))
    return p.PageText(page=page_no, width=595, height=842, lines=lines, source=registre)


def _corpus() -> list[p.PageText]:
    """Deux sections de premier niveau, la première avec une sous-section."""
    return [
        _page(1, ["S1 Titre de la premiere section",
                  "Corps de la premiere section.",
                  "S1a Titre de la sous-section",
                  "Corps de la sous-section."]),
        _page(2, ["S2 Titre de la seconde section",
                  "Corps de la seconde section.",
                  "Suite du corps de la seconde section."]),
    ]


def _registre(pages: list[p.PageText]) -> dict[str, s.Entree]:
    p.ordonner_pages(pages)
    return s.registre_lignes(pages)


def _proposition(**remplacements: Any) -> s.StructureProposee:
    """Proposition valide sur le corpus ; les tests n'en changent qu'un trait à la fois."""
    noeuds = [
        s.NoeudPropose(titre_line_uid="p1:l1", premiere_line_uid="p1:l1", derniere_line_uid="p1:l4"),
        s.NoeudPropose(titre_line_uid="p1:l3", premiere_line_uid="p1:l3", derniere_line_uid="p1:l4",
                       parent_line_uid="p1:l1"),
        s.NoeudPropose(titre_line_uid="p2:l1", premiere_line_uid="p2:l1", derniere_line_uid="p2:l3"),
    ]
    return s.StructureProposee(schema_version="1", doc_id=remplacements.get("doc_id", DOC),
                               noeuds=remplacements.get("noeuds", noeuds))


def _verdict(proposition: s.StructureProposee, pages: list[p.PageText] | None = None) -> s.Verdict:
    pages = pages or _corpus()
    return s.verifier(proposition, _registre(pages), doc_id=DOC, settings=get_settings())


# --- 1. Immuabilité du registre -----------------------------------------------------------------

def test_le_registre_conserve_uid_texte_page_bbox_et_ordre_source() -> None:
    """AC : chaque `SourceLine` traverse l'ingestion inchangée, et son uid ne dérive d'aucun bloc."""
    pages = _corpus()
    figees = [(line.uid, line.text, line.page, line.bbox, line.ordre)
              for page in pages for line in page.source.lines]
    p.build_document(pages, edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC, title="Contrat")
    assert [(line.uid, line.text, line.page, line.bbox, line.ordre)
            for page in pages for line in page.source.lines] == figees
    assert [uid for uid, *_ in figees][:4] == ["p1:l1", "p1:l2", "p1:l3", "p1:l4"]
    with pytest.raises(Exception):  # `frozen=True` : le registre n'est pas un tampon de travail
        pages[0].source.lines[0].text = "autre"  # type: ignore[misc]


def test_chaque_ligne_source_est_portee_par_un_bloc_ou_par_un_motif_de_retrait(tmp_path: Path) -> None:
    """AC : union complète, intersection vide, sur un PDF réellement extrait (bandes et table comprises)."""
    from tests.test_pdf_to_blocks import build_pdf, nominal_pages

    dossier = tmp_path / "data" / DOC
    dossier.mkdir(parents=True)
    build_pdf(dossier / "source.pdf", pages=nominal_pages())
    pages, toc = p.extract_pages(dossier / "source.pdf")
    _document, meta = p.build_document(pages, edition="2026", source_hash="0" * 64, toc=toc,
                                       doc_id=DOC, title="Contrat")
    assert p.anomalies_registre(pages, meta["source_uids"]) == []
    portees = {uid for uids in meta["source_uids"].values() for uid in uids}
    retirees = {uid for page in pages for uid in page.source.removed}
    toutes = {line.uid for page in pages for line in page.source.lines}
    assert portees | retirees == toutes and portees & retirees == set()
    assert "bande_recurrente" in set().union(*(set(page.source.removed.values()) for page in pages))
    # Le registre reste interne : il ne traverse ni `Document`, ni `document.json` (AD-2 inchangé).
    assert "source_uids" not in json.dumps(Document.model_json_schema())


def test_une_ligne_fusionnee_porte_les_deux_uid_de_ses_sources() -> None:
    """`_merge_number_lines` réunit deux lignes : le registre n'en perd aucune."""
    registre = p.SourceRegistry()
    numero = registre.add(page=1, text="1.4", bbox=[56.0, 100.0, 76.0, 112.0])
    suite = registre.add(page=1, text="Intitulé", bbox=[122.0, 100.0, 300.0, 112.0])
    lignes = [p.PageLine("1.4", [56.0, 100.0, 76.0, 112.0], 10.0, source_uids=[numero.uid]),
              p.PageLine("Intitulé", [122.0, 100.0, 300.0, 112.0], 10.0, source_uids=[suite.uid])]
    fusionnees = p._merge_number_lines(lignes)
    assert len(fusionnees) == 1 and fusionnees[0].source_uids == [numero.uid, suite.uid]


def test_une_ligne_de_table_reste_au_registre_et_est_confiee_a_sa_table() -> None:
    """Une ligne de cellule n'est pas « retirée » : son contenu est servi par le bloc `table`.

    L'inscrire sous un motif de retrait laissait `anomalies_registre` valider une ligne **servie sans
    liaison ligne/bloc** — la bijection contournée par le seul chemin qui la contourne.
    """
    class TextPage:
        @staticmethod
        def get_text(kind: str, **options: Any) -> dict[str, Any]:
            return {"blocks": [{"type": 0, "lines": [{
                "bbox": [50, 0, 100, 10],
                "spans": [{"text": "cellule atomique", "font": "helv", "size": 10}],
            }]}]}

    registre = p.SourceRegistry()
    table = p.PageTable(bbox=[40, 0, 110, 10], rows=[["cellule atomique"]])
    lignes, _ = p._raw_lines(TextPage(), page_no=3, tables=[table], registry=registre)
    assert lignes == [] and [line.uid for line in registre.lines] == ["p3:l1"]
    assert table.source_uids == ["p3:l1"] and registre.removed == {}


def _dossier_avec_table(tmp_path: Path) -> Path:
    """Un PDF synthétique dont `find_tables()` détecte réellement la table (grille tracée)."""
    import pymupdf

    from tests.test_pdf_to_blocks import FONT_BODY, FONT_TITLE, _write_sha

    dossier = tmp_path / "data" / DOC
    dossier.mkdir(parents=True)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((56, 70), "1", fontsize=17, fontname=FONT_TITLE)
    page.insert_text((122, 70), "Rubriques", fontsize=17, fontname=FONT_TITLE)
    for x in (50, 200, 350):
        page.draw_line((x, 100), (x, 180))
    for y in (100, 140, 180):
        page.draw_line((50, y), (350, y))
    for x, y, texte in ((60, 125, "Rubrique une"), (210, 125, "Valeur une"),
                        (60, 165, "Rubrique deux"), (210, 165, "Valeur deux")):
        page.insert_text((x, y), texte, fontsize=10, fontname=FONT_BODY)
    page.insert_text((56, 220), "Ligne de corps sous la table.", fontsize=10, fontname=FONT_BODY)
    doc.save(str(dossier / "source.pdf"))
    doc.close()
    _write_sha(dossier / "source.pdf")
    (dossier / "source.url").write_text("https://example.test/contrat.pdf\n", "utf-8")
    return dossier


def test_chaque_ligne_source_dune_table_extraite_est_portee_une_fois_par_son_bloc(
        tmp_path: Path) -> None:
    """AC : bijection lignes/blocs — la table sert son contenu, elle doit donc porter ses lignes.

    `_segment_page` sert les rangées d'un bloc `table` synthétique construit depuis `table.rows` ;
    compter ces lignes « retirées » revenait à servir du texte qu'aucun bloc ne réclamait, et à les
    soustraire au registre présenté au proposant.
    """
    dossier = _dossier_avec_table(tmp_path)
    pages, toc = p.extract_pages(dossier / "source.pdf")
    table = pages[0].tables[0]
    assert len(table.source_uids) >= 2  # les cellules extraites, pas les rangées reconstruites
    document, meta = p.build_document(pages, edition="2026", source_hash="0" * 64, toc=toc,
                                      doc_id=DOC, title="Contrat")
    bloc = next(block for block in document.blocks if block.kind == "table")
    portees = meta["source_uids"][bloc.block_id]
    assert portees == table.source_uids  # une fois, et une seule, dans l'ordre d'extraction
    assert len(set(portees)) == len(portees)
    assert not set(table.source_uids) & set(pages[0].source.removed)  # servi ⇒ jamais « retiré »
    assert p.anomalies_registre(pages, meta["source_uids"]) == []
    # Et elles sont proposables, à la position de la table dans l'ordre de lecture.
    registre = _registre(pages)
    ordres = [registre[uid].ordre for uid in table.source_uids]
    assert ordres == list(range(ordres[0], ordres[0] + len(ordres)))
    assert registre[table.source_uids[0]].ordre > registre["p1:l1"].ordre


def _page_avec_table(rows: list[list[str]]) -> tuple[p.PageText, list[p.SourceLine]]:
    """Un titre, une table qui a absorbé deux lignes brutes, puis un corps — registre compris."""
    registre = p.SourceRegistry()
    entete = registre.add(page=1, text="Intitule au-dessus de la table.", bbox=[56.0, 60.0, 356.0, 72.0])
    cellules = [registre.add(page=1, text=f"Cellule {rang}", bbox=[60.0, 100.0 + rang * 20.0,
                                                                  190.0, 112.0 + rang * 20.0])
                for rang in (1, 2)]
    corps = registre.add(page=1, text="Ligne de corps sous la table.", bbox=[56.0, 200.0, 356.0, 212.0])
    table = p.PageTable(bbox=[50.0, 90.0, 350.0, 180.0], rows=rows,
                        source_uids=[cellule.uid for cellule in cellules])
    lignes = [p.PageLine(source.text, list(source.bbox), 10.0, source_uids=[source.uid])
              for source in (entete, corps)]
    page = p.PageText(page=1, width=595, height=842, lines=lignes, tables=[table], source=registre)
    return page, [entete, *cellules, corps]


def test_une_table_scindee_entre_deux_noeuds_proposes_est_refusee() -> None:
    """Le bloc `table` est atomique : il ne peut pas être servi à cheval sur deux nœuds prouvés.

    Une table est une **unité de portage** au même titre qu'une ligne fusionnée : le bloc `table`
    porte toutes ses lignes absorbées d'un seul tenant. La même règle générique les couvre donc, et
    le refus est rendu **par le vérificateur** — l'écrire seulement dans `build_document` laissait la
    CLI produire un `structure.json` que l'ingestion ne pourrait jamais accepter.
    """
    page, sources = _page_avec_table([["Cellule 1"], ["Cellule 2"]])
    entete, premiere, seconde, corps = sources
    registre = _registre([page])
    assert [registre[source.uid].ordre for source in sources] == [1, 2, 3, 4]  # table à sa place
    entiere = _proposition(noeuds=[
        s.NoeudPropose(titre_line_uid=entete.uid, premiere_line_uid=entete.uid,
                       derniere_line_uid=corps.uid),
    ])
    assert s.verifier(entiere, registre, doc_id=DOC, settings=get_settings()).accepte
    document, meta = p.build_document([page], edition="2026", source_hash="0" * 64, toc=[],
                                      doc_id=DOC, title="Contrat", structure=entiere)
    bloc = next(block for block in document.blocks if block.kind == "table")
    assert meta["source_uids"][bloc.block_id] == [premiere.uid, seconde.uid]
    assert bloc.block_id in {node.node_id: node for node in document.nodes}[f"{DOC}:s1"].blocks

    scindee = _proposition(noeuds=[
        s.NoeudPropose(titre_line_uid=entete.uid, premiere_line_uid=entete.uid,
                       derniere_line_uid=premiere.uid),
        s.NoeudPropose(titre_line_uid=seconde.uid, premiere_line_uid=seconde.uid,
                       derniere_line_uid=corps.uid),
    ])
    verdict = s.verifier(scindee, registre, doc_id=DOC, settings=get_settings())
    assert not verdict.accepte and verdict.motif == "affectation_non_prouvee"
    assert "unité de portage" in verdict.detail
    with pytest.raises(s.StructureRefusee) as capture:
        p.build_document([page], edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC,
                         title="Contrat", structure=scindee)
    assert capture.value.motif == "affectation_non_prouvee"
    assert capture.value.motif in s.MOTIFS


def test_le_groupe_a_cheval_reste_refuse_au_build_meme_si_le_verificateur_est_contourne() -> None:
    """La garde de `_noeud_de_groupe` demeure : elle ne dépend pas du vérificateur pour exister.

    Le refus à l'acceptation ne remplace pas celui du build — il le précède. Un appel programmatique
    direct, un chemin futur, une proposition bâtie hors du modèle : le bloc servi doit rester refusé
    ligne à ligne.
    """
    lignes = [p.PageLine("Une rangee | Une valeur", [50.0, 90.0, 350.0, 180.0], 0.0,
                         source_uids=["p1:l2", "p1:l3"])]
    with pytest.raises(s.StructureRefusee) as capture:
        p._noeud_de_groupe(lignes, {"p1:l2": f"{DOC}:s1", "p1:l3": f"{DOC}:s2"}, page=1)
    assert capture.value.motif == "affectation_non_prouvee" and "à cheval" in capture.value.detail
    with pytest.raises(s.StructureRefusee) as vide:
        p._noeud_de_groupe(lignes, {"p9:l1": f"{DOC}:s1"}, page=1)
    assert "aucune ligne source prouvée" in vide.value.detail


def _page_avec_ligne_fusionnee() -> tuple[p.PageText, list[p.SourceLine]]:
    """Une page dont l'extracteur a scindé un intitulé — « 1.4 » puis « Objet … » — puis réuni.

    La ligne de travail porte donc **deux** uid : c'est une *unité de portage*, l'ensemble des lignes
    source qu'un même bloc portera nécessairement ensemble. L'autre cas est la table, dont le bloc
    `table` porte toutes les lignes absorbées ; une seule règle les couvre.
    """
    registre = p.SourceRegistry()
    avant = registre.add(page=1, text="Avant la section.", bbox=[56.0, 60.0, 356.0, 72.0])
    numero = registre.add(page=1, text="1.4", bbox=[56.0, 100.0, 76.0, 112.0])
    intitule = registre.add(page=1, text="Objet de la section", bbox=[122.0, 100.0, 300.0, 112.0])
    apres = registre.add(page=1, text="Corps de la section.", bbox=[56.0, 140.0, 356.0, 152.0])
    fusionnee = p.PageLine("1.4 Objet de la section", [56.0, 100.0, 300.0, 112.0], 10.0,
                           source_uids=[numero.uid, intitule.uid])
    lignes = [p.PageLine(avant.text, list(avant.bbox), 10.0, source_uids=[avant.uid]),
              fusionnee,
              p.PageLine(apres.text, list(apres.bbox), 10.0, source_uids=[apres.uid])]
    page = p.PageText(page=1, width=595, height=842, lines=lignes, source=registre)
    return page, [avant, numero, intitule, apres]


def test_le_registre_publie_lunite_de_portage_de_chaque_ligne() -> None:
    """L'unité est une propriété du registre, dérivée de la position — jamais d'un document.

    Deux uid réunis par `_merge_number_lines` partagent une unité ; les lignes ordinaires sont
    chacune la leur. C'est cette donnée-là que `verifier()` a besoin de connaître pour refuser une
    proposition qui scinde ce qu'un bloc portera de toute façon d'un seul tenant.
    """
    page, (avant, numero, intitule, apres) = _page_avec_ligne_fusionnee()
    registre = _registre([page])
    assert registre[numero.uid].portage == registre[intitule.uid].portage
    assert registre[avant.uid].portage != registre[numero.uid].portage
    assert registre[apres.uid].portage not in {registre[avant.uid].portage,
                                              registre[numero.uid].portage}
    # Le titre servi de l'un est celui de l'autre : la ligne portée est la même.
    assert registre[numero.uid].titre == registre[intitule.uid].titre == "1.4 Objet de la section"


def test_verifier_refuse_une_ligne_fusionnee_scindee_entre_deux_noeuds() -> None:
    """(1) Le refus est **à l'acceptation**, pas seulement au build.

    Sonde de la revue : deux uid fusionnés, séparés entre deux intervalles par ailleurs valides —
    disjoints, croissants, couverture totale — donnaient `accepte=True`, et seul `build_document`
    refusait ensuite. La CLI `python -m server.ingest.structure` écrivait donc un `structure.json`
    que l'ingestion ne pourrait jamais accepter, contre la promesse « le code accepte la proposition
    uniquement s'il prouve ».
    """
    page, (avant, numero, intitule, apres) = _page_avec_ligne_fusionnee()
    registre = _registre([page])
    assert [registre[source.uid].ordre for source in (avant, numero, intitule, apres)] == [1, 2, 3, 4]
    scindee = _proposition(noeuds=[
        s.NoeudPropose(titre_line_uid=avant.uid, premiere_line_uid=avant.uid,
                       derniere_line_uid=numero.uid),
        s.NoeudPropose(titre_line_uid=intitule.uid, premiere_line_uid=intitule.uid,
                       derniere_line_uid=apres.uid),
    ])
    verdict = s.verifier(scindee, registre, doc_id=DOC, settings=get_settings())
    assert not verdict.accepte and verdict.motif == "affectation_non_prouvee"
    assert verdict.motif in s.MOTIFS and "unité de portage" in verdict.detail
    with pytest.raises(s.StructureRefusee) as capture:
        p.build_document([page], edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC,
                         title="Contrat", structure=scindee)
    assert capture.value.motif == "affectation_non_prouvee"
    # Et l'appel programmatique direct, qui contourne le vérificateur : `arbre()` rendrait sinon un
    # `node_of_uid` scindant l'unité, sur lequel toute la suite raisonnerait.
    with pytest.raises(s.StructureRefusee) as direct:
        s.arbre(scindee, registre, DOC)
    assert direct.value.motif == "affectation_non_prouvee"


def test_verifier_refuse_deux_uid_dune_meme_unite_comme_titres_distincts() -> None:
    """(2) Deux uid d'une même unité intitulent deux nœuds, et le titre servi est le même.

    `titre_ambigu` comparait `(page, bbox)` des lignes **source**, qui diffèrent avant fusion — un
    numéro et son intitulé n'ont pas la même boîte. Le titre servi, lui, est relu sur la ligne
    **portée** : les deux nœuds afficheraient le même intitulé et l'arbre inspectable répondrait deux
    fois la même chose pour le même endroit de la page.

    Ici la frontière de nœud ne tombe **pas** à l'intérieur de l'unité — le contrôle (1) ne voit donc
    rien —, et pourtant les deux nœuds sont intitulés par la même ligne portée.
    """
    page, (avant, numero, intitule, apres) = _page_avec_ligne_fusionnee()
    registre = _registre([page])
    proposition = _proposition(noeuds=[
        s.NoeudPropose(titre_line_uid=numero.uid, premiere_line_uid=avant.uid,
                       derniere_line_uid=apres.uid),
        s.NoeudPropose(titre_line_uid=intitule.uid, premiere_line_uid=numero.uid,
                       derniere_line_uid=apres.uid, parent_line_uid=numero.uid),
    ])
    assert registre[numero.uid].bbox != registre[intitule.uid].bbox  # (page, bbox) ne dit rien ici
    verdict = s.verifier(proposition, registre, doc_id=DOC, settings=get_settings())
    assert not verdict.accepte and verdict.motif == "titre_ambigu"
    assert "unité de portage" in verdict.detail


def test_une_table_sans_rangee_ne_fait_pas_disparaitre_ses_lignes_en_silence() -> None:
    """Cas limite : une table détectée qui ne rend aucune rangée ne sert **aucun** bloc.

    Ses lignes brutes redeviennent alors du contenu non servi, et c'est le seul cas où l'absorption
    par une table donne un motif de retrait — nommé `table_sans_bloc`, jamais un silence.
    """
    page, sources = _page_avec_table([])
    _entete, premiere, seconde, _corps = sources
    _document, meta = p.build_document([page], edition="2026", source_hash="0" * 64, toc=[],
                                       doc_id=DOC, title="Contrat")
    assert page.source.removed == {premiere.uid: "table_sans_bloc", seconde.uid: "table_sans_bloc"}
    assert p.anomalies_registre([page], meta["source_uids"]) == []
    # Rien ne les sert : elles ne sont donc l'ancre de rien et ne sont pas proposables.
    assert not {premiere.uid, seconde.uid} & set(_registre([page]))


def _page_registre(nombre: int) -> tuple[p.PageText, list[p.SourceLine]]:
    """Une page réduite à son registre : seul l'appariement lignes source ↔ blocs est en jeu ici."""
    registre = p.SourceRegistry()
    lignes = [registre.add(page=1, text=f"Ligne source {index}.",
                           bbox=[56.0, 100.0 + index * 20.0, 356.0, 112.0 + index * 20.0])
              for index in range(1, nombre + 1)]
    return p.PageText(page=1, width=595, height=842, source=registre), lignes


def _cas_de_registre(famille: str) -> tuple[p.PageText, dict[str, list[str]]]:
    """Un registre cohérent, puis **une** incohérence de la famille demandée, et une seule."""
    page, lignes = _page_registre(4)
    page.source.retirer([lignes[3].uid], "bande_recurrente")
    portees = {"bloc:1": [lignes[0].uid, lignes[1].uid], "bloc:2": [lignes[2].uid]}
    if famille == "inconnue_du_registre":
        portees["bloc:2"].append("p1:l99")  # un bloc porte un uid que l'extraction n'a jamais produit
    elif famille == "deux_blocs":
        portees["bloc:2"].append(lignes[0].uid)
    elif famille == "sans_bloc_ni_motif":
        portees["bloc:1"].remove(lignes[1].uid)
    elif famille == "portee_et_retiree":
        portees["bloc:2"].append(lignes[3].uid)
    return page, portees


def test_un_registre_coherent_ne_rend_aucune_anomalie_sans_etre_vide() -> None:
    """Témoin positif de l'invariant, sur deux ensembles **réellement peuplés**.

    Comparer deux ensembles vides ne prouve rien : un registre sans ligne satisfait « union
    complète, intersection vide » quel que soit le code qui l'évalue.
    """
    page, portees = _cas_de_registre("aucune")
    assert p.anomalies_registre([page], portees) == []
    assert len(page.source.lines) == 4 and len(page.source.removed) == 1
    assert sum(len(uids) for uids in portees.values()) == 3


@pytest.mark.parametrize("famille,fragment", [
    # (a) un uid porté par un bloc mais absent du registre : le bloc cite une ligne qui n'existe pas.
    ("inconnue_du_registre", "sans exister au registre"),
    # (b) un uid porté par deux blocs : la même preuve serait citable à deux endroits.
    ("deux_blocs", "rattachée à 2 blocs"),
    # (c) une ligne extraite sans bloc ni motif de retrait : elle a disparu en silence.
    ("sans_bloc_ni_motif", "sans bloc ni motif de retrait"),
    # (d) une ligne à la fois portée et retirée : l'intersection n'est plus vide.
    ("portee_et_retiree", "à la fois dans"),
])
def test_chaque_incoherence_du_registre_est_nommee(famille: str, fragment: str) -> None:
    """AC : union complète, intersection vide — l'invariant doit **savoir échouer**, famille par famille."""
    page, portees = _cas_de_registre(famille)
    anomalies = p.anomalies_registre([page], portees)
    assert len(anomalies) == 1 and fragment in anomalies[0], anomalies


def test_build_document_leve_sur_une_ligne_extraite_que_rien_ne_porte() -> None:
    """L'invariant n'est pas un rapport : il arrête l'ingestion avant tout artefact."""
    pages = _corpus()
    perdue = pages[0].source.add(page=1, text="Ligne extraite que rien ne porte.",
                                 bbox=[56.0, 400.0, 356.0, 412.0])
    with pytest.raises(ValueError, match="registre de lignes source incohérent") as capture:
        p.build_document(pages, edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC,
                         title="Contrat")
    assert perdue.uid in str(capture.value) and "sans bloc ni motif" in str(capture.value)


# --- 2. Surface exacte du modèle ----------------------------------------------------------------

def test_la_charge_utile_nexpose_que_la_position_et_le_texte_en_lecture_seule() -> None:
    """AC : ni texte de bloc réinscriptible, ni `kind`, ni portée, ni applicabilité, ni verdict."""
    registre = _registre(_corpus())
    payload = json.loads(s.demande(registre, get_settings()))
    assert set(payload) == {"lignes"}
    for ligne in payload["lignes"]:
        assert set(ligne) == {"uid", "page", "colonne", "ordre", "bbox", "texte"}
    ordres = [ligne["ordre"] for ligne in payload["lignes"]]
    assert ordres == sorted(ordres) == list(range(1, len(registre) + 1))


def test_le_schema_nadmet_que_des_uid_du_registre_et_aucun_champ_de_jugement() -> None:
    registre = _registre(_corpus())
    schema = s.requete(registre, DOC, get_settings())["output_config"]["format"]["schema"]
    noeud = schema["properties"]["noeuds"]["items"]
    assert set(noeud["properties"]) == {
        "titre_line_uid", "premiere_line_uid", "derniere_line_uid", "parent_line_uid",
        "title_line_uids", "article_uid", "surface_class", "continuation_line_uids",
        "relations",
    }
    assert noeud["additionalProperties"] is False and schema["additionalProperties"] is False
    assert noeud["properties"]["titre_line_uid"]["enum"] == list(registre)
    rendu = json.dumps(schema)
    # `relations.kind` est un vocabulaire structurel fermé, pas un jugement juridique.
    for interdit in ("portee", "scope", "applicab", "verdict", "titre\"", "texte"):
        assert interdit not in rendu, interdit


def test_la_charge_utile_reste_sous_la_borne_publiee(monkeypatch: pytest.MonkeyPatch) -> None:
    registre = _registre(_corpus())
    monkeypatch.setenv("STRUCTURE_MAX_INPUT_CHARS", "50")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="STRUCTURE_MAX_INPUT_CHARS"):
            s.demande(registre, get_settings())
    finally:
        get_settings.cache_clear()


class CreateInterdit(BaseException):
    """Hors de `Exception` **exprès** : `proposer()` convertit toute panne fournisseur en refus
    contrôlé, si bien qu'une `Exception` levée ici ressortirait déguisée en « appel refusé » et le
    double resterait muet sur la faute. Elle doit traverser le filet et faire échouer le test."""


class FauxMessages:
    """Double qui **lit la charge utile réelle** et répond à partir des seuls uid reçus.

    Il n'expose `messages.parse` que parce que la convention LLM du spine l'impose : appeler
    `messages.create` est la faute recherchée, et le double échoue alors bruyamment.
    """

    def __init__(self, *, noeuds: Any = None, stop_reason: str = "end_turn",
                 usage: dict[str, int] | None = None) -> None:
        self.noeuds = noeuds
        self.stop_reason = stop_reason
        self.usage = {"input_tokens": 120, "output_tokens": 30} if usage is None else usage
        self.calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []

    def parse(self, **params: Any) -> Any:
        self.calls.append(params)
        assert "output_format" not in params, (
            "convention LLM du spine : `output_format` ferait valider le SDK avant de rendre la "
            "réponse — `usage`, `stop_reason` et le texte reçu seraient perdus")
        lignes = json.loads(params["messages"][0]["content"])["lignes"]
        premiere, derniere = lignes[0]["uid"], lignes[-1]["uid"]
        noeuds = self.noeuds if self.noeuds is not None else [
            {"titre_line_uid": premiere, "premiere_line_uid": premiere,
             "derniere_line_uid": derniere, "parent_line_uid": None},
        ]
        return SimpleNamespace(usage=self.usage, stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text",
                                                        text=json.dumps({"noeuds": noeuds}))])

    def create(self, **params: Any) -> Any:
        self.create_calls.append(params)
        raise CreateInterdit(
            "`messages.create` est interdit ici : la convention LLM du spine impose "
            "`messages.parse(..., output_config={'format': …})` sans `output_format`")


class FauxClient:
    def __init__(self, **kwargs: Any) -> None:
        self.messages = FauxMessages(**kwargs)


def test_le_faux_client_repond_a_partir_des_uid_recus_et_la_proposition_est_acceptee() -> None:
    pages = _corpus()
    registre = _registre(pages)
    client = FauxClient()
    proposition, cout = s.proposer(client, registre, doc_id=DOC, settings=get_settings())
    params = client.messages.calls[0]
    assert params["model"] == s.MODEL and params["output_config"]["effort"] == "high"
    assert params["max_tokens"] == get_settings().structure_max_output_tokens
    assert cout > 0 and proposition.doc_id == DOC
    assert proposition.noeuds[0].titre_line_uid == "p1:l1"
    assert s.verifier(proposition, registre, doc_id=DOC, settings=get_settings()).accepte


def test_opus_audit_et_document_partagent_les_memes_line_uid_content_adresses(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Régression O-01 : proposition → audit → arbre publié ne change jamais d'identité de ligne."""
    pages = _corpus()
    p.ordonner_pages(pages)
    registre = s.registre_lignes(pages, document_uid=DOC)
    attendus = tuple(registre)
    assert attendus and all(uid.startswith("line-v1:") and len(uid) == s.LINE_UID_MAX
                            for uid in attendus)
    assert not any(uid.startswith("p") and ":l" in uid for uid in attendus)

    audit: dict[str, Any] = {}

    def capturer_audit(_path: Path, **event: Any) -> dict[str, Any]:
        audit.update(event)
        return {}

    monkeypatch.setattr(s, "append_ingest_audit", capturer_audit)
    client = FauxClient()
    proposition_filaire, _ = s.proposer(
        client, registre, doc_id=DOC, settings=get_settings(),
    )
    uids_envoyes = tuple(
        ligne["uid"]
        for ligne in json.loads(client.messages.calls[0]["messages"][0]["content"])["lignes"]
    )
    assert uids_envoyes == attendus
    assert audit["trusted_line_uids"] == tuple(sorted(attendus))
    assert proposition_filaire.noeuds[0].titre_line_uid in attendus

    proposition = s.StructureProposee(schema_version="2", doc_id=DOC, noeuds=[
        s.NoeudPropose(
            titre_line_uid=attendus[0], premiere_line_uid=attendus[0],
            derniere_line_uid=attendus[-1], parent_line_uid=None,
            title_line_uids=[attendus[0]], article_uid=None, surface_class="substantiel",
            continuation_line_uids=[], relations=[],
        ),
    ])
    verdict = s.verifier(proposition, registre, doc_id=DOC, settings=get_settings())
    assert verdict.accepte
    document, _ = p.build_document(
        pages, edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC,
        title="Contrat", structure=proposition,
    )
    publies = [line.line_uid for block in document.blocks for line in block.lines]
    assert set(attendus) == set(publies)
    assert len(publies) == len(set(publies))
    assert structure_gate._semantic_issues(document) == []
    noeud = next(node for node in document.nodes if node.node_id == f"{DOC}:s1")
    assert set(noeud.blocks) == {block.block_id for block in document.blocks}


def test_proposition_v2_transmet_relations_continuations_et_vraies_sections_au_singleton() -> None:
    pages = [_page(1, [
        "Article 0 Parent", "Article 1 Cible singleton", "Article 2 Clause liée",
        "Suite liée un.", "Suite liée deux.", "Définition dérogatoire.",
    ])]
    p.ordonner_pages(pages)
    uids = tuple(s.registre_lignes(pages, document_uid=DOC))
    proposition = s.StructureProposee(schema_version="2", doc_id=DOC, noeuds=[
        s.NoeudPropose(
            titre_line_uid=uids[0], premiere_line_uid=uids[0], derniere_line_uid=uids[5],
            parent_line_uid=None, title_line_uids=[uids[0]], article_uid="article:0",
            surface_class="substantiel", continuation_line_uids=[], relations=[],
        ),
        s.NoeudPropose(
            titre_line_uid=uids[1], premiere_line_uid=uids[1], derniere_line_uid=uids[1],
            parent_line_uid=uids[0], title_line_uids=[uids[1]], article_uid="article:1",
            surface_class="substantiel", continuation_line_uids=[], relations=[
                {"kind": "explicit_dependency", "target_line_uid": uids[2]},
                {"kind": "definition_override", "target_line_uid": uids[5]},
            ],
        ),
        s.NoeudPropose(
            titre_line_uid=uids[2], premiere_line_uid=uids[2], derniere_line_uid=uids[4],
            parent_line_uid=uids[0], title_line_uids=[uids[2]], article_uid="article:2",
            surface_class="substantiel", continuation_line_uids=[uids[3], uids[4]], relations=[],
        ),
        s.NoeudPropose(
            titre_line_uid=uids[5], premiere_line_uid=uids[5], derniere_line_uid=uids[5],
            parent_line_uid=uids[0], title_line_uids=[uids[5]], article_uid=None,
            surface_class="substantiel", continuation_line_uids=[], relations=[],
        ),
    ])
    document, _ = p.build_document(
        pages, edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC,
        title="Contrat", structure=proposition,
    )
    index = Index(Corpus(documents={DOC: document}))
    source_node = next(node for node in document.nodes if node.article_uid == "article:1")
    dependency_node = next(node for node in document.nodes if node.article_uid == "article:2")
    parent_node = next(node for node in document.nodes if node.article_uid == "article:0")
    source_block = source_node.blocks[0]

    window = index.ouvrir_noeud(
        source_node.node_id, focus_block_id=source_block, node_window=20,
    )
    roles = {unit.role for unit in window.context_units}
    assert roles == {"target", "explicit_dependency", "definition_override", "parent_preamble"}
    assert next(unit for unit in window.context_units
                if unit.role == "parent_preamble").section_uid == parent_node.node_id
    assert all(unit.section_uid == dependency_node.node_id
               for unit in window.context_units if unit.role == "explicit_dependency")
    assert [block.context_role for block in window.blocks] == [
        unit.role for unit in window.context_units]

    continuation = index.ouvrir_singleton(dependency_node.blocks[0], node_window=20)
    continued = [unit.block_uid for unit in continuation.context_units
                 if unit.role == "same_clause_continuation"]
    assert continued == dependency_node.blocks[1:]


@pytest.mark.parametrize(("title", "article_uid", "surface", "detail"), [
    ("Sommaire", None, "substantiel", "table_des_matieres"),
    ("Table of contents", None, "substantiel", "table_des_matieres"),
    ("Inhaltsverzeichnis", None, "substantiel", "table_des_matieres"),
    ("Préambule", None, "substantiel", "preliminaire"),
    ("Preamble", None, "substantiel", "preliminaire"),
    ("Vorwort", None, "substantiel", "preliminaire"),
    ("Article 12 Garanties", "article:13", "substantiel", "article_uid"),
    ("Artikel 12 Deckung", None, "substantiel", "article_uid"),
    ("Titre sans article", "article:12", "substantiel", "article_uid"),
])
def test_oracle_independant_refuse_semantique_opus_contredite_par_le_titre(
        title: str, article_uid: str | None, surface: str, detail: str) -> None:
    pages = [_page(1, [title, "Corps de la section."])]
    p.ordonner_pages(pages)
    registre = s.registre_lignes(pages, document_uid=DOC)
    uids = tuple(registre)
    proposition = s.StructureProposee(schema_version="2", doc_id=DOC, noeuds=[
        s.NoeudPropose(
            titre_line_uid=uids[0], premiere_line_uid=uids[0], derniere_line_uid=uids[-1],
            parent_line_uid=None, title_line_uids=[uids[0]], article_uid=article_uid,
            surface_class=surface, continuation_line_uids=[], relations=[],
        ),
    ])

    verdict = s.verifier(proposition, registre, doc_id=DOC, settings=get_settings())

    assert not verdict.accepte and verdict.motif == "affectation_non_prouvee"
    assert detail in verdict.detail


def test_oracle_independant_refuse_une_surface_sans_preuve_locale() -> None:
    pages = [_page(1, ["Documentation"])]
    p.ordonner_pages(pages)
    registre = s.registre_lignes(pages, document_uid=DOC)
    (uid,) = tuple(registre)
    proposition = s.StructureProposee(schema_version="2", doc_id=DOC, noeuds=[
        s.NoeudPropose(
            titre_line_uid=uid, premiere_line_uid=uid, derniere_line_uid=uid,
            parent_line_uid=None, title_line_uids=[uid], article_uid=None,
            surface_class="substantiel", continuation_line_uids=[], relations=[],
        ),
    ])

    verdict = s.verifier(proposition, registre, doc_id=DOC, settings=get_settings())

    assert not verdict.accepte and verdict.motif == "affectation_non_prouvee"
    assert "surface sans preuve" in verdict.detail


def test_oracle_independant_refuse_un_parent_semantique_faux_mais_contenant() -> None:
    pages = [_page(1, [
        "Article 1 Garanties", "Article 2.1 Risques", "Corps de la sous-section.",
        "Suite du parent.",
    ])]
    p.ordonner_pages(pages)
    registre = s.registre_lignes(pages, document_uid=DOC)
    uids = tuple(registre)
    proposition = s.StructureProposee(schema_version="2", doc_id=DOC, noeuds=[
        s.NoeudPropose(
            titre_line_uid=uids[0], premiere_line_uid=uids[0], derniere_line_uid=uids[3],
            parent_line_uid=None, title_line_uids=[uids[0]], article_uid="article:1",
            surface_class="substantiel", continuation_line_uids=[], relations=[],
        ),
        s.NoeudPropose(
            titre_line_uid=uids[1], premiere_line_uid=uids[1], derniere_line_uid=uids[2],
            parent_line_uid=uids[0], title_line_uids=[uids[1]], article_uid="article:2.1",
            surface_class="substantiel", continuation_line_uids=[], relations=[],
        ),
    ])

    verdict = s.verifier(proposition, registre, doc_id=DOC, settings=get_settings())

    assert not verdict.accepte and verdict.motif == "parent_non_contenant"
    assert "parenté sémantique" in verdict.detail


def test_oracle_independant_refuse_une_ligne_de_corps_annexee_au_titre() -> None:
    pages = [_page(1, ["Titre de section", "Phrase de corps arbitraire.", "Suite."])]
    p.ordonner_pages(pages)
    registre = s.registre_lignes(pages, document_uid=DOC)
    uids = tuple(registre)
    proposition = s.StructureProposee(schema_version="2", doc_id=DOC, noeuds=[
        s.NoeudPropose(
            titre_line_uid=uids[0], premiere_line_uid=uids[0], derniere_line_uid=uids[-1],
            parent_line_uid=None, title_line_uids=[uids[0], uids[1]], article_uid=None,
            surface_class="substantiel", continuation_line_uids=[], relations=[],
        ),
    ])

    verdict = s.verifier(proposition, registre, doc_id=DOC, settings=get_settings())

    assert not verdict.accepte and verdict.motif == "affectation_non_prouvee"
    assert "ligne de corps" in verdict.detail


def test_le_line_uid_canonique_dune_table_est_celui_de_sa_ligne_publiee() -> None:
    """Une table reste atomique, mais son ancre Opus est bien une `Document.Line` réelle."""
    page, _sources = _page_avec_table([["Colonne A", "Colonne B"], ["Valeur A", "Valeur B"]])
    p.ordonner_pages([page])
    registre = s.registre_lignes([page], document_uid=DOC)
    uids = tuple(registre)
    proposition = s.StructureProposee(schema_version="2", doc_id=DOC, noeuds=[
        s.NoeudPropose(
            titre_line_uid=uids[0], premiere_line_uid=uids[0], derniere_line_uid=uids[-1],
            parent_line_uid=None, title_line_uids=[uids[0]], article_uid=None,
            surface_class="substantiel", continuation_line_uids=[], relations=[],
        ),
    ])
    document, _ = p.build_document(
        [page], edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC,
        title="Contrat", structure=proposition,
    )
    publies = [line.line_uid for block in document.blocks for line in block.lines]
    assert set(registre) <= set(publies)
    table = next(block for block in document.blocks if block.kind == "table")
    ancre_table = next(entree.uid for entree in registre.values()
                        if len(entree.source_uids) == 2)
    assert table.lines[0].line_uid == ancre_table


def test_lappel_passe_par_messages_parse_sans_output_format_et_jamais_par_create() -> None:
    """Convention LLM du spine : `messages.parse(..., output_config={"format": …})` **sans**
    `output_format`, validation locale par `TypeAdapter`.

    Le double **échoue si `create` est appelé** : avec `create`, le module d'ingestion parlerait au
    fournisseur par une surface que le spine n'autorise pas, et la seule chose qui l'aurait dit
    était l'absence de test.
    """
    registre = _registre(_corpus())
    client = FauxClient()
    s.proposer(client, registre, doc_id=DOC, settings=get_settings())
    assert client.messages.calls and client.messages.create_calls == []
    params = client.messages.calls[0]
    assert "output_format" not in params
    assert set(params["output_config"]) == {"format", "effort"}
    assert params["output_config"]["format"]["type"] == "json_schema"
    # Le piège est bien armé : `create` échoue, et son échec n'est pas rattrapable en « refus »
    # (`CreateInterdit` n'est pas une `Exception`, donc le filet à pannes fournisseur la laisse
    # passer). Une régression vers `messages.create` rougirait ici et dans chaque autre test qui
    # emploie ce double.
    with pytest.raises(CreateInterdit):
        FauxClient().messages.create(**params)
    assert not issubclass(CreateInterdit, Exception)


def test_une_reponse_localement_invalide_conserve_usage_stop_reason_et_texte_recu() -> None:
    """La raison d'être de la convention : la validation est **locale**, donc après la réponse.

    Avec `output_format`, le SDK 1.0.0 valide avant de rendre le message et lève `ValidationError` :
    `usage` (donc le coût réel), `stop_reason` et le texte reçu seraient perdus. Ici, le refus les
    porte tous les trois.
    """
    registre = _registre(_corpus())
    hors_forme = [{"titre_line_uid": "p1:l1", "premiere_line_uid": "p1:l1",
                   "derniere_line_uid": "p1:l4", "parent_line_uid": None, "kind": "garantie"}]
    with pytest.raises(ValueError) as leve:
        s.proposer(FauxClient(noeuds=hors_forme), registre, doc_id=DOC, settings=get_settings())
    message = str(leve.value)
    assert "hors schéma strict" in message  # la validation locale, non celle du SDK
    assert "coût réel" in message and "stop_reason='end_turn'" in message
    assert "caractère(s) reçus" in message and "rien n'a été écrit" in message


def test_un_uid_etranger_est_refuse_avant_tout_usage_meme_si_le_schema_limpose() -> None:
    """Défense locale de `parse_proposition` : le schéma fournisseur n'est jamais cru sur parole."""
    registre = _registre(_corpus())
    with pytest.raises(ValueError, match="inconnu du registre"):
        s.parse_proposition(json.dumps({"noeuds": [
            {"titre_line_uid": "p9:l9", "premiere_line_uid": "p1:l1",
             "derniere_line_uid": "p1:l2", "parent_line_uid": None},
        ]}), registre, DOC)
    with pytest.raises(ValueError, match="hors schéma strict"):
        s.parse_proposition(json.dumps({"noeuds": [{"titre_line_uid": "p1:l1", "kind": "garantie"}]}),
                            registre, DOC)
    with pytest.raises(ValueError, match="dupliqué"):
        s.parse_proposition(json.dumps({"noeuds": [
            {"titre_line_uid": "p1:l1", "premiere_line_uid": "p1:l1",
             "derniere_line_uid": "p1:l2", "parent_line_uid": None},
            {"titre_line_uid": "p1:l1", "premiere_line_uid": "p1:l3",
             "derniere_line_uid": "p1:l4", "parent_line_uid": None},
        ]}), registre, DOC)


def test_une_reponse_tronquee_ou_sans_usage_ne_produit_aucune_proposition() -> None:
    registre = _registre(_corpus())
    with pytest.raises(ValueError, match="interrompue"):
        s.proposer(FauxClient(stop_reason="max_tokens"), registre, doc_id=DOC, settings=get_settings())
    with pytest.raises(ValueError, match="usage facturable"):
        s.proposer(FauxClient(usage={}), registre, doc_id=DOC, settings=get_settings())


def test_la_cli_hors_ligne_ne_construit_aucun_client_et_publie_ses_bornes() -> None:
    sortie = io.StringIO()
    assert s.main(["--dry-run"], output=sortie) == 0
    rendu = sortie.getvalue()
    assert "column_gutter_min_pt" in rendu and "structure_min_coverage" in rendu
    assert "aucune requête, aucun client construit" in rendu
    for motif in s.MOTIFS:
        assert motif in rendu
    # Sans document et hors dry-run, la CLI refuse plutôt que d'appeler ; et sans clé, elle refuse
    # aussi sur un document — aucun client `anthropic` n'est jamais construit par ces chemins.
    assert s.main([], output=io.StringIO()) == 2


def _aucun_client(*args: Any, **kwargs: Any) -> Any:
    """Sentinelle posée à la place d'`anthropic.Anthropic` : le construire est la faute recherchée."""
    raise AssertionError("aucun client anthropic ne doit être construit sur ce chemin")


def _poser_la_disposition(tmp_path: Path, dossier: Path) -> None:
    """La disposition du `data-dir` d'un test, posée comme un opérateur la pose (story 4.5, N3).

    `structure.main` est un entrypoint de production : il exige une racine **installée** et refuse
    avant toute extraction et avant son unique appel payant sinon. Son lot n'a qu'une cible, et
    c'est précisément le cas où le refus « lot mixte » était structurellement inatteignable.
    """
    poser_espace(tmp_path, cibles=[dossier.relative_to(tmp_path) / "structure.json"])


def test_le_prevol_de_cout_refuse_avant_toute_construction_de_client(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """AD-1/AD-9 : le majorant est comparé au plafond **avant** tout client (idiome `type_clauses`).

    `--dry-run` sans document sort avant le registre, la requête et l'estimation : il ne joue donc ni
    `majorant_eur`, ni `estimate_cost`, ni la comparaison au plafond, alors que `config.py` promet
    « majorant vérifié avant toute construction de client ». C'est ce chemin-là qui est joué ici, sur
    un document réel du système de fichiers, avec un plafond volontairement minuscule.
    """
    dossier = _dossier(tmp_path)
    _poser_la_disposition(tmp_path, dossier)
    monkeypatch.setattr(s.anthropic, "Anthropic", _aucun_client)
    sortie = io.StringIO()
    code = s.main([DOC, "--data", str(dossier.parent), "--max-cost", "0.0001"], output=sortie)
    assert code == 3  # non nul : le run refuse, il ne se termine pas « avec succès sans rien faire »
    rendu = sortie.getvalue()
    assert "ligne(s) source" in rendu and "majorant Messages standard" in rendu
    assert "aucun appel soumis" in capsys.readouterr().err
    assert not (dossier / "structure.json").exists()


def test_le_plafond_de_cout_se_regle_aussi_par_config(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Convention Seuils : `--max-cost` ne fait que **surcharger** `structure_max_cost_eur`."""
    dossier = _dossier(tmp_path)
    _poser_la_disposition(tmp_path, dossier)
    monkeypatch.setattr(s.anthropic, "Anthropic", _aucun_client)
    reglages = Settings(_env_file=None, structure_max_cost_eur=0.0001)
    code = s.main([DOC, "--data", str(dossier.parent)], settings=reglages, output=io.StringIO())
    assert code == 3 and not (dossier / "structure.json").exists()


def _aucune_extraction(*args: Any, **kwargs: Any) -> Any:
    """Sentinelle posée à la place d'`extract_pages` : extraire est déjà trop tard."""
    raise AssertionError("aucune extraction ne doit avoir lieu avant la validation du plafond")


@pytest.mark.parametrize("valeur", ["nan", "inf", "-inf", "0", "-0.0", "-1"])
def test_un_plafond_non_fini_ou_nul_est_refuse_avant_extraction_et_avant_tout_client(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        valeur: str) -> None:
    """AD-9 : un plafond que la comparaison ne peut pas faire respecter est refusé d'entrée.

    `nan` rend `estimate > ceiling` **toujours** faux et `inf` ne bloque jamais : le plafond annoncé
    était neutralisable depuis la ligne de commande, juste avant l'appel le plus cher du projet.
    Zéro et les valeurs négatives ne sont pas davantage un plafond. Le refus précède l'extraction et
    toute construction de client, comme dans les autres CLI d'ingestion.
    """
    dossier = _dossier(tmp_path)
    monkeypatch.setattr(s.anthropic, "Anthropic", _aucun_client)
    monkeypatch.setattr(p, "extract_pages", _aucune_extraction)
    # `--max-cost=-inf` et non `--max-cost -inf` : argparse prend `-inf` pour une option.
    code = s.main([DOC, "--data", str(dossier.parent), f"--max-cost={valeur}"], output=io.StringIO())
    assert code == 2
    erreur = capsys.readouterr().err
    assert "plafond" in erreur and "fini strictement positif" in erreur
    assert not (dossier / "structure.json").exists()


def test_un_plafond_infini_venu_du_reglage_est_refuse_lui_aussi(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """L'autre source de plafond : `structure_max_cost_eur` est borné `gt=0`… ce qui laisse `inf`.

    La garde porte donc sur la valeur **résolue**, jamais sur le seul argument de ligne de commande.
    """
    dossier = _dossier(tmp_path)
    monkeypatch.setattr(s.anthropic, "Anthropic", _aucun_client)
    monkeypatch.setattr(p, "extract_pages", _aucune_extraction)
    reglages = Settings(_env_file=None, structure_max_cost_eur=math.inf)
    code = s.main([DOC, "--data", str(dossier.parent)], settings=reglages, output=io.StringIO())
    assert code == 2 and "plafond" in capsys.readouterr().err
    assert not (dossier / "structure.json").exists()


def test_sans_cle_anthropic_la_cli_refuse_sur_un_document(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """AD-14 : « sans clé, ça refuse » — y compris une fois le majorant passé, et sans rien écrire."""
    dossier = _dossier(tmp_path)
    _poser_la_disposition(tmp_path, dossier)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # posée et vide : elle fait foi
    monkeypatch.setattr(s.anthropic, "Anthropic", _aucun_client)
    code = s.main([DOC, "--data", str(dossier.parent)],
                  settings=Settings(_env_file=None, anthropic_api_key=""), output=io.StringIO())
    assert code == 2 and "ANTHROPIC_API_KEY absente" in capsys.readouterr().err
    assert not (dossier / "structure.json").exists()


def test_la_cli_ecrit_structure_json_atomiquement_avec_un_client_injecte(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le chemin nominal hors ligne : client injecté, verdict rendu, artefact écrit d'un seul coup."""
    dossier = _dossier(tmp_path)
    _poser_la_disposition(tmp_path, dossier)
    monkeypatch.setattr(s.anthropic, "Anthropic", _aucun_client)  # injecté ⇒ aucun n'est construit
    sortie = io.StringIO()
    code = s.main([DOC, "--data", str(dossier.parent)], client=FauxClient(), output=sortie)
    assert code == 0 and "verdict accepté" in sortie.getvalue()
    ecrite = s.StructureProposee.model_validate_json((dossier / "structure.json").read_bytes())
    assert ecrite.doc_id == DOC and ecrite.noeuds
    # `write_atomic` : le fichier de travail est renommé, jamais laissé à côté de l'artefact.
    assert [chemin.name for chemin in dossier.glob("*.tmp")] == []
    assert (dossier / "structure.json").read_text("utf-8").endswith("\n")


# --- 3. Rejet : une famille invalide, un refus nommé --------------------------------------------

@pytest.mark.parametrize("noeuds,motif", [
    # Ligne inconnue : un uid absent du registre.
    ([("p1:l1", "p1:l1", "p9:l1", None)], "ligne_inconnue"),
    # Parent qui n'intitule aucun nœud : l'arbre serait suspendu à rien.
    ([("p1:l1", "p1:l1", "p1:l4", "p2:l2")], "ligne_inconnue"),
    # Titre dupliqué : deux nœuds revendiquent la même ligne d'intitulé.
    ([("p1:l1", "p1:l1", "p1:l2", None), ("p1:l1", "p1:l3", "p1:l4", None)], "titre_duplique"),
    # Ordre non monotone : première ligne après la dernière.
    ([("p1:l3", "p1:l4", "p1:l2", None)], "ordre_impossible"),
    # Ordre non monotone : titre hors de son propre intervalle.
    ([("p1:l4", "p1:l1", "p1:l2", None)], "ordre_impossible"),
    # Ordre non monotone : frères rendus dans l'ordre décroissant.
    ([("p2:l1", "p2:l1", "p2:l3", None), ("p1:l1", "p1:l1", "p1:l4", None)], "ordre_impossible"),
    # Intervalles croisés : ni disjoints, ni strictement emboîtés.
    ([("p1:l1", "p1:l1", "p1:l3", None), ("p1:l2", "p1:l2", "p1:l4", None)], "intervalles_croises"),
    # Emboîtement sans filiation : contenu dans l'autre sans en descendre.
    ([("p1:l1", "p1:l1", "p1:l4", None), ("p1:l2", "p1:l2", "p1:l3", None)], "intervalles_croises"),
    # Lignes omises : une seule ligne couverte sur sept.
    ([("p1:l1", "p1:l1", "p1:l1", None)], "ligne_omise"),
])
def test_chaque_famille_invalide_rend_un_refus_nomme(noeuds: list[tuple], motif: str) -> None:
    proposition = _proposition(noeuds=[
        s.NoeudPropose(titre_line_uid=t, premiere_line_uid=a, derniere_line_uid=b, parent_line_uid=parent)
        for t, a, b, parent in noeuds
    ])
    verdict = _verdict(proposition)
    assert not verdict.accepte and verdict.motif == motif and verdict.detail
    assert verdict.motif in s.MOTIFS


def test_un_cycle_de_parents_est_refuse() -> None:
    proposition = _proposition(noeuds=[
        s.NoeudPropose(titre_line_uid="p1:l1", premiere_line_uid="p1:l1",
                       derniere_line_uid="p1:l4", parent_line_uid="p2:l1"),
        s.NoeudPropose(titre_line_uid="p2:l1", premiere_line_uid="p2:l1",
                       derniere_line_uid="p2:l3", parent_line_uid="p1:l1"),
    ])
    verdict = _verdict(proposition)
    assert not verdict.accepte and verdict.motif == "cycle"


def test_un_long_cycle_de_relations_est_refuse_par_parcours_iteratif_borne() -> None:
    page = _page(1, [f"Ligne source {index}." for index in range(1100)])
    p.ordonner_pages([page])
    registre = s.registre_lignes([page], document_uid=DOC)
    uids = tuple(registre)
    proposition = s.StructureProposee(schema_version="2", doc_id=DOC, noeuds=[
        s.NoeudPropose(
            titre_line_uid=uid, premiere_line_uid=uid, derniere_line_uid=uid,
            parent_line_uid=None if index == 0 else uids[index - 1],
            title_line_uids=[uid], article_uid=None,
            surface_class="substantiel", continuation_line_uids=[], relations=[{
                "kind": "explicit_dependency",
                "target_line_uid": uids[(index + 1) % len(uids)],
            }],
        )
        for index, uid in enumerate(uids)
    ])

    verdict = s.verifier(proposition, registre, doc_id=DOC, settings=get_settings())

    assert not verdict.accepte and verdict.motif == "cycle"


def test_une_profondeur_hors_borne_est_refusee(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRUCTURE_MAX_DEPTH", "1")
    get_settings.cache_clear()
    try:
        verdict = _verdict(_proposition())
    finally:
        get_settings.cache_clear()
    assert not verdict.accepte and verdict.motif == "profondeur_excessive"


def _dix_lignes() -> p.PageText:
    return _page(1, [f"Ligne source numero {index}." for index in range(1, 11)])


def _neuf_sur_dix() -> s.StructureProposee:
    """La sonde de revue : dix lignes au registre, neuf couvertes — `p1:l10` reste hors de tout nœud."""
    return _proposition(noeuds=[
        s.NoeudPropose(titre_line_uid="p1:l1", premiere_line_uid="p1:l1", derniere_line_uid="p1:l9"),
    ])


def test_une_seule_ligne_omise_sur_dix_est_un_refus_nomme_qui_designe_son_uid() -> None:
    """AC : « toute ligne inconnue, dupliquée, **omise** … met le document en quarantaine ».

    Une couverture de 90 % rendait `Verdict(accepte=True)` : les groupes laissés hors de tout
    intervalle étaient ensuite rattachés à un nœud voisin, et l'arbre servi portait une affectation
    que la proposition n'avait ni portée ni prouvée, sous l'annonce « proposition vérifiée ».
    """
    registre = _registre([_dix_lignes()])
    assert len(registre) == 10
    verdict = s.verifier(_neuf_sur_dix(), registre, doc_id=DOC, settings=get_settings())
    assert not verdict.accepte and verdict.motif == "ligne_omise"
    assert "p1:l10" in verdict.detail  # le refus **nomme** les uid concernés
    assert "ligne_omise" in s.MOTIFS and "couverture_insuffisante" not in s.MOTIFS


def test_abaisser_la_borne_de_couverture_ne_rouvre_pas_la_ligne_omise(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """La borne reste publiée, mais la règle par uid est **inconditionnelle** : rien ne la desserre.

    C'est ce qui empêche `structure_min_coverage` de redevenir la porte par laquelle une proposition
    incomplète était acceptée : abaisser le réglage ne rend aucune ligne omise acceptable.
    """
    monkeypatch.setenv("STRUCTURE_MIN_COVERAGE", "0.1")
    get_settings.cache_clear()
    try:
        assert get_settings().thresholds()["structure_min_coverage"] == 0.1
        verdict = s.verifier(_neuf_sur_dix(), _registre([_dix_lignes()]), doc_id=DOC,
                             settings=get_settings())
    finally:
        monkeypatch.delenv("STRUCTURE_MIN_COVERAGE")
        get_settings.cache_clear()
    assert not verdict.accepte and verdict.motif == "ligne_omise"


def test_une_ligne_omise_leve_dans_build_document_plutot_que_de_setre_fait_heriter_un_noeud() -> None:
    """L'omission arrête l'ingestion : aucun groupe ne se voit prêter le nœud de son voisin."""
    with pytest.raises(s.StructureRefusee) as capture:
        p.build_document([_dix_lignes()], edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC,
                         title="Contrat", structure=_neuf_sur_dix())
    assert capture.value.motif == "ligne_omise" and "p1:l10" in capture.value.detail


def test_une_proposition_pour_un_autre_document_est_refusee() -> None:
    verdict = _verdict(_proposition(doc_id="autre-document"))
    assert not verdict.accepte and verdict.motif == "document_different"


def test_deux_titres_au_meme_endroit_sont_ambigus() -> None:
    """Deux titres de même `(page, bbox)` rendraient deux réponses pour la même page surlignée."""
    registre = p.SourceRegistry()
    bbox = [56.0, 100.0, 356.0, 112.0]
    premiere = registre.add(page=1, text="S1 Titre", bbox=bbox)
    jumelle = registre.add(page=1, text="S1 Titre", bbox=bbox)  # même position, autre uid
    fin = registre.add(page=1, text="Corps.", bbox=[56.0, 120.0, 356.0, 132.0])
    page = p.PageText(page=1, width=595, height=842, source=registre, lines=[
        p.PageLine(line.text, list(line.bbox), 10.0, source_uids=[line.uid])
        for line in (premiere, jumelle, fin)
    ])
    proposition = _proposition(noeuds=[
        s.NoeudPropose(titre_line_uid=premiere.uid, premiere_line_uid=premiere.uid,
                       derniere_line_uid=jumelle.uid),
        s.NoeudPropose(titre_line_uid=jumelle.uid, premiere_line_uid=jumelle.uid,
                       derniere_line_uid=fin.uid),
    ])
    verdict = _verdict(proposition, [page])
    assert not verdict.accepte and verdict.motif == "titre_ambigu"


# --- L'arbre accepté : titres du registre, node_id positionnels ---------------------------------

def test_une_proposition_valide_donne_un_arbre_positionnel_titre_par_le_registre() -> None:
    """AC : titres pris au registre, `node_id` calculés par le code, ordre de lecture conservé."""
    pages = _corpus()
    document, _meta = p.build_document(pages, edition="2026", source_hash="0" * 64, toc=[],
                                       doc_id=DOC, title="Contrat", structure=_proposition())
    par_id = {node.node_id: node for node in document.nodes}
    assert set(par_id) == {DOC, f"{DOC}:s1", f"{DOC}:s1.1", f"{DOC}:s2"}
    assert par_id[f"{DOC}:s1"].title == "S1 Titre de la premiere section"
    assert par_id[f"{DOC}:s1.1"].title == "S1a Titre de la sous-section"
    assert par_id[f"{DOC}:s1.1"].level == 2 and par_id[f"{DOC}:s2"].level == 1
    assert par_id[DOC].children == [f"{DOC}:s1", f"{DOC}:s2"]
    assert par_id[f"{DOC}:s1"].children == [f"{DOC}:s1.1"]
    # Le nœud parent voit son propre corps avant le `NodeRef` de l'enfant qui le suit dans la page :
    # c'est la création paresseuse qui garde `Node.items` fidèle à l'ordre de lecture.
    items = [type(item).__name__ for item in par_id[f"{DOC}:s1"].items]
    assert items == ["BlockRef", "BlockRef", "NodeRef"]
    assert all(not node.node_id.startswith(f"{DOC}:a") for node in document.nodes)


def test_les_node_id_sont_positionnels_et_ne_reprennent_aucun_texte_du_modele() -> None:
    """Renommer les intitulés ne bouge aucun identifiant : ils viennent du chemin, pas du texte."""
    pages = _corpus()
    autres = [_page(1, ["Z9 Autre intitule", "Corps.", "Y8 Autre sous-intitule", "Corps."]),
              _page(2, ["X7 Autre section", "Corps.", "Suite."])]
    premier, _ = p.build_document(pages, edition="2026", source_hash="0" * 64, toc=[],
                                  doc_id=DOC, title="Contrat", structure=_proposition())
    second, _ = p.build_document(autres, edition="2026", source_hash="0" * 64, toc=[],
                                 doc_id=DOC, title="Contrat", structure=_proposition())
    assert [node.node_id for node in premier.nodes] == [node.node_id for node in second.nodes]
    assert [node.title for node in premier.nodes] != [node.title for node in second.nodes]


def test_build_document_leve_sur_un_refus_plutot_que_de_replier_sur_la_numerotation() -> None:
    """AD-16 : jamais de repli silencieux — l'heuristique ne rattrape pas une proposition refusée."""
    invalide = _proposition(noeuds=[
        s.NoeudPropose(titre_line_uid="p1:l1", premiere_line_uid="p1:l1", derniere_line_uid="p1:l1"),
    ])
    with pytest.raises(s.StructureRefusee) as capture:
        p.build_document(_corpus(), edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC,
                         title="Contrat", structure=invalide)
    assert capture.value.motif == "ligne_omise"


def test_la_reconciliation_finale_compare_le_proprietaire_effectif_de_chaque_uid() -> None:
    """(3) L'arbre bâti est reconfronté à `node_of_uid`, pas seulement à la présence des `node_id`.

    `_noeud_de_groupe` impose le propriétaire groupe par groupe **au moment de l'ajout** ; rien ne le
    revérifiait ensuite sur le document construit, si bien qu'un chemin qui le contournerait servirait
    une affectation divergente sous l'annonce « proposition vérifiée ». Le détournement posé ici est
    exactement ce chemin : il laisse tous les nœuds prouvés exister — `noeud_non_construit` ne voit
    donc rien — et ne déplace qu'un seul bloc.
    """
    pages = _corpus()
    original = p._noeud_de_groupe

    def detournement(lines: list[p.PageLine], node_of_uid: dict[str, str], *, page: int) -> str:
        node_id = original(lines, node_of_uid, page=page)
        uids = {uid for line in lines for uid in line.source_uids}
        return f"{DOC}:s1" if "p1:l4" in uids else node_id

    p._noeud_de_groupe = detournement  # type: ignore[assignment]
    try:
        with pytest.raises(s.StructureRefusee) as capture:
            p.build_document(pages, edition="2026", source_hash="0" * 64, toc=[], doc_id=DOC,
                             title="Contrat", structure=_proposition())
    finally:
        p._noeud_de_groupe = original  # type: ignore[assignment]
    assert capture.value.motif == "affectation_non_prouvee"
    assert "p1:l4" in capture.value.detail and f"{DOC}:s1.1" in capture.value.detail


def test_la_reconciliation_voit_aussi_un_bloc_sans_uid_et_un_bloc_sans_noeud() -> None:
    """Les deux angles morts d'une réconciliation qui ne regarderait que les uid déjà rattachés.

    Un bloc servi sans aucune ligne source prouvée n'est comparable à rien — c'est le trou par lequel
    du texte non prouvé serait servi —, et un bloc qu'aucun nœud ne réclame n'a pas de propriétaire
    à confronter. Les deux sont des refus, jamais un silence.
    """
    from server.app.domain.document import BlockRef, Node

    noeuds = {f"{DOC}:s1": Node(node_id=f"{DOC}:s1", level=1, title="S1",
                                items=[BlockRef(block_id="b1"), BlockRef(block_id="b2")])}
    with pytest.raises(s.StructureRefusee) as sans_uid:
        p.reconcilier_affectation(noeuds, {"b1": ["p1:l1"], "b2": []}, {"p1:l1": f"{DOC}:s1"})
    assert sans_uid.value.motif == "affectation_non_prouvee"
    assert "aucune ligne source" in sans_uid.value.detail and "b2" in sans_uid.value.detail
    with pytest.raises(s.StructureRefusee) as orphelin:
        p.reconcilier_affectation(noeuds, {"b1": ["p1:l1"], "b3": ["p1:l2"]},
                                  {"p1:l1": f"{DOC}:s1", "p1:l2": f"{DOC}:s1"})
    assert "b3" in orphelin.value.detail and "aucun nœud" in orphelin.value.detail
    # Témoin positif : la même réconciliation, honorée, ne dit rien.
    p.reconcilier_affectation(noeuds, {"b1": ["p1:l1"], "b2": ["p1:l2"]},
                              {"p1:l1": f"{DOC}:s1", "p1:l2": f"{DOC}:s1"})


# --- Largeur bornée : nombre de nœuds, nombre d'enfants -----------------------------------------
#
# La profondeur était bornée, la largeur ne l'était pas : `verifier()` porte une boucle en O(n²) sur
# les intervalles, si bien qu'une proposition très large faisait travailler indéfiniment un chemin
# dont toute la valeur est d'être fail-closed et déterministe. Les deux bornes sont éprouvées **à la
# borne exacte** (accepté) et **au-delà** (refusé), à chacun des cinq points où elles s'appliquent :
# schéma fournisseur, modèle Pydantic, parse local, chargement depuis le disque, vérificateur.


@contextlib.contextmanager
def _regle(monkeypatch: pytest.MonkeyPatch, **valeurs: str) -> Any:
    """Règle des bornes le temps d'un bloc, puis rend le cache de `Settings` à l'environnement réel."""
    for nom, valeur in valeurs.items():
        monkeypatch.setenv(nom, valeur)
    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        for nom in valeurs:
            monkeypatch.delenv(nom, raising=False)
        get_settings.cache_clear()


def _plate(lignes: int) -> tuple[list[p.PageText], s.StructureProposee]:
    """Un corpus d'une page et la proposition **plate** qui le couvre : N nœuds, N racines.

    « Une proposition plate de N nœuds sans parent est une largeur de N » : c'est le cas qui prouve
    que les racines comptent comme les enfants d'un parent quelconque.
    """
    page = _page(1, [f"Ligne source numero {index}." for index in range(1, lignes + 1)])
    noeuds = [s.NoeudPropose(titre_line_uid=f"p1:l{index}", premiere_line_uid=f"p1:l{index}",
                             derniere_line_uid=f"p1:l{index}") for index in range(1, lignes + 1)]
    return [page], s.StructureProposee(schema_version="1", doc_id=DOC, noeuds=noeuds)


def test_le_nombre_total_de_noeuds_est_accepte_a_la_borne_et_refuse_au_dela(
        monkeypatch: pytest.MonkeyPatch) -> None:
    pages, proposition = _plate(4)
    registre = _registre(pages)
    with _regle(monkeypatch, STRUCTURE_MAX_NODES="4", STRUCTURE_MAX_CHILDREN="4") as settings:
        assert s.verifier(proposition, registre, doc_id=DOC, settings=settings).accepte
    with _regle(monkeypatch, STRUCTURE_MAX_NODES="3", STRUCTURE_MAX_CHILDREN="4") as settings:
        verdict = s.verifier(proposition, registre, doc_id=DOC, settings=settings)
    assert not verdict.accepte and verdict.motif == "largeur_excessive"
    assert verdict.motif in s.MOTIFS and "STRUCTURE_MAX_NODES" in verdict.detail


def test_le_nombre_denfants_est_borne_racines_comprises(monkeypatch: pytest.MonkeyPatch) -> None:
    pages, plate = _plate(4)
    registre = _registre(pages)
    with _regle(monkeypatch, STRUCTURE_MAX_CHILDREN="4") as settings:
        assert s.verifier(plate, registre, doc_id=DOC, settings=settings).accepte
    with _regle(monkeypatch, STRUCTURE_MAX_CHILDREN="3") as settings:
        verdict = s.verifier(plate, registre, doc_id=DOC, settings=settings)
    assert not verdict.accepte and verdict.motif == "largeur_excessive"
    assert "racine" in verdict.detail and "STRUCTURE_MAX_CHILDREN" in verdict.detail


def test_la_largeur_se_compte_par_parent_et_pas_seulement_a_la_racine(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Une racine unique et quatre enfants sous elle : la borne porte sur **chaque** fratrie."""
    pages = [_page(1, [f"Ligne source numero {index}." for index in range(1, 6)])]
    registre = _registre(pages)
    proposition = _proposition(noeuds=[
        s.NoeudPropose(titre_line_uid="p1:l1", premiere_line_uid="p1:l1", derniere_line_uid="p1:l5"),
        *(s.NoeudPropose(titre_line_uid=f"p1:l{index}", premiere_line_uid=f"p1:l{index}",
                         derniere_line_uid=f"p1:l{index}", parent_line_uid="p1:l1")
          for index in range(2, 6)),
    ])
    with _regle(monkeypatch, STRUCTURE_MAX_CHILDREN="4", STRUCTURE_MAX_NODES="5") as settings:
        assert s.verifier(proposition, registre, doc_id=DOC, settings=settings).accepte
    with _regle(monkeypatch, STRUCTURE_MAX_CHILDREN="3", STRUCTURE_MAX_NODES="5") as settings:
        verdict = s.verifier(proposition, registre, doc_id=DOC, settings=settings)
    assert not verdict.accepte and verdict.motif == "largeur_excessive"
    assert "'p1:l1'" in verdict.detail  # la fratrie fautive est nommée, et ce n'est pas la racine


def test_le_schema_fournisseur_borne_le_nombre_de_noeuds_proposables(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Premier des cinq points : la surface d'écriture du modèle est bornée par le schéma lui-même.

    Le nombre d'enfants n'est pas exprimable en JSON Schema ; `maxItems` le borne transitivement,
    une fratrie ne pouvant pas compter plus de nœuds que l'arbre entier.
    """
    registre = _registre(_corpus())
    schema = s.requete(registre, DOC, get_settings())["output_config"]["format"]["schema"]
    assert schema["properties"]["noeuds"]["maxItems"] == get_settings().structure_max_nodes
    with _regle(monkeypatch, STRUCTURE_MAX_NODES="3") as settings:
        borne = s.requete(registre, DOC, settings)["output_config"]["format"]["schema"]
    assert borne["properties"]["noeuds"]["maxItems"] == 3


def test_le_modele_pydantic_reste_independant_des_settings_dynamiques(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Le DTO ne capture pas le singleton Settings; la requête et le parse portent la borne."""
    _pages, proposition = _plate(4)
    charge = proposition.model_dump()
    with _regle(monkeypatch, STRUCTURE_MAX_NODES="4", STRUCTURE_MAX_CHILDREN="4"):
        assert len(s.StructureProposee.model_validate(charge).noeuds) == 4  # à la borne : bâtie
    with _regle(monkeypatch, STRUCTURE_MAX_NODES="3"):
        assert len(s.StructureProposee.model_validate(charge).noeuds) == 4
    with _regle(monkeypatch, STRUCTURE_MAX_CHILDREN="3"):
        assert len(s.StructureProposee.model_validate(charge).noeuds) == 4


def test_parse_proposition_refuse_une_reponse_plus_large_que_la_borne(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Troisième point : la défense locale, **même si le schéma l'impose** (idiome `type_clauses`).

    Le refus est un `ValueError` **exactement** — pas la `ValidationError` que rendrait le modèle
    bâti en fin de fonction. Sans cette exigence, la sonde qui retire la garde de `parse_proposition`
    reste verte : le contrôle serait alors celui du modèle, hérité, et un `model_construct` ou un
    remaniement du modèle le ferait disparaître sans que rien ne rougisse.
    """
    pages, proposition = _plate(4)
    registre = _registre(pages)
    brut = json.dumps({"noeuds": [noeud.model_dump() for noeud in proposition.noeuds]})
    with _regle(monkeypatch, STRUCTURE_MAX_NODES="4", STRUCTURE_MAX_CHILDREN="4") as settings:
        assert len(s.parse_proposition(brut, registre, DOC, settings=settings).noeuds) == 4
    for bornes in ({"STRUCTURE_MAX_NODES": "3", "STRUCTURE_MAX_CHILDREN": "4"},
                   {"STRUCTURE_MAX_NODES": "4", "STRUCTURE_MAX_CHILDREN": "3"}):
        with _regle(monkeypatch, **bornes) as settings:
            with pytest.raises(ValueError, match="largeur_excessive") as leve:
                s.parse_proposition(brut, registre, DOC, settings=settings)
        assert type(leve.value) is ValueError, bornes  # refusé **avant** toute construction


def test_charger_refuse_un_artefact_plus_lourd_que_la_charge_utile_sans_le_lire(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Quatrième point, **au plus tôt** : la taille du fichier est jugée avant toute désérialisation.

    Le contenu déposé n'est pas du JSON : si l'octet avait été lu et analysé, le refus serait
    `proposition_illisible`. Qu'il soit `largeur_excessive` prouve que la borne d'entrée précède la
    lecture — c'est ce qui empêche un `structure.json` de dix millions de nœuds d'être entièrement
    désérialisé puis vérifié avant d'être rejeté.
    """
    chemin = tmp_path / "structure.json"
    chemin.write_text("x" * 400, "utf-8")
    with _regle(monkeypatch, STRUCTURE_MAX_INPUT_CHARS="200"):
        with pytest.raises(s.StructureRefusee) as leve:
            s.charger(chemin)
    assert leve.value.motif == "largeur_excessive" and "octet" in leve.value.detail
    with _regle(monkeypatch, STRUCTURE_MAX_INPUT_CHARS="400"):  # à la borne : lu, donc illisible
        with pytest.raises(s.StructureRefusee) as tolere:
            s.charger(chemin)
    assert tolere.value.motif == "proposition_illisible"


def test_charger_ne_croit_pas_la_taille_annoncee_par_le_systeme_de_fichiers(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """La lecture s'arrête d'elle-même un octet au-delà de la borne, quoi qu'annonce `stat()`.

    Un fichier qui grandit entre l'appel et la lecture, ou dont l'entrée de répertoire a changé de
    cible entre-temps, ferait sinon entrer sans borne ce que `stat()` avait déclaré minuscule.
    """
    import stat as statmod

    chemin = tmp_path / "structure.json"
    chemin.write_text("x" * 400, "utf-8")
    monkeypatch.setattr(Path, "stat", lambda self, **_: SimpleNamespace(
        st_size=0, st_mode=statmod.S_IFREG | 0o644))
    with _regle(monkeypatch, STRUCTURE_MAX_INPUT_CHARS="200"):
        with pytest.raises(s.StructureRefusee) as leve:
            s.charger(chemin)
    assert leve.value.motif == "largeur_excessive" and "jamais lu au-delà" in leve.value.detail


def test_un_artefact_profondement_imbrique_reste_un_refus_du_vocabulaire_ferme(
        tmp_path: Path) -> None:
    """`RecursionError` sort de l'analyseur JSON, pas du vocabulaire : elle est rattrapée ici.

    Non rattrapée, elle remonterait à `run()` sous le motif générique `source_illisible`, alors que
    le check `structure_proposee` promet un mot du vocabulaire fermé pour tout ce qui est présent.
    """
    chemin = tmp_path / "structure.json"
    chemin.write_bytes(b"[" * 60_000 + b"]" * 60_000)
    with pytest.raises(s.StructureRefusee) as leve:
        s.charger(chemin)
    assert leve.value.motif == "proposition_illisible" and leve.value.motif in s.MOTIFS


def test_charger_refuse_une_proposition_trop_large_sous_son_motif_dedie(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Quatrième point : un artefact de disque est borné **avant** que le moindre modèle soit bâti."""
    _pages, proposition = _plate(4)
    chemin = tmp_path / "structure.json"
    chemin.write_text(json.dumps(proposition.model_dump()), "utf-8")
    with _regle(monkeypatch, STRUCTURE_MAX_NODES="4", STRUCTURE_MAX_CHILDREN="4"):
        assert len(s.charger(chemin).noeuds) == 4  # à la borne exacte : chargé
    for borne in ("STRUCTURE_MAX_NODES", "STRUCTURE_MAX_CHILDREN"):
        with _regle(monkeypatch, **{borne: "3"}):
            with pytest.raises(s.StructureRefusee) as leve:
                s.charger(chemin)
        # Jamais `proposition_illisible` : le motif du vocabulaire fermé dit **ce qui** est refusé.
        assert leve.value.motif == "largeur_excessive", borne


def test_verifier_et_arbre_refusent_une_proposition_batie_hors_du_modele(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Cinquième point : `model_construct` contourne le modèle — comme tout appel programmatique.

    `arbre()` est documenté « sur une proposition déjà vérifiée » ; sans garde, un appel direct
    referait en O(n²) le travail que le vérificateur refuse.
    """
    pages, proposition = _plate(4)
    registre = _registre(pages)
    brute = s.StructureProposee.model_construct(schema_version="1", doc_id=DOC,
                                                noeuds=proposition.noeuds)
    with _regle(monkeypatch, STRUCTURE_MAX_NODES="3") as settings:
        verdict = s.verifier(brute, registre, doc_id=DOC, settings=settings)
        assert not verdict.accepte and verdict.motif == "largeur_excessive"
        with pytest.raises(s.StructureRefusee) as leve:
            s.arbre(brute, registre, DOC)
    assert leve.value.motif == "largeur_excessive"


def test_un_line_uid_demesure_est_refuse_par_le_modele() -> None:
    """La largeur bornée ne sert à rien si un seul `uid` peut peser autant que tout l'artefact."""
    with pytest.raises(ValidationError):
        s.NoeudPropose(titre_line_uid="p1:l" + "9" * 10_000, premiere_line_uid="p1:l1",
                       derniere_line_uid="p1:l1")
    assert len("p1:l" + "9" * 12) <= s.LINE_UID_MAX  # un uid réel reste très en deçà


def test_une_structure_json_trop_large_met_le_document_en_quarantaine(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bout en bout : la largeur hors borne est un check `bloquant`, jamais un repli (AD-16)."""
    dossier = _dossier(tmp_path)
    proposition = _proposition_du_document(dossier, noeuds=4)
    (dossier / "structure.json").write_text(
        json.dumps(proposition.model_dump(), ensure_ascii=False), "utf-8")
    with _regle(monkeypatch, STRUCTURE_MAX_NODES="3"):
        report, entry = p.run(dossier, edition="test 2026", doc_id=DOC, title="Contrat")
    check = next(c for c in report.checks if c.name == "structure_proposee")
    assert check.level == "bloquant" and "largeur_excessive" in check.detail
    assert entry.status == "quarantaine" and not (dossier / "document.json").exists()


def test_les_bornes_du_verificateur_entrent_dans_lempreinte_seulement_avec_une_proposition(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Elles font basculer accepté/refusé : elles changent donc l'arbre servi — mais elles seules.

    Un document **sans** `structure.json` n'est concerné par aucune d'elles : les faire entrer
    inconditionnellement dans l'empreinte invaliderait ses artefacts sans raison (AD-2).
    """
    proposition = _proposition()
    sans, avec = p.ingest_fingerprint(), p.ingest_fingerprint(proposition)
    assert sans != avec
    for borne, valeur in (("STRUCTURE_MAX_NODES", "7"), ("STRUCTURE_MAX_CHILDREN", "7"),
                          ("STRUCTURE_MAX_DEPTH", "5"), ("STRUCTURE_MIN_COVERAGE", "0.5"),
                          ("STRUCTURE_MAX_INPUT_CHARS", "800000")):
        with _regle(monkeypatch, **{borne: valeur}):
            assert p.ingest_fingerprint() == sans, borne
            assert p.ingest_fingerprint(proposition) != avec, borne
    # Ce qui borne la **fabrication** hors ligne de l'artefact n'entre nulle part : ni la sortie du
    # modèle, ni le plafond de coût ne peuvent changer un arbre déjà accepté.
    for hors_sujet, valeur in (("STRUCTURE_MAX_OUTPUT_TOKENS", "8000"),
                               ("STRUCTURE_MAX_COST_EUR", "4.0")):
        with _regle(monkeypatch, **{hors_sujet: valeur}):
            assert p.ingest_fingerprint() == sans and p.ingest_fingerprint(proposition) == avec
    # La version des règles de vérification suit le même partage que les bornes qu'elle gouverne.
    monkeypatch.setattr(p, "STRUCTURE_RULES_VERSION", "mutation-des-regles")
    assert p.ingest_fingerprint() == sans and p.ingest_fingerprint(proposition) != avec


# --- Colonnes et proposition, éprouvées ensemble ------------------------------------------------

def _page_a_deux_colonnes_avec_registre() -> p.PageText:
    """Les fixtures de colonnes, **dans l'ordre d'extraction réel**, munies de leur registre.

    `tests/test_colonnes.py` monte ses pages entrelacées comme l'extracteur les rend ; il ne leur
    donne pas de registre, et les corpus de structure sont mono-colonne. Les deux moitiés de la story
    ne se rencontraient donc jamais : le champ `colonne` de la charge utile valait `1` partout.
    """
    from tests.test_colonnes import DROITE_X, GAUCHE_X, _colonne, _extrait

    registre = p.SourceRegistry()
    lines = _extrait(_colonne("G", GAUCHE_X), _colonne("D", DROITE_X))
    for line in lines:
        line.source_uids = [registre.add(page=1, text=line.text, bbox=line.bbox).uid]
    return p.PageText(page=1, width=595, height=842, lines=lines, source=registre)


def test_une_page_a_deux_colonnes_porte_une_proposition_verifiee() -> None:
    """Given une page à deux colonnes, when une proposition est vérifiée, then l'arbre suit la lecture.

    La proposition n'est pas écrite sur l'ordre d'extraction — entrelacé — mais sur l'**ordre de
    lecture** que la détection de colonnes arrête : c'est ce couplage que la story promet et que rien
    n'éprouvait.
    """
    page = _page_a_deux_colonnes_avec_registre()
    registre = _registre([page])
    entrees = sorted(registre.values(), key=lambda entree: entree.ordre)
    charge = json.loads(s.demande(registre, get_settings()))
    assert {ligne["colonne"] for ligne in charge["lignes"]} == {1, 2}  # le champ n'est pas une constante
    gauche = [entree for entree in entrees if entree.colonne == 1]
    droite = [entree for entree in entrees if entree.colonne == 2]
    # La lecture épuise une colonne avant l'autre : chaque colonne est un intervalle **contigu**,
    # donc proposable. Sur l'ordre entrelacé d'avant, aucun des deux ne l'aurait été.
    assert [entree.ordre for entree in gauche] == list(range(1, len(gauche) + 1))
    assert [entree.ordre for entree in droite] == list(range(len(gauche) + 1, len(entrees) + 1))
    proposition = s.StructureProposee(schema_version="1", doc_id=DOC, noeuds=[
        s.NoeudPropose(titre_line_uid=cote[0].uid, premiere_line_uid=cote[0].uid,
                       derniere_line_uid=cote[-1].uid)
        for cote in (gauche, droite)
    ])
    assert s.verifier(proposition, registre, doc_id=DOC, settings=get_settings()).accepte
    document, meta = p.build_document([page], edition="2026", source_hash="0" * 64, toc=[],
                                      doc_id=DOC, title="Contrat", structure=proposition)
    par_id = {node.node_id: node for node in document.nodes}
    assert set(par_id) == {DOC, f"{DOC}:s1", f"{DOC}:s2"}
    assert par_id[f"{DOC}:s1"].title == gauche[0].texte and par_id[f"{DOC}:s2"].title == droite[0].texte
    # Aucun nœud ne mêle deux colonnes, et aucun bloc non plus : les deux invariants tiennent ensemble.
    for node_id, cote in ((f"{DOC}:s1", "G"), (f"{DOC}:s2", "D")):
        textes = [line.text for block_id in par_id[node_id].blocks
                  for line in document.block(block_id).lines]
        assert textes and all(texte.startswith(cote) for texte in textes), node_id
    assert all(len({line.text[:1] for line in block.lines}) == 1 for block in document.blocks)
    assert p.anomalies_registre([page], meta["source_uids"]) == []


# --- Bout en bout : servi sur proposition acceptée, quarantaine sur refus -----------------------

def _dossier(tmp_path: Path) -> Path:
    from tests.test_pdf_to_blocks import build_pdf, nominal_pages

    dossier = tmp_path / "data" / DOC
    dossier.mkdir(parents=True)
    build_pdf(dossier / "source.pdf", pages=nominal_pages())
    (dossier / "source.url").write_text("https://example.test/contrat.pdf\n", "utf-8")
    return dossier


def test_sans_structure_json_lheuristique_reste_le_chemin_nominal(tmp_path: Path) -> None:
    """Sans proposition, l'heuristique numérique reste le chemin nominal **et le rapport ne bouge pas**.

    Le check `structure_proposee` n'est alors **pas émis** : l'émettre malgré tout ajouterait un nom
    à la liste `checks` de chaque document déjà servi. Les `report.json` committés — produits avant
    cette story et relus ici tels quels — ne le portent pas, et la porte de déploiement les rejoue.
    « Aucune proposition » n'est pas un état à publier : c'est l'état d'avant la story.
    """
    dossier = _dossier(tmp_path)
    report, entry = p.run(dossier, edition="test 2026", doc_id=DOC, title="Contrat")
    noms = [c.name for c in report.checks]
    assert "structure_proposee" not in noms
    assert not any("structure" in nom for nom in noms)  # aucun nom introduit par 4.2c
    avant_la_story = {c["name"]
                      for chemin in sorted((Path(__file__).resolve().parents[1] / "data").glob("*/report.json"))
                      for c in json.loads(chemin.read_text("utf-8"))["checks"]}
    assert avant_la_story and set(noms) <= avant_la_story
    assert entry.status == "servi" and (dossier / "document.json").is_file()


def _proposition_du_document(dossier: Path, *, noeuds: int = 2) -> s.StructureProposee:
    """Deux sections contiguës sur les lignes réellement extraites du PDF déposé — couverture entière."""
    pages, _toc = p.extract_pages(dossier / "source.pdf")
    entrees = sorted(_registre(pages).values(), key=lambda entree: entree.ordre)
    coupes = [round(index * len(entrees) / noeuds) for index in range(noeuds)] + [len(entrees)]
    return s.StructureProposee(schema_version="1", doc_id=DOC, noeuds=[
        s.NoeudPropose(titre_line_uid=entrees[debut].uid, premiere_line_uid=entrees[debut].uid,
                       derniere_line_uid=entrees[fin - 1].uid)
        for debut, fin in zip(coupes, coupes[1:])
    ])


def test_une_structure_json_acceptee_est_servie_de_bout_en_bout(tmp_path: Path) -> None:
    """AC : le chemin **accepté**, joué par `run()` — le seul que les trois autres ne jouaient pas.

    « Pas de `structure.json` », « refusé » et « hors schéma » prouvaient les refus ; ils ne
    prouvaient jamais qu'une proposition valide traverse l'ingestion complète et ressorte servie.
    """
    dossier = _dossier(tmp_path)
    proposition = _proposition_du_document(dossier)
    (dossier / "structure.json").write_text(
        json.dumps(proposition.model_dump(), ensure_ascii=False, indent=2) + "\n", "utf-8")
    report, entry = p.run(dossier, edition="test 2026", doc_id=DOC, title="Contrat")
    check = next(c for c in report.checks if c.name == "structure_proposee")
    assert check.level == "info" and f"{len(proposition.noeuds)} nœud(s)" in check.detail
    assert not report.blocking and entry.status == "servi"
    document = Document.model_validate_json((dossier / "document.json").read_bytes())
    proposes = [node for node in document.nodes if node.node_id != DOC]
    assert [node.node_id for node in proposes] == [f"{DOC}:s{rang}" for rang in (1, 2)]
    assert all(node.title and node.blocks for node in proposes)  # titres relus au registre
    assert not any(node.node_id.startswith(f"{DOC}:a") for node in document.nodes)
    assert (dossier / "summary.md").is_file() and f"`{DOC}:s1`" in (dossier / "summary.md").read_text("utf-8")
    # L'empreinte servie **inclut** la proposition appliquée : sans elle, le même PDF rendrait
    # `:a…` ou `:s…` selon la présence du fichier, sans qu'aucune des deux valeurs du loader ne bouge.
    assert entry.ingest_fingerprint == document.ingest_fingerprint == p.ingest_fingerprint(proposition)
    assert entry.ingest_fingerprint != p.ingest_fingerprint()
    manifest = json.loads((dossier.parent / "manifest.json").read_text("utf-8"))[DOC]
    assert manifest["status"] == "servi" and manifest["ingest_fingerprint"] == entry.ingest_fingerprint
    assert manifest["document_hash"] == hashlib.sha256((dossier / "document.json").read_bytes()).hexdigest()


def test_une_structure_json_refusee_met_le_document_en_quarantaine(tmp_path: Path) -> None:
    """AC : refus ⇒ check `bloquant`, manifest en quarantaine, artefacts périmés purgés."""
    dossier = _dossier(tmp_path)
    p.run(dossier, edition="test 2026", doc_id=DOC, title="Contrat")
    assert (dossier / "document.json").is_file()
    pages, _toc = p.extract_pages(dossier / "source.pdf")
    ancre = next(iter(_registre(pages)))  # une ligne réellement retenue : le refus porte ailleurs
    (dossier / "structure.json").write_text(json.dumps({
        "schema_version": "1", "doc_id": DOC,
        "noeuds": [{"titre_line_uid": ancre, "premiere_line_uid": ancre,
                    "derniere_line_uid": ancre, "parent_line_uid": None}],
    }), "utf-8")
    report, entry = p.run(dossier, edition="test 2026", doc_id=DOC, title="Contrat")
    check = next(c for c in report.checks if c.name == "structure_proposee")
    assert check.level == "bloquant" and "ligne_omise" in check.detail
    assert report.blocking and entry.status == "quarantaine"
    assert not (dossier / "document.json").exists() and not (dossier / "summary.md").exists()
    manifest = json.loads((dossier.parent / "manifest.json").read_text("utf-8"))[DOC]
    assert manifest["status"] == "quarantaine"


def test_une_seule_ligne_omise_dun_document_entier_met_le_document_en_quarantaine(
        tmp_path: Path) -> None:
    """Bout en bout, sur la sonde de revue : **une** ligne hors des intervalles suffit à refuser.

    Sur un document de plusieurs dizaines de lignes, l'omission d'une seule laissait la couverture
    très au-dessus de la borne : le document était servi, et sa dernière ligne rattachée au nœud
    voisin par héritage. Le rapport annonçait alors « proposition vérifiée » sur un arbre qui
    contenait une affectation que personne n'avait prouvée.
    """
    dossier = _dossier(tmp_path)
    pages, _toc = p.extract_pages(dossier / "source.pdf")
    entrees = sorted(_registre(pages).values(), key=lambda entree: entree.ordre)
    assert len(entrees) >= 10  # la couverture reste au-dessus de 90 % : c'est tout l'enjeu
    proposition = s.StructureProposee(schema_version="1", doc_id=DOC, noeuds=[
        s.NoeudPropose(titre_line_uid=entrees[0].uid, premiere_line_uid=entrees[0].uid,
                       derniere_line_uid=entrees[-2].uid),  # la dernière ligne reste hors de tout nœud
    ])
    (dossier / "structure.json").write_text(
        json.dumps(proposition.model_dump(), ensure_ascii=False, indent=2) + "\n", "utf-8")
    report, entry = p.run(dossier, edition="test 2026", doc_id=DOC, title="Contrat")
    check = next(c for c in report.checks if c.name == "structure_proposee")
    assert check.level == "bloquant" and "ligne_omise" in check.detail
    assert entrees[-1].uid in check.detail
    assert entry.status == "quarantaine" and not (dossier / "document.json").exists()


def test_une_structure_json_hors_schema_est_un_refus_nomme_et_non_une_trace(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    (dossier / "structure.json").write_text('{"schema_version": "1", "noeuds": "tout"}', "utf-8")
    report, entry = p.run(dossier, edition="test 2026", doc_id=DOC, title="Contrat")
    check = next(c for c in report.checks if c.name == "structure_proposee")
    assert check.level == "bloquant" and "proposition_illisible" in check.detail
    assert "Traceback" not in check.detail and entry.status == "quarantaine"


def _poser_artefact_non_regulier(chemin: Path, forme: str) -> None:
    if forme == "repertoire":
        chemin.mkdir()
    elif forme == "lien_pendant":
        chemin.symlink_to(chemin.parent / "proposition-absente.json")
    elif forme == "lien_vers_repertoire":
        (chemin.parent / "ailleurs").mkdir()
        chemin.symlink_to(chemin.parent / "ailleurs")
    elif forme == "tube":
        os.mkfifo(chemin)  # lire un tube sans écrivain **bloque** : il faut refuser sans ouvrir
    else:  # pragma: no cover - garde de programmation du test lui-même
        raise AssertionError(forme)


@pytest.mark.parametrize("forme", ["repertoire", "lien_pendant", "lien_vers_repertoire", "tube"])
def test_un_structure_json_present_mais_non_regulier_part_en_quarantaine(
        tmp_path: Path, forme: str) -> None:
    """AD-16 : « présent mais illisible » n'est pas « absent » — jamais de repli sur l'heuristique.

    `is_file()` répond `False` pour un répertoire, un lien pendant, un lien vers un répertoire ou un
    tube : le chargement était alors sauté et l'ingestion retombait **silencieusement** sur
    l'heuristique numérique, en servant le document, alors que `charger()` promet précisément de
    classer ces artefacts en `proposition_illisible`. La présence se juge au sens du système de
    fichiers (`lexists`, qui voit aussi le lien pendant), et tout ce qui existe passe par le refus
    nommé et la quarantaine.
    """
    dossier = _dossier(tmp_path)
    _poser_artefact_non_regulier(dossier / "structure.json", forme)
    report, entry = p.run(dossier, edition="test 2026", doc_id=DOC, title="Contrat")
    check = next(c for c in report.checks if c.name == "structure_proposee")
    assert check.level == "bloquant" and "proposition_illisible" in check.detail
    assert check.detail.count("proposition_illisible") and "Traceback" not in check.detail
    assert report.blocking and entry.status == "quarantaine"
    assert not (dossier / "document.json").exists() and not (dossier / "summary.md").exists()


@pytest.mark.parametrize("forme", ["repertoire", "lien_pendant", "lien_vers_repertoire", "tube"])
def test_charger_refuse_tout_artefact_non_regulier_sans_jamais_louvrir(
        tmp_path: Path, forme: str) -> None:
    """Le refus est rendu par `charger()` lui-même, sur la seule nature de l'entrée de répertoire."""
    chemin = tmp_path / "structure.json"
    _poser_artefact_non_regulier(chemin, forme)
    assert s.presente(chemin), "l'artefact existe : le traiter en absence est le repli interdit"
    with pytest.raises(s.StructureRefusee) as leve:
        s.charger(chemin)
    assert leve.value.motif == "proposition_illisible" and leve.value.motif in s.MOTIFS


def test_labsence_reste_une_absence_et_non_un_refus(tmp_path: Path) -> None:
    """La contrepartie stricte : rien à cet emplacement ⇒ l'heuristique reste le chemin nominal."""
    assert not s.presente(tmp_path / "structure.json")


# --- Permutation métamorphique ------------------------------------------------------------------

def _permuter(pages: list[p.PageText], *, prefixe: str, pages_decalees: int,
              translation: float) -> tuple[list[p.PageText], str]:
    """Préfixe de `doc_id`, décalage de pages, translation de bbox et inversion d'ordre des sections.

    Le préfixe est **lu** : c'est lui qui nomme le document permuté, rendu ici avec les pages. Déclaré
    puis jamais employé, il laissait la permutation d'identifiants se réduire au décalage des pages,
    et le `doc_id` de l'autre corpus était en réalité une constante écrite à la main dans chaque test.
    """
    permutees: list[p.PageText] = []
    for page in reversed(pages):
        registre = p.SourceRegistry()
        lines = []
        for source in page.source.lines:
            bbox = [value + translation for value in source.bbox]
            nouvelle = registre.add(page=page.page + pages_decalees, text=source.text, bbox=bbox)
            lines.append(p.PageLine(source.text, bbox, 10.0, source_uids=[nouvelle.uid]))
        permutees.append(p.PageText(page=page.page + pages_decalees, width=page.width,
                                    height=page.height, lines=lines, source=registre))
    return permutees, f"{prefixe}-{DOC}"


def _proposition_permutee(pages_decalees: int, doc_id: str) -> s.StructureProposee:
    """La même structure, exprimée sur les uid du corpus permuté (pages inversées et décalées)."""
    def uid(page: int, ligne: int) -> str:
        return f"p{page + pages_decalees}:l{ligne}"

    return s.StructureProposee(schema_version="1", doc_id=doc_id, noeuds=[
        s.NoeudPropose(titre_line_uid=uid(2, 1), premiere_line_uid=uid(2, 1),
                       derniere_line_uid=uid(2, 3)),
        s.NoeudPropose(titre_line_uid=uid(1, 1), premiere_line_uid=uid(1, 1),
                       derniere_line_uid=uid(1, 4)),
        s.NoeudPropose(titre_line_uid=uid(1, 3), premiere_line_uid=uid(1, 3),
                       derniere_line_uid=uid(1, 4), parent_line_uid=uid(1, 1)),
    ])


def test_le_verdict_est_invariant_sous_permutation_du_corpus() -> None:
    """Le vérificateur décide sur les positions relatives, jamais sur un identifiant particulier."""
    origine = _verdict(_proposition())
    permutees, doc_permute = _permuter(_corpus(), prefixe="miroir", pages_decalees=40, translation=17.0)
    registre = _registre(permutees)
    autre = s.verifier(_proposition_permutee(40, doc_permute), registre,
                       doc_id=doc_permute, settings=get_settings())
    assert origine.accepte and autre.accepte


def test_un_refus_reste_un_refus_du_meme_nom_sous_permutation() -> None:
    invalide = [("p1:l1", "p1:l1", "p1:l3", None), ("p1:l2", "p1:l2", "p1:l4", None)]
    origine = _verdict(_proposition(noeuds=[
        s.NoeudPropose(titre_line_uid=t, premiere_line_uid=a, derniere_line_uid=b, parent_line_uid=parent)
        for t, a, b, parent in invalide
    ]))
    permutees, doc_permute = _permuter(_corpus(), prefixe="miroir", pages_decalees=40, translation=17.0)
    autre = s.verifier(s.StructureProposee(schema_version="1", doc_id=doc_permute, noeuds=[
        s.NoeudPropose(titre_line_uid="p41:l1", premiere_line_uid="p41:l1", derniere_line_uid="p41:l3"),
        s.NoeudPropose(titre_line_uid="p41:l2", premiere_line_uid="p41:l2", derniere_line_uid="p41:l4"),
    ]), _registre(permutees), doc_id=doc_permute, settings=get_settings())
    assert origine.motif == autre.motif == "intervalles_croises"


def _verifier_branche_sur_un_uid(uid: str) -> Any:
    """`structure.verifier` **recompilé** avec une branche interdite sur un `uid` littéral.

    Le contrôle négatif doit porter sur la fonction de production. Observer un décideur écrit dans le
    test ne prouvait que ceci : la permutation attraperait un uid codé en dur… dans une fonction qui
    n'est pas celle qu'on teste. La source réelle est donc relue, la branche y est injectée juste
    après la docstring, et la mutante est compilée dans un espace de noms **séparé** : le module n'est
    jamais modifié, et la mutation disparaît avec le test.
    """
    lignes = inspect.getsource(s.verifier).splitlines()
    fin_docstring = max(index for index, ligne in enumerate(lignes) if ligne.strip() == '"""')
    mutante = [*lignes[:fin_docstring + 1],
               f"    if {uid!r} not in registre:",
               '        return _refus("ligne_inconnue", "branchement interdit sur un uid littéral")',
               *lignes[fin_docstring + 1:]]
    espace: dict[str, Any] = {}
    exec(compile("\n".join(mutante), "<verifier-mute>", "exec"), dict(vars(s)), espace)
    return espace["verifier"]


def test_controle_negatif_le_verificateur_de_production_branche_sur_un_uid_rougirait() -> None:
    """La permutation détecterait un branchement sur un uid **dans `verifier()` lui-même**.

    Sans ce contrôle, l'invariance observée plus haut pourrait être celle d'un test qui ne regarde
    rien : elle ne vaut que si l'on montre, sur le même code, que la permutation sait rougir.
    """
    origine = _registre(_corpus())
    permutees, doc_permute = _permuter(_corpus(), prefixe="miroir", pages_decalees=40, translation=17.0)
    permute = _registre(permutees)
    proposition_permutee = _proposition_permutee(40, doc_permute)
    mutante = _verifier_branche_sur_un_uid(next(iter(origine)))  # un uid du corpus, jamais écrit ici
    assert mutante(_proposition(), origine, doc_id=DOC, settings=get_settings()).accepte
    rouge = mutante(proposition_permutee, permute, doc_id=doc_permute, settings=get_settings())
    assert not rouge.accepte and rouge.motif == "ligne_inconnue"  # la permutation rougit : le contrôle tient
    # Le vérificateur réel, lui, accepte les deux corpus : il ne connaît aucun uid particulier.
    assert s.verifier(_proposition(), origine, doc_id=DOC, settings=get_settings()).accepte
    assert s.verifier(proposition_permutee, permute, doc_id=doc_permute,
                      settings=get_settings()).accepte


def test_le_vocabulaire_du_module_et_du_corpus_est_neutre() -> None:
    """Never 4.2c : ni assureur, ni cas du golden set, ni page réelle dans le module ou son corpus."""
    source = (inspect.getsource(s) + "".join(
        inspect.getsource(fabrique) for fabrique in
        (_corpus, _page, _proposition, _page_registre, _cas_de_registre, _proposition_du_document,
         _page_a_deux_colonnes_avec_registre, _permuter, _page_avec_table, _dossier_avec_table,
         _dix_lignes))).lower()
    for interdit in ("axa", "baloise", "optihome", "bougie", "canape", "s-bougie", "p34:12",
                     "congelateur", "cigarette"):
        assert interdit not in source, f"vocabulaire non neutre : {interdit!r}"


def test_le_registre_ne_fuit_pas_dans_les_artefacts_servis(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    p.run(dossier, edition="test 2026", doc_id=DOC, title="Contrat")
    brut = (dossier / "document.json").read_text("utf-8")
    assert "source_uids" not in brut and "p1:l1" not in brut
    document = Document.model_validate_json(brut)
    assert all(line.line_id.startswith(block.block_id)
               for block in document.blocks for line in block.lines)
    assert all(line.line_uid and line.line_uid.startswith("line-v1:")
               for block in document.blocks for line in block.lines)
    assert hashlib.sha256(brut.encode("utf-8")).hexdigest()  # artefact bien sérialisé


# --- N3 : l'entrypoint de production refuse avant tout appel payant -------------------------------

def test_la_cli_refuse_un_data_dir_non_installe_avant_toute_extraction_et_tout_appel(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """N3 : lot d'**une seule cible**, donc refus « lot mixte » structurellement inatteignable.

    `structure.main` écrit `structure.json` et rien d'autre : le contrôle de couverture d'avant, qui
    ne levait que sur un lot mixte ou deux racines, ne pouvait pas le voir. Le repli rootless était
    donc atteint après l'extraction **et** après le seul appel payant du module. Ici le refus tombe
    avant tout : ni extraction, ni client, ni écriture.
    """
    dossier = _dossier(tmp_path)
    monkeypatch.setattr(s.anthropic, "Anthropic", _aucun_client)
    monkeypatch.setattr("server.ingest.pdf_to_blocks.extract_pages", _aucune_extraction)
    code = s.main([DOC, "--data", str(dossier.parent)], output=io.StringIO())
    assert code == 2
    erreur = capsys.readouterr().err
    assert "aucune racine de publication ne couvre" in erreur and "--depot" in erreur
    assert not (dossier / "structure.json").exists()


# --- Revue du tour N1–N3 : le refus de publication, et la lecture unique de la proposition --------

def _dossier_sous_racine(tmp_path: Path) -> tuple[Path, Any]:
    """Le même dossier de document, mais **sous une racine posée**, structure comprise."""
    from server.evals.espace import EspacePublie

    dossier = _dossier(tmp_path)
    espace = EspacePublie(tmp_path, tmp_path / "data")
    espace.installer([Path("data") / DOC / nom for nom in
                      ("document.json", "summary.md", "report.json", "structure.json")]
                     + [Path("data") / "manifest.json"], migrer=True)
    return dossier, espace


def test_une_structure_changee_pendant_la_publication_est_un_check_bloquant_pas_une_trace(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue du tour N1–N3, constats 3 et 11 : un refus voulu, rendu comme tous les autres.

    Le contrôle « l'entrée nommerait une structure que ce document n'applique pas » est né de ce
    tour, et il était rendu par un `ValueError` nu levé **dans** la fabrique. Or `run()` promet mot
    pour mot « toute erreur de source devient un check bloquant, jamais une trace Python », et
    `main()` l'appelle sans `try` : l'opérateur recevait une trace là où tous les refus frères de ce
    module rendent `[bloquant] …`. Aucune sonde ne l'exerçait dans un sens ni dans l'autre — le
    supprimer entièrement laissait la suite verte, et l'entrée publiée pouvait de nouveau nommer une
    empreinte que le document n'applique pas.
    """
    from server.ingest.artifacts import LectureDuLot

    dossier, espace = _dossier_sous_racine(tmp_path)
    proposition = _proposition_du_document(dossier)
    espace.basculer([(dossier / "structure.json",
                      json.dumps(proposition.model_dump(), ensure_ascii=False, indent=2) + "\n")])

    vrai_empreinte = LectureDuLot.empreinte
    change = {"fait": False}

    def _empreinte_qui_a_bouge(self: LectureDuLot, cible: Path) -> str | None:
        if cible.name == "structure.json" and not change["fait"]:
            change["fait"] = True  # la structure publiée n'est plus celle qu'on a appliquée
            return "0" * 64
        return vrai_empreinte(self, cible)

    monkeypatch.setattr(LectureDuLot, "empreinte", _empreinte_qui_a_bouge)
    report, entry = p.run(dossier, edition="test 2026", doc_id=DOC, title="Contrat")
    monkeypatch.undo()

    assert change["fait"], "la structure n'a pas changé pendant la publication"
    assert [c.name for c in report.blocking] == ["structure_a_change"], (
        "le refus doit être un check bloquant nommé, jamais une trace Python que `main()` n'attrape pas")
    assert "relancer l'ingestion" in report.blocking[0].detail
    assert entry.status == "quarantaine"
    # Le document n'est pas publié : l'entrée ne peut pas nommer une structure qu'il n'applique pas.
    assert not (dossier / "document.json").exists()


def test_structure_json_nest_lu_quune_seule_fois_par_lingestion(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Revue du tour N1–N3, constat 7 : les octets **hachés** sont les octets **appliqués**.

    `charger()` puis `structure_hash()` ouvraient deux fois le même chemin. C'est exactement le
    défaut que N1 ferme côté lecteur — « les octets hachés sont les octets parsés » —, resté ouvert
    dans un **écrivain** : un remplacement entre les deux lectures faisait nommer au manifest une
    empreinte que le document publié n'applique pas, et le contrôle de cohérence du loader mettait
    alors le document en quarantaine pour une contradiction que l'ingestion venait d'écrire.
    """
    dossier, espace = _dossier_sous_racine(tmp_path)
    proposition = _proposition_du_document(dossier)
    espace.basculer([(dossier / "structure.json",
                      json.dumps(proposition.model_dump(), ensure_ascii=False, indent=2) + "\n")])

    ouvertures = {"n": 0}
    vrai_read = Path.read_bytes

    def _compter(chemin: Path) -> bytes:
        if chemin.name == "structure.json":
            ouvertures["n"] += 1
        return vrai_read(chemin)

    monkeypatch.setattr(Path, "read_bytes", _compter)
    report, _entry = p.run(dossier, edition="test 2026", doc_id=DOC, title="Contrat")
    monkeypatch.undo()

    assert not report.blocking, [c.name for c in report.blocking]
    assert ouvertures["n"] == 1, (
        f"{ouvertures['n']} lectures de structure.json pendant l'ingestion : les octets hachés ne "
        "sont pas, par construction, les octets appliqués")
