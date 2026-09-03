"""AD-9 — Une table des tiers, une affectation étape → tier.

`python -m server.app.llm.models --check` vérifie que chaque ID existe via l'API
(exit 0 si tous présents, 1 si un ID manque, 2 sans clé, 3 sur erreur API ou réseau).
"""

from __future__ import annotations

import asyncio
import sys
from typing import Literal

Tier = Literal["ingest", "reason", "micro"]

TIERS: dict[Tier, str] = {
    "ingest": "claude-opus-5",
    "reason": "claude-sonnet-5",
    "micro": "claude-haiku-4-5-20251001",  # l'alias sans date n'est pas listé par models.list()
}

STEP_TIERS: dict[str, Tier | None] = {
    "comprendre": "reason",
    "retrouver": "reason",
    "rediger": "reason",
    "verifier": "reason",
    "restituer": None,
    "ingest": "ingest",
}

# Capacités par modèle (AD-9 amendé, story 1.3) : `effort` — Haiku 4.5 rejette `output_config.effort` ;
# `temperature` — Sonnet 5 et Opus 5 rejettent (400) toute valeur non défaut, Haiku 4.5 accepte `temperature=0` ;
# `cache_ttl` — durée du breakpoint `cache_control` du préfixe (spine : « cache 1 h » pour `reason`).
MODEL_CAPS: dict[str, dict[str, object]] = {
    TIERS["ingest"]: {"effort": True, "temperature": False, "cache_ttl": "5m",
                      "context_window": 200_000},
    TIERS["reason"]: {"effort": True, "temperature": False, "cache_ttl": "1h",
                      "context_window": 200_000},
    TIERS["micro"]: {"effort": False, "temperature": True, "cache_ttl": "5m",
                     "context_window": 200_000},
}

# Effort explicite par tier quand le modèle l'accepte (AD-9) ; `micro` n'en a pas (MODEL_CAPS.effort=False).
# Le même `cache_ttl`, en secondes — la traduction du vocabulaire du fournisseur dans l'unité qu'un
# intervalle de maintien peut comparer (story 5.6, T5). Elle vit ici parce que c'est ici que le
# vocabulaire est déclaré ; `config.PREFIX_CACHE_TTL_S` en est le reflet pour le tier servi, et
# `tests/test_caches.py` refuse que les deux divergent.
CACHE_TTL_S: dict[str, float] = {"5m": 300.0, "1h": 3600.0}


EFFORT: dict[Tier, str] = {
    "ingest": "high",
    "reason": "medium",
}

# Dérogations explicites par prompt (Convention Seuils, revue 2.7 M3). La rédaction sinistre
# transcrit des clauses déjà retrouvées : son raisonnement de couverture appartient à *vérifier*.
# La valeur reste distincte du défaut du tier `reason` et versionnée au même endroit que celui-ci.
EFFORT_PAR_PROMPT: dict[str, str] = {
    "rediger_sinistre": "low",
    # **`verifier_sinistre` est revenu au défaut du palier (`medium`) le 03/09/2026 — story 5.6, T1c.**
    # Il portait `low` depuis la nuit du 02/09 : à l'effort `medium` du tier, sur le témoin A16
    # (« vitre d'insert », `POST /api/v1/sinistre`), la réflexion adaptative consommait la borne de
    # sortie (3 072 tokens **au total**, JSON tronqué → 503 `llm_parse`) ou la deadline (75 s, 503
    # `timeout`), 3 échecs sur 3. La dérogation achetait la latence et la place, pas le jugement.
    #
    # **Ce que `low` a coûté, mesuré.** Les trois réponses A16 d'après T1b
    # (`automation/runs/20260902-structure-index/a16-t1b/a16-r{1,2,3}.json`) sont 2/3 : navigation
    # et rédaction sont 3/3 — `p34:12` est cité dans les trois —, et c'est le **vérificateur** qui
    # perd le run 2. Il y rejette la claim `p34:12` en `hors_objet` tout en remplissant, dans le même
    # objet, une applicabilité qui la traite comme au sujet (`fait_manquant` : « caractère soudain de
    # l'action de la chaleur… »). Une sortie qui se contredit d'un champ à l'autre n'est pas un
    # jugement sévère, c'est un jugement pas fini : les deux autres runs, sur le même contrat et la
    # même ébauche, retiennent la claim. C'est exactement la dépense que l'effort règle.
    #
    # **Ce qui rend le retour tenable, et qui n'existait pas le 02/09** : la borne de sortie de cet
    # appel ne vaut plus 3 072 tokens mais `verifier_sinistre_max_tokens` = **4 096** (contrat JSON
    # 1 024 + réserve de réflexion 3 072, tous deux re-dérivés sur la mesure T1b — voir `config.py`).
    # La troncature du 02/09 s'est produite quand la réflexion a saturé un total de 3 072 ; le total
    # est aujourd'hui un tiers plus grand, et le contrat JSON a sa propre part. `_coherence` tient
    # les deux bouts : le plafond reste sous `llm_max_output_tokens`, et `llm_timeout_s` (55 s) laisse
    # le temps de l'écrire (4 096 / 85 + 5 = 53,2 s).
    #
    # **T1d, 03/09/2026 : la campagne a mesuré, et elle a démenti ces 4 096.** Les deux seuls appels
    # `medium` de cette chaîne ont saturé leur plafond — 4 096 et 4 095 tokens de **réflexion** pour
    # 4 096 de sortie, aucun JSON rendu, `LlmParse` terminal. Sur Sonnet 5 la réflexion est
    # adaptative et `budget_tokens` est refusé : rien ne la borne que `max_tokens`, qu'elle partage
    # avec le contrat. La borne de cet appel vaut donc désormais 1 024 + 5 120 = **6 144**, et
    # `llm_timeout_s` est passé à 78 s pour laisser le temps de l'écrire (6 144 / 85 + 5 = 77,3 s).
    # `[HYPOTHÈSE]` : la mesure à `medium` est **censurée** (elle dit « ≥ 4 096 »), sur un seul cas.
    # Si un appel sature encore 6 144, le repli est de revenir à `low` — où la dépense est mesurée,
    # non censurée — et non d'ajouter un palier de plus. Voir `config.py`,
    # `verifier_thinking_reserve_tokens`.
    #
    # **T10, 03/09/2026 : un appel a saturé 6 144, et le repli annoncé s'applique.** Mesure A16 sur
    # `28366ad` (`automation/runs/20260902-structure-index/a16-final1/a16-r1.json`, trace complète) :
    # la première vérification consomme **6 144 tokens de réflexion sans rendre un caractère de
    # JSON** — troncature, donc retry —, puis 4 994 et 5 057. Une vérification coûte là **120 s et
    # 0,18 €**, sur une chaîne dont la deadline entière vaut 290 s. La condition posée à T1d est
    # remplie mot pour mot : la mesure a dit que cette tâche n'a pas de plafond de réflexion à cet
    # effort, et le repli est `low`, pas un palier de plus.
    #
    # **Ce qui rend `low` tenable aujourd'hui et ne l'était pas à T1c.** Le mode d'échec qui avait
    # fait remonter l'effort — une sortie qui rejette une claim en `hors_objet` tout en remplissant,
    # dans le même objet, une applicabilité qui la traite comme au sujet — n'est plus payé au tarif
    # de la réflexion : il est **constaté par le code**, par le recoupement `hors_objet_incoherent`
    # ajouté en T1f. Un jugement qui se contredit d'un champ à l'autre est désormais attrapé là où
    # il se voit, pas évité en achetant de la profondeur. Et la dépense de `low` est mesurée, non
    # censurée : ≤ 2 500 tokens de réflexion sur la campagne d'hier.
    "verifier_sinistre": "low",
}


def model_for(tier: Tier) -> str:
    """ID de modèle du tier ; tier inconnu ⇒ ValueError (jamais de modèle par défaut)."""
    try:
        return TIERS[tier]
    except KeyError:
        raise ValueError(f"tier inconnu : {tier!r} (attendu : {', '.join(TIERS)})") from None


async def check_models(api_key: str) -> dict[str, bool]:
    """Présence de chaque ID de `TIERS` dans `models.list()` (auto-pagination)."""
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)
    available: set[str] = set()
    async for m in client.models.list(limit=100):
        available.add(m.id)
    return {model_id: model_id in available for model_id in TIERS.values()}


def main(argv: list[str]) -> int:
    if "--check" not in argv:
        for tier, model_id in TIERS.items():
            print(f"{tier}: {model_id}")
        return 0
    from server.app.config import get_settings

    key = get_settings().anthropic_api_key
    if not key:
        print("ANTHROPIC_API_KEY absente (environnement ou .env) : impossible de vérifier les modèles", file=sys.stderr)
        return 2
    try:
        results = asyncio.run(check_models(key))
    except Exception as exc:  # 401, réseau, 5xx : on ne distingue pas, on signale
        print(f"vérification impossible : {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    for tier, model_id in TIERS.items():
        print(f"{tier:7} {model_id:28} {'OK' if results[model_id] else 'ABSENT'}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
