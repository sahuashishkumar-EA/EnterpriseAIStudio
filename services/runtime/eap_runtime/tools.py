"""Tool execution: builtin, REST, MCP (streamable HTTP), BigQuery (read-only).

Every call carries the end user's identity (X-On-Behalf-Of) and the trace id. Outbound
auth to internal services uses a Google ID token for the target audience.
"""

from __future__ import annotations

import ast
import operator
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from eap_core import gcp
from eap_core.context import CallContext

from .mcp_client import McpClient, result_payload
from .models import ToolSpec


class ToolError(Exception):
    pass


def tool_name(binding: dict[str, Any]) -> str:
    name = binding.get("name") or binding["ref"]
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:64]


def tool_spec(binding: dict[str, Any], entry: dict[str, Any]) -> ToolSpec:
    schema = entry.get("input_schema") or {}
    cfg = entry.get("config") or {}
    if entry.get("kind") == "bigquery" and not schema.get("properties"):
        type_map = {"INT64": "integer", "FLOAT64": "number", "BOOL": "boolean"}
        props = {p: {"type": type_map.get(t.upper(), "string")} for p, t in (cfg.get("parameters") or {}).items()}
        schema = {"type": "object", "properties": props, "required": list(props)}
    if entry.get("kind") == "rest" and cfg.get("input_schema"):
        schema = cfg["input_schema"]
    if not schema:
        schema = {"type": "object", "properties": {}}
    return ToolSpec(name=tool_name(binding), description=entry.get("description") or entry.get("title", ""),
                    parameters=schema)


# ------------------------------------------------------------------------ builtin

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def safe_eval(expr: str) -> float:
    def ev(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            if isinstance(n.op, ast.Pow) and abs(ev(n.right)) > 100:
                raise ToolError("exponent too large")
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ToolError("unsupported expression")

    if len(expr) > 200:
        raise ToolError("expression too long")
    return ev(ast.parse(expr, mode="eval"))


DEMO_CRM = {
    "C-1001": {"customer_id": "C-1001", "name": "Anna de Vries", "segment": "premium", "country": "NL",
               "lifetime_value_eur": 18250.0, "open_tickets": 1, "products": ["mortgage", "savings"]},
    "C-1002": {"customer_id": "C-1002", "name": "Jonas Becker", "segment": "standard", "country": "DE",
               "lifetime_value_eur": 2140.0, "open_tickets": 0, "products": ["current_account"]},
    "C-1003": {"customer_id": "C-1003", "name": "Sofia Rossi", "segment": "smb", "country": "IT",
               "lifetime_value_eur": 56300.0, "open_tickets": 3, "products": ["business_loan", "payments"]},
}


async def run_builtin(fn: str, args: dict[str, Any]) -> Any:
    if fn == "echo":
        return args
    if fn == "calculator":
        return {"result": safe_eval(str(args.get("expression", "")))}
    if fn == "current_time":
        return {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if fn == "crm_lookup":
        rec = DEMO_CRM.get(str(args.get("customer_id", "")).upper())
        if rec is None:
            return {"found": False, "customer_id": args.get("customer_id")}
        return {"found": True, **rec}
    raise ToolError(f"unknown builtin {fn}")


# ---------------------------------------------------------------------- bigquery

_DML = re.compile(r"\b(insert|update|delete|merge|drop|create|alter|truncate|grant|revoke|call|execute|export)\b", re.I)


def check_readonly_sql(sql: str) -> None:
    s = re.sub(r"--.*?$|/\*.*?\*/", " ", sql, flags=re.S | re.M).strip().rstrip(";").strip()
    if ";" in s:
        raise ToolError("multiple statements are not allowed")
    if not re.match(r"^(select|with)\b", s, re.I):
        raise ToolError("only SELECT queries are allowed")
    if _DML.search(s):
        raise ToolError("statement contains a non-read-only keyword")


class ToolRunner:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, project: str | None = None):
        self.client = httpx.AsyncClient(transport=transport, timeout=60)
        self.project = project
        self._mcp: dict[str, McpClient] = {}

    async def _auth_headers(self, mode: str, audience: str) -> dict[str, str]:
        if mode == "google_id_token":
            return {"Authorization": f"Bearer {await gcp.id_token(audience)}"}
        return {}

    def _identity_headers(self, ctx: CallContext) -> dict[str, str]:
        return {"X-On-Behalf-Of": ctx.on_behalf_of, "X-EAP-Trace-Id": ctx.trace_id, "X-EAP-Run-Id": ctx.run_id}

    async def run(self, entry: dict[str, Any], args: dict[str, Any], ctx: CallContext) -> Any:
        kind = entry.get("kind")
        cfg = entry.get("config") or {}
        if entry.get("status", "active") != "active":
            raise ToolError(f"tool {entry.get('id')} is {entry.get('status')}")
        if kind == "builtin":
            return await run_builtin(cfg["function"], args)
        if kind == "rest":
            return await self._rest(cfg, args, ctx)
        if kind == "mcp":
            return await self._mcp_call(cfg, args, ctx)
        if kind == "bigquery":
            return await self._bigquery(cfg, args)
        raise ToolError(f"unsupported tool kind {kind}")

    async def _rest(self, cfg: dict[str, Any], args: dict[str, Any], ctx: CallContext) -> Any:
        args = dict(args)
        path = cfg["path"]
        for k in re.findall(r"\{(\w+)\}", path):
            if k not in args:
                raise ToolError(f"missing path parameter {k}")
            path = path.replace("{" + k + "}", quote(str(args.pop(k)), safe=""))
        url = cfg["base_url"].rstrip("/") + "/" + path.lstrip("/")
        host = urlparse(url).hostname or ""
        allowed = cfg.get("allowed_hosts") or [urlparse(cfg["base_url"]).hostname]
        if host not in allowed:
            raise ToolError(f"host {host} not allowed")
        headers = {**self._identity_headers(ctx), **await self._auth_headers(cfg.get("auth", "google_id_token"), cfg["base_url"])}
        method = cfg.get("method", "GET").upper()
        timeout = float(cfg.get("timeout_s", 20))
        if method == "GET":
            r = await self.client.get(url, params=args, headers=headers, timeout=timeout)
        else:
            r = await self.client.request(method, url, json=args, headers=headers, timeout=timeout)
        if r.status_code >= 400:
            raise ToolError(f"HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            return r.text[:20000]

    async def _mcp_call(self, cfg: dict[str, Any], args: dict[str, Any], ctx: CallContext) -> Any:
        url = cfg["url"]
        client = self._mcp.get(url)
        if client is None:
            client = McpClient(url, client=self.client, timeout_s=float(cfg.get("timeout_s", 30)))
            self._mcp[url] = client
        client.headers = {**self._identity_headers(ctx), **await self._auth_headers(cfg.get("auth", "google_id_token"), url)}
        value, is_error = result_payload(await client.call_tool(cfg["tool"], args))
        if is_error:
            raise ToolError(f"MCP tool error: {str(value)[:300]}")
        return value

    async def _bigquery(self, cfg: dict[str, Any], args: dict[str, Any]) -> Any:
        sql = cfg["sql"]
        check_readonly_sql(sql)
        params = []
        for name, typ in (cfg.get("parameters") or {}).items():
            if name not in args:
                raise ToolError(f"missing query parameter {name}")
            params.append({"name": name, "parameterType": {"type": typ.upper()},
                           "parameterValue": {"value": str(args[name])}})
        body = {
            "query": sql,
            "useLegacySql": False,
            "parameterMode": "NAMED",
            "queryParameters": params,
            "maximumBytesBilled": str(cfg.get("max_bytes_billed", 1_000_000_000)),
            "maxResults": int(cfg.get("max_rows", 200)),
            "location": cfg.get("location", "EU"),
            "timeoutMs": 30000,
        }
        token = await gcp.access_token()
        r = await self.client.post(
            f"https://bigquery.googleapis.com/bigquery/v2/projects/{cfg['project']}/queries",
            json=body, headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code >= 400:
            raise ToolError(f"BigQuery {r.status_code}: {r.text[:300]}")
        data = r.json()
        fields = [f["name"] for f in data.get("schema", {}).get("fields", [])]
        rows = [{fields[i]: c.get("v") for i, c in enumerate(row.get("f", []))} for row in data.get("rows", [])]
        return {"rows": rows, "total_rows": data.get("totalRows"), "truncated": not data.get("jobComplete", True)}
