"""Contrôle hors ligne du schéma JSON réel de ``gcloud storage buckets describe``."""

from scripts.gcp_bucket_security import source_bucket_security_ok
from scripts import gcp_bucket_security


def test_source_bucket_security_accepts_the_real_gcloud_shape() -> None:
    assert source_bucket_security_ok(
        {
            "name": "foyer-retour-sources",
            "uniform_bucket_level_access": True,
            "public_access_prevention": "enforced",
        }
    )


def test_source_bucket_security_rejects_inherited_or_the_old_api_shape() -> None:
    assert not source_bucket_security_ok(
        {
            "uniform_bucket_level_access": True,
            "public_access_prevention": "inherited",
        }
    )


def test_source_bucket_security_cli_distinguishes_insecure_from_invalid_json(
    monkeypatch,
) -> None:
    import io

    monkeypatch.setattr(gcp_bucket_security.sys, "stdin", io.StringIO(
        '{"uniform_bucket_level_access": true, "public_access_prevention": "inherited"}'
    ))
    assert gcp_bucket_security.main() == 1
    monkeypatch.setattr(gcp_bucket_security.sys, "stdin", io.StringIO(
        '{"uniform_bucket_level_access": true, "public_access_prevention": "enforced"}'
    ))
    assert gcp_bucket_security.main() == 0
    monkeypatch.setattr(gcp_bucket_security.sys, "stdin", io.StringIO("not-json"))
    assert gcp_bucket_security.main() == 2
    monkeypatch.setattr(gcp_bucket_security.sys, "stdin", io.StringIO(
        '{"iamConfiguration": {"uniformBucketLevelAccess": {"enabled": true}}}'
    ))
    assert gcp_bucket_security.main() == 2
    assert not source_bucket_security_ok(
        {
            "iamConfiguration": {
                "uniformBucketLevelAccess": {"enabled": True},
                "publicAccessPrevention": "enforced",
            }
        }
    )
