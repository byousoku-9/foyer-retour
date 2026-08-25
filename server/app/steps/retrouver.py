"""AD-1 — *retrouver*, variante `deterministe` (J+1) : code pur, zéro appel modèle.

`chercher(terms + scope.themes, limit=search_limit)`, puis ouverture groupée des nœuds candidats par
score (≤ `max_opens` nœuds, fenêtre `node_window` contenant le meilleur hit du nœud), puis suivi
**automatique** d'un niveau des renvois (`Block.refs`) des blocs ouverts et des `definitions()` des
termes — de la question **et** de ceux rencontrés dans les blocs ouverts —, hors quota `max_opens`.
Story 2.3 : parmi ces `max_opens` nœuds, `profil_max_opens` places sont **réservées** aux nœuds que
le profil désigne (`noeuds_prioritaires`, calculés par l'appelant) quand ils sont candidats mais hors
quota ; elles sont prises aux derniers nœuds retenus, et les promus sont ouverts après eux.
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
from server.app.corpus.dictionary import Dictionnaire, forme
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.domain import Block, RetrievalBudget, RetrievalResult
from server.app.domain.question import ParsedQuestion
from server.app.domain.trace import CheckResult, StepTrace
from server.app.llm.models import STEP_TIERS
from server.app.llm.pricing import estimate_tokens


def _reserver(nodes: list[str], noeuds_prioritaires: Iterable[str] | None, max_opens: int,
              profil_max_opens: int) -> tuple[list[str], tuple[list[str], list[str]]]:
    """Réserve au plus `profil_max_opens` places aux nœuds désignés (story 2.3).

    Rend `(nœuds ouverts, (promus, cédés))`. Les promus sont les mieux classés des désignés restés
    hors quota ; ils prennent la place des **derniers** nœuds retenus et sont ouverts après eux. Un
    nœud lui-même désigné ne cède jamais sa place à un autre désigné : l'échange serait nul, et il
    ferait perdre au profil ce que la réserve vient de lui donner.
    """
    ouverts = nodes[:max_opens]
    designes = set(noeuds_prioritaires or ())
    if not designes or profil_max_opens <= 0:
        return ouverts, ([], [])
    hors_quota = [n for n in nodes[max_opens:] if n in designes]
    cessibles = [n for n in reversed(ouverts) if n not in designes]
    places = min(profil_max_opens, len(hors_quota), len(cessibles))
    if not places:
        return ouverts, ([], [])
    promus, cedes = hors_quota[:places], cessibles[:places]
    return [n for n in ouverts if n not in set(cedes)] + promus, (promus, cedes)


def retrouver_deterministe(parsed: ParsedQuestion, *, corpus: Corpus, index: Index,
                           budget: RetrievalBudget, settings: Settings, doc_id: str | None = None,
                           kinds_prioritaires: Iterable[str] | None = None,
                           noeuds_prioritaires: Iterable[str] | None = None,
                           dictionnaire: Dictionnaire | None = None
                           ) -> tuple[RetrievalResult, StepTrace]:
    """`kinds_prioritaires` (story 1.8) : à score égal, les blocs de ces `Block.kind` passent devant.

    Il ne **filtre** pas — AC du sinistre : « cherche les blocs `garantie|exclusion|condition|
    franchise` candidats », pas « ne cherche qu'eux ». Le typage étant manuel à J+1 et ne couvrant que
    quatre blocs du contrat, le rappel du sinistre repose encore surtout sur les termes ; c'est le
    typage automatique (story 3.2) qui donnera son plein effet à ce départage. `None` (le guide) laisse
    l'ordre de recherche exactement tel qu'il était.

    `noeuds_prioritaires` (story 2.3) : les nœuds que le **profil** désigne, calculés par l'appelant
    (`domain/profil.py::noeuds_du_profil` sur `Document.parcours`) et passés comme un paramètre
    nommé — AD-1 : *retrouver* ne reçoit que `ParsedQuestion` et des paramètres de recherche, jamais
    le profil ni l'historique. Ils se voient **réserver** au plus `settings.profil_max_opens` places
    parmi les `max_opens` nœuds ouverts, prises aux derniers nœuds retenus.

    **Le profil ordonne, il n'ajoute jamais.** Un nœud désigné n'est promu que s'il est déjà
    *candidat*, c'est-à-dire s'il a un hit pour les termes cherchés : aucune fiche n'entre dans le
    contexte du modèle du seul fait du profil, et rien n'est jamais écarté parce que le profil ne le
    désigne pas. Liste vide ou désignés tous déjà retenus ⇒ résultat identique à celui d'avant la
    story, à l'octet près.

    **Une réserve, et non un tri.** Mettre les nœuds du profil en tête serait plus littéral, mais
    l'ordre des nœuds est aussi l'ordre d'admission au budget de blocs (`retrieval_max_blocks`,
    `node_window`) : une fiche du profil ouverte en premier peut consommer tout le budget et faire
    disparaître la fiche qui répond à la question. Les nœuds promus sont donc ouverts **après** les
    autres — ils gagnent une place, pas la priorité de lecture.

    `dictionnaire` (story 2.1, AD-5) : le **seul** point d'entrée élargi. `chercher` accepte déjà
    `{canonique: [variantes]}` — formes normalisées par groupe, score = nombre de groupes touchés —
    donc l'élargissement ne change ni le classement ni la déduplication, il ajoute des formes à
    chercher pour les mêmes termes. Il n'est employé que si le dictionnaire est
    `utilisable_pour(doc_id)` (chargé, décrivant le corpus servi, **et** portant l'empreinte du
    document interrogé — revue Codex 2.1, B3) : `validated` ne commande que le court-circuit du
    pipeline, pas l'élargissement — élargir n'affirme rien, chaque phrase affichée reste vérifiée
    contre le corpus (AD-3), tandis que refuser est une affirmation négative qui, elle, demande une
    signature humaine.

    `index.definitions()` continue de recevoir `terms` **seuls** : son appariement `defines`/terme se
    fait déjà dans les deux sens, et lui donner les variantes multiplierait un faux positif connu et
    non corrigé (reprise différée `target_story: 4.2`, à border avec une mesure).

    **Le pipeline sinistre ne passe rien ici, et c'est un choix de périmètre, pas un oubli** (revue
    coordonnée 2.1). L'AC de la story 2.1 nomme littéralement le corpus `lux-guide` : le dictionnaire
    livré ne décrit que le guide, et `pipelines/sinistre.py` appelle donc cette étape sans
    `dictionnaire` — élargir la recherche d'un contrat avec le vocabulaire d'un guide d'installation
    n'aurait aucun sens. Le schéma, lui, est **déjà** multi-documents (`corpus_source_hashes` est une
    table, et `corpus/dictionary._corpus_ok` valide chaque entrée contre le manifest) ; mais l'objet
    chargé est lié à **un** document — celui que `load_dictionary` a reçu — et `utilisable_pour` le
    vérifie ici, si bien qu'un dictionnaire de contrat ne peut pas élargir la recherche du guide. Le jour où un contrat en aura un, c'est ici et dans `pipelines/sinistre.py` que le
    passage se pose, pas dans le chargement.
    """
    t0 = time.monotonic()
    # Matérialisé une fois : la liste est lue deux fois (la réserve, puis la trace), et un itérateur
    # consommé la première fois rendrait la seconde muette sans rien signaler.
    designes = list(noeuds_prioritaires or ())
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
    # `utilisable_pour(doc_id)` et non `utilisable` (revue Codex 2.1, B3) : le dictionnaire ne vaut
    # que pour le document dont il porte l'empreinte. Une recherche sans `doc_id` — sur tout le
    # corpus — n'élargit donc rien, et le vocabulaire du guide ne peut pas ouvrir des blocs de contrat.
    elargi = dictionnaire is not None and dictionnaire.utilisable_pour(doc_id)
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
    ouverts, (promus, cedes) = _reserver(nodes, designes, budget.max_opens, budget.profil_max_opens)
    fenetres: list[str] = []
    # De quel nœud vient chaque bloc de fenêtre : c'est ce qui permet de dire, **après** le budget,
    # quels nœuds ont réellement contribué aux blocs transmis au modèle (revue coordonnée 2.3, A1).
    noeud_de: dict[str, str] = {}
    for node_id in ouverts:
        window = index.ouvrir_noeud(node_id, focus_block_id=best_hit[node_id], node_window=budget.node_window)
        if window.truncated:
            truncated = True  # pas de pagination en déterministe : la fenêtre reste coupée
        for b in window.blocks:
            if b.block_id not in fenetres:
                fenetres.append(b.block_id)
                noeud_de[b.block_id] = node_id

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
    if designes and budget.profil_max_opens > 0:
        # AD-10 : la trace dit ce que le profil a **fait**, pas ce qu'il déclare. Les `node_id` du
        # guide sont nos propres identifiants, produits par l'ingestion (AD-2) — ils ne sont ni du
        # contenu de bloc ni une donnée personnelle, et sans eux la première AC (« la trace le dit »)
        # ne serait pas vérifiable. Les clés du profil, elles, n'apparaissent nulle part ici.
        #
        # **Composé après le budget, et non après la réserve** (revue coordonnée 2.3, A1). Réserver
        # une place n'est pas l'occuper : l'unité de dépendance d'un nœud promu peut être écartée par
        # `max_blocks`/`max_tokens` comme n'importe quelle autre, et le résultat est alors identique
        # au témoin sans profil — pendant que la trace annonçait « 2 places réservées ». L'AC dit
        # « la trace le dit » : elle doit dire vrai, donc elle se lit sur `opened_block_ids`.
        contributeurs = {noeud_de[b] for b in opened if b in noeud_de}
        occupees = [n for n in promus if n in contributeurs]
        perdus = [n for n in promus if n not in contributeurs]
        if not promus:
            detail, ok = ("aucune place réservée : les nœuds désignés par le profil sont déjà "
                          "retenus, ou sans hit pour les termes cherchés"), True
        else:
            detail = (f"{len(occupees)} place(s) réservée(s) sur {budget.profil_max_opens} "
                      f"({', '.join(occupees) or 'aucune'}) ; "
                      f"{len(cedes)} nœud(s) cédé(s) ({', '.join(cedes)})")
            ok = not perdus
            if perdus:
                # Le nœud a bien pris la place d'un autre, puis a tout perdu au budget de blocs : le
                # profil a coûté une place à la question **sans rien rendre**. C'est un échec, et il
                # se dit comme tel — un `ok=True` ferait passer une perte nette pour un succès.
                detail += (f" ; {len(perdus)} promu(s) sans bloc retenu ({', '.join(perdus)}) : le "
                           f"budget de blocs a écarté leur fenêtre")
        step.checks.append(CheckResult(name="noeuds_du_profil", ok=ok, detail=detail))
    if elargi:
        # AD-10 / AD-16 : la trace dit **combien** de formes ont été ajoutées et à combien de termes,
        # jamais lesquelles. AD-4 interdit de publier la liste des variantes, et la trace est lue par
        # le front « pourquoi cette réponse » : un compte se recoupe avec `variants_count` de
        # l'`AbsenceProof`, une liste ferait fuir le dictionnaire terme par terme.
        #
        # **Les deux nombres se comptent avec la même règle** (revue coordonnée 2.1). `variants_count`
        # exclut les formes déjà présentes parmi les termes cherchés — une variante qui *est* l'un
        # des termes de la question n'ajoute rien à la recherche ; un `touches` qui ne les excluait
        # pas produisait des détails comme « 0 variante(s) ajoutée(s) à 2 terme(s) », qui se lit
        # comme une contradiction. Un terme est « touché » s'il apporte au moins une forme que la
        # question ne cherchait pas déjà.
        base = {forme(t) for t in terms} - {""}
        ajoutees = dictionnaire.variants_count(terms)
        touches = sum(1 for variantes in cherches.values()
                      if any(v and v not in base for v in variantes))
        step.checks.append(CheckResult(
            name="dictionnaire", ok=True,
            detail=f"{ajoutees} variante(s) ajoutée(s) à {touches} terme(s)"))
    return result, step
