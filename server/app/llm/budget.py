"""NFR3 / AD-9 — Deadline monotone, compteur d'appels et cumul de coût, partagés par toutes les
étapes d'une requête. Créé par l'API (story 1.6) avec `deadline_s` ; le client LLM le consulte
avant chaque appel et y consigne chaque usage réel."""

from __future__ import annotations

import time
import uuid

from server.app.domain.artifact import document_artifact_uid


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
        # `cache_read` (0,1×) au lieu de l'écriture (2×). Le cache reste chaud : deadline 55 s, TTL ≥ 5 min.
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
