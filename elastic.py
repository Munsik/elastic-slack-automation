"""
elastic.py — Kibana(Agent Builder / Workflows) 호출을 담당하는 얇은 async 클라이언트.

설계 원칙
- 쓰기/실행 경로(converse, workflow run)는 반드시 Kibana plugin API를 통해야 한다.
  (이 엔드포인트들은 raw Elasticsearch가 아니라 Kibana 서버 플러그인이다.)
- 읽기 경로(목록/대화 히스토리 조회)는 Kibana API를 우선 사용하되,
  필요하면 .workflows-workflows / Agent Builder conversation 인덱스를 ES로 직접 조회하는
  fallback 도 가능하다(README 참고).
- 인증은 Kibana API Key 하나로 통일한다: Authorization: ApiKey <base64-id:key>

버전 주의 (반드시 본인 스택에서 확인)
- Agent Builder converse SSE event 이름, Workflows list / execution-status 경로는
  Tech Preview 라서 9.3 / 9.4 / 9.5 사이에 바뀔 수 있다. 아래는 9.4~9.5 기준이며,
  코드는 응답 형태를 방어적으로 파싱하도록 작성했다.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Optional

import httpx
import yaml


class ElasticClient:
    def __init__(self, kibana_url: str, api_key: str, space: str = "default"):
        self.kibana_url = kibana_url.rstrip("/")
        self.api_key = api_key
        # default space 는 prefix 가 필요 없다. 그 외 space 는 /s/<space> 를 붙인다.
        self.space_prefix = "" if space in ("", "default") else f"/s/{space}"
        self._headers = {
            "Authorization": f"ApiKey {api_key}",
            "kbn-xsrf": "true",
            "Content-Type": "application/json",
        }

    # ── URL helpers ────────────────────────────────────────────────────────
    def _api(self, path: str) -> str:
        return f"{self.kibana_url}{self.space_prefix}{path}"

    def conversation_url(self, conversation_id: str) -> str:
        """Kibana Agent Builder 대화 화면 deep-link.
        앱 경로는 스택 버전에 따라 /app/agent_builder 또는 /app/onechat 일 수 있으니 확인할 것."""
        return f"{self.kibana_url}{self.space_prefix}/app/agent_builder/conversations/{conversation_id}"

    def workflow_url(self, workflow_id: str) -> str:
        return f"{self.kibana_url}{self.space_prefix}/app/workflows/{workflow_id}"

    def execution_url(self, workflow_id: str, execution_id: str) -> str:
        return f"{self.kibana_url}{self.space_prefix}/app/workflows/{workflow_id}/executions/{execution_id}"

    # ════════════════════════════════════════════════════════════════════════
    # SCENARIO 1 — Agent Builder
    # ════════════════════════════════════════════════════════════════════════
    async def converse_stream(
        self,
        user_input: str,
        agent_id: str = "elastic-ai-agent",
        conversation_id: Optional[str] = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """POST /api/agent_builder/converse/async — SSE 스트림을 dict 이벤트로 yield.

        각 이벤트는 {"type": <event_type>, "data": <payload dict>} 형태로 정규화한다.
        주요 type:
          conversation_id_set / conversation_created — conversation_id 확보
          reasoning      — 모델의 사고 단계 (history 표시용)
          tool_call      — 도구 호출 시작 (예: ES|QL 검색)
          tool_progress  — 도구 진행 상황
          tool_result    — 도구 결과
          message_chunk  — 답변 텍스트 부분
          message_complete / round_complete — 완료
        """
        body: dict[str, Any] = {"input": user_input, "agent_id": agent_id}
        if conversation_id:
            body["conversation_id"] = conversation_id

        url = self._api("/api/agent_builder/converse/async")
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            async with client.stream("POST", url, headers=self._headers, json=body) as resp:
                resp.raise_for_status()
                event_type: Optional[str] = None
                async for raw in resp.aiter_lines():
                    line = raw.strip()
                    if not line:
                        event_type = None  # 이벤트 경계
                        continue
                    if line.startswith("event:"):
                        event_type = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        payload = line[len("data:"):].strip()
                        try:
                            obj = json.loads(payload)
                        except json.JSONDecodeError:
                            obj = {"raw": payload}
                        etype = (event_type or (obj.get("type") if isinstance(obj, dict) else None)
                                 or "unknown")
                        # onechat 이벤트는 보통 {"type": ..., "data": {...}} 로 한 단계 감싸진다.
                        inner = obj.get("data", obj) if isinstance(obj, dict) else {"raw": obj}
                        if os.environ.get("DEBUG_SSE"):
                            import json as _j
                            print(f"[sse] {etype}: {_j.dumps(inner, ensure_ascii=False)[:300]}")
                        yield {"type": etype, "data": inner}

    async def list_agents(self) -> list[dict[str, Any]]:
        url = self._api("/api/agent_builder/agents")
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=self._headers)
            r.raise_for_status()
            data = r.json()
            return data.get("results") or data.get("agents") or data if isinstance(data, list) else data.get("results", [])

    async def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        url = self._api(f"/api/agent_builder/conversations/{conversation_id}")
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=self._headers)
            r.raise_for_status()
            return r.json()

    # ════════════════════════════════════════════════════════════════════════
    # SCENARIO 2 — Workflows
    # ════════════════════════════════════════════════════════════════════════
    async def list_workflows(self) -> list[dict[str, Any]]:
        """워크플로우 목록. 경로/응답형태는 스택 버전에 따라 다를 수 있어 방어적으로 파싱."""
        url = self._api("/api/workflows")
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=self._headers)
            r.raise_for_status()
            data = r.json()
        if isinstance(data, list):
            return data
        # 흔한 래핑 키들
        for key in ("results", "workflows", "items", "data"):
            if isinstance(data.get(key), list):
                return data[key]
        return []

    async def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        url = self._api(f"/api/workflows/workflow/{workflow_id}")
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=self._headers)
            r.raise_for_status()
            return r.json()

    @staticmethod
    def extract_inputs(workflow: dict[str, Any]) -> list[dict[str, Any]]:
        """워크플로우 정의에서 inputs 스펙을 뽑아 정규화한다.
        지원하는 형식:
          (a) 리스트 형식: inputs: [ {name, type, required, default, ...}, ... ]
          (b) JSON Schema 형식: inputs: { type: object,
                properties: { <name>: {type, description, default, enum, ...} },
                required: [<name>, ...] }
          (c) dict-of-specs: inputs: { <name>: {type, required, ...} }
        위치는 최상위 inputs 또는 manual trigger 내부 inputs.
        반환: [{name, type, required, default, options, description}, ...]
        """
        definition = workflow.get("definition") or workflow.get("parsed") or {}
        if not definition and isinstance(workflow.get("yaml"), str):
            try:
                definition = yaml.safe_load(workflow["yaml"]) or {}
            except yaml.YAMLError:
                definition = {}
        if not definition and isinstance(workflow.get("yaml"), dict):
            definition = workflow["yaml"]

        # 1) 최상위 inputs → 없으면 2) manual trigger 안의 inputs
        inputs = definition.get("inputs")
        if not inputs:
            for trig in definition.get("triggers", []) or []:
                if isinstance(trig, dict) and trig.get("type") == "manual" and trig.get("inputs"):
                    inputs = trig["inputs"]
                    break
        return ElasticClient._normalize_inputs(inputs)

    @staticmethod
    def _normalize_inputs(inputs: Any) -> list[dict[str, Any]]:
        if not inputs:
            return []
        out: list[dict[str, Any]] = []

        def make(name: str, spec: Any, required: bool) -> dict[str, Any]:
            spec = spec if isinstance(spec, dict) else {}
            options = spec.get("options") or spec.get("enum")
            wtype = spec.get("type", "string")
            if options and wtype not in ("boolean",):
                wtype = "choice"
            return {
                "name": name,
                "type": wtype,
                "required": bool(spec.get("required", required)),
                "default": spec.get("default"),
                "options": options,
                "description": spec.get("description"),
            }

        # (a) 리스트 형식
        if isinstance(inputs, list):
            for item in inputs:
                if isinstance(item, dict) and item.get("name"):
                    out.append(make(item["name"], item, item.get("required", False)))
                elif isinstance(item, str):
                    out.append(make(item, {}, False))
            return out

        # (b)/(c) dict 형식
        if isinstance(inputs, dict):
            props = inputs.get("properties")
            if isinstance(props, dict):  # (b) JSON Schema
                required_set = set(inputs.get("required") or [])
                for name, prop in props.items():
                    out.append(make(name, prop, name in required_set))
            else:  # (c) dict-of-specs
                for name, prop in inputs.items():
                    if isinstance(prop, dict):
                        out.append(make(name, prop, prop.get("required", False)))
        return out

    @staticmethod
    def coerce_input(value: Any, wtype: str) -> Any:
        """Slack 입력(문자열/선택)을 워크플로우 타입에 맞게 변환."""
        if value is None:
            return None
        if wtype in ("number", "integer", "float"):
            try:
                return int(value) if str(value).strip().lstrip("-").isdigit() else float(value)
            except (ValueError, TypeError):
                return value
        if wtype == "boolean":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("true", "1", "yes", "y")
        if wtype == "array":
            if isinstance(value, list):
                return value
            return [v.strip() for v in str(value).split(",") if v.strip()]
        return value

    @staticmethod
    def has_agent_step(workflow: dict[str, Any]) -> bool:
        """워크플로우 안에 ai.agent 스텝이 있는지(=대화 히스토리 링크 노출 후보)."""
        definition = workflow.get("definition") or {}
        if not definition and isinstance(workflow.get("yaml"), str):
            try:
                definition = yaml.safe_load(workflow["yaml"]) or {}
            except yaml.YAMLError:
                definition = {}
        for step in definition.get("steps", []) or []:
            if str(step.get("type", "")).startswith("ai.agent"):
                return True
        return False

    async def run_workflow(self, workflow_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
        """POST /api/workflows/workflow/{id}/run  body: {inputs: {...}}
        보통 executionId 를 즉시 반환하고 실제 실행은 Task Manager 가 비동기로 수행한다."""
        url = self._api(f"/api/workflows/workflow/{workflow_id}/run")
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, headers=self._headers, json={"inputs": inputs})
            r.raise_for_status()
            return r.json()

    async def get_execution(self, execution_id: str) -> dict[str, Any]:
        """실행 상태 조회. 경로는 버전에 따라 다를 수 있으니 두 후보를 순차 시도."""
        candidates = [
            f"/api/workflows/executions/{execution_id}",
            f"/api/workflows/workflowExecution/{execution_id}",
        ]
        async with httpx.AsyncClient(timeout=30.0) as client:
            last_exc: Optional[Exception] = None
            for path in candidates:
                try:
                    r = await client.get(self._api(path), headers=self._headers)
                    if r.status_code == 404:
                        continue
                    r.raise_for_status()
                    return r.json()
                except httpx.HTTPError as e:
                    last_exc = e
            if last_exc:
                raise last_exc
            return {}

    async def get_execution_logs(self, execution_id: str) -> Any:
        """실행 로그(=console step 출력 등) best-effort 조회. 경로/형태는 버전마다 다를 수
        있으므로 여러 후보를 시도하고, 없으면 None 반환."""
        candidates = [
            f"/api/workflows/executions/{execution_id}/logs",
            f"/internal/workflows/executions/{execution_id}/logs",
            f"/api/workflows/executions/{execution_id}/steps",
        ]
        async with httpx.AsyncClient(timeout=30.0) as client:
            for path in candidates:
                try:
                    r = await client.get(self._api(path), headers=self._headers)
                    if r.status_code in (404, 400):
                        continue
                    r.raise_for_status()
                    return r.json()
                except httpx.HTTPError:
                    continue
        return None
