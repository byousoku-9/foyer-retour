"""Configuration centralisée (pydantic-settings).

Tous les seuils numériques `[HYPOTHÈSE]` du spine vivent ici et nulle part ailleurs ;
ils sont exposés dans `Trace.thresholds` via `Settings.thresholds()` et se règlent avec les évals.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

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
    # AD-11 : `GET /api/v1/sante` publie `version: sha7`. En production, il vient de la
    # **configuration du service** Cloud Run — `deploy.yml` pose `GIT_SHA=<sha7>`, ce que
    # `gcloud run deploy --source` sait faire alors qu'il n'accepte aucun `--build-arg` —, et cette
    # variable recouvre le `ENV GIT_SHA` que le `Dockerfile` laisse à `dev`. Hors conteneur, `dev`.
    # C'est cette valeur que le smoke de déploiement compare au SHA du commit qui l'a déclenché : sans
    # elle, il mesurerait une révision qu'il n'a pas construite. Ce n'est pas un seuil numérique : il
    # n'entre pas dans `thresholds()`.
    git_sha: str = "dev"

    # Temps (AD-1, AD-9)
    deadline_s: float = Field(55.0, gt=0)
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
    # avec les questions-témoins (4.2) — la relance rattrape-t-elle des réponses, ou brûle-t-elle du
    # budget ? Faux tant que rien ne l'a montré ; la relance reste inconditionnelle quand **aucune**
    # affirmation n'a survécu (là, elle est le seul chemin vers une réponse).
    relance_sur_non_pertinence: bool = False
    historique_max_turns: int = Field(6, ge=0)
    verifier_max_claims: int = Field(8, ge=1)
    verifier_max_tokens: int = Field(1024, ge=1)

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
    # L'appel `micro` du sinistre rend tout ce que rend celui du guide **plus** une entrée
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
    # Global à la requête. La chaîne du guide fait **cinq** appels dans son pire cas nominal —
    # *comprendre*, *rédiger*, *vérifier*, puis la relance unique d'AD-3 et la seconde vérification
    # qu'elle exige — plus une relance motivée du client sur un parse invalide (AD-16, « 1 retry »).
    # À 4, le plafond coupait **après** la relance de *rédiger* et **avant** la seconde vérification :
    # un appel avait démarré, l'échec était donc terminal (AD-16) et une question qui déclenchait la
    # relance d'AD-3 ressortait en 503 au lieu de sa réponse vérifiée — mesuré en live, revue Codex
    # 1.5, tour 3. Ce plafond est une ceinture contre l'emballement ; le garde-fou du coût, lui, est
    # `max_cost_eur_per_request`, qui s'applique **avant** qu'un appel démarre (AD-1).
    max_llm_attempts: int = Field(6, ge=1)
    max_llm_turns: int = Field(2, ge=1)
    # Story 1.4 : `RetrievalBudget` borne aussi le nombre de blocs rendus (AD-1 « blocs, tokens inclus »).
    # C'est le seul poste variable du majorant de *rédiger* : préfixe (sommaire au tarif d'écriture 1 h) et
    # sortie à `rediger_max_tokens` en consomment déjà ≈ 0,080 € des 0,10 € par requête ; 6 fiches entières
    # (65 blocs) portaient l'estimation à 0,108 € et faisaient échouer l'appel à tort (`BudgetExceeded`).
    # Story 2.7 : la couverture partielle fait entrer davantage de nœuds à égalité utile. Mesuré sur
    # la question réelle du délai d'arrivée, 30 blocs portaient le majorant froid de *rédiger* à
    # 0,1006 € après *comprendre*, donc au-dessus du plafond immuable de 0,10 € ; 28 blocs gardent la
    # FAQ et la fiche d'arrivée complètes et remettent l'appel sous le plafond. `search_limit` reste
    # 20 : la coupe porte sur le contexte transmis au modèle, jamais sur les candidats rappelés.
    # À recalibrer avec les questions-témoins, quand elles existeront (impact sur le rappel).
    retrieval_max_blocks: int = Field(28, ge=1)
    # Story 1.4 (revue Codex 1.4, B1) : AD-1 borne l'étape « appels modèle, nœuds, blocs, tokens,
    # définitions et renvois inclus ». Un compte de blocs ne borne pas les tokens — un tableau de fiche
    # pèse dix paragraphes. Faute de tokenizer en code pur, *retrouver* majore avec l'heuristique
    # d'`estimate_cost` (`estimate_chars_per_token`, `estimate_tokenizer_factor`). Valeur : la marge du
    # majorant de *rédiger* (0,10 € − 0,080 € de préfixe et de sortie) au tarif d'entrée `reason`
    # (3 USD/MTok, USD_EUR 0,92) vaut ≈ 7 200 tokens ; 6 000 laisse la marge d'arrondi.
    retrieval_max_tokens: int = Field(6000, ge=1)

    # Coût (AD-9, AD-10)
    max_cost_eur_per_request: float = Field(0.10, ge=0)
    cost_alert_eur: float = Field(0.05, ge=0)
    # AD-9 : « en évals, le plafond par requête est remplacé par un plafond **par run** (`--max-cost`) ».
    # CLAUDE.md le redit : « les évals tournent seulement avec la clé **et un plafond** ». C'est donc
    # un seuil comme les autres — il vit ici, jamais en dur dans `server/evals/run.py`, et `--max-cost`
    # ne fait que le surcharger pour un run. Valeur : le profil `vertical` exécute **deux** cas, à
    # 0,027–0,054 € pièce selon le cas et le run (quatre runs mesurés le 24/08/2026,
    # `docs/tests-live.md` § 1.10 — le cas sinistre coûte environ le double du cas guide) ; 1,00 € laisse donc
    # un facteur dix sur le run que cette story écrit, ce qui borne une dérive sans jamais gêner un
    # re-gate. Il ne suffira **pas** au golden set complet de 4.1 : 40–60 cas au tarif mesuré valent
    # 2 à 3 €, et c'est le cache de réponses d'AD-14 (story 4.1) qui doit ramener ce coût, pas ce
    # plafond qu'on relèverait. `[HYPOTHÈSE]` : à re-régler en 4.1, avec le cache.
    evals_max_cost_eur: float = Field(1.0, ge=0)

    # Client LLM (story 1.3, AD-9) : sortie maximale d'un appel, marge de deadline exigée pour le retry sur parse
    # invalide, heuristique d'estimation avant appel (caractères par token et marge tokenizer, calibrés pour que
    # 2,0/1,3 ≈ 1,54 car./token majore le pire mesuré — 1,65 sur le sommaire du contrat, revue Codex 1.3 B5),
    # délai de `count_tokens`.
    llm_max_output_tokens: int = Field(4096, ge=1)
    llm_retry_margin_s: float = Field(5.0, ge=0)
    # Étapes (story 1.4, NFR4) : sortie maximale par étape — le majorant `estimate_cost` compte la sortie
    # à `max_tokens` ; des plafonds par étape gardent chaque appel sous le plafond par requête (0,10 €).
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

    # Limiteur best-effort par instance (AD-13)
    rate_limit_per_minute: int = Field(10, ge=1)
    rate_limit_per_day: int = Field(100, ge=1)
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

    # Ingestion (AD-8)
    coverage_threshold: float = Field(0.8, ge=0, le=1)
    kind_confidence_min: float = Field(0.7, ge=0, le=1)

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
    # Longueur maximale du périmètre dérivé du corpus et rendu dans le préfixe de *comprendre*
    # (`Corpus.perimetres`). Le préfixe est cacheable (AD-9) et facturé : une projection qui
    # grossirait avec le corpus sans borne ferait grossir chaque appel `micro`. Au-delà, les
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
    fetch_timeout_s: float = Field(30.0, gt=0)
    metadata_timeout_s: float = Field(2.0, gt=0)  # serveur de métadonnées GCP (jeton du repli gs://)

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
        if self.header_caps_max_size_pt >= self.title_min_size_pt:
            raise ValueError(f"header_caps_max_size_pt ({self.header_caps_max_size_pt}) doit être "
                             f"< title_min_size_pt ({self.title_min_size_pt})")
        for nom, valeur in (("comprendre_max_tokens", self.comprendre_max_tokens),
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

    def thresholds(self) -> dict[str, float | int]:
        """Seuils actifs, tels qu'exposés dans `Trace.thresholds`."""
        return {
            "deadline_s": self.deadline_s,
            "llm_timeout_s": self.llm_timeout_s,
            "client_abort_margin_s": self.client_abort_margin_s,
            "quote_min_chars": self.quote_min_chars,
            "quote_min_ratio": self.quote_min_ratio,
            "max_opens": self.max_opens,
            "profil_max_opens": self.profil_max_opens,
            "node_window": self.node_window,
            "search_limit": self.search_limit,
            "max_llm_attempts": self.max_llm_attempts,
            "max_llm_turns": self.max_llm_turns,
            "retrieval_max_blocks": self.retrieval_max_blocks,
            "retrieval_max_tokens": self.retrieval_max_tokens,
            "max_cost_eur_per_request": self.max_cost_eur_per_request,
            "cost_alert_eur": self.cost_alert_eur,
            "evals_max_cost_eur": self.evals_max_cost_eur,
            "llm_max_output_tokens": self.llm_max_output_tokens,
            "llm_retry_margin_s": self.llm_retry_margin_s,
            "comprendre_max_tokens": self.comprendre_max_tokens,
            "rediger_max_tokens": self.rediger_max_tokens,
            "verifier_max_tokens": self.verifier_max_tokens,
            "verifier_max_claims": self.verifier_max_claims,
            "verifier_sinistre_max_tokens": self.verifier_sinistre_max_tokens,
            "fait_manquant_max_chars": self.fait_manquant_max_chars,
            "ask_client_max": self.ask_client_max,
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
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "rate_limit_per_day": self.rate_limit_per_day,
            "rate_limit_max_clients": self.rate_limit_max_clients,
            "retry_after_s": self.retry_after_s,
            "request_max_bytes": self.request_max_bytes,
            "cloud_trace_max_chars": self.cloud_trace_max_chars,
            "coverage_threshold": self.coverage_threshold,
            "kind_confidence_min": self.kind_confidence_min,
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
            "dictionary_max_output_tokens": self.dictionary_max_output_tokens,
            "dictionary_max_cost_eur": self.dictionary_max_cost_eur,
            "dictionary_batch_poll_s": self.dictionary_batch_poll_s,
            "dictionary_batch_timeout_s": self.dictionary_batch_timeout_s,
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
