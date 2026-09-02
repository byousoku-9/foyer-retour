"""Le contrôle d'étage des tests : le tier attendu se **lit** sur la configuration.

AD-9 affecte chaque étape à un tier, et la story 4.2b rend cette affectation surchargeable **par
étape** (`Settings.comprendre_tier`, `rediger_tier`, `verifier_tier`, `retrouver_outils_tier`). Un
test qui recopie `"micro"` ou `"claude-haiku"` n'épingle donc pas l'invariant d'AD-9 : il épingle la
valeur qu'avait la configuration le jour où il a été écrit, et il se met à mentir dès qu'elle bouge
— c'est exactement ce qu'a produit la promotion de *comprendre*, *retrouver* et *vérifier* sur
`reason`. Le contrôle vit donc ici, une seule fois, et lit `Settings`/`STEP_TIERS`/`TIERS` : aucun
tier ni identifiant de modèle en littéral dans un test (Convention Seuils, AD-16).

Ce que le contrôle juge, sur une `StepTrace` :
- le tier **publié** par l'étape est celui que la configuration lui sert ;
- chaque appel réellement émis porte le modèle de ce tier ;
- accessoirement, le nombre d'appels, quand l'AC en dépend (« un seul appel groupé »).

Et son message d'échec nomme les trois faits nécessaires pour le diagnostiquer sans instrumenter le
test : tier attendu, tier observé, modèles observés.
"""

from __future__ import annotations

from server.app.config import Settings
from server.app.domain.trace import StepTrace
from server.app.llm.models import STEP_TIERS, TIERS, Tier

# Les étapes dont `Settings` porte une surcharge par étape (story 4.2b). Les autres suivent
# l'affectation d'AD-9 telle quelle. *retrouver* est nommé `retrouver_outils` ici parce que sa
# surcharge ne vaut que pour les variantes qui appellent le modèle : *retrouver* déterministe est du
# code pur, il publie le tier d'AD-9 et n'appelle personne.
_SURCHARGE_PAR_ETAPE: dict[str, str] = {
    "comprendre": "comprendre_tier",
    "rediger": "rediger_tier",
    "verifier": "verifier_tier",
    "retrouver_outils": "retrouver_outils_tier",
}


def tier_attendu(etape: str, settings: Settings) -> Tier | None:
    """Le tier que la configuration sert à `etape` — `None` pour une étape sans appel modèle."""
    champ = _SURCHARGE_PAR_ETAPE.get(etape)
    if champ is not None:
        return getattr(settings, champ)
    try:
        return STEP_TIERS[etape]
    except KeyError:
        connues = ", ".join(sorted(set(STEP_TIERS) | set(_SURCHARGE_PAR_ETAPE)))
        raise ValueError(f"étape inconnue : {etape!r} (attendu : {connues})") from None


def modele_attendu(etape: str, settings: Settings) -> str:
    """L'ID de modèle servi à `etape`, lu sur `TIERS` — jamais une chaîne recopiée par un test."""
    tier = tier_attendu(etape, settings)
    if tier is None:
        raise ValueError(f"l'étape {etape!r} n'appelle aucun modèle : elle n'en attend aucun")
    return TIERS[tier]


def verifier_etage(step: StepTrace, settings: Settings, *, etape: str | None = None,
                   appels: int | None = None) -> None:
    """L'étape a bien tourné à l'étage que la configuration lui affecte (AD-9).

    `etape` n'est à passer que lorsque le nom de la `StepTrace` ne suffit pas à choisir la surcharge
    — le seul cas est *retrouver*, dont la variante par outils lit `retrouver_outils_tier` quand la
    variante déterministe reste sur l'affectation d'AD-9.
    """
    clef = etape if etape is not None else step.name
    attendu = tier_attendu(clef, settings)
    modele = TIERS[attendu] if attendu is not None else None
    observes = [call.model for call in step.calls]
    constat = (f"étape {step.name!r} : tier attendu {attendu!r} (modèle {modele!r}), "
               f"tier publié {step.tier!r}, modèles appelés {observes or ['(aucun)']}")
    assert step.tier == attendu, constat
    assert [m for m in observes if m != modele] == [], constat
    if appels is not None:
        assert len(step.calls) == appels, f"{constat} — {appels} appel(s) attendu(s)"
