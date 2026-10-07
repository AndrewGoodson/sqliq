import base64
import json
import socket
from types import SimpleNamespace

import pytest

from azure_sql_agents.broker import Denied
from azure_sql_agents.live import read_metadata, require_private_dns, require_token_tenant
from azure_sql_agents.models import Policy, make_plan


def token(tenant):
    return "header." + base64.urlsafe_b64encode(json.dumps({"tid": tenant}).encode()).decode() + ".sig"


@pytest.fixture
def adapter(monkeypatch):
    import azure.identity
    import httpx
    import pyodbc

    policy = Policy.model_validate({"target": {"tenant": "11111111-1111-1111-1111-111111111111",
        "subscription": "22222222-2222-2222-2222-222222222222", "resource_group": "rg",
        "server": "example-sql", "database": "db"}, "approver_public_key": "A" * 43 + "="})
    plan = make_plan(policy, "schema_inventory", "a" * 64)
    observed = {"sql_connections": [], "http": [], "credentials_closed": False,
                "rollback": False, "connection_closed": False, "cursor_closed": False,
                "server": {"publicNetworkAccess": "Disabled", "minimalTlsVersion": "1.2"},
                "database": {"status": "Online"}, "entra": {"azureADOnlyAuthentication": True},
                "rows": [("dbo", "Orders")], "query_fail": False}

    class Credential:
        def __init__(self, **kwargs):
            observed["identity_args"] = kwargs

        def get_token(self, scope):
            return SimpleNamespace(token=token(policy.target.tenant))

        def close(self):
            observed["credentials_closed"] = True

    class Response:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield json.dumps({"properties": self.value}).encode()

    class Client:
        def __init__(self, **kwargs):
            observed["http_options"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def stream(self, method, url, **kwargs):
            observed["http"].append((method, url, kwargs))
            name = ("entra" if "azureADOnlyAuthentications" in url else
                    "database" if "/databases/" in url else "server")
            return Response(observed[name])

    class Cursor:
        description = [("schema_name",), ("table_name",)]

        def execute(self, sql, limit):
            observed["query"] = (sql, limit)
            if observed["query_fail"]:
                raise RuntimeError("secret")

        def __iter__(self):
            return iter(observed["rows"])

        def close(self):
            observed["cursor_closed"] = True

    class Connection:
        def cursor(self):
            return Cursor()

        def rollback(self):
            observed["rollback"] = True

        def close(self):
            observed["connection_closed"] = True

    def connect(text, **kwargs):
        observed["sql_connections"].append((text, kwargs))
        return Connection()

    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", Credential)
    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(pyodbc, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 1433))])
    return policy, plan, observed


def test_connector_uses_fixed_gets_and_encrypted_readonly_connection(adapter):
    policy, plan, seen = adapter
    result = read_metadata(policy, plan)
    assert result["rows"] == [{"schema_name": "dbo", "table_name": "Orders"}]
    assert len(seen["http"]) == 3
    assert all(method == "GET" and url.startswith("https://management.azure.com/subscriptions/")
               for method, url, _ in seen["http"])
    assert seen["http_options"] == {"timeout": 10, "follow_redirects": False, "trust_env": False}
    connection, options = seen["sql_connections"][0]
    assert "ODBC Driver 18" in connection
    assert "Encrypt=yes;TrustServerCertificate=no;ApplicationIntent=ReadOnly;" in connection
    assert not any(value in connection for value in ("PWD=", "UID=", "Authentication="))
    assert options["attrs_before"][101] == 1
    assert options["autocommit"] is False
    assert seen["query"] == (plan.sql, plan.row_limit)
    assert all(seen[key] for key in ("rollback", "cursor_closed", "connection_closed", "credentials_closed"))


@pytest.mark.parametrize("section,field,value", [
    ("server", "publicNetworkAccess", "Enabled"), ("server", "minimalTlsVersion", "1.0"),
    ("entra", "azureADOnlyAuthentication", False), ("database", "status", "Offline"),
    ("server", "publicNetworkAccess", None), ("entra", "azureADOnlyAuthentication", "true"),
])
def test_posture_denies_before_sql_connect(adapter, section, field, value):
    policy, plan, seen = adapter
    seen[section][field] = value
    with pytest.raises(Denied):
        read_metadata(policy, plan)
    assert not seen["sql_connections"]
    assert seen["credentials_closed"]


def test_query_error_closes_all_resources(adapter):
    policy, plan, seen = adapter
    seen["query_fail"] = True
    with pytest.raises(RuntimeError):
        read_metadata(policy, plan)
    assert all(seen[key] for key in ("rollback", "cursor_closed", "connection_closed", "credentials_closed"))


def test_row_limit_closes_resources(adapter):
    policy, plan, seen = adapter
    seen["rows"] *= 101
    with pytest.raises(Denied):
        read_metadata(policy, plan)
    assert seen["connection_closed"]


def test_truncation_is_explicit(adapter):
    policy, plan, seen = adapter
    seen["rows"] *= 100
    assert read_metadata(policy, plan)["possibly_truncated"] is True


@pytest.mark.parametrize("addresses", [["8.8.8.8"], ["127.0.0.1"], ["::1"],
                                      ["169.254.169.254"], ["10.1.1.1", "8.8.8.8"], []])
def test_private_dns_rejects_public_local_metadata_and_mixed_addresses(monkeypatch, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 1433)) for a in addresses])
    with pytest.raises(Denied):
        require_private_dns("example.database.windows.net")


def test_tenant_mismatch_denied():
    with pytest.raises(Denied):
        require_token_tenant(token("other"), "expected")
    with pytest.raises(Denied):
        require_token_tenant("bad", "expected")
    require_token_tenant(token("expected"), "expected")


@pytest.mark.parametrize('fail', [False, True])
def test_write_connection_is_transactional_and_closes_on_failure(adapter, fail):
    from azure_sql_agents.live import approved_connection
    policy, _, seen = adapter

    def use_connection():
        with approved_connection(policy, write=True):
            if fail:
                raise RuntimeError('mock failure')
    if fail:
        with pytest.raises(RuntimeError):
            use_connection()
    else:
        use_connection()
    connection, options = seen['sql_connections'][0]
    assert 'ApplicationIntent=ReadWrite;' in connection
    assert 101 not in options['attrs_before']
    assert options['autocommit'] is False
    assert all(seen[key] for key in ('rollback', 'connection_closed', 'credentials_closed'))
