"""AD-1 — les deux implémentations de *retrouver*, sous le même contrat et le même budget.

La variante `deterministe` (J+1) reste du code pur. La variante `outils` (story 2.6) laisse le tier
configuré parcourir le sommaire avec exactement quatre outils, en deux tours au plus ; elle ne change
ni la chaîne du pipeline, ni `RetrievalResult`, ni la vérification aval. Le premier tour utile suffit
dès qu'il a admis des blocs sans laisser de pagination ouverte.

`chercher(terms + scope.themes, limit=search_limit)`, puis ouverture groupée des nœuds candidats par
score (≤ `max_opens` nœuds, fenêtre `node_window` contenant le meilleur hit du nœud), puis suivi
**automatique** d'un niveau des renvois (`Block.refs`) des blocs ouverts et des `definitions()` des
termes — de la question **et** de ceux rencontrés dans les blocs ouverts —, hors quota `max_opens`.
Story 2.3 : parmi ces `max_opens` nœuds, `profil_max_opens` places sont **réservées** aux nœuds que
le profil désigne (`ParsedQuestion.scope.noeuds`, construits par *comprendre*) quand ils sont
candidats mais hors quota ; elles sont prises aux derniers nœuds retenus, et les promus sont ouverts
après eux. Une place réservée que le budget de blocs laisse vide est **rendue** au nœud qui l'avait
cédée (revue Codex 2.3, I1) : le profil ne peut ni ajouter une fiche, ni en retirer une.
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
`max_llm_turns` est sans objet pour la variante déterministe ; la variante outils le borne à deux.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from typing import Any

from server.app.config import Settings
from server.app.corpus.dictionary import Dictionnaire, forme
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.domain import Block, RetrievalBudget, RetrievalResult
from server.app.domain.errors import PipelineError
from server.app.domain.question import ParsedQuestion
from server.app.domain.trace import CheckResult, StepTrace
from server.app.llm.models import STEP_TIERS
from server.app.llm.pricing import estimate_tokens
from server.app.llm.prompting import render_prompt, untrusted


OUTILS_RECHERCHE: list[dict[str, Any]] = [
    {
        "name": "sommaire",
        "description": "Relire le sommaire compact versionné du document courant.",
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "properties": {"doc_id": {"type": "string"}}, "required": ["doc_id"],
        },
    },
    {
        "name": "ouvrir_noeud",
        "description": "Ouvrir une fenêtre d'un nœud, éventuellement centrée ou paginée.",
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "node_id": {"type": "string"},
                "focus_block_id": {"type": "string"},
                "cursor": {"type": "integer", "minimum": 0},
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "chercher",
        "description": "Chercher des candidats sans recevoir leur texte.",
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "properties": {"termes": {"type": "array", "items": {"type": "string"}}},
            "required": ["termes"],
        },
    },
    {
        "name": "definitions",
        "description": "Obtenir les définitions applicables et les cibles des renvois ouverts.",
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "termes": {"type": "array", "items": {"type": "string"}},
                "blocs_ouverts": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["termes"],
        },
    },
]


def _strings(value: object) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        return None
    return [v.strip() for v in value if v.strip()]


def _content_json(message: Any) -> list[dict[str, Any]]:
    """Blocs bruts du SDK, réinjectables comme tour assistant sans texte parallèle."""
    return [block.model_dump(mode="json") if hasattr(block, "model_dump") else dict(block)
            for block in message.content]


async def retrouver_outils(parsed: ParsedQuestion, *, corpus: Corpus, index: Index,
                            budget: RetrievalBudget, settings: Settings, client: Any,
                            request_budget: Any, doc_id: str,
                            dictionnaire: Dictionnaire | None = None,
                            candidats_out: list[str] | None = None,
                            ) -> tuple[RetrievalResult, StepTrace]:
    """Variante bornée de navigation par les quatre outils d'AD-1, sur deux tours au plus."""
    t0 = time.monotonic()
    # L'amendement 2.6 autorise explicitement l'arbitrage du tier de navigation. Le déterministe
    # conserve l'affectation historique `reason`; la variante appelée publie son tier réel.
    step = StepTrace(name="retrouver", tier=settings.retrouver_outils_tier)
    if doc_id not in corpus.documents:
        raise KeyError(doc_id)
    document = corpus.documents[doc_id]
    terms = parsed.termes_de_recherche()
    elargi = dictionnaire is not None and dictionnaire.utilisable_pour(doc_id)
    prompt = render_prompt(
        "retrouver", doc_id=doc_id, max_llm_turns=budget.max_llm_turns,
        max_opens=budget.max_opens,
        sommaire=untrusted("sommaire", index.sommaire(doc_id)))
    question = {
        "question_resolue": parsed.question_resolue,
        "termes": terms,
        "scope": parsed.scope.model_dump(mode="json"),
    }
    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": untrusted("question_resolue", json.dumps(question, ensure_ascii=False)),
    }]

    admitted: list[str] = []
    admitted_set: set[str] = set()
    window_opened: list[str] = []
    search_candidates: list[str] = []
    searched_terms: list[str] = []
    blocks_used = 0
    tokens_used = 0
    opens = 0
    truncated = False
    pagination_expected: dict[str, int] = {}

    def block(block_id: str) -> Block:
        if index.doc_of(block_id) != doc_id:
            raise KeyError(block_id)
        return document.block(block_id)

    def admit(unit: list[str]) -> list[str]:
        """Admet une unité atomique sous les deux budgets ; rend ses nouveaux blocs."""
        nonlocal blocks_used, tokens_used, truncated
        # Une référence répétée dans une unité, ou la réouverture de la même fenêtre, ne consomme
        # jamais deux fois les budgets et ne produit jamais deux fois le même bloc.
        new: list[str] = []
        for candidate in unit:
            if candidate not in admitted_set and candidate not in new:
                new.append(candidate)
        try:
            token_cost = sum(estimate_tokens(f"{b}\n{block(b).text}", settings) for b in new)
        except KeyError:
            truncated = True
            return []
        if budget.max_blocks is not None and blocks_used + len(new) > budget.max_blocks:
            truncated = True
            return []
        if budget.max_tokens is not None and tokens_used + token_cost > budget.max_tokens:
            truncated = True
            return []
        blocks_used += len(new)
        tokens_used += token_cost
        for b in new:
            admitted_set.add(b)
            admitted.append(b)
        return new

    def rendered(block_ids: Iterable[str]) -> list[dict[str, Any]]:
        # C'est exactement la représentation comptée par `admit()` : identifiant + texte. Les
        # métadonnées de domaine ne servent pas à naviguer et gonflaient le résultat hors budget.
        return [{"block_id": b, "text": block(b).text} for b in block_ids]

    def canonical_forms(values: list[str]) -> set[str]:
        canoniques = dictionnaire.canoniser(values) if elargi else values
        return {forme(value) for value in canoniques} - {""}

    def invalid() -> tuple[dict[str, Any], bool]:
        nonlocal truncated
        truncated = True
        return {"error": "appel refusé : arguments invalides ou ressource hors du document courant"}, True

    def execute(name: str, args: object) -> tuple[dict[str, Any], bool]:
        nonlocal opens, truncated
        if not isinstance(args, dict):
            return invalid()
        if name == "sommaire":
            if set(args) != {"doc_id"} or args.get("doc_id") != doc_id:
                return invalid()
            return {"doc_id": doc_id, "sommaire": index.sommaire(doc_id)}, False
        if name == "chercher":
            termes = _strings(args.get("termes"))
            if set(args) != {"termes"} or not termes:
                return invalid()
            mapping: dict[str, list[str]] | list[str] = termes
            if elargi:
                mapping = dictionnaire.expand(termes)
            for terme in termes:
                if forme(terme) not in {forme(t) for t in searched_terms}:
                    searched_terms.append(terme)
            hits = index.chercher(mapping, limit=budget.search_limit + 1, doc_id=doc_id)
            search_truncated = len(hits) > budget.search_limit
            if search_truncated:
                truncated = True
                hits = hits[:budget.search_limit]
            for block_id, _node_id in hits:
                if block_id not in search_candidates:
                    search_candidates.append(block_id)
            return {"candidats": [{"block_id": b, "node_id": n} for b, n in hits],
                    "truncated": search_truncated}, False
        if name == "ouvrir_noeud":
            # Le quota porte sur les appels, pas sur les seules ouvertures valides : une rafale
            # d'identifiants faux ne doit pas contourner la borne globale.
            if opens >= budget.max_opens:
                truncated = True
                return {"error": "quota d'ouvertures épuisé", "truncated": True}, True
            opens += 1
            allowed = {"node_id", "focus_block_id", "cursor"}
            node_id, focus, cursor = args.get("node_id"), args.get("focus_block_id"), args.get("cursor")
            if (not set(args) <= allowed or not isinstance(node_id, str)
                    or (focus is not None and not isinstance(focus, str))
                    or isinstance(cursor, bool) or (cursor is not None and not isinstance(cursor, int))
                    or (focus is not None and cursor is not None)):
                return invalid()
            try:
                if index.doc_of_node(node_id) != doc_id:
                    return invalid()
                if focus is not None:
                    if index.doc_of(focus) != doc_id or focus not in search_candidates:
                        return invalid()
            except KeyError:
                return invalid()
            try:
                window = index.ouvrir_noeud(node_id, focus_block_id=focus, cursor=cursor,
                                            node_window=budget.node_window)
            except (KeyError, ValueError):
                return invalid()
            # Une pagination n'est résolue que si elle part du début puis suit chaque curseur.
            expected = pagination_expected.get(node_id, 0)
            follows = focus is None and (cursor or 0) == expected
            if window.next_cursor is not None:
                pagination_expected[node_id] = window.next_cursor if follows else -1
            elif follows:
                pagination_expected.pop(node_id, None)
            elif window.truncated:
                pagination_expected[node_id] = -1

            primary: list[str] = []
            newly: list[str] = []
            for item in window.blocks:
                # Une définition applicable éclaire le bloc primaire au même titre que son renvoi :
                # l'unité entière entre, ou le primaire n'est pas transmis isolément.
                definitions = [b for b, _ in index.definitions(
                    terms, doc_id=doc_id, blocs_ouverts=[item.block_id])]
                unit = [item.block_id, *[r for r in item.refs if r != item.block_id], *definitions]
                got = admit(unit)
                if item.block_id in got:
                    primary.append(item.block_id)
                if item.block_id in admitted_set:
                    if item.block_id not in window_opened:
                        window_opened.append(item.block_id)
                newly.extend(got)
            dependencies: list[str] = [b for b in newly if b not in primary]
            return {
                "node_id": window.node_id, "title": window.title,
                "children": [c.model_dump(mode="json") for c in window.children],
                "blocks": rendered(primary), "dependencies": rendered(dependencies),
                "truncated": window.truncated or any(b.block_id not in admitted_set for b in window.blocks),
                "next_cursor": window.next_cursor,
            }, False
        if name == "definitions":
            allowed = {"termes", "blocs_ouverts"}
            termes = _strings(args.get("termes"))
            ouverts = _strings(args.get("blocs_ouverts", list(window_opened)))
            if not set(args) <= allowed or termes is None or ouverts is None:
                return invalid()
            # Uniquement les blocs primaires d'une fenêtre : accepter une cible déjà admise ici
            # permettrait au modèle de suivre ses propres renvois au tour suivant, donc de créer
            # silencieusement une chaîne de profondeur > 1.
            if any(b not in window_opened for b in ouverts):
                return invalid()
            try:
                refs = [r for b in ouverts for r in block(b).refs]
            except KeyError:
                return invalid()
            # L'index sait déjà reconnaître les termes définis de la question et ceux réellement
            # rencontrés dans les blocs ouverts. On borne les demandes du modèle à cette union,
            # sans vocabulaire codé en dur ni exception documentaire.
            allowed_definitions = {
                b for b, _ in index.definitions(terms, doc_id=doc_id, blocs_ouverts=ouverts)
            }
            allowed_definitions.update(
                b for b, _ in index.definitions([], doc_id=doc_id, blocs_ouverts=ouverts)
            )
            requested_definitions = [
                b for b, _ in index.definitions(termes, doc_id=doc_id, blocs_ouverts=ouverts)
                if b in allowed_definitions
            ]
            ids: list[str] = []
            for candidate in (*refs, *requested_definitions):
                if candidate not in ids:
                    ids.append(candidate)
            for candidate in ids:
                admit([candidate])
            kept = [b for b in ids if b in admitted_set]
            return {"blocks": rendered(kept),
                    "truncated": any(b not in admitted_set for b in ids)}, False
        return invalid()

    used_tools = False
    for turn in range(budget.max_llm_turns):
        try:
            result = await client.tool_turn(
                tier=settings.retrouver_outils_tier, system_prefix=prompt,
                messages=messages, tools=OUTILS_RECHERCHE,
                budget=request_budget, step=step, max_tokens=settings.retrouver_outils_max_tokens)
        except PipelineError as exc:
            # Comme les autres étapes LLM : l'appel éventuellement commencé et son coût doivent
            # survivre dans la trace partielle de l'erreur terminale.
            step.ms = int((time.monotonic() - t0) * 1000)
            exc.step = step
            raise
        if result.message.stop_reason in {"max_tokens", "refusal", "pause_turn"}:
            truncated = True
        tool_uses = [b for b in result.message.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            # Un `end_turn` au second tour peut conclure honnêtement une recherche sans hit ; la
            # couverture canonique réellement observée décide alors seule de la complétude.
            if turn == 0:
                truncated = True
            break
        used_tools = True
        tool_results: list[dict[str, Any]] = []
        for use in tool_uses:
            payload, is_error = execute(str(use.name), use.input)
            content = untrusted("resultat_outil", json.dumps(payload, ensure_ascii=False, sort_keys=True))
            item: dict[str, Any] = {"type": "tool_result", "tool_use_id": str(use.id), "content": content}
            if is_error:
                item["is_error"] = True
            tool_results.append(item)
        # Le premier tour nominal regroupe recherche et ouvertures. Si des blocs sont admis et
        # qu'aucun curseur ne reste à suivre, un second appel ne peut qu'alourdir le chemin froid :
        # `RetrievalResult` est déjà prêt pour la chaîne commune.
        if turn == 0 and admitted and not pagination_expected:
            break
        if turn + 1 < budget.max_llm_turns:
            messages.extend([
                {"role": "assistant", "content": _content_json(result.message)},
                {"role": "user", "content": tool_results},
            ])
    expected_search = canonical_forms(terms)
    covered_search = canonical_forms(searched_terms)
    # Un refus `zero_hit` n'est honnête que si au moins un terme canonique existait et si les
    # recherches réellement exécutées les ont tous couverts. Une recherche vide, inventée ou
    # partielle ne devient jamais une preuve d'absence.
    absence_proven = bool(expected_search) and expected_search <= covered_search
    if not used_tools or (not admitted and (search_candidates or not absence_proven)):
        truncated = True
    if pagination_expected:
        truncated = True

    discarded = [b for b in search_candidates if b not in admitted_set]
    if candidats_out is not None:
        candidats_out.extend(b for b in search_candidates if b not in candidats_out)
    result = RetrievalResult(
        blocs=[block(b) for b in admitted], opened_block_ids=list(admitted),
        discarded_block_ids=discarded, truncated=truncated)
    step.ms = int((time.monotonic() - t0) * 1000)
    step.opened_block_ids = list(admitted)
    step.discarded_block_ids = list(discarded)
    if elargi and searched_terms:
        searched_expanded = dictionnaire.expand(searched_terms)
        base = {forme(t) for t in searched_terms} - {""}
        touches = sum(1 for variantes in searched_expanded.values()
                      if any(v and v not in base for v in variantes))
        step.checks.append(CheckResult(
            name="dictionnaire", ok=True,
            detail=f"{dictionnaire.variants_count(searched_terms)} variante(s) ajoutée(s) "
                   f"à {touches} terme(s)"))
    return result, step


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
                           dictionnaire: Dictionnaire | None = None,
                           candidats_out: list[str] | None = None,
                           ) -> tuple[RetrievalResult, StepTrace]:
    """`kinds_prioritaires` (story 1.8) : à score égal, les blocs de ces `Block.kind` passent devant.

    Il ne **filtre** pas — AC du sinistre : « cherche les blocs `garantie|exclusion|condition|
    franchise` candidats », pas « ne cherche qu'eux ». Le typage étant manuel à J+1 et ne couvrant que
    quatre blocs du contrat, le rappel du sinistre repose encore surtout sur les termes ; c'est le
    typage automatique (story 3.2) qui donnera son plein effet à ce départage. `None` (le guide) laisse
    l'ordre de recherche exactement tel qu'il était.

    `parsed.scope.noeuds` (story 2.3, canal corrigé par la revue Codex 2.3, B1) : les nœuds que le
    **profil** désigne. Ils arrivent **dans `ParsedQuestion`**, construits par *comprendre* à partir
    du profil et de `Document.parcours` (`domain/profil.py::noeuds_du_profil`, code pur) — l'AC dit
    « *comprendre* construit `ParsedQuestion.scope` … et *retrouver* priorise **ces** nœuds », et
    AD-1 dit « *retrouver* ne voit que `ParsedQuestion` ». L'étape ne voit donc ni le profil ni
    l'historique : elle lit une portée, comme elle lit déjà `scope.themes`. Un paramètre nommé
    parallèle (`noeuds_prioritaires`, sur le précédent de `kinds_prioritaires`) faisait le même
    travail en contournant le seul laissez-passer que le spine reconnaisse. Ces nœuds se voient
    **réserver** au plus `settings.profil_max_opens` places parmi les `max_opens` nœuds ouverts,
    prises aux derniers nœuds retenus.

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
    `{canonique: [variantes]}` — formes normalisées par groupe, meilleure couverture par canonique —
    donc l'élargissement conserve la déduplication par groupe et ajoute des formes à chercher pour
    les mêmes termes. Il n'est employé que si le dictionnaire est
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
    designes = list(parsed.scope.noeuds)
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
    if candidats_out is not None:
        candidats_out.extend(b for b, _ in hits if b not in candidats_out)

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
    def lire(ouverts: list[str]) -> tuple[list[str], dict[str, str], bool]:
        """Ouvre ces nœuds, suit renvois et définitions, applique le budget de blocs/tokens.

        Rend `(ordre des blocs transmis, nœud d'origine de chaque bloc de fenêtre, troncature)`.
        C'est une fonction de sa seule liste de nœuds : rien n'est consommé, rien n'est mémorisé, et
        elle peut donc être **rejouée** sur une autre liste — ce dont la restitution des places
        réservées a besoin (revue Codex 2.3, I1). Elle est appelée une fois sur le chemin nominal.
        """
        tronque = False
        fenetres: list[str] = []
        # De quel nœud vient chaque bloc de fenêtre : c'est ce qui permet de dire, **après** le
        # budget, quels nœuds ont réellement contribué aux blocs transmis (revue coordonnée 2.3, A1).
        noeud_de: dict[str, str] = {}
        for node_id in ouverts:
            window = index.ouvrir_noeud(node_id, focus_block_id=best_hit[node_id],
                                        node_window=budget.node_window)
            if window.truncated:
                tronque = True  # pas de pagination en déterministe : la fenêtre reste coupée
            for b in window.blocks:
                if b.block_id not in fenetres:
                    fenetres.append(b.block_id)
                    noeud_de[b.block_id] = node_id

        # Unités de dépendance, hors quota `max_opens` : un bloc de fenêtre et, avec lui, les cibles
        # d'un seul niveau de ses renvois (les cibles ne sont pas suivies à leur tour — Deferred du
        # spine « renvois en chaîne »). Une cible déjà présente dans une fenêtre reste à sa place.
        unites: list[list[str]] = []
        for block_id in fenetres:
            unite = [block_id]
            for cible in bloc(block_id).refs:
                if cible not in fenetres and cible not in unite:
                    unite.append(cible)
            unites.append(unite)

        # Définitions (hors quota `max_opens`) : des termes de la question et de ceux rencontrés dans
        # les blocs ouverts, résolues dans leur portée par l'index (AD-1, AD-2). Elles se suffisent à
        # elles-mêmes — aucun référent à conserver — et passent donc en premier dans le budget.
        definitions = [b for b, _ in index.definitions(terms, doc_id=doc_id,
                                                       blocs_ouverts=fenetres)
                       if b not in fenetres]
        unites = [[d] for d in definitions] + unites

        seen: set[str] = set()
        blocs_utilises, tokens_utilises = 0, 0
        for unite in unites:
            nouveaux = [b for b in unite if b not in seen]
            cout_tokens = sum(estimate_tokens(f"{b}\n{bloc(b).text}", settings) for b in nouveaux)
            if budget.max_blocks is not None and blocs_utilises + len(nouveaux) > budget.max_blocks:
                tronque = True
                continue  # unité sautée : les suivantes, plus petites, peuvent encore tenir
            if budget.max_tokens is not None and tokens_utilises + cout_tokens > budget.max_tokens:
                tronque = True
                continue
            blocs_utilises += len(nouveaux)
            tokens_utilises += cout_tokens
            seen.update(nouveaux)

        # Ordre rendu au modèle : les fenêtres dans l'ordre de lecture, puis les cibles de renvoi,
        # puis les définitions — l'ordre d'admission dans le budget n'est pas l'ordre de lecture.
        ordre: list[str] = []
        for b in (*fenetres, *(c for u in unites for c in u[1:]), *definitions):
            if b in seen and b not in ordre:
                ordre.append(b)
        return ordre, noeud_de, tronque

    ouverts, (promus, cedes) = _reserver(nodes, designes, budget.max_opens, budget.profil_max_opens)
    ordre, noeud_de, tronque = lire(ouverts)
    # **Réserver une place n'est pas l'occuper, et une place réservée pour rien doit être rendue**
    # (revue Codex 2.3, I1). L'unité de dépendance d'un nœud promu est soumise au budget de
    # blocs/tokens comme n'importe quelle autre : elle peut être écartée en entier. Le nœud qu'il
    # avait évincé, lui, était perdu pour de bon — le profil **retirait** alors un nœud à la question
    # sans rien lui rendre, ce que « le profil ordonne, il n'ajoute jamais » n'autorise pas plus que
    # l'inverse. Un promu qui n'a fait entrer aucun bloc rend donc sa place à celui qui la lui avait
    # cédée, et la lecture est refaite. Chaque tour retire au moins un promu : la boucle s'arrête en
    # au plus `profil_max_opens` tours, et elle ne tourne pas du tout sur le chemin nominal.
    restaures: list[str] = []   # nœuds de la question remis à leur place
    abandonnes: list[str] = []  # nœuds du profil dont la promotion n'a rien apporté
    while promus:
        contributeurs = {noeud_de[b] for b in ordre if b in noeud_de}
        perdus = [n for n in promus if n not in contributeurs]
        if not perdus:
            break
        paires = list(zip(promus, cedes, strict=True))
        abandonnes += perdus
        restaures += [c for p, c in paires if p in perdus]
        promus = [p for p, _ in paires if p not in perdus]
        cedes = [c for p, c in paires if p not in perdus]
        ouverts = [n for n in nodes[:budget.max_opens] if n not in set(cedes)] + promus
        ordre, noeud_de, tronque = lire(ouverts)
    truncated = truncated or tronque
    blocs = [bloc(b) for b in ordre]

    opened = [b.block_id for b in blocs]
    # AD-10, littéralement : « candidats de `chercher` non ouverts » — donc les hits qui ne sont pas
    # transmis au modèle, et rien d'autre. Un bloc voisin écarté par le budget n'est pas un candidat
    # de recherche : c'est `truncated` qui porte cette information (revue Codex 1.4, B5).
    discarded = [b for b, _ in hits if b not in set(ordre)]
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
        if not promus and not abandonnes:
            detail, ok = ("aucune place réservée : les nœuds désignés par le profil sont déjà "
                          "retenus, ou sans hit pour les termes cherchés"), True
        else:
            # `promus` ne contient plus, à ce point, que les promotions qui ont **fait entrer un
            # bloc** : la boucle ci-dessus a rendu les autres. Le compte dit donc ce que le profil a
            # obtenu, et non ce qu'il avait demandé.
            morceaux = [f"{len(promus)} place(s) réservée(s) sur {budget.profil_max_opens} "
                        f"({', '.join(promus) or 'aucune'}) ; "
                        f"{len(cedes)} nœud(s) cédé(s) ({', '.join(cedes) or 'aucun'})"]
            if abandonnes:
                # Rien n'est perdu pour la question — la place a été rendue —, mais rien n'est gagné
                # non plus : le seuil ou le budget sont mal réglés, et un `ok=True` le tairait.
                morceaux.append(f"{len(abandonnes)} promu(s) sans bloc retenu "
                                f"({', '.join(abandonnes)}) : le budget de blocs a écarté leur "
                                f"fenêtre, leur place a été rendue ({', '.join(restaures)})")
            detail, ok = " ; ".join(morceaux), not abandonnes
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
