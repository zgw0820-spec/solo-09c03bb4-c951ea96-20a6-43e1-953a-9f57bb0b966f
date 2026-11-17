#!/usr/bin/env python3
"""Stdlib-only check that the FastAPI layer wires up and routes correctly.

A minimal in-memory reimplementation of the tiny slice of pydantic /
fastapi the application uses lets us import the real ``app.main`` and exercise
the new circuit endpoints through the same call signatures FastAPI would use,
without any third-party packages. The dispatcher is not started: these paths
are pure, short database transactions.

Run with: python3 -I /workspace/scripts/verify_http_stubs.py
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import sys
import tempfile
import types
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_UNSET = object()
PASS = 0
FAIL = 0


def check(condition: bool, message: str) -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok: {message}")
    else:
        FAIL += 1
        print(f"FAIL: {message}")


def _install_stubs() -> None:
    # --------------------------------------------------------------- pydantic
    pydantic = types.ModuleType("pydantic")

    class ModelError(Exception):
        def __init__(self, errors):
            super().__init__(str(errors))
            self.errors = errors

    class BaseModel:
        model_config: dict = {}
        _fields: dict = {}

        def __init__(self, **data: Any):
            extra = self.model_config.get("extra", "ignore")
            for key in data:
                if key not in self._fields and extra == "forbid":
                    raise ModelError([{"loc": (key,), "msg": "extra forbidden"}])
            for name, spec in self._fields.items():
                if name in data:
                    value = data[name]
                    self._validate(name, value, spec)
                    nested = spec.get("nested_model")
                    if nested is not None and isinstance(value, dict):
                        value = nested(**value)
                    setattr(self, name, value)
                elif "default" in spec:
                    setattr(self, name, spec["default"])
                else:
                    raise ModelError([{"loc": (name,), "msg": "field required"}])

        @staticmethod
        def _validate(name, value, spec):
            if isinstance(value, str):
                if "min_length" in spec and len(value) < spec["min_length"]:
                    raise ModelError([{"loc": (name,), "msg": "too short"}])
                if "max_length" in spec and len(value) > spec["max_length"]:
                    raise ModelError([{"loc": (name,), "msg": "too long"}])
            if spec.get("pattern") and isinstance(value, str) and not re.match(spec["pattern"], value):
                raise ModelError([{"loc": (name,), "msg": "pattern mismatch"}])
            if isinstance(value, (int, float)):
                if "ge" in spec and value < spec["ge"]:
                    raise ModelError([{"loc": (name,), "msg": "below ge"}])
                if "le" in spec and value > spec["le"]:
                    raise ModelError([{"loc": (name,), "msg": "above le"}])

        def model_dump(self, mode: str | None = None) -> dict:
            def plain(value):
                if isinstance(value, BaseModel):
                    return value.model_dump(mode=mode)
                if isinstance(value, dict):
                    return {k: plain(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [plain(v) for v in value]
                return value

            return {k: plain(v) for k, v in self.__dict__.items() if not k.startswith("_")}

        @classmethod
        def validate(cls, data):
            if not isinstance(data, dict):
                raise ModelError([{"msg": "expected object"}])
            return cls(**data)

    def __init_subclass__(cls, **kwargs):
        import typing

        fields: dict = {}
        module = sys.modules.get(cls.__module__)
        for klass in cls.__mro__:
            for name in getattr(klass, "__annotations__", {}):
                if name.startswith("_") or name == "model_config":
                    continue
                value = klass.__dict__.get(name, _UNSET)
                spec = value if isinstance(value, dict) else (
                    {} if value is _UNSET else {"default": value}
                )
                annotation = getattr(klass, "__annotations__", {}).get(name)
                model = _resolve_model(annotation, module)
                if model is not None:
                    spec["nested_model"] = model
                fields[name] = spec
        cls._fields = fields

    def _resolve_model(annotation, module):
        # Resolve string / typing annotations to a BaseModel subclass if any.
        if isinstance(annotation, str) and module is not None:
            try:
                annotation = eval(annotation, module.__dict__)  # noqa: S307 - test stub
            except NameError:
                return None
        import typing
        origin = typing.get_origin(annotation)
        args = typing.get_args(annotation)
        candidates = list(args) + ([annotation] if isinstance(annotation, type) else [])
        for candidate in candidates:
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                return candidate
        return None

    BaseModel.__init_subclass__ = classmethod(__init_subclass__)

    def Field(default=_UNSET, *args, **kwargs):  # noqa: N802
        spec = dict(kwargs)
        if default is not _UNSET:
            spec["default"] = default
        return spec

    class ConfigDict(dict):
        pass

    pydantic.BaseModel = BaseModel
    pydantic.Field = Field
    pydantic.ConfigDict = ConfigDict
    pydantic.ModelError = ModelError
    sys.modules["pydantic"] = pydantic

    # ------------------------------------------------------------------ httpx
    httpx = types.ModuleType("httpx")

    class _Error(Exception):
        def __init__(self, *args, request=None):
            super().__init__(*args)
            self.request = request

    class AsyncClient:
        def __init__(self, *args, **kwargs):
            self.base_url = kwargs.get("base_url", "")

        async def aclose(self):
            return None

    class ASGITransport:
        def __init__(self, app=None):
            self.app = app

    class TimeoutException(_Error):
        pass

    class TransportError(_Error):
        pass

    class ConnectError(_Error):
        pass

    httpx.AsyncClient = AsyncClient
    httpx.ASGITransport = ASGITransport
    httpx.TimeoutException = TimeoutException
    httpx.TransportError = TransportError
    httpx.ConnectError = ConnectError
    sys.modules["httpx"] = httpx

    # ---------------------------------------------------------------- fastapi
    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: Any = None):
            self.status_code = status_code
            self.detail = detail

    class Response:
        def __init__(self):
            self.status_code = 200

    class QueryDefault:
        def __init__(self, default, **kwargs):
            self.default = default
            self.kwargs = kwargs

    def Query(default=None, **kwargs):  # noqa: N802
        return QueryDefault(default, **kwargs)

    class status:
        HTTP_201_CREATED = 201

    class FastAPI:
        def __init__(self, *args, **kwargs):
            self.title = kwargs.get("title")
            self.version = kwargs.get("version")
            self.description = kwargs.get("description")
            self.lifespan_context = kwargs.get("lifespan")
            self.state = types.SimpleNamespace()
            self.router = types.SimpleNamespace(lifespan_context=kwargs.get("lifespan"))
            self._routes: list[dict] = []

        def _add(self, methods, path, func, meta):
            self._routes.append({"methods": methods, "path": path, "func": func, "meta": meta})

        def get(self, path, *args, **kwargs):
            return lambda func: (self._add(("GET",), path, func, kwargs), func)[1]

        def post(self, path, *args, **kwargs):
            return lambda func: (self._add(("POST",), path, func, kwargs), func)[1]

        def put(self, path, *args, **kwargs):
            return lambda func: (self._add(("PUT",), path, func, kwargs), func)[1]

    class RequestObj:
        def __init__(self, app):
            self.app = app

    # Alias used as a type annotation in the real app.
    class Request(RequestObj):
        pass

    fastapi.FastAPI = FastAPI
    fastapi.HTTPException = HTTPException
    fastapi.Response = Response
    fastapi.Query = Query
    fastapi.QueryDefault = QueryDefault
    fastapi.Request = Request
    fastapi.RequestObj = RequestObj
    fastapi.status = status
    sys.modules["fastapi"] = fastapi


def _match(template: str, path: str) -> bool:
    t = [p for p in template.split("/") if p]
    p = [p for p in path.split("/") if p]
    return len(t) == len(p) and all(seg.startswith("{") or seg == x for seg, x in zip(t, p))


def _path_params(template: str, path: str) -> dict:
    out = {}
    for seg, x in zip(template.split("/"), path.split("/")):
        if seg.startswith("{") and seg.endswith("}"):
            out[seg[1:-1]] = x
    return out


async def call(app, method: str, path: str, body: dict | None = None, query: dict | None = None):
    from fastapi import HTTPException, QueryDefault, RequestObj, Response
    from pydantic import BaseModel, ModelError

    route_path = path.split("?")[0]
    route = next(
        (r for r in app._routes if method in r["methods"] and _match(r["path"], route_path)),
        None,
    )
    if route is None:
        return 404, {"detail": "no route"}

    sig = inspect.signature(route["func"])
    from pydantic import BaseModel as _BM
    from pydantic import ModelError

    def resolve(annotation):
        # `from __future__ import annotations` makes annotations strings.
        if isinstance(annotation, str):
            module = inspect.getmodule(route["func"])
            try:
                return eval(annotation, module.__dict__)  # noqa: S307 - test-only stub
            except NameError:
                return annotation
        return annotation

    kwargs = {}
    params = _path_params(route["path"], route_path)
    for name, param in sig.parameters.items():
        annotation = resolve(param.annotation)
        is_model = isinstance(annotation, type) and issubclass(annotation, _BM)
        if name == "request":
            kwargs[name] = RequestObj(app)
        elif name == "response":
            kwargs[name] = Response()
        elif name in params:
            kwargs[name] = params[name]
        elif isinstance(param.default, QueryDefault):
            raw = (query or {}).get(name, param.default.default)
            if annotation is float:
                raw = float(raw)
            if isinstance(raw, (int, float)):
                if "ge" in param.default.kwargs and raw < param.default.kwargs["ge"]:
                    return 422, {"detail": [{"loc": (name,), "msg": "below ge"}]}
                if "le" in param.default.kwargs and raw > param.default.kwargs["le"]:
                    return 422, {"detail": [{"loc": (name,), "msg": "above le"}]}
            kwargs[name] = raw
        elif body is not None and is_model:
            try:
                kwargs[name] = annotation.validate(body)
            except ModelError as exc:
                return 422, {"detail": exc.errors}
    try:
        result = route["func"](**kwargs)
        if inspect.iscoroutine(result):
            result = await result
    except HTTPException as exc:
        return exc.status_code, {"detail": exc.detail}

    if isinstance(result, BaseModel):
        result = result.model_dump()
    elif isinstance(result, list):
        result = [r.model_dump() if isinstance(r, BaseModel) else r for r in result]
    status_code = 201 if route["meta"].get("status_code") == 201 else 200
    return status_code, json.loads(json.dumps(result, default=str))


async def main() -> int:
    _install_stubs()
    from app.config import Settings
    from app.db import Database
    from app.main import create_app
    from app.service import AlarmService

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(database_path=str(Path(tmp) / "http.db"), gateway_url="http://gw.test")
        app = create_app(settings)
        # Assemble the same components lifespan would, minus the network worker.
        app.state.db = Database(settings.database_path)
        app.state.service = AlarmService(app.state.db)
        app.state.settings = settings

        class _IdleDispatcher:
            async def reconcile_unknown(self, *args, **kwargs):
                return 0

        app.state.dispatcher = _IdleDispatcher()

        print("[http] endpoint registration and routing")
        paths = {(r["methods"], r["path"]) for r in app._routes}
        check((("GET",), "/cabinets/{cabinet_no}/circuit") not in paths, "sanity: no GET on rewiring path")
        check((("PUT",), "/cabinets/{cabinet_no}/circuit") in paths, "rewiring endpoint registered")
        check((("GET",), "/cabinets/circuit-mappings") in paths, "mapping list endpoint registered")
        check((("GET",), "/circuit-clues") in paths, "current clues endpoint registered")
        check((("GET",), "/circuit-clues/history") in paths, "history endpoint registered")

        code, body = await call(app, "GET", "/cabinets/circuit-mappings")
        check(code == 200 and body == [], "static mapping route not swallowed by {cabinet_no}")

        code, body = await call(app, "PUT", "/cabinets/A07/circuit", {"circuit_no": "LOOP-A"})
        check(code == 200 and body["changed"] is True and body["previous_circuit_no"] is None,
              "first assignment accepted")
        code, _ = await call(app, "PUT", "/cabinets/A08/circuit", {"circuit_no": "LOOP-A"})
        check(code == 200, "second assignment accepted")

        alarm = {
            "alarm_no": "H-1", "content": "hot", "metadata": {"cabinet": "A07"},
            "primary_contact": {"name": "p", "channel": "app"},
            "backup_contact": {"name": "b", "channel": "sms"},
            "ack_deadline_seconds": 5,
        }
        code, body = await call(app, "POST", "/alarms", alarm)
        check(code == 201 and body["circuit_no"] == "LOOP-A" and body["cabinet_no"] == "A07",
              "submission fixes ownership at commit time")
        code, _ = await call(app, "POST", "/alarms",
                             dict(alarm, alarm_no="H-2", metadata={"cabinet": "A08"}))
        check(code == 201, "second alarm accepted")

        code, body = await call(app, "GET", "/circuit-clues")
        check(code == 200 and body["total"] == 1 and body["items"][0]["cabinet_count"] == 2,
              "current clue with two distinct cabinets")
        check(body["history"] is False and body["window_seconds"] == 60.0, "window params echoed")

        code, _ = await call(app, "PUT", "/cabinets/A07/circuit", {"circuit_no": "bad id!"})
        check(code == 422, "invalid circuit pattern rejected")
        code, _ = await call(app, "PUT", "/cabinets/A07/circuit",
                             {"circuit_no": "LOOP-A", "extra": 1})
        check(code == 422, "extra field forbidden")
        code, _ = await call(app, "GET", "/circuit-clues", query={"window_seconds": "9999"})
        check(code == 422, "window_seconds above max rejected")

        code, body = await call(app, "PUT", "/cabinets/A08/circuit", {"circuit_no": "LOOP-Z"})
        check(code == 200 and body["changed"] is True, "rewiring accepted")
        code, current = await call(app, "GET", "/circuit-clues")
        check(current["total"] == 0, "clue leaves current suggestions")
        code, history = await call(app, "GET", "/circuit-clues/history")
        check(history["total"] == 1 and history["items"][0]["archived"] is True,
              "clue retained as history")
        check(history["items"][0]["rewirings"][0]["cabinet_no"] == "A08",
              "history names the rewired cabinet")

        code, body = await call(app, "POST", "/alarms",
                                dict(alarm, alarm_no="H-3", metadata={}))
        check(code == 201 and body["circuit_no"] is None, "cabinet-less alarm still accepted")
        check(any("cabinet" in w for w in body["warnings"]), "cabinet-less warning present")

        code, body = await call(app, "GET", "/health")
        check(code == 200 and body["status"] == "ok", "health endpoint still works")
        code, body = await call(app, "GET", "/alarms/H-1")
        check(code == 200 and body["alarm_no"] == "H-1", "alarm detail still works")
        code, body = await call(app, "GET", "/alarms/NOPE")
        check(code == 404, "unknown alarm still 404")

        app.state.db.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
