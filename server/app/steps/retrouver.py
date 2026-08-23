"""AD-1 — *retrouver*, variante `deterministe` (J+1) : code pur, zéro appel modèle.

`chercher(terms + scope.themes, limit=search_limit)`, puis ouverture groupée des nœuds candidats par
score (≤ `max_opens` nœuds, fenêtre `node_window` contenant le meilleur hit du nœud), puis suivi
**automatique** d'un niveau des renvois (`Block.refs`) des blocs ouverts et des `definitions()` des
termes — hors quota `max_opens`. `truncated=True` si une fenêtre reste coupée (pas de pagination en
déterministe) ou si des nœuds candidats dépassent `max_opens` ; les hits non ouverts vont en
`discarded_block_ids`. Les blocs sont relus depuis le corpus (objets `Document.block`), jamais
modifiés ; l'étape n'affirme aucune absence du corpus (AD-1) et ne voit que `ParsedQuestion` —
jamais l'historique.

`StepTrace(tier=None, calls=[])` dit la vérité de la variante exécutée : l'affectation
`retrouver → reason` d'AD-9 vaut pour les variantes qui appellent le modèle (2.6, baselines).
`RetrievalBudget.max_blocks`, s'il est donné, borne la liste finale (`truncated=True`) en coupant la
queue des fenêtres — jamais les renvois ni les définitions, que le suivi hors quota rend justement
indispensables ; les blocs coupés rejoignent `discarded_block_ids`. `max_tokens` n'est pas appliqué
ici (pas de tokenizer en code pur) ; `max_llm_turns` est sans objet.
"""

from __future__ import annotations

import time

from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.domain import RetrievalBudget, RetrievalResult
from server.app.domain.question import ParsedQuestion
from server.app.domain.trace import StepTrace


def retrouver_deterministe(parsed: ParsedQuestion, *, corpus: Corpus, index: Index,
                           budget: RetrievalBudget, doc_id: str | None = None) -> tuple[RetrievalResult, StepTrace]:
    t0 = time.monotonic()
    terms: list[str] = []
    for t in (*parsed.terms, *parsed.scope.themes):
        if t and t not in terms:
            terms.append(t)

    blocs = []
    seen: set[str] = set()

    def add(block_id: str) -> None:
        if block_id not in seen:
            seen.add(block_id)
            blocs.append(corpus.documents[index.doc_of(block_id)].block(block_id))

    if doc_id is not None and doc_id not in corpus.documents:
        # `chercher` lève déjà sur un doc_id inconnu, mais il n'est pas appelé quand aucun terme n'a
        # été extrait : sans ce contrôle, une faute de frappe rendrait un résultat vide silencieux.
        raise KeyError(doc_id)

    truncated = False
    hits = index.chercher(terms, limit=budget.search_limit, doc_id=doc_id) if terms else []

    # Nœuds candidats par score : ordre de première apparition dans les hits (déjà triés par score,
    # puis ordre de lecture) ; la fenêtre de chaque nœud contient son meilleur hit (AD-1).
    nodes: list[str] = []
    best_hit: dict[str, str] = {}
    for block_id, node_id in hits:
        if node_id not in best_hit:
            best_hit[node_id] = block_id
            nodes.append(node_id)
    if len(nodes) > budget.max_opens:
        truncated = True  # des nœuds candidats avaient des hits au-delà du quota
    for node_id in nodes[: budget.max_opens]:
        window = index.ouvrir_noeud(node_id, focus_block_id=best_hit[node_id], node_window=budget.node_window)
        if window.truncated:
            truncated = True  # pas de pagination en déterministe : la fenêtre reste coupée
        for b in window.blocks:
            add(b.block_id)

    fenetres = len(blocs)  # tout ce qui suit est hors quota `max_opens`
    # Un niveau de renvois des blocs ouverts, hors quota `max_opens` (un seul niveau : les cibles
    # ne sont pas suivies à leur tour — Deferred du spine « renvois en chaîne »).
    for b in list(blocs):
        for target in b.refs:
            add(target)
    # Définitions des termes, hors quota.
    if terms:
        for block_id, _node_id in index.definitions(terms, doc_id=doc_id):
            add(block_id)

    ecartes: list[str] = []
    if budget.max_blocks is not None and len(blocs) > budget.max_blocks:
        # Borne de coût (`retrieval_max_blocks`) : elle coupe la **queue des fenêtres**, jamais
        # d'abord les renvois et les définitions. Ces derniers sont suivis hors quota `max_opens`
        # précisément parce qu'AD-1 les veut toujours (une clause qui renvoie à une autre est
        # inutilisable sans sa cible) ; les couper en premier — ils sont ajoutés en dernier —
        # annulait le suivi des renvois dès que les fenêtres remplissaient la borne, c'est-à-dire
        # sur presque toute question réelle (49 blocs de fenêtres pour une borne de 30, revue 1.4).
        truncated = True
        hors_quota = blocs[fenetres:]
        garde = max(budget.max_blocks - len(hors_quota), 0)
        retenus = blocs[:garde] + hors_quota[: budget.max_blocks]
        gardes = {b.block_id for b in retenus}
        ecartes = [b.block_id for b in blocs if b.block_id not in gardes]
        blocs[:] = retenus
        seen = gardes

    opened = [b.block_id for b in blocs]
    hit_ids = [b for b, _ in hits]
    # Ce qui a été trouvé mais n'est pas transmis à *rédiger* : les hits jamais ouverts, puis les
    # blocs ouverts que la borne de coût a coupés (sans quoi la coupe ne laisserait aucune trace).
    discarded = [b for b in hit_ids if b not in seen] + [b for b in ecartes if b not in hit_ids]
    result = RetrievalResult(blocs=blocs, opened_block_ids=opened, discarded_block_ids=discarded,
                             truncated=truncated)
    step = StepTrace(name="retrouver", tier=None, ms=int((time.monotonic() - t0) * 1000),
                     opened_block_ids=list(opened), discarded_block_ids=list(discarded))
    return result, step
