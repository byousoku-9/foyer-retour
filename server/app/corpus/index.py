"""AD-1 — Index déterministe du corpus et les quatre outils de *retrouver* : `sommaire`, `ouvrir_noeud`,
`chercher`, `definitions`. Les seuils (`node_window`, `limit`) sont passés par l'appelant depuis `config.py`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction

from server.app.domain import Block, BlockRef, Document, NodeRef, NodeWindow

from .loader import Corpus
from .text import normalize

_WORD = re.compile(r"[a-z0-9]+")


def words(text_norm: str) -> list[str]:
    """Mots d'un `text_norm` : suites alphanumériques (la ponctuation et l'apostrophe séparent)."""
    return _WORD.findall(text_norm)


@dataclass(frozen=True)
class _Entry:
    block: Block
    doc_id: str
    node_id: str
    rank: int  # ordre de lecture global
    tokens: frozenset[str]
    padded: str  # " mot mot … " pour les variantes multi-mots


def reading_order(doc: Document) -> list[tuple[str, str]]:
    """[(block_id, node_id)] par parcours en profondeur de `Node.items` depuis les racines."""
    by_id = {n.node_id: n for n in doc.nodes}
    referenced = {i.node_id for n in doc.nodes for i in n.items if isinstance(i, NodeRef)}
    roots = [n.node_id for n in doc.nodes if n.node_id not in referenced]
    out: list[tuple[str, str]] = []

    def visit(node_id: str) -> None:
        node = by_id[node_id]
        for item in node.items:
            if isinstance(item, BlockRef):
                out.append((item.block_id, node_id))
            else:
                visit(item.node_id)

    for r in roots:
        visit(r)
    return out


class Index:
    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus
        self._entries: list[_Entry] = []
        self._by_block: dict[str, _Entry] = {}
        self._block_frequencies: dict[str, dict[str, int]] = {}
        self._nodes: dict[str, tuple[str, list[str]]] = {}  # node_id → (doc_id, block_ids directs)
        self._levels: dict[str, int] = {}  # node_id → profondeur déclarée (AD-2), pour « la plus proche »
        for doc_id, doc in sorted(corpus.documents.items()):
            for n in doc.nodes:
                if n.node_id in self._nodes:
                    raise ValueError(f"node_id {n.node_id!r} présent dans {self._nodes[n.node_id][0]!r} et {doc_id!r}")
                self._nodes[n.node_id] = (doc_id, n.blocks)
                self._levels[n.node_id] = n.level
            for block_id, node_id in reading_order(doc):
                if block_id in self._by_block:
                    raise ValueError(f"block_id {block_id!r} présent dans {self._by_block[block_id].doc_id!r} et {doc_id!r}")
                b = doc.block(block_id)
                if not b.text_norm:
                    b.text_norm = normalize(b.text)
                ws = words(b.text_norm)
                e = _Entry(block=b, doc_id=doc_id, node_id=node_id, rank=len(self._entries),
                           tokens=frozenset(ws), padded=f" {' '.join(ws)} ")
                self._entries.append(e)
                self._by_block[block_id] = e
                frequencies = self._block_frequencies.setdefault(doc_id, {})
                for token in e.tokens:
                    frequencies[token] = frequencies.get(token, 0) + 1

    # --- accès ---------------------------------------------------------
    def parent_node(self, block_id: str) -> str:
        return self._by_block[block_id].node_id

    def doc_of(self, block_id: str) -> str:
        return self._by_block[block_id].doc_id

    def __len__(self) -> int:
        return len(self._entries)

    # --- outils AD-1 ---------------------------------------------------
    def sommaire(self, doc_id: str) -> str:
        return self.corpus.summaries[doc_id]

    def ouvrir_noeud(self, node_id: str, focus_block_id: str | None = None, cursor: int | None = None, *,
                     node_window: int) -> NodeWindow:
        """Blocs directs du nœud, par pages de `node_window` ; la page contenant `focus_block_id` si donné."""
        if node_window < 1:
            raise ValueError("node_window doit être ≥ 1")
        if focus_block_id is not None and cursor is not None:
            raise ValueError("focus_block_id et cursor sont exclusifs")
        doc_id, block_ids = self._nodes[node_id]
        start = 0
        if focus_block_id is not None:
            if focus_block_id not in block_ids:
                raise KeyError(f"{focus_block_id} n'est pas un bloc de {node_id}")
            start = (block_ids.index(focus_block_id) // node_window) * node_window
        elif cursor is not None:
            if cursor < 0 or cursor >= len(block_ids):
                raise ValueError(f"cursor {cursor} hors de [0, {len(block_ids)})")
            start = (cursor // node_window) * node_window
        end = start + node_window
        doc = self.corpus.documents[doc_id]
        return NodeWindow(node_id=node_id, blocks=[doc.block(b) for b in block_ids[start:end]],
                          truncated=len(block_ids) > node_window,
                          next_cursor=end if end < len(block_ids) else None)

    def chercher(self, termes: dict[str, list[str]] | Iterable[str], *, limit: int,
                 doc_id: str | None = None,
                 kinds_prioritaires: Iterable[str] | None = None) -> list[tuple[str, str]]:
        """Correspondance par couverture de mots entiers, puis kind et ordre de lecture.

        Le classement compte d'abord les canoniques dont une forme **composée** est entièrement
        couverte, puis tous les canoniques dont au moins une forme est entièrement couverte. Un mot
        isolé générique ne se retrouve ainsi pas au même rang qu'un sujet composé précis.
        Tant qu'aucun canonique n'est pleinement couvert, les couvertures partielles départagent les
        blocs de rappel ; dès qu'un bloc porte un plein, ses autres fragments ne bonifient plus son
        rang. Ils ne peuvent donc ni évincer une correspondance pleine, ni favoriser une clause par
        accumulation de mots-outils autour d'un plein. Pour une forme partielle, chaque mot est
        pondé par l'inverse du nombre de blocs du document qui le portent ;
        un mot fréquent contribue ainsi moins qu'un mot rare. Le mot présent le plus discriminant
        donne la contribution de la forme : accumuler des mots-outils fréquents dans le même bloc ne
        suffit donc pas à reléguer la clause qui porte le mot utile. Le meilleur score des formes d'un
        canonique est retenu, puis les scores partiels des canoniques sont additionnés. Les fractions
        exactes gardent les égalités de tri déterministes.

        `kinds_prioritaires` (story 1.8) n'écarte aucun bloc **par son kind** : à score égal, un bloc
        dont le `Block.kind` y figure passe devant les autres, et c'est tout ce que le tri fait. Il
        change en revanche **qui survit à `limit`** (revue 1.8) : au-delà du quota, ce sont les blocs
        rétrogradés qui tombent, et un bloc ordinaire ex æquo avec une clause peut donc disparaître du
        résultat alors qu'il y figurait sans priorité. C'est l'effet recherché — les clauses passent
        devant — mais ce n'est pas « rien ne change au-delà de l'ordre ».

        Le départage vit ici et non dans *retrouver* parce que le score — nombre de canoniques
        pleinement couverts puis somme des meilleures couvertures partielles — n'existe qu'ici :
        rendu au seul ordre des hits, il serait
        indevinable, et un tri par kind seul remonterait un bloc décisionnel anecdotique devant le
        paragraphe qui répond vraiment. Le pipeline sinistre y passe les quatre kinds décisionnels
        d'AD-6 ; le guide ne passe rien et son ordre de recherche est inchangé.
        """
        if limit < 1:
            raise ValueError("limit doit être ≥ 1")
        if isinstance(termes, str):
            raise TypeError("termes : dict[str, list[str]] ou liste de termes attendus, pas une chaîne")
        if doc_id is not None and doc_id not in self.corpus.documents:
            raise KeyError(doc_id)
        mapping = termes if isinstance(termes, dict) else {t: [] for t in termes}
        groups: list[list[frozenset[str]]] = []
        seen_canon: set[str] = set()  # deux clés de même forme normalisée ne comptent qu'une fois
        for canon, variants in mapping.items():
            forms = {frozenset(words(normalize(v))) for v in (canon, *variants)} - {frozenset()}
            canon_form = " ".join(words(normalize(canon)))
            if not forms or canon_form in seen_canon:
                continue
            seen_canon.add(canon_form)
            groups.append(sorted(forms, key=lambda form: tuple(sorted(form))))
        if not groups:
            return []
        prioritaires = frozenset(kinds_prioritaires or ())
        scored: list[tuple[int, int, Fraction, int, int, str, str]] = []
        for e in self._entries:
            if doc_id is not None and e.doc_id != doc_id:
                continue
            pleins = 0
            pleins_composes = 0
            partiels = Fraction()
            frequencies = self._block_frequencies[e.doc_id]
            for forms in groups:
                formes_pleines = [form for form in forms if form <= e.tokens]
                if formes_pleines:
                    pleins += 1
                    if any(len(form) > 1 for form in formes_pleines):
                        pleins_composes += 1
                    continue
                partiels += max(self._hit(e, form, frequencies) for form in forms)
            if pleins or partiels:
                rang_kind = 0 if e.block.kind in prioritaires else 1
                rappel = partiels if pleins == 0 else Fraction()
                scored.append((-pleins_composes, -pleins, -rappel, rang_kind, e.rank,
                               e.block.block_id, e.node_id))
        scored.sort()
        return [(b, n) for _, _, _, _, _, b, n in scored[:limit]]

    @staticmethod
    def _hit(e: _Entry, form: frozenset[str], frequencies: dict[str, int]) -> Fraction:
        """Couverture partielle pondérée ; zéro seulement quand aucun mot n'existe."""
        presents = e.tokens & form
        if not presents:
            return Fraction()
        # Un mot absent du document garde le poids maximal d'un mot rare dans le dénominateur.
        poids_forme = sum((Fraction(1, frequencies.get(token, 1)) for token in form), Fraction())
        poids_le_plus_discriminant = max(Fraction(1, frequencies[token]) for token in presents)
        return poids_le_plus_discriminant / poids_forme

    def definitions(self, termes: dict[str, list[str]] | Iterable[str], *, doc_id: str | None = None,
                    blocs_ouverts: Iterable[str] | None = None) -> list[tuple[str, str]]:
        """Blocs `kind="definition"` qui définissent un terme cherché, résolus dans la portée (AD-1, AD-2).

        Candidats : `defines` normalisé qui matche un terme (ou une variante si dict), en mots entiers,
        dans les deux sens (« jardin » trouve « mobilier de jardin » et réciproquement) — **et**, si
        `blocs_ouverts` est donné, `defines` qui apparaît en mots entiers dans le texte de l'un d'eux :
        AD-1 exige les définitions « des termes rencontrés dans les blocs ouverts », pas seulement de
        ceux de la question (revue Codex 1.4, B2). Une clause qui introduit elle-même un terme défini
        gardait sinon sa définition hors du contexte.

        Résolution (AD-2, « la plus proche dans la portée du bloc décisionnel, puis remontée vers les
        définitions communes ») — **par bloc à éclairer**, pas une seule fois pour tout le résultat
        (revue Codex 1.4, B2, tour 2). Le contexte d'un terme défini est l'ensemble des nœuds qu'il
        doit éclairer : tous les nœuds ouverts du document si le terme vient de la question, le seul
        nœud du bloc où il apparaît s'il a été rencontré à la lecture. Pour **chacun** de ces nœuds :
        la définition dont la portée (`Document.scope_nodes`) couvre le nœud — la plus profonde
        d'abord, une « par dérogation » (`overrides`) primant celle qu'elle déroge —, sinon la
        définition **commune** (sans portée, valide partout : c'est la remontée). Deux blocs ouverts de
        portées différentes reçoivent donc chacun la leur, et la définition commune n'est évincée que
        là où la dérogation s'applique. Aucune définition valide dans la portée ⇒ **aucune** n'est
        rendue : AD-1 dit « valides dans la portée », et une définition hors portée égarerait la
        rédaction plus sûrement qu'une définition absente. Quand le document n'a **aucun** bloc ouvert,
        aucun contexte ne peut invalider une portée : la commune d'abord, sinon l'ordre de lecture
        (c'est ce qui sort les deux définitions du contrat AXA, portées par `a1`, hors pipeline).

        Le suivi des `refs` des blocs ouverts est fait par *retrouver* (il seul sait ce qu'il a ouvert).
        """
        if isinstance(termes, str):
            raise TypeError("termes : dict[str, list[str]] ou liste de termes attendus, pas une chaîne")
        if doc_id is not None and doc_id not in self.corpus.documents:
            raise KeyError(doc_id)
        mapping = termes if isinstance(termes, dict) else {t: [] for t in termes}
        forms: set[str] = set()
        for canon, variants in mapping.items():
            for v in (canon, *variants):
                form = " ".join(words(normalize(v)))
                if form:
                    forms.add(form)
        ouverts = [self._by_block[b] for b in (blocs_ouverts or []) if b in self._by_block]
        if not forms and not ouverts:
            return []
        noeuds_par_doc: dict[str, set[str]] = {}
        for o in ouverts:
            noeuds_par_doc.setdefault(o.doc_id, set()).add(o.node_id)

        # Candidats, et pour chacun le **contexte** à éclairer : les nœuds ouverts du document quand le
        # terme vient de la question (elle porte sur tout le résultat), le nœud du bloc où il apparaît
        # quand il a été rencontré à la lecture (AD-2 : « la plus proche dans la portée du bloc »).
        par_terme: dict[tuple[str, str], list[_Entry]] = {}
        contexte: dict[tuple[str, str], set[str]] = {}
        for e in self._entries:
            if doc_id is not None and e.doc_id != doc_id:
                continue
            b = e.block
            if b.kind != "definition" or not b.defines:
                continue
            defined = " ".join(words(normalize(b.defines)))
            if not defined:
                continue
            de_la_question = any(f" {f} " in f" {defined} " or f" {defined} " in f" {f} " for f in forms)
            # AD-1 : terme rencontré dans un bloc ouvert (jamais dans la définition elle-même)
            rencontre = {o.node_id for o in ouverts
                         if o.doc_id == e.doc_id and o.block.block_id != b.block_id
                         and f" {defined} " in o.padded}
            if not de_la_question and not rencontre:
                continue
            cle = (e.doc_id, defined)
            par_terme.setdefault(cle, []).append(e)
            noeuds = contexte.setdefault(cle, set())
            noeuds |= rencontre
            if de_la_question:
                noeuds |= noeuds_par_doc.get(e.doc_id, set())

        retenus: dict[str, _Entry] = {}
        for cle, entries in par_terme.items():
            noeuds = contexte[cle]
            if not noeuds:  # aucun bloc ouvert dans ce document : aucune portée n'est invalidée
                choisi = min(entries, key=lambda e: (bool(self._portee_racines(e)), e.rank))
                retenus.setdefault(choisi.block.block_id, choisi)
                continue
            for noeud in sorted(noeuds):
                couvrantes = [e for e in entries if self._portee_couvre(e, {noeud})]
                # AD-2 : dans sa portée, la dérogation prime celle qu'elle déroge
                deroges = {e.block.overrides for e in couvrantes}
                couvrantes = [e for e in couvrantes if e.block.block_id not in deroges]
                if couvrantes:  # la plus proche : la portée la plus profonde, puis l'ordre de lecture
                    choisi = max(couvrantes, key=lambda e: (self._portee_profondeur(e), -e.rank))
                else:  # remontée vers la définition commune ; à défaut, rien n'est valide ici
                    communes = [e for e in entries if not self._portee_racines(e)]
                    if not communes:
                        continue
                    choisi = min(communes, key=lambda e: e.rank)
                retenus.setdefault(choisi.block.block_id, choisi)
        return [(e.block.block_id, e.node_id) for e in sorted(retenus.values(), key=lambda e: e.rank)]

    def _portee_racines(self, e: _Entry) -> list[str]:
        b = e.block
        return b.scope_node_ids or ([b.scope_node_id] if b.scope_node_id else [])

    def _portee_profondeur(self, e: _Entry) -> int:
        """Profondeur de la portée d'une définition ; -1 = aucune portée (définition commune)."""
        racines = self._portee_racines(e)
        return max((self._levels.get(r, 0) for r in racines), default=-1)

    def _portee_couvre(self, e: _Entry, noeuds: set[str]) -> bool:
        """La portée de la définition couvre-t-elle le nœud de l'un des blocs ouverts ?"""
        if not noeuds or not self._portee_racines(e):
            return False
        return bool(self.corpus.documents[e.doc_id].scope_nodes(e.block.block_id) & noeuds)
