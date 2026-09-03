"""AD-11 / FR12 — `GET /api/v1/sante` (et l'alias historique `/sante`) : l'état du service, sans fard.

`testerApi()` du site sonde cette route **à chaque chargement de page** et n'y lit que `ok` : elle
n'est donc jamais limitée (AD-13 protège les routes qui appellent un modèle, pas celle qui dit si
le serveur est là) et ne coûte rien — tout ce qu'elle publie a été calculé au démarrage.

Ce qu'elle dit de plus que « ok » est ce qui rend le système relisible : quels documents sont
réellement servis, à quel niveau de validation (`gate_profile` et `gate_cases`, `null` tant qu'un
document servi n'a pas de gate — AD-11 interdit d'annoncer un profil qu'aucun document ne porte),
quelles alertes pèsent sur eux (`sans_gate`, `gate_perime`, `source_absente`, `bloquant_statique`,
quarantaine, `ungated_refuse_en_production`, `dictionnaire_non_valide`, `dictionnaire_corpus_perime`),
si le **fournisseur** est configuré (`cle_fournisseur_absente` — sans clé, aucune question ne peut
aboutir, et `ok` le dit),
où en est le dictionnaire des variantes — validé, décrivant bien le corpus servi, et **le refus
« zéro hit » d'AD-5 est-il armé** (la règle est calculée ici, jamais par un front) —, et les seuils
actifs, les mêmes que ceux de `Trace.thresholds`, pour qu'une réponse et l'état du serveur se lisent
avec la même règle.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from server.app.api.schemas import (
    EtatCacheReponses,
    EtatCaches,
    EtatDictionnaire,
    EtatMaintienPrefixes,
    SanteResponse,
)


def _etat_des_caches(etat) -> EtatCaches:  # type: ignore[no-untyped-def]
    """Story 5.6 (T5) — les deux caches, publiés parce qu'ils décident de la facture.

    `compte_des_entrees()` compte des fichiers ; c'est la seule lecture de disque de cette route, et
    elle est bornée par `response_cache_max_entries`. Un cache non armé rend l'objet par défaut :
    tous les compteurs à zéro et `actif=false`.
    """
    cache = etat.cache_reponses
    reponses = EtatCacheReponses()
    if cache is not None:
        c = cache.compteurs
        reponses = EtatCacheReponses(
            actif=True, hits=c.hits, misses=c.misses, ecritures=c.ecritures,
            evictions=c.evictions, invalides=c.invalides,
            entrees=cache.compte_des_entrees())
    maintien = etat.maintien_prefixes
    prefixes = EtatMaintienPrefixes()
    if maintien is not None:
        e = maintien.etat
        prefixes = EtatMaintienPrefixes(
            actif=e.actif, prefixes=e.prefixes, maintiens=e.maintiens,
            ignores=e.ignores, echecs=e.echecs,
            cout_cumule_eur=round(e.cout_cumule_eur, 4),
            cout_du_jour_eur=round(e.cout_du_jour_eur, 4),
            plafond_du_jour_atteint=e.plafond_du_jour_atteint)
    return EtatCaches(reponses=reponses, prefixes=prefixes)


router = APIRouter()


@router.get("/sante", response_model=SanteResponse)
async def sante(request: Request) -> SanteResponse:
    etat = request.app.state.foyer
    return SanteResponse(
        # `ok` répond à la seule question que le front pose : « puis-je poser ma question ? ». Le
        # guide en quarantaine, la réponse est non, même si le serveur tourne parfaitement — et
        # **sans clé fournisseur, la réponse est non aussi**, même si tout le corpus est servi.
        # `ok` ne dépendait que du corpus : clé absente, `/sante` répondait `true` pendant que
        # chaque question rendait une erreur. Le front ne lit que `ok` ; la sonde mentait donc à la
        # seule question qu'elle sait poser, et rien d'autre n'aurait signalé la panne.
        # L'alerte `cle_fournisseur_absente` dit à côté **pourquoi** (`etat.alerts`).
        ok=(etat.settings.guide_doc_id in etat.corpus.documents
            and bool(etat.settings.anthropic_api_key.strip())),
        # AD-11 : `version: sha7`. C'est une **projection** de `git_sha`, qui porte la
        # révision complète depuis la story 4.5 — une seule source de vérité.
        version=etat.settings.version_publiee,
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
        thresholds=etat.settings.thresholds(),
        caches=_etat_des_caches(etat))
