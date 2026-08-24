"""AD-3 / AD-11 — `sources[]` et `fiches[]` : ce que le front affiche sous chaque phrase.

**Ce que `sources[]` contient (amendé par la revue Codex 1.6, B2).** `sources[]` est la liste des
citations **de la réponse affichée** : les quotes des claims d'`Answer.claims`, c'est-à-dire celles
qu'un segment `factuel` affiché cite réellement. Rien d'autre n'y entre. Une claim écartée n'a pas
d'endroit dans cette liste :

- `non_retrouvee` / `ambigue` : aucune occurrence n'a été prouvée, ses quotes sont restées les
  **chaînes du modèle** — les publier montrerait du texte non vérifié, l'« écho du modèle » qu'AD-3
  empêche ;
- `non_pertinente` : le passage existe, mais le jugement de *vérifier* dit qu'il ne répond pas à la
  question ; l'afficher comme source de la réponse lui prêterait un rôle qu'il n'a pas ;
- `non_citee` : AD-4 la définit précisément comme la claim retrouvée et pertinente qu'« aucun segment
  factuel affiché ne cite » et qui « n'a nulle part où paraître ». `sources[]` lui donnerait
  justement un endroit où paraître.

Le premier jet de la story faisait entrer les rejetées « à offsets conservés » (`non_pertinente`,
`non_citee`) en s'appuyant sur le champ `status` pour les distinguer. Mais `SourceItem` ne porte
**pas** de `claim_id` : le front reçoit une liste plate sous la réponse, et rien ne relie une entrée
au segment qui la cite. Une citation écartée s'y lisait donc comme une source de la réponse — et
`fiches[]`, qui en dérive, aurait ouvert une fiche dont la réponse ne dit rien.

Elles ne sont pas perdues pour autant : `answer.rejected_claims` les publie intégralement, avec
`rejection_kind`, `status` et — quand elles ont été retrouvées — leurs offsets, ce qu'AD-3 demande
(« conservées […] affichables par le front »). Le champ `status` de `SourceItem` reste au contrat
d'AD-11 ; il vaut `verifiee` pour toute entrée du pipeline guide.

**La citation est relue du corpus, ici aussi (revue Codex 1.6, B1).** AD-3 : « le texte affiché comme
source est toujours relu depuis `corpus` ». *vérifier* construit déjà `VerifiedQuote.quote` par
`Block.text[text_start:text_end]`, mais l'invariant se lit alors depuis un autre module : rien, dans
la couche qui **affiche**, n'empêcherait un jour de publier une chaîne fabriquée ailleurs. La
citation rendue est donc ré-extraite du bloc du corpus, aux offsets prouvés, à l'endroit même où elle
part sur le réseau.

**Une citation qui ne se relit pas est un échec terminal (revue Codex 1.6, B1, tour 2).** Le premier
correctif ré-extrayait la citation du bloc et, quand les offsets ne retombaient pas dessus, n'affichait
rien : la réponse partait quand même en 200, avec son segment `factuel` et `sources[]` vide. C'est
exactement les deux choses qu'AD-3 empêche (« phrase sans source », « jamais de dégradé silencieux »)
et ce qu'AD-16 nomme un dégradé en silence. Toute incohérence — bloc absent de l'index, document
absent du corpus, offsets hors du bloc — lève donc une `PipelineError` qui ressort dans l'enveloppe
d'AD-16, avec la `Trace` de la requête.

Le code est `internal` (500), pas `corpus_unavailable` (503) : le corpus est chargé **une seule fois
au démarrage** (`api/etat.py`) et ne bouge plus de la vie du processus. Un bloc cité qui n'y est pas
ne peut donc pas être une indisponibilité passagère que réessayer réparerait — c'est un invariant du
code qui a cédé, et `internal` est le seul code d'AD-16 qui le dise sans mentir. 503 ferait de surcroît
proposer le mode local par le front (AD-11) pour une panne que le mode local ne contourne pas.

**`fiche_id`.** Le nœud parent d'un bloc du guide est `lux-guide:f{fiche_id}` (`ingest/kb_to_blocks`) ;
`fiche_id` est donc ce nœud privé de son préfixe, et c'est exactement l'identifiant que `ui.js`
attend (`montrerFiche(id)`). Un bloc de FAQ a pour parent `lux-guide:q{i}` : `fiche_id` vaut `null`
et `titre` est la question. La règle est générique, elle vaut pour tout document à venir :
`fiche_id` n'est rendu que si le nœud parent commence par `{doc_id}:f`.
"""

from __future__ import annotations

import logging
from typing import Any

from server.app.api.schemas import SourceItem
from server.app.domain.answer import Answer, VerifiedQuote
from server.app.domain.document import Document, Node
from server.app.domain.errors import ErrorCode, PipelineError

logger = logging.getLogger("foyer.api")

STATUT_VERIFIEE = "verifiee"


def _incoherence(block_id: str, motif: str) -> PipelineError:
    """L'échec terminal d'AD-16 pour une citation que le corpus servi ne confirme pas.

    Le diagnostic — `block_id` et motif — part dans le **log serveur**, et n'atteint jamais le client
    (revue Codex 1.6, NB1) : `gestionnaire_pipeline` publie `MESSAGE_INTERNE` pour tout `internal`.
    C'est nécessaire ici plus qu'ailleurs : ce `block_id` est précisément celui qu'aucun index ne
    connaît, donc rien ne prouve qu'il vienne de notre ingestion plutôt que du modèle — le renvoyer
    dans l'enveloppe réfléchirait une chaîne non fiable à l'appelant (AD-15). Il reste porté par
    l'exception pour le log, jamais pour la réponse.
    """
    logger.error("citation incohérente sur %s : %s — la réponse n'est pas servie", block_id, motif)
    return PipelineError(ErrorCode.internal, f"citation incohérente sur le bloc {block_id} : {motif}")


def _noeud(index: Any, corpus: Any, block_id: str) -> tuple[str, Document, Node] | None:
    """(doc_id, document, nœud parent) du bloc, ou None s'il n'est pas dans l'index."""
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
            return doc_id, document, n
    return None


def _relire(document: Document, quote: VerifiedQuote) -> str:
    """AD-3, à l'endroit de l'affichage : le passage **brut** du corpus, aux offsets prouvés.

    Des offsets qui ne retombent pas sur le bloc ne peuvent pas venir du pipeline (`steps/verifier`
    les obtient de la table de correspondance de `normalize()`). Quand ils n'y retombent pas, la
    `VerifiedQuote` a été fabriquée sans ce corpus : il n'y a rien de vrai à afficher, et taire la
    source tout en servant la phrase qui la cite serait la « phrase sans source » d'AD-3. On échoue.
    """
    try:
        bloc = document.block(quote.block_id)
    except KeyError:
        raise _incoherence(quote.block_id, "le bloc cité est absent du document servi") from None
    if not 0 <= quote.text_start < quote.text_end <= len(bloc.text):
        raise _incoherence(quote.block_id, "les offsets de la citation sortent du bloc")
    return bloc.text[quote.text_start:quote.text_end]


def _source_item(quote: VerifiedQuote, statut: str, *, index: Any, corpus: Any) -> SourceItem:
    trouve = _noeud(index, corpus, quote.block_id)
    if trouve is None:
        # Une `VerifiedQuote` vient d'un bloc du corpus : ne pas le retrouver signifie que la réponse
        # a été vérifiée contre un autre corpus que celui qu'on sert. Échec terminal (AD-3/AD-16).
        raise _incoherence(quote.block_id, "le bloc cité est introuvable dans le corpus servi")
    doc_id, document, node = trouve
    texte = _relire(document, quote)
    prefixe = f"{doc_id}:f"
    fiche_id = node.node_id[len(prefixe):] if node.node_id.startswith(prefixe) else None
    lien = node.sources[0].url if node.sources else None
    return SourceItem(block_id=quote.block_id, fiche_id=fiche_id, titre=node.title, url=lien,
                      quote=texte, status=statut)


def sources_de(answer: Answer, index: Any, corpus: Any) -> list[SourceItem]:
    """Les citations de la réponse **affichée**, relues du corpus, dans l'ordre de citation (AD-3/AD-11)."""
    return [_source_item(quote, STATUT_VERIFIEE, index=index, corpus=corpus)
            for claim in answer.claims  # AD-4 : les seules claims retrouvées, pertinentes et citées
            for quote in claim.quotes]


def fiches_de(sources: list[SourceItem]) -> list[str]:
    """Ids de fiches distincts, dans l'ordre de citation (`fiches[]` d'AD-11, lu par `ui.js`)."""
    vues: list[str] = []
    for s in sources:
        if s.fiche_id and s.fiche_id not in vues:
            vues.append(s.fiche_id)
    return vues
