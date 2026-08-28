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

import hashlib
import inspect
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.app.config import Settings, get_settings
from server.app.domain import Document
from server.ingest import pdf_to_blocks as p
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


def test_une_ligne_de_table_entre_au_registre_avec_son_motif() -> None:
    class TextPage:
        @staticmethod
        def get_text(kind: str, **options: Any) -> dict[str, Any]:
            return {"blocks": [{"type": 0, "lines": [{
                "bbox": [50, 0, 100, 10],
                "spans": [{"text": "cellule atomique", "font": "helv", "size": 10}],
            }]}]}

    registre = p.SourceRegistry()
    lignes, _ = p._raw_lines(TextPage(), page_no=3, excluded=[[40, 0, 110, 10]], registry=registre)
    assert lignes == [] and [line.uid for line in registre.lines] == ["p3:l1"]
    assert registre.removed == {"p3:l1": "ligne_de_table"}


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
    }
    assert noeud["additionalProperties"] is False and schema["additionalProperties"] is False
    assert noeud["properties"]["titre_line_uid"]["enum"] == list(registre)
    rendu = json.dumps(schema)
    for interdit in ("kind", "portee", "scope", "applicab", "verdict", "titre\"", "texte"):
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


class FauxMessages:
    """Double qui **lit la charge utile réelle** et répond à partir des seuls uid reçus."""

    def __init__(self, *, noeuds: Any = None, stop_reason: str = "end_turn",
                 usage: dict[str, int] | None = None) -> None:
        self.noeuds = noeuds
        self.stop_reason = stop_reason
        self.usage = {"input_tokens": 120, "output_tokens": 30} if usage is None else usage
        self.calls: list[dict[str, Any]] = []

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        lignes = json.loads(params["messages"][0]["content"])["lignes"]
        premiere, derniere = lignes[0]["uid"], lignes[-1]["uid"]
        noeuds = self.noeuds if self.noeuds is not None else [
            {"titre_line_uid": premiere, "premiere_line_uid": premiere,
             "derniere_line_uid": derniere, "parent_line_uid": None},
        ]
        return SimpleNamespace(usage=self.usage, stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text",
                                                        text=json.dumps({"noeuds": noeuds}))])


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


def test_le_prevol_de_cout_refuse_avant_toute_construction_de_client(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """AD-1/AD-9 : le majorant est comparé au plafond **avant** tout client (idiome `type_clauses`).

    `--dry-run` sans document sort avant le registre, la requête et l'estimation : il ne joue donc ni
    `majorant_eur`, ni `estimate_cost`, ni la comparaison au plafond, alors que `config.py` promet
    « majorant vérifié avant toute construction de client ». C'est ce chemin-là qui est joué ici, sur
    un document réel du système de fichiers, avec un plafond volontairement minuscule.
    """
    dossier = _dossier(tmp_path)
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
    monkeypatch.setattr(s.anthropic, "Anthropic", _aucun_client)
    reglages = Settings(_env_file=None, structure_max_cost_eur=0.0001)
    code = s.main([DOC, "--data", str(dossier.parent)], settings=reglages, output=io.StringIO())
    assert code == 3 and not (dossier / "structure.json").exists()


def test_sans_cle_anthropic_la_cli_refuse_sur_un_document(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """AD-14 : « sans clé, ça refuse » — y compris une fois le majorant passé, et sans rien écrire."""
    dossier = _dossier(tmp_path)
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
    # Couverture insuffisante : une seule ligne couverte sur sept.
    ([("p1:l1", "p1:l1", "p1:l1", None)], "couverture_insuffisante"),
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


def test_une_profondeur_hors_borne_est_refusee(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRUCTURE_MAX_DEPTH", "1")
    get_settings.cache_clear()
    try:
        verdict = _verdict(_proposition())
    finally:
        get_settings.cache_clear()
    assert not verdict.accepte and verdict.motif == "profondeur_excessive"


def test_une_couverture_hors_borne_est_refusee(monkeypatch: pytest.MonkeyPatch) -> None:
    """La borne de couverture est un réglage publié, pas un nombre en dur dans le vérificateur."""
    partielle = _proposition(noeuds=[
        s.NoeudPropose(titre_line_uid="p1:l1", premiere_line_uid="p1:l1", derniere_line_uid="p1:l4"),
    ])
    monkeypatch.setenv("STRUCTURE_MIN_COVERAGE", "0.5")
    get_settings.cache_clear()
    try:
        assert _verdict(partielle).accepte  # 4 lignes sur 7 : au-dessus de 50 %
        assert get_settings().thresholds()["structure_min_coverage"] == 0.5
    finally:
        monkeypatch.delenv("STRUCTURE_MIN_COVERAGE")
        get_settings.cache_clear()
    verdict = _verdict(partielle)
    assert not verdict.accepte and verdict.motif == "couverture_insuffisante"


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
    assert capture.value.motif == "couverture_insuffisante"


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
    assert check.level == "bloquant" and "couverture_insuffisante" in check.detail
    assert report.blocking and entry.status == "quarantaine"
    assert not (dossier / "document.json").exists() and not (dossier / "summary.md").exists()
    manifest = json.loads((dossier.parent / "manifest.json").read_text("utf-8"))[DOC]
    assert manifest["status"] == "quarantaine"


def test_une_structure_json_hors_schema_est_un_refus_nomme_et_non_une_trace(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    (dossier / "structure.json").write_text('{"schema_version": "1", "noeuds": "tout"}', "utf-8")
    report, entry = p.run(dossier, edition="test 2026", doc_id=DOC, title="Contrat")
    check = next(c for c in report.checks if c.name == "structure_proposee")
    assert check.level == "bloquant" and "proposition_illisible" in check.detail
    assert "Traceback" not in check.detail and entry.status == "quarantaine"


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
         _page_a_deux_colonnes_avec_registre, _permuter))).lower()
    for interdit in ("axa", "baloise", "optihome", "bougie", "canape", "s-bougie", "p34:12",
                     "congelateur", "cigarette"):
        assert interdit not in source, f"vocabulaire non neutre : {interdit!r}"


def test_le_registre_ne_fuit_pas_dans_les_artefacts_servis(tmp_path: Path) -> None:
    dossier = _dossier(tmp_path)
    p.run(dossier, edition="test 2026", doc_id=DOC, title="Contrat")
    brut = (dossier / "document.json").read_text("utf-8")
    assert "source_uids" not in brut and "line_uid" not in brut and "p1:l1" not in brut
    document = Document.model_validate_json(brut)
    assert all(line.line_id.startswith(block.block_id)
               for block in document.blocks for line in block.lines)
    assert hashlib.sha256(brut.encode("utf-8")).hexdigest()  # artefact bien sérialisé
