"""AD-3 / AD-4 — *vérifier* : le code contrôle chaque citation, le modèle ne juge que la pertinence.

Deux moitiés, dans cet ordre, et jamais l'inverse :

1. **Code pur.** Pour chaque quote de chaque claim : le `block_id` existe dans le corpus, le bloc
   n'est pas un `heading` (AD-3 : « un titre n'est pas citable seul »), la quote normalisée fait au
   moins `quote_min_chars` **ou** `quote_min_ratio` du bloc, elle est **incluse** dans le
   `text_norm` du bloc **relu depuis le corpus** (jamais le texte du draft), et son occurrence n'est
   pas ambiguë (le même passage dans un second bloc du **document** attribuerait la phrase au mauvais
   endroit). Les offsets de l'occurrence et les `line_ids` traversés sont conservés pour le
   surlignage. Une claim est `retrouvee` **ssi toutes** ses quotes le sont.
2. **Un seul appel `micro` groupé** (AD-4), uniquement sur les claims retrouvées, borné par
   `verifier_max_claims` : « ces passages soutiennent-ils l'affirmation **et** répond-elle à la
   question ? ». Le modèle ne rend qu'un booléen par `claim_id` — aucun texte libre, aucun calcul :
   `found` et `complete` sont calculés ici, par le code, et le motif de rejet est composé ici aussi.
   Le **même** appel rend deux autres faits que le code ne peut pas établir seul : quelles
   affirmations couvrent chacune des facettes de `ParsedQuestion` (pour `complete` — le découpage,
   lui, vient de *comprendre* et n'est pas rediscuté ici, revue Codex 1.5 tour 3 B3), et, pour
   **chaque phrase réellement affichée**, si elle n'avance rien au-delà des passages joints (tour 2,
   B1). Une phrase `limite` — « le guide ne dit rien de X » — n'est affichable par aucune de ces
   preuves : elle ne rejoint que `unknown[]` (tour 3, B1).

**« Partiel » dit toujours ce qui manque (story 2.3).** `complete=False` naît de six causes — facettes
non couvertes, découpage non établi, retrieval tronqué, renvoi non résolu, phrases écartées, limite
déclarée par le modèle — et une seule d'entre elles écrivait quelque chose dans `unknown[]`. Chacune
est désormais constatée par le **code** sous forme de `Lacune(kind, n)` (`_lacunes`, AD-16 / NFR2)
et déposée dans `Verification.lacunes` — **distinct** d'`unknown`, qui reste ce que le modèle a
déclaré. *Restituer* projette ensuite ces causes dans la langue de la réponse et les fond dans
l'unique liste affichée (revue coordonnée 2.3, A3). `complete` se réduit alors à « trouvé, et rien
qui manque » — un seul invariant à tenir pour le domaine (`Answer._found_coherence`), une seule
liste à lire pour l'utilisateur.

Le texte soumis au modèle est celui du corpus, pas celui du draft : c'est ce qui empêche une citation
« écho » d'être jugée pertinente sur sa propre invention. Question et passages sont délimités par
`untrusted()` (AD-15).

Rien de ce qui vient du modèle ne traverse le motif ni la trace en clair (leçon de la revue 1.4, B7) :
un `block_id` inconnu du corpus devient `<bloc inconnu>`, un `claim_id` qui ne ressemble pas à un
identifiant devient `claim n° i`. Un `block_id` **connu** est notre propre chaîne : il part tel quel,
c'est ce qui rend la relance actionnable (AD-3 : « quote introuvable dans `block_id` X »).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from pydantic import BaseModel

from server.app.config import Settings
from server.app.corpus.text import normalize, normalize_spans
from server.app.domain.answer import (
    AnswerDraft,
    AnswerSegment,
    Claim,
    ClaimStatus,
    Lacune,
    Quote,
    RejectedClaim,
    Verification,
    VerifiedClaim,
    VerifiedQuote,
)
from server.app.domain.document import Block
from server.app.domain.errors import PipelineError
from server.app.domain.question import Faits, ParsedQuestion
from server.app.domain.retrieval import RetrievalResult
from server.app.domain.trace import CheckResult, StepTrace
from server.app.domain.verdict import (
    KINDS_DECISIONNELS,
    ChampsApplicabilite,
    ClaimJugee,
    ClauseCitee,
    MissingPackage,
    Verdict,
    applicable_de_claim,
    decider,
)
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import STEP_TIERS
from server.app.llm.prompting import load_prompt, render_prompt, untrusted

# Un `claim_id` produit par le modèle n'entre dans un motif que s'il ressemble à ce que le prompt
# demande (`c1`, `c2`, …) : court, sans espace ni balise. Tout le reste est nommé par sa position.
_CLAIM_ID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")

BLOC_INCONNU = "<bloc inconnu>"


class VerdictPertinence(BaseModel):
    claim_id: str
    pertinente: bool


class FacettePertinence(BaseModel):
    """La couverture d'**une** facette de `ParsedQuestion` : son rang, et les affirmations qui y répondent.

    `facette` est la position de la facette dans `ParsedQuestion.facettes`, telle qu'elle a été
    envoyée : un entier de **notre** code, pas une chaîne du modèle. Le contrôle ne **découpe** plus
    la question — le découpage lui est donné, arrêté par *comprendre* avant toute rédaction (revue
    Codex 1.5, tour 3, B3). Il ne rend donc plus aucun texte libre : une facette qu'il oublie reste
    une facette de la question, non couverte, et `complete` reste `False`.
    """

    facette: int
    claim_ids: list[str] = []


class VerdictSegment(BaseModel):
    """Une phrase de l'ébauche telle qu'elle serait **affichée**, et le seul jugement qu'on lui demande.

    `segment` est la position du segment dans `AnswerDraft.segments`, telle qu'elle a été envoyée :
    un entier de **notre** code, pas une chaîne du modèle. `soutenu` répond à une question binaire —
    ce texte n'avance-t-il rien qui ne soit pas dans les passages joints ?
    """

    segment: int
    soutenu: bool


class SortieVerifier(BaseModel):
    """Sortie de l'appel `micro` : un booléen par claim, un par phrase affichée, le découpage (AD-4).

    Aucun champ de justification : le modèle ne peut pas glisser de motif dans la trace, et il ne peut
    pas non plus « expliquer » un verdict que le code ne lui a pas demandé. `found` et `complete`
    restent calculés par le code — le modèle ne rend que les faits sur lesquels le code les calcule,
    exactement comme pour `pertinente`.
    """

    verdicts: list[VerdictPertinence]
    facettes: list[FacettePertinence] = []
    segments: list[VerdictSegment] = []


# Les mots qui nomment la **catégorie** d'une qualité, jamais la qualité elle-même : les retrouver
# dans les faits ne corrobore rien (revue Codex 1.8, B3, tour 2). « caractère soudain de l'événement »
# n'est établi que par « soudain », pas par « caractère » ni par « événement ».
MOTS_DE_STRUCTURE: frozenset[str] = frozenset({
    "caractere", "nature", "qualite", "existence", "evenement", "action", "contact", "moment",
    "presence", "situation", "element", "assure", "clause", "garantie", "sinistre"})


def _mots_significatifs(libelle: str, *, min_chars: int) -> set[str]:
    """Les mots d'un libellé qui portent la qualité — normalisés, assez longs, non structurels."""
    return {m for m in re.findall(r"[a-z0-9]+", normalize(libelle))
            if len(m) >= min_chars and m not in MOTS_DE_STRUCTURE}


def _dit_la_qualite(qualite: str, fait_cite: str, *, min_chars: int) -> bool:
    """Le fragment cité dit-il la qualité ? **Tous** ses mots porteurs, recoupés par préfixe.

    « subit » et « subite », « soudain » et « soudaine » sont le même mot pour ce qui nous occupe, et
    le projet n'embarque pas de lemmatiseur pour si peu : deux mots d'au moins `min_chars` caractères
    dont l'un préfixe l'autre se recoupent.

    Revue Codex 1.8 (B3, tour 3) : **un** mot partagé ne suffisait pas. « La chaleur a agi lentement »
    est un fait authentique qui dit le contraire de « action subite de la chaleur », et le seul mot
    « chaleur » le faisait tenir pour établie — le qualificatif déterminant (*subite*) n'était jamais
    exigé du fragment. Le fragment doit donc employer **chacun** des mots porteurs de la qualité, le
    nom générique comme le qualificatif. Une qualité dont aucun mot n'est porteur (« nature de
    l'événement ») n'est établie par rien : elle ne dit pas ce qu'elle exige.
    """
    mots = _mots_significatifs(qualite, min_chars=min_chars)
    if not mots:
        return False
    cites = _mots_significatifs(fait_cite, min_chars=min_chars)
    return all(any(a.startswith(b) or b.startswith(a) for b in cites) for a in mots)


# Revue Codex 1.8 (B3, tour 3). Les qualificatifs par lesquels une clause d'assurance **subordonne**
# son effet à une qualité de l'événement, du bien ou de l'assuré. Lexique volontairement court et
# fermé : il ne sert pas à comprendre la clause, seulement à savoir que le modèle avait quelque chose
# à énumérer. Un mot du texte qui commence par l'un d'eux le porte (« soudaine », « subitement »,
# « intentionnellement »). Ce qui n'y figure pas ne déclenche rien — le contrôle n'ajoute jamais une
# qualité que le texte de la clause n'écrit pas.
QUALIFICATIFS: frozenset[str] = frozenset({
    "soudain", "subit", "brusque", "instantane", "accidentel", "fortuit", "imprevisible", "imprevu",
    "involontaire", "intentionnel", "immediat", "direct", "permanent", "exceptionnel", "violent",
    "anormal", "malveillant", "effraction"})


def _mots_qualifiants(texte: str) -> dict[str, str]:
    """Les qualificatifs du lexique employés par un texte : `racine du lexique → mot du texte`.

    Le mot est rendu dans son **orthographe d'origine** (première occurrence) : il finit dans une
    question posée au client, où « immédiat » se lit mieux que sa forme normalisée.
    """
    trouves: dict[str, str] = {}
    for mot in re.findall(r"[^\W\d_]+", texte, flags=re.UNICODE):
        norme = normalize(mot)
        for racine in QUALIFICATIFS:
            if norme.startswith(racine):
                trouves.setdefault(racine, mot)
    return trouves


def _qualites_de_la_clause(clauses: list[ClauseCitee], *, nommees: str, place: int) -> list[str]:
    """Les qualités que **le texte de la clause** exige et que le modèle n'a pas nommées (B3, tour 3).

    Le contrôle des deux listes ne valait que ce que valait la première : rien n'obligeait le modèle à
    énumérer. Rendre `"qualites_exigees": []` sur une clause qui écrit « par un événement soudain,
    résultant de l'action subite de la chaleur » se lisait « aucune qualité exigée » — et la clause
    passait `oui`, donc `couvert`, sans qu'aucun fait n'ait établi quoi que ce soit. Le texte de la
    clause est la source indépendante qui manquait : ses qualificatifs sont relus dans le corpus
    (`ClauseCitee.qualificatifs`), et ceux que le modèle n'a nommés nulle part — ni dans les qualités
    exigées, ni dans les établies, ni dans le fait manquant — deviennent des qualités **non établies**
    composées par le code. La clause vaut alors `humain` et chaque qualité part en question au client,
    ce que « forcer `humain` et produire une question bornée » demande.

    Ne s'applique qu'aux clauses dont le modèle dit le fait requis **présent** : c'est le seul chemin
    vers `oui`, et une clause qui ne vise pas le cas n'exige rien de lui (le prompt le dit déjà : « si
    le périmètre n'est pas bon, les deux listes sont vides »).
    """
    attendus: dict[str, str] = {}
    for clause in clauses:
        for racine, mot in _mots_qualifiants(" ".join(clause.qualificatifs)).items():
            attendus.setdefault(racine, mot)
    deja = set(_mots_qualifiants(nommees))
    return [f"caractère « {mot} » exigé par la clause citée"
            for racine, mot in attendus.items() if racine not in deja][:max(place, 0)]


class QualiteEtablie(BaseModel):
    """Une qualité que la clause exige **et** le fragment des faits déclarés qui l'établit.

    Revue Codex 1.8 (B3), tour 2 : « empêcher qu'une simple auto-déclaration du modèle dans les deux
    listes tienne lieu de corroboration par les faits ». Le fragment est repris **mot pour mot** des
    faits soumis et relu par le code (`normalize`) : ce que le modèle ne peut pas montrer dans les
    faits, il ne l'a pas établi.
    """

    qualite: str
    fait_cite: str


class ChampsApplicabiliteRendus(BaseModel):
    """Les quatre valeurs typées d'AD-4 pour **une** affirmation qui cite une clause décisionnelle.

    Aucun champ de décision : ni `applicable`, ni `couvert`, ni `verdict`. AD-6 est explicite — « le
    modèle n'effectue aucun calcul : il extrait des valeurs typées, le code compare ». Le seul texte
    libre est `fait_manquant`, borné par `fait_manquant_max_chars` et jamais versé dans
    `Answer.texte` (D8 de la spec 1.8).
    """

    claim_id: str
    fait_requis_present: bool
    option_requise: bool
    cp_requise: bool
    fait_manquant: str | None
    # Revue Codex 1.8 (B3). Deux **listes** plutôt qu'un second booléen : le modèle nomme ce que la
    # clause subordonne à l'événement, au bien ou à l'assuré, puis ce que les faits déclarés
    # établissent *dans ces termes*. Le code fait la différence — c'est AD-6 à la lettre (« il extrait
    # des valeurs typées, le code compare ») et c'est ce qu'un `fait_requis_present` seul ne permettait
    # pas : rien ne pouvait contredire un booléen.
    #
    # Tour 2 de la même revue, deux corrections. (a) `None` **n'est pas** une liste vide : une liste
    # absente veut dire « je n'ai pas énuméré », et le contrôle est alors sans prise — le jeu de champs
    # entier est abandonné (`qualites_non_enumerees`) et la claim vaut `humain`. Le défaut vide laissait
    # au contraire une clause qui exige « un événement soudain » passer en `oui` sur un silence.
    # Une clause qui n'exige réellement aucune qualité se dit `[]`, explicitement. (b) une qualité
    # établie n'est pas une affirmation mais une **citation** : le modèle joint le fragment des faits
    # déclarés qui l'établit, et le code le relit mot pour mot dans les faits soumis (AD-3 appliqué aux
    # faits) — sans quoi recopier `qualites_exigees` dans `qualites_etablies` annulait le contrôle.
    qualites_exigees: list[str] | None = None
    qualites_etablies: list[QualiteEtablie] | None = None


class SortieVerifierSinistre(SortieVerifier):
    """Mode sinistre : le **même et unique** appel `micro` rend en plus l'applicabilité (AD-9 amendé).

    Hérite de `SortieVerifier` — pertinence, phrases soutenues et couverture des facettes sont
    exactement les mêmes questions, posées dans le même appel. La reprise différée de la story 1.5
    demandait littéralement que les champs typés arrivent « dans le même appel, pas dans un second » :
    un second appel `micro` de plus par requête sinistre pour des faits que le modèle a déjà sous les
    yeux serait un coût pur (NFR4), et deux lectures indépendantes des mêmes passages pourraient se
    contredire. Le guide, lui, garde `SortieVerifier` et son préfixe inchangés — donc ses fixtures.
    """

    applicabilite: list[ChampsApplicabiliteRendus] = []


def _lignes_du_bloc(block: Block) -> tuple[list[tuple[int, int, str]], bool]:
    """Position **brute** de chaque ligne dans `Block.text` : ([(début, fin, line_id)], toutes_mappées).

    Cherchée dans le texte d'origine et non dans `text_norm` (revue Codex 1.5, B7) : un bloc PDF est
    la concaténation de ses lignes, la recherche est donc exacte et aucune règle de normalisation ne
    peut faire disparaître une ligne. L'ancienne version cherchait la forme *normalisée* de chaque
    ligne et sautait celles que la règle de césure `-\\n` avait soudées à la suivante — le surlignage
    perdait alors des `line_ids` sans que rien ne le dise. Le drapeau rendu permet de le signaler si
    le cas se présentait malgré tout (bloc dont le texte n'est pas la concaténation de ses lignes).
    """
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    toutes = True
    for line in block.lines:
        if not line.text:
            continue
        i = block.text.find(line.text, cursor)
        if i < 0:
            toutes = False
            continue
        spans.append((i, i + len(line.text), line.line_id))
        cursor = i + len(line.text)
    return spans, toutes


class _Bloc:
    """Ce qu'un bloc cité coûte à préparer une fois, quel que soit le nombre de claims qui le citent.

    `spans` est l'image de `Block.text_norm` dans `Block.text` (`normalize_spans`) : c'est elle qui
    retraduit une occurrence prouvée en un passage brut, celui que le front affiche.
    """

    def __init__(self, block: Block) -> None:
        self.block = block
        norme, spans = normalize_spans(block.text)
        # Le loader a calculé `text_norm` avec `normalize()`, qui est la projection de
        # `normalize_spans()` : les deux formes coïncident par construction.
        self.norme = norme
        self.spans = spans
        self.lignes, self.lignes_completes = _lignes_du_bloc(block)


class _Controle:
    """Résultat du contrôle d'une quote : retrouvée (avec ses offsets) ou rejetée avec son motif."""

    def __init__(self, kind: str, motif: str, quote: VerifiedQuote | None = None) -> None:
        self.kind = kind  # "" | "non_retrouvee" | "ambigue"
        self.motif = motif
        self.quote = quote


def _controler_quote(block_id: str, quote: str, *, corpus: Any, index: Any, fournis: set[str],
                     blocs: dict[str, _Bloc], settings: Settings) -> _Controle:
    """AD-3, dans l'ordre de son texte : existence, kind, longueur, inclusion, non-ambiguïté.

    « `block_id` existe » se lit ici « existe **parmi les blocs transmis à *rédiger*** », pas « existe
    quelque part dans le corpus » (revue 1.5). Un identifiant réel mais jamais ouvert est une source
    que le rédacteur n'a pas lue : la quote s'y trouve par coïncidence de vocabulaire, et l'afficher
    reviendrait à sourcer une phrase sur un passage que rien n'a mis sous les yeux du modèle. AD-1
    fait de `opened_block_ids` « les blocs effectivement passés au modèle » ; c'est ce périmètre-là.
    """
    if block_id not in fournis:
        # Le `block_id` vient du modèle : il n'est pas recopié dans le motif (AD-15). Un identifiant
        # du corpus mais hors des blocs fournis n'est pas plus citable qu'un identifiant inventé.
        connu = block_id if _bloc_connu(index, block_id) else BLOC_INCONNU
        return _Controle("non_retrouvee", f"citation rattachée à un bloc qui n'a pas été fourni "
                                          f"dans ce message ({connu}) : ne cite que les blocs reçus")
    doc_id = index.doc_of(block_id)
    document = corpus.documents[doc_id]
    if block_id not in blocs:  # une seule préparation par bloc, même si plusieurs claims le citent
        blocs[block_id] = _Bloc(document.block(block_id))  # texte **toujours relu depuis le corpus**
    block = blocs[block_id].block
    if block.kind == "heading":
        return _Controle("non_retrouvee", f"le bloc {block_id} est un titre : un titre ne se cite pas seul, "
                                          "cite le paragraphe qui porte l'information")
    forme = normalize(quote)
    if not forme:
        return _Controle("non_retrouvee", f"citation vide pour le bloc {block_id}")
    assez_longue = (len(forme) >= settings.quote_min_chars
                    or len(forme) >= settings.quote_min_ratio * len(block.text_norm))
    if not assez_longue:
        return _Controle("non_retrouvee",
                         f"citation trop courte pour le bloc {block_id} : au moins "
                         f"{settings.quote_min_chars} caractères, ou "
                         f"{int(settings.quote_min_ratio * 100)} % du bloc")
    start = block.text_norm.find(forme)
    if start < 0:
        return _Controle("non_retrouvee", f"citation introuvable dans le bloc {block_id} : "
                                          "recopie le passage mot pour mot depuis le texte fourni")
    # AD-3, littéralement : « une quote présente dans plusieurs blocs du document ⇒ citation_ambigue ».
    # Deux occurrences dans le **même** bloc ne trompent personne (même bloc, même portée, même
    # texte) : on garde la première pour les offsets. Deux blocs différents, en revanche, attribuent
    # la phrase au mauvais endroit du document.
    autre = next((b.block_id for b in document.blocks
                  if b.block_id != block_id and forme in b.text_norm), None)
    if autre is not None:  # on s'arrête au premier doublon : le compte exact n'ajoute rien au motif
        return _Controle("ambigue", f"citation ambiguë : le même passage figure aussi ailleurs dans le "
                                    f"document, hors du bloc {block_id} — étends-la pour la rendre unique")
    end = start + len(forme)
    # AD-3 : « le texte affiché comme source est toujours relu depuis `corpus` ». On retraduit donc
    # l'occurrence prouvée dans le texte **brut** du bloc, et c'est ce passage-là — jamais la chaîne
    # rendue par le modèle — qui devient la citation affichée (revue Codex 1.5, B2).
    prepare = blocs[block_id]
    text_start = prepare.spans[start][0]
    text_end = prepare.spans[end - 1][1]
    line_ids = [lid for (a, b, lid) in prepare.lignes if a < text_end and b > text_start]
    return _Controle("", "", VerifiedQuote(block_id=block_id, quote=block.text[text_start:text_end],
                                           start=start, end=end, text_start=text_start,
                                           text_end=text_end, line_ids=line_ids))


def _bloc_connu(index: Any, block_id: str) -> bool:
    """Le `block_id` est-il une chaîne du corpus (donc de **notre** code) ou une invention du modèle ?"""
    try:
        index.doc_of(block_id)
    except KeyError:
        return False
    return True


def _nom_de_claim(claim: Claim, position: int) -> str:
    """Comment nommer la claim dans un motif : son `claim_id` s'il est plausible, sa position sinon."""
    return claim.claim_id if _CLAIM_ID.match(claim.claim_id) else f"claim n° {position}"


def _motif_de_relance(rejetees: list[RejectedClaim], noms: dict[str, str],
                      inactionnables: set[str]) -> str | None:
    """Motif composé par **notre** code, transmis tel quel à la relance de *rédiger* (AD-3).

    Il est délimité par `untrusted()` dans *rédiger* : ce texte mêle nos phrases à des `block_id`, et
    il ne devient jamais une consigne de confiance. L'en-tête ne présume pas de la nature du défaut :
    chaque ligne dit déjà si c'est la citation ou la pertinence qui a été rejetée, et annoncer « le
    contrôle des citations » sur un rejet de pertinence enverrait le modèle recopier mieux un passage
    déjà retrouvé mot pour mot. `None` quand rien n'est actionnable.
    """
    actionnables = [c for c in rejetees if c.claim_id not in inactionnables]
    if not actionnables:
        return None
    lignes = [f"- {noms[claim.claim_id]} : {claim.motif}" for claim in actionnables]
    return ("Le contrôle a rejeté les affirmations suivantes. Corrige précisément ce que chacune "
            "décrit, ou remplace-la par ce que les blocs fournis soutiennent vraiment :\n"
            + "\n".join(lignes))


def _clauses_citees(block_ids: list[str], *, corpus: Any, index: Any) -> list[ClauseCitee]:
    """Les blocs cités qui portent un `kind` décisionnel, relus **dans le corpus** (AD-6).

    `Block.kind` (ingestion) est la seule source de typage : ni *rédiger*, ni *vérifier* n'en
    produisent. Portée, nœud parent et socle sont pris sur le document, pas sur l'index — la table
    d'AD-6 lit `Document.scope_nodes()`, `Document.node_of()` et `Document.node_scope_kind()`, qui
    sont les mêmes calculs pour tous les appelants.
    """
    clauses: list[ClauseCitee] = []
    for block_id in block_ids:
        document = corpus.documents[index.doc_of(block_id)]
        block = document.block(block_id)
        if block.kind not in KINDS_DECISIONNELS:
            continue  # une définition ou un paragraphe est le contexte de la clause, pas une clause
        node_id = document.node_of(block_id)
        clauses.append(ClauseCitee(
            block_id=block_id, kind=block.kind, kind_confirmed=block.kind_confirmed,
            portee=document.scope_nodes(block_id), node_id=node_id,
            socle=document.node_scope_kind(node_id) == "commun",
            # Revue Codex 1.8 (B3, tour 3) : lu **dans le corpus**, comme le `kind` et la portée. Le
            # modèle énumère les qualités que la clause exige ; le texte de la clause dit, lui, s'il
            # avait quelque chose à énumérer. Une liste vide n'est plus « aucune qualité exigée »
            # quand la clause écrit « soudain » (`_qualites_de_la_clause`).
            qualificatifs=list(_mots_qualifiants(block.text).values())))
    return clauses


def _marquer_contradictions(jugees: list[ClaimJugee], *, corpus: Any, index: Any) -> None:
    """AD-6 : « deux claims en `relation=contredit` non résolues ⇒ `ne_tranche_pas`, les deux passages
    affichés ».

    La relation vit sur le bloc (`Block.relation.contredit`, posée à l'ingestion) ; elle ne devient un
    problème de verdict que quand les **deux** blocs sont cités par deux claims affichées différentes
    — un bloc qui contredit un passage que personne ne montre ne met rien en balance sous les yeux de
    l'utilisateur. « Non résolue » se lit littéralement : rien, dans le corpus servi à J+1, ne
    tranche une contradiction, donc toute paire citée en est une.
    """
    par_bloc: dict[str, str] = {}  # block_id cité → claim_id qui le cite
    for jugee in jugees:
        for clause in jugee.clauses:
            par_bloc.setdefault(clause.block_id, jugee.claim_id)
    for jugee in jugees:
        for clause in jugee.clauses:
            cible = corpus.documents[index.doc_of(clause.block_id)].block(clause.block_id).relation.contredit
            if cible is None:
                continue
            autre = par_bloc.get(cible)
            if autre is not None and autre != jugee.claim_id:
                jugee.contredit = True


async def verifier(draft: AnswerDraft, *, parsed: ParsedQuestion, retrieval: RetrievalResult,
                   corpus: Any, index: Any, client: LlmClient, budget: RequestBudget,
                   settings: Settings,
                   faits: Faits | None = None,
                   dossier: MissingPackage | None = None) -> tuple[Verification, StepTrace]:
    """`faits` non nul ⇒ **mode sinistre** (AD-6) : même étape, deux jugements de plus dans le même appel.

    Ce que le mode ajoute, et rien d'autre : `verifier_sinistre.md` appendu au préfixe, un bloc
    `untrusted("faits", …)` dans le message, le schéma `SortieVerifierSinistre`, le contrôle « une
    clause par affirmation » (D6), la dérivation d'`applicable` et l'application de la table AD-6.
    Le nombre d'appels ne change pas : **un** appel `micro` groupé, jamais deux (AD-9 amendé).
    Sans `faits`, l'étape est celle du guide, à l'octet près.

    `dossier` est le `MissingPackage` que l'appelant **a** déjà (conditions particulières, options,
    avenants, date d'effet). Il n'est jamais deviné ni rempli ici : absent, tout est réputé inconnu et
    la règle (2) d'AD-6 plafonne le verdict à `sous_conditions`.
    """
    t0 = time.monotonic()
    step = StepTrace(name="verifier", tier=STEP_TIERS["verifier"])
    sinistre = faits is not None

    # Blocs réellement transmis à *rédiger* : le périmètre exact de ce qui est citable (AD-1,
    # « les blocs effectivement passés au modèle »).
    fournis = {b.block_id for b in retrieval.blocs}
    blocs_prepares: dict[str, _Bloc] = {}

    def edition_de(block_ids: list[str]) -> str:
        """`edition` (AD-4) : celle du document **cité**, affichée « édition … — actualité non vérifiée ».

        Prise sur les blocs de la claim, jamais sur le premier bloc du retrieval : le guide n'a qu'un
        document, le sinistre en aura plusieurs (1.8), et une édition empruntée à un autre document
        serait affichée sous la citation sans que rien ne le signale (revue 1.5).
        """
        for b in block_ids:
            if b in fournis:
                return corpus.documents[index.doc_of(b)].edition
        return ""

    retrouvees: list[tuple[Claim, list[VerifiedQuote], str]] = []
    rejetees: list[RejectedClaim] = []
    noms: dict[str, str] = {}  # `claim_id` → nom sûr pour les motifs (les `claim_id` sont uniques, AD-3)
    clauses_par_claim: dict[str, list[ClauseCitee]] = {}  # mode sinistre : les clauses de chaque claim
    for position, claim in enumerate(draft.claims, start=1):
        noms[claim.claim_id] = _nom_de_claim(claim, position)
        du_draft = [Quote(block_id=q.block_id, quote=q.quote) for q in claim.quotes]
        edition = edition_de([q.block_id for q in claim.quotes])
        controles = [_controler_quote(q.block_id, q.quote, corpus=corpus, index=index, fournis=fournis,
                                      blocs=blocs_prepares, settings=settings)
                     for q in claim.quotes]
        echecs = [c for c in controles if c.kind]
        if echecs:
            # `non_retrouvee` prime `ambigue` : une citation introuvable est un défaut plus grave
            # qu'une citation trop large, et le motif doit nommer d'abord ce qu'il faut corriger.
            kind = "non_retrouvee" if any(c.kind == "non_retrouvee" for c in echecs) else "ambigue"
            rejetees.append(RejectedClaim(
                claim_id=claim.claim_id, text=claim.text, quotes=du_draft,
                status=ClaimStatus(retrouvee=False, pertinente=None, edition=edition),
                rejection_kind=kind, motif=" ; ".join(c.motif for c in echecs)))
            continue
        quotes = [c.quote for c in controles if c.quote is not None]
        if sinistre:
            # D6 / AD-6 : « une claim décisionnelle ne couvre qu'un **seul** `kind` ». Le contrôle est
            # en **code** — le typage vient de l'ingestion, le modèle n'a pas à s'en mêler. Deux kinds
            # décisionnels dans une même affirmation rendraient la table d'AD-6 indécidable : la même
            # claim serait à la fois la garantie qu'on retient et l'exclusion qui l'écarte, avec un
            # seul jeu de champs typés pour les deux. Le rejet est `ambigue`, donc un défaut de
            # citation au sens d'AD-3 : il déclenche la relance unique avec un motif actionnable.
            clauses = _clauses_citees([q.block_id for q in quotes], corpus=corpus, index=index)
            kinds = sorted({c.kind for c in clauses})
            if len(kinds) > 1:
                rejetees.append(RejectedClaim(
                    claim_id=claim.claim_id, text=claim.text, quotes=list(quotes), status=ClaimStatus(
                        retrouvee=True, pertinente=None, edition=edition),
                    line_ids=[lid for q in quotes for lid in q.line_ids],
                    rejection_kind="ambigue",
                    motif=f"affirmation qui mêle {len(kinds)} clauses de natures différentes "
                          f"({', '.join(kinds)}) : une seule clause par affirmation — fais-en autant "
                          f"d'affirmations distinctes"))
                continue
            clauses_par_claim[claim.claim_id] = clauses
        retrouvees.append((claim, quotes, edition))

    # `quote_max_chars` était annoncé au modèle dans `prompts/rediger.md`, publié dans `thresholds()`
    # — et appliqué par personne (reprise différée `target_story: 2.1`). Il l'est ici, et **jamais**
    # comme un rejet : la citation a passé tous les contrôles d'AD-3, elle est exacte au caractère
    # près et relue depuis le corpus ; elle est seulement bavarde. La rejeter transformerait une
    # réponse correcte en refus `claims_rejetes` — un dégradé bien pire que le défaut qu'on corrige.
    # Le constat va donc dans la trace, là où il sert à régler le prompt et le seuil (4.2) : la
    # promesse du README (« régler un seuil ne peut plus désynchroniser ce que le modèle produit et
    # ce que le code accepte ») vaut désormais aussi pour le maximum, et pas seulement pour le
    # minimum. Le détail ne porte que des **comptes** : AD-10 interdit le texte d'un bloc dans la trace.
    trop_longues = sum(1 for _, quotes, _ in retrouvees for q in quotes
                       if len(q.quote) > settings.quote_max_chars)
    if trop_longues:
        step.checks.append(CheckResult(
            name="quote_trop_longue", ok=False,
            detail=f"{trop_longues} citation(s) vérifiée(s) dépassent quote_max_chars "
                   f"({settings.quote_max_chars} caractères) : exactes, seulement bavardes — "
                   "affichées telles quelles, jamais tronquées ni rejetées"))

    # AD-4 : **un seul** appel `micro` groupé, borné par `verifier_max_claims`. Au-delà, les claims
    # excédentaires ne sont pas évaluées — jamais devinées (`draft_max_claims` fait que le cas ne se
    # produit pas sur le corpus servi, la borne est une ceinture).
    evaluees = retrouvees[: settings.verifier_max_claims]
    excedentaires = retrouvees[settings.verifier_max_claims:]
    # Les phrases soumises au contrôle sont celles qui ont une chance d'être affichées : un segment
    # vide ne l'est pas, et un segment `factuel` dont **aucune** claim n'a passé le contrôle de
    # citation est retiré par AD-3 de toute façon. Payer des tokens pour les juger serait du gâchis.
    citables = {claim.claim_id for claim, _, _ in retrouvees}
    a_juger = [(i, s) for i, s in enumerate(draft.segments)
               if s.text.strip() and (s.kind != "factuel" or (set(s.claim_ids) & citables))]
    verdicts: dict[str, bool] = {}
    couverture: dict[int, list[str]] = {}
    soutiens: dict[int, bool] = {}
    applicabilites: dict[str, ChampsApplicabilite] = {}
    if evaluees:
        try:
            verdicts, couverture, soutiens, applicabilites = await _pertinence(
                evaluees, parsed=parsed, segments=a_juger, corpus=corpus, index=index, client=client,
                budget=budget, settings=settings, step=step, faits=faits,
                clauses=clauses_par_claim)
        except PipelineError:
            step.ms = int((time.monotonic() - t0) * 1000)  # l'appel raté garde sa durée (AD-10)
            raise

    claims: list[VerifiedClaim] = []
    jugees: dict[str, ClaimJugee] = {}  # mode sinistre : ce que la table AD-6 lira des claims retenues
    manquants = 0
    applicabilite_manquante = 0
    for claim, quotes, edition in evaluees:
        pertinente = verdicts.get(claim.claim_id)
        if pertinente is None:
            manquants += 1
        applicable = None
        if sinistre:
            # D2 : `applicable` est calculé pour **toute** claim retenue, et vaut `None` quand aucune
            # de ses quotes ne cite un bloc décisionnel — une définition n'a pas d'applicabilité.
            clauses = clauses_par_claim.get(claim.claim_id, [])
            champs = applicabilites.get(claim.claim_id)
            if clauses and champs is None:
                applicabilite_manquante += 1
            jugee = ClaimJugee(
                claim_id=claim.claim_id, clauses=clauses, champs=champs, retenue=pertinente is True,
                renvoi_ouvert=any(corpus.documents[index.doc_of(q.block_id)]
                                  .block(q.block_id).unresolved_refs for q in quotes))
            applicable = applicable_de_claim(jugee)
            jugees[claim.claim_id] = jugee
        status = ClaimStatus(retrouvee=True, pertinente=pertinente, applicable=applicable,
                             edition=edition)
        line_ids: list[str] = []
        for q in quotes:
            line_ids += [lid for lid in q.line_ids if lid not in line_ids]
        if pertinente is True:
            claims.append(VerifiedClaim(claim_id=claim.claim_id, text=claim.text, quotes=quotes,
                                        status=status, line_ids=line_ids))
            continue
        motif = ("citation non pertinente : le passage cité ne soutient pas l'affirmation, ou "
                 "l'affirmation ne répond pas à la question posée"
                 if pertinente is False else
                 "pertinence non rendue par le contrôle groupé : l'affirmation est écartée plutôt que devinée")
        # Ces quotes **ont** été retrouvées : leurs offsets et `line_ids` sont conservés, c'est ce qui
        # rend la claim « affichable par le front » comme AD-3 le demande.
        rejetees.append(RejectedClaim(
            claim_id=claim.claim_id, text=claim.text, quotes=list(quotes), status=status,
            line_ids=line_ids, rejection_kind="non_pertinente", motif=motif))
    # Une claim que la borne `verifier_max_claims` a laissée hors du contrôle groupé n'a rien à
    # corriger : elle n'a pas été jugée. La faire figurer dans le motif de relance demanderait au
    # modèle de réparer une décision qui est la nôtre (revue 1.5).
    inactionnables = {claim.claim_id for claim, _, _ in excedentaires}
    for claim, quotes, edition in excedentaires:
        rejetees.append(RejectedClaim(
            claim_id=claim.claim_id, text=claim.text, quotes=list(quotes),
            line_ids=[lid for q in quotes for lid in q.line_ids],
            status=ClaimStatus(retrouvee=True, pertinente=None, edition=edition),
            rejection_kind="non_pertinente",
            motif=f"affirmation non évaluée : le contrôle de pertinence est borné à "
                  f"{settings.verifier_max_claims} affirmations par réponse"))

    if manquants:
        step.checks.append(CheckResult(
            name="pertinence_incomplete", ok=False,
            detail=f"{manquants} affirmation(s) sur {len(evaluees)} sans verdict de pertinence : écartées"))
    if applicabilite_manquante:
        # AC de la story : un champ d'applicabilité non rendu pour une claim décisionnelle donne
        # `humain` — jamais une valeur devinée — et la trace le dit. Le silence du modèle ne doit
        # jamais ressembler à une clause sans réserve.
        step.checks.append(CheckResult(
            name="applicabilite_incomplete", ok=False,
            detail=f"{applicabilite_manquante} affirmation(s) citant une clause décisionnelle sans "
                   "champs typés d'applicabilité : traitées comme `humain`"))
    if any(not b.lignes_completes for b in blocs_prepares.values()):
        # B7 : une ligne du bloc introuvable dans son propre texte brut — le surlignage serait partiel.
        step.checks.append(CheckResult(
            name="lignes_incompletes", ok=False,
            detail="au moins un bloc cité n'est pas la concaténation de ses lignes : "
                   "les `line_ids` de l'occurrence peuvent être incomplets"))

    # --- ce qui sera réellement affiché (revue Codex 1.5, tour 2, B1) --------
    # AD-3 ne fait porter sa règle mécanique que sur les `claim_ids` d'un segment `factuel` : elle
    # garantit qu'une phrase affichée *pointe* vers une affirmation vérifiée, pas que son **texte**
    # dise ce que cette affirmation dit. Rien ne contrôlait non plus les segments `transition` et
    # `limite`, qui ne portent aucune claim et traversaient le rendu tels quels — un fait glissé dans
    # une transition s'affichait donc sans aucune source. L'AC de la story est plus exigeant que la
    # règle mécanique (« qu'aucune phrase ne me soit montrée sans un passage du guide qui la
    # soutient ») : chaque phrase envoyée au contrôle groupé doit en revenir `soutenu=true`. Une
    # phrase sans verdict n'est pas devinée — elle n'est pas affichée (même règle que `pertinente`).
    survivants = list(draft.segments)
    ecartes = 0
    if evaluees:  # sans appel groupé, aucun verdict n'a pu être rendu : rien n'est jugé, ni retiré
        soumis = {i for i, _ in a_juger}
        survivants = [s for i, s in enumerate(draft.segments)
                      if not s.text.strip() or (i in soumis and soutiens.get(i) is True)]
        ecartes = sum(1 for i, s in enumerate(draft.segments)
                      if s.text.strip() and not (i in soumis and soutiens.get(i) is True)
                      and (s.kind != "factuel" or (set(s.claim_ids) & citables)))
        if ecartes:
            step.checks.append(CheckResult(
                name="segments_non_soutenus", ok=False,
                detail=f"{ecartes} phrase(s) de l'ébauche avancent plus que les passages joints "
                       "(ou n'ont pas été jugées) : elles ne sont pas affichées"))

    # --- ce qu'une phrase ne peut pas prouver : l'absence (revue Codex 1.5, tour 3, B1) -----
    # Un segment `limite` dit ce que le guide **ne dit pas**. Aucun passage ne peut le soutenir : une
    # assertion d'absence n'est pas citable, par construction — la seule preuve d'absence que le
    # projet sache produire est l'`AbsenceProof` d'AD-4, composée par le **code**, et la seule phrase
    # d'absence affichée est celle du refus, écrite dans `restituer.PHRASES_DE_REFUS`. Une limite
    # rédigée par le modèle ne rejoint donc jamais `Answer.texte` ni `Answer.segments[]` : elle
    # subsiste dans `Answer.unknown[]`, le canal typé qu'AD-4 réserve aux lacunes, qui interdit
    # `complete=True` et que le front rend comme une limite — jamais comme une réponse.
    segments_affiches = [s for s in survivants if s.kind != "limite"]
    unknown = [s.text for s in survivants if s.kind == "limite" and s.text.strip()]

    # AD-3 : « tout segment `factuel` référence ≥ 1 claim survivante », et `Answer.texte` n'est fait
    # que des segments survivants. Une claim que **plus aucun** segment affiché ne cite n'a donc
    # nulle part où paraître : la garder dans `claims[]` autoriserait `found=True` sur un texte vide,
    # ce qu'AD-16 nomme « réponse vide présentée comme réponse » (revue Codex 1.5, B6). Un seul
    # passage suffit : un segment factuel ne survit que par une claim retenue, laquelle est alors
    # citée par lui.
    retenus = {c.claim_id for c in claims}
    citees = {cid for s in segments_affiches if s.kind == "factuel" and s.text.strip()
              for cid in s.claim_ids if cid in retenus}
    orphelines = [c for c in claims if c.claim_id not in citees]
    if orphelines:
        claims = [c for c in claims if c.claim_id in citees]
        for c in orphelines:
            rejetees.append(RejectedClaim(
                claim_id=c.claim_id, text=c.text, quotes=list(c.quotes), status=c.status,
                line_ids=list(c.line_ids), rejection_kind="non_citee",
                motif="affirmation vérifiée qu'aucune phrase de la réponse ne cite : rattache-la à un "
                      "segment factuel, ou retire-la"))
        step.checks.append(CheckResult(
            name="claims_non_citees", ok=False,
            detail=f"{len(orphelines)} affirmation(s) vérifiée(s) qu'aucun segment factuel n'affiche : écartée(s)"))

    # AD-6 / D4 : le verdict porte sur les claims **affichées**, donc après le filtre `non_citee`
    # ci-dessus. Un verdict adossé à une clause que l'utilisateur ne voit pas contredirait « rien
    # d'affiché sans preuve » — et AD-4 vient précisément de sortir cette claim de `claims[]`.
    verdict: Verdict | None = None
    if sinistre:
        affichables = [jugees[c.claim_id] for c in claims if c.claim_id in jugees]
        _marquer_contradictions(affichables, corpus=corpus, index=index)
        verdict = decider(affichables, ask_client_max=settings.ask_client_max, missing=dossier)
        # `ok=True` quelle que soit la valeur : AD-6 fait de `ne_tranche_pas` « un résultat rare et
        # gagné, pas un repli par défaut » (AD-3 le redit). Le marquer en échec ferait passer pour un
        # défaut du système la seule réponse honnête sur un contrat qui ne tranche pas — et un lecteur
        # de trace y verrait un incident. La valeur reste dans le détail, qui est là pour ça.
        step.checks.append(CheckResult(
            name="verdict", ok=True,
            detail=f"{verdict.value} sur {len(affichables)} affirmation(s) affichée(s)"))

    # AD-4 : `found` et `complete` sont calculés **ici**, jamais produits par le modèle.
    found = bool(claims)
    cites = {q.block_id for c in claims for q in c.quotes}
    renvois_ouverts = any(corpus.documents[index.doc_of(b)].block(b).unresolved_refs for b in cites)
    # AD-4 exige « toutes les facettes de `ParsedQuestion` couvertes ». `unknown == []` n'en est pas
    # une approximation conservatrice : une réponse à deux facettes dont une est omise, sans segment
    # `limite`, sortait `complete=True` (revue Codex 1.5, B3). Les facettes sont celles de
    # `ParsedQuestion`, **littéralement** : le découpage a été arrêté par *comprendre*, avant tout
    # retrieval et toute rédaction, et le contrôle groupé ne fait que dire qui y répond (tour 3). Une
    # sous-question à laquelle la réponse n'a pas répondu ne peut donc plus s'effacer du barème avec
    # elle. Aucune facette au barème (question sans découpage rendu) ⇒ aucune preuve ⇒
    # `complete=False` : l'absence de mesure ne vaut jamais complétude.
    affichees = {c.claim_id for c in claims}
    facettes_couvertes = sorted(rang for rang, ids in couverture.items()
                                if any(cid in affichees for cid in ids))
    couvertes = bool(parsed.facettes) and len(facettes_couvertes) == len(parsed.facettes)
    if evaluees and not couvertes:
        step.checks.append(CheckResult(
            name="facettes_non_couvertes", ok=False,
            detail=f"{len(facettes_couvertes)} facette(s) couverte(s) par une affirmation affichée sur "
                   f"{len(parsed.facettes)} posée(s) par la question : la réponse n'est pas donnée "
                   "pour complète"))
    # --- « partiel » dit toujours ce qui manque (story 2.3) ------------------
    # AD-4 énumère quatre conditions de `complete`, et l'AC de la story exige « partiel **avec
    # `unknown[]` listé** ». Jusqu'ici, une seule des six causes d'incomplétude écrivait quelque
    # chose : l'utilisateur lisait « PARTIEL » sans savoir de quoi. Chaque cause est donc nommée en
    # comme une cause typée, **par le code** (AD-16 / NFR2 : jamais par le modèle), puis projetée
    # dans `unknown[]` par *restituer* — après quoi `complete ⟺ found ∧ unknown = []` devient un
    # invariant du domaine, porté par `Answer._found_coherence`.
    #
    # Les lacunes ne portent ni `block_id`, ni terme cherché, ni contenu de bloc (AD-10, AD-15), ni
    # document. Leur projection est commune au guide et au sinistre ; « le guide » y serait faux.
    #
    # **Aucune lacune sur un refus.** `found=False` porte son `AbsenceProof`, qui dit déjà ce qui a
    # été cherché et pourquoi rien n'a été retenu ; y ajouter « il me manque des éléments » ferait
    # deux comptes rendus du même fait.
    lacunes = _lacunes(retrieval=retrieval, parsed=parsed, facettes_couvertes=facettes_couvertes,
                       renvois_ouverts=renvois_ouverts, ecartes=ecartes) if found else []
    # Une phrase écartée faute de soutien est une part de la réponse que l'ébauche voulait donner et
    # qui n'est pas montrée — y compris une limite retirée. La réponse servie est alors amputée :
    # elle n'est pas donnée pour complète (AD-4, « aucune troncature »). Les six conditions d'AD-4
    # sont maintenant **toutes** représentées dans l'un des deux canaux : la conjonction se réduit
    # sans rien perdre, et il n'y a plus deux endroits où « incomplet » se décide.
    complete = found and not unknown and not lacunes

    verification = Verification(
        segments=segments_affiches, claims=claims, rejected_claims=rejetees, found=found,
        complete=complete, unknown=unknown, lacunes=lacunes,
        facettes_couvertes=facettes_couvertes, verdict=verdict,
        motif=_motif_de_relance(rejetees, noms, inactionnables) if rejetees else None,
    )
    step.checks.append(CheckResult(
        name="citations", ok=not rejetees,
        detail=f"{len(claims)} affirmation(s) retenue(s), {len(rejetees)} rejetée(s) sur {len(draft.claims)}"))
    step.ms = int((time.monotonic() - t0) * 1000)
    return verification, step


def _lacunes(*, retrieval: RetrievalResult, parsed: ParsedQuestion, facettes_couvertes: list[int],
             renvois_ouverts: bool, ecartes: int) -> list[Lacune]:
    """Les causes typées d'une réponse trouvée mais incomplète, dans l'ordre du pipeline.

    Une cause par fait, dans l'ordre où ils se produisent le long de la chaîne : ce qui n'a pas
    été lu, puis ce qui n'a pas été mesuré, puis ce qui n'a pas été couvert, puis ce qui n'a pas été
    résolu, puis ce qui n'a pas été affiché. La liste est vide quand la réponse est complète — et
    c'est **la** définition de `complete` depuis cette story.

    Aucune donnée ne contient un `block_id`, un terme cherché ni un extrait (AD-10, AD-15). Les
    cardinaux sont les nôtres, comptés ici ; la première personne et la neutralité documentaire
    appartiennent aux patrons de *restituer*, dans la langue décidée pour la réponse.
    """
    lacunes: list[Lacune] = []
    if retrieval.truncated:
        # NFR2 : budget de retrieval épuisé ou troncature ⇒ jamais d'`AbsenceProof`, et jamais
        # `complete`. La phrase dit la borne, pas ce qu'elle a coupé — nous ne le savons pas.
        lacunes.append(Lacune(kind="lecture_bornee"))
    if not parsed.facettes:
        # AD-4 (tour 3) : aucune facette au barème ⇒ aucune preuve de couverture ⇒ jamais complet.
        # L'absence de mesure est une lacune en soi, et elle se dit — sinon « partiel » resterait nu
        # sur la seule question qui n'a pas pu être découpée.
        lacunes.append(Lacune(kind="sans_decoupage"))
    elif len(facettes_couvertes) < len(parsed.facettes):
        manquantes = len(parsed.facettes) - len(facettes_couvertes)
        lacunes.append(Lacune(kind="facettes_sans_reponse", n=manquantes))
    if renvois_ouverts:
        lacunes.append(Lacune(kind="renvoi_non_resolu"))
    if ecartes:
        lacunes.append(Lacune(kind="phrases_ecartees", n=ecartes))
    return lacunes


async def _pertinence(evaluees: list[tuple[Claim, list[VerifiedQuote], str]], *, parsed: ParsedQuestion,
                      segments: list[tuple[int, AnswerSegment]], corpus: Any, index: Any,
                      client: LlmClient, budget: RequestBudget, settings: Settings, step: StepTrace,
                      faits: Faits | None = None,
                      clauses: dict[str, list[ClauseCitee]] | None = None,
                      ) -> tuple[dict[str, bool], dict[int, list[str]], dict[int, bool],
                                 dict[str, ChampsApplicabilite]]:
    """L'unique appel `micro` groupé : pertinence, phrases soutenues, couverture — et l'applicabilité.

    Tout sort du **même** appel (AD-9 amendé : « un seul appel groupé, qui rend pertinence, phrases
    soutenues, couverture des facettes **et** champs typés d'applicabilité. Jamais un second appel »).
    Les quelques dizaines de tokens des segments et des faits déclarés ne justifient pas un appel de
    plus, et deux lectures séparées des mêmes passages pourraient se contredire.

    En mode sinistre, le préfixe **appende** `verifier_sinistre.md` à celui du guide et le schéma
    devient `SortieVerifierSinistre` : le préfixe du guide reste byte-identique, donc ses fixtures
    live rejouables (D5).
    """
    sinistre = faits is not None
    clauses = clauses or {}
    prefix = load_prompt("commun") + "\n\n" + load_prompt("verifier")
    if sinistre:
        # `render_prompt` et non `load_prompt` : la borne du libellé est un seuil de `config.py`, et un
        # prompt qui l'annonce est un seuil comme un autre (convention Seuils). Sans elle, un libellé
        # trop long est **ignoré** par le code — le fait manquant disparaît alors de `ask_client` sans
        # que le modèle ait jamais su qu'il y avait une limite à tenir (revue 1.8).
        prefix += "\n\n" + render_prompt("verifier_sinistre",
                                          fait_manquant_max_chars=settings.fait_manquant_max_chars,
                                          qualites_exigees_max=settings.qualites_exigees_max)
    parts = [untrusted("question", parsed.question_resolue)]
    if faits is not None:
        # AD-15 : les faits déclarés sont du contenu utilisateur — délimités, placés après le préfixe.
        parts.append(untrusted("faits", json.dumps(faits.model_dump(), ensure_ascii=False,
                                                   sort_keys=True)))
    for rang, libelle in enumerate(parsed.facettes):
        # Le découpage vient de *comprendre* : il est **donné** au contrôle, numéroté par notre code.
        # Le contrôle n'a plus qu'à dire qui y répond — il ne peut plus faire disparaître une
        # sous-question en ne la rendant pas (revue Codex 1.5, tour 3, B3).
        parts.append(untrusted("facette", json.dumps({"facette": rang, "libelle": libelle},
                                                     ensure_ascii=False)))
    for claim, quotes, _edition in evaluees:
        # Le passage soumis est **relu dans le corpus** : `text_norm[start:end]`, l'occurrence même
        # dont l'inclusion a été prouvée — jamais la chaîne du draft. Une citation « écho » ne peut
        # donc pas être jugée pertinente sur sa propre invention (AD-3). La forme normalisée suffit
        # à juger un sens ; elle ne sert pas à l'affichage (l'UI relit le bloc par `block_id` et
        # offsets).
        citations = []
        for q in quotes:
            block = corpus.documents[index.doc_of(q.block_id)].block(q.block_id)
            citations.append({"block_id": q.block_id, "passage": block.text_norm[q.start:q.end]})
        charge: dict[str, Any] = {"claim_id": claim.claim_id, "affirmation": claim.text,
                                  "citations": citations}
        clauses_de_la_claim = clauses.get(claim.claim_id, [])
        if clauses_de_la_claim:
            # Le `kind` vient de l'ingestion, jamais du modèle (AD-6) : on le lui **dit**, pour qu'il
            # sache de quelle affirmation on attend des champs typés — et il n'y en a qu'un, le
            # contrôle « une clause par affirmation » l'a déjà garanti (D6).
            charge["clause"] = clauses_de_la_claim[0].kind
        parts.append(untrusted("claim", json.dumps(charge, ensure_ascii=False)))
    for position, segment in segments:
        # Le texte du segment vient du modèle : il est délimité comme tout le reste (AD-15). C'est
        # bien le texte **affiché** qui est soumis, pas `Claim.text` : le premier peut dire autre
        # chose que le second, et c'est le premier que l'utilisateur lit (revue Codex 1.5, tour 2, B1).
        parts.append(untrusted("segment", json.dumps(
            {"segment": position, "kind": segment.kind, "texte": segment.text,
             "claim_ids": list(segment.claim_ids)}, ensure_ascii=False)))
    content = "\n\n".join(parts)
    try:
        result = await client.parse(tier=STEP_TIERS["verifier"], system_prefix=prefix,
                                    messages=[{"role": "user", "content": content}],
                                    output_model=SortieVerifierSinistre if sinistre else SortieVerifier,
                                    budget=budget, step=step,
                                    max_tokens=(settings.verifier_sinistre_max_tokens if sinistre
                                                else settings.verifier_max_tokens))
    except PipelineError as exc:
        # AD-10/AD-16 (revue Codex 1.5, tour 2, B5) : l'appel a pu être facturé — `step.calls` le
        # porte, `budget` aussi. Sans ce rattachement, le pipeline ne peut pas distinguer un appel
        # **commencé** d'un appel qui n'a jamais démarré, et il servait alors la réponse acquise
        # (200) sur une panne du fournisseur survenue pendant la seconde vérification. L'erreur
        # reste terminale : c'est l'appelant qui décide, pas nous.
        exc.step = step
        raise
    attendus = {claim.claim_id for claim, _, _ in evaluees}
    verdicts: dict[str, bool] = {}
    for v in result.parsed.verdicts:
        if v.claim_id not in attendus:  # un identifiant inventé ne décide de rien
            continue
        if v.claim_id in verdicts and verdicts[v.claim_id] != v.pertinente:
            # Le prompt interdit de répondre deux fois pour un même identifiant, et dit « dans le
            # doute, réponds false ». Une contradiction est un doute : elle écarte la claim, elle ne
            # s'arbitre pas par l'ordre d'arrivée (revue 1.5).
            verdicts[v.claim_id] = False
            step.checks.append(CheckResult(
                name="verdict_contradictoire", ok=False,
                detail="deux verdicts opposés pour une même affirmation : elle est écartée"))
            continue
        verdicts.setdefault(v.claim_id, v.pertinente)
    # La couverture ne s'entend que sur des rangs **envoyés** et des `claim_id` **attendus** : un
    # rang inventé ne couvre rien, un identifiant inventé non plus, et une facette dont le contrôle
    # ne dit rien reste une facette de la question — non couverte, donc `complete=False`.
    rangs = set(range(len(parsed.facettes)))
    couverture: dict[int, list[str]] = {}
    for f in result.parsed.facettes:
        if f.facette not in rangs:
            continue
        couverture.setdefault(f.facette, [])
        couverture[f.facette] += [c for c in f.claim_ids if c in attendus
                                  and c not in couverture[f.facette]]
    # Même règle que pour les verdicts de pertinence : une position qui n'a pas été envoyée ne décide
    # de rien, et deux réponses opposées sur la même phrase valent « non soutenu » (« dans le doute,
    # réponds false »). Une phrase sans verdict n'est pas devinée — elle ne sera pas affichée.
    positions = {position for position, _ in segments}
    soutiens: dict[int, bool] = {}
    for s in result.parsed.segments:
        if s.segment not in positions:
            continue
        if s.segment in soutiens:
            if soutiens[s.segment] != s.soutenu:
                soutiens[s.segment] = False
                step.checks.append(CheckResult(
                    name="segment_contradictoire", ok=False,
                    detail="deux verdicts opposés pour une même phrase : elle n'est pas affichée"))
            continue
        soutiens[s.segment] = s.soutenu

    # Champs typés d'applicabilité (mode sinistre). Mêmes garde-fous que pour les verdicts : un
    # `claim_id` inventé ne décide de rien, une affirmation sans clause décisionnelle n'a pas
    # d'applicabilité à recevoir, et deux réponses pour la même affirmation ne s'arbitrent pas par
    # l'ordre d'arrivée — elles sont **écartées**, ce qui rendra `humain` (jamais deviné).
    applicabilites: dict[str, ChampsApplicabilite] = {}
    if sinistre and isinstance(result.parsed, SortieVerifierSinistre):
        doublons: set[str] = set()
        # Les faits déclarés, normalisés une fois : c'est le seul texte contre lequel une qualité
        # dite établie se relit (B3, tour 2). Tous les champs renseignés, dans l'ordre du modèle.
        faits_norm = normalize(" ".join(
            str(v) for v in (faits.model_dump() if faits is not None else {}).values() if v is not None))
        for a in result.parsed.applicabilite:
            if a.claim_id not in attendus or not clauses.get(a.claim_id):
                continue
            if a.claim_id in applicabilites or a.claim_id in doublons:
                applicabilites.pop(a.claim_id, None)
                doublons.add(a.claim_id)
                step.checks.append(CheckResult(
                    name="applicabilite_contradictoire", ok=False,
                    detail="deux jeux de champs typés pour une même affirmation : elle est traitée "
                           "comme `humain`"))
                continue
            if a.qualites_exigees is None or a.qualites_etablies is None:
                # Revue Codex 1.8 (B3), tour 2 : le silence n'est pas « aucune qualité exigée ». Une
                # liste absente laisse le contrôle sans prise — et c'est précisément par là qu'une
                # clause exigeant « un événement soudain » passait en `oui` sans que rien ne l'établisse.
                # Le jeu de champs est inexploitable : la claim retombe sur `applicabilite_incomplete`.
                step.checks.append(CheckResult(
                    name="qualites_non_enumerees", ok=False,
                    detail="les qualités exigées ou établies n'ont pas été énumérées : le jeu de "
                           "champs typés est ignoré (l'affirmation est traitée comme `humain`)"))
                continue
            libelles = [(a.fait_manquant or "").strip(),
                        *(q.strip() for q in a.qualites_exigees),
                        *(q.qualite.strip() for q in a.qualites_etablies),
                        *(q.fait_cite.strip() for q in a.qualites_etablies)]
            trop_long = any(len(libelle) > settings.fait_manquant_max_chars for libelle in libelles)
            trop_nombreux = (len(a.qualites_exigees) > settings.qualites_exigees_max
                             or len(a.qualites_etablies) > settings.qualites_exigees_max)
            if trop_long or trop_nombreux:
                # D8 : ces libellés sont les **seuls** textes du modèle que l'utilisateur lira. Hors
                # borne, ils sont ignorés — jamais tronqués : une demi-phrase de fait manquant induit
                # en erreur plus sûrement qu'un fait tu. Mais on ne peut pas se contenter d'effacer le
                # libellé en gardant les booléens (revue Codex 1.8, B2) : `fait_requis_present=false`
                # **sans** `fait_manquant` est précisément la signature du « fait connu et contraire »,
                # qui rend `applicable="non"` sur une garantie ou une exclusion. Un fait explicitement
                # manquant mais trop long devenait alors une certitude d'inapplicabilité, et une
                # exclusion potentiellement applicable pouvait être écartée. Le jeu de champs entier
                # est donc **inexploitable** : l'entrée est abandonnée, la claim retombe sur
                # `applicabilite_incomplete` et vaut `humain` (jamais deviné).
                step.checks.append(CheckResult(
                    name="applicabilite_hors_borne", ok=False,
                    detail=f"un libellé dépasse {settings.fait_manquant_max_chars} caractères ou plus "
                           f"de {settings.qualites_exigees_max} qualités sont rendues : le jeu de "
                           "champs typés est ignoré (l'affirmation est traitée comme `humain`)"))
                continue
            # B3 : le **code** compare, le modèle n'a fait qu'énumérer. La comparaison passe par
            # `normalize()` — la même normalisation que les citations — pour qu'une majuscule ou un
            # accent ne fasse pas croire à une qualité non établie.
            #
            # Tour 2 : une qualité n'est retenue pour établie que si le fragment des faits que le
            # modèle produit avec elle se **relit mot pour mot** dans les faits déclarés (AD-3 appliqué
            # aux faits). Sans cela, recopier `qualites_exigees` dans `qualites_etablies` suffisait à
            # annuler le contrôle : le modèle se corroborait lui-même.
            etablies: set[str] = set()
            for q in a.qualites_etablies:
                if not q.qualite.strip():
                    continue
                cite = normalize(q.fait_cite)
                if not cite or cite not in faits_norm:
                    step.checks.append(CheckResult(
                        name="fait_cite_introuvable", ok=False,
                        detail="une qualité dite établie ne cite aucun fragment relu dans les faits "
                               "déclarés : elle est traitée comme non établie"))
                    continue
                # Le fragment est authentique — reste à savoir s'il dit **cette** qualité. Mesuré sur
                # un run réel : le modèle a cité trois fois « Une bougie allumée posée sur une table
                # basse est tombée sur le canapé » pour établir « caractère soudain de l'événement »,
                # « action subite de la chaleur » et « contact direct et immédiat avec un foyer ».
                # Un fragment vrai, trois qualités qu'il n'établit pas. La clause exige la qualité
                # « dans ces termes » : le code demande donc que le fragment emploie au moins un des
                # mots qui portent la qualité. C'est grossier, et c'est du code — un modèle ne peut
                # plus se corroborer lui-même en recopiant une liste dans l'autre.
                if _dit_la_qualite(q.qualite, q.fait_cite, min_chars=settings.qualite_mot_min_chars):
                    etablies.add(normalize(q.qualite))
                else:
                    step.checks.append(CheckResult(
                        name="fait_cite_hors_sujet", ok=False,
                        detail="le fragment cité pour une qualité n'en emploie aucun des mots : la "
                               "qualité est traitée comme non établie"))
            exigees = [q.strip() for q in a.qualites_exigees if q.strip()]
            non_etablies: list[str] = []
            for q in exigees:
                if normalize(q) not in etablies and q not in non_etablies:
                    non_etablies.append(q)
            if a.fait_requis_present:
                # B3, tour 3 : le modèle a coché « le fait exigé est présent » — c'est la seule porte
                # vers `oui`. Le texte de la clause est relu ici, et ce qu'il exige sans que le modèle
                # l'ait nommé est ajouté aux qualités **non établies** : deux listes vides ne peuvent
                # plus valoir « cette clause n'exige rien ».
                nommees = " ".join([*exigees, *(q.qualite for q in a.qualites_etablies),
                                    a.fait_manquant or ""])
                for libelle in _qualites_de_la_clause(clauses.get(a.claim_id, []), nommees=nommees,
                                                      place=settings.qualites_exigees_max):
                    if libelle not in exigees:
                        exigees.append(libelle)
                    if libelle not in non_etablies:
                        non_etablies.append(libelle)
                        step.checks.append(CheckResult(
                            name="qualite_de_la_clause_non_enumeree", ok=False,
                            # Le libellé est dérivé du texte de la clause. Il appartient au verdict
                            # et aux questions bornées adressées au client, pas à la trace technique
                            # consultable : celle-ci ne publie qu'un compte et le statut appliqué.
                            detail="1 qualité exigée par la clause citée n'a pas été énumérée "
                                   "(l'affirmation est traitée comme `humain`)"))
            applicabilites[a.claim_id] = ChampsApplicabilite(
                fait_requis_present=a.fait_requis_present, option_requise=a.option_requise,
                cp_requise=a.cp_requise, fait_manquant=(a.fait_manquant or "").strip() or None,
                qualites_exigees=exigees, qualites_non_etablies=non_etablies)
            if a.fait_requis_present and non_etablies:
                # Le modèle s'est contredit : il coche « le fait exigé est présent » après avoir nommé
                # ce que les faits déclarés n'établissent pas. Le code tranche du côté prudent (la
                # claim vaut `humain`) et la trace le dit, parce que c'est exactement le run réel qui a
                # motivé B3 — la qualité « subite » donnée pour acquise sur des circonstances.
                step.checks.append(CheckResult(
                    name="qualite_exigee_non_etablie", ok=False,
                    detail=f"{len(non_etablies)} qualité(s) exigée(s) par une clause citée ne sont pas "
                           "établies par les faits déclarés : l'affirmation est traitée comme `humain`"))
    return verdicts, couverture, soutiens, applicabilites
