"""AD-1 — ce que *retrouver* garde après l'amendement du 03/09/2026.

Le chemin **servi** est la navigation par le modèle (`steps/naviguer.py`) : le modèle reçoit le
sommaire complet, ouvre ce qu'il veut, et rien de ce qu'il n'a pas ouvert n'entre dans la rédaction.
Les deux variantes qui **choisissaient à sa place** ont été supprimées avec leurs témoins :
`deterministe` (classement lexical, réservations de profil et de facette) et `outils` (navigation
bornée, puis complétion des facettes absentes depuis le classement). Ce qui n'existait que pour
elles a suivi : la réservation par facette, l'attribution lexicale d'un bloc à une sous-question,
la couverture par facette (`couvrir_facettes`) et l'attachement automatique des définitions.

Restent ici **deux** fonctions, et aucune ne classe pour le modèle :

- `retrouver_full_context` — variante de **comparaison**, jamais servie : tout le corpus citable
  entre dans le préfixe cacheable et c'est le modèle qui rend la liste des `block_id`. Le code n'y
  ordonne rien d'autre que l'ordre documentaire, et borne (`max_opens`, blocs, tokens).
- `satisfaire_demande` — story 4.2e : quand *vérifier* dit qu'il lui manquait un passage pour juger,
  cette passe rouvre **exactement** la cible nommée, en code pur, un seul niveau, sous le budget de
  l'étape. Elle sert sur le chemin de navigation aussi : elle ne choisit pas ce que la rédaction
  voit, elle rend au **contrôle** ce qu'il a demandé par écrit.

**Le `RetrievalBudget` borne toute l'étape** (AD-1 : « nœuds, blocs, tokens, définitions et renvois
inclus »). `max_blocks` et `max_tokens` sont appliqués ensemble par unités de dépendance : un bloc de
fenêtre voyage avec les cibles de ses renvois, jamais l'inverse — une cible sans le passage qui la
cite est inutilisable et peut même égarer la rédaction (revue Codex 1.4, B6). Une unité qui n'entre
pas est sautée (les suivantes sont essayées : le budget n'est pas gaspillé), et `truncated` le dit.
Faute de tokenizer en code pur, les tokens sont majorés par l'heuristique d'`estimate_cost`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from typing import Any

from server.app.config import Settings
from server.app.corpus.dictionary import Dictionnaire, forme
from server.app.corpus.index import Index, reading_order
from server.app.corpus.requetes import (MOTS_OUTILS_LIMITES as _MOTS_OUTILS_LIMITES,
                                        part_du_mot_borne)
from server.app.corpus.loader import Corpus
from server.app.domain import (AdmissionDecision, Block, BudgetSnapshot,
                               FullContextSelection,
                               RetrievalBudget, RetrievalResult,
                               SufficiencyDecision,
                               QuestionClauseScore, is_citable)
from server.app.domain.answer import DemandeContexte
from server.app.domain.errors import BudgetExceeded, LlmParse, PipelineError
from server.app.domain.question import ParsedQuestion
from server.app.domain.trace import CheckResult, StepTrace
from server.app.llm.client import structured_input_envelope
from server.app.llm.models import MODEL_CAPS, STEP_TIERS, model_for
from server.app.llm.pricing import estimate_tokens
from server.app.llm.prompting import render_prompt, untrusted


# Réexport assumé : `part_du_mot_borne` vit dans `corpus/requetes.py`, et la couche `pipelines`
# n'a pas le droit d'importer `corpus` (`tests/test_layers.py`). Les deux pipelines en ont besoin
# pour le pré-contrôle zéro hit d'AD-5 et pour chiffrer `AbsenceProof.variants_count` : ils le
# prennent donc ici, comme avant l'amendement AD-1 du 03/09/2026.
__all__ = ["part_du_mot_borne", "retrouver_full_context", "satisfaire_demande"]


_KINDS_LIMITATIFS = frozenset({"exclusion", "condition", "franchise"})


def _mots_porteurs(value: str) -> set[str]:
    """Mots lexicaux utilisables comme preuve, sans lexique documentaire ou métier."""
    return {mot for mot in forme(value).split()
            if mot.isalnum() and mot not in _MOTS_OUTILS_LIMITES}


def _score_positif(score: QuestionClauseScore, *, question: str, clause: str) -> bool:
    """Le score reste transporté tel quel ; sa suffisance exige un chevauchement substantiel."""
    return (score.question_numerator > 0
            and bool(_mots_porteurs(question) & _mots_porteurs(clause)))


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
            mots_source = _mots_porteurs(current.text)

            def preuve_et_proximite(candidate: str) -> tuple[bool, float, int]:
                limite = block(candidate)
                relations = {
                    current.relation.exception_de, current.relation.specialise,
                    limite.relation.exception_de, limite.relation.specialise,
                }
                liee = (candidate in current.refs or block_id in limite.refs
                        or candidate in relations or block_id in relations
                        or document.node_of(block_id) in document.scope_nodes(candidate))
                mots_candidat = _mots_porteurs(limite.text)
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
            max_tokens=max_tokens, prompt_cache=settings.retrieval_prompt_cache,
            trusted_line_uids=tuple(dict.fromkeys(
                line.line_uid for block in ordered for line in block.lines
                if line.line_uid is not None
            )))
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
    canonical_question = parsed.question_resolue.strip() or " ".join(parsed.termes_de_recherche())
    scored_hits = [index.score_clause(canonical_question, block_id)
                   for block_id in selected_ids]
    hit_by_id = {hit.clause_uid: hit for hit in scored_hits}
    unscored: list[str] = []

    def block(block_id: str) -> Block:
        if index.doc_of(block_id) != doc_id:
            raise KeyError(block_id)
        return document.block(block_id)

    # `max_opens` borne les nœuds primaires choisis par le modèle, exactement comme les fenêtres
    # des deux autres variantes. Plusieurs passages du même nœud ne consomment qu'une ouverture.
    primary_nodes: list[str] = []
    admitted_primary_ids: list[str] = []
    discarded: list[str] = list(unscored)
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
    admission_by_result: dict[str, AdmissionDecision] = {}

    opened_nodes_used: list[str] = []

    def snapshot() -> BudgetSnapshot:
        return BudgetSnapshot(
            opens_used=len(opened_nodes_used), blocks_used=blocks_used, tokens_used=tokens_used,
            opens_remaining=max(0, budget.max_opens - len(opened_nodes_used)),
            blocks_remaining=(None if budget.max_blocks is None else
                              max(0, budget.max_blocks - blocks_used)),
            tokens_remaining=(None if budget.max_tokens is None else
                              max(0, budget.max_tokens - tokens_used)),
        )
    for unit in units:
        primary_node_id = document.node_of(unit[0])
        if primary_node_id not in opened_nodes_used:
            opened_nodes_used.append(primary_node_id)
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
            hit = hit_by_id.get(unit[0])
            if hit is not None:
                admission_by_result[hit.result_uid] = AdmissionDecision(
                    result_uid=hit.result_uid, state="rejected", reason="block_budget_exceeded",
                    snapshot=snapshot())
            continue
        if budget.max_tokens is not None and tokens_used + token_cost > budget.max_tokens:
            truncated = True
            if unit[0] not in admitted and unit[0] not in discarded:
                discarded.append(unit[0])
            hit = hit_by_id.get(unit[0])
            if hit is not None:
                admission_by_result[hit.result_uid] = AdmissionDecision(
                    result_uid=hit.result_uid, state="rejected", reason="token_budget_exceeded",
                    snapshot=snapshot())
            continue
        blocks_used += len(new)
        tokens_used += token_cost
        admitted.update(new)
        admitted_primaries.append(unit[0])
        hit = hit_by_id.get(unit[0])
        if hit is not None:
            admission_by_result[hit.result_uid] = AdmissionDecision(
                result_uid=hit.result_uid, state="admitted", reason="admitted_by_exact_unit",
                snapshot=snapshot())

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
    for hit in scored_hits:
        admission_by_result.setdefault(hit.result_uid, AdmissionDecision(
            result_uid=hit.result_uid, state="rejected", reason="open_budget_exceeded",
            snapshot=snapshot()))
    sufficient_hit = next((
        hit_by_id[block_id] for block_id in opened
        if block_id in hit_by_id
        and (hit_by_id[block_id].score.full_matches > 0
             or hit_by_id[block_id].score.partial_numerator > 0)
        and _score_positif(
            hit_by_id[block_id].score,
            question=canonical_question,
            clause=block(block_id).text,
        )
    ), None)
    considered = tuple(hit.result_uid for hit in scored_hits
                       if hit.clause_uid in set(opened))
    return RetrievalResult(
        blocs=[block(block_id) for block_id in opened], opened_block_ids=opened,
        opened_node_ids=_noeuds_des_blocs(opened, corpus=corpus, index=index),
        decision_dependency_block_ids=decision_dependencies,
        discarded_block_ids=discarded, scored_hits=scored_hits,
        admission_decisions=list(admission_by_result.values()),
        sufficiency=SufficiencyDecision(
            complete=sufficient_hit is not None,
            reason=("relevant_result_admitted" if sufficient_hit else
                    "no_relevant_result_within_budget"),
            sufficiency_result_uid=(sufficient_hit.result_uid if sufficient_hit else None),
            considered_result_uids=considered,
        ), truncated=truncated), step


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
