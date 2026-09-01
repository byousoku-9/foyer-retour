"""AD-1 — les deux implémentations de *retrouver*, sous le même contrat et le même budget.

La variante `deterministe` (J+1) reste du code pur. La variante `outils` (story 2.6) laisse le tier
configuré parcourir le sommaire avec exactement quatre outils, en deux tours au plus ; elle ne change
ni la chaîne du pipeline, ni `RetrievalResult`, ni la vérification aval. Le premier tour utile suffit
dès qu'il a admis un bloc autre qu'un titre ou une définition sans laisser de pagination ouverte,
sauf si son appelant a déclaré des kinds suffisants : un bloc de l'un de ces kinds doit alors être
confirmé par le corpus. Cette exigence ne classe rien et n'infère aucune applicabilité.

Dans cette variante, après les ouvertures du navigateur et à la frontière de la phase `outils`, les
réservations de facettes encore absentes sont complétées depuis le classement unique de
`groupes_prioritaires`. Elles passent par les mêmes fenêtres, unités atomiques, quotas et budgets,
sans appel modèle supplémentaire. Les ouvertures du navigateur gardent la priorité ; dans une
fenêtre focalisée seulement, le focus réservé qui est aussi le meilleur hit effectif du nœud passe
avant ses voisins selon la même règle que dans le déterministe. Cette priorité décide uniquement
quelles unités tiennent dans le budget : les primaires transmis retrouvent ensuite l'ordre
documentaire de la fenêtre, avec leurs compagnons et leurs dépendances admises, sans double comptage.

Un candidat de `chercher` que le navigateur choisit de ne pas ouvrir reste dans
`discarded_block_ids` mais ne rend pas `truncated=True` : il n'a jamais été lu, sauf si la complétion
ci-dessus le transmet finalement. Un check `candidats_non_ouverts` en publie le compte afin que les
évals distinguent ce choix d'une fenêtre lue puis bornée. Le déterministe, lui, marque la borne quand
des nœuds candidats dépassent son quota.

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
ce qu'AD-10 en dit : les candidats de `chercher` finalement non transmis, complétion comprise.

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
from server.app.corpus.index import Index, reading_order
from server.app.corpus.loader import Corpus
from server.app.domain import Block, FullContextSelection, RetrievalBudget, RetrievalResult, is_citable
from server.app.domain.answer import DemandeContexte
from server.app.domain.errors import BudgetExceeded, LlmParse, PipelineError
from server.app.domain.question import ParsedQuestion
from server.app.domain.trace import CheckResult, StepTrace
from server.app.llm.client import structured_input_envelope
from server.app.llm.models import MODEL_CAPS, STEP_TIERS, model_for
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


_KINDS_LIMITATIFS = frozenset({"exclusion", "condition", "franchise"})
_KINDS_CONTEXTUELS = frozenset({"heading", "definition"})
_MOTS_OUTILS_LIMITES = frozenset({
    "a", "au", "aux", "avec", "ce", "ces", "dans", "de", "des", "du", "elle", "en", "est",
    "et", "il", "ils", "la", "le", "les", "leur", "leurs", "lui", "ne", "ni", "on", "ou",
    "par", "pas", "pour", "que", "qui", "sa", "se", "ses", "son", "sur", "un", "une",
})


def _noeuds_des_blocs(block_ids: list[str], *, corpus: Corpus, index: Index) -> list[str]:
    """Story 4.2f — les nœuds distincts d'où viennent ces blocs, dans l'ordre de première apparition.

    AD-2 le rend total : « chaque bloc rattaché à exactement un nœud », vérifié par les invariants
    d'arbre au chargement du modèle. Il n'y a donc rien à deviner, et aucun bloc transmis ne peut
    rester sans section d'origine — c'est ce qui empêche le compteur publié de contredire celui des
    blocs.

    Partagé par les deux variantes : deux calculs auraient divergé au premier amendement, et c'est
    un chiffre que l'utilisateur lit.
    """
    return list(dict.fromkeys(
        corpus.documents[index.doc_of(b)].node_of(b) for b in block_ids))


def _dependances_directes(block_id: str, *, block: Any, index: Index, terms: list[str],
                          doc_id: str | None, search_candidates: Iterable[str],
                          related_limit: int, related_max: int, proximity_min: float,
                          related_cache: dict[str, list[str]], search_related: bool) -> list[str]:
    """Fermeture commune aux deux variantes : refs, définitions et limites classées, un niveau.

    Les cibles sont calculées uniquement depuis le bloc primaire. Leurs propres `refs` ne sont donc
    jamais parcourus. Une limite décisionnelle déjà classée parmi les hits accompagne une garantie
    ouverte : elle ne dépend d'aucun identifiant documentaire et reste soumise à la même unité
    atomique et aux mêmes budgets globaux.
    """
    out: list[str] = []
    current_doc_id = doc_id or index.doc_of(block_id)

    def add(candidate: str) -> None:
        # Une cible peut aussi être un primaire de la même fenêtre. Elle garde alors son unité
        # propre, mais doit également voyager dans l'unité de la source : autrement la source
        # pourrait être admise seule sous un petit budget, ce qui briserait l'atomicité du renvoi.
        if candidate != block_id and candidate not in out:
            out.append(candidate)

    for candidate in block(block_id).refs:
        for membre in index.unite_de_renvoi(candidate):
            add(membre)
    for candidate, _node_id in index.definitions(terms, doc_id=current_doc_id,
                                                  blocs_ouverts=[block_id]):
        add(candidate)
    current = block(block_id)
    if current.kind == "garantie" and related_max:
        cohort = list(search_candidates)
        # Une limite peut reprendre presque mot pour mot la règle d'une garantie sans partager le
        # vocabulaire factuel de la question. Une recherche déterministe depuis la clause ouverte
        # complète donc le cohort de cette ouverture ; elle reste bornée comme la recherche initiale
        # et ne réutilise aucun résultat d'un autre tour de navigation.
        if search_related and block_id not in related_cache:
            related_cache[block_id] = [candidate for candidate, _node_id in index.chercher(
                forme(current.text).split(), limit=related_limit, doc_id=current_doc_id,
                kinds_prioritaires=_KINDS_LIMITATIFS)]
        for candidate in related_cache.get(block_id, []):
            if candidate not in cohort:
                cohort.append(candidate)
        limites = [candidate for candidate in cohort
                   if candidate != block_id and block(candidate).kind in _KINDS_LIMITATIFS]
        if limites:
            document = index.corpus.documents[current_doc_id]
            mots_source = {mot for mot in forme(current.text).split()
                           if mot.isalpha() and mot not in _MOTS_OUTILS_LIMITES}

            def preuve_et_proximite(candidate: str) -> tuple[bool, float, int]:
                limite = block(candidate)
                relations = {
                    current.relation.exception_de, current.relation.specialise,
                    limite.relation.exception_de, limite.relation.specialise,
                }
                liee = (candidate in current.refs or block_id in limite.refs
                        or candidate in relations or block_id in relations
                        or document.node_of(block_id) in document.scope_nodes(candidate))
                mots_candidat = {mot for mot in forme(limite.text).split()
                                 if mot.isalpha() and mot not in _MOTS_OUTILS_LIMITES}
                union = mots_source | mots_candidat
                proximite = len(mots_source & mots_candidat) / len(union) if union else 0.0
                return liee, proximite, -limites.index(candidate)

            prouvees = [(candidate, preuve_et_proximite(candidate)) for candidate in limites]
            prouvees = [(candidate, preuve) for candidate, preuve in prouvees
                         if preuve[0] or preuve[1] >= proximity_min]
            prouvees.sort(key=lambda item: item[1], reverse=True)
            for candidate, _preuve in prouvees[:related_max]:
                add(candidate)
            # Le classement de la question forme le cohort de limites candidates. Seules celles
            # dont le corpus prouve le lien, ou dont la proximité dépasse le seuil configuré,
            # accompagnent la garantie ; le plafond reste lui aussi une hypothèse de configuration.
    return out


def _unite_primaire(block_id: str, *, kind: str, index: Index,
                     dependances: Iterable[str]) -> list[str] | None:
    """Unité atomique commune : primaire structurel, puis dépendances directes.

    `Index.unite_de_renvoi` est l'autorité structurelle : un titre emporte son premier corps
    non-titre, tandis qu'un primaire ordinaire reste seul. Les dépendances sont celles du bloc
    demandé seulement ; le corps ajouté n'est jamais parcouru récursivement. Un titre sans corps
    non-titre ne forme aucune unité transmissible : `None` ordonne à l'appelant de le refuser et de
    publier la troncature.
    """
    structure = index.unite_de_renvoi(block_id)
    if kind == "heading" and structure == [block_id]:
        return None
    return list(dict.fromkeys((*structure, *dependances)))


def _prioriser_focus(block_ids: Iterable[str], focus_id: str | None, *, reserve: bool) -> list[str]:
    """Ordre d'essai partagé : un focus réellement réservé passe avant ses frères.

    Cet ordre sert uniquement à l'admission sous budget. Les appelants conservent séparément
    l'ordre documentaire nécessaire au rendu. Un focus non réservé reste dans cet ordre.
    """
    ids = list(block_ids)
    if not reserve or focus_id is None or focus_id not in ids:
        return ids
    return [focus_id, *(block_id for block_id in ids if block_id != focus_id)]


def _focus_est_reserve(block_id: str | None, node_id: str, *,
                       reservations: Iterable[tuple[str, str]],
                       best_hit_by_node: dict[str, str]) -> bool:
    """Autorité commune : réservation survivante et meilleur hit effectif du nœud."""
    return (block_id is not None
            and best_hit_by_node.get(node_id) == block_id
            and (block_id, node_id) in reservations)


def _ajouter_best_hits_faq(node_ids: Iterable[str], *, index: Index,
                           best_hit_by_node: dict[str, str]) -> list[str]:
    """Inscrit les premiers blocs FAQ selon la règle first-wins commune aux variantes."""
    added: list[str] = []
    for node_id in node_ids:
        if node_id in best_hit_by_node:
            continue
        try:
            window = index.ouvrir_noeud(node_id, node_window=1)
        except (KeyError, ValueError):
            continue
        if window.blocks:
            best_hit_by_node[node_id] = window.blocks[0].block_id
            added.append(node_id)
    return added


async def retrouver_outils(parsed: ParsedQuestion, *, corpus: Corpus, index: Index,
                            budget: RetrievalBudget, settings: Settings, client: Any,
                            request_budget: Any, doc_id: str,
                            dictionnaire: Dictionnaire | None = None,
                            candidats_out: list[str] | None = None,
                            kinds_suffisants: frozenset[str] | None = None,
                            ) -> tuple[RetrievalResult, StepTrace]:
    """Navigation bornée, avec une suffisance optionnelle fondée sur les kinds confirmés du corpus."""
    t0 = time.monotonic()
    # L'amendement 2.6 autorise explicitement l'arbitrage du tier de navigation. Le déterministe
    # conserve l'affectation historique `reason`; la variante appelée publie son tier réel.
    mechanisms = settings.retrieval_mechanisms()
    step = StepTrace(name="retrouver", tier=settings.retrouver_outils_tier,
                     prompt_cache=settings.retrieval_prompt_cache,
                     mechanism_order=list(mechanisms))
    if doc_id not in corpus.documents:
        raise KeyError(doc_id)
    document = corpus.documents[doc_id]
    terms = parsed.termes_de_recherche()
    elargi = dictionnaire is not None and dictionnaire.utilisable_pour(doc_id)
    dictionary_ready = False
    faq_candidates = (dictionnaire.faq_candidates(parsed.question_resolue, doc_id=doc_id)
                      if dictionnaire is not None else [])
    summary_ready = False

    def navigation_prompt() -> str:
        return render_prompt(
            "retrouver", doc_id=doc_id, max_llm_turns=budget.max_llm_turns,
            max_opens=budget.max_opens, profil_max_opens=budget.profil_max_opens,
            sommaire=untrusted(
                "sommaire", index.sommaire(doc_id) if summary_ready else ""))
    question = {
        "question_resolue": parsed.question_resolue,
        "termes": terms,
        "facettes": parsed.facettes,
        "scope": parsed.scope.model_dump(mode="json"),
    }
    messages: list[dict[str, Any]] = [{
        "role": "user",
        "content": untrusted("question_resolue", json.dumps(question, ensure_ascii=False)),
    }]

    admitted: list[str] = []
    admitted_set: set[str] = set()
    window_opened: list[str] = []
    primary_node_by_block: dict[str, str] = {}
    search_candidates: list[str] = []
    tool_search_candidates: list[str] = []
    search_runs: list[list[str]] = []
    reserved_candidates: list[tuple[str, str]] = []
    best_hit_by_node: dict[str, str] = {}
    valid_window_attempted = False
    focused_windows_attempted: set[str] = set()
    related_cache: dict[str, list[str]] = {}
    decision_dependencies: list[str] = []
    searched_terms: list[str] = []
    dictionary_searched_terms: list[str] = []
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

    def execute(name: str, args: object, *, mechanism: bool = False) -> tuple[dict[str, Any], bool]:
        nonlocal opens, truncated, valid_window_attempted
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
            if dictionary_ready:
                mapping = dictionnaire.expand(termes)
            for terme in termes:
                if forme(terme) not in {forme(t) for t in searched_terms}:
                    searched_terms.append(terme)
                if (dictionary_ready
                        and forme(terme) not in {forme(t) for t in dictionary_searched_terms}):
                    dictionary_searched_terms.append(terme)
            run_reservations: list[tuple[str, str]] = []
            hits = index.chercher(mapping, limit=budget.search_limit + 1, doc_id=doc_id,
                                  groupes_prioritaires=parsed.facettes,
                                  reservations_out=run_reservations)
            search_truncated = len(hits) > budget.search_limit
            if search_truncated:
                truncated = True
                hits = hits[:budget.search_limit]
            for reservation in run_reservations:
                if reservation in hits and reservation not in reserved_candidates:
                    reserved_candidates.append(reservation)
            for block_id, hit_node_id in hits:
                best_hit_by_node.setdefault(hit_node_id, block_id)
                if block_id not in search_candidates:
                    search_candidates.append(block_id)
                if not mechanism and block_id not in tool_search_candidates:
                    tool_search_candidates.append(block_id)
            search_runs.append([block_id for block_id, _node_id in hits])
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
            valid_window_attempted = True
            if focus is not None:
                focused_windows_attempted.add(focus)
            # Une pagination n'est résolue que si elle part du début puis suit chaque curseur.
            expected = pagination_expected.get(node_id, 0)
            follows = focus is None and (cursor or 0) == expected
            if window.next_cursor is not None:
                pagination_expected[node_id] = window.next_cursor if follows else -1
            elif follows:
                pagination_expected.pop(node_id, None)
            elif window.truncated:
                pagination_expected[node_id] = -1

            anchors = {focus} if focus is not None else {b.block_id for b in window.blocks}
            relevant_candidates: list[str] = []
            for run in search_runs:
                if anchors.intersection(run):
                    relevant_candidates.extend(
                        candidate for candidate in run if candidate not in relevant_candidates)
            primary: list[str] = []
            newly: list[str] = []
            admitted_before_window = set(admitted_set)
            promoted_dependency = False
            window_blocks = list(window.blocks)
            window_ids = [item.block_id for item in window_blocks]
            window_block_by_id = {item.block_id: item for item in window_blocks}
            focus_companions = (set(index.unite_de_renvoi(focus)[1:])
                                if focus is not None else set())
            primary_ids = [
                item.block_id for item in window_blocks
                if not (item.block_id in focus_companions
                        and item.block_id not in relevant_candidates)
            ]
            focus_reserve = _focus_est_reserve(
                focus, node_id, reservations=reserved_candidates,
                best_hit_by_node=best_hit_by_node)
            admission_ids = _prioriser_focus(
                primary_ids, focus, reserve=focus_reserve)
            for primary_id in admission_ids:
                item = window_block_by_id[primary_id]
                # Une définition applicable éclaire le bloc primaire au même titre que son renvoi :
                # l'unité entière entre, ou le primaire n'est pas transmis isolément.
                dependencies = _dependances_directes(
                    item.block_id, block=block, index=index, terms=terms, doc_id=doc_id,
                    search_candidates=relevant_candidates, related_limit=budget.search_limit,
                    related_max=settings.limite_liee_max,
                    proximity_min=settings.limite_liee_proximite_min,
                    related_cache=related_cache,
                    search_related=item.block_id == focus or item.block_id in relevant_candidates,
                )
                unit = (_unite_primaire(
                    item.block_id, kind=item.kind, index=index, dependances=dependencies)
                    if item.block_id == focus else [item.block_id, *dependencies])
                if unit is None:
                    truncated = True
                    continue
                got = admit(unit)
                if item.block_id in got:
                    primary.append(item.block_id)
                if item.block_id in admitted_set:
                    if item.block_id not in window_opened:
                        window_opened.append(item.block_id)
                    if (item.block_id in admitted_before_window
                            and item.block_id not in primary_node_by_block):
                        promoted_dependency = True
                    primary_node_by_block[item.block_id] = node_id
                    if item.kind in {"garantie", "exclusion"}:
                        decision_dependencies.extend(
                            candidate for candidate in dependencies
                            if candidate in admitted_set and candidate not in decision_dependencies)
                newly.extend(got)
            if newly:
                # `admit()` a déjà pris sa décision dans l'ordre prioritaire. Pour le rendu, les
                # membres présents dans la fenêtre retrouvent l'ordre documentaire ; les autres
                # dépendances admises restent ensuite dans leur ordre atomique, sans être recomptées.
                newly_set = set(newly)
                window_new = [block_id for block_id in window_ids if block_id in newly_set]
                window_new_set = set(window_new)
                newly = [*window_new,
                         *(block_id for block_id in newly if block_id not in window_new_set)]
                admitted[-len(newly):] = newly
            if promoted_dependency:
                # Un bloc admis plus tôt comme dépendance peut devenir le primaire d'une fenêtre
                # ultérieure. Il quitte alors sa position de dépendance et rejoint, sans nouveau
                # coût, les autres membres admis de cette fenêtre dans leur ordre documentaire.
                window_admitted = [
                    block_id for block_id in window_ids if block_id in admitted_set]
                window_admitted_set = set(window_admitted)
                external_new = [
                    block_id for block_id in newly if block_id not in window_admitted_set]
                moved = window_admitted_set | set(external_new)
                admitted[:] = [block_id for block_id in admitted if block_id not in moved]
                admitted.extend((*window_admitted, *external_new))
            primary_set = set(primary)
            primary = [block_id for block_id in window_ids if block_id in primary_set]
            dependances_rendues = [b for b in newly if b not in primary]
            return {
                "node_id": window.node_id, "title": window.title,
                "children": [c.model_dump(mode="json") for c in window.children],
                "blocks": rendered(primary), "dependencies": rendered(dependances_rendues),
                "truncated": window.truncated or any(b.block_id not in admitted_set for b in window.blocks),
                "next_cursor": window.next_cursor,
            }, False
        if name == "definitions":
            allowed = {"termes", "blocs_ouverts"}
            termes = _strings(args.get("termes"))
            ouverts = _strings(args.get("blocs_ouverts", list(window_opened)))
            if not set(args) <= allowed or termes is None or ouverts is None:
                return invalid()
            # AD-1 : dès qu'un bloc est ouvert, sa portée s'impose. Une liste explicite vide ne
            # peut donc pas recréer artificiellement le cas initial « aucun bloc ouvert », où
            # aucune portée n'est invalidée.
            if not ouverts and window_opened:
                ouverts = list(window_opened)
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

    def complete_reservations() -> None:
        # Une fenêtre valide suffit à établir que le navigateur a commencé sa lecture, même si son
        # unité atomique n'a pas tenu. Ne pas retenter un focus déjà essayé laisse le budget
        # restant aux autres facettes. L'origine de l'ouverture ne change pas la priorité : tout
        # focus réservé qui est le meilleur hit effectif du nœud passe avant ses frères.
        if not valid_window_attempted:
            return
        for block_id, node_id in reserved_candidates:
            if block_id not in admitted_set and block_id not in focused_windows_attempted:
                execute("ouvrir_noeud", {"node_id": node_id, "focus_block_id": block_id})

    def suffisance_atteinte() -> bool:
        if kinds_suffisants is not None:
            return any(
                block(block_id).kind in kinds_suffisants and block(block_id).kind_confirmed
                for block_id in admitted)
        return any(block(block_id).kind not in _KINDS_CONTEXTUELS for block_id in admitted)

    def complete_search_candidates_for_sufficiency() -> None:
        # Le navigateur peut honnêtement conclure après l'ouverture qu'impose son prompt, même si
        # celle-ci n'a pas encore satisfait le besoin déclaré par l'appelant. Ses propres hits
        # restent alors le seul classement autorisé : on les essaie dans leur ordre initial, après
        # les réservations et via l'outil commun, afin de conserver fenêtres, dépendances,
        # atomicité et budgets sans capacité cachée. Sans besoin déclaré, le comportement
        # historique reste strictement limité à une lecture exclusivement contextuelle.
        if kinds_suffisants is None:
            completion_necessaire = bool(admitted) and all(
                block(block_id).kind in _KINDS_CONTEXTUELS for block_id in admitted)
        else:
            completion_necessaire = not suffisance_atteinte()
        if not completion_necessaire:
            return
        for block_id in tool_search_candidates:
            if block_id in admitted_set or block_id in focused_windows_attempted:
                continue
            node_id = document.node_of(block_id)
            _payload, is_error = execute(
                "ouvrir_noeud", {"node_id": node_id, "focus_block_id": block_id})
            if is_error or suffisance_atteinte():
                break

    used_tools = False

    async def navigate() -> None:
        nonlocal truncated, used_tools
        # Les candidats FAQ ne deviennent visibles au navigateur qu'une fois le mécanisme FAQ
        # franchi. Cela rend notamment `outils → FAQ` distinct de `FAQ → outils`.
        if faq_candidates and "faq_candidates" in question:
            messages[0] = {
                "role": "user",
                "content": untrusted(
                    "question_resolue", json.dumps(question, ensure_ascii=False)),
            }
        prompt = navigation_prompt()
        for turn in range(budget.max_llm_turns):
            try:
                result = await client.tool_turn(
                    tier=settings.retrouver_outils_tier, system_prefix=prompt,
                    messages=messages, tools=OUTILS_RECHERCHE,
                    budget=request_budget, step=step,
                    max_tokens=settings.retrouver_outils_max_tokens,
                    prompt_cache=settings.retrieval_prompt_cache)
            except PipelineError as exc:
                # Comme les autres étapes LLM : l'appel éventuellement commencé et son coût doivent
                # survivre dans la trace partielle de l'erreur terminale.
                step.ms = int((time.monotonic() - t0) * 1000)
                exc.step = step
                raise
            if result.message.stop_reason in {"max_tokens", "refusal", "pause_turn"}:
                truncated = True
            tool_uses = [
                b for b in result.message.content if getattr(b, "type", None) == "tool_use"
            ]
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
                content = untrusted(
                    "resultat_outil", json.dumps(payload, ensure_ascii=False, sort_keys=True))
                item: dict[str, Any] = {
                    "type": "tool_result", "tool_use_id": str(use.id), "content": content,
                }
                if is_error:
                    item["is_error"] = True
                tool_results.append(item)
            # Sans besoin déclaré, un titre ou une définition éclaire les candidats sans fournir
            # encore la règle utile et tout autre bloc conserve l'arrêt froid historique. Lorsqu'un
            # appelant déclare des kinds suffisants, seuls un kind demandé **et confirmé** satisfait
            # cet arrêt. La pagination garde dans les deux cas sa propre priorité.
            if turn == 0 and suffisance_atteinte() and not pagination_expected:
                break
            if turn + 1 < budget.max_llm_turns:
                messages.extend([
                    {"role": "assistant", "content": _content_json(result.message)},
                    {"role": "user", "content": tool_results},
                ])

    # Les mécanismes sont des phases, et non un tri global des hits. Chaque phase conserve l'ordre
    # interne produit par l'index ou le dictionnaire ; seule leur concaténation suit la configuration.
    for mechanism in mechanisms:
        if mechanism == "dictionnaire":
            dictionary_ready = elargi
        elif mechanism == "faq":
            _ajouter_best_hits_faq(
                faq_candidates, index=index, best_hit_by_node=best_hit_by_node)
            if faq_candidates:
                question["faq_candidates"] = list(faq_candidates)
        elif mechanism == "sommaire":
            summary_ready = True
            # Ajoute les candidats lexicaux dans leur ordre de score sans les ouvrir à la place du
            # navigateur. Si le dictionnaire a déjà préparé les formes, la recherche les emploie.
            if terms:
                execute("chercher", {"termes": terms}, mechanism=True)
        elif mechanism == "outils":
            await navigate()
            # Le navigateur conserve la priorité : ses fenêtres sont admises en premier. La
            # complétion ne voit que les recherches des phases antérieures et de ce tour outils ;
            # un sommaire placé après ne peut donc jamais ouvrir rétroactivement un candidat caché.
            complete_reservations()
            complete_search_candidates_for_sufficiency()
    expected_search = canonical_forms(terms)
    covered_search = canonical_forms(searched_terms)
    # Un refus `zero_hit` n'est honnête que si au moins un terme canonique existait et si les
    # recherches réellement exécutées les ont tous couverts. Une recherche vide, inventée ou
    # partielle ne devient jamais une preuve d'absence.
    absence_proven = bool(expected_search) and expected_search <= covered_search
    if (kinds_suffisants is not None and not suffisance_atteinte()
            and (any(b not in admitted_set for b in search_candidates) or not absence_proven)):
        truncated = True
    if (not used_tools and not admitted) or (
            not admitted and (search_candidates or not absence_proven)):
        truncated = True
    if pagination_expected:
        truncated = True

    # AD-10 réserve cette liste aux candidats explicitement rendus par `chercher` au navigateur.
    # Les candidats préparés par la phase sommaire restent disponibles pour la navigation, mais ne
    # sont pas réétiquetés a posteriori comme un choix du modèle de ne pas les ouvrir.
    discarded = [b for b in tool_search_candidates if b not in admitted_set]
    if candidats_out is not None:
        candidats_out.extend(b for b in search_candidates if b not in candidats_out)
    result = RetrievalResult(
        blocs=[block(b) for b in admitted], opened_block_ids=list(admitted),
        # Story 4.2f : les nœuds d'où viennent les blocs **transmis**, tous, y compris ceux entrés
        # par `definitions` ou comme dépendance directe — `primary_node_by_block` n'en connaît que
        # les blocs de fenêtre, et s'y limiter laissait la variante servie annoncer « 0 section lue,
        # N passages transmis ».
        opened_node_ids=_noeuds_des_blocs(admitted, corpus=corpus, index=index),
        decision_dependency_block_ids=[b for b in decision_dependencies if b in admitted_set],
        discarded_block_ids=discarded, truncated=truncated)
    step.ms = int((time.monotonic() - t0) * 1000)
    step.opened_block_ids = list(admitted)
    step.discarded_block_ids = list(discarded)
    designes = list(parsed.scope.noeuds)
    if designes and budget.profil_max_opens > 0:
        designes_set = set(designes)
        contributeurs = list(dict.fromkeys(
            primary_node_by_block[b] for b in admitted
            if b in primary_node_by_block and primary_node_by_block[b] in designes_set
        ))
        absents = [node_id for node_id in designes if node_id not in contributeurs]
        detail = (f"{len(contributeurs)} nœud(s) désigné(s) par le profil ont contribué aux blocs "
                  f"transmis sur {budget.profil_max_opens} place(s) prioritaire(s) "
                  f"({', '.join(contributeurs) or 'aucun'})")
        if absents:
            detail += f" ; sans bloc retenu : {', '.join(absents)}"
        step.checks.append(CheckResult(
            name="noeuds_du_profil", ok=bool(contributeurs), detail=detail))
    if discarded:
        step.checks.append(CheckResult(
            name="candidats_non_ouverts", ok=False,
            detail=f"{len(discarded)} candidat(s) de chercher non lu(s) par le navigateur ; "
                   "choix de navigation distinct d'une troncature"))
    if faq_candidates:
        step.checks.append(CheckResult(
            name="faq", ok=True,
            detail=f"{len(faq_candidates)} nœud(s) candidat(s) traité(s) selon l'ordre configuré"))
    if dictionary_searched_terms:
        searched_expanded = dictionnaire.expand(dictionary_searched_terms)
        base = {forme(t) for t in dictionary_searched_terms} - {""}
        touches = sum(1 for variantes in searched_expanded.values()
                      if any(v and v not in base for v in variantes))
        step.checks.append(CheckResult(
            name="dictionnaire", ok=True,
            detail=f"{dictionnaire.variants_count(dictionary_searched_terms)} variante(s) ajoutée(s) "
                   f"à {touches} terme(s)"))
    return result, step


async def retrouver_full_context(parsed: ParsedQuestion, *, corpus: Corpus, index: Index,
                                 budget: RetrievalBudget, settings: Settings, client: Any,
                                 request_budget: Any, doc_id: str,
                                 dictionnaire: Dictionnaire | None = None,
                                 candidats_out: list[str] | None = None,
                                 ) -> tuple[RetrievalResult, StepTrace]:
    """Sélection LLM sur le sommaire et tous les passages citables du guide.

    Tout le corpus statique vit dans le préfixe cacheable. Le message utilisateur ne transporte
    que la question résolue, ses termes et sa portée. L'enveloppe froide complète est refusée avant
    le client si elle ne tient pas dans la fenêtre réelle du modèle choisi.
    """
    t0 = time.monotonic()
    step = StepTrace(name="retrouver", tier=settings.retrouver_outils_tier,
                     prompt_cache=settings.retrieval_prompt_cache,
                     mechanism_order=list(settings.retrieval_mechanisms()))

    def failure(exc: PipelineError) -> PipelineError:
        """Tout refus de la variante conserve la trace de l'étape qui l'a produit."""
        step.ms = int((time.monotonic() - t0) * 1000)
        exc.step = step
        return exc

    if doc_id not in corpus.documents:
        raise KeyError(doc_id)
    document = corpus.documents[doc_id]
    ordered = [document.block(block_id) for block_id, _node_id in reading_order(document)
               if is_citable(document.block(block_id))]
    serialized = "\n\n".join(
        json.dumps({"block_id": block.block_id, "text": block.text}, ensure_ascii=False,
                   sort_keys=True) for block in ordered)
    prompt = render_prompt(
        "retrouver_full_context", doc_id=doc_id,
        sommaire=untrusted("sommaire", index.sommaire(doc_id)),
        passages=untrusted("passages", serialized))
    question = {
        "question_resolue": parsed.question_resolue,
        "termes": parsed.termes_de_recherche(),
        "facettes": parsed.facettes,
        "scope": parsed.scope.model_dump(mode="json"),
    }
    messages = [{"role": "user", "content": untrusted(
        "question_resolue", json.dumps(question, ensure_ascii=False, sort_keys=True))}]
    max_tokens = settings.retrouver_outils_max_tokens
    model = model_for(settings.retrouver_outils_tier)
    # Même enveloppe structurée que `LlmClient.parse` : blocs système (et cache_control éventuel),
    # messages et schéma Anthropic transformé/output_config. Toute évolution du client modifie donc
    # le préflight au même endroit, au lieu de recréer ici une approximation plus petite.
    envelope = structured_input_envelope(
        tier=settings.retrouver_outils_tier, system_prefix=prompt, messages=messages,
        output_model=FullContextSelection, max_tokens=max_tokens,
        prompt_cache=settings.retrieval_prompt_cache)
    input_tokens = estimate_tokens(envelope, settings)
    context_window = int(MODEL_CAPS[model]["context_window"])
    if input_tokens + max_tokens > context_window:
        raise failure(BudgetExceeded(
            f"full_context hors enveloppe du modèle {model} : entrée majorée {input_tokens} "
            f"+ sortie réservée {max_tokens} > contexte {context_window}"))
    try:
        response = await client.parse(
            tier=settings.retrouver_outils_tier, system_prefix=prompt, messages=messages,
            output_model=FullContextSelection, budget=request_budget, step=step,
            max_tokens=max_tokens, prompt_cache=settings.retrieval_prompt_cache)
    except PipelineError as exc:
        raise failure(exc)
    known = {block.block_id: block for block in ordered}
    unknown = [block_id for block_id in response.parsed.block_ids if block_id not in known]
    if unknown:
        raise failure(LlmParse(
            f"full_context a rendu {len(unknown)} block_id absent(s) ou non citables"))
    requested = set(response.parsed.block_ids)
    if candidats_out is not None:
        candidats_out.extend(
            block_id for block_id in response.parsed.block_ids if block_id not in candidats_out)
    # Le modèle sélectionne un ensemble d'IDs ; il ne décide jamais de l'ordre documentaire. Les
    # objets sont résolus depuis le corpus et restent dans l'ordre de lecture canonique.
    selected_ids = [block.block_id for block in ordered if block.block_id in requested]

    def block(block_id: str) -> Block:
        if index.doc_of(block_id) != doc_id:
            raise KeyError(block_id)
        return document.block(block_id)

    # `max_opens` borne les nœuds primaires choisis par le modèle, exactement comme les fenêtres
    # des deux autres variantes. Plusieurs passages du même nœud ne consomment qu'une ouverture.
    primary_nodes: list[str] = []
    admitted_primary_ids: list[str] = []
    discarded: list[str] = []
    for block_id in selected_ids:
        node_id = document.node_of(block_id)
        if node_id not in primary_nodes:
            if len(primary_nodes) >= budget.max_opens:
                discarded.append(block_id)
                continue
            primary_nodes.append(node_id)
        if node_id in primary_nodes:
            admitted_primary_ids.append(block_id)
    allowed_nodes = set(primary_nodes)
    # Les autres passages sélectionnés appartenant à un nœud déjà écarté sont eux aussi rejetés.
    for block_id in selected_ids:
        if document.node_of(block_id) not in allowed_nodes and block_id not in discarded:
            discarded.append(block_id)

    related_cache: dict[str, list[str]] = {}
    dependencies_by_primary: dict[str, list[str]] = {}
    units: list[list[str]] = []
    for block_id in admitted_primary_ids:
        dependencies = _dependances_directes(
            block_id, block=block, index=index, terms=parsed.termes_de_recherche(),
            doc_id=doc_id, search_candidates=selected_ids,
            related_limit=budget.search_limit, related_max=settings.limite_liee_max,
            proximity_min=settings.limite_liee_proximite_min,
            related_cache=related_cache, search_related=True)
        units.append([block_id, *dependencies])
        if block(block_id).kind in {"garantie", "exclusion"}:
            dependencies_by_primary[block_id] = dependencies

    # Une unité primaire+dépendances entre entièrement ou pas du tout. Les unités ultérieures sont
    # encore essayées : une première unité trop grande ne doit pas gaspiller le budget résiduel.
    admitted: set[str] = set()
    blocks_used = 0
    tokens_used = 0
    truncated = bool(discarded)
    admitted_primaries: list[str] = []
    for unit in units:
        new: list[str] = []
        for candidate in unit:
            if candidate not in admitted and candidate not in new:
                new.append(candidate)
        token_cost = sum(estimate_tokens(f"{candidate}\n{block(candidate).text}", settings)
                         for candidate in new)
        if budget.max_blocks is not None and blocks_used + len(new) > budget.max_blocks:
            truncated = True
            if unit[0] not in admitted and unit[0] not in discarded:
                discarded.append(unit[0])
            continue
        if budget.max_tokens is not None and tokens_used + token_cost > budget.max_tokens:
            truncated = True
            if unit[0] not in admitted and unit[0] not in discarded:
                discarded.append(unit[0])
            continue
        blocks_used += len(new)
        tokens_used += token_cost
        admitted.update(new)
        admitted_primaries.append(unit[0])

    opened: list[str] = []
    for block_id in (*admitted_primaries, *(candidate for unit in units for candidate in unit[1:])):
        if block_id in admitted and block_id not in opened:
            opened.append(block_id)
    decision_dependencies: list[str] = []
    for primary, dependencies in dependencies_by_primary.items():
        if primary not in admitted:
            continue
        decision_dependencies.extend(candidate for candidate in dependencies
                                     if candidate in admitted
                                     and candidate not in decision_dependencies)
    step.ms = int((time.monotonic() - t0) * 1000)
    step.opened_block_ids = list(opened)
    step.discarded_block_ids = list(discarded)
    return RetrievalResult(
        blocs=[block(block_id) for block_id in opened], opened_block_ids=opened,
        opened_node_ids=_noeuds_des_blocs(opened, corpus=corpus, index=index),
        decision_dependency_block_ids=decision_dependencies,
        discarded_block_ids=discarded, truncated=truncated), step


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
    faq_nodes = (dictionnaire.faq_candidates(parsed.question_resolue, doc_id=doc_id)
                 if dictionnaire is not None else [])
    mechanisms = settings.retrieval_mechanisms()
    dictionary_ready = False
    expanded_search: dict[str, list[str]] | None = None
    hits: list[tuple[str, str]] = []
    # Nœuds candidats par phase puis, à l'intérieur de chaque phase, dans l'ordre propre de la
    # source. Il n'y a aucun retri global des hits documentaires.
    nodes: list[str] = []
    best_hit: dict[str, str] = {}
    reserved_candidates: list[tuple[str, str]] = []
    for mechanism in mechanisms:
        if mechanism == "dictionnaire":
            dictionary_ready = elargi
        elif mechanism == "faq":
            nodes.extend(_ajouter_best_hits_faq(
                faq_nodes, index=index, best_hit_by_node=best_hit))
        elif mechanism == "sommaire" and terms:
            if dictionary_ready:
                expanded_search = dictionnaire.expand(terms)
            cherches: dict[str, list[str]] | list[str] = expanded_search or terms
            phase_reservations: list[tuple[str, str]] = []
            phase_hits = index.chercher(
                cherches, limit=budget.search_limit, doc_id=doc_id,
                kinds_prioritaires=kinds_prioritaires,
                groupes_prioritaires=parsed.facettes,
                reservations_out=phase_reservations)
            reserved_candidates.extend(
                reservation for reservation in phase_reservations
                if reservation in phase_hits and reservation not in reserved_candidates)
            for block_id, node_id in phase_hits:
                if block_id not in {candidate for candidate, _node in hits}:
                    hits.append((block_id, node_id))
                if node_id not in best_hit:
                    best_hit[node_id] = block_id
                    nodes.append(node_id)
        # `outils` est la phase de navigation LLM de l'autre variante. Dans le déterministe elle
        # reste un jalon ordonné explicite, sans inventer d'appel ni modifier les hits déjà acquis.
    if candidats_out is not None:
        candidats_out.extend(b for b, _ in hits if b not in candidats_out)
    if len(nodes) > budget.max_opens:
        truncated = True  # des nœuds candidats avaient des hits au-delà du quota
    related_cache: dict[str, list[str]] = {}

    def lire(ouverts: list[str]) -> tuple[list[str], dict[str, str], list[str], bool]:
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

        # Unités de dépendance, hors quota `max_opens` : fermeture commune aux deux variantes.
        # Le primaire, ses refs directes, ses définitions applicables et toute limite classée parmi
        # les hits entrent ensemble ou sont tous refusés. Une cible n'est jamais suivie à son tour.
        unites: list[list[str]] = []
        dependances_par_primaire: dict[str, list[str]] = {}
        candidats = [block_id for block_id, _node_id in hits]
        focus_ids = {best_hit[node_id] for node_id in ouverts}
        focus_companions = {
            membre
            for focus_id in focus_ids
            for membre in index.unite_de_renvoi(focus_id)[1:]
        }
        compagnons_candidats = focus_companions & set(candidats)
        # Chaque nœud garde sa place relative. Dans sa fenêtre seulement, le `best_hit` focal est
        # tenté avant les voisins ; son compagnon structurel n'est retraité que s'il est lui-même
        # un candidat de recherche. L'ordre rendu reste celui de `fenetres` ci-dessous.
        primaires: list[str] = []
        for node_id in ouverts:
            focus_id = best_hit[node_id]
            voisins = [
                candidate for candidate in fenetres
                if noeud_de[candidate] == node_id
                and (candidate not in focus_companions
                     or candidate in compagnons_candidats)
            ]
            primaires.extend(_prioriser_focus(
                voisins, focus_id, reserve=_focus_est_reserve(
                    focus_id, node_id, reservations=reserved_candidates,
                    best_hit_by_node=best_hit)))
        for block_id in primaires:
            directes = _dependances_directes(
                block_id, block=bloc, index=index, terms=terms, doc_id=doc_id,
                search_candidates=candidats, related_limit=budget.search_limit,
                related_max=settings.limite_liee_max,
                proximity_min=settings.limite_liee_proximite_min,
                related_cache=related_cache, search_related=block_id in candidats,
            )
            unite = (_unite_primaire(
                block_id, kind=bloc(block_id).kind, index=index, dependances=directes)
                if block_id in focus_ids else [block_id, *directes])
            if unite is None:
                tronque = True
                continue
            unites.append(unite)
            if bloc(block_id).kind in {"garantie", "exclusion"}:
                dependances_par_primaire[block_id] = directes
        # Le pré-contrôle AD-5 interroge aussi `definitions()` : si aucun texte n'a de hit mais
        # qu'un terme demandé est défini, le déterministe doit pouvoir rendre cette définition au
        # lieu de fabriquer ensuite un `zero_hit`. Dès qu'une fenêtre primaire existe, cette voie
        # autonome disparaît : la définition reste alors dans l'unité atomique de chaque bloc
        # qu'elle éclaire, comme l'exigent AD-1/AD-2.
        definitions_autonomes: list[str] = []
        if not fenetres:
            definitions_autonomes = [
                block_id for block_id, _node_id in index.definitions(terms, doc_id=doc_id)
            ]
            unites.extend([block_id] for block_id in definitions_autonomes)

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

        # Ordre rendu au modèle : les fenêtres dans l'ordre de lecture, puis leurs dépendances ;
        # sans aucune fenêtre, les définitions directement demandées gardent l'ordre du corpus.
        ordre: list[str] = []
        for b in (*fenetres, *definitions_autonomes, *(c for u in unites for c in u[1:])):
            if b in seen and b not in ordre:
                ordre.append(b)
        dependances_decisionnelles: list[str] = []
        for primaire, directes in dependances_par_primaire.items():
            if primaire not in seen:
                continue
            dependances_decisionnelles.extend(
                candidate for candidate in directes
                if candidate in seen and candidate not in dependances_decisionnelles)
        return ordre, noeud_de, dependances_decisionnelles, tronque

    ouverts, (promus, cedes) = _reserver(nodes, designes, budget.max_opens, budget.profil_max_opens)
    ordre, noeud_de, dependances_decisionnelles, tronque = lire(ouverts)
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
        ordre, noeud_de, dependances_decisionnelles, tronque = lire(ouverts)
    truncated = truncated or tronque
    blocs = [bloc(b) for b in ordre]

    opened = [b.block_id for b in blocs]
    # AD-10, littéralement : « candidats de `chercher` non ouverts » — donc les hits qui ne sont pas
    # transmis au modèle, et rien d'autre. Un bloc voisin écarté par le budget n'est pas un candidat
    # de recherche : c'est `truncated` qui porte cette information (revue Codex 1.4, B5).
    discarded = [b for b, _ in hits if b not in set(ordre)]
    # Story 4.2f : la même règle que côté outils, sur `ordre` — donc **après** le budget de blocs et
    # de tokens. Un nœud dont toute la fenêtre a été écartée n'a rien fait lire ; un bloc entré hors
    # fenêtre (définition autonome, renvoi direct) a bien été lu, et son nœud compte.
    result = RetrievalResult(blocs=blocs, opened_block_ids=opened,
                             opened_node_ids=_noeuds_des_blocs(ordre, corpus=corpus, index=index),
                             discarded_block_ids=discarded,
                             decision_dependency_block_ids=dependances_decisionnelles,
                             truncated=truncated)
    step = StepTrace(name="retrouver", tier=STEP_TIERS["retrouver"],
                     prompt_cache=None,
                     mechanism_order=list(mechanisms),
                     ms=int((time.monotonic() - t0) * 1000),
                     opened_block_ids=list(opened), discarded_block_ids=list(discarded))
    if faq_nodes:
        step.checks.append(CheckResult(
            name="faq", ok=True,
            detail=f"{len(faq_nodes)} nœud(s) candidat(s) traité(s) selon l'ordre configuré"))
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
    if expanded_search is not None:
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
        touches = sum(1 for variantes in expanded_search.values()
                      if any(v and v not in base for v in variantes))
        step.checks.append(CheckResult(
            name="dictionnaire", ok=True,
            detail=f"{ajoutees} variante(s) ajoutée(s) à {touches} terme(s)"))
    return result, step


def _definitions_de_la_cible(trouvees: list[tuple[str, str]], *, cible: str,
                             bloc: Any) -> list[str]:
    """Parmi les définitions rendues par l'index, celles qui définissent **la cible demandée**.

    Le filtre n'est pas une précaution de style : `Index.definitions(..., blocs_ouverts=…)` rend
    aussi, par sa branche « terme rencontré dans un bloc ouvert », toute définition dont le
    `defines` figure dans le texte d'un bloc déjà lu — **indépendamment des termes demandés**. C'est
    exactement ce que veut la fermeture d'un niveau des deux variantes ; ce n'est pas ce que veut la
    satisfaction d'une demande. Sans le filtre, une demande portant sur un terme que le contrat ne
    définit nulle part rendait quand même « un bloc neuf » : le pipeline la déclarait satisfaite,
    payait un appel de reprise, et la fermeture `humain` / `contexte_non_relu` n'avait pas lieu —
    la garantie fail-closed de la story tombait sur son cas le plus fréquent.

    Deux groupes, du plus précis au moins précis, et **le second ne sert que si le premier est
    vide** — c'est ce qui borne l'élargissement en mots isolés :

    1. les définitions qui **nomment** la cible (`defines` la contient en mots entiers) : « jardin »
       trouve « mobilier de jardin », la règle même de `Index.definitions` ;
    2. à défaut, celles dont le `defines` est une **sous-expression** de la cible en mots entiers :
       une qualité « caractère X de l'événement » n'est définie nulle part sous ce libellé, et la
       définition de « X » est alors le seul contexte que la demande puisse viser.

    Une définition étrangère aux deux groupes n'est jamais rouverte, quel que soit le bloc ouvert
    qui emploie son terme.
    """
    forme_cible = f" {forme(cible)} "
    if not forme_cible.strip():
        return []
    nomment: list[str] = []
    sous_expressions: list[str] = []
    for block_id, _node_id in trouvees:
        defini = f" {forme(bloc(block_id).defines or '')} "
        if not defini.strip():
            continue
        if forme_cible in defini:
            nomment.append(block_id)
        elif defini in forme_cible:
            sous_expressions.append(block_id)
    return nomment or sous_expressions


def satisfaire_demande(demande: DemandeContexte, *, retrieval: RetrievalResult, corpus: Corpus,
                       index: Index, budget: RetrievalBudget, settings: Settings,
                       doc_id: str) -> tuple[RetrievalResult, StepTrace]:
    """Story 4.2e — rouvre **exactement** ce qu'une demande de contexte vise, en code pur, un niveau.

    Aucune entrée publique de cette étape n'acceptait un état déjà ouvert à compléter : c'était le
    seul manque. Cette fonction le comble sans rien ajouter au vocabulaire de l'étape.

    **Pourquoi ici et pas dans le pipeline.** AD-1 : « une étape ne peut appeler que `corpus` et
    `llm` », et les quatre outils d'AD-1 appartiennent à *retrouver*. Un pipeline qui appellerait
    `Index.definitions` lui-même déplacerait les outils hors de leur propriétaire. Ce n'est donc pas
    un composant nouveau : c'est la fermeture **déjà écrite** (`_dependances_directes`, « commune aux
    deux variantes ») exposée sur un état ouvert.

    **Aucun tour modèle.** La demande dit ce qui manque ; la satisfaire est une résolution du corpus,
    pas une navigation. Le coût en appels est donc nul, et `StepTrace.calls == []` le dit — la même
    convention que la variante déterministe.

    **Un niveau, jamais deux, et rien que la cible.** Pour un `renvoi`, les `refs` du bloc visé ;
    leurs propres `refs` ne sont pas suivis, et les définitions des termes que ce bloc emploie n'en
    font pas partie — elles relèvent de la fermeture automatique d'AD-1, que la passe initiale a
    déjà faite, pas du renvoi demandé. Pour une `definition` ou une
    `qualite`, les définitions applicables du terme demandé, résolues **dans la portée** des blocs
    déjà ouverts (AD-2, `Index.definitions(..., blocs_ouverts=…)`) — c'est ce qui donne à un contrat
    la définition de sa branche plutôt que celle d'une autre.

    **Le budget est celui de l'étape, pas celui de la passe** (AD-1 : « un `RetrievalBudget` borne
    **toute** l'étape »). Les compteurs sont donc amorcés avec ce que la passe initiale a déjà fait
    lire : sans cela, une seconde passe repartie de zéro pourrait admettre `max_blocks` blocs de
    plus, c'est-à-dire ne plus être bornée du tout. Le budget lui-même n'est jamais muté.

    Rend `(RetrievalResult augmenté, StepTrace)`. Aucun bloc neuf ⇒ résultat identique à l'entrée sur
    ses blocs, et le check le dit : c'est au pipeline de fermer sur une demande insatisfaite.
    """
    t0 = time.monotonic()
    step = StepTrace(name="retrouver", tier=STEP_TIERS["retrouver"])
    if doc_id not in corpus.documents:
        raise KeyError(doc_id)

    def bloc(block_id: str) -> Block:
        if index.doc_of(block_id) != doc_id:
            raise KeyError(block_id)
        return corpus.documents[doc_id].block(block_id)

    ouverts = [b.block_id for b in retrieval.blocs]
    deja = set(ouverts)
    candidats: list[str] = []
    if demande.kind == "renvoi":
        # La cible a été validée par *vérifier* contre les blocs fournis ; la garde reste, parce que
        # cette entrée est publique et qu'un appelant futur n'aura pas fait ce contrôle.
        #
        # Revue 4.2e (C, chemin `renvoi`) : **les `refs` du bloc visé, et rien d'autre.** La
        # fermeture commune `_dependances_directes` ajoute aussi les définitions des termes que ce
        # bloc emploie — c'est la fermeture automatique d'un niveau d'AD-1, déjà appliquée par la
        # passe initiale, et ce n'est pas ce qu'une demande de renvoi vise. L'y laisser rouvrait un
        # bloc étranger au renvoi demandé, que le pipeline comptait ensuite comme « un bloc neuf » :
        # la demande était déclarée satisfaite, un appel de reprise était payé, et la fermeture
        # `humain` / `contexte_non_relu` n'avait pas lieu — le même défaut fail-closed que le filtre
        # des définitions ferme sur l'autre chemin.
        if demande.cible in deja:
            candidats = [ref for ref in bloc(demande.cible).refs if ref != demande.cible]
    else:
        # Le libellé entier **et** ses mots : `Index.definitions` apparie `defines` et terme en mots
        # entiers, si bien qu'une qualité nommée « caractère X de l'événement » ne trouverait la
        # définition de « X » par aucun autre chemin. Aucun mot n'est ajouté ni traduit.
        termes = list(dict.fromkeys([demande.cible, *forme(demande.cible).split()]))
        candidats = _definitions_de_la_cible(
            index.definitions(termes, doc_id=doc_id, blocs_ouverts=ouverts),
            cible=demande.cible, bloc=bloc)
    candidats = [b for b in dict.fromkeys(candidats)
                 if b not in deja and index.doc_of(b) == doc_id]

    blocs_utilises = len(retrieval.blocs)
    tokens_utilises = sum(estimate_tokens(f"{b.block_id}\n{b.text}", settings)
                          for b in retrieval.blocs)
    retenus: list[str] = []
    ecartes: list[str] = []
    tronque = False
    for candidate in candidats:
        cout = estimate_tokens(f"{candidate}\n{bloc(candidate).text}", settings)
        if ((budget.max_blocks is not None and blocs_utilises + 1 > budget.max_blocks)
                or (budget.max_tokens is not None and tokens_utilises + cout > budget.max_tokens)):
            # Un candidat que le budget écarte est un candidat non lu, pas une absence du corpus :
            # il rejoint `discarded_block_ids` et marque la lecture comme tronquée (AD-1).
            ecartes.append(candidate)
            tronque = True
            continue
        blocs_utilises += 1
        tokens_utilises += cout
        retenus.append(candidate)

    blocs = [*retrieval.blocs, *(bloc(b) for b in retenus)]
    ids = list(dict.fromkeys([*retrieval.opened_block_ids, *retenus]))
    resultat = retrieval.model_copy(update={
        "blocs": blocs,
        "opened_block_ids": ids,
        "opened_node_ids": _noeuds_des_blocs([b.block_id for b in blocs], corpus=corpus, index=index),
        "discarded_block_ids": list(dict.fromkeys([*retrieval.discarded_block_ids, *ecartes])),
        "truncated": retrieval.truncated or tronque,
    })
    step.ms = int((time.monotonic() - t0) * 1000)
    step.opened_block_ids = list(retenus)
    step.discarded_block_ids = list(ecartes)
    # AD-10 : des comptes et notre propre vocabulaire fermé, jamais la cible reçue du modèle.
    #
    # `ok` exige d'avoir rouvert **et** de n'avoir rien écarté (revue croisée 4.2e, I1). Une passe
    # qui ouvre une cible et en laisse une autre dehors sous la borne de l'étape n'a relu le
    # contexte demandé qu'à moitié — et « à moitié » n'est pas une réponse à « il me manque ceci
    # pour juger ». La déclarer satisfaite laissait une reprise juger sur un contexte incomplet et
    # rendre un verdict décisoire, c'est-à-dire exactement ce que la borne devait empêcher.
    step.checks.append(CheckResult(
        name="satisfaction_demande", ok=bool(retenus) and not ecartes,
        detail=f"demande de contexte de catégorie `{demande.kind}` : {len(candidats)} bloc(s) "
               f"candidat(s), {len(retenus)} rouvert(s), {len(ecartes)} écarté(s) par le budget "
               "de l'étape — aucun appel modèle"))
    return resultat, step
