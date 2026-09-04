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

    def timeout_for_call(self, llm_timeout_s: float, *, facteur: float = 1.0,
                         marge: float = 0.0) -> float:
        """Timeout à passer au SDK : min(llm_timeout_s, deadline restante).

        Story 5.6 (L1l) — `facteur` **étire** cette borne pour un appel nommé, jamais pour tous, et
        seulement dans le temps que la deadline laisse déjà :

            min( max( llm_timeout_s, min(llm_timeout_s × facteur, restant − marge) ), restant )

        Le rejeu L1j (04/09/2026, `proto/g-partir-l1j.json`) a sorti une requête en 503 `timeout`
        avec **112,2 s de deadline encore disponibles** : l'appel de *vérifier* avait franchi les
        78 s de `llm_timeout_s`, qui majore la durée d'écriture de la plus longue sortie d'étape et
        garde donc contre un appel **pendu** — pas contre un appel long. Trois étapes sur quatre
        avaient abouti ; la personne n'a rien reçu, sur un plafond qui n'était pas celui qu'on avait
        promis de tenir. `deadline_s` est ce plafond-là, et il reste le seul dur : `restant` borne le
        résultat en dernier, si bien qu'un appel étiré finit coupé par la deadline avec l'erreur que
        l'API sait déjà rendre, jamais au-delà.

        Les deux gardes qui encadrent la dérivation :

        - `max(llm_timeout_s, …)` — l'étirement ne **raccourcit** jamais. Sans lui, un `restant` un
          peu supérieur à `marge` rendrait un délai de quelques secondes là où la borne par défaut
          en donnait 78, et le facteur serait devenu un plafond plus court que celui qu'il étire.
        - `min(…, restant)` — l'étirement ne **dépasse** jamais la deadline. Le cas où la borne par
          défaut excède déjà le temps restant retombe alors exactement sur `min(llm_timeout_s,
          restant)`, la formule d'avant L1l.

        `facteur = 1.0` (le défaut, donc tous les appels sauf celui qui le demande) rend cette
        formule d'avant à l'identique, quels que soient `marge` et `restant`.
        """
        restant = self.remaining()
        if facteur <= 1.0:
            return min(llm_timeout_s, restant)
        return min(max(llm_timeout_s, min(llm_timeout_s * facteur, restant - marge)), restant)

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
