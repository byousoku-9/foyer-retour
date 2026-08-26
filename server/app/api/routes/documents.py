"""AD-11 / AD-8 — `GET /api/v1/documents` et `GET /api/v1/documents/{doc_id}/report` (FR14).

Deux routes qui ne coûtent **rien** : tout ce qu'elles publient a été chargé au démarrage
(`api/etat.py`, AD-7 — « le serveur lit »), aucune ne touche `data/` ni n'appelle un modèle. Elles
ne sont donc pas limitées, exactement comme `/sante` : AD-13 protège le budget des routes qui
appellent un modèle, pas celles qui disent ce qui est servi.

**Pourquoi un `doc_id` inconnu est un 400 et non un 404.** L'`Enum` d'AD-16 n'a pas de code pour
« absent » ; lui en inventer un est précisément ce que cet AD interdit, et rendre un 404 nu sortirait
de l'enveloppe unique. `invalid_request` dit ce qui s'est passé sans mentir : l'appelant a nommé un
document que ce service ne sert pas, et c'est l'appel qui est à corriger. La liste des documents
servis est publiée par la route d'à côté et par `/api/v1/sante` — de quoi corriger l'appel sans
deviner.

Le message ne recopie **jamais** le `doc_id` reçu (AD-15, comme `gestionnaire_validation`) : c'est
une chaîne de l'appelant, et l'enveloppe d'erreur n'est pas un endroit où la lui réfléchir.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Path, Request, Response

from server.app.api.etat import url_publiable
from server.app.api.schemas import DOC_ID_MAX, DOC_ID_PATTERN, DocumentItem
from server.app.domain.errors import InvalidRequest
from server.app.domain.ingest import Report

router = APIRouter()


def _raison_publiable(raison: str | None) -> str | None:
    """Garde un diagnostic borné sans publier un emplacement issu des artefacts.

    Les raisons normales du loader restent inchangées. Dès qu'un URI ou un chemin apparaît, seule
    la partie précédente — le diagnostic utile — demeure, suivie d'un marqueur neutre. Les chemins
    avec espaces sont volontairement masqués jusqu'à un séparateur de diagnostic sûr.
    """
    if raison is None:
        return None
    propre = re.sub(
        r"(?i)\b[a-z][a-z0-9+.-]*:(?://)?[^\n,;)]*",
        "[emplacement masqué]",
        raison,
    )
    propre = re.sub(
        r"(?<![\w.])(?:\.\.?/|/|\\\\)[^\n,;)]*",
        "[emplacement masqué]",
        propre,
    )
    return propre if len(propre) <= 500 else propre[:499] + "…"


@router.get("/documents", response_model=list[DocumentItem])
async def documents(request: Request) -> list[DocumentItem]:
    """Les documents servis et quarantinés, triés par ``doc_id``.

    Cette liste est une projection d'audit, pas le working set : ``selectionnable`` n'est vrai que
    pour un contrat effectivement présent dans ``Corpus.documents``. Une quarantaine reste donc
    inspectable sans pouvoir atteindre ``POST /sinistre``.
    """
    etat = request.app.state.foyer
    items = []
    connus = sorted(set(etat.corpus.served) | set(etat.corpus.quarantine))
    for doc_id in connus:
        # Une clé de manifest hors convention ne peut pas devenir un segment d'URL public. Le
        # loader la garde dans ses alertes, mais la surface HTTP ne publie ni chemin ni identifiant
        # hostile sous prétexte de rendre les quarantaines visibles.
        if len(doc_id) > DOC_ID_MAX or re.fullmatch(DOC_ID_PATTERN, doc_id) is None:
            continue
        document = etat.corpus.documents.get(doc_id)
        entree = etat.corpus.manifest.get(doc_id)
        servi = document is not None
        items.append(DocumentItem(
            doc_id=doc_id,
            title=document.title if document is not None else doc_id,
            kind=document.kind if document is not None else None,
            # L'édition affichée est celle du **document chargé**, pas celle du manifest : c'est
            # elle que `ClaimStatus.edition` recopie sous chaque citation (AD-4), et deux libellés
            # différents dans le même écran seraient une incohérence que rien ne signalerait. Pour
            # une quarantaine, seul le manifest validé peut encore fournir ce fait.
            #
            # Le statut déclaré du manifest ne décide jamais de cette ligne : le statut effectif
            # vient exclusivement du résultat du loader.
            edition=document.edition if document is not None else (entree.edition if entree else None),
            status="servi" if servi else "quarantaine",
            selectionnable=bool(servi and document.kind == "contrat"),
            raison=None if servi else _raison_publiable(etat.corpus.quarantine.get(doc_id)),
            # `Document.source_url` d'abord (l'ingestion l'a validé), puis `data/{doc_id}/source.url`
            # qu'AD-7 rend canonique : l'ingestion PDF laisse le champ vide parce que le PDF n'est
            # pas committé, et le contrat n'aurait alors aucune source affichable. Les **deux**
            # passent par `url_publiable` (revue 1.9) : filtrer le fichier et pas le champ laissait
            # une ingestion future publier un `gs://` du bucket privé dans une réponse publique.
            source_url=(url_publiable(document.source_url) if document is not None else None)
            or etat.source_urls.get(doc_id),
            source_hash=entree.source_hash if entree is not None else None,
            ingest_fingerprint=entree.ingest_fingerprint if entree is not None else None,
            document_hash=entree.document_hash if entree is not None else None,
            overlay_hash=entree.overlay_hash if entree is not None else None,
            gate=entree.gate if entree is not None else None,
            report_status=("disponible" if doc_id in etat.reports
                           else etat.report_errors.get(doc_id, "absent"))))
    return items


# Le paramètre de chemin est borné comme le `doc_id` du corps de `POST /api/v1/sinistre` (revue
# 1.9) : deux portes vers la même clé de dictionnaire, une seule forme admise. Sans cela, la route
# la plus exposée des deux — un `GET` sans corps, donc sans `request_max_bytes` pour la protéger —
# était la seule à ne rien borner du tout.
DocId = Annotated[str, Path(min_length=1, max_length=DOC_ID_MAX, pattern=DOC_ID_PATTERN)]


@router.get("/documents/{doc_id}/report", response_model=Report)
async def report(request: Request, doc_id: DocId) -> Report:
    """Le `report.json` d'AD-8, « exposé tel quel », lu au démarrage et jamais relu (AD-7)."""
    etat = request.app.state.foyer
    rapport = etat.reports.get(doc_id)
    if rapport is None:
        etat_rapport = etat.report_errors.get(doc_id)
        if etat_rapport == "absent":
            detail = "le rapport d'ingestion est absent"
        elif etat_rapport == "illisible":
            detail = "le rapport d'ingestion est illisible"
        elif etat_rapport == "etranger":
            detail = "le rapport d'ingestion décrit un autre document"
        else:
            detail = "aucun rapport d'ingestion n'est publié pour ce document"
        raise InvalidRequest(detail)
    return rapport


@router.get("/documents/{doc_id}/pages/{page}.png", response_class=Response)
async def document_page(request: Request, doc_id: DocId,
                        page: Annotated[int, Path(ge=1)]) -> Response:
    """PNG paresseux ; `blocks=a,b` est l'identité canonique de ce qui peut être surligné."""
    allowed = {"blocks", "line_ids"}
    if any(key not in allowed for key in request.query_params):
        raise InvalidRequest("paramètre inconnu sur la demande de page")
    raw_blocks = request.query_params.getlist("blocks")
    if len(raw_blocks) > 1:
        raise InvalidRequest("le paramètre blocks doit être une liste canonique unique")
    block_ids: list[str] = []
    if raw_blocks:
        if not raw_blocks[0]:
            raise InvalidRequest("le paramètre blocks ne peut pas être vide")
        block_ids = raw_blocks[0].split(",")
        if any(not block_id for block_id in block_ids):
            raise InvalidRequest("la liste de blocs est invalide")

    line_ids: list[str] | None = None
    if "line_ids" in request.query_params:
        raw_lines = request.query_params.getlist("line_ids")
        # `line_ids=` exprime explicitement « citation sans lignes ». L'absence du paramètre, elle,
        # demande toutes les lignes des blocs canoniques.
        if raw_lines == [""]:
            line_ids = []
        elif any(not line_id for line_id in raw_lines):
            raise InvalidRequest("la liste de lignes est invalide")
        else:
            line_ids = raw_lines

    etat = request.app.state.foyer
    document = etat.corpus.documents.get(doc_id)
    renderer = etat.page_renderer
    if document is None or renderer is None:
        raise InvalidRequest("document non servi")
    resolved = renderer.resolve(document, page, block_ids, line_ids)
    rendered = await renderer.render(resolved, etat.pdf_sources.get(doc_id))
    return Response(
        content=rendered.png,
        media_type="image/png",
        headers={
            # Le cache LRU de process est la seule politique réglable. Aucun second seuil de durée
            # ne vit en dur dans la couche HTTP ; le navigateur garde déjà le blob tant que le
            # dialogue l'affiche, puis le script révoque son URL locale.
            "Cache-Control": "private, no-store",
            "X-Document-Page": str(page),
            "X-Document-Pages": str(rendered.page_count),
            "X-Highlighted-Lines": str(rendered.highlighted_lines),
        })
