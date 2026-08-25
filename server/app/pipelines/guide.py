"""AD-1 / AD-10 — Le pipeline du guide : les cinq étapes, les court-circuits, la relance unique, la trace.

`comprendre → retrouver → rédiger → vérifier → restituer`, dans cet ordre, toujours. Ce que le
pipeline ajoute aux étapes — et qui n'appartient à aucune d'elles :

- **les bornes d'entrée** : `RetrievalBudget` rempli depuis `settings`, historique borné par
  `historique_max_turns` (au-delà : `InvalidRequest`, **jamais** de troncature silencieuse — AD-11,
  AD-16), deadline vérifiée avant chaque étape ;
- **les court-circuits d'AD-5** : `intent ∈ {meteo, bavardage, hors_perimetre}`, clarification
  requise, « zéro hit » du dictionnaire validé (**avant** *retrouver*, story 2.1), ou retrieval vide
  ⇒ *restituer* directement, **sans** appel `reason` ;
- **la relance d'AD-3** : une claim rejetée ⇒ **une** relance de *rédiger* avec le motif de
  *vérifier* ; un draft identique (même hash canonique) arrête là ;
- **la trace d'AD-10** : les `StepTrace` de chaque étape, les digests, les seuils, les hashes du
  corpus — et jamais le texte d'un bloc.

Le pipeline ne voit ni `corpus` ni `llm` (table des couches du spine) : `corpus`, `index` et `client`
sont annotés `Any` et lui viennent de l'appelant (l'API, story 1.6). Ce n'est pas un renoncement au
typage, c'est l'invariant lui-même : un pipeline qui ne peut pas importer `llm` ne peut pas appeler
un modèle hors d'une étape.
"""

from __future__ import annotations

from typing import Any

from server.app.config import Settings
from server.app.domain.answer import AbsenceProof, Answer
from server.app.domain.errors import (
    BudgetExceeded,
    CorpusUnavailable,
    InvalidRequest,
    LlmParse,
    LlmUnavailable,
    PipelineError,
    Timeout,
)
from server.app.domain.profil import Profil
from server.app.domain.question import ClarificationRequise, ParsedQuestion, Turn
from server.app.domain.trace import CheckResult, StepTrace, Trace
from server.app.pipelines.commun import (
    APPELS_DE_LA_RELANCE,
    INTENTS_REFUSES,
    blocs_cites,
    dictionnaire_de,
    digests,
    domine,
    gate_de,
    libelles_de_blocs,
    normaliser_langue_pipeline,
    relance_abandonnee,
    relance_utile,
    retrieval_budget,
)
from server.app.steps.comprendre import comprendre
from server.app.steps.rediger import rediger
from server.app.steps.restituer import restituer
from server.app.steps.retrouver import retrouver_deterministe, retrouver_outils
from server.app.steps.verifier import verifier

PIPELINE = "guide"
VARIANT = "outils"
VARIANT_DETERMINISTE = "deterministe"
VARIANTS = frozenset({VARIANT, VARIANT_DETERMINISTE})
# L'alerte du loader qui dit que le périmètre annoncé à *comprendre* n'est plus exhaustif
# (`corpus/loader._perimetre`, palier 3). Nommée ici parce que c'est ici qu'elle **désarme** un refus.
PERIMETRE_TRONQUE = "perimetre_tronque"
LIBELLES_INTENT = {
    "meteo": "météo",
    "bavardage": "bavardage",
    "hors_perimetre": "hors périmètre",
}


def _intention_expliquee(intent: str, question_resolue: str, dictionnaire: Any) -> CheckResult:
    """AD-5 — Les déclencheurs du dictionnaire **expliquent** le refus par intent ; ils ne le décident pas.

    AD-5, dernière phrase : « les déclencheurs d'intention sont distincts des mots du corpus — la
    présence d'un mot n'est jamais une preuve de pertinence ». Le refus reste celui de *comprendre*,
    qui a lu la question entière ; ce contrôle ne fait que **compter** combien de déclencheurs de
    l'intention rendue se retrouvent dans la question résolue, et rien d'autre. Zéro déclencheur
    n'annule rien : le contrôle passe en échec, et l'écran montre alors un refus fondé sur le seul
    jugement du modèle — ce qui est un fait, pas une faute.

    **Des comptes, jamais les mots** (AD-10, AD-4) : un déclencheur reconnu dans une question est un
    fragment de cette question, et `AbsenceProof` interdit déjà de publier la liste des déclencheurs.
    Le libellé nomme l'`intent` tel que le domaine l'écrit (`meteo`, `bavardage`, `hors_perimetre`) :
    c'est un identifiant, pas une phrase, et rien n'a à le traduire ici.

    Le compte porte sur `question_resolue` — jamais sur la question brute —, comme les deux
    court-circuits d'AD-5.
    """
    reconnus, total = (dictionnaire.confirme(intent, question_resolue)
                       if dictionnaire is not None else (0, 0))
    libelle = LIBELLES_INTENT.get(intent, intent)
    if reconnus:
        detail = (f"intention « {libelle} » — {reconnus} déclencheur(s) du dictionnaire "
                  f"sur {total} la confirment")
    else:
        detail = (f"intention « {libelle} » — aucun déclencheur ne la confirme "
                  f"({total} connu(s)) : le refus tient au seul jugement du modèle")
    return CheckResult(name="intention_expliquee", ok=bool(reconnus), detail=detail)


def _absence(kind: str, parsed: ParsedQuestion | None, *, doc_id: str, corpus: Any,
             dictionnaire: Any = None) -> AbsenceProof:
    """Preuve d'absence (AD-4) : ce qui a été cherché, jamais les variantes ni les déclencheurs.

    `terms_searched` porte les **canoniques**, littéralement comme AD-4 les nomme
    (`terms_searched[] (canoniques)`, « jamais la liste des variantes ni des déclencheurs
    d'intention »). Un terme que le dictionnaire reconnaît comme variante sort donc sous le canonique
    de son groupe (`Dictionnaire.canoniser`) : la preuve dit les **notions** cherchées, pas
    l'orthographe employée. Il recopiait les termes de *comprendre* tels quels (revue Codex 2.1,
    B5) — mesuré sur l'artefact livré : « Arbeitsamt », reconnu dans le groupe « ADEM », ressortait
    dans `terms_searched`, c'est-à-dire une variante publiée. Un terme inconnu du dictionnaire est
    son propre canonique et sort inchangé ; dictionnaire inutilisable pour ce document ⇒ tous les
    termes sortent inchangés.

    `variants_count` est le nombre de formes **ajoutées** effectivement cherchées. Il vaut `0` quand
    le dictionnaire n'est pas utilisable pour ce document (absent, illisible, d'un autre corpus) :
    aucune variante n'a été essayée, et l'annoncer autrement serait faux — c'est la seule chose que
    ce chiffre promet.
    """
    if parsed is None:  # rien n'a été cherché : ni termes, ni passages parcourus
        return AbsenceProof(kind=kind)
    document = corpus.documents.get(doc_id)
    termes = parsed.termes_de_recherche()
    elargi = dictionnaire is not None and dictionnaire.utilisable_pour(doc_id)
    return AbsenceProof(kind=kind,
                        terms_searched=dictionnaire.canoniser(termes) if elargi else termes,
                        variants_count=dictionnaire.variants_count(termes) if elargi else 0,
                        blocks_scanned=len(document.blocks) if document is not None else 0,
                        documents=[doc_id] if document is not None else [])


async def repondre_guide(question: str, historique: list[Turn], profil: Profil, *, corpus: Any,
                         index: Any, client: Any, settings: Settings, request_id: str,
                         lang: str | None = None, doc_id: str | None = None, budget: Any = None,
                         pipeline_digest_hex: str | None = None,
                         prompts_digest_hex: str | None = None,
                         dictionnaire: Any = None,
                         variant: str = VARIANT) -> tuple[Answer, Trace]:
    """Une question du guide → l'unique `Answer` d'AD-4 et sa `Trace`.

    Toute sortie normale — réponse, refus, clarification, claims toutes rejetées — est un `Answer`
    (l'API en fera un 200, AD-11). Seules les entrées hors bornes (`InvalidRequest`) et les échecs
    terminaux des étapes (`Timeout`, `LlmParse`, `BudgetExceeded`, `LlmUnavailable`) remontent.
    """
    if variant not in VARIANTS:
        # Avant le budget et surtout avant *comprendre* : une faute de variante ne coûte rien.
        raise InvalidRequest(f"variant inconnu : {variant!r}")
    doc_id = doc_id or settings.guide_doc_id
    lang = normaliser_langue_pipeline(lang)
    if doc_id not in corpus.documents:
        # Document en quarantaine, absent ou mal nommé : `retrouver` lèverait un `KeyError` nu **après**
        # *comprendre*, donc après un appel facturé, et l'API en ferait un 500 (revue 1.5). AD-16 a un
        # code pour ça, et le contrôle a sa place ici, avec les autres bornes d'entrée.
        raise CorpusUnavailable(f"document {doc_id!r} non servi (absent du corpus ou en quarantaine)")
    # La longueur de `question` et celle de chaque tour sont bornées par le contrat HTTP d'AD-11
    # (≤ 1 000 et ≤ 2 000 caractères, story 1.6 ; `Turn.texte` porte déjà la seconde dans le domaine).
    # Le **nombre** de tours, lui, est une borne du pipeline : c'est lui qui passe l'historique à
    # *comprendre* et à *rédiger*.
    if len(historique) > settings.historique_max_turns:
        # Avant le premier appel modèle : une requête refusée ne coûte rien (AD-11 : 400 au-delà,
        # jamais tronqué côté serveur — tronquer perdrait le tour qui porte l'anaphore).
        raise InvalidRequest(f"historique de {len(historique)} tours : la limite est "
                             f"{settings.historique_max_turns} (jamais tronqué côté serveur)")
    if budget is None:
        budget = client.new_budget()

    steps: list[StepTrace] = []
    relances = 0
    truncated = False
    # AD-10 : l'`intent` est logué par l'API, qui ne voit que l'`Answer` et la `Trace` — et `Answer`
    # ne le porte pas. Il est renseigné dès que *comprendre* a rendu l'une de ses deux sorties
    # (`ParsedQuestion` ou `ClarificationRequise` : les deux portent un `intent`), et reste `None` si
    # l'étape n'a pas abouti.
    intent: str | None = None
    # Politique par requête, publiée par `DictionnaireTrace` autant qu'appliquée au pré-contrôle.
    # Initialisée avant `tracer()` pour que les traces partielles précédant *comprendre* restent sûres.
    hors_perimetre_desarme = False

    def echeance(avant: str) -> None:
        """AD-1/AD-9 : la deadline monotone est vérifiée **avant** chaque étape, jamais après coup."""
        if budget.remaining() <= 0:
            raise Timeout(f"deadline épuisée avant l'étape {avant} ({budget.remaining():.1f} s restantes)")

    def tracer() -> Trace:
        digest_pipeline, digest_prompts = (pipeline_digest_hex, prompts_digest_hex)
        if digest_pipeline is None or digest_prompts is None:
            defaut = digests()
            digest_pipeline = digest_pipeline if digest_pipeline is not None else defaut[0]
            digest_prompts = digest_prompts if digest_prompts is not None else defaut[1]
        entry = corpus.manifest.get(doc_id)
        # AD-10 : `retries` et `truncations` ne sont pas définis par le spine — ici, une fois pour
        # toutes : `retries` = les relances motivées du client (parse invalide) plus les relances de
        # *rédiger* décidées par AD-3 ; `truncations` = le retrieval coupé (0 ou 1 par requête).
        retries = sum(1 for s in steps for c in s.checks if c.name == "parse_retry") + relances
        return Trace(
            request_id=request_id, pipeline=PIPELINE, variant=variant, intent=intent, steps=steps,
            total_cost_eur=round(budget.cost_eur, 4),
            source_hash={doc_id: entry.source_hash} if entry is not None else {},
            ingest_fingerprint={doc_id: entry.ingest_fingerprint} if entry is not None else {},
            pipeline_digest=digest_pipeline, prompts_digest=digest_prompts,
            thresholds=settings.thresholds(), retries=retries, truncations=int(truncated),
            deadline_remaining_s=round(budget.remaining(), 3),
            # Story 2.5 : ce que la trace nommait déjà, résolu. Les trois helpers sont partagés avec
            # le pipeline sinistre (`pipelines/commun`) et ne lisent que le corpus déjà chargé —
            # aucun appel, aucune lecture de `data/`, aucun texte de bloc (AD-10).
            blocs=libelles_de_blocs(corpus, doc_id, steps),
            gate=gate_de(corpus, doc_id),
            dictionnaire=dictionnaire_de(
                dictionnaire, doc_id,
                court_circuit_autorise=not hors_perimetre_desarme),
        )

    def refuser(kind: str, parsed: ParsedQuestion | None, *, language: str,
                lang_fallback: bool = False,
                clarification: str | None = None) -> tuple[Answer, Trace]:
        echeance("restituer")  # *restituer* est une étape : la deadline se vérifie avant elle aussi
        answer, step = restituer(language=language, lang_fallback=lang_fallback,
                                 reason=_absence(kind, parsed, doc_id=doc_id, corpus=corpus,
                                                 dictionnaire=dictionnaire),
                                 clarification=clarification)
        steps.append(step)
        return answer, tracer()

    async def chaine() -> tuple[Answer, Trace]:
        """Les cinq étapes. Sortie normale : un `Answer` et sa `Trace`. Échec terminal : `PipelineError`."""
        nonlocal relances, truncated, intent, hors_perimetre_desarme
        # --- comprendre -----------------------------------------------------
        echeance("comprendre")
        # Le périmètre annoncé à *comprendre* vient du **corpus** (story 2.1) : `Corpus.perimetres`
        # est une projection des titres du document servi, calculée une fois au chargement. La liste
        # écrite à la main dans `prompts/comprendre.md` ne nommait pas l'identité numérique, et
        # « Comment obtenir LuxTrust au meilleur prix ? » ressortait `hors_perimetre` alors que le
        # guide a une fiche entière dessus (faux refus mesuré le 2026-08-24). Le préfixe reste
        # déterministe — donc cacheable (AD-9) — mais il devient vrai.
        #
        # `parcours` (story 2.3, revue Codex 2.3 B1) suit le même chemin que `perimetre` : c'est une
        # donnée du **corpus** (`Document.parcours`, projetée de la `timeline` de la source à
        # l'ingestion), et l'étape ne voit pas le corpus. C'est elle qui en dérive `scope.noeuds`,
        # par du code pur — l'AC place la construction du `scope` dans *comprendre*, et AD-1 ne
        # laisse passer vers *retrouver* que `ParsedQuestion`.
        parsed, step_comprendre = await comprendre(question, historique, profil, client=client, budget=budget,
                                                   settings=settings, lang=lang,
                                                   perimetre=corpus.perimetres.get(doc_id, ""),
                                                   parcours=corpus.documents[doc_id].parcours)
        steps.append(step_comprendre)
        intent = parsed.intent  # AD-10 : les deux sorties de *comprendre* en portent un

        # AD-5 : deux sorties typées exclusives. Une question non autonome n'atteint jamais *retrouver*.
        if isinstance(parsed, ClarificationRequise):
            # AD-5, mot pour mot : « une anaphore non résoluble avec l'historique produit
            # `Answer.clarification: str` (question à l'utilisateur) ». Elle est servie **dans tous
            # les cas**, y compris sous un repli de détection (revue Codex 2.4, tour 2, NB1) : le
            # tour 1 la retirait alors, ce qui privait l'AC 2.2 du tour d'historique qu'elle
            # reconduit. La divergence de langue n'est pas tue pour autant — `lang_fallback` la
            # publie et le front l'affiche (voir `ClarificationRequise.langue_affirmee`).
            return refuser("clarification_requise", None, language=parsed.language,
                           lang_fallback=parsed.lang_fallback,
                           clarification=parsed.clarification)
        if parsed.intent in INTENTS_REFUSES:
            # Court-circuit d'AD-5 : l'étage `reason` n'est jamais atteint pour un refus par intent.
            # Il reste actif **dans tous les cas**, dictionnaire validé ou non : c'est le seul des
            # deux court-circuits d'AD-5 qui ne dépende de rien d'autre que de l'`intent`.
            #
            # **Une exception, et une seule : le périmètre tronqué désarme le refus qu'il ne peut
            # plus fonder** (story 2.5 ; reprise différée de la revue Codex 2.1, I2). `hors_perimetre`
            # est la seule des trois intentions que le modèle rende *en lisant une liste que nous lui
            # donnons* — la projection des titres du document (`Corpus.perimetres`), dont le prompt
            # affirme qu'« elle fait foi, aucune autre ». Quand le loader a dû en retirer des
            # catégories entières (alerte `perimetre_tronque`), cette affirmation est fausse : le
            # modèle a classé hors périmètre sur une liste incomplète, et le refus porterait sur des
            # sujets que le document traite.
            #
            # Des deux remèdes que la reprise proposait — « soit le refus se désactive, soit la
            # réponse le dit » —, le premier est strictement meilleur : dire à l'utilisateur « ce
            # refus est peut-être faux » lui laisse un refus ; poursuivre vers *retrouver* lui laisse
            # une chance de réponse, et si le corpus n'a rien, le refus rendu porte alors une
            # **preuve** (`AbsenceProof`) au lieu d'un jugement sur une liste amputée. Le désarmement
            # est tracé, donc il n'est pas silencieux (AD-16).
            #
            # `meteo` et `bavardage` restent refusés dans tous les cas : ni l'un ni l'autre ne se
            # décide sur le périmètre annoncé — une question sur la pluie de demain reste hors du
            # guide quelle que soit la liste de ses catégories.
            if (parsed.intent == "hors_perimetre"
                    and PERIMETRE_TRONQUE in corpus.alerts.get(doc_id, [])):
                hors_perimetre_desarme = True
                step_comprendre.checks.append(CheckResult(
                    name="hors_perimetre_desarme", ok=False,
                    detail=f"le périmètre annoncé au modèle est tronqué ({PERIMETRE_TRONQUE}) : "
                           "le refus « hors périmètre » ne peut pas se fonder sur une liste "
                           "incomplète — la question poursuit vers retrouver"))
            else:
                step_comprendre.checks.append(
                    _intention_expliquee(parsed.intent, parsed.question_resolue, dictionnaire))
                return refuser("hors_perimetre", None, language=parsed.language,
                               lang_fallback=parsed.lang_fallback)

        # --- court-circuit « zéro hit » d'AD-5, **avant** *retrouver* -------
        # AD-5, mot pour mot : « court-circuit vers *restituer* … si aucun terme canonique (ni ses
        # variantes) n'a de hit dans l'index », et « si `validated=false` ou `corpus_source_hashes`
        # ne correspond pas au corpus chargé, le court-circuit « zéro hit » est **désactivé** et la
        # requête poursuit vers *retrouver* ». `court_circuit_actif` porte exactement cette
        # disjonction, et `/api/v1/sante` la publie.
        #
        # **Distinct du garde-fou « zéro bloc » de 1.5**, plus bas : celui-ci est fondé sur le
        # dictionnaire et s'exécute avant *retrouver* ; l'autre constate après coup qu'aucun bloc
        # n'est citable. Les deux produisent `kind="zero_hit"` — c'est le même fait pour
        # l'utilisateur — et ce qui les distingue est observable : la présence de l'étape *retrouver*
        # dans la trace.
        #
        # Il porte sur `ParsedQuestion.question_resolue` (les termes en viennent, AD-5) et sur des
        # termes **toujours en français** (invariant de `ParsedQuestion.terms`), et n'est jamais
        # atteint après un appel `reason` : *comprendre* est un appel `micro`, et rien d'autre n'a
        # tourné à ce point de la chaîne.
        #
        # **Il doit être au moins aussi large que ce que *retrouver* trouve** (revue coordonnée
        # 2.1). *retrouver* peuple `retrieval.blocs` avec **deux** outils — `index.chercher()` et
        # `index.definitions()`, dont les résultats sont hors des hits de `chercher` — et conclure
        # sur le premier seul refusait d'avance une question sans hit mais couverte par un bloc
        # `defines`, à laquelle la chaîne complète répondait. Inatteignable sur le corpus servi
        # aujourd'hui (zéro bloc du guide ne porte de `defines`), armé par le typage automatique de
        # la story 3.2 : ce n'est pas un correctif spéculatif, c'est la règle d'AD-5 (« aucun terme
        # canonique **ni ses variantes** n'a de hit dans l'index ») appliquée à l'index entier.
        #
        # **Ce que le pré-contrôle coûte, écrit plutôt que laissé à deviner** (revue coordonnée 2.1).
        # Il balaye les entrées de l'index (506 pour le guide) deux fois — `chercher` puis
        # `definitions` — et *retrouver* les rebalaye quelques lignes plus bas sur le chemin nominal ;
        # `expand()` est par ailleurs recalculé par `variants_count` dans `_absence`, puis par
        # *retrouver*, soit deux à trois fois les mêmes formes pour ≤ `question_max_terms` termes.
        # Rien de tout cela n'est mémoïsé, et c'est un choix : ce sont des comparaisons de chaînes
        # déjà normalisées, sans appel ni allocation notable, et le seul chemin où le travail est
        # perdu est celui du **refus** — où il évite un appel `reason` à ≈ 0,03 € (NFR4), quatre
        # ordres de grandeur au-dessus. Le rendre unique demanderait de faire voyager l'expansion à
        # travers `refuser`, `_absence` et `retrouver_deterministe` : trois signatures élargies pour
        # un gain non mesurable, et un état de plus à tenir cohérent. À reprendre si, et seulement
        # si, une mesure le montre (4.2). Le pré-contrôle n'a lieu que dictionnaire **signé**.
        termes = parsed.termes_de_recherche()
        # `court_circuit_pour(doc_id)` et non `court_circuit_actif` (revue Codex 2.1, B3) : le
        # dictionnaire n'arme un refus que sur le document dont il porte l'empreinte.
        # Amendement AD-5, story 2.5 : une preuve issue de la recherche effectivement menée doit
        # remplacer tout pré-contrôle quand le même périmètre tronqué a désarmé le refus.
        if (not hors_perimetre_desarme and termes and dictionnaire is not None
                and dictionnaire.court_circuit_pour(doc_id)):
            echeance("court-circuit zéro hit")  # comme avant chaque étape (AD-1/AD-9)
            if not index.chercher(dictionnaire.expand(termes), limit=1, doc_id=doc_id) \
                    and not index.definitions(termes, doc_id=doc_id):
                return refuser("zero_hit", parsed, language=parsed.language,
                               lang_fallback=parsed.lang_fallback)
        # Aucun terme extrait : rien n'a été cherché, donc rien ne prouve une absence (AD-1). La
        # question poursuit vers *retrouver*, et c'est le garde-fou « zéro bloc » qui tranchera.

        # --- retrouver (code pur) -------------------------------------------
        echeance("retrouver")
        # AD-1 : *retrouver* ne voit que `ParsedQuestion`. Les nœuds que le profil désigne y sont
        # déjà — `parsed.scope.noeuds`, construits par *comprendre* (story 2.3, canal corrigé par la
        # revue Codex 2.3, B1) —, et le pipeline n'a rien à leur ajouter ici.
        borne_retrieval = retrieval_budget(settings)
        if variant == "outils":
            candidats_outils: list[str] = []
            try:
                retrieval, step_retrouver = await retrouver_outils(
                    parsed, corpus=corpus, index=index, budget=borne_retrieval, settings=settings,
                    client=client, request_budget=budget, doc_id=doc_id, dictionnaire=dictionnaire,
                    candidats_out=candidats_outils)
            except PipelineError as exc:
                if exc.step is not None:
                    steps.append(exc.step)
                exc.trace = tracer()
                raise
        else:
            retrieval, step_retrouver = retrouver_deterministe(
                parsed, corpus=corpus, index=index, budget=borne_retrieval,
                settings=settings, doc_id=doc_id, dictionnaire=dictionnaire)
        if variant == "outils" and retrieval.truncated and not retrieval.blocs:
            # O9 : le repli protège uniquement une navigation épuisée sans bloc utile. Des blocs
            # outils partiels restent un contexte honnête : *vérifier* publiera `lecture_bornee` et
            # `complete=False`, sans les remplacer par une sélection déterministe potentiellement
            # moins pertinente et plus coûteuse.
            candidats_deterministes: list[str] = []
            fallback, fallback_step = retrouver_deterministe(
                parsed, corpus=corpus, index=index, budget=borne_retrieval,
                settings=settings, doc_id=doc_id, dictionnaire=dictionnaire,
                candidats_out=candidats_deterministes)
            step_retrouver.checks.append(CheckResult(
                name="repli_deterministe", ok=False,
                detail="navigation par outils tronquée sans bloc ; repli déterministe borné transmis"))
            step_retrouver.checks.extend(fallback_step.checks)
            step_retrouver.ms += fallback_step.ms
            step_retrouver.opened_block_ids = list(fallback.opened_block_ids)
            finaux = set(fallback.opened_block_ids)
            candidats = [*candidats_outils, *candidats_deterministes]
            discarded = list(dict.fromkeys(b for b in candidats if b not in finaux))
            step_retrouver.discarded_block_ids = discarded
            retrieval = fallback.model_copy(update={"discarded_block_ids": discarded})
        steps.append(step_retrouver)
        truncated = retrieval.truncated
        if not retrieval.blocs and retrieval.truncated:
            # AD-1, littéralement : « budget épuisé ou troncature non résolue ⇒ `complete=False` et
            # **aucune absence du corpus n'est affirmée** » (NFR2 le répète). Un retrieval vide *parce
            # que* le budget a tout écarté ne dit rien du corpus : le convertir en `zero_hit` fabriquerait
            # une preuve d'absence à partir d'une borne qui est la nôtre (revue Codex 1.5, B4). Il n'y a
            # pas d'`Answer` honnête à rendre ici — c'est une erreur terminale, avec son code.
            raise BudgetExceeded(
                f"le budget de retrieval n'a laissé passer aucun bloc ({settings.retrieval_max_blocks} blocs, "
                f"{settings.retrieval_max_tokens} tokens) : aucune absence du corpus n'est affirmée")
        if not retrieval.blocs:
            # Court-circuit « zéro bloc », **après** *retrouver* : distinct de celui d'AD-5, qui est fondé
            # sur le dictionnaire et reste désactivé tant que `validated=false` (story 2.1). Appeler
            # `reason` sans un seul bloc citable ne peut produire que des claims sans source — et coûte
            # le prix plein. L'`AbsenceProof` dit ce qui a été cherché, jamais que l'information n'existe
            # pas (AD-1).
            return refuser("zero_hit", parsed, language=parsed.language,
                           lang_fallback=parsed.lang_fallback)

        # --- rédiger --------------------------------------------------------
        echeance("rediger")
        rediger_max_tokens = (settings.outils_rediger_max_tokens if variant == "outils" else None)
        draft, step_rediger = await rediger(parsed, retrieval, historique, client=client, budget=budget,
                                            index=index, doc_id=doc_id, settings=settings,
                                            max_tokens=rediger_max_tokens)
        steps.append(step_rediger)

        # --- vérifier -------------------------------------------------------
        echeance("verifier")
        verification, step_verifier = await verifier(draft, parsed=parsed, retrieval=retrieval, corpus=corpus,
                                                     index=index, client=client, budget=budget, settings=settings)
        steps.append(step_verifier)

        # --- relance unique (AD-3) ------------------------------------------
        if verification.motif and relance_utile(verification, settings):
            acquise = verification  # ce qui est déjà vérifié : une relance ne peut que l'améliorer
            # Compteur d'appels **avant** l'appel en cours : c'est lui qui dit si un appel a démarré,
            # quoi qu'ait fait l'étape qui a échoué (voir le `except` plus bas). Il est **ré-armé**
            # après chaque appel réussi de la relance : une relance rédigée puis laissée sans seconde
            # vérification faute de budget n'est pas un appel raté, c'est un appel qui n'a jamais
            # démarré (revue Codex 1.5, tour 3 — mesuré en live, la chaîne ressortait en 503).
            appels_avant = budget.attempts
            try:
                if budget.remaining() <= settings.llm_retry_margin_s:
                    # AD-1, littéralement : « aucun retry ne démarre sans marge ». Le retry ne démarre
                    # pas ; la requête, elle, a déjà sa réponse vérifiée.
                    raise Timeout(f"marge insuffisante pour la relance ({budget.remaining():.1f} s restantes)")
                if budget.attempts + APPELS_DE_LA_RELANCE > budget.max_attempts:
                    # Même règle, appliquée au **compteur d'appels** : une relance, c'est deux appels
                    # — rédiger puis vérifier — et un draft relancé mais non vérifié n'est jamais
                    # montré (AD-3). Démarrer le premier en sachant que le second ne passera pas,
                    # c'est payer un appel `reason` pour rien (NFR4).
                    raise BudgetExceeded(
                        f"plafond d'appels trop bas pour la relance et sa vérification "
                        f"({budget.attempts}/{budget.max_attempts}, {APPELS_DE_LA_RELANCE} requis)")
                draft_2, step_rediger_2 = await rediger(parsed, retrieval, historique, client=client, budget=budget,
                                                        index=index, doc_id=doc_id, settings=settings,
                                                        motif=verification.motif,
                                                        max_tokens=rediger_max_tokens)
                steps.append(step_rediger_2)
                appels_avant = budget.attempts  # la relance a abouti : seule la suite peut encore rater
                relances += 1
                if draft_2.digest() == draft.digest():
                    # AD-3 : « chaque relance change quelque chose ». Rien n'a changé : re-vérifier rendrait
                    # exactement le même résultat pour le prix d'un appel `micro` de plus.
                    step_rediger_2.checks.append(CheckResult(
                        name="relance_sans_effet", ok=False,
                        detail="l'ébauche relancée est identique (même hash canonique) : arrêt sur la première vérification"))
                else:
                    seconde, step_verifier_2 = await verifier(draft_2, parsed=parsed, retrieval=retrieval,
                                                              corpus=corpus, index=index, client=client,
                                                              budget=budget, settings=settings)
                    steps.append(step_verifier_2)
                    if domine(seconde, acquise):
                        verification = seconde
                    else:
                        # AD-3 relance pour **améliorer** : rien ne garantit que la seconde ébauche fasse
                        # mieux. Elle ne domine pas — la prendre jetterait des affirmations déjà
                        # vérifiées, dégraderait `complete` ou allongerait `unknown`, et pourrait
                        # transformer une réponse en refus `claims_rejetes` (revue 1.5). L'acquis fait
                        # foi, et la trace dit que la relance n'a pas payé.
                        step_verifier_2.checks.append(CheckResult(
                            name="relance_moins_bonne", ok=False,
                            detail=f"la relance ne domine pas la première vérification "
                                   f"({len(seconde.claims)} affirmation(s) contre {len(acquise.claims)}, "
                                   f"{len(seconde.facettes_couvertes)} facette(s) couverte(s) contre "
                                   f"{len(acquise.facettes_couvertes)}, "
                                   f"{len(blocs_cites(seconde))} bloc(s) cité(s) contre "
                                   f"{len(blocs_cites(acquise))}, "
                                   f"complete={seconde.complete} contre {acquise.complete}, "
                                   f"manques={seconde.nb_manques} contre {acquise.nb_manques}) : "
                                   f"la première fait foi"))
            except (BudgetExceeded, Timeout, LlmParse, LlmUnavailable) as exc:
                # Deux situations qu'AD-16 sépare, et qu'il ne faut surtout pas confondre (revue Codex
                # 1.5, B5) :
                #
                # - **aucun appel n'a démarré** (plafond de coût ou d'appels atteint, marge de deadline
                #   insuffisante) : rien n'a été facturé, la relance est une *tentative d'amélioration*
                #   qui n'a pas eu lieu, et la réponse déjà vérifiée reste due. C'est le texte d'AD-1
                #   (« aucun retry ne démarre sans marge »), étendu aux euros par AD-4 ;
                # - **un appel a démarré et a échoué** (parse invalide après le retry du client, erreur
                #   fournisseur, timeout d'appel) : AD-16 est explicite — « un appel LLM en timeout, un
                #   parse invalide après 1 retry, un 429/529 fournisseur ⇒ 503 avec trace partielle ».
                #   L'avaler rendrait 200 sur une panne du fournisseur. L'erreur remonte donc, avec le
                #   `StepTrace` de l'appel raté et la trace partielle attachés.
                #
                # La question « un appel a-t-il démarré ? » se tranche sur le **budget**, pas sur ce
                # que l'étape a bien voulu attacher à son erreur (revue Codex 1.5, tour 2, B5) : le
                # compteur est incrémenté à l'envoi, par le client, pour toutes les étapes. Une étape
                # qui oublierait de renseigner `exc.step` — c'était le cas de *vérifier*, si bien
                # qu'une panne fournisseur pendant la **seconde vérification** ressortait en 200 —
                # ne peut plus faire passer un échec commencé pour une relance jamais partie. Le
                # `StepTrace` reste attaché quand l'étape l'a fourni : il porte le coût de l'appel raté.
                commence = budget.attempts > appels_avant or (exc.step is not None and bool(exc.step.calls))
                if commence:
                    if exc.step is not None:
                        steps.append(exc.step)
                    exc.trace = tracer()
                    raise
                # AD-4 : `complete=True` exige « aucune troncature de budget ». Une relance que le
                # plafond ou la deadline ont empêchée en est une : la réponse est servie, mais elle n'est
                # pas donnée pour complète. Un draft relancé mais **non vérifié** n'est jamais montré
                # (AD-3) : on repart de la vérification acquise.
                verification = relance_abandonnee(acquise)
                step_verifier.checks.append(CheckResult(
                    name="relance_abandonnee", ok=False,
                    detail=f"relance de rédiger non démarrée ({exc.code.value}) : "
                           f"la première vérification fait foi — {exc.message}"))
                if not verification.found:
                    # Rien de vérifié à servir : le refus motivé d'AD-3 est la seule sortie honnête, et
                    # c'est un `Answer` complet (200), pas une erreur.
                    step_verifier.checks[-1].detail += " ; aucune affirmation n'avait survécu"

        # --- restituer ------------------------------------------------------
        echeance("restituer")
        if not verification.found:
            if retrieval.truncated:
                # **NFR2, AD-1 et l'AC 2.3 mot pour mot** : « budget de retrieval épuisé ou
                # troncature non résolue ⇒ `complete=False` et **jamais** d'`AbsenceProof` » (revue
                # Codex 2.3, B3). Le garde-fou du retrieval vide couvrait déjà le cas où le budget
                # n'avait rien laissé passer ; celui-ci couvre le cas symétrique et jusque-là ouvert
                # — des blocs sont bien partis au modèle, mais la lecture était **bornée** et aucune
                # affirmation n'a survécu à la vérification. Publier un `AbsenceProof` reviendrait
                # alors à opposer à l'utilisateur ce que nous n'avons pas lu : la preuve annonce des
                # termes cherchés et un compte de blocs parcourus, c'est-à-dire l'exhaustivité que la
                # troncature dément. Il n'y a pas d'`Answer` honnête à rendre — c'est une erreur
                # terminale, avec son code (AD-16), et le front a son mode dégradé (UX-DR4).
                raise BudgetExceeded(
                    "aucune affirmation n'a survécu à la vérification, et la lecture du corpus avait "
                    f"été tronquée ({settings.max_opens} nœuds, {settings.retrieval_max_blocks} blocs, "
                    f"{settings.retrieval_max_tokens} tokens) : aucune absence du corpus n'est affirmée")
            # AD-3 : zéro claim survivante après la relance ⇒ refus motivé, jamais un dégradé silencieux.
            answer, step_restituer = restituer(
                language=parsed.language, lang_fallback=parsed.lang_fallback,
                verification=verification,
                reason=_absence("claims_rejetes", parsed, doc_id=doc_id, corpus=corpus,
                                dictionnaire=dictionnaire))
        else:
            answer, step_restituer = restituer(
                language=parsed.language, lang_fallback=parsed.lang_fallback,
                verification=verification)
        steps.append(step_restituer)
        return answer, tracer()

    try:
        return await chaine()
    except PipelineError as exc:
        # AD-16 : « 503 avec trace partielle ». Ce qui a déjà tourné — étapes, appels, coût engagé —
        # voyage avec l'erreur, sinon l'API de 1.6 rendrait une panne sans rien pour la situer.
        if exc.trace is None:
            exc.trace = tracer()
        raise
