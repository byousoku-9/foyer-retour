"""Ledger persistant des campagnes live 4.2b, sérialisé entre processus.

Le plafond `LIVE_BUDGET_EUR` appartient à une campagne orchestrateur, pas à une invocation du
runner. Le verrou est conservé pendant tout le run : deux producteurs ne peuvent ni réserver la
même série ni lire le même cumul avant d'appeler le fournisseur.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

SeriesKind = Literal["baseline", "final"]


class CampaignLedgerError(Exception):
    """Le ledger ne permet pas de démarrer ou de poursuivre la campagne."""


class CampaignLedger:
    def __init__(self, directory: Path, *, campaign_id: str, budget_eur: float) -> None:
        if not campaign_id.strip():
            raise CampaignLedgerError("LIVE_CAMPAIGN_ID vide")
        if not math.isfinite(budget_eur) or budget_eur <= 0:
            raise CampaignLedgerError("budget global non fini ou non positif")
        digest = hashlib.sha256(campaign_id.encode("utf-8")).hexdigest()
        self.path = directory / f"{digest}.json"
        self.lock_path = directory / f"{digest}.lock"
        self.campaign_id = campaign_id
        self.budget_eur = budget_eur
        self._lock: Any = None
        self._data: dict[str, Any] = {}

    def __enter__(self) -> CampaignLedger:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock.close()
            self._lock = None
            raise CampaignLedgerError(
                f"campagne {self.campaign_id!r} déjà active dans un autre processus") from exc
        self._data = self._load()
        return self

    def __exit__(self, *_: object) -> None:
        if self._lock is not None:
            fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            data = {
                "schema_version": 1,
                "campaign_id": self.campaign_id,
                "configured_budget_eur": self.budget_eur,
                "accrued_cost_eur": 0.0,
                "series": {"baseline": {}, "final": {}},
                "runs": [],
            }
            self._write(data)
            return data
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CampaignLedgerError(f"ledger {self.path} illisible") from exc
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise CampaignLedgerError(f"ledger {self.path} hors schéma")
        if data.get("campaign_id") != self.campaign_id:
            raise CampaignLedgerError("collision d'identité de campagne")
        configured = data.get("configured_budget_eur")
        accrued = data.get("accrued_cost_eur")
        if (isinstance(configured, bool) or not isinstance(configured, (int, float))
                or not math.isclose(float(configured), self.budget_eur, abs_tol=1e-12)):
            raise CampaignLedgerError(
                f"budget global déjà figé à {configured!r} EUR pour cette campagne")
        if (isinstance(accrued, bool) or not isinstance(accrued, (int, float))
                or not math.isfinite(float(accrued)) or float(accrued) < 0):
            raise CampaignLedgerError("coût cumulé du ledger invalide")
        if not isinstance(data.get("series"), dict) or not isinstance(data.get("runs"), list):
            raise CampaignLedgerError("ledger incomplet")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        fd, temp = tempfile.mkstemp(prefix=".campaign-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        finally:
            Path(temp).unlink(missing_ok=True)

    @property
    def accrued_cost_eur(self) -> float:
        return float(self._data["accrued_cost_eur"])

    def register_series(self, *, kind: SeriesKind, series_id: str,
                        witnesses: list[str], max_series: int = 1) -> None:
        if not series_id.strip():
            raise CampaignLedgerError("identité de série vide")
        by_kind = self._data["series"].setdefault(kind, {})
        for witness in witnesses:
            ids = by_kind.setdefault(witness, [])
            if series_id not in ids and len(ids) >= max_series:
                raise CampaignLedgerError(
                    f"seconde série {kind} refusée pour le témoin nommé {witness!r}")
        for witness in witnesses:
            ids = by_kind.setdefault(witness, [])
            if series_id not in ids:
                ids.append(series_id)
        self._write(self._data)

    def ensure_affordable(self, estimate_eur: float) -> None:
        if self.accrued_cost_eur + estimate_eur > self.budget_eur:
            raise CampaignLedgerError(
                f"budget de campagne : configured_budget_eur={self.budget_eur:.4f} "
                f"accrued_cost_eur={self.accrued_cost_eur:.4f} "
                f"refused_cost_eur={estimate_eur:.4f}")

    def record_cost(self, cost_eur: float) -> None:
        if not math.isfinite(cost_eur) or cost_eur < 0:
            raise CampaignLedgerError("coût fournisseur non fini ou négatif")
        self._data["accrued_cost_eur"] = self.accrued_cost_eur + cost_eur
        self._write(self._data)

    def record_run(self, *, run_digest: str | None, status: str) -> None:
        self._data["runs"].append({"run_digest": run_digest, "status": status,
                                   "accrued_cost_eur": self.accrued_cost_eur})
        self._write(self._data)
