"""FR41 / AD-11 — `GET /api/v1/evals/latest` : le dernier run d'évals publié, ou son absence.

Ce que cette route **ne fait pas**, et c'est l'essentiel : elle ne lit pas `data/` par requête, elle
ne recalcule rien, elle n'appelle aucun modèle, et elle ne rend **jamais** 5xx quand rien n'est
publié. L'artefact est chargé et validé une fois au démarrage (`api/etat._publication_evals`), comme
`report.json` et `dictionary.json` (AD-7).

Un artefact absent, illisible ou hors schéma rend un **état typé** — `publie: false` avec sa raison —
et pas un corps vide, pas un 404, pas un objet à moitié rempli. La différence compte : la page
d'accueil doit pouvoir dire « aucun run n'est publié » sans le déduire d'une erreur réseau, qui
voudrait dire tout autre chose.

Comme `/sante`, cette route ne coûte rien et n'est donc pas limitée (AD-13 protège les routes qui
appellent un modèle). AD-11 la nomme sous `/api/v1` et ne lui donne aucun alias historique : rien
d'ancien ne l'attend à la racine.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from server.app.domain.evals import EtatPublication

router = APIRouter()


@router.get("/evals/latest", response_model=EtatPublication)
async def evals_latest(request: Request) -> EtatPublication:
    """Le dernier run publié, rouge compris — publier n'est pas promouvoir (AD-8, FR41)."""
    etat = request.app.state.foyer
    return etat.publication_evals
