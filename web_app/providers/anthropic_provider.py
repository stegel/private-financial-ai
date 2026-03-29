"""
Anthropic (Claude) provider implementation.
"""

import os
import json
from typing import List, Dict, Any, Optional, Generator
from .base import LLMProvider, LLMResponse, ToolCall


class AnthropicProvider(LLMProvider):
    """Provider for Anthropic's Claude models."""

    # Pricing per million tokens (as of 2025)
    PRICING = {
        "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
        "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
        "claude-opus-4-6": {"input": 15.0, "output": 75.0},
        # Aliases
        "haiku": {"input": 1.0, "output": 5.0},
        "sonnet": {"input": 3.0, "output": 15.0},
        "opus": {"input": 15.0, "output": 75.0},
    }

    # Model tiers
    DEFAULT_MODELS = {
        "simple": "claude-haiku-4-5-20251001",
        "moderate": "claude-sonnet-4-6",
        "complex": "claude-sonnet-4-6",
    }

    def __init__(self, api_key: Optional[str] = None, config: Optional[Dict] = None):
        """
        Initialize Anthropic provider.

        Args:
            api_key: API key (or reads from config file/env)
            config: Optional config dict with 'api_key_file' and 'models'
        """
        self.api_key = api_key
        self.config = config or {}
        self.client = None
        self.models = self.DEFAULT_MODELS.copy()

        # Load API key from config file if specified
        if not self.api_key and self.config.get('api_key_file'):
            self.api_key = self._load_key_from_file(self.config['api_key_file'])

        # Fall back to environment variable
        if not self.api_key:
            self.api_key = os.environ.get('ANTHROPIC_API_KEY')

        # Override models from config
        if self.config.get('models'):
            self.models.update(self.config['models'])

        # Initialize client if key available
        if self.api_key:
            try:
                import anthropic
                self.client = anthropic.Anthropic(api_key=self.api_key)
            except ImportError:
                pass

    def _load_key_from_file(self, filepath: str) -> Optional[str]:
        """Load API key from a config file."""
        filepath = os.path.expanduser(filepath)
        if not os.path.exists(filepath):
            return None

        try:
            with open(filepath, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('ANTHROPIC_API_KEY='):
                        return line.split('=', 1)[1].strip()
        except Exception:
            pass
        return None

    @property
    def name(self) -> str:
        return "anthropic"

    def is_available(self) -> bool:
        return self.client is not None

    def supports_tools(self) -> bool:
        return True

    def get_model_for_tier(self, tier: str) -> str:
        return self.models.get(tier, self.models['moderate'])

    def calculate_cost(self, tokens_in: int, tokens_out: int, model: str) -> float:
        # Find pricing for model
        pricing = self.PRICING.get(model)
        if not pricing:
            # Try to match by model family
            for key, price in self.PRICING.items():
                if key in model.lower():
                    pricing = price
                    break

        if not pricing:
            return 0.0

        cost_in = (tokens_in / 1_000_000) * pricing['input']
        cost_out = (tokens_out / 1_000_000) * pricing['output']
        return cost_in + cost_out

    def chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        system: Optional[str] = None,
        stream: bool = False,
        model: Optional[str] = None,
        **kwargs
    ) -> LLMResponse | Generator:
        """Send a chat request to Claude."""

        if not self.client:
            raise RuntimeError("Anthropic client not initialized. Check API key.")

        # Use provided model or default to moderate tier
        model = model or self.get_model_for_tier('moderate')

        # Build request
        request = {
            "model": model,
            "max_tokens": kwargs.get('max_tokens', 4096),
            "messages": messages,
        }

        if system:
            request["system"] = system

        if tools:
            # Convert to Anthropic tool format
            request["tools"] = self._convert_tools(tools)

        if stream:
            return self._stream_response(request, model)
        else:
            return self._sync_response(request, model)

    def _convert_tools(self, tools: List[Dict]) -> List[Dict]:
        """Convert generic tool format to Anthropic format."""
        anthropic_tools = []
        for tool in tools:
            anthropic_tools.append({
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool.get("input_schema", {"type": "object", "properties": {}})
            })
        return anthropic_tools

    def _sync_response(self, request: Dict, model: str) -> LLMResponse:
        """Make a synchronous request."""
        response = self.client.messages.create(**request)

        # Extract content
        content = ""
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input
                })

        tokens_in = response.usage.input_tokens
        tokens_out = response.usage.output_tokens
        cost = self.calculate_cost(tokens_in, tokens_out, model)

        return LLMResponse(
            content=content,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason,
            raw_response=response
        )

    def _stream_response(self, request: Dict, model: str) -> Generator:
        """Make a streaming request."""
        with self.client.messages.stream(**request) as stream:
            for event in stream:
                if hasattr(event, 'type'):
                    if event.type == 'content_block_delta':
                        if hasattr(event.delta, 'text'):
                            yield {"type": "text", "content": event.delta.text}
                    elif event.type == 'content_block_start':
                        if hasattr(event.content_block, 'type') and event.content_block.type == 'tool_use':
                            yield {
                                "type": "tool_start",
                                "id": event.content_block.id,
                                "name": event.content_block.name
                            }
                    elif event.type == 'message_stop':
                        yield {"type": "done"}

    def format_assistant_message(self, response) -> Dict:
        """Use raw Anthropic content blocks (text + tool_use) for the assistant turn."""
        return {
            "role": "assistant",
            "content": response.raw_response.content
        }

    def format_tool_result(self, tool_call_id: str, result: Any) -> Dict:
        """Format tool result for Anthropic."""
        if isinstance(result, dict):
            content = json.dumps(result)
        else:
            content = str(result)

        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": content
                }
            ]
        }
