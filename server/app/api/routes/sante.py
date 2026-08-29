"""AD-11 / FR12 — `GET /api/v1/sante` (et l'alias historique `/sante`) : l'état du service, sans fard.

`testerApi()` du site sonde cette route **à chaque chargement de page** et n'y lit que `ok` : elle
n'est donc jamais limitée (AD-13 protège les routes qui appellent un modèle, pas celle qui dit si
le serveur est là) et ne coûte rien — tout ce qu'elle publie a été calculé au démarrage.

Ce qu'elle dit de plus que « ok » est ce qui rend le système relisible : quels documents sont
réellement servis, à quel niveau de validation (`gate_profile` et `gate_cases`, `null` tant qu'un
document servi n'a pas de gate — AD-11 interdit d'annoncer un profil qu'aucun document ne porte),
quelles alertes pèsent sur eux (`sans_gate`, `gate_perime`, `source_absente`, `bloquant_statique`,
quarantaine, `ungated_refuse_en_production`, `dictionnaire_non_valide`, `dictionnaire_corpus_perime`),
où en est le dictionnaire des variantes — validé, décrivant bien le corpus servi, et **le refus
« zéro hit » d'AD-5 est-il armé** (la règle est calculée ici, jamais par un front) —, et les seuils
actifs, les mêmes que ceux de `Trace.thresholds`, pour qu'une réponse et l'état du serveur se lisent
avec la même règle.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from server.app.api.schemas import EtatDictionnaire, SanteResponse

router = APIRouter()


@router.get("/sante", response_model=SanteResponse)
async def sante(request: Request) -> SanteResponse:
    etat = request.app.state.foyer
    return SanteResponse(
        # `ok` répond à la seule question que le front pose : « puis-je poser ma question ? ». Le
        # guide en quarantaine, la réponse est non, même si le serveur tourne parfaitement.
        ok=etat.settings.guide_doc_id in etat.corpus.documents,
        version=etat.settings.git_sha,
        documents_servis=etat.documents_servis,
        gate_profile=etat.gate_profile,
        gate_cases=etat.gate_cases,
        gate_countersigned=etat.gate_countersigned,
        # AC 4.5 : les trois réserves sont lisibles ici — la contresignature humaine, la validation
        # par un expert (toujours fausse, AD-14), et `dictionary.validated` juste en dessous.
        gate_validated_by_expert=etat.gate_validated_by_expert,
        # AD-5 : les deux faits (une main a signé ; les empreintes décrivent le corpus servi) **et**
        # la règle qu'ils décident. `refus_zero_hit_actif` est publié par le serveur parce que la
        # règle n'a qu'une autorité : la page d'accueil l'affiche au lieu de refaire la conjonction.
        dictionary=EtatDictionnaire(validated=etat.dictionnaire.validated,
                                    corpus_ok=etat.dictionnaire.corpus_ok,
                                    refus_zero_hit_actif=etat.dictionnaire.court_circuit_actif),
        alerts=etat.alerts,
        thresholds=etat.settings.thresholds())
