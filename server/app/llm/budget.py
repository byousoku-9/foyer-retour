"""NFR3 / AD-9 — Deadline monotone, compteur d'appels et cumul de coût, partagés par toutes les
étapes d'une requête. Créé par l'API (story 1.6) avec `deadline_s` ; le client LLM le consulte
avant chaque appel et y consigne chaque usage réel."""

from __future__ import annotations

import time
import uuid

from server.app.domain.artifact import document_artifact_uid
from server.app.domain.errors import Timeout


class RequestBudget:
    """Budget d'une requête : temps (deadline monotone), appels, euros."""

    def __init__(self, deadline_s: float, max_attempts: int, max_cost_eur: float) -> None:
        self.deadline_s = deadline_s
        self.max_attempts = max_attempts
        self.max_cost_eur = max_cost_eur
        self.attempts = 0
        self.cost_eur = 0.0
        self.cost_alerted = False  # AD-10 : `cout_eleve` levé une seule fois par requête, au franchissement
        self.run_uid = f"run:{uuid.uuid4()}"
        self.artifact_uid = ""
        # Story 1.4 (reprise B5) : empreintes des préfixes dont le fournisseur a confirmé l'écriture (ou la
        # lecture) pendant cette requête — `estimate_cost` peut alors compter le préfixe au tarif
        # `cache_read` (0,1×) au lieu de l'écriture (2×). Le cache reste chaud : la deadline vaut moins
        # d'une minute et demie (`Settings.deadline_s`), les TTL servis sont d'au moins 5 min.
        # Un préfixe que le fournisseur n'a pas caché (trop court pour son seuil minimal) n'entre jamais ici.
        self._prefixes: set[str] = set()
        self._t0 = time.monotonic()

    def bind_artifact(self, *, document_uid: str, source_hash: str | None,
                      ingest_fingerprint: str | None) -> str:
        """Lie tout le pipeline à un artefact sans publier son contenu."""
        candidate = document_artifact_uid(
            document_uid=document_uid, source_hash=source_hash,
            ingest_fingerprint=ingest_fingerprint,
        )
        if self.artifact_uid and self.artifact_uid != candidate:
            raise ValueError("le budget est déjà lié à un autre artefact documentaire")
        self.artifact_uid = candidate
        return candidate

    def remaining(self) -> float:
        """Secondes restantes avant la deadline (peut être négatif)."""
        return self.deadline_s - (time.monotonic() - self._t0)

    def timeout_for_call(self, llm_timeout_s: float) -> float:
        """Timeout à passer au SDK : min(llm_timeout_s, deadline restante)."""
        return min(llm_timeout_s, self.remaining())

    def exiger_le_temps_decrire(self, duree_majoree: float, *, etape: str) -> None:
        """Refuse **avant l'envoi** un appel que le temps restant ne peut pas laisser aboutir.

        Correctif du tour 4 (C2). `timeout_for_call` tronquait le délai au temps restant sans jamais
        demander si ce temps suffisait. Mesuré sur A16 : un appel a été envoyé avec 24,08 s
        disponibles pour une sortie qui en demande 45,66 au débit minoré publié. Il ne pouvait pas
        aboutir. Il a coûté 24 s, **zéro token**, zéro euro — et la totalité de la marge dont la
        remise de la réponse avait besoin. Un appel dont on sait qu'il expirera n'est pas un appel :
        c'est une attente payée en temps.

        L'erreur est `Timeout`, typée comme celle que l'appel aurait fini par lever, pour que les
        appelants qui savent déjà la traiter — la relance et la reprise servent alors l'acquis — la
        traitent sans rien apprendre de neuf. `attempts` n'est pas incrémenté : rien n'a été envoyé.
        """
        restant = self.remaining()
        if duree_majoree > restant:
            raise Timeout(
                f"temps insuffisant pour l'étape {etape} : {duree_majoree:.1f} s requises au débit "
                f"minoré, {restant:.1f} s restantes — appel non envoyé")

    def note_call(self, usage) -> None:
        """Consigne un appel réellement facturé : cumule son coût (les attempts sont comptés à l'envoi)."""
        self.cost_eur = round(self.cost_eur + usage.cost_eur, 4)

    def note_prefix(self, digest: str) -> None:
        """Consigne l'empreinte d'un préfixe (modèle + système + tools + schéma) que le fournisseur a
        effectivement écrit ou lu dans son cache. L'appelant ne l'appelle qu'au vu de l'`usage` renvoyé
        (`cache_creation` ou `cache_read_input_tokens` non nul) : ni un échec d'appel, ni un préfixe sous
        la taille minimale cacheable du modèle ne doivent escompter le tarif `cache_read`."""
        self._prefixes.add(digest)

    def prefix_seen(self, digest: str) -> bool:
        """Le préfixe a-t-il déjà été écrit dans cette requête ?"""
        return digest in self._prefixes
