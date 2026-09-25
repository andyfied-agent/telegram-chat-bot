"""Tests for Issue #33: Private prompts isolation in group chats."""

from telegram_chat_bot import State, Settings


class TestPrivatePromptsIsolation:
    """Verify private prompts don't leak into group chat requests."""
    
    def test_group_without_group_prompt_no_private_prompt(self):
        """Group chat without group prompt should NOT include user's private prompt."""
        settings = Settings(token="fake_token")
        state = State(settings)
        
        # Set user's private prompt
        state.private_prompts[123] = "You are a helpful assistant."
        state.global_prompt = "Global system prompt"
        
        # Group request without group prompt
        messages = state.messages(user_id=123, group_id=456)
        
        # Should only have global prompt, NOT private prompt
        system_msg = messages[0] if messages else {}
        assert system_msg.get("content") == "Global system prompt"
        assert "helpful assistant" not in system_msg.get("content", "")
    
    def test_group_with_group_prompt_uses_group_prompt(self):
        """Group chat with group prompt should use group prompt, not private prompt."""
        settings = Settings(token="fake_token")
        state = State(settings)
        
        # Set both group and private prompts
        state.group_prompts[456] = "You are a group assistant."
        state.private_prompts[123] = "You are a helpful assistant."
        state.global_prompt = "Global system prompt"
        
        # Group request
        messages = state.messages(user_id=123, group_id=456)
        
        # Should have global + group prompt, NOT private prompt
        content = messages[0].get("content", "")
        assert "Global system prompt" in content
        assert "group assistant" in content
        assert "helpful assistant" not in content
    
    def test_private_chat_uses_private_prompt(self):
        """Private chat should still use private prompt."""
        settings = Settings(token="fake_token")
        state = State(settings)
        
        state.private_prompts[123] = "You are a helpful assistant."
        state.global_prompt = "Global system prompt"
        
        # Private request (group_id=None)
        messages = state.messages(user_id=123, group_id=None)
        
        # Should have global + private prompt
        content = messages[0].get("content", "")
        assert "Global system prompt" in content
        assert "helpful assistant" in content
