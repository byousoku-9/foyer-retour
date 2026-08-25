"""AD-5 / AD-11 — Question comprise, tour de conversation, faits d'un sinistre."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator

from .document import DomainModel

Intent = Literal["question", "suivi", "meteo", "bavardage", "hors_perimetre"]
Role = Literal["user", "assistant"]


# Longueur d'**un** tour de conversation, et donc de tout ce que l'assistant peut avoir « dit »
# (AD-11 : « bornes d'entrée strictes ; rejet plutôt que troncature »). Ce n'est pas un seuil de
# `config.py` mais une **forme de contrat** — comme les bornes de `Faits` — et le front la porte
# sous le nom `TOUR_MAX_CARACTERES` ; `tests/test_web_chat.py` amarre les deux.
TOUR_MAX_CHARS = 2000

# Revue Codex 2.2 (B1) : la clarification est bornée **par la même valeur**, et ce n'est pas une
# coïncidence — c'est l'invariant qui referme la boucle de la story 2.2. La page conserve ce que
# l'assistant a dit dans un tour d'historique ; un tour hors borne est écarté par
# `chat.js::historiquePourApi`, si bien qu'une clarification plus longue que `Turn.texte`
# disparaissait de l'historique, l'assistant ne voyait plus sa propre question, et il la reposait
# indéfiniment. Avec `comprendre_max_tokens = 1024`, le cas était atteignable. Borner ici garantit
# que la question posée **tient toujours** dans le tour suivant : la boucle ne peut plus se rouvrir.
#
# Le rejet a lieu bien plus haut, sur la sortie de l'appel (`steps/comprendre.py`), pour emprunter la
# relance motivée du client ; cette borne-ci est la ceinture du domaine, celle qui vaut pour les deux
# pipelines et pour tout producteur futur.
CLARIFICATION_MAX_CHARS = TOUR_MAX_CHARS


class Turn(DomainModel):
    role: Role
    texte: str = Field(max_length=TOUR_MAX_CHARS)


class Faits(DomainModel):
    """Faits déclarés d'un sinistre (POST /api/v1/sinistre).

    **Les trois champs de texte sont bornés** (AD-11 : « bornes d'entrée strictes ; rejet plutôt que
    troncature »). Seule `description` l'était : `date` et `lieu` partaient au modèle dans le même
    bloc `untrusted()` que le reste, et seul `request_max_bytes` (65 536 octets, le corps entier) les
    limitait. Un `lieu` de 60 ko entrait donc dans le prompt de *comprendre* et faisait grossir des
    appels **facturés**, pour un champ qui décrit une pièce d'un logement (revue 1.9, tour 2).

    Les bornes sont des littéraux du **domaine**, comme celle de `description`, et non des seuils de
    `config.py` : ce sont des formes de contrat HTTP (AD-11 les énumère), pas des réglages qu'une
    éval déplace. `date` tient une date ISO 8601 et de quoi la commenter ; `lieu` tient une adresse
    ou la désignation d'une pièce.
    """

    date: str | None = Field(None, max_length=64)
    lieu: str | None = Field(None, max_length=200)
    montant_eur: float | None = Field(None, ge=0)
    description: str = Field(max_length=2000)


class QuestionScope(DomainModel):
    """Portée dérivée du profil ou des faits du sinistre."""

    themes: list[str] = Field(default_factory=list)  # école, allocations, auto…
    bien: str | None = None
    evenement: str | None = None
    lieu: str | None = None
    cause: str | None = None
    moment: str | None = None

    def borner(self, max_chars: int, max_themes: int) -> tuple[QuestionScope, list[str]]:
        """Copie sans les libellés hors borne, et la liste des champs ignorés (story 1.9, D4).

        Ces libellés sont **produits par le modèle** (*comprendre* les extrait des faits déclarés) et
        la page sinistre les **affiche** sous « les faits compris ». Ils tombent donc sous la règle
        de D8 (spec 1.8), qui vaut pour tout texte du modèle qui atteint un écran : hors borne, le
        libellé est **ignoré**, jamais tronqué — une demi-phrase de cause ou de lieu induirait en
        erreur plus sûrement qu'une case vide —, et la trace dit lesquels.

        Le champ ignoré devient `None` (ou disparaît de `themes`) plutôt que de rester : le front
        n'affiche que ce qui est renseigné, et un `bien` amputé serait indiscernable d'un `bien`
        déclaré. La liste rendue nomme les **champs**, jamais leur contenu — elle part dans un
        `CheckResult` de la trace, et AD-10 y interdit le texte.

        `max_themes` borne le **nombre** de thèmes, et pas seulement leur longueur (revue 1.9, tour
        2) : `themes` est une liste rendue par le modèle, la page les joint en une seule ligne, et
        deux cents libellés courts passaient tous la borne de longueur pour produire une ligne sans
        fin à l'écran. Les thèmes en trop sont écartés par la **fin** — l'ordre du modèle est celui
        de la pertinence qu'il leur prête —, et l'écart se dit comme le reste.
        """
        ignores: list[str] = []
        themes = [t for t in self.themes if len(t) <= max_chars]
        tenus = themes[:max_themes]
        if len(tenus) != len(self.themes):
            ignores.append("themes")
        themes = tenus
        remplacements: dict[str, object] = {"themes": themes}
        for nom in ("bien", "evenement", "lieu", "cause", "moment"):
            valeur = getattr(self, nom)
            if valeur is not None and len(valeur) > max_chars:
                ignores.append(nom)
                remplacements[nom] = None
        return self.model_copy(update=remplacements), ignores


# Convention « Données & formats » du spine : langues **ISO 639-1**, donc exactement deux lettres
# (revue Codex 1.4, I2 — `^[a-z]{2,3}$` laissait passer `eng`/`fra` jusque dans la consigne de rédaction).
_LANG = re.compile(r"^[a-z]{2}$")


def _lang_or_fr(value: str) -> str:
    """Code ISO 639-1 en minuscules (deux lettres) ; tout le reste retombe sur `fr` (convention Langue).

    La normalisation vit ici, pas dans les étapes : *comprendre* et *rédiger* ne peuvent pas
    s'importer l'une l'autre (AD-9) et la dupliquaient avec deux sémantiques légèrement différentes —
    un `ParsedQuestion(language="EN")` construit ailleurs (pipeline 1.5, reprise) retombait alors
    silencieusement sur `fr` à la rédaction seulement (revue 1.4). Les deux issues de *comprendre*
    (question résolue ou clarification) partagent la même règle.
    """
    v = (value or "").strip().lower()
    return v if _LANG.match(v) else "fr"


class ParsedQuestion(DomainModel):
    """Question **autonome** : toutes les anaphores sont résolues (AD-5).

    Ce type est le seul laissez-passer vers *retrouver*. Quand l'historique ne permet pas de résoudre
    une référence, *comprendre* ne construit pas cet objet du tout : il rend un `ClarificationRequise`.
    """

    question_resolue: str
    intent: Intent
    language: str = "fr"
    terms: list[str] = Field(default_factory=list)  # toujours en français
    scope: QuestionScope = Field(default_factory=QuestionScope)
    # Les sous-questions distinctes que la question pose, dans l'ordre où elle les pose (AD-4,
    # « toutes les facettes de `ParsedQuestion` couvertes »). Elles vivent **ici**, et non dans la
    # sortie de *vérifier*, parce qu'AD-4 les nomme « facettes de `ParsedQuestion` » : le découpage
    # est arrêté par *comprendre*, avant tout retrieval et toute rédaction, par un appel qui n'a
    # jamais vu l'ébauche. C'est ce qui rend une facette **omise** détectable — celui qui attribue la
    # couverture ne peut plus effacer la sous-question à laquelle il n'a pas répondu (revue Codex
    # 1.5, tour 3, B3). Liste vide = aucun découpage rendu ⇒ aucune preuve de couverture ⇒
    # `complete=False`, jamais l'inverse.
    facettes: list[str] = Field(default_factory=list)

    @field_validator("language")
    @classmethod
    def _normalise_language(cls, value: str) -> str:
        return _lang_or_fr(value)

    def termes_de_recherche(self) -> list[str]:
        """Termes réellement cherchés : `terms` puis `scope.themes`, dédupliqués, ordre conservé.

        Source **unique** (story 1.5) : *retrouver* construit ainsi ses termes, et l'`AbsenceProof`
        du pipeline doit dire exactement ce qui a été cherché (AD-4 `terms_searched`). Les deux
        calculs vivaient dans deux fichiers qu'aucun test ne reliait — une divergence aurait fait
        mentir le refus affiché à l'utilisateur sans faire échouer quoi que ce soit.
        """
        out: list[str] = []
        for t in (*self.terms, *self.scope.themes):
            t = t.strip()
            if t and t not in out:
                out.append(t)
        return out


class ClarificationRequise(DomainModel):
    """Seconde issue de *comprendre* : la question n'est pas résoluble, il faut la poser (AD-5).

    AD-5, mot pour mot : « une anaphore non résoluble avec l'historique produit `Answer.clarification`
    (question à l'utilisateur) — *comprendre* ne fabrique jamais une `question_resolue` ». Le signal
    naît donc à l'étape qui constate l'échec de résolution, et il est **typé** : un tour 2 (revue
    Codex 1.4, B4) l'avait porté par un champ de `ParsedQuestion`, ce qui laissait subsister une
    `question_resolue` non autonome — rien n'empêchait alors *retrouver* de chercher « et pour eux ? ».
    Les deux issues sont désormais des types distincts, et la question non autonome n'existe nulle
    part dans le résultat. Sa *restitution* à l'utilisateur reste l'AC de la story 2.2 ;
    `Answer.clarification` existe déjà pour la porter, `found=False`.
    """

    # Bornée : la page doit pouvoir la reconduire dans un tour d'historique, sinon l'assistant
    # ne revoit pas sa propre question et la repose (revue Codex 2.2, B1).
    clarification: str = Field(max_length=CLARIFICATION_MAX_CHARS)  # la question courte à poser, dans sa langue
    intent: Intent
    language: str = "fr"

    @field_validator("language")
    @classmethod
    def _normalise_language(cls, value: str) -> str:
        return _lang_or_fr(value)
