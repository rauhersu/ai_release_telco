"""Framework-agnostic agent invocation for channel messages (non-streaming)."""

import logging
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolEvent:
    """A tool call + result pair collected during agent execution."""

    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    result: str = ""


from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

logger = logging.getLogger(__name__)


def _provider_from_model(model_name: str) -> str:
    name = (model_name or "").lower()
    if name.startswith(("gpt", "o1", "o3", "o4", "openai")):
        return "openai"
    if name.startswith(("claude", "anthropic")):
        return "anthropic"
    if name.startswith(("gemini", "google")):
        return "google"
    return "unknown"


class AgentInvocationService:
    """Invoke the configured AI agent and return the final text response.

    Used by channel adapters where streaming is not required. Both the user
    message and the assistant reply are persisted to the database.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def invoke(
        self,
        *,
        user_message: str,
        conversation_id: UUID,
        user_id: UUID | None = None,
        project_id: UUID | None = None,
        system_prompt_override: str | None = None,
        model_override: str | None = None,
    ) -> tuple[str, list[ToolEvent]]:
        """Run the agent and return final text + tool events.

        Returns:
            Tuple of (response_text, tool_events).
        """
        # 1. Persist user message
        await self._persist_user_message(conversation_id, user_message)

        # 2. Load history (excluding the message we just added to avoid duplication)
        history = await self._load_history(conversation_id)

        # 3. Call agent
        tool_events: list[ToolEvent] = []
        try:
            response_text, tool_events = await self._call_agent(
                user_message=user_message,
                history=history,
                conversation_id=conversation_id,
                user_id=user_id,
                project_id=project_id,
                system_prompt_override=system_prompt_override,
                model_override=model_override,
            )
        except Exception as exc:
            logger.exception("Agent invocation failed: %s", exc)
            response_text = "Sorry, I encountered an error processing your request."

        # 4. Persist assistant message
        await self._persist_assistant_message(conversation_id, response_text)

        return response_text, tool_events

    # Framework-specific agent calls

    async def _call_agent(
        self,
        *,
        user_message: str,
        history: list[dict[str, str]],
        **kwargs: Any,
    ) -> tuple[str, list[ToolEvent]]:
        """Dispatch to the framework-specific agent implementation."""
        return await self._call_langgraph(user_message=user_message, history=history, **kwargs)

    async def _call_langgraph(
        self,
        *,
        user_message: str,
        history: list[dict[str, str]],
        **kwargs: Any,
    ) -> tuple[str, list[ToolEvent]]:
        """Invoke LangGraph agent (async)."""
        from langchain_core.messages import AIMessage, HumanMessage

        from app.agents.langgraph_assistant import get_agent

        assistant = get_agent()
        lc_history = self._build_langchain_history(history)
        lc_history.append(HumanMessage(content=user_message))

        result = await assistant.graph.ainvoke({"messages": lc_history})

        for msg in reversed(result.get("messages", [])):
            if isinstance(msg, AIMessage):
                content = msg.content
                return (content if isinstance(content, str) else str(content)), []
        return "", []

    def _build_langchain_history(self, history: list[dict[str, str]]) -> list[Any]:
        """Convert conversation history to LangChain message format."""
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        lc_msgs: list[Any] = []
        for msg in history:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                lc_msgs.append(HumanMessage(content=content))
            elif role == "assistant":
                lc_msgs.append(AIMessage(content=content))
            elif role == "system":
                lc_msgs.append(SystemMessage(content=content))
        return lc_msgs

    # Persistence helpers
    async def _persist_user_message(self, conversation_id: UUID, content: str) -> None:
        """Persist the user message directly via conversation repo."""
        from app.repositories import conversation_repo

        await conversation_repo.create_message(
            self.db,
            conversation_id=conversation_id,
            role="user",
            content=content,
        )

    async def _persist_assistant_message(self, conversation_id: UUID, content: str) -> None:
        """Persist the assistant reply directly via conversation repo."""
        from app.repositories import conversation_repo

        await conversation_repo.create_message(
            self.db,
            conversation_id=conversation_id,
            role="assistant",
            content=content,
            model_name=settings.AI_MODEL,
        )

    async def _load_history(self, conversation_id: UUID) -> list[dict[str, str]]:
        """Load conversation message history ordered chronologically."""
        from app.repositories import conversation_repo

        messages = await conversation_repo.get_messages_by_conversation(
            self.db,
            conversation_id=conversation_id,
            skip=0,
            limit=200,
        )
        return [{"role": m.role, "content": m.content} for m in messages]
