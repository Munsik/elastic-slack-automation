"""
app.py — Slack Bolt(Socket Mode) 봇. Kibana 를 거의 열지 않고 Slack 안에서
Agent Builder 대화(시나리오 1)와 Workflows 실행(시나리오 2)을 모두 수행한다.

왜 이 봇(thin layer)이 필요한가?
- Slack 의 Events / Slash command / Interactivity 는 (1) 서명 검증, (2) 3초 내 ACK,
  (3) Block Kit JSON 응답, (4) modal(views.open) 트리거를 요구한다.
  Kibana/Elasticsearch 는 Slack 의 이 프로토콜을 네이티브로 처리하지 못한다.
  따라서 Slack <-> ES 직결은 '인터랙티브' 시나리오에서는 불가능하고,
  가장 단순한 형태가 바로 이 단일 프로세스 Bolt 앱이다(별도 DB/queue/공인 URL 불필요).
- Socket Mode 를 쓰면 공인 IP / 리버스 프록시 / TLS 인증서가 전혀 필요 없다.
  봇 -> Slack 아웃바운드 WebSocket, 봇 -> Kibana 아웃바운드 HTTPS 만 있으면 된다.

실행: python app.py   (환경변수는 .env 참고)
"""

from __future__ import annotations

import asyncio
import os
import re
import ssl
from typing import Any

import certifi

# ── macOS / python.org 빌드의 CA 인증서 문제 해결 ────────────────────────────
# python.org Python(특히 3.13/3.14)은 시스템 키체인을 쓰지 않아 기본 CA 번들이
# 비어 있을 수 있다(= SSL: CERTIFICATE_VERIFY_FAILED). certifi 번들을 OpenSSL
# 기본 검증 경로로 지정해 두면 aiohttp(Slack WS/Web API)·httpx(Kibana) 모두
# 정상 검증된다. 기존에 잘못된 값이 있을 수 있으므로 setdefault 가 아니라 무조건 덮어쓴다.
os.environ["SSL_CERT_FILE"] = certifi.where()
print(f"[startup] CA bundle in use: {certifi.where()}")

from dotenv import load_dotenv
from slack_bolt.app.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

from elastic import ElasticClient

load_dotenv()

KIBANA_URL = os.environ["KIBANA_URL"]
KIBANA_API_KEY = os.environ["KIBANA_API_KEY"]
KIBANA_SPACE = os.environ.get("KIBANA_SPACE", "default")
DEFAULT_AGENT_ID = os.environ.get("DEFAULT_AGENT_ID", "elastic-ai-agent")

es = ElasticClient(KIBANA_URL, KIBANA_API_KEY, KIBANA_SPACE)

# certifi 기반 SSL 컨텍스트를 Slack 클라이언트에 명시적으로 주입(이중 안전장치).
# 이 client 는 SocketModeHandler 가 apps.connections.open(=에러 났던 호출)과
# 이후 chat.postMessage 등에 그대로 사용한다.
_ssl_context = ssl.create_default_context(cafile=certifi.where())
app = AsyncApp(client=AsyncWebClient(token=os.environ["SLACK_BOT_TOKEN"], ssl=_ssl_context))

# 텍스트 진행감을 주기 위한 braille 스피너 프레임
SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# Slack chat.update rate limit(약 1초/채널) 대응 throttle
UPDATE_INTERVAL = 1.1


# ════════════════════════════════════════════════════════════════════════════
# SCENARIO 1 — @멘션/스레드 답글 -> Agent Builder 대화 (멀티턴 + 스트리밍 + 딥링크)
# ════════════════════════════════════════════════════════════════════════════
# 스레드 root ts -> Agent Builder conversation_id. 같은 스레드의 후속 질문을
# 같은 대화로 이어가기 위한 매핑. (데모는 in-memory; 운영은 Redis 등으로 외부화)
THREAD_CONV: dict[str, str] = {}
# 봇 자신의 user id (멘션 중복 처리 방지용). main() 에서 auth_test 로 채운다.
BOT_USER_ID: str | None = None


async def run_agent_turn(client, channel: str, thread_ts: str, text: str, logger):
    """한 번의 질문-답변 턴. 같은 thread_ts 면 기존 conversation_id 로 대화를 이어간다."""
    placeholder = await client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f"{SPINNER[0]} Agent가 생각하는 중…",
    )
    ts = placeholder["ts"]

    conversation_id: str | None = THREAD_CONV.get(thread_ts)  # 있으면 이어가기
    answer_parts: list[str] = []
    final_message: str | None = None
    steps: list[str] = []
    current_status = "생각하는 중"
    frame = 0
    last_update = 0.0

    def progress_blocks() -> list[dict[str, Any]]:
        header = f"{SPINNER[frame]} *{current_status}…*"
        if steps:
            header += "\n" + "\n".join(f"› {s}" for s in steps[-5:])
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        ]
        partial = "".join(answer_parts).strip()
        if partial:
            blocks.append({"type": "markdown", "text": (partial[:11000] + " ▌")})
        return blocks

    async def push_update(force: bool = False):
        nonlocal frame, last_update
        frame = (frame + 1) % len(SPINNER)
        now = asyncio.get_event_loop().time()
        if not force and (now - last_update) < UPDATE_INTERVAL:
            return
        last_update = now
        try:
            await client.chat_update(
                channel=channel, ts=ts, text=f"{current_status}…", blocks=progress_blocks(),
            )
        except Exception:  # noqa: BLE001
            pass

    try:
        async for ev in es.converse_stream(
            text, agent_id=DEFAULT_AGENT_ID, conversation_id=conversation_id,
        ):
            etype, data = ev["type"], ev["data"]
            if not isinstance(data, dict):
                data = {"value": data}

            if etype in ("conversation_id_set", "conversation_created", "conversation_updated"):
                conversation_id = (
                    data.get("conversation_id") or data.get("conversationId")
                    or data.get("id") or conversation_id
                )
            elif etype == "reasoning":
                txt = data.get("reasoning") or data.get("text") or data.get("content")
                if txt:
                    current_status = "추론 중"
                    steps.append(f"🧠 {str(txt)[:200]}")
                    await push_update()
            elif etype == "tool_call":
                tool = (data.get("tool_id") or data.get("toolId") or data.get("tool")
                        or data.get("name") or "tool")
                current_status = f"도구 실행: {tool}"
                steps.append(f"🔧 `{tool}` 호출")
                await push_update(force=True)
            elif etype == "tool_progress":
                msg = data.get("message") or data.get("progress") or data.get("text")
                if msg:
                    steps.append(f"⏳ {str(msg)[:200]}")
                    await push_update()
            elif etype == "tool_result":
                tool = data.get("tool_id") or data.get("toolId") or "tool"
                steps.append(f"✅ `{tool}` 결과 수신")
                current_status = "결과 정리 중"
                await push_update(force=True)
            elif etype == "message_chunk":
                chunk = (data.get("text_chunk") or data.get("text")
                         or data.get("delta") or data.get("content") or "")
                if chunk:
                    answer_parts.append(chunk)
                    current_status = "답변 작성 중"
                    await push_update()
            elif etype in ("message_complete", "round_complete"):
                final_message = (data.get("message_content") or data.get("content")
                                 or data.get("text") or data.get("message") or final_message)
                if final_message:
                    current_status = "마무리"
                    await push_update(force=True)

    except Exception as e:  # noqa: BLE001
        logger.exception("converse stream failed")
        await client.chat_update(
            channel=channel, ts=ts,
            text=f":warning: Agent 호출 중 오류가 발생했습니다: `{e}`",
        )
        return

    # 다음 후속 질문이 이 대화를 이어가도록 매핑 저장
    if conversation_id:
        THREAD_CONV[thread_ts] = conversation_id

    # 최종 답변 확정: 완성본 > 스트리밍 누적 > 대화 재조회(Kibana엔 분명히 있음)
    answer = (final_message or "".join(answer_parts)).strip()
    if not answer and conversation_id:
        try:
            conv = await es.get_conversation(conversation_id)
            answer = _extract_conversation_answer(conv)
        except Exception:  # noqa: BLE001
            logger.exception("conversation fallback failed")
    answer = answer or "_빈 응답을 받았습니다._"
    # 답변 본문은 표준 Markdown → markdown 블록(변환 금지). text 는 알림용 폴백.
    blocks: list[dict[str, Any]] = [
        {"type": "markdown", "text": answer[:11900]},
    ]
    if steps:
        history = "\n".join(steps[-8:])
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"*추론/도구 히스토리*\n{history}"}],
        })
    if conversation_id:
        blocks.append({
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "🔗 Kibana 대화에서 열기"},
                "url": es.conversation_url(conversation_id),
                "action_id": "open_kibana_conversation",  # url 버튼이라 핸들러 불필요
            }],
        })
    await client.chat_update(channel=channel, ts=ts, text=answer[:3000], blocks=blocks)


@app.event("app_mention")
async def handle_mention(event, client, logger):
    if event.get("bot_id"):  # 봇/앱이 보낸 멘션은 무시
        return
    text = re.sub(r"<@[^>]+>", "", event.get("text", "")).strip()
    channel = event["channel"]
    # 스레드 안에서 멘션되면 그 스레드, 아니면 멘션된 메시지를 부모로 스레드 시작
    thread_ts = event.get("thread_ts") or event["ts"]

    # 시나리오 2 분기: "workflow / 워크플로우" 키워드면 워크플로우 목록을 보여준다
    if re.search(r"workflow|워크플로우|워크플로", text, re.I):
        await show_workflow_list(client, channel, thread_ts)
        return

    if not text:
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="무엇을 도와드릴까요? 질문을 함께 멘션해 주세요. "
                 "(워크플로우를 보려면 'workflow' 라고 적어주세요)",
        )
        return

    await run_agent_turn(client, channel, thread_ts, text, logger)


@app.event("message")
async def handle_message(event, client, logger):
    """스레드 답글로 들어오는 후속 질문 처리(재멘션 없이 자연어로 이어가기).
    우리가 시작한 Agent 대화 스레드(THREAD_CONV 에 있는)에서만 동작한다."""
    # 봇/시스템 메시지, 편집·삭제 등 subtype 메시지는 무시
    if event.get("bot_id") or event.get("subtype"):
        return
    thread_ts = event.get("thread_ts")
    channel = event.get("channel")
    raw = event.get("text") or ""
    # (1) 스레드 답글이어야 하고 (2) 우리가 추적 중인 Agent 대화 스레드여야 함
    if not thread_ts or thread_ts not in THREAD_CONV:
        return
    # 멘션이 포함된 답글은 app_mention 이 처리 → 여기서는 건너뛰어 중복 방지
    if BOT_USER_ID and f"<@{BOT_USER_ID}>" in raw:
        return
    text = re.sub(r"<@[^>]+>", "", raw).strip()
    if not text:
        return
    await run_agent_turn(client, channel, thread_ts, text, logger)


# ════════════════════════════════════════════════════════════════════════════
# SCENARIO 2 — 워크플로우 목록 -> 선택 -> 변수 입력(modal) -> 실행 -> 스레드 결과
# ════════════════════════════════════════════════════════════════════════════
async def show_workflow_list(client, channel, thread_ts):
    try:
        workflows = await es.list_workflows()
    except Exception as e:  # noqa: BLE001
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f":warning: 워크플로우 목록을 불러오지 못했습니다: `{e}`",
        )
        return

    if not workflows:
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text="실행 가능한 워크플로우가 없습니다.",
        )
        return

    options = []
    for w in workflows[:100]:  # static_select 최대 100개
        wid = w.get("id") or w.get("workflow_id")
        name = w.get("name") or wid
        desc = (w.get("description") or "")[:75]
        options.append({
            "text": {"type": "plain_text", "text": str(name)[:75]},
            "description": {"type": "plain_text", "text": desc} if desc else None,
            "value": str(wid),
        })
        if options[-1]["description"] is None:
            options[-1].pop("description")

    await client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text="실행할 워크플로우를 선택하세요.",
        blocks=[{
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*실행할 워크플로우를 선택하세요* :gear:"},
            "accessory": {
                "type": "static_select",
                "placeholder": {"type": "plain_text", "text": "워크플로우 선택"},
                "options": options,
                "action_id": "select_workflow",
            },
        }],
    )


@app.action("select_workflow")
async def on_select_workflow(ack, body, client, logger):
    await ack()
    workflow_id = body["actions"][0]["selected_option"]["value"]
    channel = body["channel"]["id"]
    # 결과를 되돌릴 스레드 ts (목록 메시지가 스레드 안에 있었으므로 thread_ts 보존)
    msg = body.get("message", {})
    thread_ts = msg.get("thread_ts") or msg.get("ts")
    trigger_id = body["trigger_id"]

    try:
        wf = await es.get_workflow(workflow_id)
        input_specs = ElasticClient.extract_inputs(wf)
    except Exception as e:  # noqa: BLE001
        logger.exception("get_workflow failed")
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                      text=f":warning: 워크플로우 정보를 읽지 못했습니다: `{e}`")
        return

    # 변수 입력 modal 구성
    blocks: list[dict[str, Any]] = []
    if not input_specs:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": "이 워크플로우는 입력 변수가 없습니다. 바로 실행합니다."}})
    for spec in input_specs:
        if not isinstance(spec, dict) or not spec.get("name"):
            continue  # 정규화 후에도 이상한 항목은 건너뜀
        name = spec["name"]
        required = bool(spec.get("required", False))
        wtype = spec.get("type", "string")
        label = f"{name}" + ("" if required else " (선택)")
        hint = spec.get("description")
        element: dict[str, Any]
        if wtype == "boolean":
            element = {"type": "static_select", "action_id": "v",
                       "options": [
                           {"text": {"type": "plain_text", "text": "true"}, "value": "true"},
                           {"text": {"type": "plain_text", "text": "false"}, "value": "false"},
                       ]}
        elif wtype == "choice" and spec.get("options"):
            element = {"type": "static_select", "action_id": "v",
                       "options": [{"text": {"type": "plain_text", "text": str(o)[:75]},
                                    "value": str(o)}
                                   for o in spec["options"]]}
        else:  # string / number / array -> plain_text_input
            element = {"type": "plain_text_input", "action_id": "v"}
            if spec.get("default") is not None:
                element["initial_value"] = str(spec["default"])
        block = {
            "type": "input",
            "block_id": f"in_{name}",
            "label": {"type": "plain_text", "text": label[:75]},
            "element": element,
            "optional": not required,
        }
        if hint:
            block["hint"] = {"type": "plain_text", "text": str(hint)[:150]}
        blocks.append(block)

    await client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": "run_workflow_modal",
            # 실행 후 결과를 어디로 보낼지 + 어떤 워크플로우인지 보존
            "private_metadata": f"{channel}|{thread_ts}|{workflow_id}",
            "title": {"type": "plain_text", "text": "워크플로우 실행"},
            "submit": {"type": "plain_text", "text": "실행"},
            "close": {"type": "plain_text", "text": "취소"},
            "blocks": blocks or [{"type": "section",
                                  "text": {"type": "mrkdwn", "text": "실행 준비 완료"}}],
        },
    )


@app.view("run_workflow_modal")
async def on_modal_submit(ack, body, client, view, logger):
    await ack()  # modal 닫기
    channel, thread_ts, workflow_id = view["private_metadata"].split("|", 2)

    # 입력 타입을 알기 위해 워크플로우 스펙을 다시 읽어 name -> type 맵 구성
    type_map: dict[str, str] = {}
    try:
        wf_spec = await es.get_workflow(workflow_id)
        for s in ElasticClient.extract_inputs(wf_spec):
            if isinstance(s, dict) and s.get("name"):
                type_map[s["name"]] = s.get("type", "string")
    except Exception:  # noqa: BLE001
        pass

    # 입력값 수집 + 타입 변환
    inputs: dict[str, Any] = {}
    for block_id, payload in view["state"]["values"].items():
        if not block_id.startswith("in_"):
            continue
        name = block_id[3:]
        val = payload["v"]
        wtype = type_map.get(name, "string")
        if val.get("type") == "plain_text_input":
            text = (val.get("value") or "").strip()
            if text != "":
                inputs[name] = ElasticClient.coerce_input(text, wtype)
        else:  # static_select
            sel = val.get("selected_option")
            if sel:
                inputs[name] = ElasticClient.coerce_input(sel["value"], wtype)

    # 진행 메시지
    progress = await client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text=f"{SPINNER[0]} 워크플로우 `{workflow_id}` 실행 중…",
    )
    ts = progress["ts"]

    try:
        run = await es.run_workflow(workflow_id, inputs)
    except Exception as e:  # noqa: BLE001
        logger.exception("run_workflow failed")
        await client.chat_update(channel=channel, ts=ts,
                                 text=f":warning: 실행 요청 실패: `{e}`")
        return

    execution_id = (run.get("workflowExecutionId") or run.get("executionId")
                    or run.get("execution_id") or run.get("id"))

    # 비동기 실행이면 상태를 폴링하며 진행감 유지
    outputs: Any = run.get("outputs") or run.get("output")
    status = run.get("status")
    last_ex: dict[str, Any] = run if isinstance(run, dict) else {}
    if execution_id and not outputs:
        for i in range(40):  # 최대 ~40초
            await asyncio.sleep(1.0)
            frame = SPINNER[i % len(SPINNER)]
            try:
                ex = await es.get_execution(execution_id)
                last_ex = ex or last_ex
            except Exception:  # noqa: BLE001
                ex = {}
            status = (ex.get("status") or "").upper()
            await client.chat_update(channel=channel, ts=ts,
                                     text=f"{frame} 실행 중… (status: {status or 'RUNNING'})")
            if status in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
                outputs = ex.get("outputs") or ex.get("output") or ex.get("result")
                break

    # 결과 본문 만들기:
    #  1) 워크플로우 outputs (드묾) → 2) step 출력/console 로그 → 3) 안내
    result_text = _stringify(outputs).strip() if outputs else ""
    if not result_text and execution_id:
        try:
            logs = await es.get_execution_logs(execution_id)
        except Exception:  # noqa: BLE001
            logs = None
        if os.environ.get("DEBUG_WF"):  # 실제 실행/로그 JSON 구조 확인용
            import json as _j
            print("[wf] execution=", _j.dumps(last_ex, ensure_ascii=False)[:2000])
            print("[wf] logs=", _j.dumps(logs, ensure_ascii=False)[:2000] if logs else None)
        result_text = _summarize_execution(last_ex, logs)
    if not result_text:
        result_text = (f"실행 상태: {status or 'submitted'}\n"
                       "_이 워크플로우는 console 출력/인덱스 저장으로 종료된 것 같습니다. "
                       "내용은 'Kibana 실행 보기'에서 확인하거나, 끝에 요약용 console step 을 두세요._")

    # 결과 메시지 + 딥링크들
    blocks: list[dict[str, Any]] = [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*워크플로우 실행 결과* (`{workflow_id}`)"}},
        # 결과 본문은 표준 Markdown 그대로 렌더링.
        {"type": "markdown", "text": result_text[:11900]},
        {"type": "actions", "elements": []},
    ]
    if execution_id:
        blocks[-1]["elements"].append({
            "type": "button",
            "text": {"type": "plain_text", "text": "🔗 Kibana 실행 보기"},
            "url": es.execution_url(workflow_id, execution_id),
            "action_id": "open_execution",
        })
    # ai.agent 스텝이 있으면 Agent 대화 히스토리 링크도 노출(가능 시)
    try:
        wf = await es.get_workflow(workflow_id)
        if ElasticClient.has_agent_step(wf):
            conv_id = _find_conversation_id(outputs)
            if conv_id:
                blocks[-1]["elements"].append({
                    "type": "button",
                    "text": {"type": "plain_text", "text": "💬 Agent 대화 히스토리"},
                    "url": es.conversation_url(conv_id),
                    "action_id": "open_agent_conv",
                })
    except Exception:  # noqa: BLE001
        pass
    if not blocks[-1]["elements"]:
        blocks.pop()  # 버튼 없으면 actions 블록 제거

    await client.chat_update(channel=channel, ts=ts, text=result_text[:2900], blocks=blocks)


# url 버튼 클릭은 별도 처리 불필요하지만, Slack 의 'unhandled action' 경고 방지용 ack
@app.action(re.compile("open_.*"))
async def _ack_link_buttons(ack):
    await ack()


# ── 유틸 ────────────────────────────────────────────────────────────────────
_ANSWER_KEYS = ("message_content", "message", "content", "text")


def _extract_conversation_answer(conv: Any) -> str:
    """대화 객체에서 마지막 assistant 답변 텍스트를 뽑아낸다(스트림이 비었을 때의 폴백).
    onechat 구조(rounds[-1].response.message)를 우선 시도하고, 안 되면 깊은 탐색."""
    container = conv.get("conversation", conv) if isinstance(conv, dict) else {}
    rounds = container.get("rounds") if isinstance(container, dict) else None
    if isinstance(rounds, list) and rounds:
        resp = rounds[-1].get("response") if isinstance(rounds[-1], dict) else None
        if isinstance(resp, dict):
            for k in _ANSWER_KEYS:
                v = resp.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            if isinstance(resp.get("message"), dict):
                for k in _ANSWER_KEYS:
                    v = resp["message"].get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
    # 깊은 탐색: _ANSWER_KEYS 에 해당하는 마지막 문자열
    found: list[str] = []

    def walk(o: Any):
        if isinstance(o, dict):
            for k, val in o.items():
                if k in _ANSWER_KEYS and isinstance(val, str) and val.strip():
                    found.append(val.strip())
                else:
                    walk(val)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    walk(container)
    return found[-1] if found else ""


# 로그 배열이 담길 수 있는 키들
_LOG_LIST_KEYS = ("logs", "items", "results", "hits", "data")
# 엔진 lifecycle/디버그 로그(=console 출력이 아님)를 걸러내기 위한 신호
_ENGINE_ACTIONS = {
    "workflow-complete", "workflow-start", "workflow-failed", "workflow-cancelled",
    "step-start", "step-complete", "step-failed", "debug", "scheduled", "scheduling",
}
_ENGINE_NOISE = re.compile(
    r"(workflow execution (completed|started|failed|finished|cancelled)"
    r"|task transaction|task manager|starting (workflow|step)"
    r"|step .*(started|completed|finished|failed)|executing step|scheduling)",
    re.I,
)


def _summarize_execution(execution: Any, logs: Any = None) -> str:
    """실행 로그에서 console step 의 '렌더된' 메시지만 시간순으로 뽑아 보여준다.
    엔진 lifecycle/디버그 로그는 제외한다. (execution 객체엔 런타임 step 출력이 없고
    workflowDefinition 템플릿만 있으므로 거기서는 본문을 만들지 않는다.)"""
    arr: Any = logs
    if isinstance(logs, dict):
        for k in _LOG_LIST_KEYS:
            if isinstance(logs.get(k), list):
                arr = logs[k]
                break
    if not isinstance(arr, list):
        return ""

    rows: list[tuple[str, str, Any]] = []
    for it in arr:
        if isinstance(it, str):
            if it.strip():
                rows.append(("", it.strip(), None))
            continue
        if not isinstance(it, dict):
            continue
        msg = it.get("message")
        if not isinstance(msg, str) or not msg.strip():
            continue
        if it.get("level") == "debug":
            continue
        ad = it.get("additionalData") or {}
        ev = ad.get("event") or {} if isinstance(ad, dict) else {}
        action = ev.get("action") if isinstance(ev, dict) else None
        step_ref = None
        if isinstance(ad, dict):
            step_ref = (ad.get("stepName") or ad.get("stepId")
                        or ad.get("step") or ad.get("stepType"))
        # step 참조가 없는 엔진 로그는 제외
        if not step_ref and (action in _ENGINE_ACTIONS or _ENGINE_NOISE.search(msg)):
            continue
        rows.append((it.get("timestamp", ""), msg.strip(), step_ref))

    rows.sort(key=lambda r: r[0])  # 오래된 → 최신 (실행 순서)

    out: list[str] = []
    seen: set[tuple] = set()
    for _, msg, ref in rows:
        key = (ref, msg)
        if key in seen:
            continue
        seen.add(key)
        out.append(f"*{ref}*\n{msg}" if ref else msg)
    return "\n\n".join(out[:30]).strip()


def _stringify(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    import json as _json
    try:
        return "```" + _json.dumps(v, ensure_ascii=False, indent=2)[:2800] + "```"
    except Exception:  # noqa: BLE001
        return str(v)


def _find_conversation_id(obj: Any) -> str | None:
    """워크플로우 outputs 안에서 agent conversation_id 비슷한 키를 탐색."""
    if isinstance(obj, dict):
        for k, val in obj.items():
            if k.lower() in ("conversation_id", "conversationid") and isinstance(val, str):
                return val
            found = _find_conversation_id(val)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_conversation_id(item)
            if found:
                return found
    return None


async def main():
    global BOT_USER_ID
    try:
        auth = await app.client.auth_test()
        BOT_USER_ID = auth.get("user_id")
        print(f"[startup] bot user id: {BOT_USER_ID}")
    except Exception as e:  # noqa: BLE001
        print(f"[startup] auth_test 실패(멘션 중복 방지에만 영향): {e}")
    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
