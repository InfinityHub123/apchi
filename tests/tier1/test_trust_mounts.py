"""What the Validation pod trusts: the CA bundle and the Java truststore the chart mounts into
every Trino pod, mounted the same way into the one Apchi creates.

Without them a Catalog behind an internal CA fails Validation -- the probe connects to it to
prove it is reachable -- while working on the Cluster, which is the failure that makes people
stop trusting Validation.
"""

import pytest
from pydantic import ValidationError

from app.config import CA_BUNDLE_PATH, TRUSTSTORE_DIR, TRUSTSTORE_PATH, MountedFile, Truststore
from app.pipeline.validation import validation_manifest

TRINO_IMAGE = "trinodb/trino:483"
BUNDLE = MountedFile(config_map="trusted-ca", key="ca-bundle.crt")
STORE = Truststore(secret="truststore", key="cacerts")


def _probe(**trust: object) -> dict:
    return validation_manifest("apchi-validate-probe", TRINO_IMAGE, 600, **trust)  # type: ignore[arg-type]


def test_the_paths_are_the_ones_the_chart_mounts_at() -> None:
    """Fixed, and the chart writes the same literals: a probe and a Cluster that trusted
    from different places would validate one thing and run another."""
    assert CA_BUNDLE_PATH == "/etc/pki/tls/certs/ca-bundle.crt"
    assert TRUSTSTORE_PATH == "/etc/pki/java/truststore/cacerts"


def test_nothing_is_mounted_when_nothing_is_configured() -> None:
    spec = _probe()["spec"]
    container = spec["containers"][0]

    assert {v["name"] for v in spec["volumes"]} == {"no-catalogs"}
    assert [e["name"] for e in container["env"]] == ["CATALOG_MANAGEMENT"]


def test_the_ca_bundle_is_one_file_at_the_system_path() -> None:
    """A single file, so the rest of /etc/pki/tls/certs -- the image's hash links -- stays."""
    spec = _probe(ca_bundle=BUNDLE)["spec"]

    assert {
        "name": "ca-bundle",
        "configMap": {
            "name": "trusted-ca",
            "items": [{"key": "ca-bundle.crt", "path": "ca-bundle.crt"}],
        },
    } in spec["volumes"]
    assert {
        "name": "ca-bundle",
        "mountPath": CA_BUNDLE_PATH,
        "subPath": "ca-bundle.crt",
        "readOnly": True,
    } in spec["containers"][0]["volumeMounts"]


def test_the_truststore_is_mounted_and_the_jvm_pointed_at_it() -> None:
    """Java reads only its own store, and the image's is the public one -- so a mounted store
    is used only because the JVM is told to. Through JDK_JAVA_OPTIONS, because the probe keeps
    the image's own jvm.config."""
    spec = _probe(truststore=STORE)["spec"]
    container = spec["containers"][0]

    assert {
        "name": "java-truststore",
        "secret": {"secretName": "truststore", "items": [{"key": "cacerts", "path": "cacerts"}]},
    } in spec["volumes"]
    assert {
        "name": "java-truststore",
        "mountPath": TRUSTSTORE_DIR,
        "readOnly": True,
    } in container["volumeMounts"]
    assert {
        "name": "JDK_JAVA_OPTIONS",
        "value": f"-Djavax.net.ssl.trustStore={TRUSTSTORE_PATH}",
    } in container["env"]


def test_a_truststore_password_comes_from_its_secret_and_never_appears() -> None:
    store = STORE.model_copy(update={"password_secret": "truststore-pw", "password_key": "pw"})

    container = _probe(truststore=store)["spec"]["containers"][0]
    env = {e["name"]: e for e in container["env"]}

    assert env["TRUSTSTORE_PASSWORD"]["valueFrom"] == {
        "secretKeyRef": {"name": "truststore-pw", "key": "pw"}
    }
    # Kubernetes expands $(VAR) from a variable defined before it, so the order matters.
    names = [e["name"] for e in container["env"]]
    assert names.index("TRUSTSTORE_PASSWORD") < names.index("JDK_JAVA_OPTIONS")
    assert (
        "-Djavax.net.ssl.trustStorePassword=$(TRUSTSTORE_PASSWORD)"
        in (env["JDK_JAVA_OPTIONS"]["value"])
    )


@pytest.mark.parametrize(
    "fields",
    [{"key": "k"}, {"key": "k", "config_map": "a", "secret": "b"}],
)
def test_a_source_names_exactly_one_of_a_configmap_and_a_secret(fields: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        MountedFile(**fields)


def test_a_password_is_named_whole() -> None:
    with pytest.raises(ValidationError, match="go together"):
        Truststore(secret="s", key="cacerts", password_secret="pw")
