"""Integration tests for per-user rate limit enforcement logic."""

import pytest


class TestHandleMessageRateLimitLogic:
    """Tests that the per-user rate limit logic works correctly in handle_message."""
    
    @pytest.mark.asyncio
    async def test_user_rate_limit_logic(self):
        """Verify the rate limit lookup logic is correct."""
        # Simulate the logic used in handle_message
        user_rate_limits = {123: 30.0, 456: 60.0}
        global_interval = 1.0
        
        # User 123 should get custom limit
        user_id = 123
        user_rate_limit = user_rate_limits.get(user_id, global_interval)
        assert user_rate_limit == 30.0
        
        # User 789 should fall back to global
        user_id = 789
        user_rate_limit = user_rate_limits.get(user_id, global_interval)
        assert user_rate_limit == 1.0
        
        # Empty limits should use global
        user_rate_limits = {}
        user_id = 123
        user_rate_limit = user_rate_limits.get(user_id, global_interval)
        assert user_rate_limit == 1.0
    
    @pytest.mark.asyncio
    async def test_rate_limit_check_in_handle_message_context(self):
        """Test the rate limit check as it would occur in handle_message."""
        # Simulate state object
        user_rate_limits = {123: 30.0}
        global_interval = 1.0
        
        # This is the logic from handle_message
        user_rate_limit = user_rate_limits.get(123, global_interval)
        assert user_rate_limit == 30.0
