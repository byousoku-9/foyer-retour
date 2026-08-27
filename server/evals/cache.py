"""Cache persistant du harness d'évals, namespacé et écrit atomiquement.

Le client LLM continue de dériver sa clé du corps exact envoyé au fournisseur. Ce module ajoute
la namespace du run (corpus, pipeline, variante et entrée) sans faire dépendre ``server/app`` des
évals. Une entrée n'est jamais lue « au mieux » : forme, version, namespace et coût sont validés
avant qu'elle puisse devenir un hit.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

CACHE_SCHEMA_VERSION = 1


class CacheCorrompu(ValueError):
    """Une entrée existe mais ne satisfait pas le contrat persistant."""


def json_canonique(value: Any) -> str:
    """Projection JSON byte-stable utilisée par toutes les empreintes du cache."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def empreinte_canonique(value: Any) -> str:
    return hashlib.sha256(json_canonique(value).encode("utf-8")).hexdigest()


class PersistentResponseCache:
    """Adapte le protocole ``ResponseCache`` du client à un répertoire local.

    La namespace peut changer entre deux cas d'un même run. Le runner l'arme juste avant le cas ;
    chaque clé fournisseur est alors isolée par l'ensemble des composantes normatives de ce cas.
    """

    def __init__(self, root: Path, namespace: dict[str, Any] | None = None) -> None:
        self.root = Path(root)
        self._namespace: dict[str, Any] = {}
        self._namespace_digest = ""
        self._logical_root: str | None = None
        self._logical_aliases: list[tuple[str, str, dict[str, Any]]] = []
        self.set_namespace(namespace or {})

    @property
    def namespace_digest(self) -> str:
        return self._namespace_digest

    def set_namespace(self, namespace: dict[str, Any]) -> None:
        # Copie canonique : l'appelant ne peut pas muter la namespace après son empreinte.
        try:
            copie = json.loads(json_canonique(namespace))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"namespace de cache non canonique : {type(exc).__name__}") from exc
        if not isinstance(copie, dict):
            raise ValueError("la namespace de cache doit être un objet JSON")
        self._namespace = copie
        self._namespace_digest = empreinte_canonique(copie)
        self._logical_root = None
        self._logical_aliases = []

    def _path(self, key: str) -> Path:
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("clé de cache fournisseur invalide")
        return self.root / self._namespace_digest[:2] / self._namespace_digest / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            if self._logical_root is None:
                self._logical_root = key
            return None
        except UnicodeDecodeError as exc:
            raise CacheCorrompu("cache d'évals UTF-8 invalide") from exc
        except OSError as exc:
            raise CacheCorrompu(f"cache d'évals illisible ({type(exc).__name__})") from exc
        try:
            doc = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CacheCorrompu(f"cache d'évals JSON invalide ({type(exc).__name__})") from exc
        if not isinstance(doc, dict):
            raise CacheCorrompu("cache d'évals invalide : objet JSON attendu")
        attendu = {"schema_version", "namespace_digest", "request_key", "value"}
        if set(doc) != attendu:
            raise CacheCorrompu("cache d'évals invalide : champs inattendus ou manquants")
        if type(doc["schema_version"]) is not int or doc["schema_version"] != CACHE_SCHEMA_VERSION:
            raise CacheCorrompu("cache d'évals invalide : version de schéma inconnue")
        if doc["namespace_digest"] != self._namespace_digest or doc["request_key"] != key:
            raise CacheCorrompu("cache d'évals invalide : empreinte incohérente")
        value = doc["value"]
        if not isinstance(value, dict) or set(value) != {"response", "cost_eur"}:
            raise CacheCorrompu("cache d'évals invalide : réponse ou coût absent")
        cost = value["cost_eur"]
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) \
                or not math.isfinite(cost) or cost < 0:
            raise CacheCorrompu("cache d'évals invalide : coût original non fini ou négatif")
        if not isinstance(value["response"], dict):
            raise CacheCorrompu("cache d'évals invalide : réponse fournisseur non objet")
        if self._logical_root is not None and self._logical_root != key:
            # Le premier corps a produit une réponse invalide, mais sa relance possède déjà une
            # réponse valide. L'alias sera finalisé depuis la trace courante, qui additionne aussi
            # le coût original du hit : même après une interruption entre deux écritures, le coût
            # logique complet redevient l'autorité.
            self._write(self._logical_root, value)
            self._logical_aliases.append((self._logical_root, key, value))
        self._logical_root = None
        return value

    def set(self, key: str, value: dict[str, Any]) -> None:
        # Valider avant toute création de dossier ; ``get`` réapplique les mêmes contraintes.
        cost = value.get("cost_eur") if isinstance(value, dict) else None
        if set(value) != {"response", "cost_eur"} or not isinstance(value.get("response"), dict):
            raise ValueError("valeur de cache invalide")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) \
                or not math.isfinite(cost) or cost < 0:
            raise ValueError("coût original de cache invalide")
        root = self._logical_root or key
        self._write(key, value)
        if root != key:
            self._write(root, value)
        self._logical_aliases.append((root, key, value))
        self._logical_root = None

    def finalize_logical_costs(self, costs: list[float]) -> None:
        """Réécrit chaque alias initial avec le coût de son appel logique, retries compris."""
        if len(costs) < len(self._logical_aliases):
            raise CacheCorrompu(
                "cache d'évals incohérent : appels logiques et réponses cachées divergent")
        for (root, key, value), cost in zip(
                self._logical_aliases, costs[:len(self._logical_aliases)], strict=True):
            if isinstance(cost, bool) or not isinstance(cost, (int, float)) \
                    or not math.isfinite(cost) or cost < 0:
                raise CacheCorrompu("cache d'évals invalide : coût logique non fini ou négatif")
            self._write(root, {"response": value["response"], "cost_eur": round(cost, 4)})
            if key != root:
                self._write(key, {"response": value["response"], "cost_eur": round(cost, 4)})
        self._logical_aliases = []

    def _write(self, key: str, value: dict[str, Any]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json_canonique({
            "schema_version": CACHE_SCHEMA_VERSION,
            "namespace_digest": self._namespace_digest,
            "request_key": key,
            "value": value,
        }) + "\n"
        fd, temporaire = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporaire, path)
        except BaseException:
            try:
                os.unlink(temporaire)
            except FileNotFoundError:
                pass
            raise
