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

En dernier, et seulement quand l'appelant a déclaré des kinds suffisants, la **couverture par
facette** ferme l'écart que la suffisance globale laissait ouvert : un seul bloc décisionnel
confirmé atteignait la suffisance, quelle que soit la sous-question à laquelle il répondait. Chaque
facette de `ParsedQuestion` doit désormais porter au moins un bloc décisionnel **confirmé et
transmis** que son propre classement (`Index.chercher(kinds_confirmes=…)`) a proposé, ou être
**déclarée non retrouvée** dans `RetrievalResult.facettes`. La passe ouvre au tour de rôle, au plus
`facette_max_opens` essais par facette, par le même outil et sous les mêmes quotas — elle ne crée
aucune capacité. Une facette abandonnée alors qu'un candidat restait lisible est une borne de
lecture (`truncated`) ; une facette dont le classement est vide ou épuisé est une absence, et la
couverture vide la dit. `couvrir_facettes()` expose la même règle sur un état déjà ouvert, en code
pur, pour le second cycle du pipeline.

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
candidats dépassent `max_opens`, ou si le budget de blocs/tokens a écarté quelque chose. **Il dit une
borne de lecture, et rien d'autre** : le verdict sémantique terminal de la variante outils, qu'il
soit illisible ou qu'il désigne un résultat non admis, refuse la suffisance sans jamais rendre
`truncated=True` — la trace le nomme dans le check `verdict_semantique` et dans
`SufficiencyDecision.reason` (`unreadable_semantic_verdict`, `invalid_semantic_result_uid`). Les blocs
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
from collections.abc import Callable, Iterable
from typing import Any

from server.app.config import Settings
from server.app.corpus.dictionary import Dictionnaire, forme
from server.app.corpus.index import Index, reading_order, words
from server.app.corpus.text import forme_de_nombre, normalize
from server.app.corpus.loader import Corpus
from server.app.domain import (AdmissionDecision, Block, BudgetSnapshot, FacetteCouverture,
                               FullContextSelection,
                               RetrievalBudget, RetrievalResult, ScoredHit,
                               SemanticSufficiencySelection, SufficiencyDecision,
                               QuestionClauseScore, is_citable)
from server.app.domain.answer import DemandeContexte
from server.app.domain.errors import BudgetExceeded, LlmParse, PipelineError
from server.app.domain.question import ParsedQuestion
from server.app.domain.trace import CheckResult, StepTrace
from server.app.domain.verdict import KINDS_DECISIONNELS, KINDS_FONDATEURS
from server.app.llm.client import structured_input_envelope
from server.app.llm.models import MODEL_CAPS, STEP_TIERS, model_for
from server.app.llm.pricing import estimate_tokens
from server.app.llm.prompting import render_prompt, untrusted


OUTILS_RECHERCHE: list[dict[str, Any]] = [
    {
        "name": "sommaire",
        "description": "Parcourir une page du sommaire exhaustif versionné.",
        "input_schema": {
            "type": "object", "additionalProperties": False,
            "properties": {"doc_id": {"type": "string"},
                           "cursor": {"type": "integer", "minimum": 0}},
            "required": ["doc_id"],
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
    "a", "au", "aux", "avec", "ce", "ces", "d", "dans", "de", "des", "du", "elle", "en",
    "est", "et", "il", "ils", "l", "la", "le", "les", "leur", "leurs", "lui", "ne", "ni", "on", "ou",
    "par", "pas", "pour", "que", "qui", "sa", "se", "ses", "son", "sur", "un", "une",
})


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


def _cout_des_blocs(block_ids: Iterable[str], *, block: Callable[[str], Block],
                    settings: Settings) -> int:
    """Le coût en tokens d'un ensemble de blocs, dans la représentation exacte de l'admission."""
    return sum(estimate_tokens(f"{b}\n{block(b).text}", settings)
               for b in dict.fromkeys(block_ids))


def _enumeration_lisible(block_id: str, *, index: Index, block: Callable[[str], Block],
                         settings: Settings) -> list[str] | None:
    """L'énumération à laquelle ce bloc appartient, **si elle tient sous sa borne**.

    Correctif du tour 6 (F1). `Index.enumeration_de` dit la structure ; la borne est ici, parce
    qu'elle est une affaire de budget de lecture et non de document. Au-delà de
    `enumeration_max_tokens`, l'unité redevient l'amorce et l'item demandé — le comportement d'avant
    ce correctif, qui reste juste : ce qui serait faux est de transmettre un article entier pour une
    feuille.
    """
    membres = index.enumeration_de(block_id)
    if membres is None:
        return None
    if _cout_des_blocs(membres, block=block, settings=settings) > settings.enumeration_max_tokens:
        return None
    return membres


def _membres_denumeration(window_ids: list[str], *, focus: str | None, index: Index,
                          block: Callable[[str], Block], settings: Settings) -> list[str]:
    """Les blocs d'une énumération à joindre à une fenêtre, ou rien (F1).

    Deux entrées, une seule unité : le navigateur ouvre le nœud de l'amorce (les items sont ses
    nœuds enfants, donc absents de sa fenêtre), ou il vise directement l'un des items. Une
    énumération qui dépasse `enumeration_max_tokens` n'en est plus une pour la lecture : rien n'est
    joint, et la fenêtre reste ce qu'elle était.
    """
    depart = focus if focus is not None else next(
        (b for b in window_ids if index.enumeration_de(b) is not None), None)
    if depart is None:
        return []
    membres = _enumeration_lisible(depart, index=index, block=block, settings=settings)
    return membres or []


def _unite_primaire(block_id: str, *, kind: str, index: Index,
                     dependances: Iterable[str],
                     block: Callable[[str], Block] | None = None,
                     settings: Settings | None = None) -> list[str] | None:
    """Unité atomique commune : primaire structurel, puis dépendances directes.

    `Index.unite_de_renvoi` est l'autorité structurelle : un titre emporte son premier corps
    non-titre, tandis qu'un primaire ordinaire reste seul. Les dépendances sont celles du bloc
    demandé seulement ; le corps ajouté n'est jamais parcouru récursivement. Un titre sans corps
    non-titre ne forme aucune unité transmissible : `None` ordonne à l'appelant de le refuser et de
    publier la troncature.

    **Et le cas symétrique, ajouté au tour 5 (C9) :** l'item d'une énumération emporte la phrase qui
    l'ouvre (`Index.amorce_de_lenumeration`). Une feuille comme « Les fumées et les suies ; » n'est
    pas plus citable seule qu'un titre — servie sans « La Compagnie assure les biens désignés,
    contre les périls suivants : », elle a produit une affirmation que le vérificateur a rejetée
    `non_soutenue`, le rédacteur ayant emprunté à une clause absente le membre qui manquait. Un seul
    niveau, jamais récursif, exactement comme `unite_de_renvoi`. Rayon mesuré hors ligne : 287 nœuds
    sur 751 pour le contrat AXA, 7 sur 274 pour Baloise, 0 pour le guide ; amorce médiane 13 mots
    (AXA) et 17 (Baloise).
    """
    structure = index.unite_de_renvoi(block_id)
    if kind == "heading" and structure == [block_id]:
        return None
    # F1 : quand le bloc appartient à une énumération qui tient sous sa borne, **l'unité est
    # l'énumération entière** — amorce et items —, parce que ses items se qualifient les uns les
    # autres. À défaut (borne dépassée, ou pas une énumération), C9 s'applique seul : l'item
    # emporte la phrase qui l'ouvre, rien de plus.
    enumeration = (_enumeration_lisible(block_id, index=index, block=block, settings=settings)
                   if kind != "heading" and block is not None and settings is not None else None)
    amorce = index.amorce_de_lenumeration(block_id) if kind != "heading" else None
    tete = enumeration if enumeration is not None else ([amorce] if amorce else [])
    return list(dict.fromkeys((*tete, *structure, *dependances)))


def _prioriser_focus(block_ids: Iterable[str], focus_id: str | None, *, reserve: bool) -> list[str]:
    """Ordre d'essai partagé : un focus prioritaire passe avant ses frères.

    Cet ordre sert uniquement à l'admission sous budget. Les appelants conservent séparément
    l'ordre documentaire nécessaire au rendu. Un focus non prioritaire reste dans cet ordre.
    """
    ids = list(block_ids)
    if not reserve or focus_id is None or focus_id not in ids:
        return ids
    return [focus_id, *(block_id for block_id in ids if block_id != focus_id)]


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


def _variantes_de_facette(formes: Iterable[str], *, index: Index, doc_id: str,
                          part_max: float) -> list[str]:
    """Les formes de nombre du libellé qui **désignent une clause**, jamais celles qui nomment le sujet.

    Correctif du tour 3 (R2). Un libellé de facette est une phrase et l'index est littéral : sur les
    six libellés des trois runs A16, **aucun** n'atteignait `full_matches > 0`, avec ou sans sa forme
    plurielle de phrase. La variante utile est donc au niveau du **mot** — mais seulement pour les
    mots que le document porte rarement : une variante d'un mot fréquent est pleinement couverte par
    des dizaines de blocs, ce qui rendrait la garde de R1 inerte et renverrait le rang 0 au bruit.

    Deux filtres, tous deux déjà dans le dépôt : les mots-outils du français
    (`_MOTS_OUTILS_LIMITES`, la liste fermée qu'emploie déjà `_mots_porteurs`) et la part des blocs
    que le document consacre au mot (`Index.part_des_blocs`, la fréquence documentaire qui pondère
    déjà les couvertures partielles). Aucun vocabulaire métier, aucune lecture du texte des clauses.

    La forme **de phrase** est conservée en tête : elle ne prouve rien à elle seule (aucun bloc ne
    couvre une phrase entière) mais elle remonte le bon bloc au score partiel, et c'est elle qui
    porte l'ordre quand plusieurs mots rares sont en jeu.

    Les formes sont celles du **groupe entier** — le canonique et les variantes que le dictionnaire
    lui a déjà données —, jamais du seul libellé reçu : deux libellés que le dictionnaire ramène au
    même groupe doivent rester **une** requête, et le seraient cessé de l'être si chacun dérivait
    ses variantes de son propre texte.
    """
    variantes: list[str] = []
    for forme_source in dict.fromkeys(formes):
        mots = words(normalize(forme_source))
        if not mots:
            continue
        phrase = " ".join(forme_de_nombre(mot) or mot for mot in mots)
        if phrase != " ".join(mots) and phrase not in variantes:
            variantes.append(phrase)
        for mot in mots:
            if mot in _MOTS_OUTILS_LIMITES:
                continue
            autre = forme_de_nombre(mot)
            if autre is None or autre in variantes:
                continue
            try:
                if index.part_des_blocs(autre, doc_id=doc_id) <= part_max:
                    variantes.append(autre)
            except KeyError:  # pragma: no cover — document servi, garanti par l'appelant
                continue
    return variantes


def _premier_objet_json(texte: str) -> str | None:
    """Le premier objet JSON **équilibré** d'un texte, préambule et clôture Markdown tolérés.

    Correctif du tour 3 (R5). Le verdict terminal était lu par `model_validate_json` sur le texte
    brut concaténé du dernier tour. Le navigateur avait pourtant rendu la bonne réponse — une phrase
    en français, puis un bloc ```` ```json ```` valide qui nommait la clause de la sous-question
    restée sans réponse : le préambule et la clôture Markdown faisaient échouer le parse, et ce
    verdict correct partait à la poubelle sous l'étiquette « verdict illisible ».

    L'extraction compte les accolades **hors chaîne**, en respectant l'échappement : c'est le seul
    moyen de ne pas couper sur une accolade citée dans un libellé. Elle ne répare rien et n'invente
    rien — un texte sans objet équilibré rend `None`, et le verdict reste illisible comme avant.
    """
    depart = texte.find("{")
    if depart < 0:
        return None
    profondeur = 0
    dans_chaine = False
    echappe = False
    for position in range(depart, len(texte)):
        caractere = texte[position]
        if dans_chaine:
            if echappe:
                echappe = False
            elif caractere == "\\":
                echappe = True
            elif caractere == '"':
                dans_chaine = False
            continue
        if caractere == '"':
            dans_chaine = True
        elif caractere == "{":
            profondeur += 1
        elif caractere == "}":
            profondeur -= 1
            if profondeur == 0:
                return texte[depart:position + 1]
    return None


def _noter_ambigues(step: StepTrace, dictionnaire: Dictionnaire, termes: list[str]) -> None:
    """Dit **combien** de formes cherchées n'ont rien élargi faute d'un groupe unique (E1).

    Un compte, jamais la liste : une forme ambiguë est du vocabulaire du dictionnaire, et AD-4
    n'autorise à publier que les canoniques effectivement cherchés. Le check ne se pose que
    lorsqu'il y en a — une ligne « 0 forme ambiguë » sur chaque requête ne dirait rien à personne, et
    l'absence de silence est ici l'information.
    """
    ambigues = dictionnaire.variantes_ambigues(termes)
    if ambigues:
        step.checks.append(CheckResult(
            name="variantes_ambigues", ok=True,
            detail=f"{ambigues} forme(s) ambiguë(s) non élargies : plusieurs groupes les "
                   "revendiquent et aucun ne les nomme ; le terme reste cherché tel quel"))


def part_du_mot_borne(index: Index | None, doc_id: str | None, *,
                 part_max: float) -> Callable[[str], float] | None:
    """La fréquence documentaire du document servi, **quand elle mesure quelque chose**.

    Correctif du tour 5 (C8). `Dictionnaire` ne connaît aucun index — c'est bien ainsi : il décrit
    un vocabulaire, pas un corpus chargé. La borne de fréquence lui est donc **injectée** par
    l'appelant qui, lui, sait quel document est interrogé. Une recherche sans document (`doc_id`
    nul) ne borne rien : aucune fréquence documentaire n'a de sens sur plusieurs documents à la
    fois, exactement comme `utilisable_pour(None)` ne reconnaît aucun dictionnaire.

    **Et un seuil en part n'est une mesure que si le document a de quoi la porter.** La plus fine
    part qu'un document de `N` blocs sache exprimer vaut `1/N` : tant que `part_max × N` n'atteint
    pas un bloc entier, « dépasser le seuil » et « figurer une seule fois dans le document » sont la
    même chose, et la borne ne dirait plus « cette forme nomme le sujet » mais « cette forme
    existe ». Elle s'abstient donc, et c'est le bon sens de l'abstention : une variante de
    dictionnaire est une équivalence **écrite** (AD-5), pas une forme dérivée par le code comme les
    variantes de nombre ; la refuser sans mesure désarmerait en silence le rappel qu'AD-5 apporte.
    Sur le contrat servi — 1 400 blocs, seuil 1 % — la borne parle à partir de 14 blocs.
    """
    if index is None or doc_id is None or doc_id not in index.corpus.documents:
        return None
    blocs = len(index.corpus.documents[doc_id].blocks)
    if part_max * blocs < 1:
        return None
    return lambda mot: index.part_des_blocs(mot, doc_id=doc_id)


def _mappings_facettes(facettes: Iterable[str], *, dictionnaire: Dictionnaire | None,
                       dictionary_ready: bool, index: Index | None = None,
                       doc_id: str | None = None,
                       variante_max_part: float = 0.0,
                       dictionnaire_max_part: float = 0.0) -> list[tuple[int, dict[str, list[str]] | list[str]]]:
    """Le libellé de chaque facette rendu en requête d'index, **son rang conservé**.

    Les facettes ont été arrêtées par *comprendre*, avant tout retrieval (AD-4). Leur rang — la
    position dans `ParsedQuestion.facettes` — est la clé que *vérifier* emploie déjà pour la
    couverture ; c'est aussi la seule qui n'expose pas un texte du modèle dans une trace (AD-10).
    Le libellé, lui, ne sort d'ici que vers `Index.chercher`, exactement comme les termes de la
    question.

    Une facette dont il ne reste aucun mot après normalisation n'a pas de requête : elle n'entre
    pas dans la liste, et n'est donc ni couverte ni déclarée absente — faute de mesure, jamais par
    un jugement. L'expansion par le dictionnaire suit l'état **effectif** du mécanisme, comme la
    recherche du navigateur : deux règles auraient fait diverger le rappel d'un même libellé selon
    l'endroit d'où il part.
    """
    sorties: list[tuple[int, dict[str, list[str]] | list[str]]] = []
    for rang, facette in enumerate(facettes):
        libelle = facette.strip()
        if not forme(libelle):
            continue
        mapping: dict[str, list[str]] | list[str] = [libelle]
        if dictionary_ready and dictionnaire is not None:
            mapping = dictionnaire.expand([libelle],
                                          part_du_mot=part_du_mot_borne(index, doc_id,
                                                                   part_max=dictionnaire_max_part),
                                          part_max=dictionnaire_max_part)
        if index is not None and doc_id is not None and variante_max_part > 0:
            # Les formes de nombre **s'ajoutent** aux variantes que le dictionnaire a déjà données,
            # canonique par canonique : le canonique reste ce qu'il était, et l'on n'éclate jamais
            # la requête en canoniques séparés — mesuré contre-productif, chaque mot devenant alors
            # sa propre preuve pleine. Elles sont dérivées du **canonique**, pas du libellé brut :
            # deux libellés que le dictionnaire ramène au même groupe restent ainsi une seule
            # requête, comme avant ce correctif.
            groupes = mapping if isinstance(mapping, dict) else {valeur: [] for valeur in mapping}
            toutes = [f for canon, variantes in groupes.items() for f in (canon, *variantes)]
            formes = _variantes_de_facette(toutes, index=index, doc_id=doc_id,
                                            part_max=variante_max_part)
            enrichi = {canon: [*variantes,
                               *(f for f in formes if f not in variantes and f != canon)]
                       for canon, variantes in groupes.items()}
            if any(enrichi[canon] != groupes[canon] for canon in groupes):
                mapping = enrichi
        sorties.append((rang, mapping))
    return sorties


def _signature_mapping(mapping: dict[str, list[str]] | list[str]) -> frozenset[str]:
    """Ce que deux requêtes de facette ont en commun : leurs formes, jamais leur ordre."""
    groupes = ((canon, *variantes) for canon, variantes in mapping.items()) \
        if isinstance(mapping, dict) else (mapping,)
    return frozenset(forme(valeur) for groupe in groupes for valeur in groupe if forme(valeur))


# Une facette **unique** est la question elle-même. La couverture décisionnelle de la question est
# déjà ce que mesure la suffisance déclarée par l'appelant — et elle la mesure sur la question
# résolue, pas sur une paraphrase. Superposer à cela le classement d'un libellé qui reformule la
# question entière ferait entrer des blocs choisis par une requête plus vague que celle qui a servi
# à lire, sans rien garantir de plus. C'est exactement la ligne que l'étape trace déjà pour le
# rappel après suffisance (« une facette unique conserve son arrêt historique ») : elle vaut ici
# pour la même raison, et elle est nommée une fois.
FACETTES_MIN_POUR_COUVERTURE = 2


def _mappings_dedupes(
        mappings: list[tuple[int, dict[str, list[str]] | list[str]]],
) -> list[dict[str, list[str]] | list[str]]:
    """Les requêtes de facette distinctes, dans l'ordre : deux libellés synonymes ne classent qu'une fois."""
    vues: set[frozenset[str]] = set()
    sorties: list[dict[str, list[str]] | list[str]] = []
    for _rang, mapping in mappings:
        signature = _signature_mapping(mapping)
        if signature and signature not in vues:
            vues.add(signature)
            sorties.append(mapping)
    return sorties


def _classement_par_facette(*, index: Index, doc_id: str, question: str,
                            kinds_confirmes: frozenset[str],
                            limit: int) -> Callable[[dict[str, list[str]] | list[str]],
                                                    list[ScoredHit]]:
    """Ce que l'index propose **pour une facette**, restreint aux kinds décisionnels confirmés.

    AD-1 : le modèle propose, le code vérifie. Ici, ni l'un ni l'autre n'invente : le libellé vient
    de *comprendre*, le classement et sa restriction (`kinds_confirmes`) viennent du corpus typé.
    Aucun mot du vocabulaire des clauses n'entre dans la décision — c'est le même appel, avec les
    mêmes bornes, que celui qui sert déjà le rappel après suffisance.

    Le classement est **mémoïsé par signature de requête**, et c'est la mémoïsation qui porte une
    propriété, pas seulement une économie : deux libellés que le dictionnaire ramène au même groupe
    sont une seule requête, classée une seule fois, et la passe d'ouverture puis la mesure finale
    lisent exactement le même ordre. Deux classements du même libellé auraient pu diverger si l'un
    d'eux était calculé après une admission.
    """
    memo: dict[frozenset[str], list[ScoredHit]] = {}

    def classement(mapping: dict[str, list[str]] | list[str]) -> list[ScoredHit]:
        signature = _signature_mapping(mapping)
        if signature not in memo:
            memo[signature] = [
                hit for hit in index.chercher(mapping, limit=limit, doc_id=doc_id,
                                              question=question,
                                              kinds_confirmes=kinds_confirmes)
                # **Correctif du tour 3 (R1) : une correspondance partielle ne propose rien.**
                # `full_matches > 0` est la sémantique d'AC 2.7 — au moins un canonique
                # **entièrement** couvert. Sans cette garde, le rang 0 d'une facette est gagné par
                # recouvrement de mots fréquents : sur les six classements des trois runs A16, il
                # revenait à une exclusion de responsabilité civile immeuble, `full_matches = 0`,
                # sans aucun rapport avec le sinistre. Elle était réservée avant la navigation, elle
                # était admise, elle « couvrait » donc la facette **par construction**, et le motif
                # de relance ordonnait ensuite d'écrire une claim dessus.
                #
                # Le filtre vit **ici**, à la source du classement mémoïsé, et non dans chacun des
                # quatre appelants (réserve, passe de couverture, mesure, passe pure du pipeline) :
                # une garde recopiée quatre fois aurait divergé au premier amendement, et c'est
                # exactement le genre d'écart que ce défaut a produit.
                if hit.score.full_matches > 0
            ]
        return memo[signature]

    return classement


def _unite_reservable(block_id: str, *, block: Callable[[str], Block], index: Index,
                      terms: list[str], doc_id: str, cohorte: list[str],
                      budget: RetrievalBudget, settings: Settings,
                      related_cache: dict[str, list[str]]) -> list[str] | None:
    """L'unité atomique qu'une facette ferait garder pour son meilleur candidat décisionnel.

    C'est **exactement** celle que la navigation admettrait pour ce bloc — même fermeture d'un
    niveau, mêmes dépendances directes, même règle de primaire structurel. Une réserve calculée sur
    autre chose que ce qui sera réellement admis serait une réserve fausse : elle garderait une
    place d'une taille que personne ne dépenserait.

    `None` quand le bloc n'appartient pas au document servi ou ne forme aucune unité transmissible.
    """
    try:
        dependances = _dependances_directes(
            block_id, block=block, index=index, terms=terms, doc_id=doc_id,
            search_candidates=cohorte, related_limit=budget.search_limit,
            related_max=settings.limite_liee_max,
            proximity_min=settings.limite_liee_proximite_min,
            related_cache=related_cache, search_related=True)
        return _unite_primaire(block_id, kind=block(block_id).kind, index=index,
                               dependances=dependances, block=block, settings=settings)
    except KeyError:
        return None


def _forme_gagnante(mapping: dict[str, list[str]] | list[str], *,
                    tokens: set[str]) -> str | None:
    """La forme de la requête que le bloc de tête couvre **entièrement** : celle qui l'a fait gagner.

    Instrumentation seule (C5). C'est ce mot-là qui explique un classement, et c'est lui qu'un audit
    doit lire au lieu de le redériver : « fumées » désigne la clause, « vitres » désigne la collision
    entre le pluriel d'un nom et un participe que la normalisation confond.
    """
    groupes = (((canon, *variantes) for canon, variantes in mapping.items())
               if isinstance(mapping, dict) else (mapping,))
    for groupe in groupes:
        for valeur in groupe:
            mots = set(words(normalize(valeur)))
            if mots and mots <= tokens:
                return " ".join(sorted(mots))
    return None


def _couverture_facettes(mappings: list[tuple[int, dict[str, list[str]] | list[str]]], *,
                         classement: Callable[[dict[str, list[str]] | list[str]],
                                              list[ScoredHit]],
                         admis: set[str],
                         tokens_du_bloc: Callable[[str], set[str]] | None = None,
                         part_du_mot: Callable[[str], float] | None = None,
                         tokens_reserves: dict[int, int] | None = None,
                         tokens_admis: dict[int, int] | None = None) -> list[FacetteCouverture]:
    """Pour chaque facette : les blocs décisionnels confirmés **transmis** que son classement propose.

    L'attribution est structurelle, et c'est ce qui la rend démontrable : un bloc couvre une facette
    parce que le classement de cette facette l'a proposé et que la lecture l'a réellement transmis
    — la même provenance que celle qui a servi à aller le chercher. Aucune heuristique sur le
    vocabulaire des clauses n'intervient ; le kind vient de l'ingestion et sa confirmation aussi.

    Une facette dont aucun bloc proposé n'a été transmis rend une couverture **vide**, et cette
    couverture vide est une affirmation : « rien de décisionnel n'a été retrouvé pour cette
    sous-question ». C'est la déclaration d'absence que la chaîne aval exige pour ne pas rendre une
    réponse muette sur une moitié de la question.
    """
    couverture: list[FacetteCouverture] = []
    for rang, mapping in mappings:
        hits = classement(mapping)
        tete = hits[0].clause_uid if hits else None
        forme = (_forme_gagnante(mapping, tokens=tokens_du_bloc(tete))
                 if tete is not None and tokens_du_bloc is not None else None)
        couverture.append(FacetteCouverture(
            rang=rang,
            block_ids=tuple(dict.fromkeys(hit.clause_uid for hit in hits
                                          if hit.clause_uid in admis)),
            # Le classement entier, admis ou non : c'est lui qui distingue « le contrat n'en parle
            # pas » de « je n'ai pas eu la place de le lire » (NFR2).
            candidats=len({hit.clause_uid for hit in hits}),
            tete=tete, forme_gagnante=forme,
            part_des_blocs=(part_du_mot(forme) if forme is not None and part_du_mot is not None
                            else 0.0),
            tokens_reserves=(tokens_reserves or {}).get(rang, 0),
            tokens_admis=(tokens_admis or {}).get(rang, 0)))
    return couverture


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
        # G2 : la taille de page et l'aperçu servi sont dérivés par l'index de la forme du
        # document et du budget de contexte ; aucun nombre d'entrées n'est imposé ici.
        summary_page = index.sommaire_page(
            doc_id, page_max_chars=settings.summary_page_max_chars,
            slice_max_chars=settings.summary_slice_max_chars,
            apercu_max_chars=settings.summary_apercu_max_chars) if summary_ready else None
        return render_prompt(
            "retrouver", doc_id=doc_id, max_llm_turns=budget.max_llm_turns,
            max_opens=budget.max_opens, profil_max_opens=budget.profil_max_opens,
            sommaire=untrusted(
                "sommaire", json.dumps(summary_page.model_dump(mode="json"), ensure_ascii=False)
                if summary_page is not None else ""))
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
    context_role_by_block: dict[str, str] = {}
    search_candidates: list[str] = []
    tool_search_candidates: list[str] = []
    tool_search_mappings: list[dict[str, list[str]] | list[str]] = []
    search_runs: list[list[str]] = []
    scored_hits: list[ScoredHit] = []
    # L'identité **canonique** d'une clause : la première vue, celle qui sert au classement et à la
    # priorité de focus. `hits_by_block` accumule, lui, **toutes** ses identités : c'est ce qui
    # permet d'admettre la clause pour chacune d'elles (voir `record_admitted`).
    hit_by_block: dict[str, ScoredHit] = {}
    hits_by_block: dict[str, list[ScoredHit]] = {}
    admission_by_result: dict[str, AdmissionDecision] = {}
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
    canonical_question = parsed.question_resolue.strip() or " ".join(parsed.termes_de_recherche())
    classement_facettes: Callable[[dict[str, list[str]] | list[str]],
                                  list[ScoredHit]] | None = None
    # Les requêtes de facette **telles que la passe de couverture les a vues**. La mesure finale les
    # relit plutôt que de les recalculer : les mécanismes sont des phases, et le dictionnaire peut
    # s'armer après la phase `outils`. Mesurer avec une expansion que la passe n'avait pas aurait
    # publié « des candidats sont restés fermés » sur des candidats qui n'existaient pas encore
    # quand elle a ouvert — une borne inventée après coup.
    mappings_figes: list[tuple[int, dict[str, list[str]] | list[str]]] | None = None
    # Correctif du tour 2 (R1) : la place que la lecture **garde** pour l'unité décisionnelle de
    # chaque facette, `block_id` du primaire → son unité atomique. Tant qu'un primaire réservé n'est
    # pas admis, sa place n'est dépensable par personne d'autre : ni par un voisin de fenêtre, ni
    # par une définition suivie automatiquement. Ce n'est **pas** une capacité de plus — c'est la
    # même, allouée dans l'ordre de ce qui décide.
    # `block_id` du primaire réservé → (rang de la sous-question, unité atomique gardée).
    reserve_facettes: dict[str, tuple[int, list[str]]] = {}
    # C5, instrumentation seule : ce que chaque sous-question a **réellement** fait entrer. Honorer
    # une réservation ouvre une fenêtre, pas une unité ; l'écart entre les deux n'était mesurable
    # nulle part, et il gouvernait une dépense bien plus grande que le nombre qui la bornait.
    tokens_admis_par_rang: dict[int, int] = {}
    # Une unité refusée par le budget est un fait distinct d'un candidat que le navigateur a choisi
    # de ne pas ouvrir : la trace ne doit pas dire l'un pour l'autre.
    budget_a_refuse = False

    def block(block_id: str) -> Block:
        if index.doc_of(block_id) != doc_id:
            raise KeyError(block_id)
        return document.block(block_id)

    def _decisionnel_confirme(block_id: str) -> bool:
        """Ce que le corpus typé dit d'un bloc, jamais ce que son texte a l'air de dire."""
        try:
            candidat = block(block_id)
        except KeyError:
            return False
        return candidat.kind in KINDS_DECISIONNELS and candidat.kind_confirmed

    def budget_snapshot() -> BudgetSnapshot:
        return BudgetSnapshot(
            opens_used=opens, blocks_used=blocks_used, tokens_used=tokens_used,
            opens_remaining=max(0, budget.max_opens - opens),
            blocks_remaining=(None if budget.max_blocks is None else
                              max(0, budget.max_blocks - blocks_used)),
            tokens_remaining=(None if budget.max_tokens is None else
                              max(0, budget.max_tokens - tokens_used)),
        )

    def record_admitted(block_id: str, reason: str) -> None:
        """Correctif du tour 2 (rapport citations, B) : une clause admise l'est pour **toutes** ses
        identités de résultat.

        `result_uid` dérive du `question_uid`, donc des termes de la requête : deux `chercher` aux
        termes différents produisent **deux identités pour la même clause**. La passe mécanique
        `sommaire` tourne avant la phase `outils` et amorçait `hit_by_block` avec une identité que le
        navigateur ne voyait jamais ; seule celle-là était admise, et l'identité que le modèle
        désignait à la fin recevait `rejected`. `RetrievalResult` valide la suffisance au niveau du
        `result_uid` et levait alors une `ValidationError` — **hors** de tout `PipelineError`, donc
        un 500 nu sur le chemin servi, sans trace partielle, après une minute payée.

        « Ce bloc est entré dans le contexte » est un fait de corpus : il ne peut pas être vrai pour
        une empreinte de requête et faux pour une autre.
        """
        for hit in hits_by_block.get(block_id, []):
            if hit.result_uid in admission_by_result:
                continue
            # Un candidat préparé par un mécanisme devient réellement présenté lorsque son bloc entre
            # dans le résultat d'ouverture ; c'est cette frontière, et elle seule, qui publie son hit.
            if hit.result_uid not in {existing.result_uid for existing in scored_hits}:
                scored_hits.append(hit)
            admission_by_result[hit.result_uid] = AdmissionDecision(
                result_uid=hit.result_uid, state="admitted", reason=reason,
                snapshot=budget_snapshot())

    def cout_des_blocs(block_ids: Iterable[str]) -> int:
        """Le coût en tokens de blocs **déjà admis** : la même représentation que `admit`.

        `cout_unite` ne compte que ce qui reste à admettre — c'est ce qu'il faut avant l'admission,
        et exactement ce qu'il ne faut pas après (il rendrait zéro).
        """
        return _cout_des_blocs(block_ids, block=block, settings=settings)

    def cout_unite(unit: Iterable[str]) -> tuple[int, int]:
        """Ce qu'une unité coûterait **en plus** : blocs et tokens, doublons déjà lus déduits."""
        neufs = [b for b in dict.fromkeys(unit) if b not in admitted_set]
        return len(neufs), sum(estimate_tokens(f"{b}\n{block(b).text}", settings) for b in neufs)

    def reserve_restante() -> tuple[int, int]:
        """La part du budget encore gardée pour des unités décisionnelles non lues.

        Recalculée à chaque admission plutôt que décomptée : un primaire réservé qu'une fenêtre a
        fini par admettre d'elle-même libère sa place **au moment même**, et un membre d'unité déjà
        lu ailleurs ne la fait pas payer deux fois. Une place réservée qui reste vide à la fin n'est
        pas perdue : plus rien n'admet après la passe de couverture, qui est la seule à pouvoir la
        dépenser.
        """
        blocs = tokens = 0
        for primaire, (_rang, unite) in reserve_facettes.items():
            if primaire in admitted_set:
                continue
            cout_blocs, cout_tokens = cout_unite(unite)
            blocs += cout_blocs
            tokens += cout_tokens
        return blocs, tokens

    def admit(unit: list[str], *, reserve: bool = False) -> list[str]:
        """Admet une unité atomique sous les deux budgets ; rend ses nouveaux blocs.

        `reserve` dit que cette unité **est** celle qu'une facette avait fait garder : elle seule
        peut dépenser la part gardée, et elle la libère en la dépensant.
        """
        nonlocal blocks_used, tokens_used, truncated, budget_a_refuse
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
        # La part gardée pour les unités décisionnelles encore non lues n'est retirée du plafond que
        # pour les **autres** admissions ; l'unité réservée, elle, voit le budget entier. Ce sont
        # les mêmes bornes qu'avant : `max_blocks` et `max_tokens` ne bougent pas d'un cran.
        blocs_gardes, tokens_gardes = (0, 0) if reserve else reserve_restante()
        if (budget.max_blocks is not None
                and blocks_used + len(new) > budget.max_blocks - blocs_gardes):
            truncated = True
            budget_a_refuse = True
            return []
        if (budget.max_tokens is not None
                and tokens_used + token_cost > budget.max_tokens - tokens_gardes):
            truncated = True
            budget_a_refuse = True
            return []
        blocks_used += len(new)
        tokens_used += token_cost
        for b in new:
            admitted_set.add(b)
            admitted.append(b)
            record_admitted(b, "admitted_by_exact_unit")
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

    def execute(name: str, args: object, *, mechanism: bool = False,
                prioritize_focus: bool = False,
                unite_seule: bool = False) -> tuple[dict[str, Any], bool]:
        nonlocal opens, truncated, valid_window_attempted
        if not isinstance(args, dict):
            return invalid()
        if name == "sommaire":
            if not set(args) <= {"doc_id", "cursor"} or args.get("doc_id") != doc_id:
                return invalid()
            cursor = args.get("cursor", 0)
            if isinstance(cursor, bool) or not isinstance(cursor, int):
                return invalid()
            try:
                page = index.sommaire_page(
                    doc_id, cursor=cursor, page_max_chars=settings.summary_page_max_chars,
                    slice_max_chars=settings.summary_slice_max_chars,
                    apercu_max_chars=settings.summary_apercu_max_chars)
            except ValueError:
                return invalid()
            return page.model_dump(mode="json"), False
        if name == "chercher":
            termes = _strings(args.get("termes"))
            if set(args) != {"termes"} or not termes:
                return invalid()
            mapping: dict[str, list[str]] | list[str] = termes
            if dictionary_ready:
                mapping = dictionnaire.expand(
                    termes,
                    part_du_mot=part_du_mot_borne(
                        index, doc_id, part_max=settings.dictionnaire_variante_max_part),
                    part_max=settings.dictionnaire_variante_max_part)
            if not mechanism:
                mapping_effectif = ({canon: list(variantes) for canon, variantes in mapping.items()}
                                     if isinstance(mapping, dict) else list(mapping))
                if mapping_effectif not in tool_search_mappings:
                    tool_search_mappings.append(mapping_effectif)
            for terme in termes:
                if forme(terme) not in {forme(t) for t in searched_terms}:
                    searched_terms.append(terme)
                if (dictionary_ready
                        and forme(terme) not in {forme(t) for t in dictionary_searched_terms}):
                    dictionary_searched_terms.append(terme)
            run_reservations: list[tuple[str, str]] = []
            hits = index.chercher(mapping, limit=budget.search_limit + 1, doc_id=doc_id,
                                  question=canonical_question,
                                  groupes_prioritaires=[requete for _rang, requete
                                                        in mappings_par_rang()],
                                  reservations_out=run_reservations)
            search_truncated = len(hits) > budget.search_limit
            if search_truncated:
                truncated = True
                hits = hits[:budget.search_limit]
            for reservation in run_reservations:
                if any((hit.clause_uid, hit.node_uid) == reservation for hit in hits) \
                        and reservation not in reserved_candidates:
                    reserved_candidates.append(reservation)
            for block_id, hit_node_id in hits:
                best_hit_by_node.setdefault(hit_node_id, block_id)
                if block_id not in search_candidates:
                    search_candidates.append(block_id)
                if not mechanism and block_id not in tool_search_candidates:
                    tool_search_candidates.append(block_id)
            for hit in hits:
                if (not mechanism
                        and hit.result_uid not in {existing.result_uid for existing in scored_hits}):
                    scored_hits.append(hit)
                hit_by_block.setdefault(hit.clause_uid, hit)
                identites = hits_by_block.setdefault(hit.clause_uid, [])
                if all(connue.result_uid != hit.result_uid for connue in identites):
                    identites.append(hit)
            search_runs.append([block_id for block_id, _node_id in hits])
            return {"candidats": [{
                        "result_uid": hit.result_uid,
                        "block_id": hit.clause_uid,
                        "node_id": hit.node_uid,
                        "title": hit.title,
                        "excerpt": hit.excerpt,
                        "score": hit.score.model_dump(mode="json"),
                    } for hit in hits],
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
            # **F1 : ouvrir l'amorce d'une énumération, c'est l'ouvrir tout entière.** Les items
            # sont des nœuds *enfants*, donc hors de la fenêtre du nœud : le navigateur qui ouvrait
            # « Étendue de la garantie » ne recevait que son titre et « contre les périls
            # suivants : », et repartait convaincu d'avoir lu la garantie. Une seule ouverture, un
            # seul `open` compté : c'est la même fenêtre, rendue complète.
            membres = _membres_denumeration(
                [item.block_id for item in window_blocks], focus=focus, index=index,
                block=block, settings=settings)
            deja = {item.block_id for item in window_blocks}
            window_blocks.extend(block(b) for b in membres if b not in deja)
            context_role_by_block.update({
                item.block_uid: item.role for item in window.context_units
                if item.role != "target"
            })
            window_ids = [item.block_id for item in window_blocks]
            window_block_by_id = {item.block_id: item for item in window_blocks}
            focus_companions = (set(index.unite_de_renvoi(focus)[1:])
                                if focus is not None else set())
            primary_ids = [
                item.block_id for item in window_blocks
                if not (item.block_id in focus_companions
                        and item.block_id not in relevant_candidates)
            ]
            # Le rappel garde une autorité first-wins par nœud, mais ne peut évincer un focus dont
            # le score canonique est strictement meilleur. Une égalité conserve l'autorité
            # historique ; les complétions décisionnelles peuvent explicitement forcer la priorité.
            focus_hit = hit_by_block.get(focus) if focus is not None else None
            best_id = best_hit_by_node.get(node_id)
            canonical_focus = focus_hit.score if focus_hit is not None else None
            best_hit = hit_by_block.get(best_id) if best_id is not None else None
            canonical_best = best_hit.score if best_hit is not None else None
            focus_reserve = bool(
                focus is not None and (
                    prioritize_focus
                    or focus == best_id
                    or (canonical_focus is not None and canonical_best is not None
                        and canonical_focus.sort_key < canonical_best.sort_key)
                )
            )
            # Correctif du tour 2 (R1) : **dans une fenêtre, ce qui décide passe avant ce qui
            # éclaire.** Le focus garde sa tête quand il est réservé ; derrière lui, les primaires
            # dont le corpus confirme un kind décisionnel passent avant les voisins de contexte,
            # l'ordre documentaire étant conservé à l'intérieur de chaque groupe (tri stable). Cet
            # ordre ne décide que **qui tient sous le budget** : le rendu, lui, retrouve plus bas
            # l'ordre documentaire de la fenêtre.
            # **Correctif du tour 5 (C10) : la fenêtre est ce qui est dépensé, donc c'est elle qui
            # se compte.** Le compteur du tour 4 n'additionnait que l'unité gardée du primaire
            # réservé ; les frères qui entrent avec elle étaient admis par des `admit()` distincts
            # et n'étaient attribués à personne. Mesuré : le check publiait « 99 tokens gardés pour
            # 99 admis » quand la fenêtre réellement ouverte en faisait **1 022**. L'instrumentation
            # ratait exactement le chiffre pour lequel elle avait été écrite, et un audit qui lui
            # faisait confiance concluait que la réserve était bien calibrée.
            rang_du_focus = (reserve_facettes[focus][0]
                             if focus is not None and focus in reserve_facettes else None)
            ordre = _prioriser_focus(primary_ids, focus, reserve=focus_reserve)
            tete = ordre[:1] if (focus_reserve and ordre and ordre[0] == focus) else []
            admission_ids = [
                *tete,
                *sorted(ordre[len(tete):],
                        key=lambda block_id: 0 if _decisionnel_confirme(block_id) else 1),
            ]
            if unite_seule and focus is not None:
                # **F2 : honorer une réservation, c'est lire son unité, pas le nœud qui la porte.**
                # La réservation garde la place d'une unité — mesuré 99 tokens — et l'honoration
                # ouvrait la fenêtre entière du nœud : 829 à 1 889 tokens admis par run, dont sept
                # blocs de dégâts des eaux pour un bloc réservé hors sujet, pendant que 13 à 15
                # candidats du navigateur restaient non lus faute de budget. L'unité, elle, entre
                # atomiquement par `admit()` — amorce et items compris (C9, F1) —, les frères que
                # la **recherche du navigateur** a proposés restent admissibles (eux, quelqu'un les a
                # demandés) et le **titre** de la section aussi : il coûte quelques tokens et situe la
                # clause. Ce qui est retiré est le corps du voisinage, que personne n'a demandé et
                # que la fenêtre apportait par le seul fait d'exister.
                garde = [block_id for block_id in admission_ids
                         if block_id == focus or block_id in relevant_candidates
                         or block(block_id).kind == "heading"]
                if len(garde) < len(admission_ids):
                    # Le nœud a été ouvert et n'est pas lu en entier : la lecture est bornée et se
                    # dit, exactement comme la fenêtre le déclare déjà dans sa charge utile
                    # (`truncated` y vaut « un bloc de la fenêtre n'a pas été admis »).
                    truncated = True
                admission_ids = garde
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
                    item.block_id, kind=item.kind, index=index, dependances=dependencies,
                    block=block, settings=settings)
                    if item.block_id == focus else [item.block_id, *dependencies])
                if unit is None:
                    truncated = True
                    continue
                reservee = reserve_facettes.get(item.block_id)
                got = admit(unit, reserve=reservee is not None)
                if reservee is not None and got and reservee[0] != rang_du_focus:
                    # Une seconde réservation qui se trouve dans la fenêtre d'une autre n'a pas payé
                    # l'ouverture : elle ne porte que son unité. La fenêtre est comptée une fois,
                    # là où elle a été ouverte.
                    tokens_admis_par_rang[reservee[0]] = (
                        tokens_admis_par_rang.get(reservee[0], 0) + cout_des_blocs(got))
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
            if rang_du_focus is not None and newly:
                # L'ouverture a eu lieu **pour** cette réservation : tout ce qui est entré avec elle
                # est la dépense qu'elle a ordonnée, l'unité gardée comme ses voisins.
                tokens_admis_par_rang[rang_du_focus] = (
                    tokens_admis_par_rang.get(rang_du_focus, 0) + cout_des_blocs(newly))
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
                execute("ouvrir_noeud", {"node_id": node_id, "focus_block_id": block_id},
                        unite_seule=True)

    def mappings_par_rang() -> list[tuple[int, dict[str, list[str]] | list[str]]]:
        """Les requêtes de facette, avec leur rang, dans l'état **effectif** du dictionnaire.

        Relues à chaque usage plutôt que figées au démarrage : `dictionary_ready` n'est vrai
        qu'après le mécanisme `dictionnaire`, et l'ordre des mécanismes est une configuration
        (AD-1, amendement). Une liste figée aurait fait dépendre l'expansion d'un ordre de calcul
        plutôt que de l'ordre déclaré.
        """
        return _mappings_facettes(parsed.facettes, dictionnaire=dictionnaire,
                                  dictionary_ready=dictionary_ready, index=index, doc_id=doc_id,
                                  variante_max_part=settings.facette_variante_max_part,
                                  dictionnaire_max_part=settings.dictionnaire_variante_max_part)

    def classement_des_facettes() -> Callable[[dict[str, list[str]] | list[str]],
                                              list[ScoredHit]]:
        """Un seul classement par requête de facette pour toute l'étape, ouverture et mesure comprises."""
        nonlocal classement_facettes
        if classement_facettes is None:
            classement_facettes = _classement_par_facette(
                index=index, doc_id=doc_id, question=canonical_question,
                kinds_confirmes=kinds_suffisants or frozenset(),
                limit=budget.search_limit)
        return classement_facettes

    def suffisance_atteinte() -> bool:
        return any(
            block_id in hit_by_block
            and (hit_by_block[block_id].score.full_matches > 0
                 or hit_by_block[block_id].score.partial_numerator > 0)
            and block(block_id).kind not in _KINDS_CONTEXTUELS
            and _score_positif(
                hit_by_block[block_id].score,
                question=canonical_question,
                clause=block(block_id).text,
            )
            and (kinds_suffisants is None
                 or (block(block_id).kind in kinds_suffisants
                     and block(block_id).kind_confirmed))
            for block_id in admitted)

    def complete_search_candidates_for_sufficiency() -> None:
        nonlocal truncated
        # Le navigateur peut honnêtement conclure après l'ouverture qu'impose son prompt, même si
        # celle-ci n'a pas encore satisfait le besoin déclaré par l'appelant. Ses propres hits
        # restent alors le seul classement autorisé : on les essaie dans leur ordre initial, après
        # les réservations et via l'outil commun, afin de conserver fenêtres, dépendances,
        # atomicité et budgets sans capacité cachée. Sans besoin déclaré, le comportement
        # historique reste strictement limité à une lecture exclusivement contextuelle.
        mappings_facettes_effectifs = _mappings_dedupes(mappings_par_rang())

        suffisance_initiale = suffisance_atteinte()
        # Sans facette, le replay décisionnel acquis sur la baseline reste disponible depuis la
        # requête effective. Une facette unique conserve son arrêt historique ; plusieurs facettes
        # activent en plus leurs sources fortes dédupliquées.
        recherche_outils_effective = bool(tool_search_mappings)
        replay_apres_suffisance = (
            kinds_suffisants is not None
            and len(mappings_facettes_effectifs) != 1
            and recherche_outils_effective
        )
        if kinds_suffisants is None:
            completion_necessaire = bool(admitted) and all(
                block(block_id).kind in _KINDS_CONTEXTUELS for block_id in admitted)
        else:
            completion_necessaire = not suffisance_initiale
        if not completion_necessaire and not replay_apres_suffisance:
            return
        if completion_necessaire:
            candidats_a_completer = tool_search_candidates
            prioritaires_ids: set[str] = set()
            candidats_disponibles = [
                block_id for block_id in tool_search_candidates
                if block_id not in admitted_set and block_id not in focused_windows_attempted
            ]
            # La contention porte sur ce que les appels consommeront réellement : chaque focus
            # restant coûte une ouverture, même si plusieurs focus appartiennent au même nœud, et
            # une fenêtre peut admettre plusieurs primaires ou dépendances. Cette prélecture
            # déterministe ne réserve aucune capacité ; elle mesure l'union des blocs que les
            # mêmes fenêtres et unités atomiques présenteraient à `admit()`.
            blocs_potentiels: set[str] = set()
            cache_prelecture = dict(related_cache)
            for candidat_id in candidats_disponibles:
                node_id = document.node_of(candidat_id)
                window = index.ouvrir_noeud(
                    node_id, focus_block_id=candidat_id, node_window=budget.node_window)
                relevant_candidates: list[str] = []
                for run in search_runs:
                    if candidat_id in run:
                        relevant_candidates.extend(
                            candidate for candidate in run
                            if candidate not in relevant_candidates)
                focus_companions = set(index.unite_de_renvoi(candidat_id)[1:])
                for item in window.blocks:
                    if (item.block_id in focus_companions
                            and item.block_id not in relevant_candidates):
                        continue
                    dependencies = _dependances_directes(
                        item.block_id, block=block, index=index, terms=terms, doc_id=doc_id,
                        search_candidates=relevant_candidates,
                        related_limit=budget.search_limit,
                        related_max=settings.limite_liee_max,
                        proximity_min=settings.limite_liee_proximite_min,
                        related_cache=cache_prelecture,
                        search_related=(
                            item.block_id == candidat_id
                            or item.block_id in relevant_candidates),
                    )
                    unit = (_unite_primaire(
                        item.block_id, kind=item.kind, index=index, dependances=dependencies,
                        block=block, settings=settings)
                        if item.block_id == candidat_id
                        else [item.block_id, *dependencies])
                    if unit is not None:
                        blocs_potentiels.update(unit)
            blocs_potentiels.difference_update(admitted_set)
            # Même représentation et même tokenizer que ``admit`` : identifiant, saut de ligne,
            # texte. La déduplication précède le calcul, donc un contexte partagé ne paie qu'une fois.
            cout_tokens_potentiel = sum(
                estimate_tokens(f"{block_id}\n{block(block_id).text}", settings)
                for block_id in blocs_potentiels)
            capacite_contestee = (
                (budget.max_blocks is not None
                 and len(blocs_potentiels) > budget.max_blocks - blocks_used)
                or len(candidats_disponibles) > budget.max_opens - opens
                or (budget.max_tokens is not None
                    and cout_tokens_potentiel > budget.max_tokens - tokens_used)
            )
            if kinds_suffisants is not None and capacite_contestee:
                # La complétion est explicitement chargée d'atteindre l'un de ces kinds. Une
                # partition stable présente les hits confirmés avant les autres seulement quand
                # les places blocs ou ouvertures restantes ne peuvent pas accueillir tous les
                # candidats. Sans compétition, l'ordre historique reste intact et tous les hits
                # conservent leur capacité disponible.
                candidats_prioritaires = [
                    block_id for block_id in tool_search_candidates
                    if block(block_id).kind in kinds_suffisants
                    and block(block_id).kind_confirmed
                ]
                prioritaires_ids = set(candidats_prioritaires)
                candidats_a_completer = [
                    *candidats_prioritaires,
                    *(block_id for block_id in tool_search_candidates
                      if block_id not in prioritaires_ids),
                ]
            for block_id in candidats_a_completer:
                if block_id in admitted_set or block_id in focused_windows_attempted:
                    continue
                if suffisance_atteinte() and kinds_suffisants is None:
                    return
                # La priorité change seulement l'ordre. Elle ne crée aucune capacité et ne retire
                # aucun hit qui tient encore réellement sous les quotas existants.
                if opens >= budget.max_opens:
                    break
                if budget.max_blocks is not None and blocks_used >= budget.max_blocks:
                    break
                node_id = document.node_of(block_id)
                _payload, is_error = execute(
                    "ouvrir_noeud", {"node_id": node_id, "focus_block_id": block_id},
                    prioritize_focus=block_id in prioritaires_ids)
                if is_error:
                    return

        # Une suffisance explicitement déclarée autorise ensuite un rappel borné, mais seulement
        # après l'épuisement des hits bruts du navigateur. On rejoue d'abord les requêtes effectives
        # réellement classées — expansion dictionnaire comprise — en filtrant avant `limit` sur les
        # kinds confirmés. Cette seconde vue ne fabrique aucune pertinence : elle conserve le score
        # et l'ordre canoniques de l'index, tout en empêchant les auxiliaires de consommer la coupe.
        if kinds_suffisants is None:
            return

        def classement_disponible(
                mapping: dict[str, list[str]] | list[str]) -> list[tuple[str, str]]:
            # Les doublons admis ou déjà tentés ne sont pas des candidats à présenter et ne
            # consomment donc aucune place de la coupe globale. Le surclassement local compense
            # seulement ces identifiants connus ; le nombre de candidats retenus reste borné par
            # `search_limit`.
            indisponibles = admitted_set | focused_windows_attempted
            hits = index.chercher(
                mapping, limit=budget.search_limit + len(indisponibles), doc_id=doc_id,
                question=canonical_question,
                kinds_confirmes=kinds_suffisants)
            return [
                hit for hit in hits if hit[0] not in indisponibles
            ][:budget.search_limit]

        # Les facettes ont été arrêtées en amont par *comprendre*. Pour une question réellement
        # multifacette et après au moins une recherche du navigateur, chacune conserve donc sa
        # propre source forte, avec le même état effectif du dictionnaire que le tour outils.
        sources_facettes = (
            mappings_facettes_effectifs
            if len(mappings_facettes_effectifs) > 1 and recherche_outils_effective
            else [])
        sources_faibles = list(tool_search_mappings)

        # Le repli lexical acquis sur les auxiliaires demeure ensuite disponible. Ses graines
        # viennent uniquement des hits qu'il a effectivement fait admettre ; ni un voisin de
        # fenêtre, ni un candidat jamais lu ne peut orienter cette recherche. Les mots-outils déjà
        # neutralisés pour les liens de clauses ne suffisent pas à créer une correspondance.
        for block_id in tool_search_candidates:
            if block_id not in admitted_set:
                continue
            candidat = block(block_id)
            if candidat.kind in kinds_suffisants and candidat.kind_confirmed:
                continue
            graines: list[str] = []
            for mot in forme(candidat.text).split():
                if mot.isalnum() and mot not in _MOTS_OUTILS_LIMITES and mot not in graines:
                    graines.append(mot)
            if graines:
                sources_faibles.append(graines)
        if not sources_facettes and not sources_faibles:
            return

        # Chaque source conserve son propre classement : concaténer les requêtes ou les textes
        # diluerait un lien sous les signaux des autres. La fusion équitable prépare tout le cohort
        # encore présentable avant d'en ouvrir le premier : une limite classée après sa garantie
        # participe ainsi à son unité atomique. Après chaque fenêtre, le cohort est recalculé ; un
        # candidat admis comme dépendance ne consomme donc aucune place et libère la suivante.
        presentes = 0
        presentes_ids: set[str] = set()
        presentes_ordre: list[str] = []
        run: list[str] = []

        def fusionner_sources(
                sources: list[dict[str, list[str]] | list[str]], limite: int,
                exclus: set[str]) -> list[tuple[str, str]]:
            if limite <= 0 or not sources:
                return []
            retenus: list[tuple[str, str]] = []
            retenus_ids = set(exclus)
            classements = [classement_disponible(mapping) for mapping in sources]
            while len(retenus) < limite:
                progression = False
                for classement in classements:
                    suivant = next(
                        (hit for hit in classement if hit[0] not in retenus_ids), None)
                    if suivant is None:
                        continue
                    retenus.append(suivant)
                    retenus_ids.add(suivant[0])
                    progression = True
                    if len(retenus) == limite:
                        break
                if not progression:
                    break
            return retenus

        def cohorte_disponible() -> list[tuple[str, str]]:
            restante = budget.search_limit - presentes
            fortes = fusionner_sources(sources_facettes, restante, presentes_ids)
            fortes_ids = {block_id for block_id, _node_id in fortes}
            faibles = fusionner_sources(
                sources_faibles, restante - len(fortes), presentes_ids | fortes_ids)
            return [*fortes, *faibles]

        # Les sources fortes remplissent d'abord la coupe, équitablement entre facettes. Les
        # requêtes variables puis les graines auxiliaires ne reçoivent que la capacité restante.
        run_enregistre = False
        cohorte_precedente: list[tuple[str, str]] = []
        while presentes < budget.search_limit:
            nouvelle_cohorte = cohorte_disponible()
            # L'ouverture précédente ne remet pas la fusion au début : les candidats encore
            # disponibles gardent leur rang, seuls ceux admis ou tentés sont remplacés.
            indisponibles = admitted_set | focused_windows_attempted | presentes_ids
            cohorte = [
                hit for hit in cohorte_precedente if hit[0] not in indisponibles
            ]
            cohorte_ids = {block_id for block_id, _node_id in cohorte}
            cohorte.extend(
                hit for hit in nouvelle_cohorte if hit[0] not in cohorte_ids)
            cohorte = cohorte[:budget.search_limit - presentes]
            if not cohorte:
                break
            block_id, node_id = cohorte[0]
            # Les candidats au-delà de la capacité restante ne sont jamais présentés à la
            # recherche. Ils ne créent aucune fausse troncature après suffisance ; une
            # insuffisance encore ouverte reste, elle, honnêtement tronquée.
            if opens >= budget.max_opens:
                if not suffisance_atteinte():
                    truncated = True
                return
            if not run_enregistre:
                search_runs.append(run)
                run_enregistre = True
            run[:] = [*presentes_ordre, *(block_id for block_id, _node_id in cohorte)]
            presentes_ids.add(block_id)
            presentes_ordre.append(block_id)
            presentes += 1
            best_hit_by_node.setdefault(node_id, block_id)
            if block_id not in search_candidates:
                search_candidates.append(block_id)
            _payload, is_error = execute(
                "ouvrir_noeud", {"node_id": node_id, "focus_block_id": block_id},
                prioritize_focus=True)
            if is_error:
                return
            cohorte_precedente = cohorte[1:]

    def reserver_les_facettes() -> None:
        """Garde, **avant** la navigation, la place de l'unité décisionnelle de chaque facette.

        Correctif du tour 2 (cause R1). L'ordre d'admission était exactement inverse de l'ordre des
        priorités : les voisins de fenêtre et les définitions suivies automatiquement — hors quota
        d'ouvertures mais **pleinement comptés en tokens** — étaient admis pendant les tours du
        navigateur, et la passe de couverture par facette ne trouvait plus rien à dépenser. Sur les
        trois runs A16, le budget de tokens était consommé à 99,8 % quand la clause manquante en
        coûtait 210.

        La correction ne change aucun plafond : elle **réalloue**. Pour chaque facette, le meilleur
        candidat de son propre classement restreint aux kinds décisionnels confirmés voit sa place
        gardée ; personne d'autre ne peut la dépenser tant qu'il n'est pas lu, et il la libère en
        étant lu — y compris quand c'est le navigateur qui l'ouvre de lui-même.

        La réserve est **bornée par construction** : au plus une unité par facette, et
        `question_max_facettes` borne déjà le nombre de facettes. Elle ne peut pas saturer l'étape :
        une unité dont la place gardée dépasserait à elle seule ce qui reste du budget n'est pas
        gardée du tout — mieux vaut une lecture large qu'une réserve que rien ne pourra honorer.
        """
        nonlocal mappings_figes
        if kinds_suffisants is None:
            return
        mappings = mappings_par_rang()
        if len(_mappings_dedupes(mappings)) < FACETTES_MIN_POUR_COUVERTURE:
            return
        mappings_figes = mappings
        classement = classement_des_facettes()
        for rang, mapping in mappings:
            for hit in classement(mapping):
                if hit.clause_uid in admitted_set or hit.clause_uid in reserve_facettes:
                    break  # cette facette a déjà sa place, gardée ou déjà lue
                unite = _unite_reservable(
                    hit.clause_uid, block=block, index=index, terms=terms, doc_id=doc_id,
                    cohorte=[h.clause_uid for h in classement(mapping)],
                    budget=budget, settings=settings, related_cache=related_cache)
                if unite is None:
                    continue
                blocs_gardes, tokens_gardes = reserve_restante()
                cout_blocs, cout_tokens = cout_unite(unite)
                part = settings.facette_reserve_max_part
                if ((budget.max_blocks is not None
                     and blocs_gardes + cout_blocs > int(budget.max_blocks * part))
                        or (budget.max_tokens is not None
                            and tokens_gardes + cout_tokens > int(budget.max_tokens * part))):
                    # **La réserve ordonne la lecture, elle ne la remplace pas.** Au-delà de sa
                    # part, garder une place de plus reviendrait à évincer entièrement ce que le
                    # navigateur choisit — et une unité gardée qu'on ne pourrait pas honorer
                    # retirerait du budget sans rien rendre. La facette repartira par la passe de
                    # couverture, sous les bornes ordinaires, ou sera dite bornée.
                    #
                    # **Correctif du tour 5 (C7) : la borne refuse ce candidat-ci, pas la facette.**
                    # Le `break` lisait « cette unité ne tient pas sous la part » comme « cette
                    # sous-question n'a rien à garder », alors que le commentaire ci-dessus ne
                    # justifie que la borne. Mesuré : une sous-question dont la tête pesait 3 360
                    # tokens abandonnait tout, et le quatrième candidat de son **propre** classement
                    # — la clause juste, à 41 tokens — n'était jamais gardé ; il a été refusé en fin
                    # d'étape faute de 3 tokens. La borne, elle, ne bouge pas : au plus une unité par
                    # facette, sous la même part, dans le même classement gardé, et si aucun candidat
                    # n'y tient la facette ne garde toujours rien.
                    continue
                reserve_facettes[hit.clause_uid] = (rang, unite)
                break

    def couvrir_les_facettes() -> None:
        """Chaque facette rapporte au moins une règle décisionnelle, ou elle est dite non retrouvée.

        La suffisance déclarée par l'appelant est **globale** : un seul bloc décisionnel confirmé
        l'atteint, quelle que soit la sous-question auquel il répond. Une question à deux facettes
        pouvait donc s'arrêter sur la première, et rien — ni ici, ni en aval — ne distinguait cette
        lecture d'une lecture complète. Cette passe ferme l'écart par le seul moyen borné qui ne
        demande rien au modèle : pour chaque facette encore sans bloc, le **classement de sa propre
        requête**, restreint par le corpus aux kinds décisionnels confirmés, propose son meilleur
        candidat, et l'ouverture passe par le même outil, les mêmes fenêtres, les mêmes unités
        atomiques et les mêmes quotas que tout le reste de l'étape.

        Trois bornes, aucune nouvelle capacité : le tour de rôle entre facettes (une facette
        muette n'épuise pas le quota des autres), `facette_max_opens` essais par facette, et les
        budgets de l'étape — `max_opens`, blocs, tokens — qui arrêtent la passe où qu'elle en soit.
        Une facette abandonnée alors qu'un candidat restait à lire est une **borne de lecture** et
        se dit comme telle (`truncated`) ; une facette dont le classement est vide ou épuisé n'en
        est pas une : c'est le contrat qui ne dit rien, et c'est la couverture vide qui le dira.
        """
        if kinds_suffisants is None or not admitted:
            # Aucun bloc transmis : ce n'est pas une facette qui manque, c'est la lecture entière
            # qui a échoué. Le repli déterministe de l'appelant — une passe complète sur les termes
            # de la question — est la bonne réponse ; l'occuper d'abord avec une ouverture ciblée
            # par facette le rendrait inatteignable et remplacerait une lecture large par une
            # lecture étroite. Même garde que `complete_reservations` : la passe suppose une
            # navigation commencée.
            return
        nonlocal mappings_figes
        mappings = mappings_figes if mappings_figes is not None else mappings_par_rang()
        if len(_mappings_dedupes(mappings)) < FACETTES_MIN_POUR_COUVERTURE:
            return
        mappings_figes = mappings
        classement = classement_des_facettes()
        essais: dict[int, int] = {}
        for _tour in range(settings.facette_max_opens):
            progression = False
            for rang, mapping in mappings:
                hits = classement(mapping)
                if any(hit.clause_uid in admitted_set for hit in hits):
                    continue
                if essais.get(rang, 0) >= settings.facette_max_opens:
                    continue
                candidat = next((hit for hit in hits
                                 if hit.clause_uid not in admitted_set
                                 and hit.clause_uid not in focused_windows_attempted), None)
                if candidat is None:
                    continue
                if (opens >= budget.max_opens
                        or (budget.max_blocks is not None and blocks_used >= budget.max_blocks)):
                    # Un candidat restait lisible et le budget de l'étape l'a arrêté. La borne se
                    # dit une seule fois, sur la mesure finale (`facettes_bornees`) : deux endroits
                    # où poser `truncated` auraient fini par diverger.
                    return
                essais[rang] = essais.get(rang, 0) + 1
                progression = True
                # Le hit est enregistré avant l'ouverture : c'est lui qui rend le focus recevable
                # par l'outil commun, et c'est lui que la décision d'admission publiera.
                hit_by_block.setdefault(candidat.clause_uid, candidat)
                identites = hits_by_block.setdefault(candidat.clause_uid, [])
                if all(connue.result_uid != candidat.result_uid for connue in identites):
                    identites.append(candidat)
                best_hit_by_node.setdefault(candidat.node_uid, candidat.clause_uid)
                if candidat.clause_uid not in search_candidates:
                    search_candidates.append(candidat.clause_uid)
                avant = set(admitted_set)
                _payload, is_error = execute(
                    "ouvrir_noeud",
                    {"node_id": candidat.node_uid, "focus_block_id": candidat.clause_uid},
                    prioritize_focus=True, unite_seule=True)
                # C5 : même mesure que pour la réserve — la fenêtre ouverte, pas l'unité visée.
                entres = [b for b in admitted_set - avant]
                if entres:
                    tokens_admis_par_rang[rang] = (tokens_admis_par_rang.get(rang, 0)
                                                   + cout_des_blocs(entres))
                if is_error:
                    return
            if not progression:
                break

    used_tools = False
    semantic_selection: SemanticSufficiencySelection | None = None
    # Deux échecs distincts du verdict terminal, et **aucun des deux n'est une borne de lecture**
    # (correctif G1). Le dernier tour de navigation passe par `client.tool_turn`, qui n'impose
    # aucun schéma de sortie : la forme du verdict n'est demandée que par une phrase de
    # `llm/prompts/retrouver.md`. Une navigation qui a ouvert ce qu'elle devait ouvrir n'a rien
    # laissé fermé parce que sa dernière phrase est de la politesse plutôt que du JSON — en faire
    # `truncated=True` faisait porter à une réponse parfaitement sourcée la lacune
    # `lecture_bornee` de *vérifier* et le bandeau « je n'ai pas pu lire tout ce qui pouvait
    # concerner votre question » de *restituer*. Les bornes réelles — quota d'ouvertures, budget
    # de blocs ou de tokens, `max_tokens`/`refusal`/`pause_turn`, pagination inachevée, aucun
    # outil au premier tour — restent posées ailleurs et continuent de rendre `truncated=True`.
    #
    # `verdict_illisible` : le texte du `end_turn` ne parse pas en `SemanticSufficiencySelection`.
    # `result_uid_non_admis` : il parse et se dit suffisant, mais désigne un résultat que le
    # corpus n'adoube pas. Les deux restent une **insuffisance** (aucune suffisance n'est
    # accordée sans identité admise) et se distinguent dans `sufficiency.reason` et la trace.
    verdict_illisible = False
    result_uid_non_admis = False

    async def navigate() -> None:
        nonlocal truncated, used_tools, semantic_selection, verdict_illisible
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
                    prompt_cache=settings.retrieval_prompt_cache,
                    trusted_line_uids=tuple(dict.fromkeys(
                        line.line_uid
                        for block_id in admitted
                        for line in block(block_id).lines
                        if line.line_uid is not None
                    )))
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
                if turn == 0:
                    truncated = True
                elif used_tools and result.message.stop_reason == "end_turn":
                    raw = "".join(
                        str(getattr(block, "text", ""))
                        for block in result.message.content
                        if getattr(block, "type", None) == "text"
                    ).strip()
                    objet = _premier_objet_json(raw)
                    try:
                        semantic_selection = SemanticSufficiencySelection.model_validate_json(
                            objet if objet is not None else raw)
                    except ValueError:
                        verdict_illisible = True
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
            if result.message.stop_reason in {"max_tokens", "refusal", "pause_turn"}:
                break
            # Correctif du tour 3 (R5) : **le navigateur sait combien de tours il lui reste.** Le
            # préfixe statique annonce le plafond du dialogue, jamais où l'on en est ; deux runs sur
            # trois ont dépensé leur dernier tour en ouvertures et n'ont donc rendu aucun verdict.
            # Le compte est composé par le code et voyage dans le message d'outil, avec les
            # résultats — le préfixe reste byte-identique, donc cacheable (AD-9).
            restants = budget.max_llm_turns - (turn + 1)
            tool_results.append({
                "type": "text",
                "text": (f"Il te reste {restants} tour(s) de dialogue. Le dernier ne doit appeler "
                         "aucun outil : il rend uniquement l'objet JSON de conclusion."
                         if restants == 1 else
                         f"Il te reste {restants} tour(s) de dialogue, dont le dernier est celui "
                         "de la conclusion et ne doit appeler aucun outil."),
            })
            # Sans besoin déclaré, un titre ou une définition éclaire les candidats sans fournir
            # encore la règle utile et tout autre bloc conserve l'arrêt froid historique. Lorsqu'un
            # appelant déclare des kinds suffisants, seuls un kind demandé **et confirmé** satisfait
            # cet arrêt. La pagination garde dans les deux cas sa propre priorité.
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
            # La réserve est posée **avant** le premier tour : c'est tout l'objet du correctif.
            reserver_les_facettes()
            await navigate()
            # Le navigateur conserve la priorité : ses fenêtres sont admises en premier. La
            # complétion ne voit que les recherches des phases antérieures et de ce tour outils ;
            # un sommaire placé après ne peut donc jamais ouvrir rétroactivement un candidat caché.
            complete_reservations()
            complete_search_candidates_for_sufficiency()
            # En dernier, et seulement sur ce qui reste : la complétion ci-dessus a pu couvrir
            # plusieurs facettes d'un coup, et ouvrir avant elle aurait dépensé le quota sur des
            # candidats qu'elle rapportait déjà.
            couvrir_les_facettes()
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
    # Tout hit réellement présenté obtient exactement un état terminal. Les refus sont capturés
    # ici, à l'instant où plus aucun outil ne peut les admettre, avec le snapshot courant.
    for hit in scored_hits:
        if hit.result_uid in admission_by_result:
            continue
        # Une identité dont la **clause** est entrée dans le contexte n'est pas un candidat refusé :
        # c'est la même clause, vue sous une autre empreinte de requête (correctif du tour 2).
        admise = hit.clause_uid in admitted_set
        admission_by_result[hit.result_uid] = AdmissionDecision(
            result_uid=hit.result_uid, state="admitted" if admise else "rejected",
            reason=("admitted_by_exact_unit" if admise
                    else "not_admitted_within_bounded_navigation"),
            snapshot=budget_snapshot())
    selected_hit = next((
        hit for hit in scored_hits
        if semantic_selection is not None
        and semantic_selection.sufficient
        and hit.result_uid == semantic_selection.result_uid
    ), None)
    # La suffisance est jugée sur l'identité **canonique** de la clause désignée, la même que celle
    # que `suffisance_atteinte()` lit : deux identités de la même clause portent deux scores, et les
    # faire juger par deux mesures différentes revenait à ce que le code et le modèle ne parlent pas
    # de la même chose (correctif du tour 2, rapport citations B2).
    canonique = (hit_by_block.get(selected_hit.clause_uid, selected_hit)
                 if selected_hit is not None else None)
    sufficient_hit = (
        selected_hit
        if selected_hit is not None and canonique is not None
        and selected_hit.clause_uid in admitted_set
        and _score_positif(
            canonique.score,
            question=canonical_question,
            clause=block(selected_hit.clause_uid).text,
        )
        and block(selected_hit.clause_uid).kind not in _KINDS_CONTEXTUELS
        and (kinds_suffisants is None
             or (block(selected_hit.clause_uid).kind in kinds_suffisants
                 and block(selected_hit.clause_uid).kind_confirmed))
        else None
    )
    if semantic_selection is not None and semantic_selection.sufficient and sufficient_hit is None:
        result_uid_non_admis = True
    considered = tuple(hit.result_uid for hit in scored_hits
                       if hit.clause_uid in admitted_set)
    # Ce que la lecture rapporte **par facette**, mesuré une fois sur l'état final des blocs admis.
    # Vide sans besoin déclaré : la variante guide ne pose pas de facette au barème de *retrouver*,
    # et un résultat qui ne mesure rien vaut mieux qu'un résultat qui affirme une couverture.
    # Mesurée seulement là où la passe a pu agir : mêmes requêtes qu'elle, même seuil. Publier une
    # couverture qu'aucune passe n'a eu le droit de compléter aurait fait porter à `RetrievalResult`
    # une absence ou une borne que l'étape n'a jamais cherché à lever.
    facettes_couverture = (
        _couverture_facettes(
            mappings_figes, classement=classement_des_facettes(), admis=set(admitted),
            tokens_du_bloc=lambda b: set(words(normalize(block(b).text))),
            part_du_mot=lambda mot: max((index.part_des_blocs(m, doc_id=doc_id)
                                         for m in mot.split()), default=0.0),
            tokens_reserves={rang: cout_des_blocs(unite)
                             for _primaire, (rang, unite) in reserve_facettes.items()},
            tokens_admis=tokens_admis_par_rang)
        if kinds_suffisants is not None and admitted and mappings_figes else [])
    if any(facette.bornee for facette in facettes_couverture):
        # NFR2 : des candidats décisionnels d'une sous-question sont restés fermés. La lecture est
        # bornée, et c'est `truncated` — jamais une absence — qui le dit.
        truncated = True
    # Correctif du tour 2 (cause R2) : **la suffisance n'est plus mono-bloc.** Un seul résultat admis
    # la rendait vraie pour la question entière, quelle que soit la sous-question à laquelle il
    # répondait — c'est-à-dire qu'une lecture à moitié faite pouvait se déclarer suffisante. Quand
    # la couverture par facette a été mesurée, la lecture n'est suffisante que si **chaque**
    # sous-question porte au moins un bloc décisionnel confirmé transmis. La mesure est celle du
    # code ; la déclaration du navigateur ne la remplace jamais (AD-1).
    facettes_manquantes = [facette.rang for facette in facettes_couverture if not facette.retrouvee]
    complete = sufficient_hit is not None and not facettes_manquantes
    sufficiency = SufficiencyDecision(
        complete=complete,
        reason=("semantic_result_uid_admitted" if complete
                else "facettes_sans_clause_decisionnelle" if sufficient_hit is not None
                else "invalid_semantic_result_uid" if result_uid_non_admis
                else "unreadable_semantic_verdict" if verdict_illisible
                else "explicit_semantic_insufficiency"),
        sufficiency_result_uid=(sufficient_hit.result_uid if complete else None),
        considered_result_uids=considered,
    )
    if facettes_couverture and semantic_selection is not None:
        # Ce que le navigateur **déclare** par sous-question, confronté à ce que le code mesure.
        # Aucun des deux ne corrige l'autre : le code décide, la déclaration est publiée, et l'écart
        # est nommé — c'est lui qui dira, aux témoins, si le prompt porte réellement la consigne.
        # AD-10 : des rangs et des comptes, jamais un libellé de facette.
        declarees = {facette.facette: facette.result_uid for facette in semantic_selection.facettes}
        mesurees = {facette.rang: facette.retrouvee for facette in facettes_couverture}
        muettes = sorted(rang for rang in mesurees if rang not in declarees)
        ecarts = sorted(rang for rang, retrouvee in mesurees.items()
                        if rang in declarees and (declarees[rang] is not None) != retrouvee)
        detail = (f"{len(declarees)} sous-question(s) sur {len(mesurees)} ont reçu un verdict du "
                  "navigateur")
        if muettes:
            detail += f" ; sans verdict : rang(s) {', '.join(str(rang) for rang in muettes)}"
        if ecarts:
            detail += (" ; verdict contredit par la mesure du code (qui fait foi) : rang(s) "
                       + ", ".join(str(rang) for rang in ecarts))
        step.checks.append(CheckResult(
            name="verdict_par_facette", ok=not muettes and not ecarts, detail=detail))
    result = RetrievalResult(
        blocs=[block(b).model_copy(
            update={"context_role": context_role_by_block.get(b)}, deep=True,
        ) for b in admitted], opened_block_ids=list(admitted),
        facettes=facettes_couverture,
        # Story 4.2f : les nœuds d'où viennent les blocs **transmis**, tous, y compris ceux entrés
        # par `definitions` ou comme dépendance directe — `primary_node_by_block` n'en connaît que
        # les blocs de fenêtre, et s'y limiter laissait la variante servie annoncer « 0 section lue,
        # N passages transmis ».
        opened_node_ids=_noeuds_des_blocs(admitted, corpus=corpus, index=index),
        decision_dependency_block_ids=[b for b in decision_dependencies if b in admitted_set],
        discarded_block_ids=discarded, scored_hits=scored_hits,
        admission_decisions=list(admission_by_result.values()), sufficiency=sufficiency,
        truncated=truncated)
    step.ms = int((time.monotonic() - t0) * 1000)
    step.opened_block_ids = list(admitted)
    step.discarded_block_ids = list(discarded)
    step.budget_lecture = budget_snapshot()
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
    if facettes_couverture:
        # AD-10 : des rangs et des comptes, jamais un libellé de facette — il vient du modèle.
        absentes = [f.rang for f in facettes_couverture if f.absente]
        bornees = [f.rang for f in facettes_couverture if f.bornee]
        detail = (f"{sum(1 for f in facettes_couverture if f.retrouvee)} facette(s) sur "
                  f"{len(facettes_couverture)} mesurée(s) portent au moins un bloc décisionnel "
                  f"confirmé transmis")
        if absentes:
            detail += (f" ; le contrat lu n'en porte aucun pour le(s) rang(s) "
                       f"{', '.join(str(rang) for rang in absentes)}, déclaré(s) absent(s)")
        if bornees:
            detail += (f" ; des candidats sont restés fermés sous le budget pour le(s) rang(s) "
                       f"{', '.join(str(rang) for rang in bornees)} : lecture bornée, "
                       "aucune absence affirmée")
        step.checks.append(CheckResult(
            name="facettes_retrouvees", ok=not (absentes or bornees), detail=detail))
        # C5, instrumentation seule : ce que la passe a décidé **et le fait qui l'a décidé**. Trois
        # audits successifs ont dû redériver ces nombres hors ligne ; ils sont désormais lus.
        # AD-10 : des identifiants du corpus, des comptes et une part — la forme gagnante est un mot
        # normalisé de la requête, du même ordre que les termes que la trace publie déjà.
        step.checks.append(CheckResult(
            name="selection_par_facette", ok=True,
            detail=" | ".join(
                f"rang {facette.rang} : tête {facette.tete or 'aucune'}"
                f", forme « {facette.forme_gagnante or 'aucune'} »"
                f" ({facette.part_des_blocs:.4%} des blocs)"
                f", {facette.candidats} candidat(s)"
                f", {facette.tokens_reserves} token(s) gardé(s) pour {facette.tokens_admis} admis"
                for facette in facettes_couverture)))
    if discarded:
        # Le détail disait « choix de navigation » sans condition. Sur les trois runs A16, les
        # quinze candidats non lus l'avaient été par **épuisement du budget de tokens**, pas par
        # choix du modèle : le message trompait l'exploitation sur la cause. Les deux états sont
        # désormais distingués par un fait que le code connaît — le budget a-t-il refusé une unité.
        cause = ("le budget de l'étape a refusé au moins une unité : ces candidats n'ont pas pu "
                 f"être lus ({blocks_used} bloc(s), {tokens_used} token(s) consommés)"
                 if budget_a_refuse
                 else "choix de navigation distinct d'une troncature")
        step.checks.append(CheckResult(
            name="candidats_non_ouverts", ok=False,
            detail=f"{len(discarded)} candidat(s) de chercher non lu(s) par le navigateur ; "
                   + cause))
    if verdict_illisible or result_uid_non_admis:
        # Correctif G1 : la trace nomme laquelle des deux causes a dégradé le verdict terminal, et
        # dit qu'aucune des deux ne borne la lecture. `truncated` reste le seul canal des bornes
        # réelles ; il n'est plus réarmé ici.
        cause = ("verdict terminal illisible (le dernier tour n'a pas rendu de JSON conforme)"
                 if verdict_illisible
                 else "result_uid terminal non admis par le corpus")
        step.checks.append(CheckResult(
            name="verdict_semantique", ok=False,
            detail=f"{cause} ; suffisance refusée, lecture non bornée pour autant "
                   f"(truncated={truncated})"))
    if faq_candidates:
        step.checks.append(CheckResult(
            name="faq", ok=True,
            detail=f"{len(faq_candidates)} nœud(s) candidat(s) traité(s) selon l'ordre configuré"))
    if dictionary_searched_terms:
        borne = {"part_du_mot": part_du_mot_borne(
                     index, doc_id, part_max=settings.dictionnaire_variante_max_part),
                 "part_max": settings.dictionnaire_variante_max_part}
        searched_expanded = dictionnaire.expand(dictionary_searched_terms, **borne)
        base = {forme(t) for t in dictionary_searched_terms} - {""}
        touches = sum(1 for variantes in searched_expanded.values()
                      if any(v and v not in base for v in variantes))
        step.checks.append(CheckResult(
            name="dictionnaire", ok=True,
            detail=f"{dictionnaire.variants_count(dictionary_searched_terms, **borne)} "
                   f"variante(s) ajoutée(s) "
                   f"à {touches} terme(s)"))
        _noter_ambigues(step, dictionnaire, dictionary_searched_terms)
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

    `index.definitions()` continue de recevoir `terms` **seuls**, et la raison a changé : le faux
    positif que cette ligne invoquait — l'appariement `defines`/terme dans les deux sens, où
    « assurance habitation » ramenait une définition « habitation » — a été **corrigé par la story
    3.3**. `definitions()` n'apparie plus que dans un sens, de la question vers le terme défini
    (`de_la_question`, `corpus/index.py`). Ce qui reste vrai est plus simple : donner les 1 178
    variantes du dictionnaire à `definitions()` élargirait le rappel de définitions sans qu'aucune
    mesure ne dise ce qu'il y gagne, et cette étape ne change pas son entrée sans mesure. La reprise
    différée qui portait le faux positif est **fermée** (`spec-2-1-…`, `target_story: 3.3`).

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
    canonical_question = parsed.question_resolue.strip() or " ".join(terms)

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
    hits: list[ScoredHit] = []
    # C6 : la variante déterministe réserve avec la **même** requête que la variante outils. Elle
    # est relue à l'emploi, comme `mappings_par_rang` là-bas, parce que `dictionary_ready` dépend de
    # l'ordre déclaré des mécanismes et non de l'ordre où le code se trouve écrit.
    def mappings_deterministes() -> list[tuple[int, dict[str, list[str]] | list[str]]]:
        return _mappings_facettes(
            parsed.facettes, dictionnaire=dictionnaire, dictionary_ready=dictionary_ready,
            index=index, doc_id=doc_id,
            variante_max_part=settings.facette_variante_max_part,
            dictionnaire_max_part=settings.dictionnaire_variante_max_part)
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
                expanded_search = dictionnaire.expand(
                    terms,
                    part_du_mot=part_du_mot_borne(
                        index, doc_id, part_max=settings.dictionnaire_variante_max_part),
                    part_max=settings.dictionnaire_variante_max_part)
            cherches: dict[str, list[str]] | list[str] = expanded_search or terms
            phase_reservations: list[tuple[str, str]] = []
            phase_hits = index.chercher(
                cherches, limit=budget.search_limit, doc_id=doc_id,
                question=canonical_question,
                kinds_prioritaires=kinds_prioritaires,
                groupes_prioritaires=[requete for _rang, requete in mappings_deterministes()],
                reservations_out=phase_reservations)
            reserved_candidates.extend(
                reservation for reservation in phase_reservations
                if any((hit.clause_uid, hit.node_uid) == reservation for hit in phase_hits)
                and reservation not in reserved_candidates)
            for hit in phase_hits:
                block_id, node_id = hit.clause_uid, hit.node_uid
                if block_id not in {candidate for candidate, _node in hits}:
                    hits.append(hit)
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

    def lire(ouverts: list[str]) -> tuple[
        list[str], dict[str, str], list[str], bool,
        dict[str, BudgetSnapshot], BudgetSnapshot, dict[str, str],
    ]:
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
        context_roles: dict[str, str] = {}
        for node_id in ouverts:
            window = index.ouvrir_noeud(node_id, focus_block_id=best_hit[node_id],
                                        node_window=budget.node_window)
            if window.truncated:
                tronque = True  # pas de pagination en déterministe : la fenêtre reste coupée
            for b in window.blocks:
                if b.block_id not in fenetres:
                    fenetres.append(b.block_id)
                    noeud_de[b.block_id] = node_id
                    if b.context_role is not None and b.context_role != "target":
                        context_roles[b.block_id] = b.context_role

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
            # Le meilleur hit effectif du nœud est le point focal de cette fenêtre. Il doit être
            # essayé avant ses voisins lorsque le budget ne peut pas tous les admettre ; autrement
            # le score question-clause serait calculé puis ignoré exactement à la frontière où il
            # décide quel passage est réellement consommé. L'ordre rendu reste documentaire.
            primaires.extend(_prioriser_focus(voisins, focus_id, reserve=True))
        for block_id in primaires:
            directes = _dependances_directes(
                block_id, block=bloc, index=index, terms=terms, doc_id=doc_id,
                search_candidates=candidats, related_limit=budget.search_limit,
                related_max=settings.limite_liee_max,
                proximity_min=settings.limite_liee_proximite_min,
                related_cache=related_cache, search_related=block_id in candidats,
            )
            unite = (_unite_primaire(
                block_id, kind=bloc(block_id).kind, index=index, dependances=directes,
                block=bloc, settings=settings)
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
        snapshots: dict[str, BudgetSnapshot] = {}
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
            actual = BudgetSnapshot(
                opens_used=len(ouverts), blocks_used=blocs_utilises,
                tokens_used=tokens_utilises,
                opens_remaining=max(0, budget.max_opens - len(ouverts)),
                blocks_remaining=(None if budget.max_blocks is None else
                                  max(0, budget.max_blocks - blocs_utilises)),
                tokens_remaining=(None if budget.max_tokens is None else
                                  max(0, budget.max_tokens - tokens_utilises)),
            )
            for candidate in nouveaux:
                snapshots[candidate] = actual

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
        final_snapshot = BudgetSnapshot(
            opens_used=len(ouverts), blocks_used=blocs_utilises,
            tokens_used=tokens_utilises,
            opens_remaining=max(0, budget.max_opens - len(ouverts)),
            blocks_remaining=(None if budget.max_blocks is None else
                              max(0, budget.max_blocks - blocs_utilises)),
            tokens_remaining=(None if budget.max_tokens is None else
                              max(0, budget.max_tokens - tokens_utilises)),
        )
        return (ordre, noeud_de, dependances_decisionnelles, tronque,
                snapshots, final_snapshot, context_roles)

    ouverts, (promus, cedes) = _reserver(nodes, designes, budget.max_opens, budget.profil_max_opens)
    (ordre, noeud_de, dependances_decisionnelles, tronque,
     snapshots_by_block, final_snapshot, context_roles) = lire(ouverts)
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
        (ordre, noeud_de, dependances_decisionnelles, tronque,
         snapshots_by_block, final_snapshot, context_roles) = lire(ouverts)
    truncated = truncated or tronque
    blocs = [
        (bloc(b).model_copy(update={"context_role": context_roles[b]}, deep=True)
         if b in context_roles else bloc(b))
        for b in ordre
    ]

    opened = [b.block_id for b in blocs]
    # AD-10, littéralement : « candidats de `chercher` non ouverts » — donc les hits qui ne sont pas
    # transmis au modèle, et rien d'autre. Un bloc voisin écarté par le budget n'est pas un candidat
    # de recherche : c'est `truncated` qui porte cette information (revue Codex 1.4, B5).
    discarded = [b for b, _ in hits if b not in set(ordre)]
    # Story 4.2f : la même règle que côté outils, sur `ordre` — donc **après** le budget de blocs et
    # de tokens. Un nœud dont toute la fenêtre a été écartée n'a rien fait lire ; un bloc entré hors
    # fenêtre (définition autonome, renvoi direct) a bien été lu, et son nœud compte.
    hit_by_id = {hit.clause_uid: hit for hit in hits}
    # Les ouvertures précèdent l'admission des fenêtres. Les snapshots admis sont ensuite capturés
    # à la frontière exacte où leur bloc entre dans le contexte, et non reconstruits depuis l'état
    # final du run. Les candidats rejetés terminent après la navigation bornée et voient donc, eux,
    # le dernier état réellement atteint.
    decisions = [AdmissionDecision(
        result_uid=hit.result_uid,
        state="admitted" if hit.clause_uid in snapshots_by_block else "rejected",
        reason=("admitted_by_deterministic_unit" if hit.clause_uid in snapshots_by_block
                else "not_admitted_within_bounded_navigation"),
        snapshot=snapshots_by_block.get(hit.clause_uid, final_snapshot),
    ) for hit in hits]
    priorities = frozenset(kinds_prioritaires or ())
    sufficient_hit = next((
        hit_by_id[block_id] for block_id in ordre
        if block_id in hit_by_id
        and (hit_by_id[block_id].score.full_matches > 0
             or hit_by_id[block_id].score.partial_numerator > 0)
        and _score_positif(
            hit_by_id[block_id].score,
            question=canonical_question,
            clause=bloc(block_id).text,
        )
        and (not priorities or (bloc(block_id).kind in priorities
                                and bloc(block_id).kind_confirmed))
    ), None)
    considered = tuple(hit.result_uid for hit in hits if hit.clause_uid in set(ordre))
    result = RetrievalResult(blocs=blocs, opened_block_ids=opened,
                             opened_node_ids=_noeuds_des_blocs(ordre, corpus=corpus, index=index),
                             discarded_block_ids=discarded,
                             decision_dependency_block_ids=dependances_decisionnelles,
                             scored_hits=hits, admission_decisions=decisions,
                             sufficiency=SufficiencyDecision(
                                 complete=sufficient_hit is not None,
                                 reason=("relevant_foundation_admitted" if sufficient_hit else
                                         "no_relevant_foundation_within_budget"),
                                 sufficiency_result_uid=(sufficient_hit.result_uid
                                                         if sufficient_hit else None),
                                 considered_result_uids=considered),
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
        ajoutees = dictionnaire.variants_count(
            terms,
            part_du_mot=part_du_mot_borne(
                index, doc_id, part_max=settings.dictionnaire_variante_max_part),
            part_max=settings.dictionnaire_variante_max_part)
        touches = sum(1 for variantes in expanded_search.values()
                      if any(v and v not in base for v in variantes))
        step.checks.append(CheckResult(
            name="dictionnaire", ok=True,
            detail=f"{ajoutees} variante(s) ajoutée(s) à {touches} terme(s)"))
        _noter_ambigues(step, dictionnaire, terms)
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


def couvrir_facettes(parsed: ParsedQuestion, *, retrieval: RetrievalResult, corpus: Corpus,
                     index: Index, budget: RetrievalBudget, settings: Settings, doc_id: str,
                     kinds_suffisants: frozenset[str],
                     dictionnaire: Dictionnaire | None = None,
                     rangs: Iterable[int] | None = None) -> tuple[RetrievalResult, StepTrace]:
    """Reprend, en code pur, les facettes qu'un premier tour a laissées sans règle décisionnelle.

    **Pourquoi ici et pas dans le pipeline.** Même raison que `satisfaire_demande` : AD-1 fait de
    *retrouver* le seul propriétaire des outils, et un pipeline qui appellerait `Index.chercher`
    lui-même déplacerait la recherche hors de son étape. Ce n'est pas un mécanisme neuf : c'est le
    classement par facette de la phase `outils` — même requête, mêmes kinds confirmés, même
    fermeture d'un niveau — exposé sur un état déjà ouvert.

    **Aucun tour modèle.** Le manque a déjà été constaté ; aller chercher la règle est une
    résolution du corpus, pas une navigation. `StepTrace.calls == []` le dit, comme pour la
    variante déterministe.

    **Le budget est celui de l'étape, pas celui de la passe** (AD-1). Les compteurs sont amorcés
    avec ce que la passe initiale a déjà fait lire ; sans cela, un second tour pourrait admettre
    `max_blocks` blocs de plus, c'est-à-dire ne plus être borné. Le budget lui-même n'est jamais
    muté, et `facette_max_opens` borne l'acharnement sur une facette. Cette passe-ci ne consomme
    **pas** `max_opens` : elle n'ouvre aucune fenêtre, elle admet le bloc proposé et son unité
    atomique — c'est ce qui lui permet de servir encore quand le quota d'ouvertures est épuisé.

    **Une passe, jamais une boucle.** L'appelant en fait une seule ; ce qui reste sans bloc après
    elle est déclaré absent dans `RetrievalResult.facettes`, et la chaîne cesse là.

    `rangs` restreint la reprise aux facettes que l'appelant a vues non couvertes ; la couverture
    rendue, elle, porte sur **toutes** les facettes mesurables — un résultat partiel ferait croire
    à l'aval que les autres n'ont jamais été mesurées.
    """
    t0 = time.monotonic()
    step = StepTrace(name="retrouver", tier=STEP_TIERS["retrouver"])
    if doc_id not in corpus.documents:
        raise KeyError(doc_id)

    def bloc(block_id: str) -> Block:
        if index.doc_of(block_id) != doc_id:
            raise KeyError(block_id)
        return corpus.documents[doc_id].block(block_id)

    # L'état effectif du dictionnaire est celui de la configuration des mécanismes, pas celui de
    # l'objet reçu : une expansion active ici et inerte dans la phase `outils` aurait fait varier
    # le classement d'un même libellé selon l'endroit d'où il part.
    dictionary_ready = (dictionnaire is not None and dictionnaire.utilisable_pour(doc_id)
                        and "dictionnaire" in settings.retrieval_mechanisms())
    mappings = _mappings_facettes(parsed.facettes, dictionnaire=dictionnaire,
                                  dictionary_ready=dictionary_ready, index=index, doc_id=doc_id,
                                  variante_max_part=settings.facette_variante_max_part,
                                  dictionnaire_max_part=settings.dictionnaire_variante_max_part)
    if len(_mappings_dedupes(mappings)) < FACETTES_MIN_POUR_COUVERTURE:
        # Même seuil que la phase `outils`, et le résultat est rendu **intact** : ni bloc, ni
        # couverture, ni check. Une facette unique laisse la chaîne exactement où elle était.
        step.ms = int((time.monotonic() - t0) * 1000)
        return retrieval, step
    question = parsed.question_resolue.strip() or " ".join(parsed.termes_de_recherche())
    terms = parsed.termes_de_recherche()
    vises = set(rangs) if rangs is not None else {rang for rang, _mapping in mappings}

    admis = [b.block_id for b in retrieval.blocs]
    admis_set = set(admis)
    blocs_utilises = len(retrieval.blocs)
    tokens_utilises = sum(estimate_tokens(f"{b.block_id}\n{b.text}", settings)
                          for b in retrieval.blocs)
    classement = _classement_par_facette(index=index, doc_id=doc_id, question=question,
                                         kinds_confirmes=kinds_suffisants,
                                         limit=budget.search_limit)
    related_cache: dict[str, list[str]] = {}
    retenus: list[str] = []
    ecartes: list[str] = []
    tentes: set[str] = set()
    dependances_decisionnelles: list[str] = []
    tronque = False
    essais: dict[int, int] = {}
    for _tour in range(settings.facette_max_opens):
        progression = False
        for rang, mapping in mappings:
            if rang not in vises:
                continue
            hits = classement(mapping)
            if any(hit.clause_uid in admis_set for hit in hits):
                continue
            if essais.get(rang, 0) >= settings.facette_max_opens:
                continue
            candidat = next((hit.clause_uid for hit in hits
                             if hit.clause_uid not in admis_set and hit.clause_uid not in tentes),
                            None)
            if candidat is None:
                continue
            essais[rang] = essais.get(rang, 0) + 1
            tentes.add(candidat)
            progression = True
            # La même fermeture d'un niveau que la navigation : un renvoi voyage avec le passage
            # qui le cite, une définition applicable avec la clause qu'elle éclaire.
            dependances = _dependances_directes(
                candidat, block=bloc, index=index, terms=terms, doc_id=doc_id,
                search_candidates=[hit.clause_uid for hit in hits],
                related_limit=budget.search_limit, related_max=settings.limite_liee_max,
                proximity_min=settings.limite_liee_proximite_min,
                related_cache=related_cache, search_related=True)
            unite = _unite_primaire(candidat, kind=bloc(candidat).kind, index=index,
                                    dependances=dependances, block=bloc, settings=settings)
            if unite is None:  # pragma: no cover — un kind décisionnel n'est jamais un titre seul
                ecartes.append(candidat)
                tronque = True
                continue
            neufs = [b for b in dict.fromkeys(unite) if b not in admis_set]
            cout = sum(estimate_tokens(f"{b}\n{bloc(b).text}", settings) for b in neufs)
            if ((budget.max_blocks is not None
                 and blocs_utilises + len(neufs) > budget.max_blocks)
                    or (budget.max_tokens is not None
                        and tokens_utilises + cout > budget.max_tokens)):
                # Un candidat que le budget écarte est un candidat non lu, pas une absence du
                # corpus : il rejoint `discarded_block_ids` et marque la lecture comme tronquée.
                ecartes.append(candidat)
                tronque = True
                continue
            blocs_utilises += len(neufs)
            tokens_utilises += cout
            for block_id in neufs:
                admis_set.add(block_id)
                admis.append(block_id)
                retenus.append(block_id)
            if bloc(candidat).kind in KINDS_FONDATEURS:
                dependances_decisionnelles.extend(
                    b for b in dependances
                    if b in admis_set and b not in dependances_decisionnelles)
        if not progression:
            break

    blocs = [*retrieval.blocs, *(bloc(b) for b in retenus)]
    facettes = _couverture_facettes(
        mappings, classement=classement, admis={b.block_id for b in blocs},
        tokens_du_bloc=lambda b: set(words(normalize(bloc(b).text))),
        part_du_mot=lambda mot: max((index.part_des_blocs(m, doc_id=doc_id)
                                     for m in mot.split()), default=0.0))
    if any(f.rang in vises and f.bornee for f in facettes):
        # Comme dans la phase `outils` : des candidats décisionnels sont restés fermés, la lecture
        # est bornée, et aucune absence n'est affirmée sur eux (NFR2).
        tronque = True
    resultat = retrieval.model_copy(update={
        "blocs": blocs,
        "opened_block_ids": list(dict.fromkeys([*retrieval.opened_block_ids, *retenus])),
        "opened_node_ids": _noeuds_des_blocs([b.block_id for b in blocs], corpus=corpus,
                                             index=index),
        "decision_dependency_block_ids": list(dict.fromkeys([
            *retrieval.decision_dependency_block_ids, *dependances_decisionnelles])),
        "discarded_block_ids": list(dict.fromkeys([*retrieval.discarded_block_ids, *ecartes])),
        "facettes": facettes,
        "truncated": retrieval.truncated or tronque,
    })
    step.ms = int((time.monotonic() - t0) * 1000)
    step.opened_block_ids = list(retenus)
    step.discarded_block_ids = list(ecartes)
    # AD-10 : des rangs et des comptes, jamais le libellé d'une facette (il vient du modèle).
    absentes = [f.rang for f in facettes if f.rang in vises and f.absente]
    bornees = [f.rang for f in facettes if f.rang in vises and f.bornee]
    detail = (f"{len(vises)} facette(s) sans règle décisionnelle reprise(s) : {len(retenus)} "
              f"bloc(s) rouvert(s), {len(ecartes)} écarté(s) par le budget de l'étape — aucun "
              f"appel modèle")
    if absentes:
        detail += (f" ; le contrat lu ne porte aucune clause décisionnelle confirmée pour le(s) "
                   f"rang(s) {', '.join(str(rang) for rang in absentes)}, déclaré(s) absent(s)")
    if bornees:
        detail += (f" ; des candidats sont restés fermés sous le budget pour le(s) rang(s) "
                   f"{', '.join(str(rang) for rang in bornees)} : lecture bornée, aucune absence "
                   "affirmée")
    step.checks.append(CheckResult(name="couverture_facettes",
                                   ok=not (absentes or bornees), detail=detail))
    return resultat, step
