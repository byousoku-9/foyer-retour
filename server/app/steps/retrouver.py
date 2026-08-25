"""AD-1 — *retrouver*, variante `deterministe` (J+1) : code pur, zéro appel modèle.

`chercher(terms + scope.themes, limit=search_limit)`, puis ouverture groupée des nœuds candidats par
score (≤ `max_opens` nœuds, fenêtre `node_window` contenant le meilleur hit du nœud), puis suivi
**automatique** d'un niveau des renvois (`Block.refs`) des blocs ouverts et des `definitions()` des
termes — de la question **et** de ceux rencontrés dans les blocs ouverts —, hors quota `max_opens`.
`truncated=True` si une fenêtre reste coupée (pas de pagination en déterministe), si des nœuds
candidats dépassent `max_opens`, ou si le budget de blocs/tokens a écarté quelque chose. Les blocs
sont relus depuis le corpus (objets `Document.block`), jamais modifiés ; l'étape n'affirme aucune
absence du corpus (AD-1) et ne voit que `ParsedQuestion` — jamais l'historique.

`StepTrace(tier=STEP_TIERS["retrouver"], calls=[])` : AD-9 fixe l'affectation étape → tier **sans
exception** (`retrouver → reason`) ; c'est `calls=[]` — et lui seul — qui dit que la variante
déterministe n'a appelé aucun modèle (revue Codex 1.4, B3). `discarded_block_ids` reste exactement
ce qu'AD-10 en dit : les candidats de `chercher` non transmis au modèle.

**Le `RetrievalBudget` borne toute l'étape** (AD-1 : « nœuds, blocs, tokens, définitions et renvois
inclus »). `max_blocks` et `max_tokens` sont appliqués ensemble par unités de dépendance : un bloc de
fenêtre voyage avec les cibles de ses renvois, jamais l'inverse — une cible sans le passage qui la
cite est inutilisable et peut même égarer la rédaction (revue Codex 1.4, B6). Une unité qui n'entre
pas est sautée (les suivantes sont essayées : le budget n'est pas gaspillé), et `truncated` le dit.
Faute de tokenizer en code pur, les tokens sont majorés par l'heuristique d'`estimate_cost`.
`max_llm_turns` est sans objet pour cette variante.
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from server.app.config import Settings
from server.app.corpus.dictionary import Dictionnaire
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.domain import Block, RetrievalBudget, RetrievalResult
from server.app.domain.question import ParsedQuestion
from server.app.domain.trace import CheckResult, StepTrace
from server.app.llm.models import STEP_TIERS
from server.app.llm.pricing import estimate_tokens


def retrouver_deterministe(parsed: ParsedQuestion, *, corpus: Corpus, index: Index,
                           budget: RetrievalBudget, settings: Settings, doc_id: str | None = None,
                           kinds_prioritaires: Iterable[str] | None = None,
                           dictionnaire: Dictionnaire | None = None
                           ) -> tuple[RetrievalResult, StepTrace]:
    """`kinds_prioritaires` (story 1.8) : à score égal, les blocs de ces `Block.kind` passent devant.

    Il ne **filtre** pas — AC du sinistre : « cherche les blocs `garantie|exclusion|condition|
    franchise` candidats », pas « ne cherche qu'eux ». Le typage étant manuel à J+1 et ne couvrant que
    quatre blocs du contrat, le rappel du sinistre repose encore surtout sur les termes ; c'est le
    typage automatique (story 3.2) qui donnera son plein effet à ce départage. `None` (le guide) laisse
    l'ordre de recherche exactement tel qu'il était.

    `dictionnaire` (story 2.1, AD-5) : le **seul** point d'entrée élargi. `chercher` accepte déjà
    `{canonique: [variantes]}` — formes normalisées par groupe, score = nombre de groupes touchés —
    donc l'élargissement ne change ni le classement ni la déduplication, il ajoute des formes à
    chercher pour les mêmes termes. Il n'est employé que si le dictionnaire est `utilisable`
    (chargé **et** décrivant le corpus servi) : `validated` ne commande que le court-circuit du
    pipeline, pas l'élargissement — élargir n'affirme rien, chaque phrase affichée reste vérifiée
    contre le corpus (AD-3), tandis que refuser est une affirmation négative qui, elle, demande une
    signature humaine.

    `index.definitions()` continue de recevoir `terms` **seuls** : son appariement `defines`/terme se
    fait déjà dans les deux sens, et lui donner les variantes multiplierait un faux positif connu et
    non corrigé (reprise différée `target_story: 4.2`, à border avec une mesure).
    """
    t0 = time.monotonic()
    # Source unique des termes cherchés (story 1.5) : l'`AbsenceProof` d'un refus « zéro hit » doit
    # nommer exactement ce que cette étape a cherché (AD-4 `terms_searched`).
    terms = parsed.termes_de_recherche()

    if doc_id is not None and doc_id not in corpus.documents:
        # `chercher` lève déjà sur un doc_id inconnu, mais il n'est pas appelé quand aucun terme n'a
        # été extrait : sans ce contrôle, une faute de frappe rendrait un résultat vide silencieux.
        raise KeyError(doc_id)

    def bloc(block_id: str) -> Block:
        return corpus.documents[index.doc_of(block_id)].block(block_id)

    truncated = False
    elargi = dictionnaire is not None and dictionnaire.utilisable
    cherches = dictionnaire.expand(terms) if elargi else terms
    hits = (index.chercher(cherches, limit=budget.search_limit, doc_id=doc_id,
                           kinds_prioritaires=kinds_prioritaires) if terms else [])

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
    fenetres: list[str] = []
    for node_id in nodes[: budget.max_opens]:
        window = index.ouvrir_noeud(node_id, focus_block_id=best_hit[node_id], node_window=budget.node_window)
        if window.truncated:
            truncated = True  # pas de pagination en déterministe : la fenêtre reste coupée
        for b in window.blocks:
            if b.block_id not in fenetres:
                fenetres.append(b.block_id)

    # Unités de dépendance, hors quota `max_opens` : un bloc de fenêtre et, avec lui, les cibles d'un
    # seul niveau de ses renvois (les cibles ne sont pas suivies à leur tour — Deferred du spine
    # « renvois en chaîne »). Une cible déjà présente dans une fenêtre reste à sa place.
    unites: list[list[str]] = []
    for block_id in fenetres:
        unite = [block_id]
        for cible in bloc(block_id).refs:
            if cible not in fenetres and cible not in unite:
                unite.append(cible)
        unites.append(unite)

    # Définitions (hors quota `max_opens`) : des termes de la question et de ceux rencontrés dans les
    # blocs ouverts, résolues dans leur portée par l'index (AD-1, AD-2). Elles se suffisent à
    # elles-mêmes — aucun référent à conserver — et passent donc en premier dans le budget.
    definitions = [b for b, _ in index.definitions(terms, doc_id=doc_id, blocs_ouverts=fenetres)
                   if b not in fenetres]
    unites = [[d] for d in definitions] + unites

    retenus: list[str] = []
    seen: set[str] = set()
    blocs_utilises, tokens_utilises = 0, 0
    for unite in unites:
        nouveaux = [b for b in unite if b not in seen]
        cout_tokens = sum(estimate_tokens(f"{b}\n{bloc(b).text}", settings) for b in nouveaux)
        if budget.max_blocks is not None and blocs_utilises + len(nouveaux) > budget.max_blocks:
            truncated = True
            continue  # unité sautée : les suivantes, plus petites, peuvent encore tenir
        if budget.max_tokens is not None and tokens_utilises + cout_tokens > budget.max_tokens:
            truncated = True
            continue
        blocs_utilises += len(nouveaux)
        tokens_utilises += cout_tokens
        for b in nouveaux:
            seen.add(b)
            retenus.append(b)

    # Ordre rendu au modèle : les fenêtres dans l'ordre de lecture, puis les cibles de renvoi, puis
    # les définitions — l'ordre d'admission dans le budget n'est pas l'ordre de lecture.
    ordre: list[str] = []
    for b in (*fenetres, *(c for u in unites for c in u[1:]), *definitions):
        if b in seen and b not in ordre:
            ordre.append(b)
    blocs = [bloc(b) for b in ordre]

    opened = [b.block_id for b in blocs]
    # AD-10, littéralement : « candidats de `chercher` non ouverts » — donc les hits qui ne sont pas
    # transmis au modèle, et rien d'autre. Un bloc voisin écarté par le budget n'est pas un candidat
    # de recherche : c'est `truncated` qui porte cette information (revue Codex 1.4, B5).
    discarded = [b for b, _ in hits if b not in seen]
    result = RetrievalResult(blocs=blocs, opened_block_ids=opened, discarded_block_ids=discarded,
                             truncated=truncated)
    step = StepTrace(name="retrouver", tier=STEP_TIERS["retrouver"], ms=int((time.monotonic() - t0) * 1000),
                     opened_block_ids=list(opened), discarded_block_ids=list(discarded))
    if elargi:
        # AD-10 / AD-16 : la trace dit **combien** de formes ont été ajoutées et à combien de termes,
        # jamais lesquelles. AD-4 interdit de publier la liste des variantes, et la trace est lue par
        # le front « pourquoi cette réponse » : un compte se recoupe avec `variants_count` de
        # l'`AbsenceProof`, une liste ferait fuir le dictionnaire terme par terme.
        ajoutees = dictionnaire.variants_count(terms)
        touches = sum(1 for v in cherches.values() if v)
        step.checks.append(CheckResult(
            name="dictionnaire", ok=True,
            detail=f"{ajoutees} variante(s) ajoutée(s) à {touches} terme(s)"))
    return result, step
