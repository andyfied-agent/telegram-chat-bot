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
        
        # Create mock update for group chat
        chat = Chat(id=456, type="group")
        user = User(id=123, first_name="Test", is_bot=False)
        update = Update(
            update_id=1,
            message=MagicMock(
                chat=chat,
                effective_user=user,
                reply_text=AsyncMock()
            )
        )
        
        # Create mock context
        state = State(Settings())
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
        
        # Create mock update for group chat
        chat = Chat(id=456, type="group")
        user = User(id=123, first_name="Test", is_bot=False)
        update = Update(
            update_id=1,
            message=MagicMock(
                chat=chat,
                effective_user=user,
                reply_text=AsyncMock()
            )
        )
        
        # Create mock context
        state = State(Settings())
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
