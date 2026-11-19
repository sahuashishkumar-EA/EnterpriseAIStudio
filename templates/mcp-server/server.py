"""Example domain MCP server built from the C12 template: exposure checks for the Risk BU.

Copy this directory, rename the server, replace TOOL_DEFS and the tool bodies with calls to your
existing API. Run locally:  EAP_RESOLVER_URL=http://localhost:8082 uvicorn server:app --port 8090
"""

from __future__ import annotations

import os

from eap_mcp_kit import DomainServer, ThresholdSource, current_caller

# Must match the tool_def artifacts registered in the registry (C12-F5).
TOOL_DEFS = {
    "check_exposure": {
        "mcpServerId": "exposure-mcp",
        "toolName": "check_exposure",
        "description": "Check a proposed exposure against the risk threshold",
        "endpoint": "https://exposure-mcp.internal.example/mcp",
        "inputSchema": {"type": "object", "required": ["customer_id", "amount_eur"],
                        "properties": {"customer_id": {"type": "string", "pattern": "^C-[0-9]{4,}$"},
                                       "amount_eur": {"type": "number", "minimum": 0}},
                        "additionalProperties": False},
        "permissions": ["exposure.read"],
        "thresholdKeys": ["maxExposureEur"],
    }
}


def build(thresholds: ThresholdSource | None = None, backend=None) -> DomainServer:
    server = DomainServer("exposure-mcp", thresholds or ThresholdSource.from_resolver(os.environ["EAP_RESOLVER_URL"]),
                          TOOL_DEFS)

    async def default_backend(customer_id: str, amount_eur: float, on_behalf_of: str | None) -> dict:
        # Replace with the call to the system of record, passing on_behalf_of for least privilege (C12-F3).
        return {"customer_id": customer_id, "current_exposure_eur": 120000.0}

    call_backend = backend or default_backend

    @server.tool("check_exposure", threshold=("amount_eur", "maxExposureEur"))
    async def check_exposure(customer_id: str, amount_eur: float) -> dict:
        c = current_caller()
        data = await call_backend(customer_id, amount_eur, c.on_behalf_of)
        return {**data, "proposed_eur": amount_eur, "within_threshold": True}

    return server


if os.environ.get("EAP_RESOLVER_URL"):  # pragma: no cover
    app = build().asgi_app()
