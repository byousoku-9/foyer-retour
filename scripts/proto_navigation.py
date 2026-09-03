"""Prototype à nu : le modèle navigue le document lui-même, le code ne fait que servir et vérifier.

**Ce que ce script abandonne**, et c'est tout son objet : les réservations par sous-question,
l'attribution lexicale, les passes de code qui choisissent des blocs. Rien ici ne décide à la place
du modèle. Le code sert quatre choses — le sommaire complet du document, trois outils, un plafond,
et la vérification au caractère près de ce qui est cité — et il audite tout.

`chercher` est une **proposition** : elle rend des extraits classés par mots (et par le dictionnaire
du document), jamais une décision. Le modèle ouvre ce qu'il veut, lit le texte intégral, et cite
dans la **même** conversation ce qu'il a lu. Le code vérifie ensuite chaque citation mot pour mot
dans le texte relu depuis le corpus, plafonne la lecture, le coût et le temps.

Ce n'est pas branché sur le pipeline : c'est une sonde, elle ne touche pas `server/app`.

    uv run python -m scripts.proto_navigation --cas a16 --dry-run     # sans clé, sans réseau
    uv run python -m scripts.proto_navigation --cas a16               # avec la clé, ~0,10 €
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from server.app.config import get_settings
from server.app.corpus.dictionary import Dictionnaire, load_dictionary
from server.app.corpus.index import Index
from server.app.corpus.loader import Corpus, load_corpus
from server.app.corpus.text import normalize, normalize_spans
from server.app.domain.document import Block, is_citable
from server.app.domain.errors import PipelineError
from server.app.domain.trace import StepTrace
from server.app.llm.audit import JsonlAuditSink
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import MODEL_CAPS, TIERS, Tier
from server.app.llm.pricing import estimate_cost, estimate_tokens
from server.app.llm.prompting import untrusted
from server.app.steps.retrouver import (_premier_objet_json, _variantes_de_facette,
                                        part_du_mot_borne)

AXA = "axa-lu-optihome-2017"

# Les trois cas servis. Aucun n'entre dans le prompt autrement que par `question` et `faits` : les
# `attendus` ne sont **jamais** montrés au modèle, ils ne servent qu'au témoin imprimé en fin de run.
CAS: dict[str, dict[str, Any]] = {
    "a16": {
        "doc_id": AXA,
        "question": "La vitre de l'insert a éclaté toute seule et la fumée a noirci le salon, "
                    "sans incendie : quels dommages regarder ?",
        "faits": "La vitre de l'insert de cheminée a éclaté toute seule pendant une flambée ; "
                 "la fumée a noirci le salon. Aucun incendie, aucune flamme hors du foyer.",
        # « p34:11 ou une claim sur la fumée » : la seconde branche est vérifiée sur le texte cité.
        "attendus": [["p34:12"], ["p34:11", "@fumee"], ["p39:9"]],
    },
    "bougie": {
        "doc_id": AXA,
        "question": "Ce sinistre est-il couvert par les conditions générales du contrat ?",
        "faits": "Une bougie allumée posée sur une table basse est tombée sur le canapé. "
                 "Le mobilier de salon a brûlé sur une partie, sans embrasement ni commencement "
                 "d'incendie : il n'y a eu ni flammes propagées, ni dégât au bâtiment.",
        "attendus": [["p34:12"]],
    },
    "libre": {"doc_id": AXA, "question": "", "faits": "", "attendus": []},
}

SYSTEME = """\
Tu es juriste d'assurance. Tu réponds sur **un seul** document contractuel, dont le sommaire complet
t'est donné ci-dessous : chaque ligne est un nœud, `node_id` puis titre, indenté par profondeur.

Tu disposes de trois outils :
- `sommaire(node_id)` — la sous-arborescence d'un nœud (sans argument : tout le document) ;
- `ouvrir_noeud(node_id)` — le **texte intégral** des blocs citables du nœud et de ses enfants
  feuilles, chacun avec son `block_id` et son `kind` ;
- `chercher(termes)` — des candidats classés par mots, avec un extrait. C'est une **proposition**,
  jamais une décision : un extrait n'est pas une lecture, et le classement peut se tromper. Ouvre le
  nœud avant de te prononcer.

Règles, sans exception :
1. **Ne cite que ce que tu as ouvert.** Une citation doit venir d'un bloc rendu par `ouvrir_noeud`
   dans cette conversation. Un extrait de `chercher` n'autorise pas à citer.
2. **Recopie mot pour mot.** La citation est vérifiée caractère par caractère dans le texte du bloc.
   Ne reformule pas, ne coupe pas au milieu d'un mot, ne mets pas de crochets.
3. Cherche les dispositions **qui décident** : ce qui est garanti, ce qui est exclu, à quelles
   conditions, avec quelles franchises ou limites. Une définition ne décide pas seule.
4. Décompose la question en sous-questions et traite-les **toutes**. Celle que le document ne
   soutient pas va dans `non_trouve` : ne l'oublie pas et ne l'invente pas.

Quand tu as fini de lire, rends — sans appeler d'outil — un dernier message contenant **un seul**
objet JSON de cette forme, et rien d'utile en dehors :

{"claims": [{"block_id": "…", "quote": "le passage recopié mot pour mot", "texte": "ce qu'il dit,
en français, pour l'assuré"}],
 "sous_questions": [{"libelle": "…", "block_ids": ["…"]}],
 "non_trouve": ["ce que le document ne tranche pas"]}
"""

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
]

TIER_PAR_MODELE: dict[str, Tier] = {model: tier for tier, model in TIERS.items()}


# --- ce que le code sert -------------------------------------------------------------------


def sommaire_complet(corpus: Corpus, doc_id: str, *, racine: str | None = None) -> str:
    """Tous les nœuds du document (ou du sous-arbre de `racine`), `node_id` + titre, indentés.

    Pas de pagination, pas d'aperçu, pas de budget : c'est le point du prototype. `Index.sommaire_page`
    existe et pagine sur un budget de contexte ; ici on veut mesurer ce que coûte la carte entière.
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

    Un nœud du contrat AXA est souvent un intertitre dont la règle vit dans un enfant d'un bloc ;
    servir le seul nœud demandé obligerait le modèle à un tour par ligne d'énumération. La règle est
    structurelle et vaut pour tout document : aucun cas particulier.
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


def rendre_blocs(blocs: list[Block]) -> str:
    return "\n\n".join(f"[{b.block_id}] ({b.kind})\n{b.text}" for b in blocs)


def chercher(index: Index, dictionnaire: Dictionnaire, settings: Any, *, doc_id: str,
             termes: list[str], limit: int) -> list[dict[str, Any]]:
    """`Index.chercher` élargi par le dictionnaire **et** par les formes de nombre, avec extrait.

    Les trois pièces existent et sont reprises telles quelles : `Dictionnaire.expand` (les
    équivalences écrites d'AD-5, bornées par la fréquence documentaire via `part_du_mot_borne`) et
    `_variantes_de_facette` (la règle de nombre du français, appliquée **aux requêtes seulement**,
    tour 3 R2). Sans la seconde, l'index est littéral au point d'être trompeur : « fumée » ne trouve
    pas « Les fumées et les suies », et le modèle conclurait à tort que le document est muet. Une
    recherche qui manque le seul bloc décisif n'est pas une proposition, c'est un contresens.
    """
    borne = part_du_mot_borne(index, doc_id, part_max=settings.dictionnaire_variante_max_part)
    mapping = dictionnaire.expand(termes, part_du_mot=borne,
                                  part_max=settings.dictionnaire_variante_max_part)
    for terme, variantes in mapping.items():
        for forme in _variantes_de_facette([terme, *variantes], index=index, doc_id=doc_id,
                                           part_max=settings.facette_variante_max_part):
            if forme not in variantes:
                variantes.append(forme)
    hits = index.chercher(mapping, limit=limit, doc_id=doc_id)
    return [{"block_id": h.clause_uid, "node_id": h.node_uid,
             "kind": index.corpus.documents[doc_id].block(h.clause_uid).kind,
             "titre": h.title, "extrait": h.excerpt} for h in hits]


# --- ce que le code vérifie ----------------------------------------------------------------


def verifier_citation(quote: str, block: Block, *, corpus: Corpus, doc_id: str) -> dict[str, Any]:
    """La citation est-elle dans le bloc, mot pour mot ? Même logique que *vérifier* (AD-3).

    `normalize` est l'unique convention Texte du dépôt ; `normalize_spans` retraduit l'occurrence
    prouvée dans le texte **brut**, relu depuis le corpus. La quote rendue par le modèle n'est jamais
    ce qu'on affiche : c'est le passage d'origine.
    """
    forme = normalize(quote)
    if not forme:
        return {"verifiee": False, "motif": "citation vide"}
    debut = block.text_norm.find(forme)
    if debut < 0:
        return {"verifiee": False, "motif": "citation introuvable dans le bloc (pas mot pour mot)"}
    autre = next((b.block_id for b in corpus.documents[doc_id].blocks
                  if b.block_id != block.block_id and forme in b.text_norm), None)
    if autre is not None:
        return {"verifiee": False, "motif": f"citation ambiguë : figure aussi dans {autre}"}
    _norme, spans = normalize_spans(block.text)
    fin = debut + len(forme)
    return {"verifiee": True, "motif": "",
            "passage": block.text[spans[debut][0]:spans[fin - 1][1]],
            "offsets": [spans[debut][0], spans[fin - 1][1]]}


# --- la boucle -----------------------------------------------------------------------------


class Navigation:
    """État d'un run : ce qui a été ouvert, ce que la lecture a coûté, ce que les outils ont rendu."""

    def __init__(self, *, corpus: Corpus, index: Index, dictionnaire: Dictionnaire, doc_id: str,
                 settings: Any, budget_tokens: int) -> None:
        self.corpus, self.index, self.dictionnaire = corpus, index, dictionnaire
        self.doc_id, self.settings = doc_id, settings
        self.budget_tokens = budget_tokens
        self.tokens_lus = 0
        self.ouverts: dict[str, Block] = {}
        self.noeuds_ouverts: list[str] = []
        self.recherches: list[list[str]] = []
        self.refus_budget = 0

    def executer(self, nom: str, args: dict[str, Any]) -> str:
        try:
            if nom == "sommaire":
                node_id = args.get("node_id")
                return sommaire_complet(self.corpus, self.doc_id, racine=node_id) or \
                    "ce nœud n'a pas de sous-arborescence : ouvre-le."
            if nom == "ouvrir_noeud":
                return self._ouvrir(str(args["node_id"]))
            if nom == "chercher":
                termes = [str(t) for t in args.get("termes") or []]
                if not termes:
                    return "aucun terme : donne au moins un mot à chercher."
                self.recherches.append(termes)
                hits = chercher(self.index, self.dictionnaire, self.settings,
                                doc_id=self.doc_id, termes=termes, limit=20)
                if not hits:
                    return "aucun candidat pour ces termes."
                return json.dumps(hits, ensure_ascii=False, indent=1)
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
        rendu = rendre_blocs(blocs)
        cout = estimate_tokens(rendu, self.settings)
        if self.tokens_lus + cout > self.budget_tokens:
            self.refus_budget += 1
            return (f"budget de lecture insuffisant : ce nœud coûte ≈ {cout} tokens, il en reste "
                    f"{self.budget_tokens - self.tokens_lus} sur {self.budget_tokens}. Ouvre un nœud "
                    "plus précis, ou conclus avec ce que tu as déjà lu.")
        self.tokens_lus += cout
        self.noeuds_ouverts.append(node_id)
        for bloc in blocs:
            self.ouverts[bloc.block_id] = bloc
        return rendu


async def naviguer(cas: dict[str, Any], args: argparse.Namespace) -> int:
    settings = get_settings().model_copy(update={
        "deadline_s": args.deadline, "llm_timeout_s": min(args.deadline - 1.0, 600.0)})
    doc_id = cas["doc_id"]
    corpus = load_corpus(args.data, allow_ungated=True)
    if doc_id not in corpus.documents:
        print(f"document absent du corpus servi : {doc_id}", file=sys.stderr)
        return 2
    index = Index(corpus, excerpt_max_chars=settings.excerpt_max_chars)
    dictionnaire = load_dictionary(args.data, corpus, doc_id)

    sommaire = sommaire_complet(corpus, doc_id)
    prefixe = SYSTEME + "\n\n" + untrusted("sommaire", sommaire)
    tier = TIER_PAR_MODELE[args.model]
    print(f"document        : {doc_id} ({len(corpus.documents[doc_id].nodes)} nœuds)")
    print(f"sommaire complet: {len(sommaire)} caractères, ≈ {estimate_tokens(sommaire, settings)} tokens "
          f"(majorant hors ligne d'`estimate_tokens`)")
    print(f"préfixe système : ≈ {estimate_tokens(prefixe, settings)} tokens "
          f"(sommaire compris), mis en cache")
    print(f"modèle          : {args.model} (tier {tier}, TTL de cache "
          f"{MODEL_CAPS[args.model]['cache_ttl']})")
    if args.sommaire_seulement:
        return 0

    # Le plafond du run porte sur le coût **réel**, mesuré entre les tours. Le garde-fou du client,
    # lui, compare un **majorant** (sortie entière facturée au tarif de sortie) : réglé sur
    # `--max-cost`, il refuserait le deuxième appel d'un run qui n'a rien dépensé. On lui laisse la
    # place des tours prévus, et c'est la mesure réelle qui arrête le run.
    majorant_appel = estimate_cost(args.model, [{"type": "text", "text": prefixe}], [],
                                   args.max_tokens, settings, tools=OUTILS)
    budget = RequestBudget(deadline_s=args.deadline, max_attempts=args.max_tours + 1,
                           max_cost_eur=args.max_cost + args.max_tours * majorant_appel)
    audit = JsonlAuditSink(Path(args.audit))
    fake = _FauxAnthropic(_scenario_dry_run(corpus, doc_id)) if args.dry_run else None
    client = LlmClient(settings, anthropic_client=fake, audit_sink=audit)
    step = StepTrace(name="proto_navigation", tier=tier, prompt_cache=True)
    nav = Navigation(corpus=corpus, index=index, dictionnaire=dictionnaire, doc_id=doc_id,
                     settings=settings, budget_tokens=args.budget_tokens)

    demande = {"question": cas["question"], "faits": cas["faits"]}
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": untrusted("demande", json.dumps(demande, ensure_ascii=False))}]

    t0 = time.monotonic()
    tours = 0
    arret = ""
    dernier_texte = ""
    while tours < args.max_tours:
        if budget.cost_eur >= args.max_cost:
            arret = f"plafond de coût atteint ({budget.cost_eur:.4f} € ≥ {args.max_cost:.4f} €)"
            break
        tours += 1
        try:
            resultat = await client.tool_turn(
                tier=tier, system_prefix=prefixe, messages=messages, tools=OUTILS,
                budget=budget, step=step, max_tokens=args.max_tokens, prompt_cache=True)
        except PipelineError as exc:
            arret = f"{type(exc).__name__}: {exc}"
            break
        message = resultat.message
        contenu = [b.model_dump(mode="json") if hasattr(b, "model_dump") else dict(b)
                   for b in message.content]
        messages = [*messages, {"role": "assistant", "content": contenu}]
        dernier_texte = "".join(b.get("text", "") for b in contenu if b.get("type") == "text")
        appels = [b for b in contenu if b.get("type") == "tool_use"]
        print(f"  tour {tours} — {message.stop_reason}, {len(appels)} appel(s) d'outil, "
              f"{resultat.usage.cost_eur:.4f} €, lecture {nav.tokens_lus}/{args.budget_tokens} tokens")
        if not appels:
            arret = arret or f"fin de tour ({message.stop_reason})"
            break
        resultats = []
        for appel in appels:
            sortie = nav.executer(str(appel.get("name")), dict(appel.get("input") or {}))
            print(f"      {appel.get('name')}({json.dumps(appel.get('input'), ensure_ascii=False)[:120]})"
                  f" → {len(sortie)} car.")
            resultats.append({"type": "tool_result", "tool_use_id": appel.get("id"),
                              "content": sortie})
        messages = [*messages, {"role": "user", "content": resultats}]
    else:
        arret = f"plafond de tours atteint ({args.max_tours})"

    duree = time.monotonic() - t0
    rapport = _rendre(dernier_texte, nav, corpus=corpus, doc_id=doc_id)
    _imprimer(rapport, cas=cas, nav=nav, budget=budget, duree=duree, tours=tours, arret=arret,
              audit=args.audit)
    return 0


def _rendre(texte: str, nav: Navigation, *, corpus: Corpus, doc_id: str) -> dict[str, Any]:
    """Le JSON terminal du modèle, avec chaque claim vérifiée contre le bloc qu'elle cite."""
    brut = _premier_objet_json(texte)
    if brut is None:
        return {"lisible": False, "claims": [], "sous_questions": [], "non_trouve": []}
    try:
        sortie = json.loads(brut)
    except json.JSONDecodeError as exc:
        return {"lisible": False, "erreur": str(exc), "claims": [],
                "sous_questions": [], "non_trouve": []}
    claims = []
    for claim in sortie.get("claims") or []:
        block_id = str(claim.get("block_id", ""))
        quote = str(claim.get("quote", ""))
        bloc = nav.ouverts.get(block_id)
        if bloc is None:
            controle = {"verifiee": False,
                        "motif": "bloc jamais ouvert dans cette conversation : non citable"}
        else:
            controle = verifier_citation(quote, bloc, corpus=corpus, doc_id=doc_id)
        claims.append({"block_id": block_id, "quote": quote,
                       "texte": str(claim.get("texte", "")), **controle})
    return {"lisible": True, "claims": claims,
            "sous_questions": sortie.get("sous_questions") or [],
            "non_trouve": sortie.get("non_trouve") or []}


def _imprimer(rapport: dict[str, Any], *, cas: dict[str, Any], nav: Navigation,
              budget: RequestBudget, duree: float, tours: int, arret: str, audit: str) -> None:
    print(f"\narrêt           : {arret or 'fin normale'}")
    if not rapport["lisible"]:
        print("verdict         : JSON terminal illisible")
    print(f"\nclaims ({len(rapport['claims'])}) :")
    for claim in rapport["claims"]:
        etat = "VERIFIEE" if claim["verifiee"] else f"REJETEE ({claim['motif']})"
        print(f"  [{claim['block_id']}] {etat}")
        print(f"      « {claim['quote'][:160]} »")
        if claim["texte"]:
            print(f"      → {claim['texte'][:200]}")
    print("\nsous-questions :")
    for sq in rapport["sous_questions"]:
        print(f"  - {sq.get('libelle', '')} : {', '.join(sq.get('block_ids') or []) or '—'}")
    for absent in rapport["non_trouve"]:
        print(f"  - non trouvé : {absent}")

    verifiees = [c for c in rapport["claims"] if c["verifiee"]]
    if cas["attendus"]:
        print("\ntémoin (jamais soufflé au modèle) :")
        for alternatives in cas["attendus"]:
            atteint = any(
                any(c["block_id"].endswith(":" + a) for c in verifiees) if not a.startswith("@")
                else any(a[1:] in normalize(c["quote"] + " " + c["texte"]) for c in verifiees)
                for a in alternatives)
            print(f"  {'OK ' if atteint else 'MANQUE'} {' ou '.join(alternatives)}")

    print(f"\ntours           : {tours}")
    print(f"nœuds ouverts   : {len(nav.noeuds_ouverts)} — {', '.join(nav.noeuds_ouverts) or '—'}")
    print(f"blocs citables  : {len(nav.ouverts)}")
    print(f"recherches      : {len(nav.recherches)}")
    print(f"lecture         : {nav.tokens_lus}/{nav.budget_tokens} tokens "
          f"({nav.refus_budget} ouverture(s) refusée(s))")
    print(f"coût réel       : {budget.cost_eur:.4f} € ({budget.attempts} appel(s))")
    print(f"durée           : {duree:.1f} s")
    print(f"audit           : {audit}")


class _FauxAnthropic:
    """Un fournisseur simulé, pour prouver hors réseau que la boucle d'outils et le contrôle tournent.

    Il rejoue exactement ce qu'un modèle ferait sur A16 : chercher, ouvrir, puis citer. La citation
    du dernier tour est **recopiée depuis le corpus** par le script d'appel (voir `--dry-run`), donc
    la vérification est réelle : elle échoue si `normalize` ou les offsets se cassent.
    """

    def __init__(self, scenario: list[dict[str, Any]]) -> None:
        self.messages = self
        self._tour = 0
        self.scenario = scenario

    async def create(self, **kwargs: Any) -> Any:
        import anthropic

        from tests.llm_fake import fake_message

        etape = self.scenario[min(self._tour, len(self.scenario) - 1)]
        self._tour += 1
        return anthropic.types.Message.model_validate(
            fake_message(content=etape["content"], model=kwargs["model"],
                         stop_reason=etape["stop_reason"]))


def _scenario_dry_run(corpus: Corpus, doc_id: str) -> list[dict[str, Any]]:
    """Les trois tours simulés : `chercher`, `ouvrir_noeud`, puis le JSON terminal.

    Le nœud ouvert et la quote sortent du corpus **au moment du run** : rien n'est codé en dur, et
    la vérification finale prouve la chaîne `normalize` → offsets → passage brut.
    """
    doc = corpus.documents[doc_id]
    # Un nœud dont un bloc porte assez de texte pour qu'une citation partielle soit unique : c'est le
    # cas nominal qu'on veut prouver (`verifiee=True`), pas un titre que deux nœuds répètent.
    noeud, bloc = next(
        (n, b) for n in doc.nodes if n.node_id != doc_id
        for b in blocs_du_noeud(corpus, doc_id, n.node_id) if len(b.text) > 300)
    quote = " ".join(bloc.text.split())[20:160]
    verdict = {"claims": [{"block_id": bloc.block_id, "quote": quote,
                           "texte": "claim simulée : la vérification doit la retrouver."},
                          {"block_id": "inexistant:0", "quote": quote,
                           "texte": "claim simulée sur un bloc jamais ouvert : doit être rejetée."}],
               "sous_questions": [{"libelle": "sous-question simulée",
                                   "block_ids": [bloc.block_id]}],
               "non_trouve": ["ce que le faux client ne cherche pas"]}
    return [
        {"stop_reason": "tool_use", "content": [
            {"type": "text", "text": "Je cherche d'abord."},
            {"type": "tool_use", "id": "t1", "name": "chercher",
             "input": {"termes": ["vitre", "fumee"]}}]},
        {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t2", "name": "ouvrir_noeud",
             "input": {"node_id": noeud.node_id}}]},
        {"stop_reason": "end_turn", "content": [
            {"type": "text", "text": json.dumps(verdict, ensure_ascii=False)}]},
    ]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cas", choices=sorted(CAS), default="a16")
    parser.add_argument("--question", default="", help="cas `libre` : la question")
    parser.add_argument("--faits", default="", help="cas `libre` : les faits")
    parser.add_argument("--doc-id", default="", help="cas `libre` : le document (défaut AXA)")
    parser.add_argument("--model", choices=sorted(TIER_PAR_MODELE), default="claude-sonnet-5")
    parser.add_argument("--max-tours", type=int, default=8)
    parser.add_argument("--budget-tokens", type=int, default=12000)
    parser.add_argument("--max-tokens", type=int, default=16000)
    parser.add_argument("--max-cost", type=float, default=0.6, help="plafond de coût réel du run, €")
    parser.add_argument("--deadline", type=float, default=1800.0)
    parser.add_argument("--data", default="data")
    parser.add_argument("--audit", default=".audit/proto-navigation.jsonl")
    parser.add_argument("--dry-run", action="store_true",
                        help="sans clé ni réseau : faux fournisseur, boucle et contrôle réels")
    parser.add_argument("--sommaire-seulement", action="store_true",
                        help="composer et mesurer le sommaire, sans aucun appel")
    args = parser.parse_args(argv)

    cas = dict(CAS[args.cas])
    if args.cas == "libre":
        if not args.question:
            parser.error("--cas libre exige --question")
        cas["question"], cas["faits"] = args.question, args.faits
        cas["doc_id"] = args.doc_id or cas["doc_id"]

    return asyncio.run(naviguer(cas, args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
