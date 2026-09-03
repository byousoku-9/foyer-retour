"""AD-3 / AD-9 — *rédiger* : un appel `reason`, sortie structurée `AnswerDraft` du domaine — ses
invariants (min 1 quote, une quote par bloc, cohérence segments/claims) se valident au parse et
déclenchent le retry motivé du client.

Préfixe système byte-identique = `commun.md` + `rediger.md` + sommaire versionné du document (avec
son en-tête de hash) — FR13 : le sommaire vit dans le préfixe cacheable (breakpoint 1 h, relu à 0,1×
après la première écriture). Historique, blocs (avec leur `block_id`) et question résolue viennent
**après**, chacun délimité par `untrusted()` (AD-15) — le `motif` de relance de la story 1.5 aussi,
quand il est présent : il est composé à partir de sorties de modèle et de texte de blocs.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable

from server.app.config import Settings
from server.app.corpus.ebauche import (fusionner_quotes_du_meme_bloc,
                                       rattacher_claims_sinistre)
from server.app.corpus.index import Index
from server.app.domain.answer import AnswerDraft
from server.app.domain.document import Block
from server.app.domain.langue import LANGUES_SERVIES
from server.app.domain.errors import PipelineError
from server.app.domain.question import ParsedQuestion, Turn
from server.app.domain.retrieval import RetrievalResult
from server.app.domain.verdict import KINDS_FONDATEURS
from server.app.domain.trace import CheckResult, StepTrace
from server.app.llm.budget import RequestBudget
from server.app.llm.client import LlmClient
from server.app.llm.models import EFFORT_PAR_PROMPT, MODEL_CAPS, model_for
from server.app.llm.prompting import load_prompt, render_prompt, untrusted


def _clause_autonome(bloc: Block) -> bool:
    """Distingue une clause lisible seule d'un item qui prolonge une liste.

    Revue Codex 4.2a (I2) : la **structure d'ingestion** prime la casse. Un bloc que l'ingestion a
    classé `list` prolonge son amorce quelle que soit sa première lettre — un item capitalisé ou
    ouvert par un acronyme ne perd plus son contexte. Pour les autres structures, la casse reste le
    seul signal disponible : l'ingestion étiquette `para` des items numérotés (« 3.1.10.3.3 aux
    biens par… »), si bien qu'un `structural_kind` non-`list` ne prouve pas l'autonomie. Les
    numéros et puces ne portent pas la syntaxe : après eux, une phrase autonome commence par une
    capitale ; une première lettre minuscule — item numéroté comme phrase mal océrisée — reçoit le
    contexte parent, comportement conservateur : la rubrique parente est un contexte de
    formulation, jamais une preuve (revue B4), et un contexte de trop est un bruit, pas une
    erreur. Ce critère reste indépendant du corpus, du vocabulaire métier et des témoins live.
    """
    if bloc.structural_kind == "list":
        return False
    premiere_lettre = next((caractere for caractere in bloc.text if caractere.isalpha()), None)
    return premiere_lettre is not None and premiere_lettre.isupper()


def _rubrique_parente(index: Index, block_id: str) -> str | None:
    """Titre de la rubrique parente d'un bloc, si la structure en fournit une."""
    document = index.corpus.documents[index.doc_of(block_id)]
    node_id = document.node_of(block_id)
    noeuds = {noeud.node_id: noeud for noeud in document.nodes}
    parent_id = next((noeud.node_id for noeud in document.nodes
                      if node_id in noeud.children), None)
    if parent_id is None:
        return None
    titre = noeuds[parent_id].title.strip()
    return titre or None


def _rattacher_claims_sinistre(draft: AnswerDraft, settings: Settings) -> tuple[AnswerDraft, int]:
    """`corpus.ebauche.rattacher_claims_sinistre` avec les deux bornes de la configuration."""
    return rattacher_claims_sinistre(draft, max_claims=settings.draft_max_claims,
                                     max_segments=settings.draft_max_segments)


async def rediger(parsed: ParsedQuestion, retrieval: RetrievalResult, historique: list[Turn], *,
                  client: LlmClient, budget: RequestBudget, index: Index, doc_id: str,
                  settings: Settings, motif: str | None = None,
                  blocs_a_conserver: Iterable[str] = (),
                  blocs_hors_objet: Iterable[str] = (),
                  prompt: str = "rediger"
                  ) -> tuple[AnswerDraft, StepTrace]:
    """`prompt` nomme le fichier de `llm/prompts/` inséré entre `commun.md` et le sommaire.

    Story 1.8 : le sinistre passe `prompt="rediger_sinistre"` — mêmes contrats d'entrée et de sortie,
    consigne « une seule clause par affirmation » en plus (AD-6). Le défaut **est** le guide : son
    préfixe reste byte-identique, donc cacheable (AD-9) et rejouable depuis ses fixtures live.
    `settings.rediger_max_tokens` est l'unique plafond de rédaction des deux pipelines et de toutes
    leurs variantes : la navigation choisie ne change ni le modèle, ni le schéma, ni cette borne.
    """
    t0 = time.monotonic()
    # Story 4.2b : tier épinglable par la matrice baseline ; `STEP_TIERS` reste le défaut AD-9.
    tier = settings.rediger_tier
    step = StepTrace(name="rediger", tier=tier,
                     opened_block_ids=[b.block_id for b in retrieval.blocs])
    etrangers = [b.block_id for b in retrieval.blocs if index.doc_of(b.block_id) != doc_id]
    if etrangers:
        # AD-1/AD-9 (revue Codex 1.4, I3) : le sommaire du préfixe situe les blocs dans *leur* document.
        # Un `doc_id` qui ne recouvre pas les blocs reçus enverrait au modèle le mauvais plan de lecture
        # sans aucune erreur — AD-16 : jamais de dégradé silencieux.
        raise ValueError(f"blocs hors du document {doc_id!r} : {etrangers}")
    # `quote_max_chars` **n'est plus annoncé au modèle** (correctif du tour 2, rapport rédiger §4) :
    # rien ne l'appliquait. Les citations qui le dépassent sont exactes, seulement bavardes, et sont
    # servies telles quelles — la règle du dépôt est de borner, jamais de tronquer, et rejeter une
    # citation exacte perdrait une preuve valide. Annoncer une borne que rien n'applique est ce
    # qu'AD-16 refuse ailleurs sous le nom de dégradé silencieux ; et la fusion des extraits d'un
    # même bloc rend les citations plus longues, pas plus courtes. Le seuil reste publié par
    # `thresholds()` et observé par le check `quote_trop_longue` de *vérifier*.
    prefix = load_prompt("commun") + "\n\n" + render_prompt(
        prompt, quote_min_chars=settings.quote_min_chars,
        draft_max_segments=settings.draft_max_segments, draft_max_claims=settings.draft_max_claims,
    ) + "\n\n" + index.sommaire(doc_id)
    parts = [untrusted("historique", json.dumps([{"role": t.role, "texte": t.texte} for t in historique],
                                                ensure_ascii=False))]
    parts += [untrusted("document", f"{b.block_id}\n{b.text}") for b in retrieval.blocs]
    parts.append(untrusted("question", parsed.question_resolue))
    if prompt == "rediger_sinistre":
        # Correctif du tour 2 : le rédacteur était **noté sur un barème qu'on ne lui montrait pas.**
        # *vérifier* mesure la couverture des facettes une par une (`verifier.py`, même forme
        # `untrusted("facette", …)`), et *rédiger* n'en recevait que le **nombre** — il devait
        # redécouper la question lui-même, sous une consigne de concision. Le découpage vient de
        # *comprendre*, il est arrêté avant tout retrieval (AD-4), et il est déjà borné en nombre
        # (`question_max_facettes`) et en longueur (`libelle_max_chars`) : le transmettre ne coûte
        # que quelques dizaines de tokens et referme l'écart entre ce qui est demandé et ce qui est
        # mesuré. C'est du texte du modèle : il est délimité comme le reste (AD-15).
        #
        # **Correctif du tour 7b (H1) : la sous-question porte son libellé, et rien de plus.** Le
        # tour 7 y avait joint les blocs décisionnels que le classement de cette sous-question avait
        # proposés, au motif qu'AD-1 fait mesurer le code. Mais cette attribution-là n'est pas une
        # mesure du **sens** : elle est **lexicale**, et elle porte les collisions de l'index. Mesuré
        # sur trois runs — la sous-question du bris d'une vitre s'y voyait attribuer un bloc de
        # dégâts des eaux, parce que la normalisation confond le pluriel d'un nom avec un participe
        # (`collision-vitres-vitres-sous-normalize`). Le rédacteur a obéi : il a écrit une claim sur
        # ce bloc — rejetée — et laissé de côté les deux clauses justes qu'il avait sous les yeux.
        #
        # Dire au modèle « voici les blocs de cette sous-question » revient à lui transmettre une
        # attribution que le code **ne sait pas faire**, avec l'autorité d'une mesure. Le code sait
        # quels blocs il a transmis et quelles sous-questions ont été posées ; l'appariement des uns
        # aux autres appartient à la lecture du texte, donc au modèle. On ne le lui vole pas.
        parts += [untrusted("facette", json.dumps({"facette": rang, "libelle": libelle},
                                                  ensure_ascii=False))
                  for rang, libelle in enumerate(parsed.facettes)]
    tail = (f"Langue de rédaction : {LANGUES_SERVIES[parsed.language]} ({parsed.language}). "
            "Les citations restent recopiées mot pour mot dans la langue du bloc source.")
    if prompt == "rediger_sinistre":
        # A11 : avec deux facettes déjà arrêtées par *comprendre*, le modèle pouvait développer
        # quatre claims et plusieurs transitions jusqu'à `max_tokens`, puis recommencer. Le nombre
        # ci-dessous vient du contrat `ParsedQuestion` de la requête — ce n'est ni un nouveau seuil,
        # ni une déduction depuis le texte. La consigne vit dans le message dynamique pour garder le
        # préfixe cacheable byte-identique.
        #
        # **Correctif du tour 7b (H2) : ce plan contredisait le préfixe, et c'est lui que le modèle
        # a suivi.** Il demandait « seulement les claims directement nécessaires » et « n'énumère pas
        # les autres items d'une liste contractuelle » — écrit contre la paraphrase d'une liste, mais
        # lu, à juste titre, comme une consigne d'omission. Mesuré sur trois runs : la clause qui
        # répond au cas est le sixième item d'une énumération de périls dont le cinquième était déjà
        # cité ; elle sautait **par construction**, et la règle du préfixe — une claim par clause
        # décisionnelle qui vise les faits — n'avait aucune chance contre une consigne placée plus
        # près de la sortie.
        #
        # Le plan porte donc désormais cette règle, et la concision y retrouve son sens : une phrase
        # courte et la quote la plus courte qui la soutient, **jamais moins de clauses**.
        tail += (f"\nPlan de sortie : {len(parsed.facettes)} sous-question(s) ont déjà été "
                 "extraites, et elles te sont données ci-dessus, numérotées. Traite **chacune**, "
                 "dès les premiers segments. Pour chaque sous-question, rends une claim par clause "
                 "décisionnelle fournie dont le texte vise les faits énoncés dans la question — deux "
                 "clauses qui visent le même dommage par des chemins différents font deux claims. "
                 "Un item d'énumération dont le texte vise ces faits **est** une telle clause : "
                 "traite-le comme les autres, et ne le tiens pas pour un doublon de l'item voisin. "
                 "La concision porte sur la **forme**, jamais sur le nombre de clauses : une phrase "
                 "courte, la plus courte quote contiguë qui la soutient, et ni transition, ni "
                 "reformulation de contexte, ni segment limite si les claims factuelles suffisent.")
        fondatrices_confirmees: list[str] = []
        for bloc in retrieval.blocs:
            if bloc.kind not in KINDS_FONDATEURS or not bloc.kind_confirmed:
                continue
            description = f"{bloc.block_id}={bloc.kind}"
            if not _clause_autonome(bloc):
                rubrique = _rubrique_parente(index, bloc.block_id)
                if rubrique:
                    description += f" (rubrique parente : {rubrique})"
            fondatrices_confirmees.append(description)
        if fondatrices_confirmees:
            # Le kind confirmé guide l'opérateur mais n'est jamais une citation. Pour un item qui
            # dépend d'une liste, le titre parent (déjà dans le sommaire) situe le contexte de
            # formulation — jamais une preuve (revue Codex 4.2a, B4) : l'opérateur revendiqué doit
            # être porté par un passage cité, sinon *vérifier* rejette `non_soutenue`.
            tail += ("\nOpérateurs contractuels confirmés : " + "; ".join(fondatrices_confirmees) +
                     ". Ce typage guide la formulation mais ne constitue pas à lui seul une preuve "
                     "citable. Respecte l'opérateur de chaque identifiant. Si un item est "
                     "grammaticalement incomplet, appuie le sujet et l'opérateur que tu revendiques "
                     "sur un passage cité qui les porte réellement — l'amorce de la liste, citée en "
                     "plus de l'item, quand elle est citable — ou décris seulement ce que l'item "
                     "énumère sans conclure ; la rubrique parente situe le contexte mais n'est "
                     "jamais une preuve ; n'invente pas `couvre` ou `exclut` sur la seule "
                     "étiquette. Ne transforme jamais une exclusion en garantie, ni l'inverse.")
        reserve_facettes = min(len(parsed.facettes), settings.draft_max_claims)
        places_dependances = settings.draft_max_claims - reserve_facettes
        dependances_directes = set(retrieval.decision_dependency_block_ids)
        # Correctif du tour 2 (rapport rédiger E). **Le message ne peut pas ordonner d'émettre la
        # claim que le motif ordonne de remplacer.** L'audit du cas bougie montre les deux
        # consignes côte à côte dans la même requête : le motif rejette `p46:1` comme hors de
        # l'objet, et quatre lignes plus haut « Limites à rendre vérifiables : … p46:1 » demande de
        # la rendre. Le modèle la ré-émet à l'octet près, le contrôle la re-rejette, et le second
        # cycle est stérile par construction sur toute cette classe.
        #
        # Le filtre ne porte que sur `hors_objet` — un jugement de périmètre que la relance ne
        # déplace pas. Une limite rejetée `non_soutenue` reste demandée : là, la relance est utile,
        # et c'est justement la story 3.3 qui veut que le code, pas le modèle, décide de
        # l'applicabilité.
        hors_objet = set(blocs_hors_objet)
        limites_portees = [b.block_id for b in retrieval.blocs
                            if b.kind in {"exclusion", "condition", "franchise"}
                            and b.scope_node_ids and b.block_id in dependances_directes
                            and b.block_id not in hors_objet]
        limites_portees = limites_portees[:places_dependances]
        if limites_portees:
            # Story 3.3 : le code aval est seul autorisé à décider qu'une portée explicite ne couvre
            # pas le cas. Si la rédaction omet la clause, cette décision pure ne peut jamais devenir
            # visible. Les IDs viennent du corpus typé, pas de la question, et la consigne s'applique
            # uniformément à toute limite explicitement bornée retrouvée.
            tail += ("\nLimites à rendre vérifiables : " + ", ".join(limites_portees) +
                     ". Pour chacun de ces blocs à portée explicite, rends une claim courte avec une "
                     "citation contiguë, même si sa portée semble différente du cas : ne décide pas "
                     "toi-même de son applicabilité, le code la calculera et affichera la raison.")
        places_restantes = places_dependances - len(limites_portees)
        # Une définition éclaire la clause ; elle ne doit ni s'y substituer ni multiplier les
        # verdicts structurés de *vérifier* : les clauses décisionnelles et limites restent toutes
        # prioritaires, et le choix demeure celui déjà résolu par `definitions()` dans l'ordre du
        # corpus. La borne vit dans la configuration (`draft_max_definitions`, publiée dans
        # `thresholds()`) et ne change aucun budget de retrieval ou de modèle.
        definitions = [b.block_id for b in retrieval.blocs
                       if b.kind == "definition" and b.defines
                       and b.block_id in dependances_directes
                       ][:min(places_restantes, settings.draft_max_definitions)]
        if definitions:
            # `definitions()` a déjà résolu la proximité de portée et les overrides. Une définition
            # ainsi sélectionnée mais omise par la rédaction rendrait cette résolution invisible ;
            # le modèle la transcrit, sans refaire le choix sémantique acquis par le code.
            tail += ("\nDéfinitions applicables à rendre vérifiables : " + ", ".join(definitions) +
                     ". Pour ces blocs déjà résolus par portée, rends au plus une claim courte "
                     "avec une citation contiguë ; n'en substitue pas une autre et n'en déduis pas "
                     "une conclusion que son texte ne porte pas.")
        disponibles = {b.block_id for b in retrieval.blocs}
        a_conserver = [block_id for block_id in dict.fromkeys(blocs_a_conserver)
                       if block_id in disponibles]
        if a_conserver:
            # Ces identifiants sont relus parmi les blocs du retrieval : la consigne est de confiance
            # et ne peut pas être alimentée par un identifiant inventé dans le motif du modèle.
            tail += ("\nAcquis à reconduire pendant la relance : " + ", ".join(a_conserver) +
                     ". Conserve au moins une claim vérifiable pour chacun de ces blocs, avec ses "
                     "facettes déjà traitées, en plus de corriger le motif ; ne remplace pas une "
                     "preuve acquise par la nouvelle clause.")
    if motif is not None:
        # AD-15 : le motif vient de *vérifier* (1.5), qui le compose à partir de la sortie du modèle et
        # du texte des blocs — il est délimité comme tout le reste, jamais concaténé en clair.
        tail += "\n" + untrusted("motif", motif)
    content = "\n\n".join(parts) + "\n\n" + tail
    trusted_line_uids = tuple(dict.fromkeys(
        line.line_uid
        for block in retrieval.blocs
        for line in block.lines
        if line.line_uid is not None
    ))
    try:
        result = await client.parse(tier=tier, system_prefix=prefix,
                                    messages=[{"role": "user", "content": content}], output_model=AnswerDraft,
                                    budget=budget, step=step,
                                    max_tokens=settings.rediger_max_tokens,
                                    # La rédaction sinistre transcrit des clauses déjà retrouvées ;
                                    # son raisonnement de couverture appartient à *vérifier*. Avec
                                    # `medium`, le raisonnement invisible pouvait consommer les 2 048
                                    # tokens malgré un JSON court et forcer un retry. `low` conserve le
                                    # même modèle, le même schéma et les mêmes bornes, et ne touche pas
                                    # la variante guide 2.6.
                                    # Story 4.2b : un tier épinglé sur un modèle sans `effort`
                                    # (Haiku) ne reçoit aucune dérogation — le client refuserait.
                                    effort=(EFFORT_PAR_PROMPT.get(prompt)
                                            if MODEL_CAPS[model_for(tier)]["effort"] else None),
                                    trusted_line_uids=trusted_line_uids)
    except PipelineError as exc:
        # AD-10/AD-16 : l'appel raté a pu être facturé (`step.calls` le porte, `budget` aussi). Sans
        # ce rattachement, l'étape disparaît de la trace alors que son coût y compte, et l'appelant ne
        # peut pas distinguer un appel **commencé** d'un appel qui n'a jamais démarré (revue Codex
        # 1.5, B5). L'erreur reste terminale : c'est l'appelant qui décide, pas nous.
        step.ms = int((time.monotonic() - t0) * 1000)
        exc.step = step
        raise
    draft = result.parsed
    draft, fusions = fusionner_quotes_du_meme_bloc(draft, index=index, doc_id=doc_id)
    if fusions:
        step.checks.append(CheckResult(
            name="quotes_fusionnees", ok=True,
            detail=f"{fusions} affirmation(s) citaient deux extraits d'un même bloc : fusionnés en "
                   "un passage contigu qui les couvre, au lieu d'un échec de schéma terminal"))
    if prompt == "rediger_sinistre":
        # Revue 4.2a (I1) : aucune réécriture de claim en code. L'ancienne « ancre » remplaçait
        # claim et quote par le texte intégral du bloc fondateur : le contrôle de soutien devenait
        # tautologique (claim byte-identique à sa quote) et les blocs au-delà de `quote_max_chars`
        # devenaient le texte affiché. Une conclusion appliquée au dossier est traitée là où AD-3
        # la place : *vérifier* la rejette avec la raison fermée `conclusion_ajoutee` et la relance
        # typée redemande la règle conditionnelle — le texte soumis au contrôle reste celui du
        # modèle, mot pour mot.
        draft, _changements = _rattacher_claims_sinistre(draft, settings)
        if len(draft.claims) < len(result.parsed.claims):
            step.checks.append(CheckResult(
                name="claims_hors_borne_ecartees", ok=False,
                detail=f"{len(result.parsed.claims) - len(draft.claims)} claim(s) au-delà de "
                       "draft_max_claims écartée(s) mécaniquement avant vérification : la borne "
                       "annoncée au prompt fait foi"))
    step.ms = int((time.monotonic() - t0) * 1000)
    return draft, step
