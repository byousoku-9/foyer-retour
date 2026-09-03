"""AD-1, amendement du 03/09/2026 — *naviguer* : le modèle lit le document, le code sert et vérifie.

**Une seule conversation.** Le modèle reçoit la question (et, en sinistre, les faits), les
sous-questions arrêtées par *comprendre* et le **sommaire complet** du document, mis en cache une
heure ; il ouvre ce qu'il veut avec quatre outils, sur `navigation_max_llm_turns` tours au plus et
sous un budget de lecture ; puis, **dans le même fil**, il rend l'ébauche `AnswerDraft` — le schéma
terminal de *rédiger*, inchangé (AD-3). La relance d'AD-3 est un message de plus dans cette même
conversation : le préfixe est déjà écrit, elle ne repaie que ce qu'elle ajoute.

**Aucune passe de code ne choisit ce que la rédaction voit.** Pas de réservation d'une part du
budget par sous-question, pas d'attribution lexicale d'un bloc à une sous-question, pas de
complétion au nom de la couverture, pas d'attachement automatique des définitions — `definitions`
est un outil que le modèle appelle. `chercher` **propose** des extraits ; seul `ouvrir_noeud` rend
un bloc citable. Ce que le code garde est ce qu'AD-1 lui laisse : servir le document, borner la
lecture, le coût et le temps, auditer, et vérifier ensuite chaque citation au caractère près — la
vérification, elle, reste un appel distinct sur un contexte propre (`steps/verifier.py`, inchangé).

L'étape publie **deux** `StepTrace`, et c'est la chaîne d'AD-1 qui l'exige : les tours d'outils sont
*retrouver*, l'appel qui rend l'ébauche est *rédiger*. L'ordre *comprendre → retrouver → rédiger →
vérifier → restituer* ne bouge pas ; ce qui change est l'implémentation interne de deux étapes.

**Le tour terminal est demandé sans outils.** La boucle d'outils va jusqu'à
`navigation_max_llm_turns` ; l'appel qui rend l'ébauche, lui, part avec `tool_choice` fermé et le
dit au modèle. Mesuré le 03/09/2026 sur l'audit exact (runs 1 et 3 de la série A16) : le tour
terminal répondait `stop_reason=tool_use` — le modèle voulait ouvrir un nœud de plus — et le code
levait « dialogue d'outils non supporté », c'est-à-dire un 503 sur une lecture qui n'avait rien
d'anormal. Le prototype ne connaissait pas ce cas parce que sa boucle ne demandait l'ébauche
qu'après un `end_turn` ; la chaîne, elle, la demande aussi quand la borne des tours est atteinte.
Si un appel censé être terminal appelle quand même un outil, on le sert et on redemande l'ébauche,
dans la borne des tours et du budget de lecture — jamais une erreur terminale pour cela.

Mesures qui ont décidé les défauts (prototype `scripts/proto_navigation.py`, série du 03/09/2026,
`automation/runs/20260902-structure-index/proto-runs/serie2/`) : A16 strict 3/3, 2 à 4 tours,
12 à 27 s, 0,05 € à cache chaud.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from typing import Any

from server.app.config import Settings
from server.app.corpus.dictionary import Dictionnaire
from server.app.corpus.ebauche import (fusionner_quotes_du_meme_bloc,
                                       joindre_amorces_denumeration, rattacher_claims_sinistre)
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus
from server.app.corpus.requetes import part_du_mot_borne, variantes_de_nombre
from server.app.domain.answer import AnswerDraft
from server.app.domain.document import Block, is_citable
from server.app.domain.errors import LlmParse, PipelineError
from server.app.domain.verdict import KINDS_DECISIONNELS
from server.app.domain.langue import LANGUES_SERVIES
from server.app.domain.profil import Profil
from server.app.domain.question import Faits, ParsedQuestion, Turn
from server.app.domain.retrieval import BudgetSnapshot, RetrievalResult
from server.app.domain.trace import CheckResult, StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient, ToolUseDemande
from server.app.llm.models import MODEL_CAPS, model_for
from server.app.llm.pricing import estimate_tokens
from server.app.llm.prompting import load_prompt, render_prompt, untrusted

# Claude 5 : l'adaptatif se demande explicitement, et `budget_tokens` y est refusé (400). Mesuré sur
# le prototype : sans ce paramètre, le tour **qui cite** ne réfléchit sur aucun run (0 token, contre
# 56 à 131 au tour qui navigue) — et c'est le seul tour où le prototype perdait.
REFLEXION_ADAPTATIVE: dict[str, Any] = {"type": "adaptive"}

# Le tour terminal se demande **sans outil**, et c'est `tool_choice` qui les ferme plutôt que le
# retrait de `tools` du corps : chez le fournisseur, un changement de `tool_choice` laisse intacts
# les caches d'outils et de système, alors que retirer `tools` réécrirait le préfixe entier — les
# 42 470 tokens de sommaire compris — à chaque question. La même conversation, le même préfixe, un
# seul tour où les outils sont clos.
TOOL_CHOICE_AUCUN: dict[str, Any] = {"type": "none"}

# Ce que le message du tour terminal dit au modèle : *où il en est*. Mesuré le 03/09/2026 (audit
# exact, runs 1 et 3 de la série A16) : sans cette phrase et avec les outils ouverts, le tour qui
# devait rendre l'ébauche rendait `stop_reason=tool_use` — le modèle voulait ouvrir un nœud de plus
# — et l'étape levait « dialogue d'outils non supporté », c'est-à-dire un 503 sur une lecture qui
# n'avait rien d'anormal.
DEMANDE_TERMINALE = (
    "Tu as fini de lire : ce message ouvre le **tour terminal** et les outils y sont fermés. Rends "
    "maintenant l'ébauche `AnswerDraft` demandée par le préfixe, en JSON et sans appel d'outil, en "
    "ne citant que des blocs rendus par `ouvrir_noeud` dans cette conversation.")
# La règle que l'inventaire rappelle, et la seule chose que le code ait à dire sur le fond : une
# clause qui **qualifie** la circonstance déclarée n'est pas un doublon de la clause qui nomme le
# péril. Mesuré le 03/09/2026 (r3 de la série finale A16) : l'énumération des périls incendie est
# lue en entier, `p34:12` — « … événement soudain … même lorsqu'il n'y a pas eu embrasement » — est
# ouvert et transmis, et l'ébauche cite à sa place la définition de l'incendie `p34:7`. Le modèle
# n'a pas manqué la clause : il l'a tenue pour redondante.
CONSIGNE_INVENTAIRE = (
    "Avant de rédiger, prends **une décision par bloc** de cet inventaire : vise-t-il les faits "
    "déclarés ? Si oui, il te faut une claim qui rapporte sa règle. Si non, déclare-le dans "
    "`blocs_ecartes` avec un motif d'une ligne. Une clause qui **qualifie** la circonstance "
    "déclarée — sa soudaineté, son origine, une condition qu'elle pose — n'est jamais redondante "
    "avec la clause qui nomme le péril ou avec sa définition : ce sont deux clauses, et elles font "
    "deux claims. N'écarte pas un bloc parce qu'un bloc voisin lui ressemble.")
# Ce que « premiers mots » veut dire : de quoi reconnaître un bloc dans une liste, pas de quoi le
# juger sans le relire. Le texte des blocs est déjà dans le fil, in extenso.
INVENTAIRE_AMORCE_MAX_CHARS = 140
# Ce que le code dit au modèle quand sa **propre sortie** a dépassé le plafond du tour terminal.
# Mesuré le 03/09/2026 (gate Baloise 13 h 43, `b-bougie-canape` rép. 3, effort `medium`, plafond
# 5 056) : sur Sonnet 5 la réflexion adaptative n'a pas de budget propre — elle partage `max_tokens`
# avec le contrat JSON (`llm/models.py`, T1d/T10) — et sa queue dépasse tout plafond que la deadline
# autorise (T13 : 5 888 prescrits, 5 056 possibles). La sortie de ce texte est donc de demander
# **moins de réflexion**, pas plus de place : l'ébauche directement, à `low`.
CONSIGNE_REPRISE_TRONQUEE = (
    "Ta sortie a dépassé le plafond de tokens : elle a été coupée avant la fin du JSON, et rien "
    "n'en a été retenu. Rends l'ébauche `AnswerDraft` directement, en JSON et sans appel d'outil, "
    "sans réflexion étendue préalable. Garde toutes les claims et leurs citations exactes ; abrège "
    "les textes libres.")
# L'alternance du dialogue demande un tour assistant avant la consigne. La sortie coupée n'est pas
# réinjectée : elle est incomplète par définition, la refacturer en entrée n'apprend rien au modèle
# et l'invite à prolonger la même réponse trop longue — le même marqueur constant que le retry de
# `llm/client.py`.
REPRISE_ASSISTANT_TRONQUEE = "(réponse tronquée omise)"
# L'effort de la reprise, et il n'est pas un réglage : la reprise existe **parce que** la réflexion
# a mangé la place du JSON. Le servir depuis `navigation_draft_effort` la rejouerait à l'identique.
EFFORT_REPRISE_TRONQUEE = "low"
RAPPEL_TERMINAL = (
    "Voilà ce que ton appel a rendu : c'était ta dernière lecture, la boucle d'outils est close. "
    "Rends maintenant l'ébauche `AnswerDraft` en JSON, sans appel d'outil, avec les blocs que "
    "`ouvrir_noeud` t'a rendus ; dis dans un segment `limite` ce que ta lecture n'a pas couvert.")

# Les quatre outils d'AD-1. `sommaire` et `chercher` proposent ; `ouvrir_noeud` seul rend citable ;
# `definitions` est appelé par le modèle et n'est plus une passe implicite du code.
OUTILS: list[dict[str, Any]] = [
    {
        "name": "sommaire",
        "description": "La sous-arborescence d'un nœud (node_id + titre, indentée). "
                       "Sans node_id : le sommaire complet du document.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "properties": {"node_id": {"type": "string"}}, "required": []},
    },
    {
        "name": "ouvrir_noeud",
        "description": "Le texte intégral des blocs citables d'un nœud et de ses enfants feuilles à "
                       "un bloc. Seuls ces blocs deviennent citables.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "properties": {"node_id": {"type": "string"}}, "required": ["node_id"]},
    },
    {
        "name": "chercher",
        "description": "Candidats classés par couverture de mots (élargis par le dictionnaire du "
                       "document), avec un extrait. Proposition, pas décision.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "properties": {"termes": {"type": "array", "items": {"type": "string"}}},
                         "required": ["termes"]},
    },
    {
        "name": "definitions",
        "description": "Les blocs de définition qui définissent ces termes, valides dans la portée "
                       "des nœuds déjà ouverts. Une définition éclaire, elle ne décide pas.",
        "input_schema": {"type": "object", "additionalProperties": False,
                         "properties": {"termes": {"type": "array", "items": {"type": "string"}}},
                         "required": ["termes"]},
    },
]


def sommaire_complet(corpus: Corpus, doc_id: str, *, racine: str | None = None) -> str:
    """Tous les nœuds du document (ou du sous-arbre de `racine`), `node_id` + titre, indentés.

    Pas de pagination, pas d'aperçu, pas de budget de contexte : c'est **la** décision de
    l'amendement. `Index.sommaire_page` pagine sur un budget et rend une tranche ; servir une carte
    partielle revient à choisir pour le modèle ce qu'il a le droit de considérer. Le sommaire entier
    du contrat servi vaut 42 470 tokens mesurés au tokenizer du fournisseur : il est **payé une
    fois** puis relu au tarif de cache (breakpoint 1 h), ce qui est précisément ce que le cache est.
    """
    doc = corpus.documents[doc_id]
    par_id = {n.node_id: n for n in doc.nodes}
    if racine is not None and racine not in par_id:
        raise KeyError(racine)
    noeuds = list(doc.nodes)
    # Le nœud racine porte le titre du document, déjà connu : sa ligne n'apprend rien **tant qu'il
    # ne porte aucun bloc en propre**. Quand il en porte — le contrat AXA y attache la liste des
    # garanties souscrites, cinq blocs de la page 5 —, le taire les rend **inatteignables** :
    # `ouvrir_noeud` demande un `node_id`, et le seul qui les rendrait n'est nommé nulle part.
    # Mesuré le 03/09/2026 : interrogé sur un dommage qu'aucune garantie ne vise, le modèle a cité
    # l'énumération interne d'une garantie faute de pouvoir ouvrir celle du contrat. La règle est
    # structurelle et vaut pour tout document : un nœud qui porte des blocs citables se voit.
    racine_citable = any(is_citable(doc.block(b)) for b in par_id[doc_id].blocks) if doc_id in par_id else False
    if racine is not None:
        garde: list[str] = [racine]
        vus: set[str] = set()
        while garde:
            courant = garde.pop()
            if courant in vus:
                continue
            vus.add(courant)
            garde.extend(par_id[courant].children)
        noeuds = [n for n in noeuds if n.node_id in vus]
    rendus = [n for n in noeuds if n.node_id != doc_id or racine_citable]
    # La profondeur de référence est celle des lignes **rendues** : un document dont la racine reste
    # tue s'indente comme avant, à l'espace près.
    base = min((n.level for n in rendus), default=0)
    return "\n".join(f"{'  ' * max(0, n.level - base)}{n.node_id} — {n.title}" for n in rendus)


def blocs_du_noeud(corpus: Corpus, doc_id: str, node_id: str) -> list[Block]:
    """Les blocs citables du nœud, **et** ceux de ses enfants feuilles à un seul bloc.

    Un nœud d'un contrat est souvent un intertitre dont la règle vit dans un enfant d'un bloc ;
    servir le seul nœud demandé obligerait le modèle à un tour par ligne d'énumération — et une
    énumération lue à moitié est exactement la façon dont on perd l'item qui décide. La règle est
    **structurelle** et vaut pour tout document : aucun cas particulier, aucun `kind` privilégié.
    """
    doc = corpus.documents[doc_id]
    par_id = {n.node_id: n for n in doc.nodes}
    noeud = par_id[node_id]
    ids = list(noeud.blocks)
    for enfant in noeud.children:
        fils = par_id[enfant]
        if not fils.children and len(fils.blocks) == 1:
            ids.extend(fils.blocks)
    return [b for b in (doc.block(i) for i in ids) if is_citable(b)]


def _role(block: Block) -> str | None:
    """Le rôle du bloc dans l'enregistrement d'origine (`Block.source_field`), sans son rang.

    C'est une donnée d'**ingestion**, comme `kind`, et elle vaut pour tout document qui en porte
    une : `titre`, `resume`, `corps`, `aRetenir`, `tableaux` pour une fiche du guide, rien pour un
    PDF dont l'extraction n'en produit pas. Le rang (`corps[3]`) est retiré — il ne dit rien de plus
    que l'ordre, déjà donné par la suite des blocs.

    Elle est rendue au modèle parce que, sans elle, deux paragraphes d'une même fiche sont
    indiscernables : mesuré le 03/09/2026 en prod, la réponse du guide citait le `resume` d'une
    fiche — une accroche qui annonce sans expliquer — et jamais son `corps`, où vivent le délai,
    l'adresse et la liste des pièces. Le modèle n'avait pas préféré le résumé : il ne pouvait pas
    savoir que c'en était un.
    """
    champ = block.source_field
    return champ.split("[", 1)[0] if champ else None


def _rendre_blocs(blocs: Iterable[Block]) -> str:
    def entete(b: Block) -> str:
        role = _role(b)
        return f"[{b.block_id}] ({b.kind})" if role is None else f"[{b.block_id}] ({b.kind} · {role})"
    return "\n\n".join(f"{entete(b)}\n{b.text}" for b in blocs)


class Navigation:
    """Une conversation de navigation : ce qui a été lu, ce que la lecture a coûté, ce qu'elle rend.

    L'objet vit le temps d'une requête et porte le fil : la même instance sert la lecture, l'ébauche
    et la relance, parce que ce sont trois messages d'un même dialogue et non trois appels.
    """

    def __init__(self, parsed: ParsedQuestion, *, corpus: Corpus, index: Index,
                 dictionnaire: Dictionnaire | None, doc_id: str, settings: Settings,
                 client: LlmClient, request_budget: RequestBudget, prompt: str,
                 faits: Faits | None = None, historique: Iterable[Turn] = (),
                 profil: Profil | None = None,
                 on_tour: Callable[[int], None] | None = None) -> None:
        if doc_id not in corpus.documents:
            raise KeyError(doc_id)
        self.parsed, self.corpus, self.index = parsed, corpus, index
        self.dictionnaire, self.doc_id, self.settings = dictionnaire, doc_id, settings
        self.client, self.request_budget, self.prompt = client, request_budget, prompt
        self.faits, self.historique = faits, list(historique)
        self.profil = profil
        # Story 5.6 (L1) : le rappel d'avancement de la route de progression, **optionnel** et sans
        # pouvoir — il reçoit le numéro du tour d'outils qui commence, rien d'autre. `None` par
        # défaut : les évals, les gates et les tests hermétiques ne voient aucune différence.
        self.on_tour = on_tour
        self.tier = settings.navigation_tier
        # Ce que la lecture a fait entrer, dans l'ordre où le modèle l'a ouvert.
        self.ouverts: dict[str, Block] = {}
        self.noeuds_ouverts: list[str] = []
        self.refuses: list[str] = []
        self.tokens_lus = 0
        self.tours = 0
        # Combien de fois un appel *censé être terminal* a quand même demandé un outil : la trace du
        # cas qui rendait un 503, et qui se répare maintenant en servant l'outil.
        self.tour_terminal_force = 0
        # Combien de fois un tour terminal a été redemandé parce que **sa propre sortie** avait
        # dépassé le plafond : jamais plus d'une fois par tour terminal (voir `_appel_terminal`).
        self.tour_terminal_repris = 0
        self.recherches = 0
        self._messages: list[dict[str, Any]] = [{
            "role": "user", "content": self._demande()}]

    # --- ce que le code sert ---------------------------------------------------------------

    @property
    def prefixe(self) -> str:
        """Le préfixe système, byte-identique d'un bout à l'autre de la conversation (AD-9)."""
        if not hasattr(self, "_prefixe"):
            self._prefixe = (
                load_prompt("commun") + "\n\n" + render_prompt(
                    self.prompt,
                    quote_min_chars=self.settings.quote_min_chars,
                    navigation_draft_max_segments=self.settings.navigation_draft_max_segments,
                    navigation_draft_max_claims=self.settings.navigation_draft_max_claims,
                    navigation_max_llm_turns=self.settings.navigation_max_llm_turns,
                    navigation_budget_tokens=self.settings.navigation_budget_tokens,
                ) + "\n\n" + untrusted("sommaire",
                                       sommaire_complet(self.corpus, self.doc_id)))
        return self._prefixe

    def fiches_suggerees(self) -> list[tuple[str, str]]:
        """Les fiches que le profil déclaré désigne (story 2.3), avec leur titre — pour **indication**.

        `parsed.scope.noeuds` est calculé par *comprendre* en code pur, depuis `Document.parcours` :
        des `node_id` de notre ingestion, jamais un mot du modèle (AD-10 les autorise donc dans la
        trace). L'AC de la story 2.3 dit « *retrouver* priorise ces nœuds » ; la passe qui les
        honorait — une réservation de places dans un budget d'ouvertures — appartenait à la variante
        retirée, et **c'est bien elle qu'AD-1 amendé interdit** : aucun code ne choisit plus ce que
        la rédaction verra.

        Ce qui reste de l'AC, et qui n'est pas rien, c'est la désignation elle-même. Elle est servie
        au modèle comme ce qu'elle est : une suggestion nommée, dans la demande, à côté de la
        question. Le modèle ouvre s'il juge que c'est utile, ou n'ouvre pas ; rien n'est ouvert
        d'office, aucune place n'est réservée, aucun classement n'est modifié, et le budget de
        lecture ne connaît pas le profil. La trace publie ensuite ce qu'il en a fait — c'est la
        seule façon honnête de mesurer une priorisation qu'on ne force plus.

        Les nœuds étrangers au document servi sont écartés en silence : ils viennent du parcours
        d'un autre document et n'auraient aucun sens dans ce sommaire.
        """
        scope = self.parsed.scope
        if scope is None or not scope.noeuds:
            return []
        par_id = {n.node_id: n for n in self.corpus.documents[self.doc_id].nodes}
        return [(node_id, par_id[node_id].title)
                for node_id in dict.fromkeys(scope.noeuds) if node_id in par_id]

    def _demande(self) -> str:
        """Question, faits et sous-questions, délimités comme tout contenu non fiable (AD-15)."""
        demande: dict[str, Any] = {"question": self.parsed.question_resolue,
                                   "sous_questions": list(self.parsed.facettes),
                                   "termes": self.parsed.termes_de_recherche()}
        suggerees = self.fiches_suggerees()
        if suggerees:
            # La clé n'apparaît que si le profil a désigné quelque chose : un dossier de sinistre
            # n'a ni profil ni parcours, et son corps de requête ne bouge pas d'un octet.
            demande["fiches_suggerees_par_le_profil"] = [
                {"node_id": node_id, "titre": titre} for node_id, titre in suggerees]
        if self.faits is not None:
            demande["faits"] = self.faits.model_dump(mode="json", exclude_none=True)
        if self.profil is not None:
            # La projection d'AD-11 (`PROFIL_KEYS`), jamais le corps brut : le profil voyage déjà
            # filtré vers *comprendre*, et le champ n'apparaît que s'il porte une réponse — un
            # dossier de sinistre n'a pas de profil, son corps de requête ne bouge pas d'un octet.
            renseigne = {k: v for k, v in self.profil.filtered().items()
                         if v not in (None, "", [], {})}
            if renseigne:
                demande["profil"] = renseigne
        parts = [untrusted("demande", json.dumps(demande, ensure_ascii=False, sort_keys=True))]
        if self.historique:
            parts.insert(0, untrusted("historique", json.dumps(
                [{"role": t.role, "texte": t.texte} for t in self.historique],
                ensure_ascii=False)))
        return "\n\n".join(parts)

    def executer(self, nom: str, args: dict[str, Any]) -> str:
        """Un appel d'outil. Une erreur d'identifiant se **dit** au modèle, elle n'arrête rien."""
        try:
            if nom == "sommaire":
                node_id = args.get("node_id")
                return sommaire_complet(self.corpus, self.doc_id,
                                        racine=str(node_id) if node_id else None) or \
                    "ce nœud n'a pas de sous-arborescence : ouvre-le."
            if nom == "ouvrir_noeud":
                return self._ouvrir(str(args["node_id"]))
            if nom == "chercher":
                return self._chercher([str(t) for t in args.get("termes") or []])
            if nom == "definitions":
                return self._definitions([str(t) for t in args.get("termes") or []])
        except KeyError as exc:
            return f"identifiant inconnu dans ce document : {exc}"
        return f"outil inconnu : {nom}"

    def _ouvrir(self, node_id: str) -> str:
        if self.index.doc_of_node(node_id) != self.doc_id:
            return "ce nœud n'appartient pas au document servi."
        blocs = blocs_du_noeud(self.corpus, self.doc_id, node_id)
        if not blocs:
            return (f"{node_id} ne porte aucun bloc citable en propre (c'est un intertitre) : "
                    "ouvre l'un de ses enfants, listés par `sommaire`.")
        rendu = _rendre_blocs(blocs)
        cout = estimate_tokens(rendu, self.settings)
        budget = self.settings.navigation_budget_tokens
        if self.tokens_lus + cout > budget:
            # Le budget s'applique au **refus**, jamais à la sélection : le code ne coupe rien en
            # silence, il dit le coût, le restant et quoi faire, et le modèle arbitre (AD-16).
            self.refuses.extend(b.block_id for b in blocs if b.block_id not in self.ouverts)
            return (f"budget de lecture insuffisant : ce nœud coûte ≈ {cout} tokens, il en reste "
                    f"{budget - self.tokens_lus} sur {budget}. Ouvre un nœud plus précis, ou "
                    "conclus avec ce que tu as déjà lu.")
        self.tokens_lus += cout
        if node_id not in self.noeuds_ouverts:
            self.noeuds_ouverts.append(node_id)
        for bloc in blocs:
            self.ouverts.setdefault(bloc.block_id, bloc)
        return rendu

    def _mapping(self, termes: list[str]) -> dict[str, list[str]] | list[str]:
        """La requête élargie : équivalences écrites du dictionnaire (AD-5), puis formes de nombre.

        Les deux élargissements portent sur la **requête** et sur elle seule ; aucun ne choisit un
        bloc. Sans les formes de nombre, l'index est littéral au point d'être trompeur — un
        singulier ne trouve pas la clause écrite au pluriel, et le modèle conclurait à tort que le
        document est muet. Une recherche qui manque le seul bloc décisif n'est pas une proposition,
        c'est un contresens.
        """
        settings = self.settings
        if self.dictionnaire is None or not self.dictionnaire.utilisable_pour(self.doc_id):
            mapping: dict[str, list[str]] = {terme: [] for terme in termes}
        else:
            mapping = self.dictionnaire.expand(
                termes,
                part_du_mot=part_du_mot_borne(self.index, self.doc_id,
                                              part_max=settings.dictionnaire_variante_max_part),
                part_max=settings.dictionnaire_variante_max_part)
        for terme, variantes in mapping.items():
            for forme in variantes_de_nombre([terme, *variantes], index=self.index,
                                             doc_id=self.doc_id,
                                             part_max=settings.variante_nombre_max_part):
                if forme not in variantes:
                    variantes.append(forme)
        return mapping

    def _chercher(self, termes: list[str]) -> str:
        if not termes:
            return "aucun terme : donne au moins un mot à chercher."
        self.recherches += 1
        hits = self.index.chercher(self._mapping(termes),
                                   limit=self.settings.navigation_search_limit,
                                   doc_id=self.doc_id)
        if not hits:
            return "aucun candidat pour ces termes."
        document = self.corpus.documents[self.doc_id]
        return json.dumps([{"block_id": h.clause_uid, "node_id": h.node_uid,
                            "kind": document.block(h.clause_uid).kind,
                            "titre": h.title, "extrait": h.excerpt} for h in hits],
                          ensure_ascii=False, indent=1)

    def _definitions(self, termes: list[str]) -> str:
        if not termes:
            return "aucun terme : donne au moins un mot à définir."
        trouvees = self.index.definitions(self._mapping(termes), doc_id=self.doc_id,
                                          blocs_ouverts=list(self.ouverts))
        if not trouvees:
            return "aucune définition de ces termes n'est valide dans la portée de ce que tu as lu."
        document = self.corpus.documents[self.doc_id]
        # Comme `chercher`, `definitions` **propose** : le texte est montré, mais seul
        # `ouvrir_noeud` rend citable. Le contrôle de *vérifier* rejetterait sinon une citation
        # qu'aucune ouverture n'a portée — et l'amendement retire l'attachement automatique.
        blocs = [document.block(block_id) for block_id, _ in trouvees]
        return (_rendre_blocs(blocs) + "\n\nCes définitions éclairent ; pour en citer une, ouvre "
                "son nœud.")

    # --- la conversation -------------------------------------------------------------------

    async def lire(self) -> StepTrace:
        """Les tours d'outils : l'étape *retrouver*, dont la lecture se relit par `retrieval()`.

        Séparée de la rédaction bien qu'elles partagent le fil : la chaîne doit pouvoir refuser
        entre les deux — une lecture qui n'a rendu aucun bloc citable ne mérite pas qu'on paie une
        rédaction sans source (garde-fou « zéro bloc » des deux pipelines).
        """
        t0 = time.monotonic()
        settings = self.settings
        step = StepTrace(name="retrouver", tier=self.tier, prompt_cache=True)
        tronquee = False
        try:
            while self.tours < settings.navigation_max_llm_turns:
                self.tours += 1
                if self.on_tour is not None:
                    self.on_tour(self.tours)
                resultat = await self.client.tool_turn(
                    tier=self.tier, system_prefix=self.prefixe, messages=self._messages,
                    tools=OUTILS, budget=self.request_budget, step=step,
                    max_tokens=settings.retrouver_outils_max_tokens,
                    thinking=REFLEXION_ADAPTATIVE)
                contenu = [b.model_dump(mode="json") if hasattr(b, "model_dump") else dict(b)
                           for b in resultat.message.content]
                self._messages = [*self._messages, {"role": "assistant", "content": contenu}]
                appels = [b for b in contenu if b.get("type") == "tool_use"]
                if not appels:
                    break
                self._messages = [*self._messages, {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": appel.get("id"),
                     "content": self.executer(str(appel.get("name")),
                                              dict(appel.get("input") or {}))}
                    for appel in appels]}]
            else:
                # Le plafond de tours est une **borne**, pas une fin de lecture : ce qui n'a pas été
                # ouvert ne prouve aucune absence (NFR2). La chaîne le publie par `truncated`.
                tronquee = True
                step.checks.append(CheckResult(
                    name="tours_epuises", ok=False,
                    detail=f"plafond de {settings.navigation_max_llm_turns} tours atteint : la "
                           "lecture est déclarée bornée, aucune absence du document n'est affirmée"))
        except PipelineError as exc:
            # AD-10/AD-16 : l'appel raté a pu être facturé ; l'étape partielle voyage avec l'erreur.
            step.ms = int((time.monotonic() - t0) * 1000)
            self._publier(step)
            exc.step = step
            raise
        self._tronquee = tronquee or bool(self.refuses)
        if self.refuses:
            step.checks.append(CheckResult(
                name="lecture_refusee", ok=False,
                detail=f"{len(self.refuses)} bloc(s) laissé(s) fermé(s) : le budget de lecture "
                       f"({settings.navigation_budget_tokens} tokens) n'en laissait pas la place"))
        suggerees = self.fiches_suggerees()
        if suggerees:
            # AD-10 : des `node_id` produits par l'ingestion, jamais une clé de profil, jamais un
            # terme cherché, jamais un contenu de bloc. `ok=True` sans condition, et c'est le fond
            # de l'affaire : une fiche suggérée que le modèle n'ouvre pas n'est pas une faute, ni la
            # sienne ni celle du code — c'est une lecture qu'il a jugée inutile, et le contrôle la
            # **rapporte** au lieu de la juger. Un `ok=False` ici rétablirait par la porte de la
            # trace la contrainte que l'amendement retire.
            ouvertes = [node_id for node_id, _ in suggerees if node_id in self.noeuds_ouverts]
            step.checks.append(CheckResult(
                name="noeuds_du_profil", ok=True,
                detail=f"{len(suggerees)} fiche(s) suggérée(s) par le profil "
                       f"({', '.join(node_id for node_id, _ in suggerees)}), "
                       f"{len(ouvertes)} ouverte(s) par le modèle ({', '.join(ouvertes) or 'aucune'})"
                       " : une indication, jamais une ouverture"))
        step.checks.append(CheckResult(
            name="navigation", ok=True,
            detail=f"{self.tours} tour(s), {len(self.noeuds_ouverts)} nœud(s) ouvert(s) "
                   f"({', '.join(self.noeuds_ouverts) or '—'}), {self.recherches} recherche(s), "
                   f"lecture {self.tokens_lus}/{settings.navigation_budget_tokens} tokens, "
                   f"réflexion {sum(call.thinking for call in step.calls)} tokens"))
        step.ms = int((time.monotonic() - t0) * 1000)
        self._publier(step)
        return step

    def _publier(self, step: StepTrace) -> None:
        step.opened_block_ids = list(self.ouverts)
        step.discarded_block_ids = list(dict.fromkeys(self.refuses))
        step.budget_lecture = BudgetSnapshot(
            opens_used=len(self.noeuds_ouverts), blocks_used=len(self.ouverts),
            tokens_used=self.tokens_lus, opens_remaining=0,
            tokens_remaining=max(0, self.settings.navigation_budget_tokens - self.tokens_lus))

    def blocs_decisionnels_ouverts(self) -> list[Block]:
        """Les blocs `garantie|exclusion|condition|franchise` que **la lecture a ouverts**.

        Le `kind` vient de l'ingestion, relu sur les blocs que le modèle a lui-même ouverts ; ni la
        question, ni les faits, ni un mot du modèle n'entrent dans ce filtre. L'ordre est celui des
        ouvertures : c'est la mesure de ce qui a été lu, pas un classement.
        """
        return [bloc for bloc in self.ouverts.values() if bloc.kind in KINDS_DECISIONNELS]

    def inventaire_decisionnel(self) -> str | None:
        """L'inventaire des blocs décisionnels ouverts, composé par le code, pour le tour terminal.

        **Ce n'est pas une sélection (AD-1).** Le code ne retient rien, ne classe rien, n'attribue
        rien à une sous-question : il liste *tout* ce que le modèle a ouvert et dont l'ingestion dit
        que c'est une clause qui décide, avec son `kind`, le titre de son nœud et ses premiers mots
        — de quoi le reconnaître, jamais de quoi le juger. La décision reste entière au modèle, et
        `CONSIGNE_INVENTAIRE` la lui demande bloc par bloc.

        **Pourquoi elle existe.** Sur les 17 ébauches intégrées du 03/09/2026, l'étage encore
        variable est l'omission d'une clause **lue** : ≈ 3 sur 17, dont r3 de la série finale A16 où
        `p34:12` est ouvert, transmis, et remplacé par la définition `p34:7`. Rien dans le fil ne
        rappelait au tour terminal ce qu'il avait sous les yeux : le texte des blocs y est, dispersé
        dans des résultats d'outils de plusieurs milliers de tokens, mais leur **liste** n'y est pas.
        Le code, lui, la connaît exactement. La lui donner ne choisit pas à sa place ; ne pas la lui
        donner lui fait choisir de mémoire.

        Les premiers mots sont recopiés du corpus, jamais reformulés, et bornés à une ligne : ce
        n'est pas une citation — seul `ouvrir_noeud` a rendu le bloc citable, et il l'a déjà fait.
        """
        blocs = self.blocs_decisionnels_ouverts()
        if not blocs:
            return None
        document = self.corpus.documents[self.doc_id]
        titres = {n.node_id: n.title for n in document.nodes}
        lignes = []
        for bloc in blocs:
            titre = titres.get(document.node_of(bloc.block_id), "")
            amorce = " ".join(bloc.text.split())[:INVENTAIRE_AMORCE_MAX_CHARS]
            lignes.append(f"- {bloc.block_id} ({bloc.kind}) — {titre} : « {amorce}… »")
        return (f"Blocs décisionnels que ta lecture a ouverts ({len(blocs)}) :\n"
                + "\n".join(lignes))

    async def rediger(self) -> tuple[AnswerDraft, StepTrace]:
        """L'ébauche terminale (AD-3), demandée dans le fil de la navigation."""
        return await self._rediger()

    async def _rediger(self, *, consigne: str | None = None, motif: str | None = None,
                       blocs_a_conserver: Iterable[str] = ()) -> tuple[AnswerDraft, StepTrace]:
        """L'ébauche, demandée dans le fil de la navigation — la première fois comme à la relance."""
        t0 = time.monotonic()
        step = StepTrace(name="rediger", tier=self.tier, prompt_cache=True,
                         opened_block_ids=list(self.ouverts))
        demande = [consigne] if consigne is not None else [DEMANDE_TERMINALE]
        inventaire = self.inventaire_decisionnel()
        if inventaire is not None:
            # Composé par le code depuis l'ingestion et le corpus (AD-15) : des `block_id`, des
            # `kind` et du texte de bloc, jamais un mot de la question ni une sortie de modèle.
            # Il voyage aussi à la relance : c'est le même tour terminal, et la relance est
            # précisément le moment où une clause acquise se perd.
            demande.append(inventaire)
            demande.append(CONSIGNE_INVENTAIRE)
        demande.append(
            f"Langue de rédaction : {LANGUES_SERVIES[self.parsed.language]} "
            f"({self.parsed.language}). Les citations restent recopiées mot pour mot dans la "
            "langue du bloc source.")
        acquis = [block_id for block_id in dict.fromkeys(blocs_a_conserver)
                  if block_id in self.ouverts]
        if acquis:
            # Relus parmi les blocs **ouverts** : la consigne ne peut pas être alimentée par un
            # identifiant inventé dans le motif du modèle (AD-15).
            demande.append(
                "Acquis à reconduire : " + ", ".join(acquis) + ". Conserve au moins une claim "
                "vérifiable pour chacun de ces blocs, avec ses sous-questions déjà traitées, en "
                "plus de corriger le motif ; ne remplace pas une preuve acquise par la nouvelle "
                "clause.")
        if motif is not None:
            # AD-15 : le motif vient de *vérifier*, composé depuis une sortie de modèle et du texte
            # des blocs — il est délimité comme tout le reste, jamais concaténé en clair.
            demande.append(untrusted("motif", motif))
        messages = [*self._messages, {"role": "user", "content": "\n\n".join(demande)}]
        try:
            resultat, messages = await self._appel_terminal(messages, step=step)
        except PipelineError as exc:
            step.ms = int((time.monotonic() - t0) * 1000)
            exc.step = step
            raise
        # Une lecture faite au tour terminal entre dans les blocs ouverts : la trace de l'étape la
        # publie comme les autres.
        step.opened_block_ids = list(self.ouverts)
        # Le fil garde l'ébauche : la relance corrige un texte que le modèle a **sous les yeux**.
        self._messages = [*messages, {"role": "assistant",
                                      "content": resultat.parsed.model_dump_json()}]
        draft = self._projeter(resultat.parsed, step=step)
        self._publier_les_ecarts(draft, step=step)
        if self.tour_terminal_force:
            step.checks.append(CheckResult(
                name="tour_terminal_force", ok=False,
                detail=f"{self.tour_terminal_force} appel(s) terminal(aux) ont demandé un outil "
                       "malgré `tool_choice` fermé : l'outil a été exécuté et l'ébauche redemandée "
                       "dans la borne des tours et du budget de lecture, au lieu d'un échec de "
                       "dialogue"))
        step.checks.append(CheckResult(
            name="ebauche_dans_la_conversation", ok=True,
            detail=f"ébauche rendue dans le fil de navigation ({len(self.ouverts)} bloc(s) "
                   f"ouvert(s) citables), {self.tours} tour(s) dont "
                   f"{self.tour_terminal_force} tour(s) terminal(aux) forcé(s), réflexion "
                   f"{sum(call.thinking for call in step.calls)} tokens"))
        step.ms = int((time.monotonic() - t0) * 1000)
        return draft, step

    def _publier_les_ecarts(self, draft: AnswerDraft, *, step: StepTrace) -> None:
        """AD-4 : ce que l'ébauche a décidé d'écarter se lit dans la trace, pas nulle part.

        Sans ce check, l'omission d'une clause lue est **silencieuse** : la réponse est cohérente,
        aucun contrôle ne rougit, et rien ne distingue « le modèle a jugé ce bloc hors sujet » de
        « le modèle l'a oublié ». Les deux se corrigent différemment. Le check est `ok=False` quand
        il y a un écart : ce n'est pas un reproche au modèle — écarter un bloc est une décision qui
        lui appartient — mais un fait que la lecture d'une trace doit rencontrer, comme
        `lecture_refusee` ou `tours_epuises`.

        Un `block_id` que la lecture n'a pas ouvert est rapporté à part : c'est une sortie de modèle
        comme une autre, et le recoupement se fait ici, sur ce que le code a réellement servi.
        """
        if not draft.blocs_ecartes:
            return
        ouverts = {bloc.block_id for bloc in self.blocs_decisionnels_ouverts()}
        connus = [e.block_id for e in draft.blocs_ecartes if e.block_id in ouverts]
        inconnus = len(draft.blocs_ecartes) - len(connus)
        detail = (f"{len(connus)} bloc(s) décisionnel(s) lu(s) écarté(s) par la rédaction "
                  f"({', '.join(connus) or 'aucun'}) sur {len(ouverts)} ouvert(s) : une décision "
                  "du modèle, tracée pour qu'une omission ne soit pas silencieuse")
        if inconnus:
            detail += (f" ; {inconnus} identifiant(s) écarté(s) ne correspondent à aucun bloc "
                       "décisionnel ouvert et ne sont pas republiés (AD-15)")
        step.checks.append(CheckResult(name="blocs_decisionnels_ecartes", ok=False, detail=detail))

    async def _appel_terminal(self, messages: list[dict[str, Any]], *, step: StepTrace
                              ) -> tuple[Any, list[dict[str, Any]]]:
        """L'appel qui rend l'ébauche : outils fermés, et une lecture de plus si le modèle insiste.

        La navigation reste une boucle d'outils jusqu'à `navigation_max_llm_turns` ; ce qui change
        ici est le **tour terminal**, demandé sans outils et annoncé comme tel. Le prototype ne
        connaissait pas ce cas parce que sa boucle continuait tant que le modèle appelait des outils
        et ne demandait l'ébauche qu'après un `end_turn` : la chaîne, elle, demande aussi l'ébauche
        quand la borne des tours est atteinte, et c'est là que le tour terminal partait avec les
        outils ouverts.

        Le refus qu'on remplace n'était pas une propriété du fournisseur mais une borne du code : si
        un appel censé être terminal appelle quand même un outil, on le sert et on redemande
        l'ébauche, dans la borne des tours **et** du budget de lecture — que `_ouvrir` applique
        inchangé. Jamais une erreur terminale pour une lecture inachevée.

        **Une sortie coupée par son plafond est redemandée une fois, à `low`.** Le second cas est
        symétrique du premier, et il n'est pas non plus une propriété du fournisseur : le tour
        terminal rend `stop_reason=max_tokens`, le client relance déjà une fois à consigne
        « concis » — au **même** effort — puis lève `llm_parse`, c'est-à-dire un 503 sur une lecture
        entière et une réflexion qui, elle, était allée au bout. Mesuré trois fois le 03/09/2026
        (gate Baloise 12 h 11 à `high`, 13 h 14 à 3 072, 13 h 43 à 5 056 et `medium`) : sur Sonnet 5
        la réflexion adaptative partage `max_tokens` avec le JSON et sa queue dépasse tout plafond
        que la deadline autorise. Ce qui manque n'est donc pas de la place — la deadline n'en a plus
        à donner (T13) — mais un tour qui ne réfléchisse pas : le même fil, dont le préfixe est en
        cache, une consigne courte composée par le code, et `output_config.effort="low"`.

        Trois bornes, et elles suffisent à ce que cette reprise ne puisse rien coûter d'imprévu :
        une seule reprise par tour terminal, jamais deux ; seulement sur `stop_reason=max_tokens`,
        jamais sur un schéma invalide ni un refus ; et seulement si le temps restant couvre la durée
        majorée d'un appel au plafond (C2) — sinon l'erreur de troncature reste telle quelle, au
        lieu d'un `Timeout` levé plus loin pour un appel qu'on savait perdu. C'est cette dernière
        borne qui rend la reprise compatible avec `deadline_s` **sans l'amender** : comptée au
        plafond, elle sortirait la chaîne de sa deadline (voir
        `tests/test_config.py::test_la_deadline_couvre_la_chaine_de_navigation_par_le_modele`), donc
        elle n'est tentée que quand le chemin réel a laissé de quoi la payer.
        """
        settings = self.settings
        effort_du_palier = (settings.navigation_draft_effort
                            if MODEL_CAPS[model_for(self.tier)]["effort"] else None)
        effort = effort_du_palier
        repris = False
        while True:
            try:
                resultat = await self.client.parse(
                    tier=self.tier, system_prefix=self.prefixe, messages=messages,
                    output_model=AnswerDraft, budget=self.request_budget, step=step,
                    tools=OUTILS, tool_choice=TOOL_CHOICE_AUCUN,
                    max_tokens=settings.navigation_rediger_max_tokens,
                    # L'effort ne se relève **que** sur ce tour-ci (`navigation_draft_effort`,
                    # `config.py`) : c'est l'unique appel de la chaîne qui choisit quelles clauses
                    # sont citées, et les trois runs A16 de `f858a28` le mesurent à 0 token de
                    # réflexion. Un palier épinglé sur un modèle sans `effort` n'en reçoit aucun —
                    # le client refuserait le paramètre (AD-9), même idiome que *rédiger*.
                    effort=effort,
                    thinking=REFLEXION_ADAPTATIVE)
            except ToolUseDemande as exc:
                if self.tours >= settings.navigation_max_llm_turns:
                    # La borne est une borne : au-delà, l'insistance du modèle n'est plus une
                    # lecture inachevée mais un dialogue qui ne se referme pas.
                    raise
                self.tours += 1
                self.tour_terminal_force += 1
                messages = self._servir_les_outils_du_tour_terminal(messages, exc.reponse)
                continue
            except LlmParse as exc:
                if repris or exc.stop_reason != "max_tokens" or effort_du_palier is None:
                    raise
                requise = settings.duree_majoree_pour(settings.navigation_rediger_max_tokens)
                if self.request_budget.remaining() < requise:
                    # C2, appliqué ici plutôt que subi plus loin : le client refuserait de lui-même
                    # cet appel, mais en `Timeout` — l'appelant perdrait la raison réelle de
                    # l'échec, qui est la troncature.
                    step.checks.append(CheckResult(
                        name="tour_terminal_repris", ok=False,
                        detail=f"tour terminal tronqué (stop_reason=max_tokens) non repris : "
                               f"{requise:.1f} s requises au débit minoré pour une reprise au "
                               f"plafond, {self.request_budget.remaining():.1f} s restantes"))
                    raise
                repris = True
                self.tour_terminal_repris += 1
                effort = EFFORT_REPRISE_TRONQUEE
                messages = [*messages,
                            {"role": "assistant", "content": REPRISE_ASSISTANT_TRONQUEE},
                            {"role": "user", "content": CONSIGNE_REPRISE_TRONQUEE}]
                step.checks.append(CheckResult(
                    name="tour_terminal_repris", ok=False,
                    detail=f"{self.tour_terminal_repris} tour(s) terminal(aux) tronqué(s) par leur "
                           f"propre sortie (max_tokens="
                           f"{settings.navigation_rediger_max_tokens}) : l'ébauche a été redemandée "
                           f"une fois dans le même fil à l'effort {EFFORT_REPRISE_TRONQUEE}, au lieu "
                           "d'un échec terminal"))
                continue
            return resultat, messages

    def _servir_les_outils_du_tour_terminal(self, messages: list[dict[str, Any]],
                                            reponse: Any) -> list[dict[str, Any]]:
        """Le tour d'outils que le modèle a réclamé au tour terminal, puis le rappel de l'ébauche.

        Le rappel voyage dans le **même** message que les résultats, en bloc `text` après eux : le
        modèle doit savoir où il en est au moment où il lit ce que son outil a rendu, et un message
        de plus ne le dirait pas mieux tout en allongeant le fil.
        """
        contenu = [b.model_dump(mode="json") if hasattr(b, "model_dump") else dict(b)
                   for b in reponse.content]
        rendus: list[dict[str, Any]] = [
            {"type": "tool_result", "tool_use_id": appel.get("id"),
             "content": self.executer(str(appel.get("name")), dict(appel.get("input") or {}))}
            for appel in contenu if appel.get("type") == "tool_use"]
        rendus.append({"type": "text", "text": RAPPEL_TERMINAL})
        return [*messages, {"role": "assistant", "content": contenu},
                {"role": "user", "content": rendus}]

    def _projeter(self, brut: AnswerDraft, *, step: StepTrace) -> AnswerDraft:
        """Les projections de *rédiger*, inchangées : fusion des extraits, jonction des amorces
        d'énumération, claims affichées."""
        draft, fusions = fusionner_quotes_du_meme_bloc(brut, index=self.index, doc_id=self.doc_id)
        if fusions:
            step.checks.append(CheckResult(
                name="quotes_fusionnees", ok=True,
                detail=f"{fusions} affirmation(s) citaient deux extraits d'un même bloc : fusionnés "
                       "en un passage contigu qui les couvre, au lieu d'un échec de schéma terminal"))
        draft, amorces = joindre_amorces_denumeration(draft, index=self.index,
                                                     doc_id=self.doc_id,
                                                     blocs_servis=self.ouverts)
        if amorces:
            step.checks.append(CheckResult(
                name="amorce_jointe", ok=True,
                detail=f"{amorces} citation(s) d'un item d'énumération n'emportaient pas la phrase "
                       "qui l'ouvre : l'amorce a été jointe telle quelle à la même affirmation, "
                       "comme son contexte — le contrôle juge une clause entière"))
        if self.faits is None:
            return draft
        draft, _changements = rattacher_claims_sinistre(
            draft, max_claims=self.settings.navigation_draft_max_claims,
            max_segments=self.settings.navigation_draft_max_segments)
        if len(draft.claims) < len(brut.claims):
            step.checks.append(CheckResult(
                name="claims_hors_borne_ecartees", ok=False,
                detail=f"{len(brut.claims) - len(draft.claims)} claim(s) au-delà de "
                       "navigation_draft_max_claims écartée(s) mécaniquement avant vérification : "
                       "la borne annoncée au prompt fait foi"))
        return draft

    async def relancer(self, motif: str, *,
                       blocs_a_conserver: Iterable[str] = ()) -> tuple[AnswerDraft, StepTrace]:
        """La relance d'AD-3 : **un message de plus** dans la conversation, pas un second dialogue.

        Le préfixe — sommaire compris — est déjà écrit et relu au tarif de cache ; ce que la relance
        paie est ce qu'elle ajoute. Le modèle garde sous les yeux ce qu'il a lu **et** ce qu'il a
        rédigé : la consigne « conserve les acquis » cesse d'être une demande de mémoire.
        """
        return await self._rediger(
            blocs_a_conserver=blocs_a_conserver,
            consigne="Ton ébauche précédente a été contrôlée. La lecture est close et les outils "
                     "sont fermés : corrige exactement ce que le motif ci-dessous décrit, conserve "
                     "les affirmations déjà acquises, et rends une ébauche `AnswerDraft` complète, "
                     "en JSON et sans appel d'outil — pas seulement la correction.",
            motif=motif)

    # --- ce que l'étape rend ---------------------------------------------------------------

    def retrieval(self) -> RetrievalResult:
        """La lecture, telle que le modèle l'a faite, dans la forme que le reste de la chaîne lit.

        `facettes` reste **vide** : aucune couverture par sous-question n'est calculée ici, parce
        qu'aucune passe de code n'attribue plus un bloc à une sous-question. La couverture est
        mesurée par *vérifier* sur les affirmations **affichées** (AD-4), et renvoyée au modèle
        comme consigne — jamais employée pour choisir un bloc.
        """
        return RetrievalResult(
            blocs=list(self.ouverts.values()),
            opened_block_ids=list(self.ouverts),
            opened_node_ids=list(self.noeuds_ouverts),
            discarded_block_ids=list(dict.fromkeys(self.refuses)),
            truncated=getattr(self, "_tronquee", False))
