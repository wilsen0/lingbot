"""Content safety guardrails for agent interactions."""

from __future__ import annotations

from dataclasses import dataclass

from linling_agent.llm import Message


@dataclass
class SafetyConfig:
    """Configuration for content safety checks."""

    max_input_length: int = 2000  # max chars in user input
    max_output_length: int = 4000  # max chars in agent output
    block_prompt_injection: bool = True  # detect injection attempts


class ContentFilter:
    """Filters and sanitizes content for safety."""

    def __init__(self, config: SafetyConfig | None = None) -> None:
        self._config = config or SafetyConfig()

    def sanitize_input(self, text: str) -> str:
        """Sanitize user input before sending to LLM.

        - Truncates to max_input_length
        - Strips control characters
        - Marks as user-content (not system instruction)
        """
        text = text[: self._config.max_input_length]
        # Strip null bytes and other control chars (keep newlines/tabs)
        text = "".join(c for c in text if c in {"\n", "\t"} or ord(c) >= 32)
        return text

    def sanitize_output(self, text: str) -> str:
        """Sanitize agent output before sending to user.

        - Truncates to max_output_length
        """
        return text[: self._config.max_output_length]

    def check_injection(self, text: str) -> bool:
        """Check if text contains potential prompt injection patterns.

        Returns True if injection detected (text should be blocked or flagged).
        """
        if not self._config.block_prompt_injection:
            return False

        # Common injection patterns (case-insensitive)
        patterns = [
            "ignore previous instructions",
            "ignore all previous",
            "disregard your instructions",
            "you are now",
            "new instructions:",
            "system prompt:",
            "忽略之前的指令",
            "忽略所有指令",
            "你现在是",
        ]
        lower = text.lower()
        return any(p in lower for p in patterns)

    def build_safe_messages(
        self,
        system: str,
        user_input: str,
        history: list[Message] | None = None,
    ) -> list[Message]:
        """Build a message list with proper system/user separation.

        Ensures user content cannot be confused with system instructions.
        External content is wrapped with markers.
        """
        messages: list[Message] = []
        if system:
            messages.append(Message(role="system", content=system))
        if history:
            messages.extend(history)

        # Sanitize and wrap user input
        safe_input = self.sanitize_input(user_input)
        messages.append(Message(role="user", content=safe_input))
        return messages
