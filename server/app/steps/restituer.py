"""AD-3 / AD-4 — *restituer* : la seule étape qui fabrique l'`Answer`, sans jamais appeler un modèle.

Deux issues, et rien entre les deux :

- **une réponse** — `Answer.texte` est rendu **déterministement** depuis les segments survivants de
  l'ébauche (AD-3 : « un segment factuel dont toutes les claims sont rejetées est retiré ; aucun
  texte libre parallèle n'est affiché ») ; `Answer.segments[]` est conservé tel quel pour que l'UI
  place chaque citation sous la phrase qu'elle soutient (FR3) ;
- **un refus** — un unique segment `limite` porteur d'une phrase composée **par le code**, et
  l'`AbsenceProof` qui dit ce qui a été cherché. C'est la **seule** phrase d'absence que
  l'utilisateur lise : une limite rédigée par le modèle (« le guide ne dit rien de X ») n'est
  soutenue par aucun passage — aucune citation ne prouve une absence — et *vérifier* l'a déjà
  renvoyée vers `Answer.unknown[]` plutôt que vers le texte affiché (revue Codex 1.5, tour 3, B1). Une phrase par `AbsenceProof.kind` : l'utilisateur
  doit pouvoir distinguer « hors périmètre » de « rien trouvé » et de « rien qui tienne la
  vérification » — ce sont trois situations différentes, et une seule d'entre elles vaut la peine de
  reformuler sa question.

Les phrases composées par le code sont rendues depuis des registres `fr/en/de/pt`. Les citations,
elles, restent le texte brut du corpus français. `lang_fallback` ne signale donc plus un mélange de
langues : il est réservé au repli d'une détection illisible ou non servie.

`restituer → aucun tier` (AD-9) : le `StepTrace` porte donc `tier=None` et `calls=[]`.
"""

from __future__ import annotations

import time
from typing import get_args

from server.app.domain.answer import (
    LACUNES_PLURALISEES,
    AbsenceKind,
    AbsenceProof,
    Answer,
    AnswerSegment,
    Lacune,
    LacuneKind,
    Verification,
)
from server.app.domain.langue import LANGUES_SERVIES
from server.app.domain.question import QuestionScope
from server.app.domain.trace import CheckResult, StepTrace
from server.app.domain.verdict import Verdict
from server.app.llm.models import STEP_TIERS

# Une phrase par `AbsenceProof.kind` (AD-4). Elles ne nomment jamais un terme cherché ni un extrait :
# le détail chiffré est dans `Answer.reason`, que le front rend séparément (« N variantes essayées,
# M passages parcourus »), et les termes eux-mêmes ne sont jamais logués (AD-10).
PHRASES_DE_REFUS: dict[str, dict[str, str]] = {
    "fr": {
        "hors_perimetre": "Cette question sort de ce que couvre le guide : je n'y réponds pas plutôt que d'y répondre à côté.",
        "zero_hit": "Je n'ai trouvé aucun passage du guide qui traite de cette question. Je préfère ne rien affirmer que d'avancer une réponse sans source.",
        "claims_rejetes": ("Je n'ai gardé aucune affirmation : les passages cités ne soutenaient pas "
                            "la réponse, ou ne répondaient pas à la question. Rien ne vous est montré "
                            "sans une source vérifiée."),
        "clarification_requise": "Je n'ai pas pu déterminer à quoi votre question fait référence ; précisez-la et je chercherai.",
    },
    "en": {
        "hors_perimetre": "This question falls outside what the guide covers. I would rather not answer than give you an off-topic answer.",
        "zero_hit": "I found no passage in the guide that addresses this question. I would rather make no claim than give an unsourced answer.",
        "claims_rejetes": "I kept no claim: the cited passages did not support the answer or did not address the question. Nothing is shown without a verified source.",
        "clarification_requise": "I could not determine what your question refers to. Please clarify it and I will search again.",
    },
    "de": {
        "hors_perimetre": "Diese Frage liegt außerhalb dessen, was der Ratgeber abdeckt. Ich antworte lieber nicht, als an der Frage vorbeizugehen.",
        "zero_hit": "Ich habe im Ratgeber keine Passage gefunden, die diese Frage behandelt. Ich behaupte lieber nichts, als eine Antwort ohne Quelle zu geben.",
        "claims_rejetes": ("Ich habe keine Aussage beibehalten: Die zitierten Passagen stützten die "
                            "Antwort nicht oder beantworteten die Frage nicht. Ohne geprüfte Quelle "
                            "wird nichts angezeigt."),
        "clarification_requise": "Ich konnte nicht feststellen, worauf sich Ihre Frage bezieht. Bitte präzisieren Sie sie, dann suche ich erneut.",
    },
    "pt": {
        "hors_perimetre": "Esta pergunta está fora do âmbito do guia. Prefiro não responder do que dar uma resposta que não corresponde à pergunta.",
        "zero_hit": "Não encontrei nenhuma passagem do guia que trate desta pergunta. Prefiro não afirmar nada do que dar uma resposta sem fonte.",
        "claims_rejetes": "Não mantive nenhuma afirmação: as passagens citadas não sustentavam a resposta ou não respondiam à pergunta. Nada é mostrado sem uma fonte verificada.",
        "clarification_requise": "Não consegui determinar a que se refere a sua pergunta. Esclareça-a e voltarei a pesquisar.",
    },
}

# Les mêmes quatre situations, dites pour un dossier de sinistre (story 1.8, revue). Un refus servait
# jusqu'ici les phrases du guide — « Cette question sort de ce que couvre **le guide** », « aucun
# passage **du guide** » — à un gestionnaire qui vient de décrire un sinistre sur un contrat AXA :
# le texte affiché nommait un document que la requête ne touche pas. Les clés sont exactement celles
# de `PHRASES_DE_REFUS` (`REGISTRES` le vérifie au chargement) : un registre n'ajoute jamais un kind,
# il traduit les mêmes.
PHRASES_DE_REFUS_SINISTRE: dict[str, dict[str, str]] = {
    "fr": {
        "hors_perimetre": "Cette demande ne relève pas de ce que couvre un contrat d'assurance habitation : je ne la traite pas plutôt que de la rapprocher d'une clause qui ne la vise pas.",
        "zero_hit": "Je n'ai trouvé aucune clause du contrat qui traite du sinistre décrit. Je préfère ne rien conclure que d'opposer au dossier un passage qui ne le concerne pas.",
        "claims_rejetes": ("Je n'ai retenu aucune clause : les passages cités ne soutenaient pas ce "
                            "qui en était dit, ou ne répondaient pas au sinistre décrit. Aucune clause "
                            "ne vous est montrée sans vérification."),
        "clarification_requise": "Je n'ai pas pu déterminer sur quoi porte la demande ; précisez-la et je chercherai dans le contrat.",
    },
    "en": {
        "hors_perimetre": "This request falls outside what a home insurance policy covers. I would rather not process it than match it to an unrelated clause.",
        "zero_hit": "I found no policy clause that addresses the reported loss. I would rather draw no conclusion than apply an unrelated passage to the case.",
        "claims_rejetes": "I kept no clause: the cited passages did not support what was said about them or did not address the reported loss. No clause is shown without verification.",
        "clarification_requise": "I could not determine what the request concerns. Please clarify it and I will search the policy.",
    },
    "de": {
        "hors_perimetre": "Diese Anfrage fällt nicht in den Deckungsbereich einer Hausratversicherung. Ich bearbeite sie lieber nicht, als sie einer unpassenden Klausel zuzuordnen.",
        "zero_hit": "Ich habe keine Vertragsklausel gefunden, die den beschriebenen Schaden behandelt. Ich ziehe lieber keinen Schluss, als eine unpassende Passage auf den Fall anzuwenden.",
        "claims_rejetes": ("Ich habe keine Klausel beibehalten: Die zitierten Passagen stützten die "
                            "Aussagen nicht oder betrafen den beschriebenen Schaden nicht. Ohne "
                            "Prüfung wird keine Klausel angezeigt."),
        "clarification_requise": "Ich konnte nicht feststellen, worum es bei der Anfrage geht. Bitte präzisieren Sie sie, dann suche ich im Vertrag.",
    },
    "pt": {
        "hors_perimetre": "Este pedido não está abrangido pelo âmbito de um contrato de seguro de habitação. Prefiro não o tratar do que associá-lo a uma cláusula que não se aplica.",
        "zero_hit": ("Não encontrei nenhuma cláusula do contrato que trate do sinistro descrito. "
                     "Prefiro não tirar conclusões do que aplicar ao processo uma passagem que não "
                     "lhe diz respeito."),
        "claims_rejetes": ("Não mantive nenhuma cláusula: as passagens citadas não sustentavam o que "
                            "delas se dizia ou não respondiam ao sinistro descrito. Nenhuma cláusula "
                            "é mostrada sem verificação."),
        "clarification_requise": "Não consegui determinar o objeto do pedido. Esclareça-o e pesquisarei no contrato.",
    },
}

# Projection des causes typées que le code peut constater. Les deux patrons pluralisés portent un
# couple singulier/pluriel ; les autres portent une chaîne unique. Le garde de chargement ci-dessous
# relie ce registre au `LacuneKind` du domaine et aux quatre langues servies.
PHRASES_DE_LACUNE: dict[str, dict[str, str | tuple[str, str]]] = {
    "fr": {
        "lecture_bornee": "Je n'ai pas pu lire tout ce qui pouvait concerner votre question : ma lecture a été bornée, et des passages sont restés fermés.",
        "sans_decoupage": "Je n'ai pas pu découper votre question en sous-questions : je ne peux donc pas garantir de l'avoir traitée en entier.",
        "facettes_sans_reponse": ("Il reste {n} sous-question sans réponse dans ce que vous m'avez demandé.", "Il reste {n} sous-questions sans réponse dans ce que vous m'avez demandé."),
        "renvoi_non_resolu": "Un passage que je cite renvoie à un autre passage que je n'ai pas pu retrouver.",
        "contradiction_non_resolue": "Deux passages que je cite se contredisent sans que le contrat les départage.",
        "phrases_ecartees": (
            "J'ai retiré {n} phrase de ma réponse : les passages joints ne la soutenaient pas.",
            "J'ai retiré {n} phrases de ma réponse : les passages joints ne les soutenaient pas.",
        ),
        "segments_retires": "J'ai retiré de ma réponse ce que je ne pouvais pas sourcer : les affirmations qui la portaient n'ont pas passé la vérification.",
        "relance_abandonnee": "Je n'ai pas pu reprendre ma réponse pour l'améliorer : je la donne telle que je l'avais vérifiée du premier coup.",
    },
    "en": {
        "lecture_bornee": "I could not read everything that might concern your question: my reading was limited, and some passages remained unopened.",
        "sans_decoupage": "I could not break your question into sub-questions, so I cannot guarantee that I addressed it in full.",
        "facettes_sans_reponse": ("There is still {n} unanswered sub-question in what you asked me.", "There are still {n} unanswered sub-questions in what you asked me."),
        "renvoi_non_resolu": "A passage I cite refers to another passage that I could not retrieve.",
        "contradiction_non_resolue": "Two passages I cite contradict each other, and the contract does not resolve the conflict.",
        "phrases_ecartees": (
            "I removed {n} sentence from my answer because the attached passages did not support it.",
            "I removed {n} sentences from my answer because the attached passages did not support them.",
        ),
        "segments_retires": "I removed what I could not source from my answer: the supporting claims did not pass verification.",
        "relance_abandonnee": "I could not revise my answer to improve it, so I am giving it as it was verified the first time.",
    },
    "de": {
        "lecture_bornee": "Ich konnte nicht alles lesen, was Ihre Frage betreffen könnte: Meine Lektüre war begrenzt, und einige Passagen blieben ungeöffnet.",
        "sans_decoupage": "Ich konnte Ihre Frage nicht in Teilfragen gliedern und kann daher nicht garantieren, sie vollständig behandelt zu haben.",
        "facettes_sans_reponse": ("In Ihrer Frage bleibt noch {n} Teilfrage unbeantwortet.", "In Ihrer Frage bleiben noch {n} Teilfragen unbeantwortet."),
        "renvoi_non_resolu": "Eine von mir zitierte Passage verweist auf eine andere Passage, die ich nicht finden konnte.",
        "contradiction_non_resolue": "Zwei von mir zitierte Passagen widersprechen sich, ohne dass der Vertrag den Widerspruch auflöst.",
        "phrases_ecartees": (
            "Ich habe {n} Satz aus meiner Antwort entfernt, weil die beigefügten Passagen ihn nicht stützten.",
            "Ich habe {n} Sätze aus meiner Antwort entfernt, weil die beigefügten Passagen sie nicht stützten.",
        ),
        "segments_retires": "Ich habe aus meiner Antwort entfernt, was ich nicht belegen konnte: Die zugehörigen Aussagen haben die Prüfung nicht bestanden.",
        "relance_abandonnee": "Ich konnte meine Antwort nicht überarbeiten, um sie zu verbessern, und gebe sie daher in der zuerst geprüften Fassung wieder.",
    },
    "pt": {
        "lecture_bornee": "Não consegui ler tudo o que poderia dizer respeito à sua pergunta: a minha leitura foi limitada e algumas passagens ficaram por abrir.",
        "sans_decoupage": "Não consegui dividir a sua pergunta em subperguntas, pelo que não posso garantir que a tratei por completo.",
        "facettes_sans_reponse": ("Falta responder a {n} subpergunta do que me perguntou.", "Falta responder a {n} subperguntas do que me perguntou."),
        "renvoi_non_resolu": "Uma passagem que cito remete para outra passagem que não consegui encontrar.",
        "contradiction_non_resolue": "Duas passagens que cito contradizem-se, sem que o contrato resolva o conflito.",
        "phrases_ecartees": (
            "Retirei {n} frase da minha resposta porque as passagens associadas não a sustentavam.",
            "Retirei {n} frases da minha resposta porque as passagens associadas não as sustentavam.",
        ),
        "segments_retires": "Retirei da minha resposta o que não consegui fundamentar: as afirmações correspondentes não passaram na verificação.",
        "relance_abandonnee": "Não consegui rever a minha resposta para a melhorar, por isso apresento-a tal como foi verificada da primeira vez.",
    },
}

REGISTRE_GUIDE = "guide"
REGISTRE_SINISTRE = "sinistre"
# Le registre choisit **le vocabulaire du refus**, jamais sa logique : mêmes kinds, mêmes règles, même
# `AbsenceProof`. Le défaut est le guide, à l'octet près — ses fixtures et ses tests en dépendent.
REGISTRES: dict[str, dict[str, dict[str, str]]] = {
    REGISTRE_GUIDE: PHRASES_DE_REFUS,
    REGISTRE_SINISTRE: PHRASES_DE_REFUS_SINISTRE,
}

# Une réponse sinistre doit garder lisibles les repères structurés que *comprendre* a déjà extraits
# et que le pipeline a bornés, même lorsque *rédiger* va droit aux clauses. Ce ne sont pas des
# affirmations sur le contrat : un segment `transition` sans claim donne cause, événement et moment
# dans cet ordre. La description HTTP brute (jusqu'à 2 000 caractères) n'est jamais réinjectée.
REPERES_DECLARATION: dict[str, dict[str, str]] = {
    "fr": {"prefixe": "Faits compris", "cause": "cause", "evenement": "puis événement",
           "moment": "moment"},
    "en": {"prefixe": "Understood facts", "cause": "cause", "evenement": "then event",
           "moment": "time"},
    "de": {"prefixe": "Verstandene Fakten", "cause": "Ursache", "evenement": "dann Ereignis",
           "moment": "Zeitpunkt"},
    "pt": {"prefixe": "Factos compreendidos", "cause": "causa", "evenement": "depois evento",
           "moment": "momento"},
}
# Invariant de chargement : aucun registre n'invente ni n'oublie un kind d'`AbsenceProof`. Un `KeyError`
# à la première phrase de refus servie serait un 500 sur le chemin le plus exposé (AD-16).
_LANGUES_MANQUANTES = {
    nom: set(LANGUES_SERVIES) ^ set(phrases) for nom, phrases in REGISTRES.items()
}
_KINDS_REFUS = set(get_args(AbsenceKind))
_MANQUANTS = {
    f"{nom}.{lang}": _KINDS_REFUS ^ set(phrases[lang])
    for nom, phrases in REGISTRES.items()
    for lang in set(LANGUES_SERVIES) & set(phrases)
}
_LANGUES_LACUNES_MANQUANTES = set(LANGUES_SERVIES) ^ set(PHRASES_DE_LACUNE)
_LANGUES_REPERES_MANQUANTES = set(LANGUES_SERVIES) ^ set(REPERES_DECLARATION)
_CHAMPS_REPERES = {"prefixe", "cause", "evenement", "moment"}
_CHAMPS_REPERES_MANQUANTS = {
    lang: _CHAMPS_REPERES ^ set(REPERES_DECLARATION[lang])
    for lang in set(LANGUES_SERVIES) & set(REPERES_DECLARATION)
}
_KINDS_LACUNE = set(get_args(LacuneKind))
_LACUNES_MANQUANTES = {
    lang: _KINDS_LACUNE ^ set(PHRASES_DE_LACUNE[lang])
    for lang in set(LANGUES_SERVIES) & set(PHRASES_DE_LACUNE)
}
_FORMES_LACUNES_INVALIDES = {
    f"lacunes.{lang}.{kind}"
    for lang in set(LANGUES_SERVIES) & set(PHRASES_DE_LACUNE)
    for kind, patron in PHRASES_DE_LACUNE[lang].items()
    if ((kind in LACUNES_PLURALISEES
         and (not isinstance(patron, tuple) or len(patron) != 2
              or any(not forme.strip() for forme in patron)))
        or (kind not in LACUNES_PLURALISEES
            and (not isinstance(patron, str) or not patron.strip())))
}
if (any(_LANGUES_MANQUANTES.values()) or _LANGUES_LACUNES_MANQUANTES or _LANGUES_REPERES_MANQUANTES
        or any(_CHAMPS_REPERES_MANQUANTS.values())
        or any(_MANQUANTS.values()) or any(_LACUNES_MANQUANTES.values())
        or _FORMES_LACUNES_INVALIDES):
    differences = {**_LANGUES_MANQUANTES, **_MANQUANTS, **_LACUNES_MANQUANTES}
    if _LANGUES_LACUNES_MANQUANTES:
        differences["lacunes.langues"] = _LANGUES_LACUNES_MANQUANTES
    if _LANGUES_REPERES_MANQUANTES:
        differences["reperes.langues"] = _LANGUES_REPERES_MANQUANTES
    differences.update({f"reperes.{lang}": champs
                        for lang, champs in _CHAMPS_REPERES_MANQUANTS.items() if champs})
    if _FORMES_LACUNES_INVALIDES:
        differences["lacunes.formes"] = _FORMES_LACUNES_INVALIDES
    raise RuntimeError(f"registre(s) de refus incomplet(s) : "
                       f"{ {n: sorted(k) for n, k in differences.items() if k} }")


def _texte(segments: list[AnswerSegment]) -> str:
    """Rendu déterministe : les textes des segments survivants, dans l'ordre, séparés par une espace."""
    return " ".join(s.text.strip() for s in segments if s.text.strip())


def _reperes_declares(scope: QuestionScope | None, language: str) -> AnswerSegment | None:
    """Projette cause, événement puis moment sans relire la description brute.

    Les champs viennent de `QuestionScope` et sont déjà bornés par le pipeline. Leur présence est le
    seul signal structurel : aucun mot du cas A9 n'est recherché. Même un simple moment daté reste
    donc une transition courte au lieu de rouvrir jusqu'à 2 000 caractères de déclaration.
    """
    if scope is None:
        return None
    registre = REPERES_DECLARATION[language]
    valeurs = [(registre[nom], (getattr(scope, nom) or "").strip())
               for nom in ("cause", "evenement", "moment")]
    morceaux = [f"{libelle} : {valeur}" for libelle, valeur in valeurs if valeur]
    if not morceaux:
        return None
    return AnswerSegment(text=f"{registre['prefixe']} — {' ; '.join(morceaux)}.", kind="transition")


def _rendre_lacune(lacune: Lacune, language: str) -> str:
    """Projette une cause typée dans la langue décidée par *comprendre*."""
    patron = PHRASES_DE_LACUNE[language][lacune.kind]
    if lacune.kind in LACUNES_PLURALISEES:
        assert isinstance(patron, tuple)  # garanti par l'invariant de chargement
        patron = patron[0 if lacune.n == 1 else 1]
    else:
        assert isinstance(patron, str)  # garanti par l'invariant de chargement
    return patron.format(n=lacune.n)


def _fusionner_inconnues(declarees: list[str], lacunes: list[Lacune], language: str) -> list[str]:
    """Fusionne les deux canaux dans leur ordre, sans perdre ni dupliquer une lacune du code."""
    inconnues = list(declarees)
    for lacune in lacunes:
        rendue = _rendre_lacune(lacune, language)
        if rendue not in inconnues:
            inconnues.append(rendue)
    return inconnues


def restituer(*, language: str, lang_fallback: bool = False,
              verification: Verification | None = None,
              reason: AbsenceProof | None = None, clarification: str | None = None,
              verdict: Verdict | None = None, faits_compris: QuestionScope | None = None,
              registre: str = REGISTRE_GUIDE) -> tuple[Answer, StepTrace]:
    """`Answer` + son `StepTrace`. `reason` est obligatoire dès que la vérification n'a rien retenu.

    `registre` (story 1.8, revue) choisit le **vocabulaire** de la phrase de refus, et rien d'autre :
    le guide parle du guide, le sinistre parle du contrat. Le paramètre est explicite plutôt que
    déduit de la présence d'un verdict — deux appelants, deux registres, aucune inférence — et son
    défaut laisse le guide inchangé à l'octet près.

    `verdict` (story 1.8) : *restituer* **recopie**, il ne calcule pas. AD-4 fait de `Verdict` un
    champ de l'unique `Answer` et AD-6 confie la table à *vérifier* ; ce qui arrive ici est donc soit
    `Verification.verdict`, soit — quand le pipeline sinistre court-circuite *vérifier* (question non
    autonome, aucun bloc citable) — le `ne_tranche_pas` que le pipeline a composé. AD-16 : « aucun
    repli pour le sinistre » se lit aussi dans l'autre sens — un refus sinistre porte un verdict,
    jamais rien, sans quoi le front n'aurait qu'une absence à afficher. Les deux sources sont
    exclusives : un `Verification` qui porte déjà un verdict n'en admet pas un second.

    `faits_compris` (story 1.9, D4) : *restituer* **recopie** ici aussi. C'est
    `ParsedQuestion.scope`, borné par l'appelant — le pipeline est le seul à voir `settings` et la
    question comprise —, et il est recopié tel quel sur **les deux** issues. Sur un refus surtout :
    « je n'ai trouvé aucune clause » se lit tout autrement quand on voit à côté ce que le système a
    cru comprendre du sinistre, et c'est le seul écran où l'utilisateur peut constater qu'il a été
    mal compris. `None` en guide, comme le verdict.
    """
    t0 = time.monotonic()
    step = StepTrace(name="restituer", tier=STEP_TIERS["restituer"])
    trouve = verification is not None and verification.found
    if verdict is not None and verification is not None and verification.verdict is not None:
        raise ValueError("restituer reçoit deux verdicts (celui de la vérification et un second) : "
                         "un seul est calculé par requête, par *vérifier*")
    if verification is not None and verification.verdict is not None:
        verdict = verification.verdict

    if not trouve and reason is None:
        # AD-16 : un `Answer` sans réponse **et** sans preuve d'absence serait un dégradé silencieux —
        # le domaine le refuserait de toute façon, autant le dire à l'appelant avec ses mots.
        raise ValueError("restituer sans réponse retenue exige une AbsenceProof (reason)")
    if trouve and reason is not None:
        raise ValueError("restituer avec une réponse retenue n'admet pas d'AbsenceProof (reason)")

    if registre not in REGISTRES:
        raise ValueError(f"registre de refus inconnu : {registre!r} "
                         f"(connus : {', '.join(sorted(REGISTRES))})")
    if language not in LANGUES_SERVIES:
        raise ValueError(f"langue de restitution inconnue : {language!r}")

    if not trouve:
        assert reason is not None  # garanti par le contrôle ci-dessus (mypy/lecture)
        phrase = REGISTRES[registre][language][reason.kind]
        answer = Answer(
            found=False, complete=False, lang=language, lang_fallback=lang_fallback, texte=phrase,
            segments=[AnswerSegment(text=phrase, kind="limite")],
            rejected_claims=list(verification.rejected_claims) if verification is not None else [],
            reason=reason, verdict=verdict, faits_compris=faits_compris,
            # L'`AbsenceProof` explique le refus ; les lacunes constatées sur un chemin producteur
            # différent restent dues. Les perdre ici serait un dégradé silencieux.
            unknown=_fusionner_inconnues(list(verification.unknown), list(verification.lacunes),
                                         language) if verification is not None else [],
            clarification=clarification,
        )
        step.checks.append(CheckResult(name="refus", ok=True, detail=reason.kind))
        step.ms = int((time.monotonic() - t0) * 1000)
        return answer, step

    assert verification is not None
    survivantes = {c.claim_id for c in verification.claims}
    # AD-3 : un segment `factuel` dont toutes les claims sont rejetées est **retiré**. Les
    # `transition` ne portent aucune affirmation à soutenir — mais ils portent du texte, et
    # *vérifier* a déjà retiré de `Verification.segments` toute phrase, de n'importe quel kind, qui
    # avance plus que ses passages (revue Codex 1.5, tour 2, B1), puis les `limite` en entier, qui
    # affirment une absence qu'aucun passage ne prouve (tour 3, B1). Ce qui arrive ici est donc déjà
    # le texte contrôlé : *restituer* n'applique plus que la règle mécanique d'AD-3.
    segments = [s for s in verification.segments
                if s.kind != "factuel" or (set(s.claim_ids) & survivantes)]
    # Seconde ceinture sur l'assertion d'absence (revue Codex 1.5, tour 3, B1). *vérifier* a déjà
    # sorti les `limite` de `Verification.segments` ; *restituer* est le **seul** endroit où un
    # `Answer` se fabrique (guide aujourd'hui, sinistre en 1.8, API en 1.6), et c'est donc ici que
    # l'invariant doit tenir quel que soit l'appelant : rien de ce qui affirme une absence n'entre
    # dans le texte affiché. La lacune n'est pas perdue pour autant — elle rejoint `unknown[]`.
    retires = len(verification.segments) - len(segments)  # compté avant, il ne nomme que la règle d'AD-3
    limites = [s.text.strip() for s in segments if s.kind == "limite" and s.text.strip()]
    segments = [s for s in segments if s.kind != "limite"]
    if limites:
        step.checks.append(CheckResult(
            name="limites_non_affichees", ok=False,
            detail=f"{len(limites)} phrase(s) d'absence retirée(s) du texte affiché : aucune citation "
                   "ne prouve une absence — elles restent dans unknown[]"))
    # Les `claim_ids` d'un segment conservé sont ramenés aux claims survivantes : l'UI place les
    # citations sous la phrase, elle ne doit pas chercher une claim qui n'est plus dans `claims[]`.
    segments = [AnswerSegment(text=s.text, kind=s.kind,
                              claim_ids=[cid for cid in s.claim_ids if cid in survivantes])
                for s in segments]
    reperes = (_reperes_declares(faits_compris, language)
               if registre == REGISTRE_SINISTRE else None)
    if reperes is not None:
        # Ajout après *vérifier* : ce segment ne prétend rien sur le contrat et ne doit donc pas être
        # évalué contre une quote. Il projette seulement les champs bornés que l'unique `Answer`
        # publie déjà dans `faits_compris`.
        segments = [reperes, *segments]
    texte = _texte(segments)
    if not any(s.kind == "factuel" and s.text.strip() for s in segments):
        # AD-16, « réponse vide présentée comme réponse » : une réponse dont plus aucune phrase
        # n'affirme quoi que ce soit n'est pas une réponse. *vérifier* garantit l'inverse — il écarte
        # (`non_citee`) toute claim qu'aucun segment factuel affiché ne cite, donc `found=True` implique
        # un segment factuel affiché. Y arriver quand même est une incohérence d'appel, au même titre
        # que les deux ci-dessus, et elle se dit avec les mêmes mots (revue Codex 1.5, B6).
        raise ValueError("restituer avec found=True exige au moins un segment factuel survivant "
                         "portant du texte (une réponse vide n'est pas une réponse)")
    if retires:
        step.checks.append(CheckResult(name="segments_retires", ok=False,
                                       detail=f"{retires} segment(s) factuel(s) sans claim survivante retiré(s)"))
    # Ce que le code constate qu'il manque, à cet étage : les lacunes que *vérifier* a déjà nommées,
    # plus celle des segments retirés ici (A2). Un `CheckResult` ne suffisait pas — il n'atteint ni
    # l'utilisateur ni `complete`, et une réponse amputée sortait badgée « sûr ».
    lacunes = list(verification.lacunes)
    lacune_segments = Lacune(kind="segments_retires")
    if retires and lacune_segments not in lacunes:
        lacunes.append(lacune_segments)
    # Ce que le modèle a déclaré, puis ce que le code constate : une seule liste affichée, dans cet
    # ordre — les deux fronts rendent `unknown[]` sans une ligne de changement.
    declare = list(verification.unknown) + [t for t in limites if t not in verification.unknown]
    unknown = _fusionner_inconnues(declare, lacunes, language)
    answer = Answer(
        found=True, complete=verification.complete and not retires and not limites,
        lang=language,
        lang_fallback=lang_fallback,
        texte=texte, segments=segments, claims=list(verification.claims),
        rejected_claims=list(verification.rejected_claims), reason=None, verdict=verdict,
        faits_compris=faits_compris,
        unknown=unknown,
        clarification=clarification,
    )
    answer._decision_claims = list(verification._decision_claims)
    step.ms = int((time.monotonic() - t0) * 1000)
    return answer, step
