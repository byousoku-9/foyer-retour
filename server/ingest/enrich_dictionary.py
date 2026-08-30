"""AD-5 / AD-7 — L'ingestion hors ligne qui écrit `data/dictionary.json`, et la signature qui l'arme.

```bash
uv run python -m server.ingest.enrich_dictionary --dry-run      # le plan et le majorant, rien d'écrit
uv run python -m server.ingest.enrich_dictionary                # écrit le dictionnaire, `validated: false`
uv run python -m server.ingest.enrich_dictionary --valider "Nom" # la seule chose qui arme le refus
```

**Pourquoi ce module importe `anthropic` en direct**, au lieu de passer par `server/app/llm/` :
`pipeline_digest` (AD-10) couvre `steps`, `pipelines`, `corpus` et `llm`. Faire passer
l'enrichissement par `llm/` mettrait son code dans cette empreinte, si bien que **régénérer un
dictionnaire** rendrait les deux gates `gate_perime` — pour un fichier que le serveur n'exécute
jamais. `server/ingest/fetch_source.py` importe déjà `httpx` de la même façon, `tests/test_layers.py`
ne couvre que `server/app`, et la table des couches du spine ne nomme pas `ingest`. Ce qui **reste**
partagé, parce qu'il n'y a qu'une autorité pour ça : la table des tiers (`llm/models.py`) et la table
des prix (`llm/pricing.py`), lues sans être modifiées.

**Une requête par unité documentaire** : pour le guide structuré, l'unité historique reste sa
catégorie et ses fiches. Pour un contrat, l'unité est un groupe borné de blocs citables appartenant
à un même vrai nœud ; les extraits envoyés sont exactement ceux de ces blocs (un bloc individuel
trop long est préfixé et signalé comme tel). Aucune fiche ni hiérarchie n'est inventée. Une requête
de plus porte les déclencheurs d'intention.

**Le majorant est calculé avant soumission** et comparé à `dictionary_max_cost_eur` : le run refuse
de démarrer plutôt que de découvrir la facture après coup (AD-1, AD-9).

**Le code ne fait pas confiance au modèle** (AD-5 / AD-7 / FR29 : « le modèle d'ingestion ne renvoie
jamais de texte de bloc »). Chaque chaîne rendue passe les bornes de `config.py` et le contrôle
« chaîne recopiée d'un bloc » ; une chaîne hors borne est **écartée**, jamais tronquée, et l'écart
est compté puis affiché. Un `fiche_id` inconnu est écarté de même.

Le transport historique reste Batch par défaut. En développement, `--transport standard` part de
zéro par `messages.create`, sans Batch ni retry ; son majorant et son coût réel sont calculés sans
remise Batch.

Codes de sortie : `0` ok · `2` pas de clé · `3` majorant dépassé · `4` aucun résultat exploitable
· `5` `--valider` sur un corpus périmé.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

from server.app.config import REPO_ROOT, Settings, cle_absente, get_settings
from server.app.corpus.index import words
from server.app.corpus.loader import Corpus, load_corpus, perimetre
from server.app.corpus.text import normalize
from server.app.domain.dictionary import (
    DICTIONARY_FILE,
    INTENTS_DU_DICTIONNAIRE,
    SCHEMA_VERSION,
    DictionaryFile,
)
from server.app.domain.document import DOC_ID_MAX, DOC_ID_RE, Block, is_citable
from server.app.llm.models import EFFORT, TIERS
from server.app.llm.pricing import BATCH_DISCOUNT, cost_from_usage, estimate_cost

from .artifacts import LectureDuLot, exiger_espace_installe, republier, write_atomic

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
TIER = "ingest"
MODEL = TIERS[TIER]
# `- `lux-guide:farrivee` · Titre · Résumé… · tags : a, b, c` — la ligne de fiche de `summary.md`.
# Les tags et les résumés ne sont **pas** dans `document.json` (reprise différée `target_story: 2.1`,
# revue 1.1) : le sommaire est le seul artefact qui les porte, et il est écrit par l'ingestion du
# guide, donc couvert par `ingest_fingerprint`. Le lire ici évite d'ajouter un champ à `Node` — et
# donc de rejouer l'ingestion du guide — pour une information qui ne sert qu'à ce prompt.
_LIGNE_FICHE = re.compile(r"^- `([^`]+)` · (.+)$")


# --- ce que le modèle rend (schémas de sortie structurée) ------------------

class TermeRendu(BaseModel):
    fiche_id: str
    canonique: str
    variantes: list[str]


class QuestionRendue(BaseModel):
    fiche_id: str
    question: str


class SortieCategorie(BaseModel):
    termes: list[TermeRendu]
    questions: list[QuestionRendue]


class DeclencheursRendus(BaseModel):
    intent: str
    declencheurs: list[str]


class SortieIntents(BaseModel):
    intents: list[DeclencheursRendus]


# --- lecture du corpus -----------------------------------------------------

class Fiche(BaseModel):
    fiche_id: str
    titre: str
    resume: str = ""
    tags: list[str] = []


class Categorie(BaseModel):
    node_id: str
    titre: str
    fiches: list[Fiche]


class ExtraitContrat(BaseModel):
    """Un vrai bloc, éventuellement borné par préfixe — jamais une fiche ou un résumé fabriqué."""

    block_id: str
    node_id: str
    node_title: str
    text: str
    truncated: bool = False


class UniteContrat(BaseModel):
    """Une unité de transport, identifiée par son premier bloc réel et son vrai propriétaire."""

    unit_id: str
    node_id: str
    node_title: str
    extraits: list[ExtraitContrat]


def _fiches_du_sommaire(summary: str) -> dict[str, tuple[str, list[str]]]:
    """`{fiche_id: (résumé, tags)}` lus dans `summary.md` ; une ligne illisible est simplement ignorée."""
    out: dict[str, tuple[str, list[str]]] = {}
    for ligne in summary.splitlines():
        m = _LIGNE_FICHE.match(ligne.strip())
        if m is None:
            continue
        champs = [c.strip() for c in m.group(2).split(" · ")]
        resume = champs[1] if len(champs) > 1 else ""
        tags: list[str] = []
        for champ in champs[2:]:
            if champ.startswith("tags :"):
                tags = [t.strip() for t in champ[len("tags :"):].split(",") if t.strip()]
        out[m.group(1)] = (resume, tags)
    return out


def categories(corpus: Corpus, doc_id: str, settings: Settings | None = None) \
        -> list[Categorie | UniteContrat]:
    """Unités honnêtes du document, sans modifier la projection historique du guide.

    Le guide conserve strictement ses catégories de niveau 1 et leurs fiches directes. Tout autre
    document est projeté depuis ses blocs citables, groupés par leur véritable nœud propriétaire,
    puis bornés en nombre et en caractères. `unit_id` est le premier `block_id` du groupe : c'est
    une identité source existante, pas un nœud synthétique.
    """
    doc = corpus.documents[doc_id]
    if doc.kind != "guide":
        s = settings or Settings(_env_file=None, anthropic_api_key="")
        par_noeud = {n.node_id: n for n in doc.nodes}
        groupes: dict[str, list[Block]] = {}
        ordre_noeuds: list[str] = []
        for bloc in doc.blocks:
            if not is_citable(bloc):
                continue
            node_id = doc.node_of(bloc.block_id)
            if node_id not in groupes:
                groupes[node_id] = []
                ordre_noeuds.append(node_id)
            groupes[node_id].append(bloc)

        unites: list[UniteContrat] = []
        for node_id in ordre_noeuds:
            node = par_noeud[node_id]
            courants: list[ExtraitContrat] = []
            caracteres = 0

            def fermer() -> None:
                nonlocal courants, caracteres
                if courants:
                    unites.append(UniteContrat(
                        unit_id=courants[0].block_id, node_id=node_id,
                        node_title=node.title, extraits=courants))
                courants, caracteres = [], 0

            for bloc in groupes[node_id]:
                texte = bloc.text[:s.dictionary_flat_max_input_chars]
                if courants and (
                        len(courants) >= s.dictionary_flat_max_blocks_per_request
                        or caracteres + len(texte) > s.dictionary_flat_max_input_chars):
                    fermer()
                courants.append(ExtraitContrat(
                    block_id=bloc.block_id, node_id=node_id, node_title=node.title,
                    text=texte, truncated=len(texte) < len(bloc.text)))
                caracteres += len(texte)
            fermer()
        return unites

    par_id = {n.node_id: n for n in doc.nodes}
    meta = _fiches_du_sommaire(corpus.summaries.get(doc_id, ""))
    out: list[Categorie] = []
    for node in doc.nodes:
        if node.level != 1:
            continue
        fiches = []
        for enfant in node.children:
            n = par_id.get(enfant)
            if n is None:
                continue
            resume, tags = meta.get(enfant, ("", []))
            fiches.append(Fiche(fiche_id=enfant, titre=n.title, resume=resume, tags=tags))
        if fiches:
            out.append(Categorie(node_id=node.node_id, titre=node.title, fiches=fiches))
    return out


def formes_des_blocs(corpus: Corpus, doc_id: str) -> list[str]:
    """`" mot mot … "` de chaque bloc, pour le contrôle « chaîne recopiée d'un bloc » (AD-5, FR29)."""
    return [f" {' '.join(words(b.text_norm or normalize(b.text)))} "
            for b in corpus.documents[doc_id].blocks]


# --- les contrôles que le code applique à ce que le modèle rend ------------

class Controles:
    """Les bornes de `config.py`, appliquées à chaque chaîne. Compte les écarts, ne les corrige pas.

    « Écarté, jamais tronqué » : un terme amputé chercherait autre chose que ce que le modèle a voulu
    dire, et une question coupée en deux serait affichée telle quelle un jour. Le compte des écarts
    est affiché à la fin du run — c'est ce qui rend le contrôle vérifiable, et c'est ce que la ligne
    de `docs/tests-live.md` consigne.
    """

    def __init__(self, settings: Settings, formes_blocs: Iterable[str]) -> None:
        self.s = settings
        self._blocs = list(formes_blocs)
        self.ecarts: dict[str, int] = {}

    def ecart(self, motif: str) -> None:
        """Compte un écart. **Publique** : `agreger` en constate aussi (intent inconnu, doublon).

        Le total affiché en fin de run est ce que `docs/tests-live.md` consigne comme preuve que les
        contrôles ont écarté quelque chose : un rejet qui n'y entre pas rend cette preuve fausse par
        omission.
        """
        self.ecarts[motif] = self.ecarts.get(motif, 0) + 1

    def recopie(self, texte: str) -> bool:
        """La chaîne est-elle un passage du guide recopié ?

        La ligne de partage est `dictionary_term_max_words` : au-dessous, une chaîne qui figure dans
        un bloc est **normale** — « allocations familiales » est précisément le terme qu'on cherche,
        et il est dans le guide. Au-dessus, une chaîne qui figure telle quelle dans un bloc est un
        passage, pas un terme : c'est ce qu'AD-5 et FR29 interdisent de faire sortir de l'ingestion.
        """
        forme = " ".join(words(normalize(texte)))
        if not forme or len(forme.split()) <= self.s.dictionary_term_max_words:
            return False
        cible = f" {forme} "
        return any(cible in bloc for bloc in self._blocs)

    def terme(self, texte: str) -> str:
        """Le terme s'il passe tous les contrôles, `""` sinon (et l'écart est compté)."""
        t = " ".join(texte.split())
        if not t:
            return ""
        if len(t) > self.s.dictionary_term_max_chars:
            self.ecart("terme_trop_long")
            return ""
        if len(words(normalize(t))) > self.s.dictionary_term_max_words:
            self.ecart("terme_trop_de_mots")
            return ""
        if not words(normalize(t)):
            self.ecart("terme_vide_apres_normalisation")
            return ""
        if self.recopie(t):
            self.ecart("terme_recopie_dun_bloc")
            return ""
        return t

    def question(self, texte: str) -> str:
        q = " ".join(texte.split())
        if not q:
            return ""
        if len(q) > self.s.dictionary_question_max_chars:
            self.ecart("question_trop_longue")
            return ""
        if self.recopie(q):
            self.ecart("question_recopiee_dun_bloc")
            return ""
        return q

    def fiche(self, fiche_id: str, connues: set[str]) -> bool:
        if fiche_id in connues:
            return True
        self.ecart("fiche_inconnue")
        return False


# --- construction des requêtes de batch ------------------------------------

# Motif imposé par l'API pour `custom_id` (mesuré : un `cat:lux-guide:cat:administratif` ressort en
# 400 `requests.0.custom_id: String should match pattern`). Les `node_id` du corpus portent des
# deux-points ; ils ne peuvent donc pas servir de `custom_id` tels quels.
CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
CUSTOM_ID_INTENTS = "intents"
_NON_CONFORME = re.compile(r"[^a-zA-Z0-9_-]+")


def custom_id(node_id: str) -> str:
    """`node_id` → `custom_id` **déterministe** et conforme au motif de l'API.

    Deux propriétés, et elles comptent toutes les deux : la partie lisible sert à celui qui regarde
    un lot en cours, et l'empreinte garantit l'injectivité — deux `node_id` distincts peuvent
    donner le même slug (`a:b` et `a-b`), et deux requêtes de même `custom_id` rendraient
    l'agrégation ambiguë, donc les termes d'une catégorie attribuables à une autre.

    Elle est recalculée à l'agrégation plutôt que mémorisée : aucune table à tenir entre la
    soumission et la lecture des résultats, qui peuvent être séparées par une heure d'attente.
    """
    lisible = _NON_CONFORME.sub("-", node_id).strip("-")[:50]
    empreinte = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:8]
    return f"cat-{lisible}-{empreinte}"



def _rendu(nom: str, **valeurs: object) -> str:
    from string import Template

    return Template((PROMPTS_DIR / f"{nom}.md").read_text("utf-8")).substitute(**valeurs)


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    return {"type": "json_schema", "schema": anthropic.transform_schema(model.model_json_schema())}


def _params(system: str, contenu: str, schema: dict[str, Any], settings: Settings,
            *, max_tokens: int | None = None) -> dict[str, Any]:
    """Le corps d'un appel `messages`, tel que le batch le rejoue. `effort` explicite (AD-9)."""
    return {
        "model": MODEL,
        "max_tokens": max_tokens or settings.dictionary_max_output_tokens,
        "system": [{"type": "text", "text": system}],
        "messages": [{"role": "user", "content": contenu}],
        "output_config": {"format": schema, "effort": EFFORT[TIER]},
    }


def requetes(corpus: Corpus, doc_id: str, settings: Settings, *,
             limit: int | None = None) -> list[dict[str, Any]]:
    """Une requête par unité réelle, plus une pour les intentions."""
    document = corpus.documents[doc_id]
    cats = categories(corpus, doc_id, settings)
    if limit is not None:
        cats = cats[:limit]
    perimetre_texte = corpus.perimetres.get(doc_id) or perimetre(corpus.documents[doc_id])
    guide = document.kind == "guide"
    systeme_cat = _rendu("enrich_dictionary" if guide else "enrich_dictionary_contract",
                         max_terms=(settings.dictionary_max_terms_per_fiche if guide
                                    else settings.dictionary_flat_max_terms_per_block),
                         max_variants=settings.dictionary_max_variants_per_term,
                         max_questions=settings.dictionary_max_questions_per_fiche,
                         term_max_chars=settings.dictionary_term_max_chars,
                         term_max_words=settings.dictionary_term_max_words,
                         question_max_chars=settings.dictionary_question_max_chars)
    schema_cat = _schema(SortieCategorie)
    out: list[dict[str, Any]] = []
    for cat in cats:
        if isinstance(cat, Categorie):
            # Branche guide byte-identique : même prompt, même JSON, même max_tokens.
            contenu = json.dumps({"categorie": cat.titre,
                                  "fiches": [f.model_dump() for f in cat.fiches]},
                                 ensure_ascii=False, indent=2, sort_keys=True)
            cle = cat.node_id
            max_tokens = None
        else:
            contenu = json.dumps({
                "document": {"kind": document.kind, "title": document.title},
                "unite": {"node_id": cat.node_id, "node_title": cat.node_title},
                "extraits": [extrait.model_dump() for extrait in cat.extraits],
            }, ensure_ascii=False, indent=2, sort_keys=True)
            cle = cat.unit_id
            max_tokens = settings.dictionary_flat_max_output_tokens
        out.append({"custom_id": custom_id(cle),
                    "params": _params(systeme_cat, contenu, schema_cat, settings,
                                      max_tokens=max_tokens)})
    systeme_int = _rendu("enrich_intents" if guide else "enrich_intents_contract",
                         max_triggers=settings.dictionary_max_intent_triggers,
                         term_max_chars=settings.dictionary_term_max_chars,
                         term_max_words=settings.dictionary_term_max_words,
                         **({"perimetre_guide": perimetre_texte} if guide
                            else {"perimetre_documentaire": perimetre_texte}))
    out.append({"custom_id": CUSTOM_ID_INTENTS,
                "params": _params(systeme_int, "Produis les déclencheurs des trois intentions.",
                                  _schema(SortieIntents), settings)})
    return out


def majorant_eur(reqs: list[dict[str, Any]], settings: Settings, *, batch: bool = True) -> float:
    """Majorant avant appel, avec remise uniquement pour le transport Batch."""
    total = 0.0
    for r in reqs:
        p = r["params"]
        estimation = estimate_cost(p["model"], p["system"], p["messages"], p["max_tokens"], settings,
                                   output_schema=p["output_config"]["format"])
        total += estimation * (BATCH_DISCOUNT if batch else 1.0)
    return round(total, 4)


# --- soumission et attente -------------------------------------------------

class EchecDeBatch(RuntimeError):
    pass


class EchecStandard(RuntimeError):
    pass


# Les `stop_reason` qui disent « le modèle a fini de parler ». Tout le reste — `max_tokens` en
# premier — signale une sortie **coupée**, donc un JSON qu'aucun schéma ne validera.
FINS_NORMALES = frozenset({"end_turn", "stop_sequence", "tool_use"})


def _annuler(client: Any, batch_id: str) -> str:
    """Annule un lot abandonné, et dit ce qu'il en est. Best-effort : l'échec n'écrase pas la cause."""
    try:
        client.messages.batches.cancel(batch_id)
    except Exception as exc:  # noqa: BLE001 — l'annulation est un secours, jamais la raison de l'échec
        return (f"son annulation a échoué ({type(exc).__name__}) : l'annuler à la main "
                f"(`client.messages.batches.cancel(\"{batch_id}\")`), il est encore facturé")
    return "il a été annulé"


def _attr(obj: Any, nom: str, defaut: Any = None) -> Any:
    """Lecture tolérante : le SDK rend des objets, un double de test peut rendre des dicts."""
    if isinstance(obj, dict):
        return obj.get(nom, defaut)
    return getattr(obj, nom, defaut)


def executer(client: Any, reqs: list[dict[str, Any]], settings: Settings,
             *, sortie: Any = sys.stdout, dormir: Any = time.sleep,
             maintenant: Any = time.monotonic) -> tuple[dict[str, Any], float, list[str]]:
    """Soumet le lot, attend `ended`, et rend `({custom_id: texte}, coût réel €, échecs)`.

    L'agrégation se fait **par `custom_id`, jamais par position** : l'API ne promet pas l'ordre, et
    apparier des résultats à des requêtes par leur rang ferait attribuer les termes d'une catégorie
    à une autre — une erreur silencieuse et indétectable dans le fichier produit.

    Une requête `errored` / `expired` / `canceled` n'annule pas les **autres** : ce qui est revenu
    est conservé et l'échec est **affiché** (AD-16 — dit, jamais tu). Ce qui est refusé, plus haut
    dans `main`, c'est d'**écrire** un dictionnaire amputé en le déclarant complet (revue Codex 2.1,
    B2) : la fonction rend les résultats et les échecs, la décision d'écrire se prend là où l'on sait
    ce qui était attendu.

    **Une réponse tronquée est un échec, pas un résultat** (revue coordonnée 2.1). Le lot rend un
    `stop_reason` ; une sortie coupée à `max_tokens` rend du JSON invalide, et sans ce contrôle
    `agreger` s'en tirait par une plainte sur stderr pendant qu'une catégorie entière disparaissait
    du dictionnaire — avec un code de sortie 0. Le cas n'est pas théorique : la catégorie « Questions
    fréquentes » du guide porte 41 fiches. Tout `stop_reason` qui n'est pas une fin normale est donc
    nommé dans `echecs`, comme une requête `errored`, et le coût de l'appel tronqué reste compté :
    il a été facturé.
    """
    lot = client.messages.batches.create(requests=reqs)
    batch_id = _attr(lot, "id")
    print(f"lot soumis : {batch_id} ({len(reqs)} requête(s))", file=sortie)
    debut = maintenant()
    while True:
        etat = client.messages.batches.retrieve(batch_id)
        if _attr(etat, "processing_status") == "ended":
            break
        if maintenant() - debut > settings.dictionary_batch_timeout_s:
            # Un lot abandonné continue de tourner **et d'être facturé** : partir sans l'annuler
            # laissait la dépense courir après une commande qui vient de déclarer forfait.
            # L'annulation est best-effort — si elle échoue, la seule chose utile est de dire quel
            # lot reste en vol, avec de quoi le rattraper à la main.
            annulation = _annuler(client, batch_id)
            raise EchecDeBatch(
                f"le lot {batch_id} n'a pas terminé en {settings.dictionary_batch_timeout_s:.0f} s "
                f"(dernier état : {_attr(etat, 'processing_status')!r}) — rien n'a été écrit ; "
                f"{annulation}. Ses résultats restent récupérables tant que le lot n'a pas expiré : "
                f"`client.messages.batches.results(\"{batch_id}\")`.")
        dormir(settings.dictionary_batch_poll_s)

    textes: dict[str, Any] = {}
    echecs: list[str] = []
    cout = 0.0
    for entree in client.messages.batches.results(batch_id):
        cle = _attr(entree, "custom_id", "?")  # jamais `custom_id` : le nom est celui de la fonction
        resultat = _attr(entree, "result")
        kind = _attr(resultat, "type")
        if kind != "succeeded":
            echecs.append(f"{cle} : {kind}")
            continue
        message = _attr(resultat, "message")
        usage = _attr(message, "usage")
        if usage is not None:
            cout += cost_from_usage(MODEL, usage, settings.usd_eur, batch=True).cost_eur
        arret = _attr(message, "stop_reason")
        if arret is not None and arret not in FINS_NORMALES:
            echecs.append(f"{cle} : réponse interrompue (stop_reason={arret!r}) — sortie "
                          f"incomplète, la catégorie est écartée plutôt que tronquée")
            continue
        textes[cle] = "".join(
            _attr(bloc, "text", "") for bloc in (_attr(message, "content") or [])
            if _attr(bloc, "type") == "text")
    return textes, round(cout, 4), echecs


def executer_standard(client: Any, reqs: list[dict[str, Any]], settings: Settings,
                      *, sortie: Any = sys.stdout) -> tuple[dict[str, Any], float, list[str]]:
    """Exécute les requêtes depuis zéro via Messages standard, séquentiellement et sans retry.

    Chaque coût vient de l'usage de la réponse au tarif standard. Une exception terminale arrête la
    campagne ; une réponse sans usage ou tronquée est rendue comme échec, sans texte exploitable.
    L'appelant n'écrit qu'après couverture complète de tous les `custom_id`, donc les résultats déjà
    acquis ne peuvent jamais produire un dictionnaire partiel.
    """
    textes: dict[str, Any] = {}
    echecs: list[str] = []
    cout = 0.0
    for requete in reqs:
        cle = requete["custom_id"]
        try:
            message = client.messages.create(**requete["params"])
        except Exception as exc:  # noqa: BLE001 — frontière SDK, transformée en refus atomique
            raise EchecStandard(
                f"{cle} : appel Messages standard échoué ({type(exc).__name__}) après "
                f"{len(textes)} réponse(s), sans retry ; coût réel acquis {cout:.4f} € — "
                "rien n'a été écrit") from exc
        usage = _attr(message, "usage")
        if usage is None:
            echecs.append(f"{cle} : réponse standard sans usage facturable")
            break
        cout += cost_from_usage(MODEL, usage, settings.usd_eur, batch=False).cost_eur
        arret = _attr(message, "stop_reason")
        if arret is not None and arret not in FINS_NORMALES:
            echecs.append(f"{cle} : réponse interrompue (stop_reason={arret!r}) — sortie "
                          "incomplète, l'unité est écartée plutôt que tronquée")
            break
        textes[cle] = "".join(
            _attr(bloc, "text", "") for bloc in (_attr(message, "content") or [])
            if _attr(bloc, "type") == "text")
    print(f"Messages standard : {len(reqs)} appel(s), coût réel {cout:.4f} €", file=sortie)
    return textes, round(cout, 4), echecs


# --- agrégation ------------------------------------------------------------

def _valider(texte: str, model: type[BaseModel]) -> BaseModel | None:
    try:
        return model.model_validate_json(texte)
    except (ValidationError, ValueError):
        return None


def agreger(textes: dict[str, Any], corpus: Corpus, doc_id: str, controles: Controles,
            settings: Settings) -> tuple[dict[str, list[str]], dict[str, list[str]],
                                         dict[str, list[str]], list[str], set[str]]:
    """`(corpus_termes, intents, candidate_questions, plaintes, traites)` — ce qui passe les contrôles.

    Rien n'est rendu tel quel : chaque chaîne repasse par `Controles`, chaque `fiche_id` est confronté
    aux nœuds réels du document, et les bornes de nombre (`max_variants`, `max_terms_per_fiche`,
    `max_questions_per_fiche`, `max_intent_triggers`) sont appliquées **ici**, après filtrage — sans
    quoi une entrée écartée aurait consommé un rang.

    **`traites` est ce qui rend l'incomplétude détectable** (revue Codex 2.1, B2) : les `custom_id`
    dont la requête a réellement produit quelque chose. Une catégorie y entre quand elle a livré **au
    moins un canonique** — c'est le mot de l'AC (« au moins un canonique par catégorie du guide ») —
    et non quand son JSON s'est laissé lire : une catégorie dont tous les termes sont écartés par les
    contrôles ne donne aucun vocabulaire aux fiches qu'elle couvre, et le fichier ne peut pas
    prétendre décrire le corpus. La requête des intentions, elle, y entre dès que sa sortie est
    conforme : `intents` n'est lu par personne (`target_story: 2.5`), un déclencheur écarté ne
    change rien à ce que le serveur trouve ou refuse.
    """
    cats = {
        custom_id(c.node_id if isinstance(c, Categorie) else c.unit_id): c
        for c in categories(corpus, doc_id, settings)
    }
    plaintes: list[str] = []
    termes: dict[str, list[str]] = {}
    questions: dict[str, list[str]] = {}
    intents: dict[str, list[str]] = {}
    par_fiche: dict[str, int] = {}
    traites: set[str] = set()

    for cle, texte in sorted(textes.items()):
        if cle == CUSTOM_ID_INTENTS:
            sortie = _valider(texte, SortieIntents)
            if sortie is None:
                plaintes.append("intents : sortie non conforme au schéma, ignorée")
                continue
            for entree in sortie.intents:
                if entree.intent not in INTENTS_DU_DICTIONNAIRE:
                    controles.ecart("intent_inconnu")
                    continue
                retenus = intents.setdefault(entree.intent, [])
                for brut in entree.declencheurs:
                    d = controles.terme(brut)
                    if d and d not in retenus and len(retenus) < settings.dictionary_max_intent_triggers:
                        retenus.append(d)
            traites.add(cle)
            continue

        cat = cats.get(cle)
        if cat is None:
            plaintes.append(f"{cle} : catégorie inconnue, ignorée")
            continue
        sortie = _valider(texte, SortieCategorie)
        if sortie is None:
            plaintes.append(f"{cle} : sortie non conforme au schéma, ignorée")
            continue
        connues = ({f.fiche_id for f in cat.fiches} if isinstance(cat, Categorie)
                   else {extrait.block_id for extrait in cat.extraits})
        max_termes = (settings.dictionary_max_terms_per_fiche if isinstance(cat, Categorie)
                      else settings.dictionary_flat_max_terms_per_block)
        for entree in sortie.termes:
            if not controles.fiche(entree.fiche_id, connues):
                continue
            if par_fiche.get(entree.fiche_id, 0) >= max_termes:
                continue
            canonique = controles.terme(entree.canonique)
            if not canonique:
                continue  # l'écart est déjà compté par `Controles.terme`
            if canonique in termes:
                # Deux catégories qui proposent le même canonique : la première le garde (l'ordre
                # des `custom_id` est trié, donc déterministe). Le compter est ce qui rend le total
                # honnête — sans quoi un modèle qui rendrait dix fois « commune » ferait un run
                # « aucun écart » sur neuf rejets.
                controles.ecart("canonique_duplique")
                continue
            variantes: list[str] = []
            for brut in entree.variantes:
                v = controles.terme(brut)
                if v and v != canonique and v not in variantes \
                        and len(variantes) < settings.dictionary_max_variants_per_term:
                    variantes.append(v)
            termes[canonique] = variantes
            traites.add(cle)  # la catégorie a livré au moins un canonique
            par_fiche[entree.fiche_id] = par_fiche.get(entree.fiche_id, 0) + 1
        for entree in sortie.questions:
            if not isinstance(cat, Categorie):
                if entree.question.strip() or entree.fiche_id.strip():
                    controles.ecart("question_non_supportee_pour_contrat")
                continue
            if not controles.fiche(entree.fiche_id, connues):
                continue
            q = controles.question(entree.question)
            if not q:
                continue  # jamais une entrée vide : une fiche sans question n'a pas de clé du tout
            retenues = questions.setdefault(entree.fiche_id, [])
            if q not in retenues and len(retenues) < settings.dictionary_max_questions_per_fiche:
                retenues.append(q)
    return termes, intents, questions, plaintes, traites


# --- écriture --------------------------------------------------------------

def _serialiser(fichier: DictionaryFile) -> str:
    """Même forme que tout le JSON du dépôt (`server/ingest/artifacts.py`) : `indent=2`, UTF-8, `\\n` final."""
    return json.dumps(fichier.model_dump(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _trier(fichier: DictionaryFile) -> DictionaryFile:
    """Ordre stable : relancer l'ingestion ne doit pas produire un diff d'ordre (AD-7)."""
    return fichier.model_copy(update={
        "corpus": {k: sorted(v) for k, v in sorted(fichier.corpus.items())},
        "intents": {k: sorted(v) for k, v in sorted(fichier.intents.items())},
        "candidate_questions": {k: sorted(v) for k, v in sorted(fichier.candidate_questions.items())},
    })


def valider_a_la_main(chemin: Path, corpus: Corpus, nom: str, doc_id: str,
                      *, sortie: Any = sys.stdout) -> int:
    """`--valider "Nom"` : trois champs, et **rien d'autre** (AD-5).

    C'est la seule chose qui arme le refus « zéro hit », et c'est un acte humain : le run refuse si le
    fichier ne décrit plus le corpus livré (code 5), parce que signer un dictionnaire périmé
    armerait un refus sur un vocabulaire qui ne décrit pas ce qui est servi.

    **Rien d'invalide n'atteint le disque** (revue coordonnée 2.1). `model_copy(update=…)` ne rejoue
    **aucun** validateur pydantic : `--valider ""` écrivait `validated: true, validated_by: ""`, que
    `DictionaryFile` interdit et que `load_dictionary` refusait ensuite en bloc — le dépôt se
    retrouvait avec un dictionnaire signé, illisible, et un serveur qui n'élargissait plus rien.
    Le nom vide est donc refusé en amont (un « validé par personne » est la contradiction que le
    schéma nomme), et la copie signée est **revalidée** avant l'écriture atomique : le schéma est la
    seule autorité, y compris pour le code qui l'écrit.
    """
    if not chemin.is_file():
        print(f"{chemin} absent : lancer l'enrichissement avant de valider", file=sys.stderr)
        return 5
    if not nom.strip():
        print("--valider exige un nom : un dictionnaire « validé par personne » n'arme aucun refus "
              "(AD-5) — rien n'a été écrit", file=sys.stderr)
        return 5

    class Refus(Exception):
        """Le refus, porté hors de la transaction sans qu'aucune cible ait bougé."""

        def __init__(self, message: str) -> None:
            super().__init__(message)

    signe_publie: DictionaryFile | None = None

    def fabriquer(lecture: LectureDuLot) -> list[tuple[Path, str | None]]:
        """Lire, contrôler et signer **sous le verrou** (story 4.5, N3).

        `--valider` était un read-modify-write à cheval sur le verrou : la lecture précédait
        l'écriture atomique, et un enrichissement publié entre les deux était écrasé par une
        signature portant l'ancien contenu. Les trois gestes vivent maintenant dans la même section
        critique, et tout refus sort **avant** que la moindre cible ait bougé.
        """
        nonlocal signe_publie
        octets = lecture.octets(chemin)
        if octets is None:
            raise Refus(f"{chemin} absent : lancer l'enrichissement avant de valider")
        try:
            fichier = DictionaryFile.model_validate_json(octets)
        except (OSError, ValueError) as exc:
            raise Refus(f"{chemin} illisible ou non conforme, rien n'a été écrit : "
                        f"{type(exc).__name__}") from exc
        attendu = {declare: corpus.manifest[declare].source_hash for declare in corpus.served
                   if declare in fichier.corpus_source_hashes}
        # `doc_id` — le document que le pipeline appliquera — doit être **nommé** (revue Codex 2.1,
        # B3) : sans cette ligne, signer un dictionnaire ne décrivant que le contrat AXA sortait en
        # code 0 avec « le refus « zéro hit » est armé », alors que le serveur le refuse.
        if doc_id not in fichier.corpus_source_hashes or attendu != fichier.corpus_source_hashes:
            raise Refus(f"{chemin} ne décrit pas le corpus servi pour {doc_id!r} "
                        f"({sorted(fichier.corpus_source_hashes)}) : rien n'a été écrit — relancer "
                        "l'enrichissement avant de valider")
        try:
            signe = DictionaryFile.model_validate(fichier.model_dump() | {
                "validated": True, "validated_by": nom.strip(),
                "validated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})
        except ValueError as exc:
            raise Refus("la signature ne produirait pas un dictionnaire conforme, rien n'a été "
                        f"écrit : {type(exc).__name__}") from exc
        signe_publie = signe
        return [(chemin, _serialiser(signe))]

    try:
        republier([chemin], fabriquer)
    except Refus as exc:
        print(str(exc), file=sys.stderr)
        return 5
    assert signe_publie is not None
    print(f"{chemin} : validated=true par {signe_publie.validated_by} le "
          f"{signe_publie.validated_at} — le refus « zéro hit » d'AD-5 est armé", file=sortie)
    return 0


# --- CLI -------------------------------------------------------------------

def _client(api_key: str) -> Any:
    return anthropic.Anthropic(api_key=api_key, max_retries=0)


def main(argv: list[str] | None = None, *, client: Any = None, settings: Settings | None = None,
         sortie: Any = None, dormir: Any = time.sleep, maintenant: Any = time.monotonic) -> int:
    sortie = sys.stdout if sortie is None else sortie
    parser = argparse.ArgumentParser(prog="python -m server.ingest.enrich_dictionary",
                                     description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=REPO_ROOT / "data", type=Path)
    parser.add_argument("--doc-id", default=None, help="document enrichi (défaut : guide_doc_id)")
    parser.add_argument("--max-cost", type=float, default=None,
                        help="surcharge dictionary_max_cost_eur pour ce run")
    parser.add_argument("--transport", choices=("batch", "standard"), default="batch",
                        help="Batch historique, ou Messages standard depuis zéro sans Batch ni retry")
    parser.add_argument("--dry-run", action="store_true",
                        help="affiche le plan et le majorant, ne soumet rien et n'écrit rien")
    parser.add_argument("--limit", type=int, default=None,
                        help="n'enrichit que les N premières catégories — le fichier produit est "
                             "alors **inerte** (corpus_source_hashes vide) : ni variantes, ni "
                             "court-circuit, et --valider le refuse. Pour une mise au point, jamais "
                             "pour un dictionnaire servi.")
    parser.add_argument("--valider", metavar="NOM", default=None,
                        help="signe le dictionnaire existant (la seule chose qui arme le refus AD-5)")
    args = parser.parse_args(argv)

    # Un argument explicite invalide est refusé avant de charger `.env` et ses secrets. Le défaut,
    # lui, vient nécessairement des Settings et est validé juste après leur construction.
    if (args.doc_id is not None
            and (len(args.doc_id) > DOC_ID_MAX or not DOC_ID_RE.fullmatch(args.doc_id))):
        print(f"doc_id invalide (slug [a-z0-9-]+ de {DOC_ID_MAX} caractères maximum attendu) : "
              f"{args.doc_id!r}", file=sys.stderr)
        return 2
    settings = settings or get_settings()
    doc_id = args.doc_id or settings.guide_doc_id
    if len(doc_id) > DOC_ID_MAX or not DOC_ID_RE.fullmatch(doc_id):
        print(f"doc_id invalide (slug [a-z0-9-]+ de {DOC_ID_MAX} caractères maximum attendu) : "
              f"{doc_id!r}", file=sys.stderr)
        return 2
    data_dir = Path(args.data)
    corpus = load_corpus(data_dir, allow_ungated=True,
                         perimetre_max_chars=settings.perimetre_max_chars)
    if doc_id not in corpus.documents:
        raison = corpus.quarantine.get(doc_id, "absent du manifest")
        print(f"document {doc_id!r} non servi ({raison}) : rien n'a été écrit", file=sys.stderr)
        return 2
    chemin = (data_dir / DICTIONARY_FILE
              if corpus.documents[doc_id].kind == "guide"
              else data_dir / doc_id / DICTIONARY_FILE)
    # **Un entrypoint de production exige une racine installée** (story 4.5, N3), et le dit avant
    # tout appel payant comme avant toute signature. Le lot n'a qu'une cible — le cas où le refus
    # « lot mixte » était structurellement inatteignable, et où une cible couverte dont le lien
    # aurait été cassé était réécrite en fichier ordinaire, silencieusement.
    try:
        exiger_espace_installe([chemin])
    except Exception as exc:  # noqa: BLE001 — une disposition absente n'est pas une trace Python
        print(f"refus, rien n'a été écrit : {exc}", file=sys.stderr)
        return 2

    if args.valider is not None:
        return valider_a_la_main(chemin, corpus, args.valider.strip(), doc_id, sortie=sortie)

    reqs = requetes(corpus, doc_id, settings, limit=args.limit)
    plafond = settings.dictionary_max_cost_eur if args.max_cost is None else args.max_cost
    if not math.isfinite(plafond) or plafond <= 0:
        print(f"plafond {plafond!r} invalide : une valeur finie strictement positive est exigée ; "
              "aucun appel n'est soumis, rien n'a été écrit", file=sys.stderr)
        return 3
    batch = args.transport == "batch"
    majorant = majorant_eur(reqs, settings, batch=batch)
    for r in reqs:
        print(f"  {r['custom_id']}", file=sortie)
    transport = (f"Batch (remise {BATCH_DISCOUNT})" if batch
                 else "Messages standard (sans remise Batch, sans retry)")
    print(f"{len(reqs)} requête(s), tier {TIER} ({MODEL}), transport {transport}, "
          f"majorant {majorant:.4f} € contre un plafond de {plafond:.4f} €", file=sortie)
    if majorant > plafond:
        print(f"majorant {majorant:.4f} € > plafond {plafond:.4f} € : aucun appel n'est soumis, "
              "rien n'a été écrit", file=sys.stderr)
        return 3
    if args.dry_run:
        print("--dry-run : rien n'a été soumis ni écrit", file=sortie)
        return 0

    if client is None:
        # `config.cle_absente` : la variable **posée**, vide comprise, fait foi. Sans elle,
        # `ANTHROPIC_API_KEY= uv run python -m server.ingest.enrich_dictionary` retombait sur le
        # `.env` du poste et soumettait un lot pour de bon — mesuré, story 2.1.
        if cle_absente(settings):
            print("ANTHROPIC_API_KEY vide ou absente (environnement, puis .env) : "
                  "rien n'a été soumis ni écrit", file=sys.stderr)
            return 2
        client = _client(settings.anthropic_api_key)

    try:
        if batch:
            textes, cout, echecs = executer(client, reqs, settings, sortie=sortie, dormir=dormir,
                                            maintenant=maintenant)
        else:
            textes, cout, echecs = executer_standard(client, reqs, settings, sortie=sortie)
    except (EchecDeBatch, EchecStandard) as exc:
        print(str(exc), file=sys.stderr)
        return 4
    for echec in echecs:
        print(f"requête en échec : {echec}", file=sys.stderr)
    if not textes:
        print("aucun résultat exploitable : rien n'a été écrit", file=sys.stderr)
        return 4

    controles = Controles(settings, formes_des_blocs(corpus, doc_id))
    termes, intents, questions, plaintes, traites = agreger(textes, corpus, doc_id, controles,
                                                            settings)
    for plainte in plaintes:
        print(f"avertissement : {plainte}", file=sys.stderr)
    if not termes:
        print("aucun terme n'a passé les contrôles : rien n'a été écrit", file=sys.stderr)
        return 4

    # **Un lot incomplet ne produit pas un dictionnaire complet** (revue Codex 2.1, B2). Le seul
    # garde-fou était `--limit` : une requête `errored`, `expired`, tronquée à `max_tokens`, hors
    # schéma, ou dont tous les termes tombaient sous les contrôles, disparaissait avec un simple
    # message sur stderr — et le fichier recevait quand même l'empreinte **entière** du corpus. Il
    # était donc signable, et armait le refus « zéro hit » sur les catégories absentes : un faux
    # refus par construction, exactement ce que l'écriture inerte d'un run `--limit` évite.
    # Rien n'est écrit dans ce cas : le dictionnaire déjà commité — éventuellement signé — reste en
    # place, et relancer est la seule suite. Le code 4 est celui des autres échecs de lot.
    manquants = sorted({r["custom_id"] for r in reqs} - traites)
    if manquants:
        print(f"lot incomplet : {len(manquants)} requête(s) sur {len(reqs)} n'ont rien donné "
              f"({', '.join(manquants)}) — un dictionnaire amputé de ces catégories armerait un "
              "faux refus sur les fiches qu'elles couvrent. Rien n'a été écrit : relancer "
              "`python -m server.ingest.enrich_dictionary`.", file=sys.stderr)
        return 4

    # **Un run partiel ne se déclare pas complet** (revue coordonnée 2.1). `corpus_source_hashes`
    # est l'affirmation « ce fichier décrit ce corpus » : l'écrire en entier après un `--limit`
    # produisait un dictionnaire de deux catégories sur dix qui passait `_corpus_ok`, passait
    # `--valider`, et armait le refus « zéro hit » sur les huit autres — un faux refus par
    # construction. Vide, le fichier est inerte : `_corpus_ok` le refuse déjà (« ne dit pas quel
    # corpus il décrit »), donc ni variantes ni court-circuit, et `--valider` sort en 5.
    partiel = args.limit is not None
    fichier = _trier(DictionaryFile(
        schema_version=SCHEMA_VERSION,
        corpus_source_hashes={} if partiel else {doc_id: corpus.manifest[doc_id].source_hash},
        corpus=termes, intents=intents, candidate_questions=questions,
        # **Jamais** `validated: true` ici, sous aucune forme : AD-5 réserve la signature à un
        # humain, et c'est elle — et elle seule — qui arme le refus « zéro hit ».
        validated=False, validated_by=None, validated_at=None))
    write_atomic(chemin, _serialiser(fichier))

    variantes = sum(len(v) for v in fichier.corpus.values())
    print(f"{chemin} écrit : {len(fichier.corpus)} canonique(s), {variantes} variante(s), "
          f"{sum(len(q) for q in fichier.candidate_questions.values())} question(s) candidate(s), "
          f"{sum(len(d) for d in fichier.intents.values())} déclencheur(s) — validated=false",
          file=sortie)
    print(f"coût réel : {cout:.4f} € (majorant {majorant:.4f} €)", file=sortie)
    if partiel:
        print(f"--limit {args.limit} : run **partiel**, corpus_source_hashes laissé vide — ce "
              "dictionnaire est inerte (ni variantes, ni court-circuit) et `--valider` le refusera. "
              "Relancer sans --limit pour un fichier servi.", file=sortie)
    if controles.ecarts:
        print("écarts rejetés par les contrôles : "
              + ", ".join(f"{k}={v}" for k, v in sorted(controles.ecarts.items())), file=sortie)
    else:
        print("écarts rejetés par les contrôles : aucun", file=sortie)
    print("validation humaine due : `--valider \"Nom\"` "
          "(sans elle, le refus « zéro hit » d'AD-5 dort)", file=sortie)
    return 0



if __name__ == "__main__":
    sys.exit(main())
