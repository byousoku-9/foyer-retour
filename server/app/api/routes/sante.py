"""AD-11 / FR12 — `GET /api/v1/sante` (et l'alias historique `/sante`) : l'état du service, sans fard.

`testerApi()` du site sonde cette route **à chaque chargement de page** et n'y lit que `ok` : elle
n'est donc jamais limitée (AD-13 protège les routes qui appellent un modèle, pas celle qui dit si
le serveur est là) et ne coûte rien — tout ce qu'elle publie a été calculé au démarrage.

Ce qu'elle dit de plus que « ok » est ce qui rend le système relisible : quels documents sont
réellement servis, à quel niveau de validation (`gate_profile`, `null` tant qu'aucun gate n'est
écrit — AD-11 interdit d'annoncer un profil qu'aucun document ne porte), quelles alertes pèsent sur
eux (`sans_gate`, `gate_perime`, `source_absente`, quarantaine), si le dictionnaire est validé
(AD-5 : sinon le court-circuit « zéro hit » dort), et les seuils actifs — les mêmes que ceux de
`Trace.thresholds`, pour qu'une réponse et l'état du serveur se lisent avec la même règle.
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
        dictionary=EtatDictionnaire(validated=etat.dictionary_validated),
        alerts=etat.alerts,
        thresholds=etat.settings.thresholds())
