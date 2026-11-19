"""A deterministic model factory for real-Harness local application tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from octop_harness.config import ModelConfig, ProviderConfig
from octop_harness.llm.factory import ChatModelFactory
from pydantic import PrivateAttr


class LocalModel(BaseChatModel):
    _started: asyncio.Event = PrivateAttr(default_factory=asyncio.Event)

    @property
    def _llm_type(self) -> str:
        return "local-application-test"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> LocalModel:
        return self

    def _reply(self, messages: list[BaseMessage]) -> AIMessage:
        if isinstance(messages[-1], ToolMessage):
            return AIMessage(content=f"Tool completed: {messages[-1].content}")
        if "write" in str(messages[-1].content):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "write-1",
                        "name": "write_file",
                        "args": {"file_path": "result.txt", "content": "saved-by-tool"},
                    }
                ],
            )
        return AIMessage(
            content="",
            tool_calls=[{"id": "read-1", "name": "read_file", "args": {"file_path": "note.txt"}}],
        )

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None, **kwargs: Any
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._reply(messages))])

    async def _astream(
        self, messages: list[BaseMessage], stop: list[str] | None = None, **kwargs: Any
    ) -> AsyncIterator[ChatGenerationChunk]:
        if isinstance(messages[-1], HumanMessage) and "wait" in str(messages[-1].content):
            self._started.set()
            await asyncio.Future()
        message = self._reply(messages)
        yield ChatGenerationChunk(
            message=AIMessageChunk(content=message.content, tool_calls=message.tool_calls)
        )


class LocalModelFactory(ChatModelFactory):
    def __init__(self) -> None:
        super().__init__(
            [
                ProviderConfig(
                    id="platform",
                    base_url="https://test.invalid",
                    api_key="never-sent",
                    models=[ModelConfig(id="test")],
                )
            ]
        )
        self.model = LocalModel()

    def get_chat_model(self, model_ref: str, *, max_retries: int | None = None) -> BaseChatModel:
        return self.model
