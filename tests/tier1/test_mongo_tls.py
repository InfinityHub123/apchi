"""Authenticating to Apchi's own MongoDB with a client certificate (#186).

Against a real mongod running `--tlsMode requireTLS --auth`, because the assertion worth
making is not that PyMongo accepted an option -- it is that **MongoDB** derived an identity
from the certificate Apchi presented. A test that only inspected client options would pass
against a server that was never asked.

x509 is worth preferring where MongoDB offers it, beyond the deployments that mandate it:
`mongo_uri` then carries no secret at all, where a SCRAM password in it sits in
`helm get values` and in the pod spec.
"""

from collections.abc import Iterator

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.adapters.mongo import Mongo
from app.config import Environment, Settings
from tests.certificates import TlsChain, tls_chain

MONGO_IMAGE = "mongo:7"

#: The identity MongoDB gives Apchi, derived from its certificate's subject rather than
#: configured anywhere. `tls_chain` issues the client certificate to CN=apchi, and MongoDB
#: names the user by the subject in RFC2253 form -- so this string is a consequence of the
#: certificate, not a choice made here.
X509_USER = "CN=apchi"


@pytest.fixture(scope="module")
def chain() -> TlsChain:
    return tls_chain()


@pytest.fixture(scope="module")
def pem_files(chain: TlsChain, tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Apchi's own side of the handshake, on disk for this process to read.

    Only the files Apchi reads. The server's go into the container by copy rather than by
    bind mount, for the reason the container fixture gives.
    """
    directory = tmp_path_factory.mktemp("mongo-tls")
    ca = directory / "ca.pem"
    ca.write_bytes(chain.ca)
    # PyMongo's `tlsCertificateKeyFile` takes one PEM holding the certificate together with
    # its key, which differs from the Trino client's shape -- that one wants them apart.
    client = directory / "client.pem"
    client.write_bytes(chain.client + chain.client_key)
    return {"ca": str(ca), "client": str(client)}


@pytest.fixture(scope="module")
def tls_mongo(chain: TlsChain, pem_files: dict[str, str]) -> Iterator[Settings]:
    """A mongod that requires TLS and authenticates by certificate.

    The server's files are **copied in** rather than bind-mounted. A bind mount needs the
    container's uid to traverse the host directory, and pytest's temporary directories are
    0700 and owned by whoever runs the suite -- mongod runs as uid 999, which is nobody's
    host account, so a mount could not be read at all. The same mistake failed the Trino
    TLS tests in CI while passing locally, where the host uid happened to match that image's.

    The two `mongosh` calls are MongoDB's localhost exception, which allows creating only
    the *first* user and only in `admin` -- so the x509 identity in `$external` has to be
    created by that first user rather than directly. Discovered by trying the direct route
    and being told "not authorized on $external".
    """
    container = (
        DockerContainer(MONGO_IMAGE)
        .with_exposed_ports(27017)
        .with_copy_into_container(chain.ca, "/etc/mongo-tls/ca.pem")
        .with_copy_into_container(chain.server + chain.server_key, "/etc/mongo-tls/server.pem")
        .with_copy_into_container(chain.client + chain.client_key, "/etc/mongo-tls/client.pem")
        .with_command(
            "--tlsMode requireTLS"
            " --tlsCertificateKeyFile /etc/mongo-tls/server.pem"
            " --tlsCAFile /etc/mongo-tls/ca.pem"
            " --auth"
        )
        .waiting_for(LogMessageWaitStrategy("Waiting for connections"))
    )
    with container as started:
        shell = (
            "mongosh --quiet --tls --tlsCAFile /etc/mongo-tls/ca.pem"
            " --tlsCertificateKeyFile /etc/mongo-tls/client.pem --host localhost"
        )
        created = started.exec(
            [
                "sh",
                "-c",
                f'{shell} --eval \'db.getSiblingDB("admin").createUser('
                '{user: "root", pwd: "rootpw", roles: ["root"]})\'',
            ]
        )
        assert created.exit_code == 0, created.output
        granted = started.exec(
            [
                "sh",
                "-c",
                f"{shell} -u root -p rootpw --authenticationDatabase admin --eval "
                f'\'db.getSiblingDB("$external").runCommand({{createUser: "{X509_USER}", '
                'roles: [{role: "readWrite", db: "apchi"}, {role: "dbAdmin", db: "apchi"}]})\'',
            ]
        )
        assert granted.exit_code == 0, granted.output

        host = started.get_container_host_ip()
        if host not in ("localhost", "127.0.0.1"):
            # The server certificate's SAN names localhost, and matching it is the point of
            # handing Apchi the CA -- so against a remote Docker daemon this would fail
            # hostname verification and read as a bug in the adapter.
            pytest.skip(f"the test certificate names localhost, and Docker is at {host}")
        port = started.get_exposed_port(27017)
        yield Settings(
            environment=Environment.TEST,
            # The mechanism lives here, where PyMongo already reads it. Note there is no
            # username and no password: that is the point.
            mongo_uri=f"mongodb://localhost:{port}/?authMechanism=MONGODB-X509",
            mongo_database="apchi",
            mongo_ca_file=pem_files["ca"],
            mongo_certificate_key_file=pem_files["client"],
        )


async def test_apchi_reaches_a_mongodb_that_requires_tls(tls_mongo: Settings) -> None:
    mongo = Mongo(tls_mongo)
    try:
        assert await mongo.ping() is True
    finally:
        await mongo.close()


async def test_mongodb_derives_apchis_identity_from_the_certificate(
    tls_mongo: Settings,
) -> None:
    """The assertion that separates authentication from encryption. Nothing told MongoDB who
    Apchi is -- the URI has no username -- so this name came out of the certificate."""
    mongo = Mongo(tls_mongo)
    try:
        status = await mongo.database.client.admin.command("connectionStatus")
        assert status["authInfo"]["authenticatedUsers"] == [{"user": X509_USER, "db": "$external"}]
    finally:
        await mongo.close()


async def test_a_write_round_trips_under_certificate_authentication(
    tls_mongo: Settings,
) -> None:
    """Not just the handshake: the x509 identity has to carry the roles Apchi's writes need,
    and a mongod that authenticated it but granted nothing would still fail here."""
    mongo = Mongo(tls_mongo)
    try:
        await mongo.database["probe"].insert_one({"_id": 1, "written": "under x509"})
        found = await mongo.database["probe"].find_one({"_id": 1})
        assert found is not None and found["written"] == "under x509"
    finally:
        await mongo.close()


async def test_without_the_certificate_the_server_is_unreachable(
    tls_mongo: Settings,
) -> None:
    """`ping` returns False rather than raising, which is what makes `/api/v1/health` answer
    503 instead of crashing -- and is also why a missing *file* must not come through here,
    since it would be indistinguishable from a MongoDB that is down."""
    plaintext = tls_mongo.model_copy(
        update={
            "mongo_uri": tls_mongo.mongo_uri.split("/?")[0],
            "mongo_ca_file": None,
            "mongo_certificate_key_file": None,
        }
    )
    mongo = Mongo(plaintext)
    try:
        assert await mongo.ping() is False
    finally:
        await mongo.close()


def test_either_file_alone_turns_tls_on(tls_mongo: Settings) -> None:
    """PyMongo builds its SSL context from these and does not also need `tls=true`, so there
    is no silent-plaintext configuration to fall into here -- unlike the Trino client, whose
    port-based scheme inference was the whole of #188."""
    from app.adapters.mongo import _tls_options

    assert _tls_options(tls_mongo) == {
        "tlsCAFile": tls_mongo.mongo_ca_file,
        "tlsCertificateKeyFile": tls_mongo.mongo_certificate_key_file,
    }
    assert _tls_options(tls_mongo.model_copy(update={"mongo_ca_file": None})) == {
        "tlsCertificateKeyFile": tls_mongo.mongo_certificate_key_file
    }
    assert (
        _tls_options(
            tls_mongo.model_copy(update={"mongo_ca_file": None, "mongo_certificate_key_file": None})
        )
        == {}
    )


def test_a_missing_pem_names_the_setting_rather_than_only_the_path(
    tls_mongo: Settings,
) -> None:
    """PyMongo opens these while validating its options, and `Mongo` is built in the
    lifespan -- so this stops Apchi at startup rather than degrading to a 503. The path was
    assembled by the chart from a mount point and a Secret key, so the raw FileNotFoundError
    names a file nobody chose by that name."""
    with pytest.raises(RuntimeError, match="mongo_certificate_key_file") as raised:
        Mongo(tls_mongo.model_copy(update={"mongo_certificate_key_file": "/nonexistent.pem"}))

    assert "/nonexistent.pem" in str(raised.value)
    assert isinstance(raised.value.__cause__, FileNotFoundError)


def test_an_unrecognised_option_is_refused_rather_than_ignored() -> None:
    """Why the files are passed as keyword arguments and not appended to `mongo_uri` as
    query parameters: PyMongo validates a keyword argument it does not know."""
    from pymongo import AsyncMongoClient
    from pymongo.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="Unknown option"):
        AsyncMongoClient("mongodb://localhost:27017/", tlsCAFileTypo=True, connect=False)
