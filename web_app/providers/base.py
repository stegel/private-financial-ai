"""
Base classes for LLM providers.
All providers implement this interface for consistent behavior.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Generator
import json


@dataclass
class LLMResponse:
    """Standardized response from any LLM provider."""
    content: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""
    raw_response: Any = None


@dataclass
class ToolCall:
    """Represents a tool call from the LLM."""
    id: str
    name: str
    arguments: Dict[str, Any]


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    def chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        system: Optional[str] = None,
        stream: bool = False,
        **kwargs
    ) -> LLMResponse | Generator:
        """
        Send a chat request to the LLM.

        Args:
            messages: List of message dicts with 'role' and 'content'
            tools: Optional list of tool definitions
            system: Optional system prompt
            stream: If True, return a generator yielding chunks
            **kwargs: Provider-specific options

        Returns:
            LLMResponse or Generator if streaming
        """
        pass

    @abstractmethod
    def supports_tools(self) -> bool:
        """Whether this provider supports tool/function calling."""
        pass

    @abstractmethod
    def get_model_for_tier(self, tier: str) -> str:
        """
        Get the model name for a complexity tier.

        Args:
            tier: 'simple', 'moderate', or 'complex'

        Returns:
            Model identifier string
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this provider is properly configured and available."""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name for logging and display."""
        pass

    def calculate_cost(self, tokens_in: int, tokens_out: int, model: str) -> float:
        """
        Calculate cost for a request. Override in subclasses with actual pricing.

        Args:
            tokens_in: Input token count
            tokens_out: Output token count
            model: Model identifier

        Returns:
            Cost in USD
        """
        return 0.0

    def format_tool_result(self, tool_call_id: str, result: Any) -> Dict:
        """
        Format a tool result for sending back to the LLM.
        Override if provider needs different format.
        """
        if isinstance(result, dict):
            content = json.dumps(result)
        else:
            content = str(result)

        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content
        }

    def format_assistant_message(self, response: 'LLMResponse') -> Dict:
        """
        Format the assistant turn (including tool calls) for the next request.
        Override in providers that use a different message format.
        """
        return {
            "role": "assistant",
            "content": response.content,
            "tool_calls": response.tool_calls
        }
