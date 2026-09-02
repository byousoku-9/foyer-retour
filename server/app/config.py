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
    if value["variant"] not in {"deterministe", "outils", "full_context"}:
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


def _est_revision_complete(valeur: str) -> bool:
    """40 hexadécimaux — la seule forme qui identifie un commit sans ambiguïté."""
    return len(valeur) == 40 and all(c in _HEX for c in valeur.lower())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", env_file_encoding="utf-8",
                                      env_ignore_empty=True, extra="ignore")

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
    deadline_s: float = Field(75.0, gt=0)
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
    # d'appels (`max_llm_attempts`) et, au déploiement, `--timeout=60` de Cloud Run. 40 s laisse la
    # chaîne nominale (comprendre ≈ 3 s + rédiger + vérifier ≈ 4 s) tenir sous les 55 s, et une
    # relance qui déborderait est coupée par la deadline globale — le même 503, mais pour la vraie
    # raison. `[HYPOTHÈSE]` : à re-régler sur la distribution complète que donneront les 15–20
    # sinistres des questions-témoins (4.2), qui diront aussi s'il faut baisser l'effort de *rédiger*
    # plutôt que d'attendre plus longtemps.
    llm_timeout_s: float = Field(40.0, gt=0)
    # Marge que le **navigateur** ajoute à `deadline_s` avant d'abandonner sa requête (AD-11 :
    # `chat.js` borne son attente, sans quoi la saisie reste verrouillée indéfiniment). Elle vit ici
    # et non dans `chat.js` — un seuil numérique n'a qu'un domicile (convention du projet) — et
    # `GET /sante` la publie pour que le front la lise au lieu de la recopier. Sous la deadline du
    # serveur, le navigateur couperait une requête à laquelle il aurait répondu : la marge est donc
    # strictement positive (`gt=0`), et s'ajoute à `deadline_s` au lieu de la remplacer.
    client_abort_margin_s: float = Field(10.0, gt=0)
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
    # ~90 tokens de plus par qualité établie, soit ~1 200 tokens de plus au pire : 3 072.
    verifier_sinistre_max_tokens: int = Field(3072, ge=1)

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
    # **9, et non 8 (02/09/2026, tour « budgets Sonnet »).** La séquence la plus longue en consomme
    # exactement huit — *comprendre*, les deux tours de navigation, *rédiger*, *vérifier*, la relance
    # d'AD-3 (`APPELS_DE_LA_RELANCE` = 2) et la reprise de 4.2e (`APPELS_DE_LA_REPRISE` = 1) — si
    # bien qu'à 8 le premier retry motivé d'un parse invalide (AD-16, « 1 retry ») n'avait plus de
    # place : il ressortait en `BudgetExceeded` terminal sur un chemin conforme. 9 est le **minimum**
    # qui rend ce retry survivable, et c'est délibérément le minimum : une unité de plus autoriserait
    # un second retry, c'est-à-dire la porte d'une boucle. Le garde-fou du coût reste ailleurs et
    # s'applique avant chaque envoi (`max_cost_eur_per_request`).
    max_llm_attempts: int = Field(9, ge=1)
    max_llm_turns: int = Field(2, ge=1, le=2)
    # Décision 2.6 mesurée : Haiku réduit le coût de navigation. `reason` reste autorisé pour
    # rejouer l'arbitrage, mais n'est plus le défaut.
    # Le triplet servi vient d'un artefact versionné unique. Les champs restent surchargeables par
    # environnement pour qu'une cellule d'éval exécute ses réglages sans réécrire le défaut.
    # Une nouvelle instance relit l'artefact : après promotion atomique, HTTP, pipeline direct et
    # runner convergent au prochain démarrage/chargement sans dépendre d'une constante importée
    # avant la publication. Les variables d'environnement gardent leur priorité Pydantic normale.
    retrieval_variant: Literal["deterministe", "outils", "full_context"] = RETRIEVAL_DEFAULT.variant
    retrouver_outils_tier: Literal["micro", "reason"] = RETRIEVAL_DEFAULT.tier
    retrieval_prompt_cache: bool = RETRIEVAL_DEFAULT.prompt_cache
    # Artefact exact réservé aux runners et ingestions hors ligne. L'API en ligne emploie un sink
    # mémoire et ne crée jamais ce fichier (AD-10/AD-15). Rotation et rétention bornent le disque.
    llm_audit_path: Path = REPO_ROOT / ".audit" / "llm-calls.jsonl"
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
    max_cost_eur_per_request: float = Field(0.45, ge=0)
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
    evals_max_cost_eur: float = Field(7.0, ge=0)
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
    live_budget_eur: float = Field(7.00, gt=0)
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
    draft_max_segments: int = Field(6, ge=1)
    draft_max_claims: int = Field(4, ge=1)
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
    # pas. Elle ne remplace rien : quelle que soit sa taille, le navigateur devra chercher. Elle est
    # donc bornée bien plus bas, et le préfixe est payé à **chaque** requête. Calibré sur la seule
    # contrainte qui le borne réellement : le contrat AXA (750 nœuds) n'a pas de marge sur
    # `max_cost_eur_per_request` — `tests/test_sinistre_live.py::test_preflight_outils_nominal_passe_
    # et_un_depassement_reste_refuse` rougit dès 8 000, et c'est lui qui tient cette valeur.
    summary_slice_max_chars: int = Field(6000, ge=200)
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
    # Majorant vérifié **avant** toute construction de client (idiome `type_clauses`).
    structure_max_cost_eur: float = Field(5.0, gt=0)

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
    type_clauses_arbitration_confidence_min: float = Field(0.8, ge=0, le=1)
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
                            ("verifier_max_tokens", self.verifier_max_tokens),
                            ("verifier_sinistre_max_tokens", self.verifier_sinistre_max_tokens)):
            # Le plafond par étape ne peut pas dépasser le plafond de sortie du client : il part tel
            # quel au fournisseur et entre au tarif `output` dans le majorant `estimate_cost` (NFR4).
            if valeur > self.llm_max_output_tokens:
                raise ValueError(f"{nom} ({valeur}) doit être <= llm_max_output_tokens "
                                 f"({self.llm_max_output_tokens})")
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

    def thresholds(self) -> dict[str, float | int]:
        """Seuils actifs, tels qu'exposés dans `Trace.thresholds`."""
        return {
            "deadline_s": self.deadline_s,
            "llm_timeout_s": self.llm_timeout_s,
            "client_abort_margin_s": self.client_abort_margin_s,
            "raison_publiable_max_chars": self.raison_publiable_max_chars,
            "quote_min_chars": self.quote_min_chars,
            "quote_min_ratio": self.quote_min_ratio,
            "max_opens": self.max_opens,
            "profil_max_opens": self.profil_max_opens,
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
            "llm_audit_retention_files": self.llm_audit_retention_files,
            "llm_max_output_tokens": self.llm_max_output_tokens,
            "llm_retry_margin_s": self.llm_retry_margin_s,
            "comprendre_max_tokens": self.comprendre_max_tokens,
            "rediger_max_tokens": self.rediger_max_tokens,
            "verifier_max_tokens": self.verifier_max_tokens,
            "verifier_max_claims": self.verifier_max_claims,
            "verifier_sinistre_max_tokens": self.verifier_sinistre_max_tokens,
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
            "structure_max_input_chars": self.structure_max_input_chars,
            "structure_max_output_tokens": self.structure_max_output_tokens,
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
            "type_clauses_max_blocks_per_request": self.type_clauses_max_blocks_per_request,
            "type_clauses_max_input_chars": self.type_clauses_max_input_chars,
            "type_clauses_max_requests_per_batch": self.type_clauses_max_requests_per_batch,
            "type_clauses_max_output_tokens": self.type_clauses_max_output_tokens,
            "type_clauses_max_cost_eur": self.type_clauses_max_cost_eur,
            "type_clauses_arbitration_confidence_min": self.type_clauses_arbitration_confidence_min,
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
