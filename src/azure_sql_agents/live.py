"""Fixed Azure public-cloud GETs and fixed SQL metadata catalog. No write API."""
from __future__ import annotations

import base64
import ipaddress
import json
import socket
import struct

from .broker import Denied
from .models import Plan, Policy

PRIVATE_RANGES = tuple(ipaddress.ip_network(n) for n in
                       ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def require_private_dns(host: str):
    addresses = {item[4][0] for item in socket.getaddrinfo(host, 1433, type=socket.SOCK_STREAM)}
    if not addresses or any(not any(ipaddress.ip_address(a) in n for n in PRIVATE_RANGES)
                            for a in addresses):
        raise Denied("SQL DNS must resolve exclusively to RFC1918 private endpoint addresses")


def require_token_tenant(token: str, tenant: str):
    # This is a binding check on the trusted credential provider's output, not a
    # JWT signature verifier. Azure validates the token when the request arrives.
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if claims["tid"] != tenant:
            raise ValueError("Tenant mismatch")
    except Exception:
        raise Denied("Credential tenant does not match approved target") from None


def read_metadata(policy: Policy, plan: Plan) -> dict:
    # Lazy imports ensure offline planning requires neither Azure SDK nor ODBC.
    import httpx
    import pyodbc
    from azure.identity import AzureCliCredential, ManagedIdentityCredential

    host = f"{policy.target.server}.database.windows.net"
    require_private_dns(host)
    credential = (AzureCliCredential(tenant_id=policy.target.tenant, process_timeout=10)
                  if policy.identity == "azure_cli" else
                  ManagedIdentityCredential(client_id=policy.managed_identity_client_id))
    try:
        arm_token = credential.get_token("https://management.azure.com/.default").token
        require_token_tenant(arm_token, policy.target.tenant)
        target = policy.target
        base = (f"https://management.azure.com/subscriptions/{target.subscription}"
                f"/resourceGroups/{target.resource_group}/providers/Microsoft.Sql"
                f"/servers/{target.server}")
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            def get(suffix):
                with client.stream("GET", base + suffix, params={"api-version": "2023-08-01"},
                                   headers={"Authorization": f"Bearer {arm_token}"}) as response:
                    response.raise_for_status()
                    chunks = bytearray()
                    for chunk in response.iter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > 262144:
                            raise Denied("Azure response too large")
                    return json.loads(chunks)["properties"]
            server = get("")
            database = get(f"/databases/{target.database}")
            entra = get("/azureADOnlyAuthentications/Default")
        if (server.get("publicNetworkAccess") != "Disabled"
                or server.get("minimalTlsVersion") != "1.2"
                or entra.get("azureADOnlyAuthentication") is not True
                or database.get("status") != "Online"):
            raise Denied("Private network, TLS 1.2, Entra-only and online database required")
        sql_token = credential.get_token("https://database.windows.net/.default").token
        require_token_tenant(sql_token, policy.target.tenant)
        token = sql_token.encode("utf-16-le")
        token_struct = struct.pack("<I", len(token)) + token
        # Pooling must be disabled before any connection in this dedicated broker process.
        pyodbc.pooling = False
        connection = pyodbc.connect(
            "DRIVER={ODBC Driver 18 for SQL Server};"
            f"SERVER=tcp:{host},1433;DATABASE={target.database};"
            "Encrypt=yes;TrustServerCertificate=no;ApplicationIntent=ReadOnly;ConnectRetryCount=0;",
            attrs_before={1256: token_struct, 101: 1}, timeout=5, autocommit=False)
        try:
            connection.timeout = plan.timeout_seconds
            cursor = connection.cursor()
            try:
                cursor.execute(plan.sql, plan.row_limit)
                columns = [column[0] for column in cursor.description]
                rows = []
                for row in cursor:
                    rows.append(dict(zip(columns, row, strict=True)))
                    if len(rows) > plan.row_limit or len(json.dumps(rows).encode()) > 60000:
                        raise Denied("Metadata response exceeds approved bounds")
                return {"action": plan.action, "rows": rows,
                        "possibly_truncated": len(rows) == plan.row_limit,
                        "posture": {"public_network": "Disabled", "entra_only": True,
                                    "tls_minimum": "1.2"},
                        "coverage": "Metadata visible to this principal only; absence is not proof."}
            finally:
                cursor.close()
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()
    finally:
        credential.close()
