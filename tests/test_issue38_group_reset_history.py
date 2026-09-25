"""Tests for Issue #38: Group /reset and /history reachability."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Update, Chat, User
from telegram.ext import ContextTypes

from telegram_chat_bot import State, Settings


class TestGroupResetHistoryReachability:
    """Verify reset and history commands work in group chats."""
    
    @pytest.mark.asyncio
    async def test_reset_works_in_group(self):
        """Group chat users should be able to reset their group context."""
        from telegram_chat_bot import reset
        
        # Create proper mock update for group chat
        chat = MagicMock()
        chat.type = "group"
        chat.id = 456
        
        user = MagicMock()
        user.id = 123
        
        update = MagicMock()
        update.effective_chat = chat
        update.effective_user = user
        update.message = MagicMock(
            reply_text=AsyncMock()
        )
        
        # Create mock context
        state = State(Settings(token="fake_token"))
        state.group_registrations.set_status(456, "approved")
        context = MagicMock(
            application=MagicMock(
                bot_data={"state": state}
            )
        )
        
        # Call reset
        await reset(update, context)
        
        # Verify reply was sent (group context reset)
        update.message.reply_text.assert_called_once()
        call_args = update.message.reply_text.call_args[0][0]
        assert "group" in call_args.lower()
    
    @pytest.mark.asyncio
    async def test_history_works_in_group(self):
        """Group chat users should be able to view their group context."""
        from telegram_chat_bot import history
        
        # Create proper mock update for group chat
        # effective_chat must be set (not chat)
        chat = MagicMock()
        chat.type = "group"
        chat.id = 456
        
        user = MagicMock()
        user.id = 123
        
        update = MagicMock()
        update.effective_chat = chat
        update.effective_user = user
        update.message = MagicMock(
            reply_text=AsyncMock()
        )
        
        # Create mock context
        state = State(Settings(token="fake_token"))
        state.group_registrations.set_status(456, "approved")
        state.history[(123, 456)] = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"}
        ]
        context = MagicMock(
            application=MagicMock(
                bot_data={"state": state}
            )
        )
        
        # Call history
        await history(update, context)
        
        # Verify reply was sent with history count
        update.message.reply_text.assert_called_once()
        call_args = update.message.reply_text.call_args[0][0]
        assert "2" in call_args  # 2 messages
        assert "group" in call_args.lower()
