"""Configuration centralisée (pydantic-settings).

Tous les seuils numériques `[HYPOTHÈSE]` du spine vivent ici et nulle part ailleurs ;
ils sont exposés dans `Trace.thresholds` via `Settings.thresholds()` et se règlent avec les évals.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class RetrievalDefault:
    variant: str
    tier: str
    prompt_cache: bool


def load_retrieval_default(path: Path) -> RetrievalDefault:
    """Lit strictement le triplet versionné, sans repli silencieux."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"défaut retrieval illisible ({path}) : {exc}") from exc
    expected = {"variant", "tier", "prompt_cache"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("défaut retrieval : champs exacts variant, tier, prompt_cache attendus")
    if value["variant"] not in {"navigation", "deterministe", "outils", "full_context"}:
        raise ValueError(f"défaut retrieval : variant invalide {value['variant']!r}")
    if value["tier"] not in {"reason", "micro"}:
        raise ValueError(f"défaut retrieval : tier invalide {value['tier']!r}")
    if type(value["prompt_cache"]) is not bool:
        raise ValueError("défaut retrieval : prompt_cache doit être un booléen JSON")
    return RetrievalDefault(**value)


RETRIEVAL_DEFAULT_PATH = REPO_ROOT / "data" / "retrieval-default.json"
RETRIEVAL_DEFAULT = load_retrieval_default(RETRIEVAL_DEFAULT_PATH)

# Nombre maximal d'éléments des trois listes que *comprendre* fait rendre au modèle (`terms`,
# `themes`, `facettes`). Revue Codex 2.1 (M3), puis 2.2 (I2) : la valeur vivait en dur dans
# `steps/comprendre.py`, ce que la Convention Seuils interdit sans exception — « les seuils
# numériques vivent dans `server/app/config.py`, jamais en dur ». Elle agit sur le coût d'un appel
# et sur ce qui part en `LlmParse` : elle se règle, donc elle se publie (`Trace.thresholds`).
#
# C'est une **constante de module** et non un champ de `Settings`, et c'est la seule chose que
# l'étape avait raison de vouloir : cette borne-ci entre dans le schéma JSON envoyé au modèle, donc
# dans le préfixe caché et dans la clé de requête (AD-9). Un champ `.env` la ferait dépendre du poste
# de travail — ce qui est facturé changerait avec un fichier local, et les fixtures enregistrées
# cesseraient de se rejouer. `comprendre_max_tokens`, lui, entre aussi dans la requête **et** reste
# un champ : la différence est qu'il ne décrit pas le contrat de sortie, il plafonne une dépense, et
# c'est précisément ce qu'une éval doit pouvoir déplacer.
LISTE_MAX_ITEMS = 32
# Projection HTTP des diagnostics de quarantaine. Le défaut est nommé ici, à côté du champ qui
# l'emploie, afin que les consommateurs n'aient pas à introspecter ``Settings.model_fields`` à
# l'import (une couture fragile aux changements de Pydantic).
RAISON_PUBLIABLE_MAX_DEFAULT = 500
# Story 4.5 (FR41) — **l'unique autorité** du nom de l'artefact machine des résultats d'évals.
#
# L'écrivain vit dans `server/evals/publication.py`, le lecteur dans `server/app/api/etat.py`, et la
# table des couches interdit à `api` d'importer `evals` : sans un nom partagé, les deux auraient eu
# leur propre littéral, et un caractère de différence aurait fait rendre `publie: false` à la route
# pour toujours — un défaut muet, exactement ce qu'AD-16 interdit. `config` est la seule couche que
# les deux peuvent lire.
EVALS_PUBLICATION_FILE = "evals-latest.json"
# Longueur de la forme **publiée** d'une révision (AD-11 : « `version: sha7` »). C'est une projection
# d'affichage, jamais une valeur de comparaison : le gate se compare sur la révision complète.
SHA_COURT = 7
_HEX = frozenset("0123456789abcdef")


# Durée de vie du cache de préfixe des modèles servis, en secondes. C'est le `"1h"` que
# `llm/models.MODEL_CAPS` déclare par modèle (AD-9), exprimé dans l'unité qu'un intervalle de
# maintien peut comparer — `config` ne peut pas importer `llm` (table des couches), et une chaîne
# `"1h"` ne se compare à rien. `tests/test_caches.py` relit `MODEL_CAPS` pour que les deux textes ne
# puissent pas diverger.
PREFIX_CACHE_TTL_S = 3600.0


def _est_revision_complete(valeur: str) -> bool:
    """40 hexadécimaux — la seule forme qui identifie un commit sans ambiguïté."""
    return len(valeur) == 40 and all(c in _HEX for c in valeur.lower())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", env_file_encoding="utf-8",
                                      env_ignore_empty=True, extra="ignore")

    # **Le temps qu'un front accepte d'attendre pour un petit GET** — `/api/v1/sante`,
    # `/api/v1/documents`, un rapport d'ingestion. Aucune de ces routes n'appelle un modèle : tout y
    # est calculé au démarrage du serveur, et dix secondes sont une éternité pour elles.
    #
    # **Pourquoi ce champ existe depuis la story 5.6 (T3, 03/09/2026).** Ce budget-là n'avait pas de
    # nom : les trois fronts qui sondent (`web/app/chat.js`, `tools/accueil/accueil.js`,
    # `tools/sinistre/ingestion.js`) empruntaient `client_abort_margin_s`, parce qu'elle valait 10 s
    # et que ça tombait bien. L'emprunt tenait par coïncidence, pas par raison : la marge d'abandon
    # est ce que le **navigateur** ajoute à la deadline d'un appel de **pipeline**, et l'amendement
    # AD-1 du 03/09/2026 la fait dépendre du `--timeout` de Cloud Run — elle vaut 150 s. Une sonde
    # de santé bornée à 150 s laisse la page « en chargement » pendant deux minutes et demie devant
    # un serveur mort, et `chat.js` **attend** sa sonde avant la première question : la saisie
    # serait restée verrouillée d'autant. Le seuil est donc nommé pour ce qu'il est, et les deux
    # valeurs peuvent désormais bouger sans se traîner l'une l'autre.
    #
    # 10 s : la valeur que les trois fronts servaient déjà, et qu'aucune mesure ne conteste — ces
    # routes répondent en quelques millisecondes. `[HYPOTHÈSE]`, comme les autres bornes de temps.
    client_probe_timeout_s: float = Field(10.0, gt=0)

    env: Literal["dev", "prod"] = "dev"
    # Dérogation d'AD-7 : servir un document sans gate valide, avec l'alerte `sans_gate`. AD-7 la
    # cadre — « dev / J+1 avant le premier gate » — et l'AC de 1.10 la ferme : « `ALLOW_UNGATED` est
    # **désactivé** en production à la fin de cette story ». Depuis 1.10 les deux gates existent :
    # en `prod`, `_coherence` force donc `False`, que la variable soit absente, `false`, ou `true`.
    # Retirer la ligne du `Dockerfile` ne suffisait pas — la surface réelle est la configuration du
    # service (`--set-env-vars ALLOW_UNGATED=true`), qu'aucun test hors ligne ne voit (revue Codex
    # 1.10, B3). La demande n'est pas perdue pour autant : `ungated_demande_en_prod` la retient, et
    # `/api/v1/sante` la publie en alerte — refusée, jamais muette (AD-16).
    allow_ungated: bool | None = None
    # Dérivé, jamais configuré : `_coherence` l'écrase en `prod`. Vrai quand `ALLOW_UNGATED=true` a
    # été posé sur un service de production, et donc refusé.
    ungated_demande_en_prod: bool = False
    anthropic_api_key: str = ""
    usd_eur: float = Field(0.92, gt=0)
    # **La révision produit qui tourne, en entier.** En production elle vient de la configuration
    # du service Cloud Run — `deploy.yml` pose `GIT_SHA=<sha40>`, ce que `gcloud run deploy --source`
    # sait faire alors qu'il n'accepte aucun `--build-arg` —, et cette variable recouvre le
    # `ENV GIT_SHA` que le `Dockerfile` laisse à `dev`. Hors conteneur, `dev`.
    #
    # **Pourquoi la révision complète, et non le `sha7` d'avant** (story 4.5, revue B2) : un gate
    # `full` porte 40 hexadécimaux et affirme avoir mesuré *ce* commit. Comparé à un `sha7`, le
    # contrôle ne discriminait plus que 16⁷ classes — un gate d'un **autre** commit partageant les
    # sept premiers caractères était servi sans alerte, et les deux moitiés du même invariant
    # n'avaient pas la même exigence (`plancher.py` exige 40 hex exacts côté preuve). La cause
    # n'était pas la comparaison, c'était l'ambiguïté de ce que le service sait de lui-même : c'est
    # donc elle qu'on lève.
    #
    # AD-11 continue de promettre `GET /api/v1/sante` → `version: sha7` : la valeur **publiée** est
    # une projection courte de celle-ci (`version_publiee`), pas une seconde source de vérité. Le
    # smoke de déploiement compare cette projection au sha7 du commit qui l'a déclenchée.
    #
    # Ce n'est pas un seuil numérique : il n'entre pas dans `thresholds()`.
    git_sha: str = "dev"

    @property
    def version_publiee(self) -> str:
        """La forme **courte** publiée par `/api/v1/sante` — AD-11, `scripts/smoke.py`, README.

        Une seule source de vérité (`git_sha`, complète), une projection pour l'affichage. L'inverse
        — publier la valeur brute et tronquer ailleurs — laisserait deux endroits décider de ce
        qu'est « la version », et c'est exactement la divergence que la revue B2 a trouvée.
        """
        return self.git_sha[:SHA_COURT] if _est_revision_complete(self.git_sha) else self.git_sha

    # Temps (AD-1, AD-9)
    # **75 s, et non 55 (02/09/2026 ; remesuré au tour « budgets Sonnet », appliqué au tour final).**
    # 55 s a été calibré quand une seule étape sur cinq était servie par `reason` ; depuis la
    # promotion de *comprendre*, *retrouver* et *vérifier*, les cinq appels du chemin nominal sont
    # des appels Sonnet à effort `medium`, donc avec réflexion étendue.
    # **Ancre de latence** — la seule que le projet possède : *rédiger* (tier `reason`, effort
    # `medium`), mesuré 12,9 / 15,9 / 17,6 s pour 904 → 1 130 tokens de sortie enregistrés, soit
    # **14,3 à 15,6 ms par token de sortie**. **Charge de sortie** du chemin nominal, relevée sur les
    # 108 réponses Sonnet enregistrées (médiane / maximum par étape) : *comprendre* 146 / 220, les
    # deux tours de *retrouver* 99 / 195 chacun, *rédiger* 838 / 1 509, *vérifier* 215 / 820.
    #   — charge médiane, 1 397 tokens : **20 à 22 s** ;
    #   — pire charge observée, 2 939 tokens : **42 à 46 s** ;
    #   — même pire, corrigé de la dispersion mesurée d'un seul appel (*rédiger* a franchi 25 s
    #     **deux fois sur six**, soit 1,42 fois son maximum typique) : ≈ 65 s, plus ≈ 2,5 s
    #     d'établissement des cinq connexions, soit **≈ 68 s**.
    # 55 s couvrait le pire *observé* avec 9 s de marge — moins que la dispersion d'un seul appel —
    # et laissait donc atteignable un `Timeout` **terminal** (503) sur une question nominale : deux
    # mécanismes vivent hors de tout `except`, le contrôle `budget.remaining() <= 0` posé avant
    # chacune des cinq étapes (`pipelines/`) et le `timeout_for_call() = min(llm_timeout_s,
    # remaining())` que le SDK reçoit (`llm/budget.py`). 75 s couvre les 68 s majorés avec 10 %.
    # **Ce que ce relèvement ne fait pas : rallonger une requête.** La deadline est un **budget**,
    # jamais une attente — rien n'attend qu'elle s'écoule. Une requête normale finit en 20 à 22 s
    # exactement comme avant ; ce qui change est qu'une requête lente **aboutit** au lieu de sortir
    # en 503. **Latence utilisateur maximale entérinée** : `deadline_s + client_abort_margin_s`
    # = **85 s** avant que le navigateur abandonne (`web/app/chat.js` lit les deux sur `/sante`).
    # **Ce que la deadline ne couvre pas, et c'est voulu** : la relance d'AD-3 (≈ 82 s au pire) et la
    # reprise de 4.2e (≈ 95 s) dépassent, et leurs pré-contrôles (`pipelines/sinistre.py`,
    # `budget.remaining() <= llm_retry_margin_s`) les refusent **avant** tout appel puis servent
    # l'acquis en 200 avec `relance_abandonnee` ou `reprise_sans_place` — jamais un `Timeout`.
    #
    # **La valeur couvrante mesurée**, celle sous laquelle la deadline ne doit pas redescendre sans
    # une nouvelle mesure : 68 s. Elle est tenue par
    # `tests/test_budget.py::test_la_deadline_couvre_la_queue_mesuree_du_chemin_nominal`.
    # **100 s, et non 75 (02/09/2026, 20 h 30, témoin A16 en live).** Le chiffrage à 68 s ne
    # comptait qu'un cycle rédiger → vérifier. Sur « vitre d'insert » (`POST /api/v1/sinistre`,
    # service local réel), le vérificateur signale des facettes non couvertes et la chaîne fait
    # **deux cycles**, avec une relance de parse de *rédiger* : 61,4 s et 60,5 s quand elle aboutit
    # (200, `p34:12` cité), et une troisième requête coupée à 75 s alors que son dernier
    # *vérifier* avait démarré (étapes cumulées 75,0 s, ≈ 80 s au total). Un 503 sur un chemin
    # nominal à deux cycles est le 503 de configuration qu'AD-16 refuse. 100 s couvre les ≈ 80 s
    # mesurés avec 25 % de marge, reste sous `--timeout=120` de Cloud Run avec la marge
    # d'abandon du navigateur (`client_abort_margin_s` : 110 s au pire, lue sur `/sante`), et ne
    # rallonge aucune requête : une question à un cycle finit toujours en 20 à 30 s.
    # **165 s, et non 100 (03/09/2026, story 5.6 T3, sur la mesure du prototype validé).**
    # Le chiffrage à 100 s décrivait un chemin dont le **code** choisissait ce que la rédaction
    # voyait : deux tours de navigation outillés, la sélection faite par des passes lexicales.
    # L'amendement AD-1 du 03/09/2026 le périme — « le modèle navigue, le code vérifie » : la
    # navigation se fait par le modèle sur le **sommaire complet**, en 6 à 8 tours, *rédiger*
    # fusionné dans la même conversation. Ce n'est pas la même chaîne, donc pas la même borne.
    #
    # **La mesure.** Prototype validé, `automation/runs/20260902-structure-index/proto-runs/serie2/`
    # (03/09/2026 07 h 00–07 h 01 ; A16 ×3 et bougie ×1 sur le contrat AXA, sommaire complet de
    # 42 967 tokens en préfixe caché 1 h, trois outils `sommaire`/`ouvrir_noeud`/`chercher`) :
    #   — A16 #1 : 4 tours, 2 069 tokens de sortie, **27,4 s** (réflexion adaptative 831 tokens,
    #     dont 657 au seul tour 3) ;
    #   — A16 #2 : 2 tours, 1 009 tokens, 12,3 s ; A16 #3 : 2 tours, 1 435 tokens, 16,0 s ;
    #   — bougie : 2 tours, 1 194 tokens, 16,0 s.
    # Débit d'écriture, une fois la latence d'amorçage retirée : **85,3 à 102,5 tokens/s** — le
    # minorant publié (`llm_output_tokens_per_s_min` = 85) tient, et c'est lui qui majore ci-dessous.
    # Latence d'amorçage résiduelle, au même minorant : 0,77 / 0,22 / 0 / 0,98 s par appel — aucun
    # appel au-dessus d'une seconde ; on la majore du double, **2 s par appel**.
    #
    # **La dérivation, sur le pire chemin nominal qu'AD-1 rend légitime** (8 tours, la borne haute
    # de « 6–8 ») : *comprendre* (1 appel), sept tours d'outils, le tour terminal qui rend
    # l'`AnswerDraft`, *vérifier* (appel distinct sur contexte propre), puis la **relance atomique**
    # d'AD-3 — *rédiger* et *vérifier* indissociables. Douze appels.
    #   — *comprendre* 220 tokens (max des 108 réponses Sonnet enregistrées) ;
    #   — 7 tours d'outils × 729 (pire tour d'outils mesuré, réflexion comprise) = 5 103 ;
    #   — tour terminal 1 509 (pire *rédiger* enregistré ; les tours terminaux du prototype, 709 à
    #     900 tokens de texte libre, restent dessous) ;
    #   — *vérifier* 820 (pire enregistré) ; relance 1 509 + 820 = 2 329.
    # Soit **9 981 tokens**, à 85 tokens/s = 117,4 s d'écriture, plus 12 × 2 s d'amorçage = 24 s :
    # **141,4 s**. 165 s les couvre avec **16,7 %** de marge, et tombe dans la fenêtre
    # `[HYPOTHÈSE : 150–180 s]` qu'AD-1 annonce.
    #
    # **Ce que ce relèvement ne fait pas, et c'est le même argument qu'à 75 puis 100 s : rallonger
    # une requête.** La deadline est un **budget**, jamais une attente — rien n'attend qu'elle
    # s'écoule, `remaining()` n'est lu que pour **refuser**. Les runs du prototype finissent en 12 à
    # 27 s et continueront ; ce qui change est qu'une navigation longue **aboutit** au lieu de sortir
    # en 503, et que la relance d'AD-3 redevient atteignable au lieu d'être refusée avant tout appel
    # sur tout chemin à plus de deux tours.
    # **Ce qu'elle ne couvre toujours pas, et c'est voulu** : la reprise de 4.2e (un appel de plus)
    # reste refusée avant envoi quand la place manque (`reprise_sans_place`), et l'acquis est servi
    # en 200 — jamais un `Timeout`.
    #
    # **L'ordre imposé par AD-11 (amendement AD-1)** : délai d'attente du client **>** `--timeout`
    # Cloud Run **>** deadline serveur, soit **315 s > 300 s > 165 s**. Le `--timeout` passe à 300 s
    # dans `.github/workflows/deploy.yml`, et la patience du navigateur est
    # `deadline_s + client_abort_margin_s` (voir ce champ), lue sur `/sante`.
    #
    # **250 s, et non 165 (03/09/2026, story 5.6 T1c).** Deux choses ont bougé depuis T3, et les deux
    # allongent le même chemin : le pipeline intégré a remplacé le prototype (T1b), et le
    # vérificateur sinistre est revenu à l'effort `medium` de son palier (T1c). La dérivation est
    # refaite terme par terme sur les trois réponses A16 d'après T1b
    # (`automation/runs/20260902-structure-index/a16-t1b/a16-r{1,2,3}.json`) :
    #   — *comprendre* **360** (mesuré 316 / 336 / 359, contre 220 lus sur les fixtures enregistrées :
    #     ce n'est plus le même prompt) ;
    #   — 7 tours d'outils × **729** (pire tour du prototype, réflexion comprise ; le pipeline intégré
    #     reste dessous — 46 à 503) = 5 103 ;
    #   — tour terminal **2 386**, la dérivation de T1b à six affirmations, inchangée ;
    #   — *vérifier* **4 096** — et c'est le terme qui change de **nature**. Les autres sont des
    #     maxima mesurés ; celui-ci ne peut plus l'être, puisque l'effort de l'appel vient de
    #     changer et que personne n'a mesuré sa sortie à `medium` sur cette chaîne. Le seul majorant
    #     honnête est alors le plafond qu'on lui envoie, `verifier_sinistre_max_tokens`. Y mettre les
    #     3 130 tokens mesurés à `low` serait majorer un appel par la mesure d'un appel qu'on ne fait
    #     plus.
    #   — relance atomique d'AD-3 : tour terminal + *vérifier* une seconde fois.
    # Soit **18 427 tokens**, à 85 tokens/s = 216,8 s d'écriture, plus 12 × 2 s d'amorçage = 24 s :
    # **240,8 s**. 250 s les couvre avec **3,8 %**.
    #
    # **Pourquoi 3,8 % suffisent ici alors que 16,7 % étaient exigés à 165 s.** La marge n'a pas la
    # même fonction quand la dérivation change de régime : celle de T3 majorait des termes mesurés,
    # celle-ci empile quatre majorations qui ne se produisent jamais ensemble — le débit **minoré**
    # (85 quand on mesure 85 à 102), la latence d'amorçage **doublée** (2 s quand aucun appel ne
    # dépasse 1 s), **huit** tours quand le pipeline en fait deux ou trois, et le **plafond** de
    # *vérifier* au lieu de sa sortie. Ajouter 17 % par-dessus n'achèterait pas de sûreté, cela
    # rapprocherait la deadline des 300 s de Cloud Run, qui sont, eux, une vraie coupure.
    #
    # **Ce que ce relèvement ne fait pas, une troisième fois : rallonger une requête.** Les trois
    # runs A16 finissent en 52 à 65 s et rendent 100 à 113 s de deadline ; ce qui change est qu'une
    # navigation longue, suivie d'une relance, **aboutit** au lieu de sortir en 503.
    #
    # **La valeur couvrante mesurée**, sous laquelle la deadline ne doit pas redescendre sans une
    # nouvelle mesure : **240,8 s**. Elle est tenue par
    # `tests/test_config.py::test_la_deadline_couvre_la_chaine_de_navigation_par_le_modele`, et
    # l'ordre des trois délais par `tests/test_workflows.py::
    # test_le_timeout_cloud_run_couvre_la_deadline_du_serveur` — l'ordre d'AD-11 est conservé sans
    # toucher au `--timeout` : **315 s > 300 s > 250 s**, la patience du client restant à 315 s parce
    # que `client_abort_margin_s` est re-dérivée avec (150 → 65).
    # `[HYPOTHÈSE]` : ni le nombre de tours (2 à 4 observés, jamais 8) ni la sortie de *vérifier* à
    # `medium` ne sont mesurés sur le chemin **servi**. La campagne `--repeat 3` doit donner les deux,
    # et c'est elle qui resserrera cette borne — pas un arbitrage.
    deadline_s: float = Field(250.0, gt=0)
    # **40 s, et non 25 (amendement AD-16, story 1.9, sur mesure).** Le spine écrivait « un appel LLM
    # en timeout (25 s) ⇒ 503 » ; la règle — l'échec est terminal, jamais dégradé — ne bouge pas, la
    # valeur si. Mesuré sur le cas bougie servi par `POST /api/v1/sinistre` : *rédiger* (tier
    # `reason`, Sonnet 5, effort `medium`, une ébauche de clauses citées) prend **12,9 / 15,9 / 17,6 s**
    # quand il aboutit, et a franchi les 25 s **deux fois sur six** requêtes — une fois sur son premier
    # appel, une fois sur celui de la relance d'AD-3. Un tiers des soumissions d'une démonstration
    # publique ressortait donc en 503 sur un chemin parfaitement nominal, sans que rien ne soit en
    # panne. 25 s bornait la queue de la distribution, pas un incident.
    # Le garde-fou réel reste ailleurs, et il n'est pas touché : la deadline globale (`deadline_s`)
    # que `RequestBudget.timeout_for_call()` impose déjà en `min(llm_timeout_s, restant)`, le plafond
    # de coût par requête (`max_cost_eur_per_request`, appliqué **avant** l'appel), le plafond
    # d'appels (`max_llm_attempts`) et, au déploiement, `--timeout=120` de Cloud Run. 40 s laisse la
    # chaîne nominale (comprendre ≈ 3 s + rédiger + vérifier ≈ 4 s) tenir sous les 75 s, et une
    # relance qui déborderait est coupée par la deadline globale — le même 503, mais pour la vraie
    # raison. `[HYPOTHÈSE]` : à re-régler sur la distribution complète que donneront les 15–20
    # sinistres des questions-témoins (4.2), qui diront aussi s'il faut baisser l'effort de *rédiger*
    # plutôt que d'attendre plus longtemps.
    #
    # **55 s depuis le correctif du tour 3.** À 40 s, le plafond de sortie du vérificateur sinistre
    # était **inatteignable dans le temps qu'on lui laissait** : le débit mesuré sur les quatre
    # appels audités est de 89 à 95 tokens/s, soit ≈ 46 s pour 4 096 tokens. La borne effective
    # était donc 3 575 tokens (87 % du plafond déclaré), et toute requête qui demandait davantage
    # sortait en 503 `timeout` — c'est exactement ce qui a tué la deuxième réponse A16, dont
    # l'ébauche était pourtant la meilleure des trois, pour 0,14 € brûlés alors que la deadline
    # laissait encore 73 s. Baisser `max_tokens` à la place aurait tronqué la sortie, donc rendu un
    # `LlmParse` terminal : le même 503, pour une raison pire. L'invariante `_coherence` ci-dessous
    # lie désormais les deux nombres, pour qu'ils ne puissent plus diverger en silence.
    #
    # **Re-dérivé le 03/09/2026 (story 5.6 T3, puis T1c) et inchangé : 55 s.** La navigation par le
    # modèle allonge la **chaîne**, pas l'appel. Ce que ce délai doit couvrir reste ce que
    # `_coherence` calcule : la plus longue sortie d'**étape**, c'est-à-dire
    # `verifier_sinistre_max_tokens`, passé à T1c de 3 456 à **4 096** (1 024 + 3 072) avec le retour
    # du vérificateur sinistre à l'effort `medium`. Soit 4 096 / 85 + 5 = **53,2 s** — 3,3 % sous les
    # 55 s. La marge est mince et c'est le contrôle qui la surveille : un plafond de plus, ou une
    # réserve de réflexion de plus, et la configuration refusera de démarrer tant que ce délai n'aura
    # pas été re-dérivé avec elle. Le prototype ne
    # déplace pas ce majorant : son plus long tour rend 900 tokens (15,6 s majorées), et son tour
    # le plus réfléchi 729 dont 657 de réflexion (13,6 s). Relever ce délai sans que le plafond de
    # sortie d'une étape ait bougé n'achèterait rien et retarderait la détection d'un appel pendu.
    # L'invariante ci-dessous mord toujours : si un tour de navigation-rédaction se voit doter d'une
    # réserve de réflexion qui pousse son plafond au-delà de 4 250 tokens, la configuration refusera
    # de démarrer, et c'est là — pas ici — qu'il faudra re-mesurer.
    llm_timeout_s: float = Field(55.0, gt=0)
    # Débit de sortie **minoré** du fournisseur, réflexion comprise, tel que l'audit le mesure :
    # 89,4 tokens/s en régression sur les quatre appels du vérificateur sinistre (89 à 95 selon
    # l'appel, ordonnée à l'origine ≈ 0). 85 le minore de 5 % — un minorant, parce qu'il sert à
    # majorer une durée : le surestimer ferait passer une configuration qui expire en réel.
    # `[HYPOTHÈSE]`, à re-mesurer dès qu'un autre modèle ou un autre effort est servi.
    # **Reconfirmé le 03/09/2026** sur les dix appels de navigation du prototype validé (série 2) :
    # 85,3 / 97,9 / 102,5 / 88,4 tokens/s par run, une fois retirée la latence d'amorçage. Le plus
    # lent des quatre runs (bougie, 1 194 tokens en 16,0 s) donne 85,3 : le minorant tient de
    # justesse, et c'est ce qu'on lui demande. La valeur ne bouge pas.
    llm_output_tokens_per_s_min: float = Field(85.0, gt=0)
    # Marge laissée entre la durée majorée de la plus longue sortie et le délai d'un appel : le
    # temps que le fournisseur met à commencer à répondre, et la latence réseau. Mesurée sur les
    # mêmes appels — l'ordonnée à l'origine de la régression est ≈ 0, donc 5 s est déjà confortable.
    llm_latence_marge_s: float = Field(5.0, ge=0)
    # Marge que le **navigateur** ajoute à `deadline_s` avant d'abandonner sa requête (AD-11 :
    # `chat.js` borne son attente, sans quoi la saisie reste verrouillée indéfiniment). Elle vit ici
    # et non dans `chat.js` — un seuil numérique n'a qu'un domicile (convention du projet) — et
    # `GET /sante` la publie pour que le front la lise au lieu de la recopier. Sous la deadline du
    # serveur, le navigateur couperait une requête à laquelle il aurait répondu : la marge est donc
    # strictement positive (`gt=0`), et s'ajoute à `deadline_s` au lieu de la remplacer.
    #
    # **150 s, et non 10 (03/09/2026, story 5.6 T3).** Ce n'est pas la deadline qui a triplé qui
    # déplace cette marge, c'est un **ordre** que l'amendement AD-1 du 03/09/2026 rend explicite
    # dans AD-11 : « délai d'attente du client (`web/app/chat.js`) **>** `--timeout` Cloud Run
    # (**300 s**) **>** deadline serveur ». Les trois délais étaient jusqu'ici rangés autrement — le
    # navigateur abandonnait à 110 s quand Cloud Run coupait à 120 —, et cet ordre-là a un défaut
    # que la deadline courte masquait : une requête tuée par l'**infrastructure** n'était jamais vue
    # comme telle par la page, qui avait déjà coupé la sienne. L'utilisateur lisait « assistant
    # indisponible » sans qu'aucun 503 ni aucune panne réseau ne l'ait causé, c'est-à-dire le repli
    # sans échec réel qu'AD-11 interdit. Le navigateur doit rester en écoute assez longtemps pour
    # **recevoir** le 504 de Cloud Run et le montrer pour ce qu'il est.
    # Dérivation : la patience du client vaut `deadline_s + client_abort_margin_s`, et doit dépasser
    # les 300 s de Cloud Run. 165 + 150 = **315 s**, soit 15 s au-dessus — de quoi laisser la
    # coupure de l'infrastructure et sa réponse traverser le réseau, sans rendre le verrou de saisie
    # éternel si tout se tait.
    #
    # **65 s, et non 150 (03/09/2026, story 5.6 T1c) : c'est la dérivation ci-dessus qui est
    # inchangée, pas la valeur.** Cette marge n'est pas un seuil propre, c'est un **reste** : la
    # patience du client vaut 315 s et la deadline serveur passe à 250 s, donc la marge vaut 65.
    # La laisser à 150 aurait verrouillé la saisie 400 s devant un serveur muet — 100 s après que
    # Cloud Run a coupé —, ce qui n'ajoute rien à ce que la page peut apprendre et retire tout à ce
    # que l'utilisateur peut faire. Ce qui est publié sur `/sante` reste la somme, et elle ne bouge
    # pas : les replis de `web/app/chat.js` (165 + 150) totalisent toujours les mêmes 315 s.
    # **Ce n'est pas une attente** : c'est le délai au bout duquel le navigateur renonce. Une
    # requête normale rend la main en 20 à 30 s, et un 503 est affiché dès qu'il arrive.
    # `tests/test_workflows.py::test_le_timeout_cloud_run_couvre_la_deadline_du_serveur` est le seul
    # endroit où les trois nombres se rencontrent — ils vivent dans trois fichiers qui ne se lisent
    # pas l'un l'autre.
    client_abort_margin_s: float = Field(65.0, gt=0)
    # Story 3.5 : les raisons de quarantaine sont affichées sur deux surfaces publiques
    # (`/sante` et `/documents`). Leur borne est un seuil d'exploitation réglable et publié,
    # pas une propriété du schéma de domaine : une raison plus longue reste conservée en mémoire
    # et dans les journaux, seule sa projection HTTP est abrégée.
    raison_publiable_max_chars: int = Field(RAISON_PUBLIABLE_MAX_DEFAULT, ge=1)

    # Vérification des citations (AD-3)
    quote_min_chars: int = Field(25, ge=1)
    quote_min_ratio: float = Field(0.6, ge=0, le=1)

    # Pipeline guide (story 1.5) : document servi par `pipelines/guide.py` — un slug, jamais un seuil
    # numérique, donc absent de `thresholds()` ; longueur maximale de l'historique accepté (AD-11 :
    # 400 au-delà, **jamais** de troncature côté serveur) ; bornes de *vérifier* (AD-4 « max_claims »
    # de l'appel de pertinence groupé, et sortie maximale de cet appel).
    guide_doc_id: str = "lux-guide"
    # AD-3 nomme les motifs de relance par des défauts de **citation** ; une claim écartée par le seul
    # jugement de pertinence est déjà « conservée dans rejected_claims[] », et relancer *rédiger* pour
    # elle coûte un second appel `reason` (≈ 0,03 €, le tiers du budget). `[HYPOTHÈSE]` : à mesurer
    # avec les questions-témoins (4.2). Le pipeline sinistre possède en plus la règle sémantique
    # d'AD-6 : une claim fondatrice rejetée ne peut jamais être masquée par une auxiliaire survivante.
    relance_sur_non_pertinence: bool = False
    historique_max_turns: int = Field(6, ge=0)
    verifier_max_claims: int = Field(8, ge=1)
    # 1 024 → 3 072 (02/09/2026), et voici la mesure qui déplace ce seuil.
    # **Cause.** Depuis que *vérifier* est servi par le tier `reason` (`verifier_tier`), son appel
    # part avec `EFFORT["reason"] = "medium"` : la réflexion étendue de Sonnet 5 est comptée **dans
    # le même `max_tokens`** que la sortie. Un plafond calibré sur le seul contrat JSON du guide
    # (≈ 440 tokens : 8 verdicts, 8 phrases soutenues, 4 facettes) est donc partagé avec une dépense
    # de réflexion **intermittente**. Mesuré sur les 12 appels enregistrés de ce schéma de sortie :
    # 8 aboutissent (174 → 820 tokens de sortie, dont 3 avec un bloc `thinking`) et **4 sont tronqués
    # à exactement 1 024**, avec pour tout contenu un bloc `thinking` et **zéro caractère de JSON** —
    # c'est-à-dire un `LlmParse`, donc un 503 sur une question parfaitement nominale, ce qu'AD-16
    # refuse. Le témoin de non-troncature est l'étage voisin : `rediger_max_tokens = 2048`, même tier
    # et même effort, 86 appels enregistrés, **aucun** tronqué, maximum observé 1 509.
    # **Valeur.** 3 072, la même que `verifier_sinistre_max_tokens` : ce plafond-là sert déjà le même
    # tier au même effort pour un contrat strictement plus grand. Il laisse au contrat du guide plus
    # de 2 600 tokens de réflexion, soit plus du triple de la plus forte dépense jamais observée sur
    # cet appel. Les deux champs restent **distincts** parce que leurs contrats le sont : si le paquet
    # d'applicabilité du sinistre grandit, le guide n'a pas à suivre.
    # **Coût.** `max_tokens` ne facture pas, il borne — ce qui est payé est la sortie réellement
    # produite. Le seul risque est le majorant de préflight, et il vaut exactement les tokens ajoutés
    # au tarif de sortie du tier servi : +2 048 tokens à 15 USD/MTok (`usd_eur` 0,92) = **+0,0283 €**,
    # quel que soit le corpus. C'est cette identité-là qui est prouvée hors réseau, par
    # `tests/test_pipeline_guide.py::test_le_relevement_du_plafond_ne_coute_que_les_tokens_ajoutes` ;
    # elle rougit si le plafond, le tier de *vérifier* ou la tarification changent.
    # **Ce que la mesure de coût ne dit pas.** Les totaux de bout en bout du témoin sont relevés sur
    # le corpus de fixtures (« Mini guide », 4 blocs) : ils montrent que la chaîne tient, ils ne
    # transposent pas en production, où le préfixe est le guide entier. La marge réelle sous
    # `max_cost_eur_per_request` se mesure en ligne, pas ici.
    # **Ce qui justifie le chiffre**, lui, est la panne supprimée :
    # `test_le_plafond_retenu_ecarte_la_troncature_mesuree` rejoue la troncature enregistrée et finit
    # en `LlmParse` — donc en 503 — tant que le plafond ne dépasse pas la réflexion mesurée.
    # Ce qui reste à confirmer au réenregistrement : qu'aucune réponse ne se tronque plus ici.
    verifier_max_tokens: int = Field(3072, ge=1)

    # Pipeline sinistre (story 1.8, AD-6) : contrat servi par `pipelines/sinistre.py` — un slug, pas un
    # seuil numérique, donc absent de `thresholds()` comme `guide_doc_id`.
    sinistre_doc_id: str = "axa-lu-optihome-2017"
    # D8 de la spec 1.8 : `Verdict.reason`, `ask_client[]` et `escalate[]` sont composés par le code ;
    # seuls les libellés `fait_manquant` viennent du modèle. Ce sont donc les deux seules bornes à
    # poser sur du texte non fiable qui sera **affiché** : sa longueur (au-delà, le libellé est ignoré
    # et la trace le dit — jamais tronqué, une demi-phrase de fait manquant induirait en erreur) et le
    # nombre de questions posées au client. 200 caractères tiennent une question précise
    # (« caractère subit de l'action de la chaleur ») sans ouvrir la porte à un paragraphe.
    fait_manquant_max_chars: int = Field(200, ge=1)
    ask_client_max: int = Field(8, ge=1)
    # Revue Codex 1.8 (B3) : les qualités que la clause exige sont **énumérées** par le modèle et
    # recoupées par le code (`qualites_exigees − qualites_etablies`). Ce sont des libellés du modèle
    # affichables dans `ask_client` : même borne de longueur que `fait_manquant`, plus une borne de
    # nombre par affirmation. Une clause d'assurance subordonne rarement son effet à plus de trois
    # qualités (« soudain », « accidentel », « direct et immédiat ») ; au-delà, le modèle paraphrase.
    qualites_exigees_max: int = Field(4, ge=1)
    # Revue Codex 1.8 (B3, tour 2). Une qualité n'est tenue pour établie que si le fragment des faits
    # que le modèle cite emploie **les mots de la qualité** : mesuré, le modèle citait trois fois le
    # même fragment (« Une bougie allumée posée sur une table basse est tombée sur le canapé ») pour
    # établir « caractère soudain », « action subite de la chaleur » et « contact direct et immédiat
    # avec un foyer » — un fragment authentique qui n'établit aucune des trois. Le recoupement porte
    # sur les mots d'au moins 5 caractères : en dessous, « été », « une », « feu » recouperaient
    # n'importe quoi.
    # Tour 3 de la même revue : le fragment doit employer **tous** les mots porteurs de la qualité, et
    # non un seul. « La chaleur a agi lentement » partage « chaleur » avec « action subite de la
    # chaleur » et dit exactement le contraire ; c'est le qualificatif (*subite*) qui décide.
    qualite_mot_min_chars: int = Field(5, ge=1)
    # L'appel `reason` du sinistre rend tout ce que rend celui du guide **plus** une entrée
    # `applicabilite` par claim décisionnelle. Le partage de `verifier_max_tokens` (1 024) tenait tant
    # que le contrat ne rendait qu'une clause — c'est ce que le run live a montré, et c'est exactement
    # ce qui masquait le problème : à `verifier_max_claims` (8) claims, la sortie tronquée devient un
    # `LlmParse`, donc un sinistre **sans verdict** (AD-16), pour une raison de configuration.
    # Calcul : 8 verdicts de pertinence (~25 tokens), 8 phrases soutenues (~15), 4 facettes (~30) et
    # 8 blocs d'applicabilité ≈ 1 300 tokens, plus la marge de la ponctuation JSON : 2 048.
    # Revue Codex 1.8 (B3, tour 2) : une qualité établie porte désormais **avec elle** le fragment des
    # faits qui l'établit (`fait_cite`, relu par le code). Un bloc d'applicabilité peut donc rendre
    # jusqu'à `qualites_exigees_max` libellés de plus, chacun borné par `fait_manquant_max_chars` —
    # ~90 tokens de plus par qualité établie, soit ~1 200 tokens de plus au pire : 3 072 de **JSON**.
    #
    # **Correctif du tour 2 (rapport rédiger C) : ce calcul ne réservait rien pour la réflexion.**
    # La réflexion étendue de Sonnet 5 est comptée par le fournisseur **dans le même `max_tokens`**
    # que la sortie — c'est exactement pour cela que `verifier_max_tokens` a été corrigé le matin du
    # 02/09/2026, sans que la dérivation sinistre soit reprise. Mesuré sur les 20 appels
    # `verifier_sinistre` audités : la réflexion représente 55 à 91 % de la sortie, **1 904 tokens
    # au maximum observé**, pour 300 à 1 100 caractères de JSON utile. La borne « tenait » par
    # accident : `draft_max_claims = 4` rend inatteignables les 8 claims du calcul, et la moitié de
    # budget ainsi libérée absorbait la réflexion. Elle ne tiendrait plus si `draft_max_claims`
    # montait, et une sortie tronquée est un `LlmParse` terminal — donc un 503 sur un sinistre
    # nominal. La borne est donc `contrat JSON + réserve de réflexion`, sur le patron de
    # `structure_thinking_reserve_tokens`, avec une réflexion **mesurée** et non déduite.
    #
    # `max_tokens` ne facture pas : seul le majorant de préflight bouge.
    #
    # Le contrat JSON est **redimensionné sur ce que le sinistre peut réellement produire** :
    # `draft_max_claims` (4), et non `verifier_max_claims` (8) qu'il ne peut pas atteindre — c'est
    # cette confusion qui donnait 3 072 et qui masquait l'absence de réserve. À quatre claims :
    # 4 verdicts (~25), 4 phrases soutenues (~15), 4 facettes (~30), et 4 blocs d'applicabilité
    # portant chacun jusqu'à `qualites_exigees_max` qualités avec leur `fait_cite` borné par
    # `fait_manquant_max_chars` (~90 tokens la qualité) ≈ 1 880, plus la ponctuation JSON : 2 048.
    #
    # **768 depuis le correctif du tour 3.** Le calcul ci-dessus majorait un contrat que le sinistre
    # ne produit pas : le JSON réellement rendu est de **329 à 510 tokens** sur les quatre appels
    # audités. 2 048 était quatre fois trop grand, et cette place volée à la réserve de réflexion
    # est exactement ce qui la rendait insuffisante. 768 majore le pire mesuré de 50 %.
    #
    # **1 024 depuis le 03/09/2026 (story 5.6, T1c), et ce n'est pas l'effort qui le déplace : c'est
    # le nombre d'affirmations à juger.** 768 était dérivé de `draft_max_claims` **à quatre**. Depuis
    # T1b, la place de l'ébauche servie vaut `navigation_draft_max_claims` = 6. Mesure sur les trois
    # réponses A16 d'après T1b (`automation/runs/20260902-structure-index/a16-t1b/a16-r{1,2,3}.json`,
    # sortie moins réflexion) : **738 / 638 / 484 tokens de JSON pour 5 / 4 / 3 affirmations jugées**,
    # soit ≈ 148 tokens par affirmation au pire. À six : 6 × 148 ≈ 888. 1 024 le majore de 15 %.
    # Le run à cinq affirmations remplissait déjà 96 % de l'ancien contrat : la borne ne tenait plus
    # qu'à ce que le modèle juge moins de claims que la borne ne lui en annonce.
    verifier_sinistre_json_tokens: int = Field(1024, ge=1)
    # 2 048 pour 1 904 mesurés : ~7 % de marge, sur une mesure qui ne couvre qu'un contrat et un
    # cas-témoin. `[HYPOTHÈSE]`, à resserrer quand d'autres cas décisoires auront été joués.
    #
    # La somme vaut **exactement** `llm_max_output_tokens` (4 096), et c'est voulu : le contrôle de
    # cohérence mord désormais. Toute croissance future de `draft_max_claims`, de
    # `qualites_exigees_max` ou de la réflexion mesurée exigera de relever d'abord le plafond du
    # client — au lieu de rogner en silence sur la réflexion, ce qui tronque la sortie et rend un
    # `LlmParse` terminal sur un sinistre nominal.
    # **2 688 depuis le correctif du tour 3.** La mesure du tour 2 (1 904) était périmée de 26 % :
    # l'audit des quatre appels donne 2 337 et 2 394 tokens de réflexion, soit 82 % de la sortie.
    # La réserve était donc **déjà dépassée** au moment où elle a été écrite. 2 688 majore 2 394 de
    # 12 %.
    #
    # **3 072 depuis le 03/09/2026 (story 5.6, T1c), parce que l'effort de l'appel remonte à
    # `medium`** (`llm/models.EFFORT_PAR_PROMPT` : la dérogation `low` est retirée). Deux mesures
    # encadrent la réserve, et aucune ne la donne :
    #   — à `low`, sur la chaîne servie, les trois réponses A16 d'après T1b rendent **1 859 / 2 492 /
    #     1 668** tokens de réflexion (72 à 80 % de la sortie). 2 688 n'en majorait plus le pire que
    #     de 8 % : la réserve était déjà à bout **avant** de relever l'effort ;
    #   — à `medium`, sur le même témoin le 02/09, la réflexion a **saturé** la borne — 3 072 tokens
    #     au total, zéro caractère de JSON, `LlmParse` terminal. C'est une mesure **censurée** : elle
    #     dit ≥ 3 072, pas combien.
    # 3 072 majore de 23 % le pire mesuré à `low`. Surtout, la troncature du 02/09 tient à un
    # **total** de 3 072 partagé avec le JSON, et `max_tokens` est un total : la place dont la
    # réflexion dispose avant de tronquer passe de 3 072 à `verifier_sinistre_max_tokens` = **4 096**,
    # soit un tiers de plus, le contrat JSON ayant en outre sa part propre. La somme vaut de nouveau
    # **exactement** `llm_max_output_tokens` (4 096) : `_coherence` mord, et toute croissance future
    # exigera de relever d'abord le plafond du client au lieu de rogner en silence sur la réflexion.
    # `[HYPOTHÈSE]`, et c'est la plus faible de ce fichier : personne n'a mesuré la réflexion de cet
    # appel à `medium` sur la chaîne de navigation. La campagne `--repeat 3` doit la relever ; si elle
    # dépasse 3 072, c'est `llm_max_output_tokens` **et** `llm_timeout_s` qu'il faudra re-dériver
    # ensemble, jamais la réserve seule.
    verifier_thinking_reserve_tokens: int = Field(3072, ge=0)

    @property
    def verifier_sinistre_max_tokens(self) -> int:
        """Le plafond réellement envoyé : le contrat JSON **plus** la réflexion qu'il faut payer."""
        return self.verifier_sinistre_json_tokens + self.verifier_thinking_reserve_tokens

    # Retrouver (AD-1)
    max_opens: int = Field(6, ge=1)
    # Story 2.3 : le nombre de places **réservées**, parmi `max_opens`, aux nœuds que le profil
    # désigne (`domain/profil.py::noeuds_du_profil`). Ce n'est ni un quota de plus ni un filtre :
    # `max_opens` reste le nombre de nœuds ouverts, et les places réservées sont prises aux
    # **derniers** nœuds retenus, ceux que la question classait le moins bien. À 0, le profil
    # n'ordonne plus rien et *retrouver* se comporte comme avant la story ; à `max_opens`, il
    # pourrait évincer la fiche qui répond. 2 sur 6 est le compromis que l'AC demande — une place,
    # pas la priorité de lecture — et c'est une valeur `[HYPOTHÈSE]`, à régler avec les
    # questions-témoins (4.2) comme `max_opens` lui-même.
    profil_max_opens: int = Field(2, ge=0)
    # Combien d'ouvertures **ciblées** la couverture par facette s'autorise pour *une* facette qui
    # n'a encore aucun bloc décisionnel confirmé — dans *retrouver* comme au second cycle du
    # pipeline. Ce n'est pas une réserve prise sur `max_opens` (contrairement à `profil_max_opens`) :
    # les ouvertures ciblées passent par le **même** quota `max_opens` et les mêmes budgets de blocs
    # et de tokens, et s'arrêtent avec eux. Ce nombre borne seulement l'acharnement sur une facette,
    # pour qu'une sous-question dont le contrat ne parle pas ne consomme pas le quota des autres.
    #
    # Dérivation : le classement de la facette est déjà restreint aux kinds décisionnels **confirmés
    # par le corpus** (`Index.chercher(kinds_confirmes=…)`), donc son premier candidat est la
    # meilleure règle que l'index connaisse pour cette sous-question ; un second essai ne sert qu'au
    # cas où l'unité atomique du premier n'a pas tenu sous le budget de blocs. Au-delà de deux, ce
    # n'est plus le classement qui est mal ordonné, c'est la facette qui n'est pas dans le contrat —
    # et la dire absente est alors la réponse honnête. `[HYPOTHÈSE]`, à régler aux témoins comme
    # `max_opens` lui-même.
    facette_max_opens: int = Field(2, ge=1)
    # Correctif du tour 2 (R1) : la part du budget de lecture que la couverture par facette peut
    # **garder** avant la navigation, pour que l'unité décisionnelle de chaque sous-question ne soit
    # pas mangée par les voisins de fenêtre et les définitions suivies automatiquement — mesuré sur
    # les trois runs A16 : 99,8 % du budget de tokens consommé, dont 39 % de lexique, quand la clause
    # manquante en coûtait 210.
    #
    # C'est une **réallocation bornée**, jamais une capacité de plus : `retrieval_max_blocks` et
    # `retrieval_max_tokens` ne bougent pas. La borne existe pour la même raison que
    # `profil_max_opens < max_opens` — une réserve qui prendrait tout le budget ne classerait plus
    # la lecture, elle la remplacerait, et le navigateur ne rapporterait plus rien de son propre
    # choix. La moitié laisse largement la place : quatre unités décisionnelles du contrat servi
    # coûtent ~1 000 tokens sur 3 500 (29 %), et une seule ~210 (6 %). `[HYPOTHÈSE]`, à régler aux
    # témoins comme les autres bornes de l'étape.
    facette_reserve_max_part: float = Field(0.5, gt=0, le=1)
    # Correctif du tour 3 (R2). Un libellé de facette est une **phrase** ; `Index.chercher` est un
    # lexique strictement littéral. « …par la fumée » ne rencontre donc jamais « Les fumées et les
    # suies », et aucune phrase de facette n'atteint jamais `full_matches > 0` — mesuré sur les six
    # libellés des trois runs A16, sur le contrat servi. La requête de facette ajoute donc à son
    # libellé les **formes de nombre régulières** de ses mots, comme variantes du même canonique.
    #
    # Toutes, ce serait pire que rien : `dommages`, `liés`, `salon` sont des mots que le document
    # porte partout, et une variante d'un mot fréquent est pleinement couverte par des dizaines de
    # blocs — la garde de R1 redeviendrait inerte, et le rang 0 repartirait au bruit (mesuré :
    # 8 à 20 blocs pleins par libellé). Seuls les mots que le document porte **rarement** nomment
    # une clause plutôt que son sujet.
    #
    # Une **part** des blocs, jamais un compte : un document deux fois plus long porterait deux fois
    # plus de blocs pour le même mot. Mesuré sur le contrat servi (1 400 blocs) : `fumées` et
    # `suies` 0,07 %, `bris` 0,57 % — contre `liés` 1,36 %, `dommage` 2,9 %, `dommages` 8,9 %. 1 %
    # sépare les deux familles avec de la marge des deux côtés. `[HYPOTHÈSE]`, à régler aux témoins.
    facette_variante_max_part: float = Field(0.01, gt=0, le=1)
    # Correctif du tour 5 (C8), **le seuil frère du précédent, et il en est bien un**. Le raisonnement
    # est mot pour mot celui de `facette_variante_max_part` : une forme d'**un seul mot** que le
    # document porte partout est pleinement couverte par des dizaines de blocs, et le `full_matches`
    # sur lequel toute la sélection par sous-question repose depuis R1 redevient inerte. La seule
    # différence est la provenance de la forme — dérivée par le code là-bas, **écrite dans le
    # dictionnaire** ici — et c'est précisément pourquoi les deux ne peuvent pas partager un champ :
    # la règle de nombre est calibrée par trois tours de mesure et ne doit plus bouger, tandis que la
    # largeur d'un dictionnaire qui n'a jamais encore été généré se réglera sur les premiers runs.
    #
    # Une variante de **plusieurs** mots n'est pas concernée : une phrase entièrement couverte par un
    # bloc dit quelque chose, quelle que soit la fréquence de ses mots pris un à un. Mesuré sur le
    # contrat servi : `fumee` 0,07 % — un unique bloc, et c'est `p50:18`, le faux positif fondateur —
    # contre `incendie` 1,21 %. Même valeur de départ que la règle de nombre, pour la même raison et
    # sur la même échelle. `[HYPOTHÈSE]`, à régler au premier dictionnaire réellement généré.
    dictionnaire_variante_max_part: float = Field(0.01, gt=0, le=1)
    node_window: int = Field(30, ge=1)
    search_limit: int = Field(20, ge=1)
    # Story 3.3, revue indépendante I3 : une garantie ne peut aspirer qu'un nombre borné de clauses
    # limitatives directement liées. Le seuil lexical s'applique après retrait des mots-outils ; il
    # conserve le témoin chaleur (0,57 mesuré) et rejette les rapprochements fortuits (< 0,12).
    limite_liee_max: int = Field(1, ge=0)
    limite_liee_proximite_min: float = Field(0.35, ge=0, le=1)
    # Lecteur PDF (story 3.4) : le navigateur fournit les block_id canoniques et, facultativement,
    # leurs line_id précis — jamais de coordonnées. La route borne ces listes, puis le renderer
    # rasterise hors event loop, sous concurrence/file/pixels bornés, et garde seulement ce nombre
    # de PNG. La résolution est celle du PNG rendu, jamais celle de l'ingestion/OCR (`ocr_dpi`).
    # `[HYPOTHÈSE]` : une quote de 250 caractères atteint 34 lignes dans le corpus servi ; 40 les
    # couvre avec marge. Le coût de rasterisation reste borné séparément par `pdf_render_max_pixels`.
    pdf_highlight_max_lines: int = Field(40, ge=1)
    pdf_highlight_max_blocks: int = Field(10, ge=1)
    pdf_render_concurrency: int = Field(2, ge=1)
    pdf_render_cache_pages: int = Field(32, ge=1)
    pdf_render_dpi: int = Field(144, ge=72, le=600)
    pdf_render_max_pixels: int = Field(16_000_000, ge=1)
    pdf_render_queue_timeout_s: float = Field(2.0, gt=0)
    # Global à la requête. La chaîne du guide fait **cinq** appels dans son pire cas nominal —
    # *comprendre*, *rédiger*, *vérifier*, puis la relance unique d'AD-3 et la seconde vérification
    # qu'elle exige — plus une relance motivée du client sur un parse invalide (AD-16, « 1 retry »).
    # À 4, le plafond coupait **après** la relance de *rédiger* et **avant** la seconde vérification :
    # un appel avait démarré, l'échec était donc terminal (AD-16) et une question qui déclenchait la
    # relance d'AD-3 ressortait en 503 au lieu de sa réponse vérifiée — mesuré en live, revue Codex
    # 1.5, tour 3. Ce plafond est une ceinture contre l'emballement ; le garde-fou du coût, lui, est
    # `max_cost_eur_per_request`, qui s'applique **avant** qu'un appel démarre (AD-1).
    # Story 2.6 : pire chemin = deux tours de navigation, comprendre, rédiger, vérifier,
    # relance rédiger+vérifier et un retry de parse. Le plafond de coût reste inchangé.
    # Story 4.2e : la reprise d'une demande de contexte coûte **un** appel de plus (la satisfaction,
    # elle, est du code pur). Le pire chemin nominal du sinistre en demande donc neuf, et cette
    # valeur n'est **pas** relevée — les budgets et limites sont hors périmètre de 4.2e. Conséquence
    # assumée et dite : sur le pire chemin, c'est-à-dire une navigation par outils **et** une relance
    # d'AD-3, la reprise est refusée avant tout appel (`reprise_sans_place`) et la réponse acquise
    # est servie sans être donnée pour complète. Le mécanisme reste fail-closed ; ce qu'il perd, ce
    # n'est jamais une garantie, c'est une chance de relire. Relever ce plafond est une décision de
    # coût, mesurable par l'orchestrateur, qui appartient au gate 4.5.
    # **10, et non 9 (02/09/2026, correctif du tour 2).** La séquence la plus longue en consomme
    # exactement neuf — *comprendre*, les **trois** tours de navigation (`max_llm_turns`, dont le
    # tour de conclusion sans lequel aucun verdict de suffisance n'est atteignable), *rédiger*,
    # *vérifier*, la relance d'AD-3 (`APPELS_DE_LA_RELANCE` = 2) et la reprise de 4.2e
    # (`APPELS_DE_LA_REPRISE` = 1). Le plafond est donc `9 + 1`, et ce `+1` est le premier retry
    # motivé d'un parse invalide (AD-16, « 1 retry ») : sans lui, il ressortait en `BudgetExceeded`
    # terminal sur un chemin conforme. C'est délibérément le **minimum** : une unité de plus
    # autoriserait un second retry, c'est-à-dire la porte d'une boucle. Le garde-fou du coût reste
    # ailleurs et s'applique avant chaque envoi (`max_cost_eur_per_request`).
    # **Amendement AD-1 du 03/09/2026 (story 5.6).** La séquence la plus longue n'est plus celle
    # de la variante `outils` : le chemin servi est *comprendre* (1), les tours de navigation
    # (`navigation_max_llm_turns`), l'ébauche terminale rendue dans la même conversation (1),
    # *vérifier* (1), la relance d'AD-3 (`APPELS_DE_LA_RELANCE` = 2, dont la rédaction est un
    # message de plus dans le même fil) et la reprise de 4.2e (`APPELS_DE_LA_REPRISE` = 1), soit
    # 14 — puis le `+1` d'AD-16, le premier retry motivé d'un parse invalide. C'est toujours le
    # **minimum** : une unité de plus autoriserait un second retry, c'est-à-dire la porte d'une
    # boucle. Le garde-fou du coût reste ailleurs et s'applique avant chaque envoi
    # (`max_cost_eur_per_request`), tout comme la deadline.
    max_llm_attempts: int = Field(15, ge=1)
    # Correctif du tour 2 (cause R2/R5). **À deux tours, le verdict terminal de la navigation est
    # inatteignable** : le tour 0 cherche, le tour 1 ouvre, et les résultats du tour 1 ne sont
    # jamais réinjectés (le dialogue s'arrête). Le navigateur ne voit donc jamais ce qu'il a ouvert,
    # ne peut constater aucun manque par sous-question, et ne rend aucun verdict — les trois runs
    # A16 montrent deux appels et une suffisance toujours refusée, donc le bandeau « je n'ai pas pu
    # lire tout ce qui pouvait concerner votre question » sur une réponse parfaitement sourcée.
    #
    # Coût du troisième tour : **un appel `reason` de plus, et seulement quand le navigateur a
    # encore appelé un outil au deuxième**. Mesuré sur les traces A16, l'étape *retrouver* coûte
    # 0,047 € et 8,5-9,9 s pour deux appels ; le tour de conclusion en ajoute donc ~0,015-0,024 € et
    # ~2-5 s, sur une requête à 0,17-0,20 € et 60-74 s — soit ~+10 % de coût et ~+5 % de latence au
    # pire, sur une deadline de 100 s dont 26 à 40 restaient libres. Ce tour n'ouvre pas plus : il
    # reste borné par `max_opens`, `retrieval_max_blocks` et `retrieval_max_tokens`.
    max_llm_turns: int = Field(3, ge=1, le=3)
    # Décision 2.6 mesurée : Haiku réduit le coût de navigation. `reason` reste autorisé pour
    # rejouer l'arbitrage, mais n'est plus le défaut.
    # Le triplet servi vient d'un artefact versionné unique. Les champs restent surchargeables par
    # environnement pour qu'une cellule d'éval exécute ses réglages sans réécrire le défaut.
    # Une nouvelle instance relit l'artefact : après promotion atomique, HTTP, pipeline direct et
    # runner convergent au prochain démarrage/chargement sans dépendre d'une constante importée
    # avant la publication. Les variables d'environnement gardent leur priorité Pydantic normale.
    # « navigation » est le chemin servi depuis l'amendement AD-1 du 03/09/2026 ; les trois autres
    # restent réglables pour rejouer une comparaison, et ne sont plus servies.
    retrieval_variant: Literal["navigation", "deterministe", "outils",
                               "full_context"] = RETRIEVAL_DEFAULT.variant
    retrouver_outils_tier: Literal["micro", "reason"] = RETRIEVAL_DEFAULT.tier
    retrieval_prompt_cache: bool = RETRIEVAL_DEFAULT.prompt_cache
    # Artefact exact réservé aux runners et ingestions hors ligne. L'API en ligne emploie un sink
    # mémoire et ne crée jamais ce fichier (AD-10/AD-15). Rotation et rétention bornent le disque.
    llm_audit_path: Path = REPO_ROOT / ".audit" / "llm-calls.jsonl"
    # Correctif du tour 2 (défaut 9 des trois rapports). **Le témoin qui porte le plancher était le
    # seul chemin sans audit exact.** Le sink `JsonlAuditSink` n'était câblé que dans le runner
    # d'évals ; le service HTTP prenait le défaut `ProjectionAuditSink`, qui n'écrit rien. Les trois
    # runs A16 portent donc `audit_persisted: false` sur chacun de leurs appels, et aucune des trois
    # enquêtes n'a pu produire les termes réellement cherchés, les nœuds ouverts tour par tour ni le
    # verdict du navigateur — tout a dû être déduit ou rejoué hors ligne.
    #
    # L'enveloppe exacte contient question, historique et blocs : elle ne doit jamais quitter la
    # machine (AD-10/AD-15), et le fichier est écrit en 0600 avec taille et rétention bornées. Le
    # défaut suit donc l'environnement — actif hors production, désarmé en production — et reste
    # réglable explicitement dans les deux sens. Ce n'est pas de la donnée publiée : c'est de la
    # donnée **conservée**, et la distinction est exactement celle qu'AD-15 fait déjà.
    llm_audit_exact: bool | None = None
    llm_audit_max_bytes: int = Field(16 * 1024 * 1024, ge=1)
    llm_audit_retention_files: int = Field(4, ge=1)
    retrieval_mechanism_order: str = "dictionnaire,faq,sommaire,outils"
    # Story 4.2b : surcharges de tier **par étape**, pour que la matrice baseline (`micro`/`reason`
    # par étape) soit exécutable à paramètres épinglés. Le mode doit être demandé explicitement :
    # sans lui, le produit servi refuse toute descente des trois étapes au plancher Sonnet.
    baseline_tiers: bool = False
    comprendre_tier: Literal["micro", "reason"] = "reason"
    rediger_tier: Literal["micro", "reason"] = "reason"
    verifier_tier: Literal["micro", "reason"] = "reason"
    retrouver_outils_max_tokens: int = Field(1024, ge=1)
    # Story 1.4 : `RetrievalBudget` borne aussi le nombre de blocs rendus (AD-1 « blocs, tokens inclus »).
    # C'est le seul poste variable du majorant de *rédiger* : préfixe (sommaire au tarif d'écriture 1 h) et
    # sortie à `rediger_max_tokens` en consomment déjà ≈ 0,080 € des 0,10 € par requête ; 6 fiches entières
    # (65 blocs) portaient l'estimation à 0,108 € et faisaient échouer l'appel à tort (`BudgetExceeded`).
    # Revue 2.7 I2 : le compte de blocs n'est pas un budget de coût — deux paragraphes peuvent peser
    # moins qu'un tableau. Il revient donc à la valeur de rappel antérieure ; la coupe adaptative par
    # longueur est portée par `retrieval_max_tokens` juste dessous.
    retrieval_max_blocks: int = Field(30, ge=1)
    # Story 1.4 (revue Codex 1.4, B1) : AD-1 borne l'étape « appels modèle, nœuds, blocs, tokens,
    # définitions et renvois inclus ». Un compte de blocs ne borne pas les tokens — un tableau de fiche
    # pèse dix paragraphes. Faute de tokenizer en code pur, *retrouver* majore avec l'heuristique
    # d'`estimate_cost` (`estimate_chars_per_token`, `estimate_tokenizer_factor`). Valeur : la marge du
    # majorant de *rédiger* (0,10 € − 0,080 € de préfixe et de sortie) au tarif d'entrée `reason`
    # (3 USD/MTok, USD_EUR 0,92) vaut ≈ 7 200 tokens. Revue 2.7 I2 : 3 500 borne aussi les contextes
    # faits de blocs longs et laisse une marge mesurée jusque dans l'enveloppe multilingue « arrivée »
    # aux JSON / identifiants de 30 blocs ; le nombre de blocs ne sert plus de point d'équilibre.
    retrieval_max_tokens: int = Field(3500, ge=1)
    # Correctif du tour 6 (F1). **Une énumération est une unité de lecture, tant qu'elle en reste
    # une.** Les périls d'une même garantie se qualifient les uns les autres — « même lorsqu'il n'y
    # a pas eu embrasement, ni commencement d'incendie » du sixième péril dit quelque chose des cinq
    # autres —, et les lire séparément fait mentir chacun par omission. Au-delà de cette borne,
    # l'unité redevient l'amorce et l'item demandé : c'est le comportement d'avant ce correctif, et
    # il reste juste ; ce qui serait faux est de transmettre un article entier pour une feuille.
    #
    # Mesuré hors ligne sur les documents servis : le contrat AXA porte **22 énumérations** (médiane
    # 6 blocs, maximum 15 ; tokens p50 592, p75 799, p90 1 261, maximum 1 526) ; Baloise et le guide
    # n'en portent **aucune** au sens structurel retenu — la borne n'y change donc rien. L'unité du
    # cas mesuré, « Étendue de la garantie » incendie, vaut **593 tokens** pour 7 blocs.
    #
    # 900 : les 17 énumérations sur 22 qui sont des énumérations de règles passent, les 5 qui sont
    # des articles déguisés (925 à 1 526) sont refusées, et une unité ne peut jamais occuper plus du
    # quart de `retrieval_max_tokens` — deux sous-questions apportant chacune la leur laissent donc
    # plus de la moitié du budget au navigateur. `[HYPOTHÈSE]`, à régler aux témoins.
    enumeration_max_tokens: int = Field(900, ge=1)

    # Coût (AD-9, AD-10).
    # **0,45 €, et non 0,18 (02/09/2026, tour « budgets Sonnet »).** 0,18 € datait du chiffrage fait
    # quand *comprendre*, *retrouver* et *vérifier* étaient servis par `micro` ; il refusait la
    # chaîne servie **avant son premier appel de *vérifier***, donc sur un sinistre parfaitement
    # nominal — un 503 de configuration, ce qu'AD-16 refuse.
    # **Mesure**, chaîne sinistre par outils, corpus AXA réel, prompts, schémas et outils réels,
    # usages enregistrés rejoués au tarif du tier servi (écriture de cache au TTL 1 h de `reason`).
    # Ce que le garde-fou compare est `engagé + majorant` **avant chaque appel** :
    #   — sorties enregistrées, séquence la plus longue (8 appels) : pire somme **0,2699 €** ;
    #   — chaque sortie saturant son `max_tokens` : pire somme **0,4074 €** ;
    #   — majorant froid rigoureux (aucun préfixe jamais relu) : **0,5059 €**.
    # 0,45 € couvre le pire mesuré avec 67 % de marge et le pire « sorties saturées » avec 10 %,
    # tout en restant **11 % sous** le majorant froid : le garde-fou mord donc encore sur une requête
    # réellement anormale, il ne devient pas décoratif. Le témoin
    # `tests/test_sinistre_live.py::test_preflight_outils_nominal_passe_et_un_depassement_reste_refuse`
    # tient les deux moitiés — le nominal passe, un plafond d'un centième de centime trop bas refuse.
    # **0,75 €, et non 0,45 (02/09/2026, 16 h 35, après réingestion d'AXA).** Le chiffrage ci-dessus
    # datait d'un arbre AXA d'environ 300 nœuds. Réingéré avec le parseur courant, AXA porte 751
    # nœuds et son sommaire — qui vit **entier** dans le préfixe cacheable de *rédiger* et de
    # *vérifier* (FR13) — pèse 82 000 caractères. Mesure live, `s-bougie-canape`, chaîne complète :
    # **0,4245 € à froid** (préfixe jamais relu), 0,0925 € à chaud. Le nominal froid passait donc à
    # 0,03 € du plafond : un sommaire à peine plus long (Baloise structuré) ou une sortie un peu plus
    # longue refusait une requête parfaitement nominale, précisément le 503 de configuration qu'AD-16
    # interdit. 0,75 € couvre le froid mesuré avec 77 % de marge et reste sous le double du majorant
    # froid rigoureux recalculé à l'échelle du préfixe (≈ 0,5059 × 82 000 / 43 000 ≈ 0,96 € si tout
    # saturait) : le garde-fou mord encore sur une requête anormale. La cause durable — un préfixe
    # qui grandit avec l'arbre — est consignée en reprise différée (`prefixe-sommaire-borne`) : borner
    # ou trancher le sommaire du préfixe ramènera le froid vers 0,15 € et ce plafond pourra redescendre.
    max_cost_eur_per_request: float = Field(0.75, ge=0)
    # **0,25 €, et non 0,05.** `cout_eleve` est de l'**observabilité** (AD-10), pas un garde-fou : il
    # doit désigner une requête anormale. À 0,05 € il se levait au sortir de *retrouver* — 0,0427 € à
    # son second tour, 0,0548 € avant *rédiger* — c'est-à-dire sur toutes les requêtes, ce qui n'est
    # plus un signal mais du bruit. Bornes mesurées ci-dessus : la chaîne la plus longue jamais
    # enregistrée facture 0,2295 €. 0,25 € se place 9 % au-dessus d'elle et 1,8 fois sous le plafond :
    # l'alerte dit donc « cette requête a dépassé tout ce qu'une chaîne enregistrée a coûté », et elle
    # le dit avant que le plafond ne refuse.
    cost_alert_eur: float = Field(0.25, ge=0)
    # AD-9 : « en évals, le plafond par requête est remplacé par un plafond **par run** (`--max-cost`) ».
    # CLAUDE.md le redit : « les évals tournent seulement avec la clé **et un plafond** ». C'est donc
    # un seuil comme les autres — il vit ici, jamais en dur dans `server/evals/run.py`, et `--max-cost`
    # ne fait que le surcharger pour un run.
    # **7,00 €, et non 1,00 (02/09/2026, tour « budgets Sonnet »).** Ce plafond n'est pas relevé pour
    # lui-même : il est **entraîné** par `max_cost_eur_per_request`, parce que `estimate_run_majorant`
    # chiffre le préflight d'une campagne à `exécutions × plafond par requête` (`llm/pricing.py`). À
    # 0,45 €, toute campagne de trois exécutions ou plus était refusée **avant de commencer** — or
    # les campagnes `--repeat` ≥ 3 et les re-gates sont l'étape suivante du projet.
    # **Mesure.** Le profil `vertical` retient aujourd'hui **cinq** cas (`server/evals/cases/**` :
    # guide 1, sinistre 1, baloise 3 ; `run.py` ne filtre pas le profil `vertical` par document).
    # Une campagne de gate vertical à `--repeat 3` vaut donc 15 exécutions, soit un majorant de
    # 15 × 0,45 = **6,75 €**. 7,00 € la laisse passer avec 3,7 % de marge, pour un coût **réel**
    # attendu d'environ 2,9 € (15 × 0,19 € mesurés) : le plafond reste un facteur 2,4 au-dessus du
    # réel, jamais illimité. Ce qu'il continue de refuser sans `--max-cost` explicite : le même gate
    # à `--repeat` 5 (11,25 €) et le profil `full` (56 cas, 25,2 €). Cette dernière borne est
    # voulue — c'est le cache de réponses d'AD-14 (story 4.1) qui doit ramener le coût du golden set,
    # pas ce plafond qu'on relèverait. `[HYPOTHÈSE]` : à re-régler en 4.1, avec le cache.
    # **12,0 €, et non 7,0 (02/09/2026, 16 h 50).** Même mesure, même règle, plafond par requête
    # passé à 0,75 € (sommaire entier dans le préfixe, voir `max_cost_eur_per_request`) : le gate
    # vertical à `--repeat 3` majore désormais 15 × 0,75 = **11,25 €**. 12,0 € le laisse partir avec
    # 6,7 % de marge, pour un coût réel attendu d'environ 3 à 6 € (0,09 à 0,42 € par exécution selon
    # que le préfixe est chaud ou froid). `--repeat 5` (18,75 €) et `full` restent refusés sans
    # `--max-cost` explicite.
    evals_max_cost_eur: float = Field(12.0, ge=0)
    # Story 4.5 (FR41) — **où vit l'artefact machine des résultats publiés**, relativement à `data/`.
    #
    # Le **nom** est l'unique autorité partagée par l'écrivain (`server/evals/publication.py`) et le
    # lecteur (`server/app/api/etat.py`) : il vit dans `EVALS_PUBLICATION_FILE`, ci-dessus, et les
    # deux le lisent. Deux constantes séparées auraient pu diverger d'un caractère, et la route
    # aurait rendu `publie: false` pour toujours sans que rien ne le dise.
    #
    # Le motif est strict — `[A-Za-z0-9._-]+` — et interdit donc tout séparateur de chemin. Sans
    # lui, `EVALS_PUBLICATION_FILE=../../etc/passwd` ou `sous/dossier.json` faisait lire (et écrire)
    # hors de `data/` : un réglage d'environnement ne doit pas pouvoir choisir un chemin.
    #
    # Il est dans `data/` et non dans `docs/` pour une raison mécanique : `Dockerfile` copie
    # `server data web tools` et **pas** `docs/`. Un `docs/evals/latest.json` serait absent de
    # l'image, et `GET /api/v1/evals/latest` rendrait `publie: false` en production — exactement là
    # où FR41 demande qu'il publie. `data/dictionary.json` est le précédent d'un artefact `data/` qui
    # n'appartient à aucun document. Le rendu **lisible** (`docs/evals/latest.md`) reste dérivé du
    # même objet, pour qui lit le dépôt plutôt que le service.
    #
    # C'est un nom de fichier, pas un seuil : il n'entre pas dans `thresholds()`.
    evals_publication_file: str = Field(EVALS_PUBLICATION_FILE, min_length=1,
                                        pattern=r"^[A-Za-z0-9._-]+$")

    @field_validator("evals_publication_file")
    @classmethod
    def _nom_de_fichier_seul(cls, valeur: str) -> str:
        """`.` et `..` passent le motif mais ne nomment aucun fichier : ils désignent un dossier."""
        if valeur in (".", ".."):
            raise ValueError("evals_publication_file doit nommer un fichier, pas un répertoire")
        return valeur
    # Story 4.2b corrective — plafond **agrégé persistant** de story/campagne, surchargé par
    # `LIVE_BUDGET_EUR`. `--max-cost` reste une borne locale distincte par run.
    # L'orchestrateur fournit `LIVE_CAMPAIGN_ID`; le ledger inter-processus conserve le coût réel
    # entre invocations et refuse toute seconde série baseline/finale par témoin nommé.
    # **7,00 €, et non 1,00 (02/09/2026, tour « budgets Sonnet »).** Cette borne-ci n'était pas au
    # programme du tour : elle a été **trouvée** en vérifiant ce qui casse derrière les autres. Le
    # budget qu'un run confronte à son majorant est `min(--max-cost, LIVE_BUDGET_EUR)`
    # (`evals/run.py`) : tant que celui-ci reste à 1,00 €, relever `evals_max_cost_eur` seul est
    # **inopérant** — une campagne de trois exécutions au nouveau plafond par requête vaut déjà
    # 3 × 0,45 = 1,35 € et se fait refuser avant le premier appel, ce que seize tests de
    # `tests/test_gate_full.py` constatent. Les deux plafonds bougent donc ensemble, à la même
    # valeur et pour la même mesure (voir `evals_max_cost_eur` : gate vertical `--repeat 3`,
    # 15 exécutions, majorant 6,75 €). Ce que ce relèvement ne change pas : l'orchestrateur reste
    # tenu de passer `LIVE_BUDGET_EUR` explicitement (CLAUDE.md, `automation/epreuves-agent.md`) —
    # ce défaut est le filet de celui qui l'oublie, pas l'autorisation de s'en passer.
    # 12,00 € depuis le 02/09/2026 16 h 50, avec `evals_max_cost_eur` : 15 exécutions × 0,75 €.
    live_budget_eur: float = Field(12.00, gt=0)
    live_campaign_id: str | None = Field(None, min_length=1, max_length=128)

    # Client LLM (story 1.3, AD-9) : sortie maximale d'un appel, marge de deadline exigée pour le retry sur parse
    # invalide, heuristique d'estimation avant appel (caractères par token et marge tokenizer, calibrés pour que
    # 2,0/1,3 ≈ 1,54 car./token majore le pire mesuré — 1,65 sur le sommaire du contrat, revue Codex 1.3 B5),
    # délai de `count_tokens`.
    llm_max_output_tokens: int = Field(4096, ge=1)
    llm_retry_margin_s: float = Field(5.0, ge=0)
    # Étapes (story 1.4, NFR4) : sortie maximale par étape — le majorant `estimate_cost` compte la sortie
    # à `max_tokens` ; des plafonds par étape gardent chaque appel sous le plafond par requête (0,18 €).
    comprendre_max_tokens: int = Field(1024, ge=1)
    rediger_max_tokens: int = Field(2048, ge=1)
    # Bornes comportementales annoncées aux prompts des étapes (story 1.4, revue Codex 1.4 I1) : la
    # convention Seuils du spine interdit toute valeur numérique en dur dans une étape — un prompt en
    # est une. `quote_min_chars` est le seuil que *vérifier* appliquera (AD-3) : le prompt le rend
    # littéralement, il ne le duplique pas. Les prompts sont rendus par `prompting.render_prompt`,
    # donc restent déterministes et byte-identiques d'un appel à l'autre (préfixe cacheable, AD-9).
    quote_max_chars: int = Field(250, ge=1)
    # **6 et 9 depuis la story 5.6 T1b (03/09/2026), et la raison n'est pas *rédiger*.** Ces deux
    # champs sont lus par `pipelines/sinistre.py` comme « la place d'une ébauche », **quel que soit
    # l'étage qui l'a produite** : `fusionner_acquis` lève si les acquis dépassent
    # `draft_max_claims`, et les trois gardes « sans place » abandonnent la relance dès que les
    # affirmations retenues les atteignent. Depuis l'amendement AD-1, l'ébauche servie vient de
    # *naviguer* et sa borne propre est `navigation_draft_max_claims` (6) : laisser ces deux-ci à
    # 4 et 6 rendait un `ValueError` atteignable sur un chemin nominal (cinq claims retenues, une
    # relance due, la fusion refuse) et abandonnait la relance sur les ébauches à quatre claims que
    # les réponses A16 rendent déjà. Ils majorent donc la borne du producteur servi — invariant tenu
    # par `_coherence`, qui refuse au démarrage la configuration où ils ne la couvrent plus.
    # La dérivation des valeurs, elle, est celle de la navigation (voir `navigation_draft_max_claims`).
    draft_max_segments: int = Field(9, ge=1)
    draft_max_claims: int = Field(6, ge=1)
    # Story 4.2a : nombre maximal de définitions auxiliaires que la rédaction sinistre rend
    # vérifiables par ébauche. Une définition éclaire la clause décisionnelle sans s'y substituer ;
    # ce seuil de comportement se règle ici, jamais en dur dans l'étape.
    draft_max_definitions: int = Field(1, ge=0)
    question_min_terms: int = Field(2, ge=0)
    question_max_terms: int = Field(6, ge=1)
    # AD-4 : le découpage de la question en sous-questions, rendu par *comprendre*. Borné pour la
    # même raison que `verifier_max_claims` — un découpage qui s'emballe rendrait `complete` hors
    # d'atteinte et gonflerait le prompt de *vérifier* sans rien prouver.
    question_max_facettes: int = Field(4, ge=1)
    # Story 1.9 (revue, tour 2) : le nombre de thèmes de `ParsedQuestion.scope` retenus pour
    # l'affichage. `QuestionScope.borner()` bornait la **longueur** de chaque libellé mais pas leur
    # nombre : deux cents thèmes courts passaient tous, et la page les joint en une seule ligne sous
    # « Ce que j'ai compris du sinistre ». Convention Seuils — un nombre se règle avec les évals,
    # une forme de contrat non. Six couvre largement un sinistre d'habitation ; au-delà, le modèle
    # ne classe plus, il énumère.
    scope_max_themes: int = Field(6, ge=1)
    # Longueur d'**un** libellé rendu par *comprendre* (`terms`, `themes`, `facettes`). Revue Codex
    # 2.1 (M3), reprise en story 2.2 : la valeur vivait en dur dans `steps/comprendre.py`, ce que la
    # Convention Seuils interdit. Elle en est bien un — c'est le **code** qui l'applique, elle se
    # règle sur ce qu'on observe des termes utiles, et elle est publiée dans `Trace.thresholds`. Sa
    # jumelle de **nombre** est `LISTE_MAX_ITEMS`, en tête de ce fichier depuis la revue Codex 2.2
    # (I2) : elle aussi se règle et se publie, mais en constante de module, parce qu'elle entre dans
    # le schéma JSON envoyé au modèle (AD-9) et qu'un `.env` la ferait varier d'un poste à l'autre.
    #
    # Volontairement plus haute que les bornes d'affichage (`fait_manquant_max_chars`) : celles-là
    # sont plus fines et se disent en trace. Au-delà d'ici, ce n'est plus un terme, c'est un
    # déversement — le libellé est **écarté**, jamais coupé (un terme tronqué se chercherait, et se
    # publierait dans `terms_searched`, sous une forme que personne n'a écrite).
    libelle_max_chars: int = Field(500, ge=1)
    estimate_chars_per_token: float = Field(2.0, gt=0)
    estimate_tokenizer_factor: float = Field(1.3, gt=0)
    count_tokens_timeout_s: float = Field(10.0, gt=0)

    # Sommaires (story 1.3, FR13) : compactage décidé sur la mesure au tokenizer réel (docs/tests-live.md).
    # Guide : résumés tronqués et tags limités ; contrat : nœuds de niveau <= summary_max_level.
    summary_max_tags: int = Field(5, ge=1)
    summary_resume_max_chars: int = Field(90, ge=10)
    summary_max_level: int = Field(2, ge=1)
    # Correctif G2 — **la carte servie au navigateur se dérive du document et du budget, jamais
    # d'un nombre d'entrées fixe.** Une taille de page constante (40) traitait de la même façon un
    # contrat profond de 750 nœuds longs et un guide plat de 87 fiches courtes : le second n'était
    # plus vu qu'aux 40 premières entrées, et les suivantes n'étaient plus atteignables que par
    # `chercher` — avec un seul tour outillé pour paginer, chercher et ouvrir (`max_llm_turns`).
    # Ces deux seuils sont un **budget**, pas une taille : `Index.sommaire_page` en déduit, pour
    # chaque document, combien d'entrées tiennent dans une page et quelle longueur d'aperçu chacune
    # peut porter. Un document plat et large reçoit donc sa carte entière avec son signal ; un
    # document vaste reçoit une carte compacte paginée.
    #
    # **Deux budgets, parce qu'il y a deux régimes** (`Index._mise_en_page_du_sommaire`).
    #
    # `summary_page_max_chars` — le budget d'une carte **complète** : le document entier, avec son
    # aperçu, dans le préfixe cacheable de navigation. Il est cher (~4 250 tokens à l'heuristique du
    # dépôt) et il vaut son prix, parce qu'il **remplace la pagination** : le navigateur voit tout
    # le document en un tour, ce qui compte quand `max_llm_turns` n'en laisse qu'un d'outillé. Le
    # guide (87 fiches, 16 008 caractères mesurés) entre dedans.
    summary_page_max_chars: int = Field(17000, ge=200)
    # `summary_slice_max_chars` — le budget d'une carte **partielle**, pour un document qui ne tient
    # pas. Elle ne remplace rien : quelle que soit sa taille, le navigateur devra chercher, et le
    # préfixe est payé à **chaque** requête. Elle est donc bornée plus bas que la carte complète.
    #
    # **La valeur de 6 000 reposait sur une affirmation fausse**, et elle est corrigée ici. Le
    # commentaire précédent disait que le contrat AXA « n'a pas de marge » sur
    # `max_cost_eur_per_request`, et citait 0,4502 € contre 0,4500 € de plafond. Remesuré sur le
    # corpus servi, le chiffre n'est pas reproductible, et surtout **le raisonnement était faux** :
    # la carte n'entre que dans le préfixe de l'appel de **navigation**, et cet appel n'est pas le
    # plus cher de la chaîne. Le plus cher est *vérifier*, qui porte les blocs retrouvés et **ne
    # contient aucune carte** — sa somme ne bouge pas d'un centime quand la tranche décuple.
    #
    # Mesure du 02/09/2026, chaîne sinistre réelle jouée hors ligne, `estimate_cost` relevé à chaque
    # appel (`engagé + estimé`, le majorant que le client oppose avant d'envoyer) :
    #
    #     tranche   nœuds servis   appel de navigation   pire appel de la chaîne
    #       6 000      44 / 750           0,1402 €              0,4175 €
    #      16 008     118 / 750           0,1708 €              0,4175 €
    #      30 000     222 / 750           0,2265 €              0,4175 €
    #      85 000     629 / 750           0,4459 €              0,4459 €   ← marge +0,0041 €
    #      88 000     651 / 750           0,4566 €              0,4566 €   ← plafond franchi
    #
    # Le plafond n'est donc franchi qu'aux environs de **86 000 caractères**, quand la navigation
    # dépasse enfin *vérifier*. **30 000 est le tiers de ce point de franchissement** : la marge est
    # explicite et vaut un facteur 3, l'appel de navigation reste à la moitié du plafond, et le
    # navigateur du contrat voit 222 nœuds sur 750 au lieu de 44. Deux témoins la tiennent des deux
    # côtés dans `tests/test_index.py` — l'un rougit si la tranche redevient plus étroite que la
    # mesure ne l'autorise, l'autre si elle approche le plafond.
    summary_slice_max_chars: int = Field(30000, ge=200)
    # Longueur maximale de l'aperçu d'une entrée — le texte du premier bloc citable de son nœud,
    # c'est-à-dire le signal de navigation que le document porte lui-même (pour le guide, le résumé
    # de la fiche). Plafond, pas valeur servie : la longueur réelle est dérivée par document.
    summary_apercu_max_chars: int = Field(90, ge=0)
    # Longueur maximale d'un extrait de `chercher` (`ScoredHit.excerpt`). Vivait en dur dans
    # `Index.__init__` : convention Seuils — un nombre vit ici et se publie.
    excerpt_max_chars: int = Field(1000, ge=1)

    # Limiteur best-effort par instance (AD-13)
    rate_limit_per_minute: int = Field(10, ge=1)
    rate_limit_per_day: int = Field(100, ge=1)
    # Les suivis conversationnels ne dépensent aucun appel fournisseur, mais restent bornés contre
    # le flood. Leur compteur distinct évite qu'une exploration légitime épuise le quota des
    # premiers tours payants, tout en réutilisant exactement les mêmes fenêtres et l'identité AD-13.
    conversation_rate_limit_per_minute: int = Field(30, ge=1)
    conversation_rate_limit_per_day: int = Field(300, ge=1)
    conversation_max_turns: int = Field(20, ge=1)
    conversation_active_questions_max: int = Field(3, ge=2, le=3)
    # Story 1.6 — nombre maximal d'identités clientes suivies simultanément par le limiteur. Le
    # limiteur vit en mémoire de process (AD-13 : best-effort par instance) ; sans borne, une adresse
    # forgée par requête ferait grossir la table jusqu'à la mémoire du conteneur. Au-delà, la plus
    # ancienne identité vue est évincée — elle repart donc à zéro, ce qui est la limite assumée d'un
    # limiteur best-effort. 4 096 identités ≈ quelques centaines de ko, très au-dessus du trafic
    # d'une démonstration servie par une seule instance.
    rate_limit_max_clients: int = Field(4096, ge=1)
    # Borne haute du `Retry-After` annoncé sur un 429 (AD-13). La valeur exacte serait le temps
    # restant de la fenêtre dépassée ; sur la fenêtre **journalière**, cela peut faire des heures, et
    # annoncer 80 000 s n'aide personne. On annonce donc au plus `retry_after_s` : le client revient
    # à un rythme raisonnable et reçoit un nouveau 429 tant que sa fenêtre n'est pas retombée —
    # `Retry-After` est une indication, pas une promesse.
    retry_after_s: int = Field(60, ge=1)
    # Story 1.6 — taille maximale du corps HTTP accepté (AD-16 `413 input_too_long`), vérifiée sur
    # `Content-Length` **avant** toute lecture du corps. Le contrat d'AD-11 tient très en dessous :
    # question ≤ 1 000 caractères + 6 tours ≤ 2 000 + profil, soit ≤ 13 000 caractères ≈ 52 ko en
    # pire cas UTF-8 sur quatre octets. 65 536 laisse la marge du JSON (guillemets, échappements)
    # sans ouvrir la porte à un corps que le serveur lirait entièrement pour rien.
    request_max_bytes: int = Field(65536, ge=1)
    # Story 1.6 — longueur maximale du `X-Cloud-Trace-Context` recopié dans la ligne de log (AD-10).
    # C'est une valeur **cliente** : Cloud Run en pose une de quelques dizaines d'octets, n'importe
    # qui peut en poster une de plusieurs kilos, et ce serait alors le journal qu'on ferait grossir à
    # sa place. Elle est ici, et publiée dans `thresholds()`, parce que c'est un seuil numérique
    # opérationnel comme les autres (revue Codex 1.6, M2) — pas une constante de protocole.
    cloud_trace_max_chars: int = Field(128, ge=1)

    # Ingestion (AD-8 / story 3.1). Ces valeurs sont des hypothèses de détection, pas des
    # vérités métier : elles sont exposées dans `thresholds()` et les valeurs qui modifient
    # l'extraction ou la segmentation entrent aussi dans `ingest_fingerprint`.
    coverage_threshold: float = Field(0.8, ge=0, le=1)
    kind_confidence_min: float = Field(0.7, ge=0, le=1)
    mixed_page_image_density: float = Field(0.2, ge=0, le=1)
    ocr_dpi: int = Field(300, ge=72, le=600)
    quality_min_words: int = Field(12, ge=1)
    foreign_signal_min: int = Field(3, ge=1)
    french_signal_ratio_min: float = Field(0.08, ge=0, le=1)
    gibberish_ratio_max: float = Field(0.35, ge=0, le=1)
    residual_header_min_pages_ratio: float = Field(0.3, ge=0, le=1)
    # Géométrie de TdM (revue 3.1) : alignement du numéro de page, colonne d'entrées,
    # retrait d'une continuation, interligne maximal et préfixe minimal accepté par le rapport.
    toc_page_number_baseline_pt: float = Field(8.0, ge=0)
    toc_column_tolerance_pt: float = Field(80.0, ge=0)
    toc_indent_tolerance_pt: float = Field(5.0, ge=0)
    toc_line_gap_ratio: float = Field(1.5, gt=0)
    toc_title_prefix_min_chars: int = Field(20, ge=1)

    # Colonnes du corps (story 4.2c, AD-2 / AD-8). La table des matières avait déjà sa géométrie de
    # colonne (`toc_column_tolerance_pt`) ; le **corps** n'en avait aucune, si bien que l'ordre de
    # `get_text(sort=True)` entrelaçait deux colonnes en une seule suite de lignes. Ces trois bornes
    # décrivent une gouttière, jamais une mise en page particulière : elles n'ont ni nombre de
    # colonnes, ni position, ni document dans leur énoncé. Elles entrent dans `ingest_fingerprint`
    # parce qu'elles changent l'ordre de lecture, donc `seq`, donc les `block_id` (AD-2, stabilité).
    # Aucune gouttière retenue ⇒ l'ordre de lecture reste l'ordre d'extraction, à l'octet.
    # Largeur minimale du blanc vertical qui sépare deux colonnes. Une gouttière imprimée fait
    # couramment 20 à 40 pt ; 18 pt (≈ 6 mm) reste sous toute gouttière réelle et très au-dessus
    # d'une espace entre deux mots, qui ne peut donc jamais être prise pour une séparation.
    column_gutter_min_pt: float = Field(18.0, gt=0)
    # Nombre minimal de boîtes **entièrement** d'un côté de la gouttière, de chaque côté. Deux
    # étiquettes isolées de part et d'autre d'un blanc ne font pas deux colonnes. Une boîte est une
    # ligne de texte **ou** une rangée de table : une table est du contenu écrit, et la compter pour
    # un objet unique laissait une colonne entièrement tabulaire sous ce seuil — la page se lisait
    # alors en rangées. La table reste un bloc atomique : ses rangées entrent à l'aplomb de sa boîte,
    # si bien qu'aucune frontière ne peut la couper (elle est d'un côté, ou traversante).
    column_min_lines: int = Field(4, ge=2)
    # Part de la hauteur écrite que chaque côté doit couvrir. Une colonne est haute ; un encadré
    # local, un pied de tableau ou une paire de légendes ne le sont pas. La hauteur écrite compte
    # les tables : un côté qui ne couvre pas une page dominée par un tableau n'est pas une colonne.
    column_min_span_ratio: float = Field(0.35, gt=0, le=1)
    # Appariement maximal des lignes de base entre les deux côtés d'une gouttière, au-delà duquel
    # celle-ci n'est **pas** retenue. Mesuré en revue : une liste « libellé … montant » — huit
    # libellés à gauche, huit montants à droite, sur les **mêmes** lignes de base, que
    # `find_tables()` ne voit pas — satisfaisait les trois critères précédents et se lisait comme
    # deux colonnes, séparant chaque montant de son libellé. Un appariement mutuel des lignes de
    # base est la signature d'une **rangée**, pas d'une colonne de lecture : deux colonnes de texte
    # dérivent l'une de l'autre dès qu'un paragraphe s'achève.
    # `[HYPOTHÈSE]` — la tolérance verticale de l'appariement est `baseline_tolerance_pt`, déjà la
    # définition de « même ligne de base » ailleurs dans le parseur. Cette part **ne suffit pas** à
    # elle seule à écarter une gouttière : une mise en page professionnelle à deux colonnes partage
    # très souvent la même grille de lignes de base d'un bout à l'autre, si bien qu'un appariement
    # élevé y est la règle, pas l'exception. Employée seule, la garde annulait donc la correction
    # sur les documents mêmes qu'elle vise. Elle n'est retenue qu'en **conjonction** avec
    # `column_min_fill_ratio` ci-dessous, qui sépare la rangée de la colonne sur un autre axe.
    column_row_pairing_max_ratio: float = Field(0.5, gt=0, le=1)
    # Remplissage minimal du côté le **moins** rempli d'une gouttière : la part de la largeur dont ce
    # côté dispose qu'il occupe réellement. La largeur disponible va de la marge de texte de la page
    # au bord **opposé** de la gouttière — jamais l'étendue des lignes du côté lui-même, qui vaudrait
    # 1 par construction et ne mesurerait rien. Une colonne de texte remplit sa largeur utile ; une
    # colonne de montants alignés à droite n'en occupe qu'une fraction. Avec une gouttière de 20 à
    # 60 pt sur une largeur de texte A4, une vraie colonne se remplit à 0,73 – 0,92 (le blanc de la
    # gouttière est le seul creux qu'elle laisse), là où une colonne de montants tombe sous 0,3 :
    # 0,6 sépare les deux familles avec de la marge de chaque côté. Le critère est géométrique et
    # sans énoncé propre à un document ; il entre dans `ingest_fingerprint` au même titre que les
    # autres bornes de colonne.
    # Cette garde à deux signaux ne s'applique qu'aux **lignes**, jamais aux rangées d'une table :
    # elle protège une rangée que `find_tables()` n'a pas vue, dont rien d'autre ne tient ensemble le
    # libellé et le montant. Une rangée détectée est déjà un bloc atomique — ses cellules sont dans
    # le même bloc `table`, qu'aucune gouttière ne disjoint — et la grille régulière d'un tableau,
    # appariée par construction, aurait écarté les colonnes voisines si on l'y avait fait voter.
    column_min_fill_ratio: float = Field(0.6, gt=0, le=1)

    # Structure proposée puis vérifiée (story 4.2c, AD-2 / AD-7 / AD-16). Le tier `ingest` propose
    # une hiérarchie **sur des uid de lignes source** ; le code la prouve ou la refuse. Ces bornes
    # sont appliquées par le vérificateur, hors réseau : elles ne dépendent d'aucun document et un
    # refus est bloquant (quarantaine), jamais un repli silencieux vers l'heuristique numérique.
    # Profondeur maximale d'un arbre proposé. Au-delà, la « hiérarchie » n'est plus une structure
    # lisible mais une chaîne que rien ne peut vérifier à l'œil.
    structure_max_depth: int = Field(6, ge=1)
    # Largeur de l'arbre proposé : nombre total de nœuds, et nombre d'enfants d'un même parent —
    # **racines comprises**, une proposition « plate » de N nœuds sans parent étant une largeur de N.
    # La profondeur était bornée, la largeur ne l'était pas : `verifier()` porte une boucle en O(n²)
    # sur les intervalles, et un `structure.json` très large faisait donc travailler indéfiniment un
    # chemin dont toute la valeur est d'être fail-closed et déterministe. Un refus est nommé et
    # bloquant (quarantaine), jamais un rognage silencieux de la proposition.
    # **Mesuré, et non supposé** : les trois documents déjà ingérés portent 751, 88 et 2 nœuds
    # (`data/*/document.json`), et leur fratrie la plus large en compte 87 (les racines du guide),
    # puis 54. Les deux bornes sont réglées à ~2,7 fois le maximum mesuré ; elles restent en deçà du
    # nombre de lignes source d'un contrat réel (4 214 et 4 802 mesurées), ce qui est voulu : un
    # document dont la moitié des lignes seraient des titres n'est pas une hiérarchie.
    structure_max_nodes: int = Field(2000, ge=1)
    structure_max_children: int = Field(256, ge=1)
    structure_max_blocks_per_leaf: int = Field(100, ge=1)
    # Part minimale des lignes du registre que les intervalles proposés doivent couvrir. L'AC exige
    # une borne de couverture **explicite** : elle est nommée ici, publiée par `thresholds()` et
    # documentée. Son défaut est `1.0` parce que l'AC exige aussi que « toute ligne omise » mette le
    # document en quarantaine — une proposition qui laisse une ligne hors de tout nœud ne structure
    # pas le document, elle en structure un extrait, et le reste serait servi sous un nœud voisin que
    # personne n'a prouvé. Mesuré en revue : à 0,9, dix lignes dont neuf couvertes rendaient
    # `accepte=True`. Ce réglage ne peut donc que **durcir** la règle par uid du vérificateur, jamais
    # la desserrer : l'abaisser ne rouvre aucun trou, chaque ligne non couverte reste un refus
    # `ligne_omise` (prouvé par `test_abaisser_la_borne_de_couverture_ne_rouvre_pas_la_ligne_omise`).
    structure_min_coverage: float = Field(1.0, ge=0, le=1)
    # Longueur maximale d'une **étiquette** : une ligne qui nomme, adresse ou identifie, par
    # opposition à une phrase. Sert à reconnaître le cartouche légal qui clôt certains contrats —
    # raison sociale, siège, registre du commerce — et qui suit le dernier corps de la dernière page
    # portant du texte. La porte de lecture le rend alors non citable, comme la couverture : c'est
    # la même notion de provenance, prise par l'autre bout du document.
    # **Mesuré, et non supposé** : les deux lignes du cartouche du contrat le plus long du corpus
    # font 124 et 118 caractères et ne se terminent par aucune ponctuation de phrase, quand le
    # dernier alinéa de corps qui les précède finit par un point. La borne à 160 est celle que
    # `structure._candidates_ancres` emploie déjà pour la même question de forme — « ceci
    # ressemble-t-il à un intitulé plutôt qu'à une phrase ? ». Les deux doivent rester égales ;
    # les unifier changerait la charge utile envoyée au fournisseur, donc la relecture des réponses
    # archivées, et n'appartient pas à ce tour.
    etiquette_max_chars: int = Field(160, ge=1)
    # Bornes de structure hors ligne. `structure_max_input_chars` borne chaque charge utile
    # segmentée **et** l'artefact global relu. Le planificateur mesure en plus l'enveloppe sérialisée
    # complète (prompt, message, ancres de frontière bornées et schéma) contre
    # `MODEL_CAPS.context_window`, via une borne supérieure en octets UTF-8 et sortie maximale
    # comprise. Il choisit des segments contigus aux frontières d'unités de portage avant le premier
    # appel, puis le préflight additionne leurs majorants non arrondis. Cette valeur n'est donc jamais
    # relevée pour faire tenir artificiellement un monolithe fournisseur. La réponse ne porte que des
    # uid et des liens, jamais du texte, et la couture globale reste fail-closed.
    structure_max_input_chars: int = Field(900000, ge=1)
    structure_max_output_tokens: int = Field(16000, ge=1)
    # Capacité de sortie d'un segment : **dérivée de sa taille**, jamais supposée constante. Le
    # planificateur ne bornait un segment que par son entrée ; la sortie disponible était le plafond
    # fixe ci-dessus, qui suffisait par accident tant que le schéma énumératif tenait les segments
    # petits. Le schéma constant les a fait grossir à ≈ 1 200 lignes et le premier appel réel a été
    # perdu en entier sur `stop_reason='max_tokens'` — 0,7654 € pour rien.
    #
    # Les deux valeurs sont **mesurées** sur cet appel (Baloise, segment 1, 1 226 lignes,
    # `effort=high`, 02/09/2026, `output_tokens=16 000` dont `thinking_tokens=7 047`) :
    #
    # - la réponse a rendu 19 355 caractères de JSON (≈ 8 950 tokens) décrivant 93 nœuds jusqu'à la
    #   ligne 1 011 du segment, soit **≈ 8,9 tokens de JSON par ligne**. `12.0` majore ce ratio de
    #   ~35 % : la mesure vient d'un seul segment d'un seul contrat, et une zone dense — plus de
    #   titres pour le même nombre de lignes — rend davantage de nœuds. Le prix d'un majorant trop
    #   large est des segments plus petits, pas un appel perdu.
    # - la réflexion est comptée **dans** `max_tokens` par le fournisseur, et n'est donc jamais
    #   disponible pour le JSON. Mesurée à 7 047 tokens, réservée à `8000` (~13 % de marge). Sans
    #   cette réserve, chaque segment est court de la taille de sa propre réflexion.
    #
    # Un segment dont la sortie attendue dépasse `structure_max_output_tokens` n'est pas admissible,
    # même si son entrée tient dans la fenêtre : c'est cette règle, et non le hasard des tailles, qui
    # décide du découpage.
    structure_output_tokens_per_line: float = Field(12.0, gt=0)
    structure_thinking_reserve_tokens: int = Field(8000, ge=0)
    # Scissions adaptatives autorisées dans un run, toutes causes confondues (contexte refusé par le
    # fournisseur, ou réponse interrompue). Une réponse interrompue n'est plus un refus terminal : le
    # segment est scindé en deux aux frontières de portage et resoumis, le coût de l'appel perdu
    # entrant au cumul. La borne est ce qui empêche un contrat inconnu, dont une zone déborderait
    # systématiquement, de faire resoumettre le run sans fin en payant à chaque tour. Quatre
    # scissions divisent au pire un segment par seize, très au-delà du dépassement mesuré (≈ 18 000
    # tokens attendus pour 16 000 disponibles, soit un facteur 1,13) ; au-delà, le refus est nommé et
    # dit le coût acquis.
    structure_max_refinements: int = Field(4, ge=0)
    # Majorant vérifié **avant** toute construction de client (idiome `type_clauses`).
    # **Valeur.** Mesuré le 2026-09-02 sur les deux contrats réels avec la sortie dérivée
    # (7 segments de ≤ 666 lignes) : majorant Baloise **5,99 €**, AXA **6,28 €** — le 5,0 € initial
    # avait été posé avant toute exécution réelle et refusait les deux documents avant le premier
    # appel. 8,0 € couvre le plus lourd des deux avec ≈ 27 % de marge (une scission adaptative ajoute
    # au plus le coût d'un appel interrompu) et reste un plafond, pas une dépense : le coût réel d'un
    # run entier Baloise est attendu ≈ 3,5 € (réflexion mesurée 3 500-7 000 tokens contre 16 000
    # majorés). `--max-cost` le surcharge ponctuellement.
    structure_max_cost_eur: float = Field(8.0, gt=0)

    # Dictionnaire enrichi (story 2.1, AD-5 / AD-7). Toutes ces bornes s'appliquent **par le code**
    # à ce que le modèle d'ingestion rend : AD-5 et AD-7 disent qu'il ne renvoie jamais de texte de
    # bloc, et le code le vérifie plutôt que de le croire. Une chaîne hors borne est **écartée**,
    # jamais tronquée — un terme amputé chercherait autre chose que ce que le modèle a voulu dire.
    # `dictionary_term_max_words` sert deux fois : il borne la longueur d'un terme **et** il est la
    # ligne de partage du contrôle « chaîne recopiée d'un bloc » — au-delà de quatre mots, une chaîne
    # qui figure telle quelle dans un bloc est un passage du guide, pas un terme du domaine.
    dictionary_term_max_chars: int = Field(60, ge=1)
    dictionary_term_max_words: int = Field(4, ge=1)
    dictionary_max_variants_per_term: int = Field(8, ge=1)
    dictionary_max_terms_per_fiche: int = Field(20, ge=1)
    dictionary_question_max_chars: int = Field(160, ge=1)
    dictionary_max_questions_per_fiche: int = Field(5, ge=1)
    dictionary_max_intent_triggers: int = Field(30, ge=1)
    # Un contrat sans hiérarchie exploitable est enrichi depuis des unités de **vrais blocs**,
    # jamais depuis des fiches inventées. Ces quatre bornes dimensionnent séparément leur entrée et
    # leur sortie : le gros plafond historique reste nécessaire à la catégorie FAQ du guide.
    dictionary_flat_max_blocks_per_request: int = Field(20, ge=1)
    dictionary_flat_max_input_chars: int = Field(12000, ge=1)
    dictionary_flat_max_terms_per_block: int = Field(3, ge=1)
    dictionary_flat_max_output_tokens: int = Field(4096, ge=1)
    # Sortie maximale d'une requête de batch. Elle n'est **pas** bornée par `llm_max_output_tokens` :
    # celui-ci borne les appels du **serveur**, qui vivent sous la deadline et le plafond par requête
    # (AD-9) ; l'ingestion est hors ligne, en Batch API, et son majorant est le plafond de coût
    # ci-dessous.
    # **Mesuré, et non supposé** (revue coordonnée 2.1) : les catégories du guide portent de 2 à
    # **41** fiches — « Questions fréquentes » les regroupe toutes —, pas « jusqu'à sept » comme
    # l'écrivait ce commentaire. C'est cette catégorie qui dimensionne le seuil, et c'est elle qui
    # sera coupée la première. La borne n'est donc plus le seul garde-fou : une réponse dont le
    # `stop_reason` vaut `max_tokens` est traitée par `ingest/enrich_dictionary.executer` comme une
    # **requête en échec**, nommée dans la sortie — une catégorie écartée et dite vaut mieux qu'une
    # catégorie disparue en silence avec un code de sortie 0.
    dictionary_max_output_tokens: int = Field(16000, ge=1)
    # Majorant du run entier, vérifié **avant** toute soumission (le run refuse de démarrer plutôt
    # que de découvrir la facture après coup). 3 € laissent la marge d'un guide qui doublerait de
    # taille : le majorant mesuré du guide livré est très en dessous (voir `--dry-run`).
    dictionary_max_cost_eur: float = Field(3.0, gt=0)
    dictionary_batch_poll_s: float = Field(20.0, gt=0)
    dictionary_batch_timeout_s: float = Field(3600.0, gt=0)
    # Palier de la campagne d'enrichissement (AD-9 : « une table des tiers, une affectation étape →
    # tier », et cette affectation se **lit dans la configuration**, elle n'est pas codée en dur dans
    # `ingest/enrich_dictionary`). Le défaut reste `ingest` — Opus 5, l'affectation `ingest/* →
    # ingest` du spine : rien ne change sans surcharge explicite par `DICTIONARY_TIER`.
    #
    # **Dérivation de la valeur, et de l'ensemble admissible.** L'enrichissement est une tâche
    # lexicale : nommer les mots par lesquels un bloc se cherche. C'est un choix sémantique, et
    # l'amendement Epic 5 d'AD-9 du 02/09/2026 en fixe le plancher — « Sonnet est le plancher de tout
    # choix sémantique ; Haiku reste un axe d'évaluation non promu ». `micro` est donc exclu du
    # littéral plutôt que laissé au réglage : un palier sous le plancher n'est pas une option de
    # configuration, c'est une violation d'AD-9 qu'aucun `.env` ne doit pouvoir écrire. Les deux
    # paliers restants acceptent tous deux `output_config.effort` (`MODEL_CAPS`), donc `EFFORT` les
    # couvre l'un et l'autre et le corps d'appel reste le même.
    #
    # **Ce que la surcharge rend possible, mesuré le 03/09/2026.** `axa-lu-optihome-2017` porte 1 392
    # blocs citables : à 20 blocs par requête, 71 appels. Au palier `ingest`, leur majorant vaut
    # 9,68 € — au-dessus de tout plafond raisonnable pour un dictionnaire de recherche. Le palier est
    # donc ce qui manque pour que la campagne tienne sous son plafond, et il se règle sans toucher au
    # code ni au défaut servi.
    dictionary_tier: Literal["ingest", "reason"] = "ingest"

    # Typage des clauses (story 3.2, AD-2 / AD-7 / AD-8). Une première lecture couvre tous les
    # blocs citables, puis une seconde relit seulement les kinds juridiques. Le regroupement est
    # borné à la fois en blocs et en caractères : aucune taille moyenne de paragraphe n'est supposée.
    type_clauses_max_blocks_per_request: int = Field(10, ge=1)
    type_clauses_max_input_chars: int = Field(60000, ge=1)
    type_clauses_max_requests_per_batch: int = Field(1000, ge=1)
    type_clauses_max_output_tokens: int = Field(2048, ge=1)
    # Majorant des **deux** lectures, vérifié avant la première soumission avec le pire cas où tous
    # les blocs seraient juridiques. Le coût publié après exécution vient toujours de l'usage API.
    type_clauses_max_cost_eur: float = Field(12.0, gt=0)
    # Confiance minimale exigée de l'**arbitre**, et d'elle seule, dans le seul cas où il départage
    # un vrai désaccord : deux lectures qui se contredisent sur le `kind`, et un arbitre qui tranche
    # contre l'une d'elles. Ce seuil n'est **pas** une garde d'admission des lectures 1 et 2 : deux
    # lectures indépendantes qui s'accordent confirment quelle que soit leur confiance, et trois
    # lectures concordantes confirment aussi sous le seuil. Un flottant que le modèle se donne à
    # lui-même, que rien n'a calibré, ne peut pas annuler un accord ; il peut seulement refuser de
    # faire foi quand il est le dernier mot. L'observabilité de la confiance vit ailleurs :
    # `kind_confidence` publié par bloc et l'alerte `confiance_typage_faible` à
    # `kind_confidence_min`.
    type_clauses_arbitration_confidence_min: float = Field(0.8, ge=0, le=1)
    # Écart de confiance toléré entre deux lectures indépendantes avant de parler de désaccord. Le
    # défaut `1.0` — l'écart maximal possible — dit qu'aucun écart de confiance ne fait à lui seul
    # un désaccord : deux lectures qui donnent le même `kind` à 0,88 et 0,85 sont d'accord, et le
    # « pas assez sûr » est déjà porté par `type_clauses_arbitration_confidence_min`. Abaisser ce
    # seuil est la seule façon de faire compter un écart ; l'égalité stricte n'en est pas une.
    type_clauses_confidence_tolerance: float = Field(1.0, ge=0, le=1)
    type_clauses_batch_poll_s: float = Field(20.0, gt=0)
    type_clauses_batch_timeout_s: float = Field(7200.0, gt=0)
    # Transport CLI de reprise (jamais utilisé par le runtime HTTP) : Messages standard, sans
    # retry implicite du SDK, avec parallélisme et relances fournisseur explicitement bornés. Zéro
    # est un réglage intentionnel : une campagne corrective peut interdire toute seconde tentative.
    type_clauses_standard_concurrency: int = Field(8, ge=1, le=32)
    type_clauses_standard_max_retries: int = Field(3, ge=0, le=8)
    type_clauses_standard_retry_base_s: float = Field(1.0, gt=0, le=30)
    # Bornes appliquées aux étiquettes : une cible d'article trop large ou une liste partielle ne
    # produit aucun lien. Le verdict reste alors humain via `unresolved_refs`.
    type_clauses_max_article_refs: int = Field(12, ge=1)
    type_clauses_max_scope_articles: int = Field(20, ge=1)
    type_clauses_max_relations: int = Field(6, ge=1)
    type_clauses_ref_expansion_max_blocks: int = Field(30, ge=1)
    type_clauses_definition_max_chars: int = Field(120, ge=1)
    type_clauses_definition_max_words: int = Field(12, ge=1)
    # Longueur maximale du périmètre dérivé du corpus et rendu dans le préfixe de *comprendre*
    # (`Corpus.perimetres`). Le préfixe est cacheable (AD-9) et facturé : une projection qui
    # grossirait avec le corpus sans borne ferait grossir chaque appel `reason`. Au-delà, les
    # dernières catégories sont **retirées** (jamais une ligne coupée en deux, et jamais la
    # première : un périmètre vide serait pire que court).
    # **Mesuré (revue coordonnée 2.1), et la marge est plus mince qu'annoncé** : le guide livré rend
    # **3 004 caractères sur les 4 000** du seuil, pour 10 catégories et **77** enfants directs — les
    # 39 fiches plus les 38 entrées de « Questions fréquentes ». Il reste donc **996 caractères**,
    # soit 25 % du plafond (ou 33 % de la taille actuelle, selon le dénominateur qu'on prend — d'où
    # le chiffre en caractères, qui, lui, ne se lit que d'une façon). Pas d'« un facteur trois ».
    # C'est étroit, et l'étroitesse est dangereuse ici : le prompt affirme « c'est la liste qui fait
    # foi, aucune autre », si bien qu'une catégorie retirée par la borne réintroduit exactement le
    # faux `hors_perimetre` que cette story vient de corriger. `tests/test_loader.py` rougit donc
    # bien avant la coupure, à `PERIMETRE_MARGE_MIN` de marge — c'est ce test, et non ce
    # commentaire, qui préviendra le jour où quelques fiches de plus seront ajoutées.
    perimetre_max_chars: int = Field(4000, ge=1)

    # Ingestion PDF (story 1.2) : bandes d'en-tête/pied en points, récurrence minimale d'un en-tête,
    # écart vertical (en hauteurs de ligne) qui sépare deux paragraphes, abscisse maximale d'un numéro d'article.
    header_band_pt: float = Field(40.0, ge=0)
    footer_band_pt: float = Field(40.0, ge=0)
    header_min_pages_ratio: float = Field(0.3, ge=0, le=1)
    para_gap_ratio: float = Field(1.5, gt=0)
    article_number_max_x: float = Field(70.0, ge=0)
    # Segmentation (revue Codex 1.2) : taille minimale d'un titre d'article, taille maximale d'un en-tête courant en
    # capitales, tolérance de ligne de base (numéro + texte sur la même ligne), tolérance horizontale entre le numéro
    # et son texte, retrait minimal d'une continuation d'item de liste.
    title_min_size_pt: float = Field(12.0, gt=0)
    header_caps_max_size_pt: float = Field(10.0, gt=0)
    baseline_tolerance_pt: float = Field(3.0, ge=0)
    number_gap_tolerance_pt: float = Field(1.0, ge=0)
    list_indent_pt: float = Field(4.0, ge=0)
    # Dé-indentation structurelle (story 3.3) : un court dernier item numéroté peut être suivi
    # d'un paragraphe de clôture aligné sur le corps de son parent. Les deux bornes restent
    # géométriques/structurelles et entrent dans l'empreinte d'ingestion.
    dedent_tolerance_pt: float = Field(1.0, ge=0)
    dedent_starter_max_lines: int = Field(2, ge=1)
    fetch_timeout_s: float = Field(30.0, gt=0)
    metadata_timeout_s: float = Field(2.0, gt=0)  # serveur de métadonnées GCP (jeton du repli gs://)

    # --- navigation par le modèle (amendement AD-1 du 03/09/2026, story 5.6) ------------------
    # Les quatre réglages du chemin servi : le modèle reçoit le sommaire **complet** du document et
    # navigue lui-même, puis rédige dans la même conversation. Tous mesurés sur le prototype
    # (`scripts/proto_navigation.py`, série du 03/09/2026 : A16 3/3 strict, 2 à 4 tours, 12 à 27 s,
    # 0,05 € à cache chaud). Ils sont ajoutés en fin de classe et ne touchent aucun seuil existant.
    #
    # `navigation_max_llm_turns` : le plafond de **sûreté** des tours d'outils, pas une cible. La
    # série réelle en a employé 2 à 4 ; 8 laisse la place d'une recherche infructueuse suivie d'une
    # exploration, et c'est le budget de lecture — non le nombre de tours — qui borne la dépense.
    # Distinct de `max_llm_turns` (≤ 3), qui borne la variante `outils` que cette story ne sert plus.
    navigation_max_llm_turns: int = Field(8, ge=1)
    # `navigation_budget_tokens` : ce que la **lecture** peut coûter, tous nœuds ouverts confondus.
    # Appliqué au refus, jamais à la sélection (le code ne coupe rien en silence : il refuse une
    # ouverture en disant son coût, le restant et quoi faire, et le modèle arbitre). Les trois blocs
    # du témoin A16 tiennent ensemble dans 1 029 tokens ; 12 000 laisse dix fois cette marge.
    navigation_budget_tokens: int = Field(12000, ge=1)
    # `navigation_search_limit` : le nombre de candidats que `chercher` **propose**. Ce n'est pas
    # `search_limit` (20 aussi, mais celui-là borne une passe de code qui ouvre) : ici rien n'entre
    # dans le contexte de rédaction sans que le modèle ait ouvert le nœud.
    navigation_search_limit: int = Field(20, ge=1)
    # AD-9 : Sonnet reste le plancher de tout choix sémantique servi, et la navigation **est** le
    # choix sémantique le plus lourd de la chaîne. `micro` reste réglable pour rejouer l'arbitrage.
    navigation_tier: Literal["micro", "reason"] = "reason"

    # --- Story 5.6 (T1b, 03/09/2026) : la place de l'ébauche de navigation, re-dérivée ---------
    # **Pourquoi ces trois seuils existent au lieu de réutiliser `draft_*` et `rediger_max_tokens`.**
    # `draft_max_claims` (4) a été dérivé de `rediger_max_tokens` (2 048), lui-même calibré sur
    # l'ancien *rédiger* — un appel **sans réflexion** dont le contexte était une sélection déjà
    # faite par le code. La navigation rédige dans une conversation dont le tour terminal demande la
    # réflexion adaptative (`REFLEXION_ADAPTATIVE`, `steps/naviguer.py`) : le fournisseur compte
    # cette réflexion **dans le même `max_tokens`** que la sortie. Les deux étages n'ont donc ni le
    # même contrat ni la même dépense, et la borne de l'un ne dérive pas celle de l'autre.
    #
    # **La mesure qui les décide** : les trois réponses A16 du pipeline intégré
    # (`automation/runs/20260902-structure-index/a16-t1/a16-r{1,2,3}.json`, sinistre, deux
    # sous-questions, chemin servi). A16 y est 2/3. Le run 1 tient dans **trois** claims — la
    # condition d'applicabilité de l'option (`p39:7`) y prend une place entière comme claim, et
    # `p34:12`, lu et transmis, n'en a plus. Le modèle se rationne **sous** la borne annoncée : ce
    # n'est pas la coupe mécanique de `rattacher_claims_sinistre` qui a mordu (aucun
    # `claims_hors_borne_ecartees` dans la trace), c'est la borne elle-même, écrite au prompt, qui
    # lui fait choisir entre deux clauses décisionnelles. Le prototype, sans borne, rendait
    # jusqu'à 5 claims.
    #
    # `navigation_draft_max_claims` : les sous-questions qu'une question de sinistre arrête (2 sur
    # A16) × les trois clauses qui décident d'une sous-question d'assurance — la garantie de base,
    # la garantie optionnelle avec sa condition d'acquisition, l'exclusion ou la condition qui la
    # borne. C'est la forme du document, pas une marge : le prompt exige déjà « pas la meilleure
    # d'entre elles, toutes ». Ce n'est **pas** `question_max_facettes` (4) × 3 = 12 : cette borne-ci
    # est un comportement mesuré, pas un produit de bornes structurelles.
    navigation_draft_max_claims: int = Field(6, ge=1)
    # `navigation_draft_max_segments` : `rattacher_claims_sinistre` rend **un segment factuel par
    # claim**, et les segments non factuels n'occupent que les places restantes. Six claims sous
    # `draft_max_segments` (6) chasseraient donc toute articulation et, surtout, le segment `limite`
    # par lequel la lecture dit ce qu'elle ne couvre pas — la borne de claims dégraderait la réponse
    # au lieu de l'élargir. Mesuré sur les trois réponses A16 : 2, 1 et 1 segments non factuels.
    # 6 + 3 laisse la place des deux transitions mesurées **et** de la limite.
    navigation_draft_max_segments: int = Field(9, ge=1)
    # `navigation_rediger_max_tokens` : le plafond du **tour terminal** de la navigation, sur le
    # patron de `verifier_sinistre_max_tokens` — contrat JSON **plus** réserve de réflexion, les deux
    # mesurés. Sortie du tour terminal sur les trois réponses A16 : 1 574 / 1 181 / 1 259 tokens,
    # dont 496 / 0 / 0 de réflexion, soit **1 078 / 1 181 / 1 259 de JSON à quatre claims** — pire
    # par claim ≈ 315. À six claims : 6 × 315 ≈ 1 890, arrondi à **1 920** de contrat. La réflexion
    # de ce tour est **intermittente** (deux runs sur trois à zéro, un à 496) : la réserve majore le
    # pire mesuré de 2,3×, soit **1 152**. Total **3 072**.
    # Ce plafond reste sous `verifier_sinistre_max_tokens` (3 456) : le terme le plus long de
    # `_coherence` ne bouge pas, donc `llm_timeout_s` (55 s) n'est pas re-dérivé — mais il entre
    # bien dans le contrôle, pour qu'une hausse future ne puisse plus passer inaperçue.
    # `max_tokens` ne facture pas : seul le majorant de préflight bouge, des tokens ajoutés au tarif
    # de sortie du tier servi.
    navigation_rediger_max_tokens: int = Field(3072, ge=1)
    # --- Story 5.6 (T5, 03/09/2026) : les deux caches de la facture ----------
    # Décision de Lancelot du 03/09, sur les deux chiffres mesurés par le prototype de navigation :
    # une première requête après expiration du préfixe paie ≈ 0,28 € d'écriture de cache, contre
    # ≈ 0,015 € à chaud. Deux caches, deux places : l'un chez le fournisseur (on relit le préfixe
    # avant qu'il ne refroidisse), l'autre ici (on ne repaie pas une question déjà posée mot pour
    # mot). Aucun des deux ne choisit quoi que ce soit à la place du modèle — c'est la condition
    # posée par l'amendement AD-1 du 03/09/2026.
    #
    # `prefix_keepalive_enabled` — **faux par défaut**, et c'est la borne du cahier : le maintien n'a
    # de sens que sous `--min-instances=1`, drapeau que `deploy.yml` décide seul (AD-13, convention
    # Seuils : jamais deux textes autoritaires sur la même valeur). Le workflow pose la variable à
    # côté du drapeau, et `tests/test_workflows.py` refuse que l'un existe sans l'autre. Hors de là
    # — poste de développement, révision candidate, suite hermétique — rien ne démarre.
    prefix_keepalive_enabled: bool = False
    # `prefix_keepalive_s` — **dérivé du TTL**, jamais choisi : le cache de préfixe des modèles servis
    # vit une heure (`llm/models.MODEL_CAPS[...]["cache_ttl"] == "1h"`, AD-9), soit
    # `PREFIX_CACHE_TTL_S` = 3 600 s. 3 000 s laissent 600 s de marge sous l'expiration — de quoi
    # absorber un réveil tardif de la boucle, un tour de maintien qui traîne sur plusieurs préfixes
    # et le délai d'appel lui-même. Un intervalle au-delà du TTL ne maintiendrait rien : le validateur
    # de cohérence le refuse au démarrage.
    prefix_keepalive_s: float = Field(3000.0, gt=0)
    # Combien de préfixes distincts on accepte de tenir au chaud. Le registre est alimenté par le
    # trafic (quatre étapes × documents × langues) : sans borne, le maintien croîtrait avec l'usage,
    # c'est-à-dire à l'inverse de ce pour quoi il existe. 12 couvre les quatre étapes des deux
    # documents servis, avec de la place ; au-delà, le nouveau venu est refusé, jamais un ancien
    # évincé (une rotation ferait payer une écriture à chaque tour).
    prefix_keepalive_max_prefixes: int = Field(12, ge=1)
    # Le plafond dur de ce que le maintien peut coûter par jour, quantième UTC. Un préfixe tenu en
    # continu coûte ≈ 0,015 € × 86 400 / 3 000 ≈ 0,43 €/jour : 1,00 € tient deux préfixes chauds en
    # permanence et **arrête** le reste au lieu de laisser la facture suivre le nombre de préfixes.
    # `/sante` publie le coût cumulé et dit si le plafond du jour est atteint.
    prefix_keepalive_max_cost_eur_per_day: float = Field(1.0, ge=0)
    # `response_cache_*` — le cache interne de réponses. Actif par défaut, mais **jamais armé sans
    # clé fournisseur** (`api/etat.py`) : un service qui ne peut rien payer n'a rien à économiser, et
    # cette règle — la même que celle de `ok` dans `/sante` — laisse toute exécution hors ligne, la
    # suite hermétique comprise, sans état sur disque qu'elle n'a pas demandé.
    response_cache_enabled: bool = True
    # Sept jours : au-delà, ce n'est plus « la même question qu'hier », c'est un stock. La borne
    # utile est de toute façon celle des empreintes — une réingestion, un prompt ou un seuil qui
    # bouge périment l'entrée avant son TTL, quel qu'il soit.
    response_cache_ttl_s: float = Field(604800.0, gt=0)
    # Deux bornes, parce qu'une seule ne borne rien : le nombre d'entrées empêche le magasin de
    # devenir un journal, les octets empêchent quelques réponses très longues d'occuper la mémoire
    # de l'instance (le disque de Cloud Run est en RAM, sous `--memory=1Gi` et `--concurrency=2`).
    # 200 entrées × ~64 Kio de réponse+trace tiennent largement sous les 32 Mio.
    response_cache_max_entries: int = Field(200, ge=1)
    response_cache_max_bytes: int = Field(33_554_432, ge=4096)

    @model_validator(mode="before")
    @classmethod
    def _versioned_retrieval_default(cls, value: Any) -> Any:
        """Injecte d'un seul snapshot le triplet absent après résolution de l'environnement."""
        if not isinstance(value, dict):
            return value
        default = load_retrieval_default(RETRIEVAL_DEFAULT_PATH)
        resolved = dict(value)
        resolved.setdefault("retrieval_variant", default.variant)
        resolved.setdefault("retrouver_outils_tier", default.tier)
        resolved.setdefault("retrieval_prompt_cache", default.prompt_cache)
        return resolved

    @model_validator(mode="after")
    def _coherence(self) -> Settings:
        if self.prefix_keepalive_s >= PREFIX_CACHE_TTL_S:
            # Un maintien qui arrive après l'expiration ne maintient rien : il **paie** l'écriture
            # qu'il prétendait éviter, à chaque tour. Le refus est au démarrage, pas dans la facture.
            raise ValueError(
                f"prefix_keepalive_s ({self.prefix_keepalive_s}) doit être < la durée de vie du "
                f"cache de préfixe ({PREFIX_CACHE_TTL_S})")
        if self.llm_timeout_s >= self.deadline_s:
            raise ValueError(f"llm_timeout_s ({self.llm_timeout_s}) doit être < deadline_s ({self.deadline_s})")
        if self.llm_retry_margin_s >= self.deadline_s:
            raise ValueError(f"llm_retry_margin_s ({self.llm_retry_margin_s}) doit être < deadline_s ({self.deadline_s})")
        if self.profil_max_opens >= self.max_opens:
            # Le même invariant que `RetrievalBudget`, vérifié **au démarrage** : une configuration
            # contradictoire (`MAX_OPENS=2` dans un `.env`) doit refuser de booter, pas produire un
            # `RetrievalBudget` invalide à la première question (revue coordonnée 2.3, A4).
            raise ValueError(f"profil_max_opens ({self.profil_max_opens}) doit être < max_opens "
                             f"({self.max_opens}) : le profil ordonne, il ne remplace pas la question")
        tiers_proteges = {
            "comprendre_tier": self.comprendre_tier,
            "verifier_tier": self.verifier_tier,
            "retrouver_outils_tier": self.retrouver_outils_tier,
        }
        if self.baseline_tiers and self.env == "prod":
            raise ValueError("baseline_tiers est un mode de mesure hors ligne, interdit en production")
        if not self.baseline_tiers:
            abaisses = [name for name, tier in tiers_proteges.items() if tier != "reason"]
            if abaisses:
                raise ValueError(
                    "baseline_tiers=true est requis pour mesurer micro sur "
                    + ", ".join(abaisses)
                    + "; le produit actif reste au plancher reason",
                )
        mecanismes = tuple(part.strip() for part in self.retrieval_mechanism_order.split(","))
        if (len(mecanismes) != 4 or len(set(mecanismes)) != 4
                or set(mecanismes) != {"dictionnaire", "faq", "sommaire", "outils"}):
            raise ValueError("retrieval_mechanism_order doit contenir exactement, sans doublon, "
                             "dictionnaire,faq,sommaire,outils")
        if self.header_caps_max_size_pt >= self.title_min_size_pt:
            raise ValueError(f"header_caps_max_size_pt ({self.header_caps_max_size_pt}) doit être "
                             f"< title_min_size_pt ({self.title_min_size_pt})")
        for nom, valeur in (("comprendre_max_tokens", self.comprendre_max_tokens),
                            ("retrouver_outils_max_tokens", self.retrouver_outils_max_tokens),
                            ("rediger_max_tokens", self.rediger_max_tokens),
                            ("navigation_rediger_max_tokens", self.navigation_rediger_max_tokens),
                            ("verifier_max_tokens", self.verifier_max_tokens),
                            ("verifier_sinistre_max_tokens", self.verifier_sinistre_max_tokens)):
            # Le plafond par étape ne peut pas dépasser le plafond de sortie du client : il part tel
            # quel au fournisseur et entre au tarif `output` dans le majorant `estimate_cost` (NFR4).
            if valeur > self.llm_max_output_tokens:
                raise ValueError(f"{nom} ({valeur}) doit être <= llm_max_output_tokens "
                                 f"({self.llm_max_output_tokens})")
        # Correctif du tour 3 (R3). **Un plafond de sortie qu'on n'a pas le temps d'écrire est un
        # 503 qui s'ignore.** À 4 096 tokens et 40 s, la borne effective du vérificateur sinistre
        # était 3 575 tokens — 87 % du plafond déclaré — et la deuxième réponse A16 est morte là,
        # sur son délai d'appel, avec la meilleure ébauche des trois et 73 s de deadline encore
        # disponibles. Les deux nombres vivaient dans deux dérivations qui s'ignoraient ; ils sont
        # désormais liés, et une configuration qui les fait diverger refuse de démarrer.
        #
        # Le débit est **minoré** et la marge est une latence d'amorçage, toutes deux mesurées :
        # majorer une durée demande de sous-estimer la vitesse, pas de la moyenner.
        plus_longue = max(self.verifier_sinistre_max_tokens, self.verifier_max_tokens,
                          self.rediger_max_tokens, self.navigation_rediger_max_tokens,
                          self.comprendre_max_tokens, self.retrouver_outils_max_tokens)
        duree_majoree = self.duree_majoree_pour(plus_longue)
        if duree_majoree > self.llm_timeout_s:
            raise ValueError(
                f"llm_timeout_s ({self.llm_timeout_s} s) ne laisse pas écrire la plus longue "
                f"sortie d'étape ({plus_longue} tokens) : {duree_majoree:.1f} s requises à "
                f"{self.llm_output_tokens_per_s_min} tokens/s plus {self.llm_latence_marge_s} s "
                "de latence — relever le délai, ou baisser le plafond de sortie")
        if self.quote_min_chars > self.quote_max_chars:
            raise ValueError(f"quote_min_chars ({self.quote_min_chars}) doit être "
                             f"<= quote_max_chars ({self.quote_max_chars})")
        if self.question_min_terms > self.question_max_terms:
            raise ValueError(f"question_min_terms ({self.question_min_terms}) doit être "
                             f"<= question_max_terms ({self.question_max_terms})")
        if self.draft_max_claims > self.draft_max_segments:
            raise ValueError(f"draft_max_claims ({self.draft_max_claims}) doit être "
                             f"<= draft_max_segments ({self.draft_max_segments}) : une claim "
                             "sinistre exige son segment factuel atomique")
        if self.verifier_max_claims < self.draft_max_claims:
            # Story 1.5 : *rédiger* peut rendre `draft_max_claims` claims ; si *vérifier* en évalue
            # moins, des claims retrouvées seraient rejetées « non évaluées » par pure configuration —
            # un dégradé silencieux du rappel (AD-16), invisible dans la réponse.
            raise ValueError(f"verifier_max_claims ({self.verifier_max_claims}) doit être "
                             f">= draft_max_claims ({self.draft_max_claims})")
        # Story 5.6 T1b : les deux mêmes invariants, sur le couple que la **navigation** annonce.
        # `rattacher_claims_sinistre` lève sur le premier ; le second garde du dégradé silencieux du
        # rappel, une claim rendue qu'un vérificateur trop étroit rejetterait « non évaluée ».
        if self.navigation_draft_max_claims > self.navigation_draft_max_segments:
            raise ValueError(
                f"navigation_draft_max_claims ({self.navigation_draft_max_claims}) doit être "
                f"<= navigation_draft_max_segments ({self.navigation_draft_max_segments}) : une "
                "claim sinistre exige son segment factuel atomique")
        # Story 5.6 T1b : `pipelines/sinistre.py` borne la fusion de relance et ses trois gardes
        # « sans place » sur les champs `draft_*`, **sans savoir quel étage a rédigé**. C'est la
        # raison pour laquelle `draft_max_claims` et `draft_max_segments` ont été re-dérivés en même
        # temps que les bornes de navigation : à 4 et 6, une ébauche de navigation à cinq claims
        # retenues avec une relance due passait le pré-contrôle des limites (5 + 1 + 0 = 6) puis
        # faisait lever `fusionner_acquis` — un `ValueError` sur un chemin nominal. Les deux couples
        # doivent donc rester en phase tant que la fusion lit `draft_*` ; le jour où elle demandera
        # sa borne à l'étage qui a produit l'ébauche, ce commentaire tombe et l'invariant se pose.
        if self.env == "prod":
            # AC 1.10 : « désactivé en production ». Forcé, et non seulement dérivé de l'absence de
            # la variable — sinon `ENV=prod ALLOW_UNGATED=true` armait la dérogation en production,
            # exactement ce que l'AC ferme. La demande est retenue pour être **dite** (`/sante`).
            self.ungated_demande_en_prod = bool(self.allow_ungated)
            self.allow_ungated = False
        elif self.allow_ungated is None:
            self.allow_ungated = True
        return self

    def retrieval_mechanisms(self) -> tuple[str, ...]:
        """Ordre effectif des mécanismes, distinct de tout classement interne des hits."""
        return tuple(part.strip() for part in self.retrieval_mechanism_order.split(","))

    @property
    def deroger_au_gate(self) -> bool:
        """La disjonction d'AD-7, **ses trois termes** (dette D1, refermée par la story 4.5).

        AD-7 écrit la règle de service ainsi : « servi ssi aucun bloquant statique **et**
        (`gate.evals_ok` **ou** `ENV=dev` **ou** `ALLOW_UNGATED`) ». Le code n'en honorait que
        deux : `config.py` absorbait `ENV=dev` **dans** `ALLOW_UNGATED` (la dérogation ne valait
        `True` que lorsque la variable était absente), si bien que poser explicitement
        `ALLOW_UNGATED=false` en dev mettait en quarantaine un document sans gate — alors que le
        deuxième terme de la disjonction, `ENV=dev`, le sert. Un opérateur qui écrivait « non » à
        une dérogation en obtenait une **autre** règle que celle de l'AD.

        Les deux faits restent donc distincts, et c'est ce qui les rend lisibles : `allow_ungated`
        dit ce que l'opérateur a demandé (et vaut `False` de force en `prod`), cette propriété dit
        ce que la règle décide. La fermeture en production est intacte : `allow_ungated` y est
        forcé à `False` et `env` n'y vaut pas `dev`, donc la disjonction est fausse par ses trois
        termes à la fois.
        """
        return bool(self.allow_ungated) or self.env == "dev"

    @property
    def audit_exact_actif(self) -> bool:
        """L'audit exact est-il écrit sur disque pour cette configuration ?

        `None` — le défaut — suit l'environnement : actif partout sauf en production, où l'enveloppe
        exacte (question, historique, blocs) n'a rien à faire sur un disque partagé. Un booléen
        explicite tranche dans les deux sens, y compris pour l'armer en production le temps d'un
        diagnostic — c'est une décision d'exploitation, elle se prend, elle ne se devine pas.
        """
        if self.llm_audit_exact is not None:
            return self.llm_audit_exact
        return self.env != "prod"

    def duree_majoree_pour(self, max_tokens: int) -> float:
        """Le temps qu'il faut, au pire, pour écrire `max_tokens` de sortie : la seule dérivation.

        Correctif du tour 4 (C2). Le tour 3 avait écrit ce calcul pour lier le délai d'appel au
        plafond de sortie **au démarrage** ; il n'existait nulle part à l'exécution. Un appel a donc
        été envoyé avec 24,08 s de deadline restante pour une sortie qui en demande 45,66 : il ne
        pouvait pas aboutir, il a coûté 24 s et zéro token, et il a emporté la marge dont la remise
        de la réponse avait besoin.

        Le débit est **minoré** et la latence est une amorce, tous deux mesurés : majorer une durée
        demande de sous-estimer la vitesse, pas de la moyenner. Une seule méthode, lue par la
        validation de configuration, par le budget de requête et par les gardes de second cycle —
        trois copies auraient divergé, et c'est exactement ce qui s'est produit entre le délai
        d'appel et la marge de relance.
        """
        return max_tokens / self.llm_output_tokens_per_s_min + self.llm_latence_marge_s

    def thresholds(self) -> dict[str, float | int]:
        """Seuils actifs, tels qu'exposés dans `Trace.thresholds`."""
        return {
            "deadline_s": self.deadline_s,
            "llm_timeout_s": self.llm_timeout_s,
            "llm_output_tokens_per_s_min": self.llm_output_tokens_per_s_min,
            "llm_latence_marge_s": self.llm_latence_marge_s,
            "client_abort_margin_s": self.client_abort_margin_s,
            "client_probe_timeout_s": self.client_probe_timeout_s,
            "raison_publiable_max_chars": self.raison_publiable_max_chars,
            "quote_min_chars": self.quote_min_chars,
            "quote_min_ratio": self.quote_min_ratio,
            "max_opens": self.max_opens,
            "profil_max_opens": self.profil_max_opens,
            "navigation_max_llm_turns": self.navigation_max_llm_turns,
            "navigation_budget_tokens": self.navigation_budget_tokens,
            "navigation_search_limit": self.navigation_search_limit,
            "navigation_draft_max_claims": self.navigation_draft_max_claims,
            "navigation_draft_max_segments": self.navigation_draft_max_segments,
            "navigation_rediger_max_tokens": self.navigation_rediger_max_tokens,
            "navigation_tier_reason": int(self.navigation_tier == "reason"),
            "facette_max_opens": self.facette_max_opens,
            "facette_reserve_max_part": self.facette_reserve_max_part,
            "facette_variante_max_part": self.facette_variante_max_part,
            "node_window": self.node_window,
            "search_limit": self.search_limit,
            "limite_liee_max": self.limite_liee_max,
            "limite_liee_proximite_min": self.limite_liee_proximite_min,
            "pdf_highlight_max_lines": self.pdf_highlight_max_lines,
            "pdf_highlight_max_blocks": self.pdf_highlight_max_blocks,
            "pdf_render_concurrency": self.pdf_render_concurrency,
            "pdf_render_cache_pages": self.pdf_render_cache_pages,
            "pdf_render_dpi": self.pdf_render_dpi,
            "pdf_render_max_pixels": self.pdf_render_max_pixels,
            "pdf_render_queue_timeout_s": self.pdf_render_queue_timeout_s,
            "max_llm_attempts": self.max_llm_attempts,
            "max_llm_turns": self.max_llm_turns,
            "retrouver_outils_max_tokens": self.retrouver_outils_max_tokens,
            "retrieval_max_blocks": self.retrieval_max_blocks,
            "retrieval_max_tokens": self.retrieval_max_tokens,
            "max_cost_eur_per_request": self.max_cost_eur_per_request,
            "cost_alert_eur": self.cost_alert_eur,
            "evals_max_cost_eur": self.evals_max_cost_eur,
            "live_budget_eur": self.live_budget_eur,
            # Story 4.2b : la matrice baseline épingle les tiers par étape. `Trace.thresholds` est
            # numérique : 1 = `reason` (défaut AD-9), 0 = `micro`. Publiés ici, ils entrent dans la
            # namespace de cache des évals via `thresholds()`.
            "baseline_tiers": int(self.baseline_tiers),
            "comprendre_tier_reason": int(self.comprendre_tier == "reason"),
            "rediger_tier_reason": int(self.rediger_tier == "reason"),
            "verifier_tier_reason": int(self.verifier_tier == "reason"),
            "retrouver_outils_tier_reason": int(self.retrouver_outils_tier == "reason"),
            "retrieval_prompt_cache": int(self.retrieval_prompt_cache),
            "llm_audit_max_bytes": self.llm_audit_max_bytes,
            "llm_audit_exact": int(self.audit_exact_actif),
            "llm_audit_retention_files": self.llm_audit_retention_files,
            "llm_max_output_tokens": self.llm_max_output_tokens,
            "llm_retry_margin_s": self.llm_retry_margin_s,
            "comprendre_max_tokens": self.comprendre_max_tokens,
            "rediger_max_tokens": self.rediger_max_tokens,
            "verifier_max_tokens": self.verifier_max_tokens,
            "verifier_max_claims": self.verifier_max_claims,
            "verifier_sinistre_max_tokens": self.verifier_sinistre_max_tokens,
            "verifier_sinistre_json_tokens": self.verifier_sinistre_json_tokens,
            "verifier_thinking_reserve_tokens": self.verifier_thinking_reserve_tokens,
            "fait_manquant_max_chars": self.fait_manquant_max_chars,
            "ask_client_max": self.ask_client_max,
            "conversation_rate_limit_per_minute": self.conversation_rate_limit_per_minute,
            "conversation_rate_limit_per_day": self.conversation_rate_limit_per_day,
            "conversation_max_turns": self.conversation_max_turns,
            "conversation_active_questions_max": self.conversation_active_questions_max,
            "qualites_exigees_max": self.qualites_exigees_max,
            "qualite_mot_min_chars": self.qualite_mot_min_chars,
            "historique_max_turns": self.historique_max_turns,
            # `Trace.thresholds` est typé `float | int` : un bool y est publié comme 0/1 par
            # pydantic. On le convertit ici plutôt que de laisser la sérialisation décider
            # (revue 1.5) — la valeur reste lisible, et le type déclaré reste vrai.
            "relance_sur_non_pertinence": int(self.relance_sur_non_pertinence),
            "quote_max_chars": self.quote_max_chars,
            "draft_max_segments": self.draft_max_segments,
            "draft_max_claims": self.draft_max_claims,
            "draft_max_definitions": self.draft_max_definitions,
            "question_min_terms": self.question_min_terms,
            "question_max_terms": self.question_max_terms,
            "question_max_facettes": self.question_max_facettes,
            "scope_max_themes": self.scope_max_themes,
            "libelle_max_chars": self.libelle_max_chars,
            # Constante de module, pas un champ : cf. `LISTE_MAX_ITEMS`. Publiée quand même —
            # un seuil actif que la trace tait est un seuil qu'aucune éval ne peut discuter.
            "liste_max_items": LISTE_MAX_ITEMS,
            "estimate_chars_per_token": self.estimate_chars_per_token,
            "estimate_tokenizer_factor": self.estimate_tokenizer_factor,
            "count_tokens_timeout_s": self.count_tokens_timeout_s,
            "summary_max_tags": self.summary_max_tags,
            "summary_resume_max_chars": self.summary_resume_max_chars,
            "summary_max_level": self.summary_max_level,
            "summary_page_max_chars": self.summary_page_max_chars,
            "summary_slice_max_chars": self.summary_slice_max_chars,
            "summary_apercu_max_chars": self.summary_apercu_max_chars,
            "excerpt_max_chars": self.excerpt_max_chars,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "rate_limit_per_day": self.rate_limit_per_day,
            "rate_limit_max_clients": self.rate_limit_max_clients,
            "retry_after_s": self.retry_after_s,
            "request_max_bytes": self.request_max_bytes,
            "cloud_trace_max_chars": self.cloud_trace_max_chars,
            "coverage_threshold": self.coverage_threshold,
            "kind_confidence_min": self.kind_confidence_min,
            "mixed_page_image_density": self.mixed_page_image_density,
            "ocr_dpi": self.ocr_dpi,
            "quality_min_words": self.quality_min_words,
            "foreign_signal_min": self.foreign_signal_min,
            "french_signal_ratio_min": self.french_signal_ratio_min,
            "gibberish_ratio_max": self.gibberish_ratio_max,
            "residual_header_min_pages_ratio": self.residual_header_min_pages_ratio,
            "toc_page_number_baseline_pt": self.toc_page_number_baseline_pt,
            "toc_column_tolerance_pt": self.toc_column_tolerance_pt,
            "toc_indent_tolerance_pt": self.toc_indent_tolerance_pt,
            "toc_line_gap_ratio": self.toc_line_gap_ratio,
            "toc_title_prefix_min_chars": self.toc_title_prefix_min_chars,
            # Story 4.2c : la géométrie des colonnes du corps et les bornes du vérificateur de
            # structure se publient comme les autres (convention Seuils). Les trois premières
            # entrent aussi dans `ingest_fingerprint` : elles changent l'ordre de lecture.
            "column_gutter_min_pt": self.column_gutter_min_pt,
            "column_min_lines": self.column_min_lines,
            "column_min_span_ratio": self.column_min_span_ratio,
            "column_row_pairing_max_ratio": self.column_row_pairing_max_ratio,
            "column_min_fill_ratio": self.column_min_fill_ratio,
            "structure_max_depth": self.structure_max_depth,
            "structure_max_nodes": self.structure_max_nodes,
            "structure_max_children": self.structure_max_children,
            "structure_max_blocks_per_leaf": self.structure_max_blocks_per_leaf,
            "structure_min_coverage": self.structure_min_coverage,
            "etiquette_max_chars": self.etiquette_max_chars,
            "structure_max_input_chars": self.structure_max_input_chars,
            "structure_max_output_tokens": self.structure_max_output_tokens,
            "structure_output_tokens_per_line": self.structure_output_tokens_per_line,
            "structure_thinking_reserve_tokens": self.structure_thinking_reserve_tokens,
            "structure_max_refinements": self.structure_max_refinements,
            "structure_max_cost_eur": self.structure_max_cost_eur,
            # Story 2.1 : les bornes du dictionnaire enrichi et celle du périmètre dérivé du corpus.
            # Elles sont publiées comme les autres (convention Seuils) — `/api/v1/sante` et
            # `Trace.thresholds` se lisent avec la même règle, y compris pour ce que l'ingestion a
            # appliqué au fichier que le serveur relit.
            "dictionary_term_max_chars": self.dictionary_term_max_chars,
            "dictionary_term_max_words": self.dictionary_term_max_words,
            "dictionary_max_variants_per_term": self.dictionary_max_variants_per_term,
            "dictionary_max_terms_per_fiche": self.dictionary_max_terms_per_fiche,
            "dictionary_question_max_chars": self.dictionary_question_max_chars,
            "dictionary_max_questions_per_fiche": self.dictionary_max_questions_per_fiche,
            "dictionary_max_intent_triggers": self.dictionary_max_intent_triggers,
            "dictionary_flat_max_blocks_per_request": self.dictionary_flat_max_blocks_per_request,
            "dictionary_flat_max_input_chars": self.dictionary_flat_max_input_chars,
            "dictionary_flat_max_terms_per_block": self.dictionary_flat_max_terms_per_block,
            "dictionary_flat_max_output_tokens": self.dictionary_flat_max_output_tokens,
            "dictionary_max_output_tokens": self.dictionary_max_output_tokens,
            "dictionary_max_cost_eur": self.dictionary_max_cost_eur,
            "dictionary_batch_poll_s": self.dictionary_batch_poll_s,
            "dictionary_batch_timeout_s": self.dictionary_batch_timeout_s,
            # Même patron que `comprendre_tier_reason` : `Trace.thresholds` est numérique, et un
            # palier s'y publie par le booléen qui le distingue du défaut. 1 = `reason`, 0 = `ingest`.
            "dictionary_tier_reason": int(self.dictionary_tier == "reason"),
            "type_clauses_max_blocks_per_request": self.type_clauses_max_blocks_per_request,
            "type_clauses_max_input_chars": self.type_clauses_max_input_chars,
            "type_clauses_max_requests_per_batch": self.type_clauses_max_requests_per_batch,
            "type_clauses_max_output_tokens": self.type_clauses_max_output_tokens,
            "type_clauses_max_cost_eur": self.type_clauses_max_cost_eur,
            "type_clauses_arbitration_confidence_min": self.type_clauses_arbitration_confidence_min,
            "type_clauses_confidence_tolerance": self.type_clauses_confidence_tolerance,
            "type_clauses_batch_poll_s": self.type_clauses_batch_poll_s,
            "type_clauses_batch_timeout_s": self.type_clauses_batch_timeout_s,
            "type_clauses_standard_concurrency": self.type_clauses_standard_concurrency,
            "type_clauses_standard_max_retries": self.type_clauses_standard_max_retries,
            "type_clauses_standard_retry_base_s": self.type_clauses_standard_retry_base_s,
            "type_clauses_max_article_refs": self.type_clauses_max_article_refs,
            "type_clauses_max_scope_articles": self.type_clauses_max_scope_articles,
            "type_clauses_max_relations": self.type_clauses_max_relations,
            "type_clauses_ref_expansion_max_blocks": self.type_clauses_ref_expansion_max_blocks,
            "type_clauses_definition_max_chars": self.type_clauses_definition_max_chars,
            "type_clauses_definition_max_words": self.type_clauses_definition_max_words,
            "perimetre_max_chars": self.perimetre_max_chars,
            "header_band_pt": self.header_band_pt,
            "footer_band_pt": self.footer_band_pt,
            "header_min_pages_ratio": self.header_min_pages_ratio,
            "para_gap_ratio": self.para_gap_ratio,
            "article_number_max_x": self.article_number_max_x,
            "title_min_size_pt": self.title_min_size_pt,
            "header_caps_max_size_pt": self.header_caps_max_size_pt,
            "baseline_tolerance_pt": self.baseline_tolerance_pt,
            "number_gap_tolerance_pt": self.number_gap_tolerance_pt,
            "list_indent_pt": self.list_indent_pt,
            "dedent_tolerance_pt": self.dedent_tolerance_pt,
            "dedent_starter_max_lines": self.dedent_starter_max_lines,
            "fetch_timeout_s": self.fetch_timeout_s,
            "metadata_timeout_s": self.metadata_timeout_s,
            # Story 5.6 (T5) : les deux caches entrent dans les seuils publiés, et donc dans la
            # **clé** du cache de réponses lui-même — un TTL ou une borne qui bouge périme le stock
            # au lieu de servir des entrées produites sous d'autres règles. Même patron numérique
            # que `baseline_tiers` pour les booléens : 1 = actif.
            "prefix_keepalive_enabled": int(self.prefix_keepalive_enabled),
            "prefix_keepalive_s": self.prefix_keepalive_s,
            "prefix_keepalive_max_prefixes": self.prefix_keepalive_max_prefixes,
            "prefix_keepalive_max_cost_eur_per_day": self.prefix_keepalive_max_cost_eur_per_day,
            "response_cache_enabled": int(self.response_cache_enabled),
            "response_cache_ttl_s": self.response_cache_ttl_s,
            "response_cache_max_entries": self.response_cache_max_entries,
            "response_cache_max_bytes": self.response_cache_max_bytes,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


def cle_absente(settings: Settings) -> bool:
    """« Sans `ANTHROPIC_API_KEY`, ça refuse de tourner » — la règle, à un seul endroit.

    La variable d'environnement fait foi **quand elle est posée, vide comprise** : `Settings` la
    laisse tomber quand elle est vide (`env_ignore_empty=True`) et retombe alors sur le `.env` du
    poste, si bien que `ANTHROPIC_API_KEY= uv run …` tournerait et facturerait — l'inverse exact de
    ce que la commande dit vouloir. Non posée du tout, c'est `.env` qui répond, comme pour le serveur.

    Posée en story 1.10 dans `server/evals/run.py`, elle vit ici depuis la 2.1 : l'ingestion du
    dictionnaire soumet des lots de Batch API, et sa version naïve (`if not
    settings.anthropic_api_key`) a réellement appelé l'API sous `ANTHROPIC_API_KEY=` — mesuré. Deux
    commandes qui promettent la même chose ne peuvent pas la tenir par deux codes différents.
    """
    brut = os.environ.get("ANTHROPIC_API_KEY")
    if brut is not None:
        return not brut.strip()
    return not settings.anthropic_api_key.strip()
