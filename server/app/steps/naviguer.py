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
from collections.abc import Iterable
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
from server.app.domain.errors import PipelineError
from server.app.domain.langue import LANGUES_SERVIES
from server.app.domain.question import Faits, ParsedQuestion, Turn
from server.app.domain.retrieval import BudgetSnapshot, RetrievalResult
from server.app.domain.trace import CheckResult, StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient, ToolUseDemande
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
    base = min((n.level for n in noeuds), default=0)
    return "\n".join(f"{'  ' * max(0, n.level - base)}{n.node_id} — {n.title}"
                     for n in noeuds if n.node_id != doc_id)


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


def _rendre_blocs(blocs: Iterable[Block]) -> str:
    return "\n\n".join(f"[{b.block_id}] ({b.kind})\n{b.text}" for b in blocs)


class Navigation:
    """Une conversation de navigation : ce qui a été lu, ce que la lecture a coûté, ce qu'elle rend.

    L'objet vit le temps d'une requête et porte le fil : la même instance sert la lecture, l'ébauche
    et la relance, parce que ce sont trois messages d'un même dialogue et non trois appels.
    """

    def __init__(self, parsed: ParsedQuestion, *, corpus: Corpus, index: Index,
                 dictionnaire: Dictionnaire | None, doc_id: str, settings: Settings,
                 client: LlmClient, request_budget: RequestBudget, prompt: str,
                 faits: Faits | None = None, historique: Iterable[Turn] = ()) -> None:
        if doc_id not in corpus.documents:
            raise KeyError(doc_id)
        self.parsed, self.corpus, self.index = parsed, corpus, index
        self.dictionnaire, self.doc_id, self.settings = dictionnaire, doc_id, settings
        self.client, self.request_budget, self.prompt = client, request_budget, prompt
        self.faits, self.historique = faits, list(historique)
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
        """
        settings = self.settings
        while True:
            try:
                resultat = await self.client.parse(
                    tier=self.tier, system_prefix=self.prefixe, messages=messages,
                    output_model=AnswerDraft, budget=self.request_budget, step=step,
                    tools=OUTILS, tool_choice=TOOL_CHOICE_AUCUN,
                    max_tokens=settings.navigation_rediger_max_tokens,
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
