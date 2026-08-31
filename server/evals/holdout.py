"""FR38 — reçu public, scellement et prise one-shot du holdout C.

Ce module ne connaît aucun témoin. Il manipule trois objets génériques : un payload chiffré, un
reçu public strict et un verrou fermé. Le contenu clair n'est produit qu'en mémoire, après la prise
irréversible de l'unique tentative, puis remis au runner sous forme de modèles déjà validés.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from server.evals import run as runner

SHA256 = r"^[0-9a-f]{64}$"
CONDITIONS = (
    "epic_5_termine",
    "test_utilisateur_termine",
    "derniers_correctifs_termines",
)
CIPHER = "aes-256-cbc-pbkdf2-sha256+hmac-sha256-v1"


class RefusHoldout(RuntimeError):
    """Refus fermé, avant déchiffrement ou exécution."""


def json_canonique(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest_json(value: Any) -> str:
    return hashlib.sha256(json_canonique(value)).hexdigest()


class ModeleFige(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Comptages(ModeleFige):
    guide: int = Field(ge=1)
    sinistre: int = Field(ge=3, le=4)
    total: int = Field(ge=4)

    @model_validator(mode="after")
    def _total_exact(self) -> Comptages:
        if self.total != self.guide + self.sinistre:
            raise ValueError("counts.total doit être la somme guide + sinistre")
        return self


class SondesFichiers(ModeleFige):
    generator_stage: Literal[True]
    generator_product_code: Literal[False]
    generator_cases_a: Literal[False]
    generator_parent: Literal[False]
    validator_product_code: Literal[True]
    validator_corpora: Literal[False]
    sealer_parent: Literal[False]
    sealer_product: Literal[False]
    sealer_generator_runtime: Literal[False]


class SondesEgress(ModeleFige):
    allowed_hosts: tuple[Literal["chatgpt.com"], ...] = Field(min_length=1, max_length=1)
    provider_via_relay: Literal[True]
    public_repository_via_relay: Literal[False]
    direct_external_egress: Literal[False]


class IsolationPublique(ModeleFige):
    sandbox: Literal["sandbox-exec+provider-relay"]
    filesystem_probes: SondesFichiers
    egress: SondesEgress
    runtime_validated: Literal[True]
    stdout_contains_cleartext: Literal[False]
    stderr_contains_cleartext: Literal[False]


class RecuPublic(ModeleFige):
    schema_version: Literal[1]
    created_at: str = Field(min_length=1)
    cases_hash: str = Field(pattern=SHA256)
    schema_digest: str = Field(pattern=SHA256)
    parameters_digest: str = Field(pattern=SHA256)
    source_digests: dict[str, str]
    payload_digest: str = Field(pattern=SHA256)
    payload_hmac: str = Field(pattern=SHA256)
    arm_token_digest: str = Field(pattern=SHA256)
    payload_bytes: int = Field(gt=0)
    counts: Comptages
    cipher: Literal["aes-256-cbc-pbkdf2-sha256+hmac-sha256-v1"]
    key_service: str = Field(min_length=1, max_length=200)
    key_account: str = Field(min_length=1, max_length=200)
    sealed: Literal[True]
    executed: Literal[False]
    isolation: IsolationPublique

    @model_validator(mode="after")
    def _sources_fermees(self) -> RecuPublic:
        if set(self.source_digests) != {"guide", "sinistre"}:
            raise ValueError("source_digests doit porter exactement guide et sinistre")
        if any(re.fullmatch(SHA256, digest) is None for digest in self.source_digests.values()):
            raise ValueError("chaque empreinte source doit être un SHA-256")
        return self


class VerrouPublic(ModeleFige):
    schema_version: Literal[1]
    receipt_digest: str = Field(pattern=SHA256)
    state: Literal["sealed", "armed", "consumed"]
    attempts_remaining: Literal[0, 1]
    conditions: dict[str, bool]
    created_at: str = Field(min_length=1)
    armed_at: str | None = None
    consumed_at: str | None = None
    arm_token: str | None = Field(default=None, pattern=SHA256)

    @model_validator(mode="after")
    def _coherence(self) -> VerrouPublic:
        if set(self.conditions) != set(CONDITIONS):
            raise ValueError("les trois préconditions one-shot sont obligatoires")
        if self.state == "sealed" and (any(self.conditions.values()) or self.attempts_remaining != 1 or self.arm_token is not None):
            raise ValueError("un verrou sealed est fermé et conserve son unique tentative")
        if self.state == "armed" and (not all(self.conditions.values()) or self.attempts_remaining != 1 or self.arm_token is None):
            raise ValueError("un verrou armed exige les trois preuves et une tentative")
        if self.state == "consumed" and (not all(self.conditions.values()) or self.attempts_remaining != 0 or self.arm_token is not None):
            raise ValueError("un verrou consumed garde les conditions vraies, sans tentative ni jeton")
        return self


class Attestation(ModeleFige):
    schema_version: Literal[1]
    condition: Literal[
        "epic_5_termine",
        "test_utilisateur_termine",
        "derniers_correctifs_termines",
    ]
    completed: Literal[True]
    evidence_file: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    evidence_digest: str = Field(pattern=SHA256)
    completed_at: str = Field(min_length=1)


class CasScelle(ModeleFige):
    doc_id: str | None = None
    case: runner.Cas

    @model_validator(mode="after")
    def _coherence(self) -> CasScelle:
        if self.case.profile != "full" or self.case.truth.source != "codex":
            raise ValueError("un cas C exige profile=full et truth.source=codex")
        if self.case.truth.countersigned_by is not None:
            raise ValueError("C ne peut pas inventer de contresignature humaine")
        if self.case.suite == "guide" and self.case.famille == "multilingue":
            raise ValueError("C exclut la famille multilingue sans contrôle de retraduction scellé")
        if self.case.suite == "guide" and self.doc_id is not None:
            raise ValueError("un cas guide ne porte pas de doc_id")
        if self.case.suite == "sinistre" and not self.doc_id:
            raise ValueError("un cas sinistre doit nommer son document propriétaire")
        if self.case.suite not in {"guide", "sinistre"}:
            raise ValueError("C ne contient que les suites guide et sinistre")
        return self


class LotScelle(ModeleFige):
    schema_version: Literal[1]
    cases: list[CasScelle] = Field(min_length=4)
    references: list[runner.ReferenceUtilite] = Field(min_length=1)

    @model_validator(mode="after")
    def _fermer_le_lot(self) -> LotScelle:
        ids = [item.case.id for item in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("les identifiants C sont uniques")
        guides = {item.case.id for item in self.cases if item.case.suite == "guide"}
        references = {reference.case_id for reference in self.references}
        if references != guides or len(self.references) != len(references):
            raise ValueError("chaque cas guide C exige exactement une référence d'utilité")
        return self

    @property
    def cases_hash(self) -> str:
        objets = [item.model_dump(mode="json") for item in sorted(self.cases, key=lambda value: value.case.id)]
        return digest_json(objets)

    def pour_runner(self, cases_hash: str, *, observed_cases_hash: str | None = None) -> runner.LotCasesFournis:
        if (observed_cases_hash or self.cases_hash) != cases_hash:
            raise RefusHoldout("cases_hash du payload différent du reçu public")
        cas = []
        for item in self.cases:
            modele = item.case.model_copy(deep=True)
            modele._doc_id = item.doc_id
            cas.append(modele)
        reference_dump = [value.model_dump(mode="json") for value in sorted(self.references, key=lambda value: value.case_id)]
        references = runner.ReferencesSnapshot(digest_json(reference_dump))
        snapshot = runner.CasesSnapshot(cases_hash)
        return runner.LotCasesFournis(tuple(cas), references, snapshot)


def _lire_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RefusHoldout(f"artefact public invalide : {path.name}") from exc


def _lire_octets_reguliers(path: Path, *, objet: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb", closefd=True) as flux:
            if not stat.S_ISREG(os.fstat(flux.fileno()).st_mode):
                raise RefusHoldout(f"{objet} doit être un fichier régulier")
            return flux.read()
    except RefusHoldout:
        raise
    except OSError as exc:
        raise RefusHoldout(f"{objet} absent ou illisible") from exc


def lire_recu(path: Path) -> RecuPublic:
    try:
        return RecuPublic.model_validate(_lire_json(path))
    except ValidationError as exc:
        raise RefusHoldout(f"artefact public invalide : {path.name}") from exc


def lire_verrou(path: Path) -> VerrouPublic:
    try:
        return VerrouPublic.model_validate(_lire_json(path))
    except ValidationError as exc:
        raise RefusHoldout(f"artefact public invalide : {path.name}") from exc


def verifier_public(*, receipt_path: Path, lock_path: Path, payload_path: Path) -> dict[str, Any]:
    recu = lire_recu(receipt_path)
    verrou = lire_verrou(lock_path)
    try:
        octets = payload_path.read_bytes()
    except OSError as exc:
        raise RefusHoldout("payload chiffré absent ou illisible") from exc
    if hashlib.sha256(octets).hexdigest() != recu.payload_digest or len(octets) != recu.payload_bytes:
        raise RefusHoldout("payload chiffré différent du reçu")
    if digest_json(recu.model_dump(mode="json")) != verrou.receipt_digest:
        raise RefusHoldout("verrou lié à un autre reçu")
    if verrou.state == "armed" and (verrou.arm_token is None or hashlib.sha256(bytes.fromhex(verrou.arm_token)).hexdigest() != recu.arm_token_digest):
        raise RefusHoldout("verrou armé sans jeton authentique")
    if verrou.state == "consumed":
        marque = lock_path.with_name("consumed.marker")
        brut_marque = _lire_json(marque)
        if (
            not isinstance(brut_marque, dict)
            or brut_marque.get("receipt_digest") != verrou.receipt_digest
            or brut_marque.get("kind") != "secret-hmac-sha256-v1"
            or re.fullmatch(SHA256, str(brut_marque.get("proof", ""))) is None
        ):
            raise RefusHoldout("marque one-shot absente ou invalide")
    return {
        "cases_hash": recu.cases_hash,
        "schema_digest": recu.schema_digest,
        "parameters_digest": recu.parameters_digest,
        "source_digests": recu.source_digests,
        "payload_digest": recu.payload_digest,
        "counts": recu.counts.model_dump(mode="json"),
        "sealed": recu.sealed,
        "executed": verrou.state == "consumed",
        "lock_state": verrou.state,
        "attempts_remaining": verrou.attempts_remaining,
        "isolation": recu.isolation.model_dump(mode="json"),
    }


def armer(*, receipt_path: Path, lock_path: Path, attestations: list[Path]) -> None:
    recu = lire_recu(receipt_path)
    verrou = lire_verrou(lock_path)
    if verrou.state != "sealed":
        raise RefusHoldout("seul un verrou sealed peut être armé")
    if digest_json(recu.model_dump(mode="json")) != verrou.receipt_digest:
        raise RefusHoldout("verrou lié à un autre reçu")
    preuves: list[Attestation] = []
    try:
        for path in attestations:
            preuve = Attestation.model_validate(_lire_json(path))
            evidence_path = path.with_name(preuve.evidence_file)
            if evidence_path == path:
                raise RefusHoldout("une attestation ne peut pas être sa propre preuve")
            evidence = _lire_octets_reguliers(evidence_path, objet="preuve d'attestation")
            if not hmac.compare_digest(hashlib.sha256(evidence).hexdigest(), preuve.evidence_digest):
                raise RefusHoldout("digest de preuve d'attestation différent de l'artefact")
            preuves.append(preuve)
    except ValidationError as exc:
        raise RefusHoldout("attestation invalide") from exc
    if {preuve.condition for preuve in preuves} != set(CONDITIONS) or len(preuves) != len(CONDITIONS):
        raise RefusHoldout("les trois attestations distinctes sont requises")
    secret = _secret_keychain(recu)
    arm_token = hmac.new(secret.encode(), b"holdout-c/arm-token/v1", hashlib.sha256).hexdigest()
    if hashlib.sha256(bytes.fromhex(arm_token)).hexdigest() != recu.arm_token_digest:
        raise RefusHoldout("jeton d'armement différent du reçu")
    arme = VerrouPublic.model_validate(
        verrou.model_dump(mode="json")
        | {
            "state": "armed",
            "conditions": {condition: True for condition in CONDITIONS},
            "armed_at": datetime.now(UTC).isoformat(),
            "arm_token": arm_token,
        }
    )
    _ecrire_json_atomique(lock_path, arme.model_dump(mode="json"))


def _ecrire_json_atomique(path: Path, value: Any) -> None:
    temporaire = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporaire, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as flux:
            flux.write(json_canonique(value) + b"\n")
            flux.flush()
            os.fsync(flux.fileno())
        os.replace(temporaire, path)
    finally:
        try:
            temporaire.unlink()
        except FileNotFoundError:
            pass


def _preuve_marque(secret: bytes, receipt_digest: str) -> str:
    return hmac.new(
        secret,
        b"holdout-c/consumed/v1:" + receipt_digest.encode(),
        hashlib.sha256,
    ).hexdigest()


def prendre_tentative_unique(lock_path: Path, arm_token_digest: str) -> VerrouPublic:
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    except OSError as exc:
        raise RefusHoldout("verrou absent ou illisible") from exc
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError as exc:
        os.close(fd)
        raise RefusHoldout("verrou absent ou illisible") from exc
    if not regular:
        os.close(fd)
        raise RefusHoldout("verrou absent ou illisible")
    marque = lock_path.with_name("consumed.marker")
    with os.fdopen(fd, "r+b", closefd=True) as flux:
        fcntl.flock(flux.fileno(), fcntl.LOCK_EX)
        try:
            verrou = VerrouPublic.model_validate(json.loads(flux.read().decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            raise RefusHoldout("verrou illisible") from exc
        if verrou.state != "armed" or not all(verrou.conditions.values()):
            raise RefusHoldout("holdout encore fermé")
        if verrou.arm_token is None or hashlib.sha256(bytes.fromhex(verrou.arm_token)).hexdigest() != arm_token_digest:
            raise RefusHoldout("verrou armé sans jeton authentique")
        try:
            marque_fd = os.open(marque, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError as exc:
            raise RefusHoldout("tentative one-shot déjà consommée") from exc
        with os.fdopen(marque_fd, "wb", closefd=True) as marque_flux:
            marque_flux.write(
                json_canonique(
                    {
                        "schema_version": 1,
                        "receipt_digest": verrou.receipt_digest,
                        "kind": "arm-token-derived-hmac-sha256-v1",
                        "proof": _preuve_marque(bytes.fromhex(verrou.arm_token), verrou.receipt_digest),
                    }
                )
                + b"\n"
            )
            marque_flux.flush()
            os.fsync(marque_flux.fileno())
        consomme = VerrouPublic.model_validate(
            verrou.model_dump(mode="json")
            | {
                "state": "consumed",
                "attempts_remaining": 0,
                "consumed_at": datetime.now(UTC).isoformat(),
                "arm_token": None,
            }
        )
        flux.seek(0)
        flux.truncate()
        flux.write(json_canonique(consomme.model_dump(mode="json")) + b"\n")
        flux.flush()
        os.fsync(flux.fileno())
        return consomme


def _secret_keychain(recu: RecuPublic) -> str:
    resultat = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", recu.key_service, "-a", recu.key_account, "-w"],
        check=False,
        capture_output=True,
        text=True,
    )
    if resultat.returncode != 0 or not resultat.stdout.strip():
        raise RefusHoldout("clé one-shot indisponible ou interaction utilisateur refusée")
    return resultat.stdout.strip()


def _supprimer_secret_keychain(recu: RecuPublic) -> None:
    resultat = subprocess.run(
        ["/usr/bin/security", "delete-generic-password", "-s", recu.key_service, "-a", recu.key_account],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if resultat.returncode != 0:
        raise RefusHoldout("révocation de la clé one-shot impossible après consommation")


def _finaliser_marque(lock_path: Path, recu: RecuPublic, secret: str) -> None:
    marque = lock_path.with_name("consumed.marker")
    _ecrire_json_atomique(
        marque,
        {
            "schema_version": 1,
            "receipt_digest": digest_json(recu.model_dump(mode="json")),
            "kind": "secret-hmac-sha256-v1",
            "proof": _preuve_marque(secret.encode(), digest_json(recu.model_dump(mode="json"))),
        },
    )


def _openssl(payload: bytes, secret: str, *, decrypt: bool) -> bytes:
    lecture, ecriture = os.pipe()
    try:
        os.write(ecriture, (secret + "\n").encode())
    finally:
        os.close(ecriture)
    commande = [
        "openssl",
        "enc",
        "-aes-256-cbc",
        "-pbkdf2",
        "-md",
        "sha256",
        "-iter",
        "200000",
        "-pass",
        f"fd:{lecture}",
    ]
    if decrypt:
        commande.insert(2, "-d")
    try:
        resultat = subprocess.run(
            commande,
            input=payload,
            check=False,
            capture_output=True,
            pass_fds=(lecture,),
        )
    finally:
        os.close(lecture)
    if resultat.returncode != 0:
        raise RefusHoldout("déchiffrement refusé" if decrypt else "chiffrement refusé")
    return resultat.stdout


def dechiffrer(payload: bytes, recu: RecuPublic, secret: str) -> bytes:
    if hashlib.sha256(payload).hexdigest() != recu.payload_digest:
        raise RefusHoldout("payload chiffré différent du reçu")
    mac_key = hmac.new(secret.encode(), b"holdout-c/mac/v1", hashlib.sha256).digest()
    mac = hmac.new(mac_key, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, recu.payload_hmac):
        raise RefusHoldout("authenticité du payload refusée")
    return _openssl(payload, secret, decrypt=True)


def executer(*, receipt_path: Path, lock_path: Path, payload_path: Path, runner_args: list[str]) -> int:
    """Révoque d'abord la clé : même une panne ou restauration ultérieure interdit tout retry."""
    recu = lire_recu(receipt_path)
    verrou = lire_verrou(lock_path)
    if digest_json(recu.model_dump(mode="json")) != verrou.receipt_digest:
        raise RefusHoldout("verrou lié à un autre reçu")
    secret = _secret_keychain(recu)
    _supprimer_secret_keychain(recu)
    prendre_tentative_unique(lock_path, recu.arm_token_digest)
    _finaliser_marque(lock_path, recu, secret)
    try:
        payload = payload_path.read_bytes()
    except OSError as exc:
        raise RefusHoldout("payload chiffré absent ou illisible après consommation") from exc
    clair = bytearray(dechiffrer(payload, recu, secret))
    try:
        brut = json.loads(clair)
        observed_cases_hash = digest_json(sorted(brut["cases"], key=lambda item: item["case"]["id"]))
        lot = LotScelle.model_validate(brut)
        fourni = lot.pour_runner(recu.cases_hash, observed_cases_hash=observed_cases_hash)
        return runner.main(runner_args, lot_cases_fourni=fourni)
    except (json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
        raise RefusHoldout("payload clair hors schéma") from exc
    finally:
        clair[:] = b"\0" * len(clair)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verrou one-shot du holdout C")
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    arm = sub.add_parser("arm")
    execute = sub.add_parser("execute")
    for command in (status, arm, execute):
        command.add_argument("--receipt", type=Path, required=True)
        command.add_argument("--lock", type=Path, required=True)
    status.add_argument("--payload", type=Path, required=True)
    arm.add_argument("--attestation", action="append", type=Path, required=True)
    execute.add_argument("--payload", type=Path, required=True)
    execute.add_argument("runner_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "status":
            print(
                json_canonique(
                    verifier_public(
                        receipt_path=args.receipt,
                        lock_path=args.lock,
                        payload_path=args.payload,
                    )
                ).decode()
            )
        elif args.command == "arm":
            armer(receipt_path=args.receipt, lock_path=args.lock, attestations=args.attestation)
        else:
            runner_args = args.runner_args[1:] if args.runner_args[:1] == ["--"] else args.runner_args
            return executer(
                receipt_path=args.receipt,
                lock_path=args.lock,
                payload_path=args.payload,
                runner_args=runner_args,
            )
    except RefusHoldout as exc:
        print(f"REFUS_HOLDOUT: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
