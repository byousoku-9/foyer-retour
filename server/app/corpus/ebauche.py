"""Les deux projections d'une ébauche sur le corpus, partagées par *rédiger* et *naviguer*.

Elles ne jugent rien et n'écrivent aucun texte : l'une rend contigu ce que le modèle a cité en deux
morceaux du même bloc, l'autre fait des claims atomiques du sinistre les segments effectivement
soumis à *vérifier*. Elles vivaient dans `steps/rediger.py`, seule étape qui rédigeait ; l'amendement
AD-1 du 03/09/2026 fait rédiger *naviguer* dans la conversation de navigation, et une étape n'importe
jamais une autre étape (table des couches). Elles vivent donc ici, une fois, à la couche qui porte
déjà le texte des blocs — et les deux bornes de `config.py` leur arrivent en paramètres, la couche
`corpus` ne connaissant pas les réglages.
"""

from __future__ import annotations

from server.app.corpus.index import Index
from server.app.corpus.text import normalize
from server.app.domain.answer import AnswerDraft, AnswerSegment, Claim, Quote


def fusionner_quotes_du_meme_bloc(draft: AnswerDraft, *, index: Index,
                                  doc_id: str) -> tuple[AnswerDraft, int]:
    """Deux extraits d'un même bloc dans une même claim deviennent **un** passage qui les couvre.

    Correctif du tour 2 (rapport rédiger §1, rapport citations A3). La règle « au plus une quote par
    bloc » vivait dans le schéma du domaine : sa violation était donc **terminale**. Le retry unique
    d'AD-16 était consommé, le modèle rejouait la même faute — c'est documenté comme observé en
    live —, et l'utilisateur recevait un 503 sur une question nominale. Les deux issues que le
    prompt proposait étaient fermées toutes les deux : le passage englobant dépassait la borne
    annoncée, et la place de claims était déjà prise.

    La fusion est exactement ce que fait déjà `verifier._controler_quote` pour retraduire une
    occurrence : les extraits sont localisés dans le texte **normalisé** du bloc — la seule forme sur
    laquelle une inclusion se prouve —, et le passage retenu va de `min(start)` à `max(end)`. Il est
    rendu depuis ce même texte normalisé : *vérifier* le renormalisera pour le retrouver, puis
    republiera le texte brut du corpus, comme pour toute autre citation (AD-3 — le texte affiché
    est toujours relu depuis le corpus).

    Un extrait qu'on ne retrouve pas dans le bloc n'est **pas** fusionné : ce n'est pas ici qu'on
    juge une citation, et *vérifier* doit pouvoir la rejeter avec son motif actionnable.
    """
    fusions = 0
    claims: list[Claim] = []
    for claim in draft.claims:
        par_bloc: dict[str, list[Quote]] = {}
        for quote in claim.quotes:
            par_bloc.setdefault(quote.block_id, []).append(quote)
        if all(len(groupe) == 1 for groupe in par_bloc.values()):
            claims.append(claim)
            continue
        quotes: list[Quote] = []
        for block_id, groupe in par_bloc.items():
            if len(groupe) == 1:
                quotes.append(groupe[0])
                continue
            try:
                if index.doc_of(block_id) != doc_id:
                    raise KeyError(block_id)
                texte = index.corpus.documents[doc_id].block(block_id).text_norm
            except KeyError:
                quotes.extend(groupe)
                continue
            bornes = [(texte.find(normalize(q.quote)), len(normalize(q.quote))) for q in groupe]
            if any(debut < 0 for debut, _ in bornes):
                quotes.extend(groupe)  # au moins un extrait est introuvable : *vérifier* tranchera
                continue
            debut = min(debut for debut, _ in bornes)
            fin = max(debut + longueur for debut, longueur in bornes)
            quotes.append(Quote(block_id=block_id, quote=texte[debut:fin]))
            fusions += 1
        claims.append(claim.model_copy(update={"quotes": quotes}))
    if not fusions:
        return draft, 0
    return draft.model_copy(update={"claims": claims}), fusions


def rattacher_claims_sinistre(draft: AnswerDraft, *, max_claims: int,
                              max_segments: int) -> tuple[AnswerDraft, int]:
    """Fait des claims atomiques le texte factuel effectivement soumis à *vérifier*.

    Campagne réelle 2.7 : le rédacteur savait citer l'exclusion animale de `p35:2`, mais pouvait
    créer une claim distincte sans la rattacher à aucun segment. La citation et sa pertinence
    passaient alors tous les contrôles, avant que la claim ne soit justement rejetée `non_citee`.

    En sinistre, `Claim.text` est déjà l'affirmation atomique (« une seule clause par affirmation »)
    que *vérifier* confronte aux passages. La projection reconstruit donc exactement un segment
    factuel par claim : ordre de la première référence dans le brouillon, puis claims orphelines dans
    leur ordre. Les transitions et limites ne gardent aucun `claim_id` et n'occupent que les places
    restantes sous `draft_max_segments`. L'invariant de configuration
    `draft_max_claims <= draft_max_segments` garantit ainsi qu'aucune claim autorisée n'est perdue.
    Le guide conserve son brouillon à l'octet près, comme sa variante 2.6.
    """
    par_id = {claim.claim_id: claim for claim in draft.claims}
    ordre: list[str] = []
    vus: set[str] = set()
    for segment in draft.segments:
        if segment.kind != "factuel":
            continue
        for cid in segment.claim_ids:
            if cid in par_id and cid not in vus:
                ordre.append(cid)
                vus.add(cid)
    for claim in draft.claims:
        if claim.claim_id not in vus:
            ordre.append(claim.claim_id)
            vus.add(claim.claim_id)

    # Revue Codex 4.2a (B1) : la borne annoncée au prompt est appliquée **mécaniquement** à la
    # sortie du modèle — la fusion de relance et ses invariants de conservation reposent sur
    # `len(claims) <= draft_max_claims`. L'appelant trace l'écart (`claims_hors_borne_ecartees`) ;
    # rien n'est tu, et rien de vérifié n'est perdu : ces claims n'ont jamais été soumises au
    # contrôle.
    hors_borne = ordre[max_claims:]
    if hors_borne:
        ordre = ordre[:max_claims]

    if len(ordre) > max_segments:
        raise ValueError("plus de claims que de segments autorisés : la configuration doit garantir "
                         "draft_max_claims <= draft_max_segments")
    factuels = [AnswerSegment(text=par_id[cid].text.strip(), kind="factuel", claim_ids=[cid])
                for cid in ordre]
    place = max_segments - len(factuels)
    # Recheck Codex 4.2a (B2, tour 2) : les segments non factuels sont **normalisés une seule
    # fois, ici, avant la première `Verification`** — deux limites byte-identiques ne disent pas
    # deux réserves. `nb_manques` devient ainsi une métrique stable des deux côtés de la dominance
    # (première vérification, fusion de relance, seconde vérification passent toutes par cette
    # même projection) : aucune déduplication ultérieure ne peut plus l'abaisser artificiellement.
    vus_non_factuels: set[tuple[str, str]] = set()
    non_factuels: list[AnswerSegment] = []
    for segment in draft.segments:
        if segment.kind == "factuel":
            continue
        cle = (segment.kind, segment.text.strip())
        if cle in vus_non_factuels:
            continue
        vus_non_factuels.add(cle)
        non_factuels.append(AnswerSegment(text=segment.text, kind=segment.kind, claim_ids=[]))
    non_factuels = non_factuels[:place]
    segments = [*factuels, *non_factuels]
    claims = ([claim for claim in draft.claims if claim.claim_id not in set(hors_borne)]
              if hors_borne else draft.claims)
    changements = sum(1 for avant, apres in zip(draft.segments, segments, strict=False)
                       if avant != apres) + abs(len(draft.segments) - len(segments)) + len(hors_borne)
    if not changements:
        return draft, 0
    return draft.model_copy(update={"segments": segments, "claims": claims}), changements
