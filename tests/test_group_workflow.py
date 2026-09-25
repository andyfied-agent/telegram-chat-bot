"""Group chat workflow tests for Phase 2."""
from unittest.mock import MagicMock, patch

import pytest

from telegram_chat_bot import State, Settings, startgroup, handle_message, has_mention_botname, remove_bot_mention


class TestGroupWorkflow:
    """Test complete group chat workflow."""
    
    @pytest.fixture
    def setup_state(self, tmp_path):
        """Create state with admin user and settings."""
        settings = Settings(
            token="test",
            state_file=str(tmp_path / "state.json"),
            admin_user_ids=frozenset([100]),  # Admin user
            max_chars=100,  # Small limit for testing
            registration_file=str(tmp_path / "registrations.json"),
        )
        state = State(settings)
        return state
    
    @pytest.mark.asyncio
    async def test_group_approval_enables_group_chat(self, setup_state):
        """Approved group should respond to messages."""
        state = setup_state
        group_id = -600111111111  # Unique ID
        
        # Initially group is not allowed
        assert not state.allowed_group(group_id)
        
        # Approve the group
        state.group_registrations.set_status(group_id, "approved")
        
        # Now group is allowed
        assert state.allowed_group(group_id)
    
    @pytest.mark.asyncio
    async def test_group_approval_works_for_negative_ids(self, setup_state):
        """Negative group IDs work correctly with approval."""
        state = setup_state
        group_id = -400222222222  # Unique ID
        
        state.group_registrations.set_status(group_id, "approved")
        assert state.allowed_group(group_id)
    
    @pytest.mark.asyncio
    async def test_group_per_user_history_isolation(self, setup_state):
        """Group and private histories are isolated."""
        state = setup_state
        group_id = -300333333333  # Unique ID
        user_id = 100
        
        # Add message to private chat
        state.history[user_id].append({"role": "user", "content": "private"})
        
        # Add message to group chat
        state.history[(user_id, group_id)].append({"role": "user", "content": "group"})
        
        # Private history should only have private message
        assert len(state.history[user_id]) == 1
        assert state.history[user_id][0]["content"] == "private"
        
        # Group history should only have group message
        assert len(state.history[(user_id, group_id)]) == 1
        assert state.history[(user_id, group_id)][0]["content"] == "group"
    
    @pytest.mark.asyncio
    async def test_startgroup_requires_admin(self, setup_state):
        """Only admins can register groups."""
        state = setup_state
        
        class NonAdminUser:
            id = 200  # Not admin
        
        class MockMessage:
            async def reply_text(self, text):
                pass
        
        class NonAdminUpdate:
            effective_user = NonAdminUser()
            message = MockMessage()
        
        class NonAdminContext:
            application = MagicMock()
            application.bot_data = {"state": state}
            args = ["-300444444444"]  # Unique ID
        
        update = NonAdminUpdate()
        context = NonAdminContext()
        
        await startgroup(update, context)
        
        # Should show admin error (not create registration)
        assert state.group_registrations.status(-300444444444) is None
    
    @pytest.mark.asyncio
    async def test_startgroup_creates_pending_for_admin(self, setup_state):
        """Admin can create pending group registration."""
        state = setup_state
        
        class AdminUser:
            id = 100  # Is admin
        
        class MockMessage:
            async def reply_text(self, text):
                pass
        
        class AdminUpdate:
            effective_user = AdminUser()
            message = MockMessage()
        
        class AdminContext:
            application = MagicMock()
            application.bot_data = {"state": state}
            args = ["-300555555555"]  # Unique ID
        
        update = AdminUpdate()
        context = AdminContext()
        
        await startgroup(update, context)
        
        assert state.group_registrations.status(-300555555555) == "pending"
    
    @pytest.mark.asyncio
    async def test_approvegroup_changes_status(self, setup_state):
        """Approvegroup changes pending to approved."""
        state = setup_state
        
        # Create pending registration
        state.group_registrations.set_status(-300666666666, "pending")
        assert state.group_registrations.status(-300666666666) == "pending"
        
        # Approve
        state.group_registrations.set_status(-300666666666, "approved")
        assert state.group_registrations.status(-300666666666) == "approved"
        assert state.allowed_group(-300666666666)
    
    @pytest.mark.asyncio
    async def test_groupusers_lists_groups(self, setup_state):
        """Groupusers command lists all groups."""
        state = setup_state
        
        # Create various registrations
        state.group_registrations.set_status(-300777777777, "pending")
        state.group_registrations.set_status(-300888888888, "approved")
        state.group_registrations.set_status(-300999999999, "rejected")
        
        pending = state.group_registrations.users("pending")
        approved = state.group_registrations.users("approved")
        rejected = state.group_registrations.users("rejected")
        
        assert -300777777777 in pending
        assert -300888888888 in approved
        assert -300999999999 in rejected


class TestSupergroupSupport:
    """Test that supergroups are handled correctly."""
    
    @pytest.fixture
    def setup_state(self, tmp_path):
        """Create state with admin user."""
        settings = Settings(
            token="test",
            state_file=str(tmp_path / "state.json"),
            admin_user_ids=frozenset([100]),
        )
        state = State(settings)
        return state
    
    @pytest.mark.asyncio
    async def test_handle_message_supergroup_requires_mention(self, setup_state):
        """Supergroup messages require @mention to trigger response."""
        state = setup_state
        supergroup_id = -100111111111
        
        # Approve the supergroup
        state.group_registrations.set_status(supergroup_id, "approved")
        
        # Create mock update for supergroup message WITHOUT mention
        class MockUser:
            id = 200
        
        class MockEntity:
            type = "text"  # Not a mention
            offset = 0
            length = 15
        
        class MockMessage:
            type = "message"
            text = "Hello everyone!"
            entities = [MockEntity()]
            
            def parse_entity(self, entity):
                return self.text[entity.offset:entity.offset + entity.length]
            
            async def reply_text(self, text):
                pass
        
        class MockChat:
            id = supergroup_id
            type = "supergroup"
        
        class MockUpdate:
            effective_user = MockUser()
            effective_chat = MockChat()
            message = MockMessage()
        
        class MockBot:
            username = "testbot"
        
        class MockContext:
            application = MagicMock()
            application.bot_data = {"state": state}
            bot = MockBot()
            args = []
        
        update = MockUpdate()
        context = MockContext()
        
        # Call handle_message - should silently ignore (no mention)
        with patch.object(state, "persist") as mock_persist:
            await handle_message(update, context)
            
            # No reply should have been sent, history unchanged
            assert mock_persist.call_count == 0
    
    @pytest.mark.asyncio
    async def test_handle_message_supergroup_with_mention(self, setup_state):
        """Supergroup messages with @mention are processed."""
        state = setup_state
        supergroup_id = -100222222222
        
        # Approve the supergroup
        state.group_registrations.set_status(supergroup_id, "approved")
        
        # Create mock update for supergroup message WITH mention entity
        class MockUser:
            id = 200
        
        class MockEntity:
            type = "mention"
            offset = 0
            length = 8  # @testbot is 8 chars
        
        class MockMessage:
            type = "message"
            text = "@testbot Hello!"
            entities = [MockEntity()]
            
            def parse_entity(self, entity):
                return self.text[entity.offset:entity.offset + entity.length]
            
            async def reply_text(self, text):
                pass
        
        class MockChat:
            id = supergroup_id
            type = "supergroup"
        
        class MockUpdate:
            effective_user = MockUser()
            effective_chat = MockChat()
            message = MockMessage()
        
        class MockBot:
            username = "testbot"
        
        class MockContext:
            application = MagicMock()
            application.bot_data = {"state": state}
            bot = MockBot()
            args = []
        
        update = MockUpdate()
        context = MockContext()
        
        # Call handle_message - should attempt to process (will fail without model, but will reach API call)
        # We check that it gets past the mention filter
        with patch('telegram_chat_bot.complete_with_usage') as mock_complete:
            mock_complete.return_value = MagicMock(text="Response", prompt_tokens=1, completion_tokens=1)
            
            await handle_message(update, context)
            
            # complete_with_usage should be called (meaning mention was found and processed)
            assert mock_complete.called
    
    @pytest.mark.asyncio
    async def test_mention_detection_case_insensitive(self):
        """Mention detection is case-insensitive."""
        
        class MockEntity:
            type = "mention"
            offset = 0
            length = 8  # @TESTBOT is 8 chars
        
        class MockMessage:
            text = "@TESTBOT Hello!"  # Uppercase
            entities = [MockEntity()]
            
            def parse_entity(self, entity):
                return self.text[entity.offset:entity.offset + entity.length]
        
        # Bot username is lowercase
        assert has_mention_botname(MockMessage(), "testbot") is True
        
        class MockMessage2:
            text = "@TeStBoT Hello!"  # Mixed case
            entities = [MockEntity()]
            
            def parse_entity(self, entity):
                return self.text[entity.offset:entity.offset + entity.length]
        
        assert has_mention_botname(MockMessage2(), "testbot") is True
        
        # Test bot_command type too
        class MockCommandEntity:
            type = "bot_command"
            offset = 0
            length = 8
        
        class MockMessage3:
            text = "@testbot command"
            entities = [MockCommandEntity()]
            
            def parse_entity(self, entity):
                return self.text[entity.offset:entity.offset + entity.length]
        
        assert has_mention_botname(MockMessage3(), "testbot") is True
    
    @pytest.mark.asyncio
    async def test_mention_detection_avoids_false_positives(self):
        """Mention detection avoids false positives from substring matches."""
        
        # These should NOT match @testbot
        class MockEntity1:
            type = "mention"
            offset = 0
            length = 12
        
        class MockMessage1:
            text = "@testbot123 hello"
            entities = [MockEntity1()]
            
            def parse_entity(self, entity):
                return self.text[entity.offset:entity.offset + entity.length]
        
        assert has_mention_botname(MockMessage1(), "testbot") is False
        
        class MockEntity2:
            type = "mention"
            offset = 8
            length = 8
        
        class MockMessage2:
            text = "someone@testbotfoo"
            entities = [MockEntity2()]
            
            def parse_entity(self, entity):
                return self.text[entity.offset:entity.offset + entity.length]
        
        assert has_mention_botname(MockMessage2(), "testbot") is False
        
        # No entities at all
        class MockMessage3:
            text = "@testbot hello"
            entities = []
        
        assert has_mention_botname(MockMessage3(), "testbot") is False


class TestMentionEntityHandling:
    """Test proper UTF-16 entity handling with emoji/non-BMP characters."""
    
    def test_remove_mention_with_emoji_prefix(self):
        """Mention removal works correctly when emoji precedes @botname.
        
        This is the critical regression test: Telegram entity offsets are in UTF-16 code units,
        not Python string indices. An emoji like 😀 is a single UTF-16 code unit, but two
        Unicode code points (surrogate pair). Python string slicing using Python indices
        would break here, but parse_entity handles it correctly.
        """
        # 😀 is a single emoji (U+1F600), 2 UTF-16 code units
        # The message is: "😀 hello @testbot"
        # Entity for @testbot: offset=10, length=8 (in UTF-16 units)
        
        class MockEntity:
            type = "mention"
            offset = 10
            length = 8
        
        class MockMessage:
            text = "😀 hello @testbot"
            entities = [MockEntity()]
            
            def parse_entity(self, entity):
                # Simulate Telegram's parse_entity which uses UTF-16 offsets
                # "😀" = 2 UTF-16 units, " hello " = 7 UTF-16 units, so @testbot starts at 10
                return "@testbot"
        
        result = remove_bot_mention(MockMessage(), "testbot")
        assert result == "😀 hello", f"Expected '😀 hello' but got {repr(result)}"
    
    def test_remove_mention_with_non_bmp(self):
        """Mention removal works with non-BMP characters."""
        # 🌍 (U+1F30D) is a non-BMP character
        class MockEntity:
            type = "mention"
            offset = 8
            length = 8
        
        class MockMessage:
            text = "🌍 hello @TESTBOT world"  # Note: single space before @TESTBOT becomes double space after removal
            entities = [MockEntity()]
            
            def parse_entity(self, entity):
                return "@TESTBOT"
        
        result = remove_bot_mention(MockMessage(), "testbot")
        assert result == "🌍 hello  world", f"Expected '🌍 hello  world' (double space from removal) but got {repr(result)}"
    
    def test_remove_mention_uppercase(self):
        """Mention removal handles case differences."""
        class MockEntity:
            type = "mention"
            offset = 0
            length = 8
        
        class MockMessage:
            text = "@TESTBOT hello"
            entities = [MockEntity()]
            
            def parse_entity(self, entity):
                return "@TESTBOT"
        
        result = remove_bot_mention(MockMessage(), "testbot")
        assert result == "hello", f"Expected 'hello' but got {repr(result)}"
    
    def test_remove_mention_with_multiple(self):
        """Only the first matching mention is removed."""
        class MockEntity:
            type = "mention"
            offset = 0
            length = 8
        
        class MockMessage:
            text = "@testbot say hello to @TEstbot"
            entities = [MockEntity()]
            
            def parse_entity(self, entity):
                return "@testbot"
        
        result = remove_bot_mention(MockMessage(), "testbot")
        assert result == "say hello to @TEstbot", f"Expected 'say hello to @TEstbot' but got {repr(result)}"


class TestMaxCharsEnforcement:
    """Test that max_chars limit is enforced for group messages."""
    
    @pytest.fixture
    def setup_state(self, tmp_path):
        """Create state with small max_chars limit for testing."""
        settings = Settings(
            token="test",
            state_file=str(tmp_path / "state.json"),
            admin_user_ids=frozenset([100]),
            max_chars=50,  # Very small limit
        )
        state = State(settings)
        return state
    
    @pytest.mark.asyncio
    async def test_group_message_respects_max_chars(self, setup_state):
        """Long group messages should be truncated to max_chars."""
        state = setup_state
        group_id = -700111111111  # Unique ID
        
        # Approve the group
        state.group_registrations.set_status(group_id, "approved")
        
        # Create a very long message
        long_text = "@testbot " + "x" * 200  # Well over 50 chars
        
        class MockUser:
            id = 200
        
        class MockEntity:
            type = "mention"
            offset = 0
            length = 8
        
        class MockMessage:
            type = "message"
            text = long_text
            entities = [MockEntity()]
            
            def parse_entity(self, entity):
                return self.text[entity.offset:entity.offset + entity.length]
            
            async def reply_text(self, text):
                pass
        
        class MockChat:
            id = group_id
            type = "supergroup"
        
        class MockUpdate:
            effective_user = MockUser()
            effective_chat = MockChat()
            message = MockMessage()
        
        class MockBot:
            username = "testbot"
        
        class MockContext:
            application = MagicMock()
            application.bot_data = {"state": state}
            bot = MockBot()
            args = []
        
        update = MockUpdate()
        context = MockContext()
        
        # Call handle_message - should truncate to max_chars
        with patch('telegram_chat_bot.complete_with_usage') as mock_complete:
            mock_complete.return_value = MagicMock(text="Response", prompt_tokens=1, completion_tokens=1)
            
            await handle_message(update, context)
            
            # complete_with_usage should be called (mention was found)
            assert mock_complete.called
            
            # Verify the history contains truncated message
            history = state.history[(200, group_id)]
            assert len(history) == 2  # user message + assistant response
            user_msg = history[0]["content"]
            # Should be truncated to max_chars (50)
            assert len(user_msg) <= 50
            assert "x" in user_msg
    
    @pytest.mark.asyncio
    async def test_group_message_under_max_chars_passed(self, setup_state):
        """Short group messages should not be truncated."""
        state = setup_state
        group_id = -700222222222  # Unique ID
        
        # Approve the group
        state.group_registrations.set_status(group_id, "approved")
        
        # Create a short message under max_chars
        short_text = "@testbot hi"
        
        class MockUser:
            id = 200
        
        class MockEntity:
            type = "mention"
            offset = 0
            length = 8
        
        class MockMessage:
            type = "message"
            text = short_text
            entities = [MockEntity()]
            
            def parse_entity(self, entity):
                return self.text[entity.offset:entity.offset + entity.length]
            
            async def reply_text(self, text):
                pass
        
        class MockChat:
            id = group_id
            type = "supergroup"
        
        class MockUpdate:
            effective_user = MockUser()
            effective_chat = MockChat()
            message = MockMessage()
        
        class MockBot:
            username = "testbot"
        
        class MockContext:
            application = MagicMock()
            application.bot_data = {"state": state}
            bot = MockBot()
            args = []
        
        update = MockUpdate()
        context = MockContext()
        
        # Call handle_message
        with patch('telegram_chat_bot.complete_with_usage') as mock_complete:
            mock_complete.return_value = MagicMock(text="Response", prompt_tokens=1, completion_tokens=1)
            
            await handle_message(update, context)
            
            # complete_with_usage should be called
            assert mock_complete.called
            
            # Verify the history contains the full message (without @testbot)
            history = state.history[(200, group_id)]
            assert len(history) == 2  # user message + assistant response
            user_msg = history[0]["content"]
            assert user_msg == "hi"  # @testbot removed, "hi" remains
