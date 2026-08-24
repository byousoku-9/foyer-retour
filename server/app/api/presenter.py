"""AD-3 / AD-11 — `sources[]` et `fiches[]` : ce que le front affiche sous chaque phrase.

**Ce que `sources[]` contient, et pourquoi pas tout.** AD-3 : « le texte affiché comme source est
toujours relu depuis `corpus` ». Une claim rejetée pour `non_retrouvee` ou `ambigue` n'a pas
d'occurrence prouvée : ses quotes sont restées les **chaînes du modèle**, sans offsets, et les
publier dans la liste que le front affiche montrerait du texte non vérifié — exactement l'« écho du
modèle » qu'AD-3 empêche. `sources[]` ne porte donc que des `VerifiedQuote`, c'est-à-dire :

- les claims d'`Answer.claims` (`retrouvee ∧ pertinente`) → `status="verifiee"` ;
- les claims d'`Answer.rejected_claims` qui ont **gardé leurs offsets** — celles écartées après avoir
  été retrouvées (`non_pertinente`, `non_citee`) → `status` = leur `rejection_kind`, pour que le
  front puisse dire ce qu'il montre.

Le reste n'est pas perdu : il reste lisible dans `answer.rejected_claims`, où sa nature est explicite
et où le front sait qu'il regarde une citation non retrouvée.

**`fiche_id`.** Le nœud parent d'un bloc du guide est `lux-guide:f{fiche_id}` (`ingest/kb_to_blocks`) ;
`fiche_id` est donc ce nœud privé de son préfixe, et c'est exactement l'identifiant que `ui.js`
attend (`montrerFiche(id)`). Un bloc de FAQ a pour parent `lux-guide:q{i}` : `fiche_id` vaut `null`
et `titre` est la question. La règle est générique, elle vaut pour tout document à venir :
`fiche_id` n'est rendu que si le nœud parent commence par `{doc_id}:f`.
"""

from __future__ import annotations

from typing import Any

from server.app.api.schemas import SourceItem
from server.app.domain.answer import Answer, VerifiedClaim, VerifiedQuote
from server.app.domain.document import Node

STATUT_VERIFIEE = "verifiee"


def _noeud(index: Any, corpus: Any, block_id: str) -> tuple[str, Node] | None:
    """(doc_id, nœud parent) du bloc, ou None s'il n'est pas dans l'index."""
    try:
        doc_id = index.doc_of(block_id)
        node_id = index.parent_node(block_id)
    except KeyError:
        return None
    document = corpus.documents.get(doc_id)
    if document is None:
        return None
    for n in document.nodes:
        if n.node_id == node_id:
            return doc_id, n
    return None


def _source_item(quote: VerifiedQuote, statut: str, *, index: Any, corpus: Any) -> SourceItem | None:
    trouve = _noeud(index, corpus, quote.block_id)
    if trouve is None:
        # Une `VerifiedQuote` vient d'un bloc du corpus : ne pas le retrouver signifie que le corpus a
        # changé sous les pieds de la requête. On n'affiche pas de source plutôt que d'en inventer une.
        return None
    doc_id, node = trouve
    prefixe = f"{doc_id}:f"
    fiche_id = node.node_id[len(prefixe):] if node.node_id.startswith(prefixe) else None
    lien = node.sources[0].url if node.sources else None
    return SourceItem(block_id=quote.block_id, fiche_id=fiche_id, titre=node.title, url=lien,
                      quote=quote.quote, status=statut)


def sources_de(answer: Answer, index: Any, corpus: Any) -> list[SourceItem]:
    """Une entrée par citation **relue du corpus**, dans l'ordre de citation (AD-3/AD-11)."""
    sources: list[SourceItem] = []
    couples: list[tuple[VerifiedClaim, str]] = [(c, STATUT_VERIFIEE) for c in answer.claims]
    couples += [(c, c.rejection_kind) for c in answer.rejected_claims]
    for claim, statut in couples:
        for quote in claim.quotes:
            if not isinstance(quote, VerifiedQuote):
                continue  # citation jamais retrouvée : c'est la chaîne du modèle, elle n'est pas affichée
            item = _source_item(quote, statut, index=index, corpus=corpus)
            if item is not None:
                sources.append(item)
    return sources


def fiches_de(sources: list[SourceItem]) -> list[str]:
    """Ids de fiches distincts, dans l'ordre de citation (`fiches[]` d'AD-11, lu par `ui.js`)."""
    vues: list[str] = []
    for s in sources:
        if s.fiche_id and s.fiche_id not in vues:
            vues.append(s.fiche_id)
    return vues
