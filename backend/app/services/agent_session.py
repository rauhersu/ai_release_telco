"""Per-connection AI agent session (LangGraph)."""

import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from app.agents.langgraph_assistant import AgentContext, get_agent
from app.db.models.user import User
from app.services.agent import (
    persist_assistant_turn,
    persist_user_turn,
    send_event,
)

logger = logging.getLogger(__name__)


class AgentSession:
    """One WebSocket session with the LangGraph ReAct agent."""

    def __init__(
        self,
        websocket: WebSocket,
        user: User,
    ) -> None:
        self.websocket = websocket
        self.user = user
        self.conversation_history: list[dict[str, str]] = []
        self.context: AgentContext = {}
        self.context["user_id"] = str(user.id) if user else None
        self.context["user_name"] = user.email if user else None
        self.current_conversation_id: str | None = None

    async def process_message(self, data: dict[str, Any]) -> None:
        """Process one user turn: persist input, run the agent, stream events, persist output."""
        user_message = data.get("message", "")
        file_ids = data.get("file_ids", [])

        if not user_message and not file_ids:
            await send_event(self.websocket, "error", {"message": "Empty message"})
            return
        self.current_conversation_id, newly_created, organization_id = await persist_user_turn(
            self.user,
            user_message,
            file_ids,
            requested_conversation_id=data.get("conversation_id"),
            current_conversation_id=self.current_conversation_id,
        )
        if newly_created and self.current_conversation_id:
            await send_event(
                self.websocket,
                "conversation_created",
                {"conversation_id": self.current_conversation_id},
            )

        await send_event(self.websocket, "user_prompt", {"content": user_message})

        try:
            assistant = get_agent(
                model_name=data.get("model"),
                thinking_effort=data.get("thinking_effort"),
            )
            collected_tool_calls: list[dict[str, Any]] = []
            final_output = await self._stream_agent_response(
                assistant, user_message, collected_tool_calls
            )

            if final_output:
                self.conversation_history.append({"role": "user", "content": user_message})
                self.conversation_history.append({"role": "assistant", "content": final_output})
            assistant_msg_id: str | None = None
            if self.current_conversation_id and final_output:
                assistant_msg_id = await persist_assistant_turn(
                    self.current_conversation_id,
                    final_output,
                    getattr(assistant, "model_name", None),
                    collected_tool_calls,
                )

            if assistant_msg_id:
                await send_event(
                    self.websocket,
                    "message_saved",
                    {
                        "message_id": assistant_msg_id,
                        "conversation_id": self.current_conversation_id,
                    },
                )

            await send_event(
                self.websocket,
                "complete",
                {"conversation_id": self.current_conversation_id},
            )
        except WebSocketDisconnect:
            raise
        except Exception as e:
            logger.exception(f"Error processing agent request: {e}")
            await send_event(self.websocket, "error", {"message": str(e)})

    async def _stream_agent_response(
        self,
        assistant: Any,
        user_message: str,
        collected_tool_calls: list[dict[str, Any]],
    ) -> str:
        """Run the LangGraph agent stream and forward all events; return accumulated text."""
        final_output = ""
        seen_tool_call_ids: set[str] = set()
        pending: dict[str, dict[str, Any]] = {}
        self._last_usage_metadata = None
        accumulator: AIMessageChunk | None = None

        await send_event(self.websocket, "model_request_start", {})

        async for stream_mode, data in assistant.stream(
            user_message, history=self.conversation_history, context=self.context
        ):
            if stream_mode == "messages":
                chunk, _metadata = data
                if isinstance(chunk, AIMessageChunk):
                    accumulator = chunk if accumulator is None else accumulator + chunk
                    final_output += await self._stream_message_chunk(chunk, seen_tool_call_ids)
            elif stream_mode == "updates":
                await self._stream_update_event(
                    data, seen_tool_call_ids, pending, collected_tool_calls
                )

        if accumulator is not None:
            self._last_usage_metadata = getattr(accumulator, "usage_metadata", None)
        await send_event(self.websocket, "final_result", {"output": final_output})
        return final_output

    async def _stream_message_chunk(
        self,
        chunk: AIMessageChunk,
        seen_tool_call_ids: set[str],
    ) -> str:
        """Emit text + reasoning deltas + partial tool_call events from a streaming chunk.

        Detects three reasoning shapes:
          * Anthropic extended thinking — content blocks ``{"type":"thinking","thinking":"..."}``
          * OpenAI Responses API — ``{"type":"reasoning","summary":[{"type":"summary_text","text":"..."}]}``
          * Legacy LangChain providers — ``additional_kwargs.reasoning_content`` (string)
        """
        text_content = ""
        reasoning_content = ""
        if chunk.content:
            if isinstance(chunk.content, str):
                text_content = chunk.content
            elif isinstance(chunk.content, list):
                for block in chunk.content:
                    if isinstance(block, str):
                        text_content += block
                    elif isinstance(block, dict):
                        block_type = block.get("type")
                        if block_type == "text":
                            text_content += block.get("text", "")
                        elif block_type == "thinking":
                            reasoning_content += block.get("thinking", "") or ""
                        elif block_type == "reasoning":
                            for summary in block.get("summary", []) or []:
                                if (
                                    isinstance(summary, dict)
                                    and summary.get("type") == "summary_text"
                                ):
                                    reasoning_content += summary.get("text", "") or ""
            if text_content:
                await send_event(self.websocket, "text_delta", {"content": text_content})
        # Legacy shape: some providers stash the chain-of-thought outside content.
        legacy_reasoning = (chunk.additional_kwargs or {}).get("reasoning_content")
        if isinstance(legacy_reasoning, str) and legacy_reasoning:
            reasoning_content += legacy_reasoning
        if reasoning_content:
            await send_event(self.websocket, "thinking_delta", {"content": reasoning_content})

        if chunk.tool_call_chunks:
            for tc_chunk in chunk.tool_call_chunks:
                tc_id = tc_chunk.get("id")
                tc_name = tc_chunk.get("name")
                if tc_id and tc_name and tc_id not in seen_tool_call_ids:
                    seen_tool_call_ids.add(tc_id)
                    await send_event(
                        self.websocket,
                        "tool_call",
                        {"tool_name": tc_name, "args": {}, "tool_call_id": tc_id},
                    )
        return text_content

    async def _stream_update_event(
        self,
        update_data: dict[str, Any],
        seen_tool_call_ids: set[str],
        pending: dict[str, dict[str, Any]],
        collected_tool_calls: list[dict[str, Any]],
    ) -> None:
        """Process LangGraph ``updates`` events: tool results + canonical tool calls."""
        for node_name, update in update_data.items():
            if node_name == "tools":
                for msg in update.get("messages", []):
                    if isinstance(msg, ToolMessage):
                        tc = pending.get(msg.tool_call_id)
                        if tc is not None:
                            tc["result"] = str(msg.content)
                        await send_event(
                            self.websocket,
                            "tool_result",
                            {"tool_call_id": msg.tool_call_id, "content": msg.content},
                        )
            elif node_name == "agent":
                for msg in update.get("messages", []):
                    if isinstance(msg, AIMessage) and msg.tool_calls:
                        for tc_in in msg.tool_calls:
                            tc_id = tc_in.get("id", "")
                            if not tc_id:
                                continue
                            tc = {
                                "tool_call_id": tc_id,
                                "tool_name": tc_in.get("name", ""),
                                "args": tc_in.get("args", {}),
                            }
                            pending[tc_id] = tc
                            collected_tool_calls.append(tc)
                            if tc_id not in seen_tool_call_ids:
                                seen_tool_call_ids.add(tc_id)
                                await send_event(self.websocket, "tool_call", tc)
