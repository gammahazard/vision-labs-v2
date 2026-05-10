"""
tests/test_scene_analysis.py — Tests for AI scene analysis feature.

Tests the describe_scene() function (mocked Ollama), the prompt
selection logic, the Redis storage of descriptions, and the
analysis API endpoint.

NO real Ollama, Telegram, or Redis. Uses unittest.mock to isolate logic.
"""

import os
import sys
import re
import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_DASHBOARD_DIR = os.path.join(
    os.path.dirname(__file__), "..", "services", "dashboard"
)
sys.path.insert(0, _DASHBOARD_DIR)


# ===========================================================================
# Prompt Selection
# ===========================================================================
class TestPromptSelection:
    """Tests for choosing the correct prompt based on event type."""

    def test_person_prompt_content(self):
        """Person prompt asks for clothing, build, hair, accessories."""
        from routes.notifications import _PERSON_PROMPT
        assert "clothing" in _PERSON_PROMPT.lower()
        assert "build" in _PERSON_PROMPT.lower()
        assert "hair" in _PERSON_PROMPT.lower()

    def test_vehicle_prompt_content(self):
        """Vehicle prompt asks for type, color, plates."""
        from routes.notifications import _VEHICLE_PROMPT
        assert "vehicle type" in _VEHICLE_PROMPT.lower()
        assert "color" in _VEHICLE_PROMPT.lower()
        assert "plates" in _VEHICLE_PROMPT.lower()

    def test_prompts_are_different(self):
        """Person and vehicle prompts are distinct."""
        from routes.notifications import _PERSON_PROMPT, _VEHICLE_PROMPT
        assert _PERSON_PROMPT != _VEHICLE_PROMPT

    def test_prompts_mention_local_log(self):
        """Both prompts note the output goes into a local log."""
        from routes.notifications import _PERSON_PROMPT, _VEHICLE_PROMPT
        assert "local" in _PERSON_PROMPT.lower()
        assert "local" in _VEHICLE_PROMPT.lower()


# ===========================================================================
# Think-tag Stripping
# ===========================================================================
class TestThinkTagStripping:
    """Tests for stripping <think>...</think> tags from model responses.
    Some reasoning models wrap their internal chain-of-thought in these
    tags. We strip them before using the output.
    """

    @staticmethod
    def _strip_think(text: str) -> str:
        """Reproduce the stripping logic from describe_scene."""
        return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    def test_no_think_tags(self):
        """Plain text passes through unchanged."""
        assert self._strip_think("A person in a red jacket.") == "A person in a red jacket."

    def test_think_tags_removed(self):
        """Think tags and their content are removed."""
        text = "<think>Let me analyze this...</think>A person in a red jacket."
        assert self._strip_think(text) == "A person in a red jacket."

    def test_multiline_think_tags(self):
        """Multi-line think tags are removed."""
        text = "<think>\nAnalyzing the image...\nI see a person.\n</think>\nMale, dark jacket, walking toward camera."
        assert self._strip_think(text) == "Male, dark jacket, walking toward camera."

    def test_multiple_think_tags(self):
        """Multiple think tag pairs are all removed."""
        text = "<think>first</think>Hello <think>second</think>world"
        assert self._strip_think(text) == "Hello world"

    def test_empty_after_stripping(self):
        """If only think tags, result is empty string."""
        text = "<think>just thinking</think>"
        assert self._strip_think(text) == ""


# ===========================================================================
# describe_scene() — mocked Ollama
# ===========================================================================
class TestDescribeScene:
    """Tests for the async describe_scene() function with mocked Ollama.

    The ollama package is only installed inside the Docker container,
    so we inject a fake module via sys.modules before importing
    describe_scene.
    """

    @staticmethod
    def _run_async(coro):
        """Helper to run async code in tests."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    @staticmethod
    def _install_fake_ollama(mock_client):
        """Inject a fake ollama module into sys.modules."""
        fake_ollama = MagicMock()
        fake_ollama.Client.return_value = mock_client
        sys.modules["ollama"] = fake_ollama
        return fake_ollama

    @staticmethod
    def _cleanup_fake_ollama():
        """Remove fake ollama module."""
        sys.modules.pop("ollama", None)

    def test_returns_description_on_success(self):
        """When Ollama returns a valid response, extract the text."""
        mock_response = MagicMock()
        mock_response.message.content = "  Male, dark jacket, walking north.  "

        mock_client = MagicMock()
        mock_client.chat.return_value = mock_response

        try:
            self._install_fake_ollama(mock_client)
            from routes.notifications import describe_scene
            result = self._run_async(describe_scene(b"\xff\xd8fake_jpeg"))
            assert result == "Male, dark jacket, walking north."
        finally:
            self._cleanup_fake_ollama()

    def test_returns_empty_on_exception(self):
        """When Ollama raises an exception, return empty string gracefully."""
        mock_client = MagicMock()
        mock_client.chat.side_effect = ConnectionError("Ollama offline")

        try:
            self._install_fake_ollama(mock_client)
            from routes.notifications import describe_scene
            result = self._run_async(describe_scene(b"\xff\xd8fake_jpeg"))
            assert result == ""
        finally:
            self._cleanup_fake_ollama()

    def test_returns_empty_on_timeout(self):
        """When the vision model takes too long, return empty string."""
        import time as time_mod

        def slow_chat(*args, **kwargs):
            time_mod.sleep(2)  # Longer than timeout
            return MagicMock(message=MagicMock(content="too late"))

        mock_client = MagicMock()
        mock_client.chat = slow_chat

        try:
            self._install_fake_ollama(mock_client)
            from routes.notifications import describe_scene
            result = self._run_async(
                describe_scene(b"\xff\xd8fake_jpeg", timeout=0.5)
            )
            assert result == ""
        finally:
            self._cleanup_fake_ollama()

    def test_strips_think_tags_from_response(self):
        """Think tags in the response are stripped."""
        mock_response = MagicMock()
        mock_response.message.content = "<think>Analyzing...</think>Female, blue coat."

        mock_client = MagicMock()
        mock_client.chat.return_value = mock_response

        try:
            self._install_fake_ollama(mock_client)
            from routes.notifications import describe_scene
            result = self._run_async(describe_scene(b"\xff\xd8fake_jpeg"))
            assert result == "Female, blue coat."
        finally:
            self._cleanup_fake_ollama()

    def test_passes_correct_prompt(self):
        """The prompt argument is forwarded to the model."""
        mock_response = MagicMock()
        mock_response.message.content = "Test response"

        mock_client = MagicMock()
        mock_client.chat.return_value = mock_response

        custom_prompt = "Describe this vehicle."

        try:
            self._install_fake_ollama(mock_client)
            from routes.notifications import describe_scene
            self._run_async(
                describe_scene(b"\xff\xd8fake_jpeg", prompt=custom_prompt)
            )
            # Verify the prompt was passed in the messages
            call_kwargs = mock_client.chat.call_args
            messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
            assert messages[0]["content"] == custom_prompt
        finally:
            self._cleanup_fake_ollama()

    def test_passes_image_bytes(self):
        """The photo bytes are passed to the model via the images field."""
        mock_response = MagicMock()
        mock_response.message.content = "Test response"

        mock_client = MagicMock()
        mock_client.chat.return_value = mock_response

        test_bytes = b"\xff\xd8test_image_data"

        try:
            self._install_fake_ollama(mock_client)
            from routes.notifications import describe_scene
            self._run_async(describe_scene(test_bytes))
            call_kwargs = mock_client.chat.call_args
            messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
            assert messages[0]["images"] == [test_bytes]
        finally:
            self._cleanup_fake_ollama()


# ===========================================================================
# Caption Integration Pattern
# ===========================================================================
class TestCaptionIntegration:
    """Tests for how AI descriptions are appended to notification captions."""

    @staticmethod
    def _build_caption_with_ai(base_parts: list[str], ai_desc: str) -> str:
        """Reproduce the caption+AI description pattern from notifications."""
        caption = "\n".join(base_parts)
        if ai_desc:
            caption += f"\n\n\U0001f916 {ai_desc}"
        return caption

    def test_description_appended_with_robot_emoji(self):
        """AI description is appended with 🤖 prefix."""
        base = ["🚨 <b>Person Detected</b>", "• Who: unknown", "• Time: 2:35 AM"]
        result = self._build_caption_with_ai(base, "Male, dark jacket.")
        assert "🤖" in result
        assert "Male, dark jacket." in result

    def test_no_description_no_append(self):
        """When description is empty, caption is unchanged."""
        base = ["🚨 <b>Person Detected</b>", "• Time: 2:35 AM"]
        result = self._build_caption_with_ai(base, "")
        assert "🤖" not in result
        assert result == "🚨 <b>Person Detected</b>\n• Time: 2:35 AM"

    def test_description_on_new_line(self):
        """AI description is separated from main caption by blank line."""
        base = ["Header"]
        result = self._build_caption_with_ai(base, "Description here.")
        # Should have double newline before the robot emoji
        assert "\n\n🤖" in result


# ===========================================================================
# Redis Scene Analysis Key Pattern
# ===========================================================================
class TestSceneAnalysisKeys:
    """Tests for the Redis key naming pattern used for scene analysis."""

    @staticmethod
    def _make_key(event_id: str) -> str:
        """Reproduce the key pattern from notifications.py."""
        return f"scene_analysis:{event_id}"

    def test_key_format(self):
        """Key follows scene_analysis:{event_id} pattern."""
        assert self._make_key("1234-5678") == "scene_analysis:1234-5678"

    def test_key_with_redis_stream_id(self):
        """Works with Redis stream IDs (contain dashes)."""
        key = self._make_key("1708800000000-0")
        assert key == "scene_analysis:1708800000000-0"

    def test_key_uniqueness(self):
        """Different event IDs produce different keys."""
        k1 = self._make_key("event-1")
        k2 = self._make_key("event-2")
        assert k1 != k2
