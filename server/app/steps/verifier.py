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
from server.app.corpus.text import normalize
from server.app.domain.answer import (
    AnswerDraft,
    Claim,
    ClaimStatus,
    Quote,
    RejectedClaim,
    Verification,
    VerifiedClaim,
    VerifiedQuote,
)
from server.app.domain.document import Block
from server.app.domain.question import ParsedQuestion
from server.app.domain.retrieval import RetrievalResult
from server.app.domain.trace import CheckResult, StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import STEP_TIERS
from server.app.llm.prompting import load_prompt, untrusted

# Un `claim_id` produit par le modèle n'entre dans un motif que s'il ressemble à ce que le prompt
# demande (`c1`, `c2`, …) : court, sans espace ni balise. Tout le reste est nommé par sa position.
_CLAIM_ID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")

BLOC_INCONNU = "<bloc inconnu>"


class VerdictPertinence(BaseModel):
    claim_id: str
    pertinente: bool


class SortieVerifier(BaseModel):
    """Sortie de l'appel `micro` : un booléen par claim, rien d'autre (AD-4).

    Aucun champ de texte libre : le modèle ne peut pas glisser de motif dans la trace, et il ne peut
    pas non plus « expliquer » un verdict que le code ne lui a pas demandé.
    """

    verdicts: list[VerdictPertinence]


def _ligne_spans(block: Block) -> list[tuple[int, int, str]]:
    """Position de chaque ligne du bloc dans son `text_norm` : [(début, fin, line_id)].

    Les lignes sont cherchées **dans l'ordre**, à partir de la fin de la précédente : un bloc PDF est
    la concaténation de ses lignes, donc leurs formes normalisées s'y suivent. Une ligne introuvable
    (la règle de césure `-\\n` de `normalize()` peut souder deux lignes en un mot) est simplement
    sautée : mieux vaut un `line_id` manquant qu'un surlignage faux.
    """
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    for line in block.lines:
        forme = normalize(line.text)
        if not forme:
            continue
        i = block.text_norm.find(forme, cursor)
        if i < 0:
            continue
        spans.append((i, i + len(forme), line.line_id))
        cursor = i + len(forme)
    return spans


class _Controle:
    """Résultat du contrôle d'une quote : retrouvée (avec ses offsets) ou rejetée avec son motif."""

    def __init__(self, kind: str, motif: str, quote: VerifiedQuote | None = None) -> None:
        self.kind = kind  # "" | "non_retrouvee" | "ambigue"
        self.motif = motif
        self.quote = quote


def _controler_quote(block_id: str, quote: str, *, corpus: Any, index: Any,
                     settings: Settings) -> _Controle:
    """AD-3, dans l'ordre de son texte : existence, kind, longueur, inclusion, non-ambiguïté."""
    try:
        doc_id = index.doc_of(block_id)
    except KeyError:
        # Le `block_id` vient du modèle : il n'est pas recopié dans le motif (AD-15).
        return _Controle("non_retrouvee", f"citation rattachée à un bloc qui n'existe pas ({BLOC_INCONNU})")
    document = corpus.documents[doc_id]
    block = document.block(block_id)  # texte **toujours relu depuis le corpus**
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
    blocs_porteurs = [b.block_id for b in document.blocks if forme in b.text_norm]
    if len(blocs_porteurs) > 1:
        return _Controle("ambigue", f"citation ambiguë : le même passage figure dans {len(blocs_porteurs)} blocs "
                                    f"du document (dont {block_id}) — étends la citation pour la rendre unique")
    end = start + len(forme)
    line_ids = [lid for (a, b, lid) in _ligne_spans(block) if a < end and b > start]
    return _Controle("", "", VerifiedQuote(block_id=block_id, quote=quote, start=start, end=end,
                                           line_ids=line_ids))


def _nom_de_claim(claim: Claim, position: int) -> str:
    """Comment nommer la claim dans un motif : son `claim_id` s'il est plausible, sa position sinon."""
    return claim.claim_id if _CLAIM_ID.match(claim.claim_id) else f"claim n° {position}"


def _motif_de_relance(rejetees: list[RejectedClaim], noms: dict[str, str]) -> str:
    """Motif composé par **notre** code, transmis tel quel à la relance de *rédiger* (AD-3).

    Il est délimité par `untrusted()` dans *rédiger* : ce texte mêle nos phrases à des `block_id`, et
    il ne devient jamais une consigne de confiance.
    """
    lignes = [f"- {noms[claim.claim_id]} : {claim.motif}" for claim in rejetees]
    return ("Le contrôle des citations a rejeté les affirmations suivantes. Corrige-les précisément "
            "(ou remplace-les par ce que les blocs fournis soutiennent vraiment) :\n" + "\n".join(lignes))


async def verifier(draft: AnswerDraft, *, parsed: ParsedQuestion, retrieval: RetrievalResult,
                   corpus: Any, index: Any, client: LlmClient, budget: RequestBudget,
                   settings: Settings) -> tuple[Verification, StepTrace]:
    t0 = time.monotonic()
    step = StepTrace(name="verifier", tier=STEP_TIERS["verifier"])

    # `edition` (AD-4) : celle du document cité, affichée « édition … — actualité non vérifiée ».
    # Une réponse du guide porte sur un seul document (le pipeline passe un `doc_id`), et elle est
    # connue avant tout contrôle — y compris pour une claim dont aucune citation ne survit.
    edition = corpus.documents[index.doc_of(retrieval.blocs[0].block_id)].edition if retrieval.blocs else ""

    retrouvees: list[tuple[Claim, list[VerifiedQuote]]] = []
    rejetees: list[RejectedClaim] = []
    noms: dict[str, str] = {}  # `claim_id` → nom sûr pour les motifs (les `claim_id` sont uniques, AD-3)
    for position, claim in enumerate(draft.claims, start=1):
        noms[claim.claim_id] = _nom_de_claim(claim, position)
        du_draft = [Quote(block_id=q.block_id, quote=q.quote) for q in claim.quotes]
        controles = [_controler_quote(q.block_id, q.quote, corpus=corpus, index=index, settings=settings)
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
        retrouvees.append((claim, [c.quote for c in controles if c.quote is not None]))

    # AD-4 : **un seul** appel `micro` groupé, borné par `verifier_max_claims`. Au-delà, les claims
    # excédentaires ne sont pas évaluées — jamais devinées (`draft_max_claims` fait que le cas ne se
    # produit pas sur le corpus servi, la borne est une ceinture).
    evaluees = retrouvees[: settings.verifier_max_claims]
    excedentaires = retrouvees[settings.verifier_max_claims:]
    verdicts: dict[str, bool] = {}
    if evaluees:
        verdicts = await _pertinence(evaluees, parsed=parsed, corpus=corpus, index=index, client=client,
                                     budget=budget, settings=settings, step=step)

    claims: list[VerifiedClaim] = []
    manquants = 0
    for claim, quotes in evaluees:
        pertinente = verdicts.get(claim.claim_id)
        if pertinente is None:
            manquants += 1
        status = ClaimStatus(retrouvee=True, pertinente=pertinente, edition=edition)
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
        rejetees.append(RejectedClaim(
            claim_id=claim.claim_id, text=claim.text,
            quotes=[Quote(block_id=q.block_id, quote=q.quote) for q in quotes], status=status,
            rejection_kind="non_pertinente", motif=motif))
    for claim, quotes in excedentaires:
        rejetees.append(RejectedClaim(
            claim_id=claim.claim_id, text=claim.text,
            quotes=[Quote(block_id=q.block_id, quote=q.quote) for q in quotes],
            status=ClaimStatus(retrouvee=True, pertinente=None, edition=edition),
            rejection_kind="non_pertinente",
            motif=f"affirmation non évaluée : le contrôle de pertinence est borné à "
                  f"{settings.verifier_max_claims} affirmations par réponse"))

    if manquants:
        step.checks.append(CheckResult(
            name="pertinence_incomplete", ok=False,
            detail=f"{manquants} affirmation(s) sur {len(evaluees)} sans verdict de pertinence : écartées"))

    # AD-4 : `found` et `complete` sont calculés **ici**, jamais produits par le modèle.
    found = bool(claims)
    unknown = [s.text for s in draft.segments if s.kind == "limite" and s.text.strip()]
    cites = {q.block_id for c in claims for q in c.quotes}
    renvois_ouverts = any(corpus.documents[index.doc_of(b)].block(b).unresolved_refs for b in cites)
    # « Toutes les facettes couvertes » (AD-4) n'est pas mesurable en 1.5 : `unknown == []` en est
    # l'approximation conservatrice — elle ne surestime jamais la complétude.
    complete = found and not retrieval.truncated and not unknown and not renvois_ouverts

    verification = Verification(
        segments=list(draft.segments), claims=claims, rejected_claims=rejetees, found=found,
        complete=complete, unknown=unknown,
        motif=_motif_de_relance(rejetees, noms) if rejetees else None,
    )
    step.checks.append(CheckResult(
        name="citations", ok=not rejetees,
        detail=f"{len(claims)} affirmation(s) retenue(s), {len(rejetees)} rejetée(s) sur {len(draft.claims)}"))
    step.ms = int((time.monotonic() - t0) * 1000)
    return verification, step


async def _pertinence(evaluees: list[tuple[Claim, list[VerifiedQuote]]], *, parsed: ParsedQuestion,
                      corpus: Any, index: Any, client: LlmClient, budget: RequestBudget,
                      settings: Settings, step: StepTrace) -> dict[str, bool]:
    """L'unique appel `micro` groupé : un booléen par `claim_id`, verdicts inconnus ignorés."""
    prefix = load_prompt("commun") + "\n\n" + load_prompt("verifier")
    parts = [untrusted("question", parsed.question_resolue)]
    for claim, quotes in evaluees:
        # Le passage soumis est **relu dans le corpus** : `text_norm[start:end]`, l'occurrence même
        # dont l'inclusion a été prouvée — jamais la chaîne du draft. Une citation « écho » ne peut
        # donc pas être jugée pertinente sur sa propre invention (AD-3). La forme normalisée suffit
        # à juger un sens ; elle ne sert pas à l'affichage (l'UI relit le bloc par `block_id` et
        # offsets).
        citations = []
        for q in quotes:
            block = corpus.documents[index.doc_of(q.block_id)].block(q.block_id)
            citations.append({"block_id": q.block_id, "passage": block.text_norm[q.start:q.end]})
        parts.append(untrusted("claim", json.dumps(
            {"claim_id": claim.claim_id, "affirmation": claim.text, "citations": citations},
            ensure_ascii=False)))
    content = "\n\n".join(parts)
    result = await client.parse(tier=STEP_TIERS["verifier"], system_prefix=prefix,
                                messages=[{"role": "user", "content": content}],
                                output_model=SortieVerifier, budget=budget, step=step,
                                max_tokens=settings.verifier_max_tokens)
    attendus = {claim.claim_id for claim, _ in evaluees}
    verdicts: dict[str, bool] = {}
    for v in result.parsed.verdicts:
        if v.claim_id in attendus:  # un identifiant inventé ne décide de rien
            verdicts.setdefault(v.claim_id, v.pertinente)
    return verdicts
