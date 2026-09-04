"""Les projections d'une ébauche sur le corpus, partagées par *rédiger* et *naviguer*.

Elles ne jugent rien et n'écrivent aucun texte : la première rend contigu ce que le modèle a cité en
deux morceaux du même bloc, la deuxième joint à l'item d'une énumération la phrase qui l'ouvre, la
troisième fait des claims atomiques du sinistre les segments effectivement
soumis à *vérifier*. Elles vivaient dans `steps/rediger.py`, seule étape qui rédigeait ; l'amendement
AD-1 du 03/09/2026 fait rédiger *naviguer* dans la conversation de navigation, et une étape n'importe
jamais une autre étape (table des couches). Elles vivent donc ici, une fois, à la couche qui porte
déjà le texte des blocs — et les deux bornes de `config.py` leur arrivent en paramètres, la couche
`corpus` ne connaissant pas les réglages.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from server.app.corpus.index import Index
from server.app.corpus.text import normalize
from server.app.domain.answer import AnswerDraft, AnswerSegment, Claim, Quote
from server.app.domain.verdict import KINDS_DECISIONNELS


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


def joindre_amorces_denumeration(draft: AnswerDraft, *, index: Index, doc_id: str,
                                 blocs_servis: Iterable[str]) -> tuple[AnswerDraft, int]:
    """L'item d'une énumération cité seul reçoit **la phrase qui l'ouvre**, mot pour mot.

    Mesuré sur A16 (`a16-final1/a16-r1.json`) : « Le contrat garantit les biens désignés contre le
    péril des fumées et des suies », citant le seul `p34:11` (« Les fumées et les suies ; »), a été
    rejetée `non_soutenue` — légitimement, puisque « garantit les biens désignés » vient de `p34:6`,
    « La Compagnie assure les biens désignés, contre les périls suivants : », qui n'était pas cité.
    Le rédacteur n'avait rien inventé ; il avait cité la moitié d'une phrase que le corpus coupe en
    deux blocs.

    Le prompt le demande déjà (« tu peux joindre à la clause le passage qui l'éclaire […] la phrase
    d'amorce de l'énumération : c'est son contexte, cité **dans la même claim** »), et c'est
    précisément une consigne que le code n'a pas à espérer : `Index.amorce_de_lenumeration` **sait**
    qu'un bloc est un item d'énumération — c'est l'unité de lecture que *retrouver* sert déjà. La
    jonction est donc structurelle, avant toute vérification.

    Quatre bornes, et elles disent ce que cette projection n'est pas :

    - **rien n'est écrit.** La citation ajoutée est le texte normalisé du bloc d'amorce, tel que le
      corpus le porte ; *vérifier* le renormalisera pour le retrouver et republiera le texte brut,
      comme pour toute autre citation. Une sous-chaîne exacte par construction, jamais une
      reformulation ;
    - **la claim reste à une clause.** L'amorce est le **contexte** de l'item, pas une seconde
      clause : elle rejoint la même claim, et n'en crée jamais une nouvelle. C'est exactement la
      distinction que le prompt trace, appliquée par le code ;
    - **un seul niveau.** Seules les citations rendues par le modèle sont examinées, jamais les
      amorces qu'on vient d'ajouter : la jonction ne se propage pas, comme
      `amorce_de_lenumeration` elle-même ne remonte qu'un cran ;
    - **l'amorce doit avoir été servie.** `blocs_servis` est l'univers des blocs transmis au
      contrôle (`RetrievalResult.blocs` — celui-là même que `verifier._controler_quote` nomme
      `fournis`). Une amorce hors de cet univers existe dans le corpus mais n'a été mise sous les
      yeux de personne : la joindre rattachait la claim à un bloc « qui n'a pas été fourni dans ce
      message », et *vérifier* rejetait alors la claim **entière** en `non_retrouvee` — la
      projection censée sauver une citation la détruisait. Mesuré le 03/09/2026 sur le témoin de
      préflight du pipeline sinistre : l'item ouvert et son amorce vivent dans deux nœuds
      distincts, la claim tombait, et la chaîne payait une relance qu'aucun défaut ne justifiait.
      Hors de l'univers servi, l'item reste donc cité seul — exactement l'état d'avant cette
      projection, jamais moins.

    Un bloc d'un autre document, inconnu de l'index ou sans amorce est laissé tel quel : ce n'est
    pas ici qu'on juge une citation.

    **Une borne de plus, story 5.6 (L1c), et c'est la même leçon que la quatrième.** Une amorce
    jointe traverse ensuite les contrôles du code, et l'un d'eux peut rejeter la claim **entière** à
    cause du passage qu'on vient d'ajouter — la projection censée sauver une citation la détruisait
    alors une seconde fois, par un autre chemin :

    - **l'amorce ne doit pas ajouter un second `kind` décisionnel.** « Une seule clause par
      affirmation » (D6) est un contrôle de code : une claim qui cite une `garantie` et une
      `condition` rend la table d'AD-6 indécidable et part en `ambigue`. Or la phrase qui ouvre une
      énumération de garanties est souvent typée `condition` (« La Compagnie garantit, pour autant
      qu'une plainte ait été déposée : »). Mesuré le 04/09/2026 sur le rejeu « vol » : trois
      affirmations rejetées sur un passage que le modèle n'avait pas écrit.

    L'item reste alors cité seul — l'état d'avant cette projection, jamais moins.

    **La borne jumelle a été levée, story 5.6 (L1f).** L1c interdisait aussi de joindre une amorce
    non unique dans le document, parce qu'AD-3 rejetait `ambigue` toute citation dont le passage se
    relit ailleurs — et la phrase qui ouvre une énumération d'exclusions est la plus répétée d'un
    contrat (huit des 58 amorces d'énumération d'AXA, deux des six de Baloise). C'était renoncer au
    contexte précisément là où le contrat en a le plus besoin. AD-3, précisé le 04/09/2026, lit
    désormais l'amorce **adjacente** à un item cité par la même claim comme le contexte de cet item :
    ce que le code joint ici est par construction `amorce_de_lenumeration(item)`, donc adjacent, donc
    accepté (`steps.verifier._delier_les_amorces`). Il n'y a plus de rejet à devancer.
    """
    servis = set(blocs_servis)
    jointes = 0
    claims: list[Claim] = []
    for claim in draft.claims:
        deja = {quote.block_id for quote in claim.quotes}
        ajouts: list[Quote] = []
        for quote in claim.quotes:
            try:
                if index.doc_of(quote.block_id) != doc_id:
                    continue
                amorce = index.amorce_de_lenumeration(quote.block_id)
                if amorce is None or amorce in deja or amorce not in servis:
                    continue
                document = index.corpus.documents[doc_id]
                texte = document.block(amorce).text_norm
            except KeyError:
                continue
            if not texte.strip():
                continue
            if _second_kind_decisionnel(document, amorce, deja):
                continue  # D6 la rejetterait `ambigue` : deux clauses dans une même affirmation
            deja.add(amorce)
            ajouts.append(Quote(block_id=amorce, quote=texte))
        claims.append(claim.model_copy(update={"quotes": [*claim.quotes, *ajouts]})
                      if ajouts else claim)
        jointes += len(ajouts)
    if not jointes:
        return draft, 0
    return draft.model_copy(update={"claims": claims}), jointes


def _second_kind_decisionnel(document: Any, amorce: str, deja: set[str]) -> bool:
    """L'amorce ajouterait-elle à cette claim un **second** `kind` décisionnel (D6) ?

    Le typage vient de l'ingestion, jamais du modèle, et c'est exactement le calcul que
    `steps.verifier` refait sur les blocs cités : deux kinds décisionnels dans une même affirmation
    la font rejeter `ambigue`. Une amorce non décisionnelle (un paragraphe, une définition) est le
    contexte de la clause et n'entre pas dans ce compte — c'est ce que la projection sert à joindre.
    """
    try:
        kind = document.block(amorce).kind
    except KeyError:
        return False
    if kind not in KINDS_DECISIONNELS:
        return False
    kinds = {kind}
    for block_id in deja:
        try:
            autre = document.block(block_id).kind
        except KeyError:
            continue
        if autre in KINDS_DECISIONNELS:
            kinds.add(autre)
    return len(kinds) > 1


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


# Story 5.6 (L1b). Les **ouvertures anaphoriques** : les mots par lesquels une phrase reprend un
# objet nommé ailleurs. Lexique volontairement court et fermé, et il vit dans le **code**, jamais
# dans un prompt : la règle des antécédents y était écrite depuis L1 et n'était pas respectée
# (mesuré le 03/09/2026 — « Sans cette déclaration, ni matricule… », dont l'antécédent est dans la
# phrase précédente). Une consigne de plus n'aurait rien changé ; une jointure mécanique, si.
#
# Les quatre langues servies, parce que la jointure est un contrat d'affichage et qu'une réponse en
# portugais se coupe comme une réponse en français. Ce qui n'y figure pas ne déclenche rien : ne
# jamais joindre est le comportement d'avant, jamais une régression.
#
# Ni « en » ni « y » : en tête de phrase, ces deux-là ne sont jamais le pronom (« En résumé, »,
# « En ce qui concerne… ») — mesuré sur un témoin de la chaîne, où « En résumé. » se faisait joindre
# à la phrase d'avant. Une jointure de trop grossit l'unité de vérification sans rien réparer.
OUVERTURES_ANAPHORIQUES: frozenset[str] = frozenset({
    "ce", "cet", "cette", "ces", "celui", "celle", "ceux", "celles", "cela", "ceci", "ca",
    "il", "elle", "ils", "elles", "lui", "leur", "leurs",
    "this", "that", "these", "those", "it", "its", "they", "them", "their",
    "dies", "diese", "dieser", "dieses", "diesen", "diesem", "er", "sie", "es", "ihr", "ihre",
    "este", "esta", "estes", "estas", "esse", "essa", "esses", "essas", "isso", "isto",
    "ele", "ela", "eles", "elas", "seu", "sua",
})

# Une préposition ne donne pas d'antécédent : « Sans cette déclaration » ouvre aussi mal que
# « Cette déclaration ». Elle est donc traversée avant de lire le mot qui suit.
PREPOSITIONS_D_OUVERTURE: frozenset[str] = frozenset({
    "sans", "avec", "pour", "par", "dans", "sur", "sous", "apres", "avant", "malgre", "selon",
    "des", "depuis", "outre", "chez",
    "without", "with", "for", "by", "in", "on", "under", "after", "before", "besides",
    "ohne", "mit", "fur", "nach", "vor", "bei", "unter", "trotz", "laut",
    "sem", "com", "para", "por", "em", "sob", "apos", "antes", "alem",
})

# Les emplois **impersonnels** de « il » : ils n'ont pas d'antécédent parce qu'ils n'ont pas de
# sujet, et les joindre grossirait l'unité de vérification sans rien réparer (« Il faut apporter les
# originaux… » est une phrase autonome). Lus seulement après « il », jamais ailleurs.
IMPERSONNELS: frozenset[str] = frozenset({
    "faut", "y", "existe", "convient", "suffit", "importe", "vaut", "s'agit", "arrive",
})


def _ouverture_anaphorique(texte: str) -> bool:
    """Le texte s'ouvre-t-il sur un démonstratif ou un pronom dont l'objet n'est pas dans la phrase ?

    Lecture mécanique, sur la forme normalisée (`normalize` : minuscules, diacritiques retirés) :
    on traverse au plus une préposition, puis on lit le premier mot porteur. C'est grossier, et
    c'est du code — une phrase orpheline se voit à son ouverture, pas à son sens.
    """
    mots = normalize(texte).replace("'", " ").split()
    if not mots:
        return False
    if mots[0] in PREPOSITIONS_D_OUVERTURE:
        mots = mots[1:]
    if not mots or mots[0] not in OUVERTURES_ANAPHORIQUES:
        return False
    if mots[0] == "il" and len(mots) > 1 and mots[1] in IMPERSONNELS:
        return False
    return True


def joindre_segments_orphelins(draft: AnswerDraft) -> tuple[AnswerDraft, int]:
    """Un segment qui s'ouvre sur une anaphore est **joint** au précédent : une seule unité.

    Story 5.6 (L1b). Le contrôle juge un segment et l'affichage retire un segment : ce sont les
    deux faces d'une même coupe. Une phrase dont l'antécédent est dans la phrase d'avant ne survit
    donc pas à la coupe — elle est affichée seule (« Sans cette déclaration, ni matricule… », sans
    qu'on sache laquelle) ou jugée seule, sur des passages qui soutenaient sa voisine.

    La jointure est mécanique et **conservatrice** : elle ne retire rien, ne réécrit rien et
    n'ajoute rien. Elle fusionne deux segments consécutifs en un seul texte (l'espace simple les
    sépare) dont les `claim_ids` sont l'union ordonnée des deux, si bien que le contrôle juge le
    tout sur la réunion de leurs passages et que l'affichage les emporte ensemble.

    Un segment `limite` n'est jamais joint, dans un sens ni dans l'autre : il ne rejoint pas
    `Answer.texte` mais `Answer.unknown[]`, et le fondre dans une phrase affichée transformerait une
    lacune déclarée en réponse. Le `kind` du segment joint est `factuel` dès que l'un des deux l'est
    — un texte qui affirme quelque chose n'est pas une transition, et `AnswerDraft` exige alors
    qu'il cite au moins une affirmation.
    """
    if len(draft.segments) < 2:
        return draft, 0
    segments: list[AnswerSegment] = []
    jointures = 0
    for segment in draft.segments:
        precedent = segments[-1] if segments else None
        joignable = (precedent is not None and precedent.text.strip() and segment.text.strip()
                     and precedent.kind != "limite" and segment.kind != "limite")
        if not joignable or not _ouverture_anaphorique(segment.text):
            segments.append(segment)
            continue
        assert precedent is not None
        claim_ids = list(precedent.claim_ids)
        claim_ids += [cid for cid in segment.claim_ids if cid not in claim_ids]
        kind = "factuel" if "factuel" in (precedent.kind, segment.kind) else precedent.kind
        segments[-1] = AnswerSegment(text=f"{precedent.text.rstrip()} {segment.text.lstrip()}",
                                     kind=kind, claim_ids=claim_ids)
        jointures += 1
    if not jointures:
        return draft, 0
    return draft.model_copy(update={"segments": segments}), jointures


# La frontière de phrase : une ponctuation terminale, de l'espace, puis une ouverture de phrase —
# une majuscule, éventuellement précédée d'un guillemet ou d'une parenthèse. Exiger la majuscule est
# ce qui évite de couper « 3.5 % », « e.g. » ou « art. 12 » ; les quatre langues servies ouvrent
# toutes leurs phrases par une capitale, et l'allemand capitalise ses noms **au milieu** d'une
# phrase, jamais après un point (le contre-exemple n'existe donc pas).
_FIN_DE_PHRASE = re.compile(r'(?<=[.!?…])\s+(?=[«"“‘\'(\[]*[A-ZÀ-ÖØ-Þ])')


def decouper_en_phrases(texte: str, *, place: int) -> list[str]:
    """Le texte découpé en **unités de lecture** : des phrases, dont les orphelines sont jointes.

    Story 5.6 (L1d). Depuis L1, une affirmation peut être un **paragraphe** : le contrôle la jugeait
    alors d'un bloc, et une seule phrase qui dépassait les passages cités faisait tomber le
    paragraphe entier — donc la sous-question qu'il portait, et donc la réponse (mesuré le
    04/09/2026 sur `g-ecole` et `g-impots`, en français comme en anglais). La propriété exigée est
    plus fine que le bloc : *chaque phrase affichée* est soutenue. C'est donc la phrase qui est
    l'unité, ici comme elle l'est déjà pour les segments.

    La coupe est celle de L1b, réemployée telle quelle : une phrase qui s'ouvre sur une anaphore
    (`_ouverture_anaphorique`) n'a pas son antécédent en elle-même — elle est **jointe** à la
    précédente, et les deux vivent ou tombent ensemble. Sans cela, retirer une phrase pourrait
    laisser « Sans cette déclaration, … » sans son objet, ce qui est précisément ce que la jointure
    des segments empêche déjà à l'affichage. Un seul découpage, une seule règle d'antécédent.

    `place` borne le nombre d'unités : au-delà, le reste est **fondu dans la dernière** — jamais
    retiré. Une borne qui supprimerait du texte affiché serait un mode d'échec de plus ; une borne
    qui rend l'unité plus grosse ne fait que retrouver le comportement d'avant, qui jugeait tout
    d'un bloc.
    """
    brutes = [p for p in _FIN_DE_PHRASE.split(texte.strip()) if p.strip()]
    if not brutes:
        return []
    phrases: list[str] = []
    for brute in brutes:
        if phrases and _ouverture_anaphorique(brute):
            phrases[-1] = f"{phrases[-1].rstrip()} {brute.lstrip()}"
            continue
        phrases.append(brute.strip())
    if place >= 1 and len(phrases) > place:
        phrases = [*phrases[: place - 1], " ".join(phrases[place - 1:])]
    return phrases
