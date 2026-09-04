"""AD-1 — Index déterministe du corpus et les quatre outils de *retrouver* : `sommaire`, `ouvrir_noeud`,
`chercher`, `definitions`. Les seuils (`node_window`, `limit`) sont passés par l'appelant depuis `config.py`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction

from server.app.domain import (Block, BlockRef, ContextUnit, Document, Node, NodeChild, NodeRef,
                               NodeWindow, QuestionClauseScore, ScoredHit, SummaryEntry,
                               SummaryPage, is_citable)
from server.app.domain.retrieval import stable_uid

from .loader import Corpus
from .text import normalize

def _couper(text: str, limite: int) -> str:
    """Aperçu borné, coupé sur un mot et marqué — jamais au milieu d'un mot (comme `summary.md`)."""
    if limite <= 0 or not text:
        return ""
    if len(text) <= limite:
        return text
    coupe = text[:limite]
    if " " in coupe:
        coupe = coupe.rsplit(" ", 1)[0]
    return coupe.rstrip(" ,;:.") + "…"


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


# Repli de `Settings.excerpt_max_chars`, pour un index construit sans budget (tests, outils). La
# couche `corpus` n'importe que `domain` (table des couches du spine, `tests/test_layers.py`) : elle
# ne peut pas lire `config.py`, et un repli qui divergerait en silence du seuil publié serait pire
# que pas de repli du tout. `test_le_repli_dextrait_de_lindex_est_le_seuil_que_la_configuration_publie`
# interdit cette dérive. Le seuil, lui, **vit dans `config.py`** et se publie par `thresholds()` ;
# le chemin servi le passe explicitement.
EXCERPT_MAX_CHARS_REPLI = 1000


class Index:
    """Projection déterministe du corpus. Ses budgets arrivent de l'appelant (convention Seuils).

    Comme `node_window` et `limit`, les bornes de projection sont passées par celui qui les connaît.
    Celles de `sommaire_page` sont **obligatoires** : un sommaire sans budget servirait l'arbre
    entier, ce que le correctif G2 interdit précisément — sur le contrat AXA, cela voulait dire ses
    750 nœuds dans le préfixe de navigation, et le plafond de coût par requête franchi.
    """

    def __init__(self, corpus: Corpus, *, excerpt_max_chars: int = EXCERPT_MAX_CHARS_REPLI) -> None:
        if excerpt_max_chars < 1:
            raise ValueError("excerpt_max_chars doit être ≥ 1")
        self.corpus = corpus
        self.excerpt_max_chars = excerpt_max_chars
        self._entries: list[_Entry] = []
        self._by_block: dict[str, _Entry] = {}
        self._block_frequencies: dict[str, dict[str, int]] = {}
        self._nodes: dict[str, tuple[str, list[str]]] = {}  # node_id → (doc_id, block_ids directs)
        self._node_titles: dict[str, str] = {}
        self._node_children: dict[str, list[str]] = {}
        self._node_parents: dict[str, str] = {}
        self._node_relations: dict[str, list[tuple[str, str]]] = {}
        self._levels: dict[str, int] = {}  # node_id → profondeur déclarée (AD-2), pour « la plus proche »
        for doc_id, doc in sorted(corpus.documents.items()):
            for n in doc.nodes:
                if n.node_id in self._nodes:
                    raise ValueError(f"node_id {n.node_id!r} présent dans {self._nodes[n.node_id][0]!r} et {doc_id!r}")
                # `autre` et les tables explicitement marquées préliminaire/TdM restent auditables
                # dans `Document.blocks`, mais ne consomment jamais une fenêtre ni le budget de rappel.
                self._nodes[n.node_id] = (doc_id, [block_id for block_id in n.blocks
                                                   if is_citable(doc.block(block_id))])
                self._node_titles[n.node_id] = n.title
                self._node_children[n.node_id] = n.children
                self._node_relations[n.node_id] = [
                    (relation.target_node_id, relation.kind) for relation in n.relations
                ]
                for child in n.children:
                    self._node_parents[child] = n.node_id
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
                if is_citable(b):
                    frequencies = self._block_frequencies.setdefault(doc_id, {})
                    for token in e.tokens:
                        frequencies[token] = frequencies.get(token, 0) + 1

    # --- accès ---------------------------------------------------------
    def parent_node(self, block_id: str) -> str:
        return self._by_block[block_id].node_id

    def doc_of(self, block_id: str) -> str:
        return self._by_block[block_id].doc_id

    def doc_of_node(self, node_id: str) -> str:
        """Document propriétaire d'un nœud ; sert au verrou inter-document des outils."""
        return self._nodes[node_id][0]

    def __len__(self) -> int:
        return len(self._entries)

    # --- outils AD-1 ---------------------------------------------------
    def sommaire(self, doc_id: str) -> str:
        return self.corpus.summaries[doc_id]

    def _apercu_source(self, doc: Document, node: Node) -> str:
        """Le signal que le nœud porte lui-même : le texte de son premier bloc **citable** direct.

        Aucune règle propre à un document : c'est la même projection pour une fiche de guide (dont
        le premier bloc utile est son résumé) et pour une section de contrat (sa première clause).
        Un nœud sans bloc direct citable — une catégorie, un intertitre — n'en a pas, et le dit en
        rendant la chaîne vide plutôt qu'en empruntant celui d'un enfant.

        Un bloc qui **redit le titre** est sauté : le titre est déjà servi à côté, et un aperçu qui
        le recopie ne dit rien de plus au navigateur. C'est le cas du bloc `heading` qui ouvre
        chaque fiche du guide comme chaque section de contrat, et de tout premier bloc dont le
        texte normalisé est celui du nœud.
        """
        titre = normalize(node.title)
        for block_id in node.blocks:
            block = doc.block(block_id)
            if not is_citable(block):
                continue
            texte = " ".join(block.text.split())
            if block.kind == "heading" or (titre and normalize(texte) == titre):
                continue
            return texte
        return ""

    def _mise_en_page_du_sommaire(self, apercus: list[tuple[str, str, str]], *,
                                  page_max_chars: int, slice_max_chars: int,
                                  apercu_max_chars: int) -> tuple[int, int]:
        """(entrées par page, longueur d'aperçu), dérivées de la forme du document et des budgets.

        Le coût fixe moyen d'une entrée de *ce* document — identifiant + titre + enveloppe JSON —
        est **mesuré**, pas supposé : un contrat aux titres de 51 caractères et aux identifiants de
        30 ne coûte pas ce que coûte un guide aux titres de 32 et aux identifiants de 17. Cette
        mesure, confrontée aux budgets, décide de deux régimes — et rien d'autre ne décide.

        **A — la carte tient.** Le document entier, avec son aperçu, entre dans
        `summary_page_max_chars` : il est servi d'un bloc. Le navigateur voit tout le document et
        n'a aucune page à tourner, ce qui comptait quand un seul tour était outillé
        pour paginer, chercher **et** ouvrir. C'est le cas du guide : 87 fiches plates et courtes.

        **B — elle ne tient pas.** La carte est alors partielle *par nature* : quelle que soit sa
        taille, le navigateur devra chercher. Une tranche n'achète donc pas ce qu'achète une carte
        complète, et elle est bornée par un budget distinct et plus petit,
        `summary_slice_max_chars`, sans aperçu — le signal par entrée ne sert à rien quand on n'en
        voit qu'un seizième. C'est le cas du contrat AXA : 750 nœuds aux titres longs. Le préfixe de
        navigation est payé **à chaque requête**, et le contrat n'a pas de marge sur
        `max_cost_eur_per_request` (mesure du 02/09/2026) : gonfler sa tranche coûterait sans rien
        rendre.

        Aucune ligne ne connaît le guide ni le contrat : seule leur forme mesurée les sépare.
        """
        if not apercus:
            return 1, 0
        # Enveloppe JSON d'une entrée sérialisée par `model_dump` : les quatre clés, leurs guillemets,
        # les deux-points, les virgules et les accolades. Majorée, et comptée une fois pour toutes.
        enveloppe = 48
        fixe = sum(len(node_id) + len(title) for node_id, title, _ in apercus) // len(apercus)
        fixe += enveloppe
        if len(apercus) * (fixe + apercu_max_chars) <= page_max_chars:
            return len(apercus), apercu_max_chars          # régime A : la carte entière
        return max(1, slice_max_chars // fixe), 0          # régime B : une tranche, sans aperçu

    def sommaire_page(self, doc_id: str, *, page_max_chars: int, slice_max_chars: int,
                      apercu_max_chars: int, cursor: int = 0,
                      page_size: int | None = None) -> SummaryPage:
        """Navigation complète, paginée sur le budget de contexte, sans injection de l'arbre entier.

        Les deux budgets sont **obligatoires** : ils viennent de `config.py` par l'appelant, et sans
        eux il n'y a pas de page — seulement un arbre entier déversé dans un préfixe de navigation.
        `page_size` reste surchargeable pour qu'un appelant exprime une borne explicite ; laissé à
        `None` — le cas servi — il est **dérivé** de la forme du document et du budget (G2).
        """
        if cursor < 0 or (page_size is not None and page_size < 1):
            raise ValueError("cursor et page_size doivent être positifs")
        if page_max_chars < 1 or slice_max_chars < 1 or apercu_max_chars < 0:
            raise ValueError("les budgets de sommaire doivent être positifs")
        if doc_id not in self.corpus.documents:
            raise KeyError(doc_id)
        doc = self.corpus.documents[doc_id]
        noeuds = [node for node in doc.nodes if node.node_id != doc_id]
        apercus = [(node.node_id, node.title, self._apercu_source(doc, node)) for node in noeuds]
        derive, apercu_max = self._mise_en_page_du_sommaire(
            apercus, page_max_chars=page_max_chars, slice_max_chars=slice_max_chars,
            apercu_max_chars=apercu_max_chars)
        entries = [
            SummaryEntry(node_id=node.node_id, title=node.title, level=node.level,
                         apercu=_couper(apercu, apercu_max))
            for node, (_node_id, _title, apercu) in zip(noeuds, apercus, strict=True)
        ]
        if cursor > len(entries):
            raise ValueError("cursor hors sommaire")
        taille = derive if page_size is None else page_size
        page = tuple(entries[cursor:cursor + taille])
        next_cursor = cursor + len(page) if cursor + len(page) < len(entries) else None
        return SummaryPage(document_uid=doc_id, entries=page, cursor=cursor,
                           next_cursor=next_cursor, truncated=next_cursor is not None,
                           page_size=taille, total_entries=len(entries))

    def ouvrir_singleton(self, block_id: str, *, node_window: int) -> NodeWindow:
        """Cible et contexte typé, bornés au document et à la section propriétaire."""
        if node_window < 1:
            raise ValueError("node_window doit être ≥ 1")
        entry = self._by_block[block_id]
        doc = self.corpus.documents[entry.doc_id]
        target = entry.block
        candidates: list[tuple[str, str, str]] = [(block_id, "target", entry.node_id)]

        # Fermeture transitive de la chaîne `continues`, dans les deux sens, bornée par le nombre de
        # blocs du document. Chaque bloc est visité au plus une fois.
        continuation_ids: list[str] = []
        frontier = [block_id]
        visited = {block_id}
        while frontier:
            current = frontier.pop(0)
            current_block = doc.block(current)
            neighbors = [candidate.block_id for candidate in doc.blocks
                         if candidate.continues == current]
            if current_block.continues is not None:
                neighbors.append(current_block.continues)
            for candidate in neighbors:
                if (candidate in visited or candidate not in self._by_block
                        or self._by_block[candidate].node_id != entry.node_id):
                    continue
                visited.add(candidate)
                frontier.append(candidate)
                continuation_ids.append(candidate)
        candidates.extend((candidate, "same_clause_continuation", entry.node_id)
                          for candidate in continuation_ids)
        candidates.extend((candidate, "explicit_dependency", entry.node_id) for candidate in target.refs
                          if candidate in self._by_block
                          and self._by_block[candidate].doc_id == entry.doc_id
                          and self._by_block[candidate].node_id == entry.node_id)
        if target.overrides and target.overrides in self._by_block \
                and self._by_block[target.overrides].doc_id == entry.doc_id \
                and self._by_block[target.overrides].node_id == entry.node_id:
            candidates.append((target.overrides, "definition_override", entry.node_id))
        # Les relations Opus portent des nœuds, pas des blocs. Leur contexte est donc constitué des
        # blocs directs citables du nœud cible, avec le rôle exact de la relation acceptée.
        for target_node_id, role in self._node_relations.get(entry.node_id, []):
            if self._nodes[target_node_id][0] != entry.doc_id:
                continue
            candidates.extend((candidate, role, target_node_id)
                              for candidate in self._nodes[target_node_id][1])
        # L'amorce parent est volontairement dernière : elle est la première tronquée.
        parent_id = self._node_parents.get(entry.node_id)
        if parent_id is not None:
            candidates.extend((candidate, "parent_preamble", parent_id)
                              for candidate in self._nodes[parent_id][1])
        unique: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for candidate, role, section_id in candidates:
            if candidate not in seen and candidate in self._by_block:
                seen.add(candidate)
                unique.append((candidate, role, section_id))
        selected = unique[:node_window]
        blocks = [doc.block(candidate).model_copy(update={"context_role": role}, deep=True)
                  for candidate, role, _section_id in selected]
        units = [ContextUnit(
            block_uid=candidate, role=role, document_uid=entry.doc_id,
            section_uid=section_id, order=order,
        ) for order, (candidate, role, section_id) in enumerate(selected)]
        return NodeWindow(
            node_id=entry.node_id, title=self._node_titles[entry.node_id],
            children=[NodeChild(node_id=child, title=self._node_titles[child])
                      for child in self._node_children[entry.node_id]],
            blocks=blocks, context_units=units, truncated=len(selected) < len(unique),
        )

    def ouvrir_noeud(self, node_id: str, focus_block_id: str | None = None, cursor: int | None = None, *,
                     node_window: int) -> NodeWindow:
        """Blocs directs du nœud, par pages de `node_window` ; la page contenant `focus_block_id` si donné."""
        if node_window < 1:
            raise ValueError("node_window doit être ≥ 1")
        if focus_block_id is not None and cursor is not None:
            raise ValueError("focus_block_id et cursor sont exclusifs")
        doc_id, block_ids = self._nodes[node_id]
        if focus_block_id is not None and focus_block_id not in block_ids:
            raise KeyError(f"{focus_block_id} n'est pas un bloc de {node_id}")
        if focus_block_id is not None and len(block_ids) == 1:
            return self.ouvrir_singleton(block_ids[0], node_window=node_window)
        start = 0
        if focus_block_id is not None:
            start = (block_ids.index(focus_block_id) // node_window) * node_window
        elif cursor is not None:
            if cursor < 0 or cursor >= len(block_ids):
                raise ValueError(f"cursor {cursor} hors de [0, {len(block_ids)})")
            start = (cursor // node_window) * node_window
        end = start + node_window
        doc = self.corpus.documents[doc_id]
        next_cursor = end if end < len(block_ids) else None
        # Une page séquentielle n'est tronquée que s'il reste une page à lire. Une ouverture
        # focalisée, elle, omet aussi honnêtement ce qui précède sa fenêtre.
        truncated = next_cursor is not None or (focus_block_id is not None and start > 0)
        return NodeWindow(node_id=node_id, title=self._node_titles[node_id],
                          children=[NodeChild(node_id=child, title=self._node_titles[child])
                                    for child in self._node_children[node_id]],
                          blocks=[doc.block(b) for b in block_ids[start:end]],
                          truncated=truncated, next_cursor=next_cursor)

    def unite_de_renvoi(self, block_id: str) -> list[str]:
        """Unité locale minimale d'une cible de renvoi, sans fermeture récursive.

        Un titre seul situe une fiche mais ne porte généralement pas la règle à citer. Lorsqu'un
        renvoi cible un `heading`, son premier passage citable non-titre dans le même nœud voyage
        donc avec lui. La méthode ne consulte jamais les `refs` de la cible : elle ne produit qu'une
        fenêtre structurelle directe, ensuite admise atomiquement par *retrouver*.
        """
        entry = self._by_block[block_id]
        if entry.block.kind != "heading":
            return [block_id]
        _doc_id, directs = self._nodes[entry.node_id]
        try:
            start = directs.index(block_id) + 1
        except ValueError:
            return [block_id]
        for candidate in directs[start:]:
            if self._by_block[candidate].block.kind != "heading":
                return [block_id, candidate]
        return [block_id]

    def introduit_immediatement(self, amorce_id: str, item_id: str) -> bool:
        """`amorce_id` est-il la phrase qui **ouvre** `item_id`, immédiatement avant lui ?

        Story 5.6 (L1f). Une énumération s'écrit en deux blocs au moins : l'amorce (« Les dommages
        matériels subis par les biens assurés causés par : ») et l'item qui la complète. Le corpus
        les range de deux façons, et les deux se lisent **sur l'arbre**, jamais sur la ponctuation :

        - **le même nœud** : l'amorce est le bloc citable qui précède immédiatement l'item parmi les
          blocs directs du nœud — Baloise range ainsi « Sont exclus : » et l'exclusion qui suit ;
        - **le nœud parent direct** : l'amorce est portée par le nœud parent et l'item est la feuille
          à un seul bloc, cas déjà nommé par `amorce_de_lenumeration` — AXA range ainsi ses périls.

        Le prédicat sert *vérifier* (AD-3) : une amorce citée avec son item n'est pas une citation
        indépendante mais le contexte de l'item, et cette adjacence-là lève l'ambiguïté qu'un texte
        répété dans le document ferait peser sur elle. Comme `amorce_de_lenumeration`, l'amorce n'est
        jamais un titre : le code ne fabrique pas une unité de lecture à partir d'un titre.
        """
        try:
            amorce, item = self._by_block[amorce_id], self._by_block[item_id]
        except KeyError:
            return False  # un bloc non citable n'introduit rien et ne se cite pas
        if amorce_id == item_id or amorce.doc_id != item.doc_id or amorce.block.kind == "heading":
            return False
        if amorce.node_id == item.node_id:
            directs = self._nodes[item.node_id][1]
            return directs.index(amorce_id) + 1 == directs.index(item_id)
        return self.amorce_de_lenumeration(item_id) == amorce_id

    def amorce_de_lenumeration(self, block_id: str) -> str | None:
        """La phrase qui **ouvre** l'énumération dont ce bloc est l'item, ou `None`.

        Correctif du tour 5 (C9), et c'est le cas **symétrique** de `unite_de_renvoi`. Celle-ci
        traite déjà « un titre seul situe une fiche mais ne porte généralement pas la règle à
        citer » : un `heading` emporte son premier corps non-titre. Le miroir manquait — une feuille
        qui **est** l'item d'une énumération n'emportait pas la phrase qui l'introduit, alors qu'elle
        n'est pas plus citable seule qu'un titre.

        Mesuré sur le contrat servi : `a3.1.1.1.5` ne porte que `p34:11`, « 3.1.1.1.5 Les fumées et
        les suies ; ». Son nœud parent `a3.1.1.1` porte `p34:6`, « La Compagnie assure les biens
        désignés, contre les périls suivants : ». Servi seul, l'item a produit une affirmation
        rejetée `non_soutenue` — le rédacteur avait emprunté à une clause absente le membre qui
        manquait ; servi **avec** son amorce, il a produit quatre affirmations retenues sur quatre.

        Deux bornes, les mêmes que `unite_de_renvoi` : **un seul niveau**, jamais récursif, et
        seulement quand le nœud n'a **aucun autre bloc citable** que celui-là — un nœud qui porte
        ses voisins n'est pas un item, il est une section. L'amorce n'est jamais un titre : le code
        ne fabrique pas une unité à partir d'un titre, ici comme là-bas.
        """
        entry = self._by_block[block_id]
        _doc_id, directs = self._nodes[entry.node_id]
        if directs != [block_id]:
            return None
        parent_id = self._node_parents.get(entry.node_id)
        if parent_id is None:
            return None
        return next((candidate for candidate in reversed(self._nodes[parent_id][1])
                     if self._by_block[candidate].block.kind != "heading"), None)

    def _tete_denumeration(self, node_id: str) -> str | None:
        """Le bloc qui **ouvre** l'énumération portée par ce nœud, ou `None` si ce n'en est pas une.

        Une énumération, structurellement : un nœud dont le dernier bloc citable propre n'est pas un
        titre — c'est l'amorce, « La Compagnie assure les biens désignés, contre les périls
        suivants : » — et dont **tous** les enfants sont des feuilles à un seul bloc citable, les
        items. Exiger *tous* les enfants et non *un* est ce qui distingue une énumération d'une
        section qui mêle des sous-parties : deviner sur une section reviendrait à transmettre un
        article entier chaque fois qu'une de ses feuilles est demandée.
        """
        enfants = self._node_children.get(node_id) or []
        if not enfants:
            return None
        for enfant in enfants:
            if self._node_children.get(enfant) or len(self._nodes[enfant][1]) != 1:
                return None
        directs = self._nodes[node_id][1]
        amorce = next((b for b in reversed(directs)
                       if self._by_block[b].block.kind != "heading"), None)
        return amorce

    def enumeration_de(self, block_id: str) -> list[str] | None:
        """`[amorce, *items]` quand ce bloc appartient à une énumération, sinon `None`.

        Correctif du tour 6 (F1). **Les items d'une même énumération se lisent ensemble.** C9 avait
        déjà établi qu'un item ne se cite pas sans la phrase qui l'ouvre ; la réciproque manquait, et
        elle est de la même nature. Mesuré sur trois runs : le navigateur a ouvert « Étendue de la
        garantie » incendie (`a3.1.1.1`) et n'a reçu que son titre et son amorce — les **six périls**
        sont des nœuds enfants, donc hors de la fenêtre —, si bien que « même lorsqu'il n'y a pas eu
        embrasement, ni commencement d'incendie », qui est la réponse au cas, n'a jamais été
        transmise. Les périls d'une même garantie se qualifient les uns les autres : lus séparément,
        chacun ment par omission.

        Le bloc demandé peut être l'amorce (le navigateur ouvre la garantie) ou l'un des items (il
        ouvre un péril, ou une réservation en désigne un) : les deux rendent la même unité, dans
        l'ordre de lecture. Aucune borne de taille ici — c'est de la structure ; le budget est
        l'affaire de la lecture, bornée en tokens par `navigation_budget_tokens`.
        """
        entry = self._by_block[block_id]
        parent_id: str | None = None
        if self._tete_denumeration(entry.node_id) == block_id:
            parent_id = entry.node_id
        elif self.amorce_de_lenumeration(block_id) is not None:
            candidat = self._node_parents.get(entry.node_id)
            if candidat is not None and self._tete_denumeration(candidat) is not None:
                parent_id = candidat
        if parent_id is None:
            return None
        amorce = self._tete_denumeration(parent_id)
        if amorce is None:
            return None
        return [amorce, *(self._nodes[enfant][1][0]
                          for enfant in self._node_children[parent_id])]

    def part_des_blocs(self, mot: str, *, doc_id: str) -> float:
        """Quelle **part des blocs** du document porte ce mot **normalisé** : 0 s'il n'y est pas.

        L'argument est un token tel que `words(normalize(...))` les produit — c'est la seule forme
        sur laquelle l'index compte, et la convention Texte du dépôt veut que `normalize()` soit
        l'unique normalisation admise. Une chaîne accentuée ou capitalisée rendrait donc 0, comme
        n'importe quel mot absent : c'est à l'appelant de normaliser, une fois, chez lui.

        L'index compte déjà cette fréquence documentaire — c'est elle qui pondère les couvertures
        partielles (`_hit`, « chaque mot est pondéré par l'inverse du nombre de blocs qui le
        portent »). Elle n'était simplement lisible de nulle part. Un appelant qui construit une
        requête peut ainsi savoir si un mot **désigne une clause** ou **nomme le sujet du
        document** : « fumées » vit dans 1 bloc du contrat servi, « dommages » dans 124.

        Une part, et non un compte : un document deux fois plus long porte deux fois plus de blocs
        pour le même mot, et un seuil absolu aurait dit le contraire d'un document à l'autre.
        """
        if doc_id not in self.corpus.documents:
            raise KeyError(doc_id)
        blocs = len(self.corpus.documents[doc_id].blocks)
        if not blocs:
            return 0.0
        return self._block_frequencies.get(doc_id, {}).get(mot, 0) / blocs

    def chercher(self, termes: dict[str, list[str]] | Iterable[str], *, limit: int,
                 doc_id: str | None = None,
                 question: str | None = None,
                 kinds_prioritaires: Iterable[str] | None = None,
                 kinds_confirmes: Iterable[str] | None = None,
                 ) -> list[ScoredHit]:
        """Correspondance par couverture de mots entiers, puis kind et ordre de lecture.

        Le classement compte d'abord tous les canoniques dont au moins une forme est entièrement
        couverte, conformément à l'AC 2.7. Une forme composée pleine vaut donc un canonique plein,
        jamais davantage qu'un canonique simple plein.
        Les couvertures partielles départagent seulement les blocs qui ne satisfont aucun canonique
        pleinement. Dès qu'un bloc porte un plein, les fragments de ses autres groupes ne bonifient
        plus son rang ; la densité de la meilleure forme pleine préfère alors un titre ou une clause
        concise à un long paragraphe où les mêmes mots sont dispersés. Pour une forme partielle,
        chaque mot est pondé par l'inverse du nombre de blocs du document qui le portent ;
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

        `kinds_confirmes`, lorsqu'il est fourni, restreint au contraire les candidats aux kinds
        demandés dont le typage est confirmé par le corpus. La sélection précède le classement et
        `limit` ; son défaut `None` laisse donc tous les appels historiques strictement inchangés.

        **Ce classement propose, il ne réserve plus** (amendement AD-1 du 03/09/2026). Jusqu'au
        02/09, `groupes_prioritaires` préservait avant `limit` un candidat par sous-question, et
        `reservations_out` les rendait effectives : c'est l'attribution lexicale d'un bloc à une
        sous-question, et elle a été supprimée avec les variantes qui l'employaient. Aucun critère
        lexical ne sépare « la fumée » d'une exclusion de « les fumées et les suies » d'une
        garantie — ce qui les distingue, un nom au pluriel contre un participe passé, n'est pas une
        information que l'index, qui supprime les accents, porte. Le rang sert donc à **ordonner une
        proposition** que le modèle lit, jamais à décider ce qu'il verra.
        """
        if limit < 1:
            raise ValueError("limit doit être ≥ 1")
        if isinstance(termes, str):
            raise TypeError("termes : dict[str, list[str]] ou liste de termes attendus, pas une chaîne")
        if doc_id is not None and doc_id not in self.corpus.documents:
            raise KeyError(doc_id)
        if isinstance(kinds_confirmes, str):
            raise TypeError("kinds_confirmes : liste de kinds attendue, pas une chaîne")
        mapping = termes if isinstance(termes, dict) else {t: [] for t in termes}

        def groupes(mapping_: dict[str, list[str]]) -> list[list[frozenset[str]]]:
            # **Un groupe est son ensemble de formes, pas la clé qui l'a demandé** (correctif du
            # tour 5, C8). La déduplication ne portait que sur la forme du canonique, c'est-à-dire
            # sur le terme de la question. Trois termes synonymes que le dictionnaire ramène au
            # **même** groupe produisaient donc trois groupes identiques, et un bloc couvert par une
            # seule de leurs formes marquait `full_matches = 3` : la seule chose que ce score doit
            # dire — combien de canoniques distincts ce bloc satisfait entièrement — devenait un
            # compte de synonymes. Mesuré sur un dictionnaire simulé aux bornes du prompt contrat :
            # `p50:18`, une exclusion de responsabilité civile immeuble sans rapport avec le
            # sinistre, remontait rang 1 avec `full = 3` là où le même bloc vaut `full = 1`.
            #
            # La déduplication par canonique **reste**, en plus : deux clés qui se normalisent
            # identiquement désignaient déjà un seul groupe, et l'ajout n'en retire aucun. Sans
            # dictionnaire, chaque terme sort seul et les deux règles coïncident — `question_uid`
            # ne bouge pas, et c'est ce qu'un témoin de non-régression fixe.
            resultat: list[list[frozenset[str]]] = []
            seen_canon: set[str] = set()
            seen_forms: set[frozenset[frozenset[str]]] = set()
            for canon, variants in mapping_.items():
                forms = {frozenset(words(normalize(v))) for v in (canon, *variants)} - {frozenset()}
                canon_form = " ".join(words(normalize(canon)))
                if not forms or canon_form in seen_canon or frozenset(forms) in seen_forms:
                    continue
                seen_canon.add(canon_form)
                seen_forms.add(frozenset(forms))
                resultat.append(sorted(forms, key=lambda form: tuple(sorted(form))))
            return resultat

        groups = groupes(mapping)
        if not groups:
            return []
        # Les termes et leurs variantes ne servent qu'au rappel. La pertinence publique est mesurée
        # contre la question résolue entière : une reformulation de recherche ne devient jamais la
        # question à la place de celle réellement posée.
        question_form = frozenset(words(normalize(question or "")))
        # La question entière ajoute un axe au score sans effacer le classement de rappel : les
        # canoniques/variantes restent mesurés avec leur pondération documentaire, puis le premier
        # groupe porte la couverture propre à la question résolue.
        score_groups = [[question_form], *groups] if question_form else None
        canonical_question = [
            [" ".join(sorted(form)) for form in forms]
            for forms in (score_groups or groups)
        ]
        canonical_question.sort(key=lambda forms: tuple(forms))
        question_uid = stable_uid("question-v1", {
            "resolved_question": normalize(question) if question is not None else None,
            "groups": canonical_question,
        })
        prioritaires = frozenset(kinds_prioritaires or ())
        selection_confirmed = (frozenset(kinds_confirmes)
                               if kinds_confirmes is not None else None)

        def classer(groupes_: list[list[frozenset[str]]], *, question_uid_: str,
                    score_groups_: list[list[frozenset[str]]] | None = None) -> list[ScoredHit]:
            scored: list[ScoredHit] = []
            for e in self._entries:
                if doc_id is not None and e.doc_id != doc_id:
                    continue
                if not is_citable(e.block):
                    continue
                if (selection_confirmed is not None
                        and (e.block.kind not in selection_confirmed
                             or not e.block.kind_confirmed)):
                    continue
                candidate_match = any(
                    any(form <= e.tokens or e.tokens & form for form in forms)
                    for forms in groupes_
                )
                if not candidate_match:
                    continue
                public_hit = self._score_entry(
                    e, score_groups_ or groupes_, question_uid_, prioritaires,
                    whole_question=score_groups_ is not None,
                )
                scored.append(public_hit)
            scored.sort(key=lambda hit: (
                # Le score public précède les seuls tie-breaks canoniques auditables. Les formes de
                # rappel décident de l'éligibilité, jamais d'un ordre caché divergent du record rendu.
                *hit.score.sort_key,
                hit.document_uid, hit.clause_uid, hit.result_uid))
            return scored

        return classer(groups, question_uid_=question_uid,
                      score_groups_=score_groups)[:limit]

    def score_clause(self, question: str, block_id: str) -> ScoredHit:
        """Score une clause désignée sans transformer sa sélection en pertinence implicite."""
        entry = self._by_block[block_id]
        if not is_citable(entry.block):
            raise ValueError(f"{block_id}: clause non citable ou non scorée")
        form = frozenset(words(normalize(question)))
        groups = [[form]] if form else []
        question_uid = stable_uid("question-v1", {
            "resolved_question": normalize(question),
            "groups": [[" ".join(sorted(form))]] if form else [],
        })
        return self._score_entry(
            entry, groups, question_uid, frozenset(), whole_question=True,
        )

    def _score_entry(self, entry: _Entry, score_groups: list[list[frozenset[str]]],
                     question_uid: str, prioritaires: frozenset[str], *,
                     whole_question: bool = False) -> ScoredHit:
        """Construit le record question-clause, indépendamment du mécanisme de rappel."""
        pleins = 0
        precision_plein = Fraction()
        partiels = Fraction()
        question_coverage = Fraction()
        frequencies = self._block_frequencies[entry.doc_id]
        recall_groups = score_groups
        if whole_question and score_groups:
            question_forms = score_groups[0]
            if question_forms:
                question_coverage = max(
                    Fraction(len(entry.tokens & form), len(form))
                    for form in question_forms if form
                )
            recall_groups = score_groups[1:]
        for forms in recall_groups:
            formes_pleines = [form for form in forms if form and form <= entry.tokens]
            if formes_pleines:
                pleins += 1
                composees = [form for form in formes_pleines if len(form) > 1]
                if composees:
                    precision_plein = max(
                        precision_plein,
                        max(Fraction(len(form), len(entry.tokens)) for form in composees),
                    )
                continue
            if forms:
                partiels += max(self._hit(entry, form, frequencies) for form in forms)
        rappel = (partiels / len(recall_groups)
                  if recall_groups and pleins == 0 else Fraction())
        score = QuestionClauseScore(
            question_uid=question_uid, clause_uid=entry.block.block_id,
            scorer_uid="lexical-question-clause", scorer_version="4-question-and-recall",
            full_matches=pleins, partial_numerator=rappel.numerator,
            partial_denominator=rappel.denominator,
            precision_numerator=precision_plein.numerator,
            precision_denominator=precision_plein.denominator,
            question_numerator=question_coverage.numerator,
            question_denominator=question_coverage.denominator,
            # Un label modèle ne devient décisionnel qu'après confirmation terminale T2/T3.
            kind_priority=(0 if entry.block.kind_confirmed
                           and entry.block.kind in prioritaires else 1),
        )
        excerpt = entry.block.text[:self.excerpt_max_chars]
        payload = {
            "question_uid": score.question_uid, "clause_uid": entry.block.block_id,
            "scorer_uid": score.scorer_uid, "scorer_version": score.scorer_version,
            "score": score.model_dump(mode="json"), "document_uid": entry.doc_id,
            "node_uid": entry.node_id, "title": self._node_titles[entry.node_id],
            "excerpt": excerpt,
        }
        return ScoredHit(
            result_uid=stable_uid("result-v1", payload), document_uid=entry.doc_id,
            clause_uid=entry.block.block_id, node_uid=entry.node_id,
            title=self._node_titles[entry.node_id], excerpt=excerpt, score=score,
        )

    @staticmethod
    def _hit(e: _Entry, form: frozenset[str], frequencies: dict[str, int]) -> Fraction:
        """Couverture partielle pondérée ; zéro seulement quand aucun mot n'existe."""
        presents = e.tokens & form
        if not presents:
            return Fraction()
        # Un mot absent du document ne peut jamais être trouvé : il ne pèse pas dans la normalisation
        # documentaire. Le plein reste décidé séparément par `form <= e.tokens`, donc ce score de
        # rappel ne transforme jamais une forme incomplète en correspondance pleine.
        poids_forme = sum((Fraction(1, frequencies[token]) for token in form if token in frequencies),
                          Fraction())
        poids_le_plus_discriminant = max(Fraction(1, frequencies[token]) for token in presents)
        return poids_le_plus_discriminant / poids_forme

    def definitions(self, termes: dict[str, list[str]] | Iterable[str], *, doc_id: str | None = None,
                    blocs_ouverts: Iterable[str] | None = None) -> list[tuple[str, str]]:
        """Blocs `kind="definition"` qui définissent un terme cherché, résolus dans la portée (AD-1, AD-2).

        Candidats : `defines` normalisé qui contient un terme (ou une variante si dict), en mots
        entiers (« jardin » trouve « mobilier de jardin »). L'inverse n'est pas une preuve : une
        question sur « assurance habitation » ne demande pas automatiquement la définition plus
        générique « habitation » — **et**, si
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
            de_la_question = any(f" {f} " in f" {defined} " for f in forms)
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
                communes = [e for e in entries if not self._portee_racines(e)]
                eligibles = communes or entries
                deroges = {e.block.overrides for e in eligibles if e.block.overrides}
                eligibles = [e for e in eligibles if e.block.block_id not in deroges]
                choisi = min(eligibles, key=lambda e: e.rank)
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
                    deroges = {e.block.overrides for e in communes if e.block.overrides}
                    communes = [e for e in communes if e.block.block_id not in deroges]
                    choisi = min(communes, key=lambda e: e.rank)
                retenus.setdefault(choisi.block.block_id, choisi)
        return [(e.block.block_id, e.node_id) for e in sorted(retenus.values(), key=lambda e: e.rank)]

    def _portee_racines(self, e: _Entry) -> list[str]:
        b = e.block
        if b.scope_node_ids:
            return list(b.scope_node_ids)
        if not b.scope_node_id:
            return []
        return [b.scope_node_id]

    def _portee_profondeur(self, e: _Entry) -> int:
        """Profondeur de la portée d'une définition ; -1 = aucune portée (définition commune)."""
        racines = self._portee_racines(e)
        return max((self._levels.get(r, 0) for r in racines), default=-1)

    def _portee_couvre(self, e: _Entry, noeuds: set[str]) -> bool:
        """La portée de la définition couvre-t-elle le nœud de l'un des blocs ouverts ?"""
        if not noeuds or not self._portee_racines(e):
            return False
        return bool(self.corpus.documents[e.doc_id].scope_nodes(e.block.block_id) & noeuds)
