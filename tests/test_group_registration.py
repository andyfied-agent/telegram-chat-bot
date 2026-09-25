"""Group registration tests for Phase 2."""
import tempfile
from pathlib import Path

import pytest
from telegram_chat_bot import RegistrationStore


class TestGroupRegistrationStore:
    """Test group registration functionality."""
    
    def test_group_registration_store_initializes_empty(self):
        """Store starts empty."""
        with tempfile.NamedTemporaryFile(delete=False, mode='w') as f:
            f.write("{}\n")
            path = f.name
        try:
            store = RegistrationStore(path, group=True)
            assert store.status(12345) is None
            assert store.users("pending") == []
            assert store.users("approved") == []
        finally:
            Path(path).unlink(missing_ok=True)
    
    def test_group_registration_store_adds_user(self):
        """User can be added to store."""
        with tempfile.NamedTemporaryFile(delete=False, mode='w') as f:
            f.write("{}\n")
            path = f.name
        try:
            store = RegistrationStore(path, group=True)
            store.set_status(12345, "pending")
            assert store.status(12345) == "pending"
            assert store.users("pending") == [12345]
        finally:
            Path(path).unlink(missing_ok=True)
    
    def test_group_registration_store_handles_negative_group_ids(self):
        """Negative group IDs (Telegram style) work correctly."""
        with tempfile.NamedTemporaryFile(delete=False, mode='w') as f:
            f.write("{}\n")
            path = f.name
        try:
            store = RegistrationStore(path, group=True)
            store.set_status(-100, "pending")
            assert store.status(-100) == "pending"
            assert -100 in store.users("pending")
        finally:
            Path(path).unlink(missing_ok=True)
    
    def test_group_registration_store_persists_across_instances(self):
        """State persists when store is reloaded."""
        with tempfile.NamedTemporaryFile(delete=False, mode='w') as f:
            f.write("{}\n")
            path = f.name
        try:
            # First instance creates pending
            store1 = RegistrationStore(path, group=True)
            store1.set_status(54321, "pending")
            
            # Second instance sees the same state
            store2 = RegistrationStore(path, group=True)
            assert store2.status(54321) == "pending"
            assert 54321 in store2.users("pending")
        finally:
            Path(path).unlink(missing_ok=True)
    
    def test_group_registration_store_updates_status(self):
        """Status can be updated."""
        with tempfile.NamedTemporaryFile(delete=False, mode='w') as f:
            f.write("{}\n")
            path = f.name
        try:
            store = RegistrationStore(path, group=True)
            store.set_status(11111, "pending")
            store.set_status(11111, "approved")
            assert store.status(11111) == "approved"
            assert 11111 in store.users("approved")
            assert 11111 not in store.users("pending")
        finally:
            Path(path).unlink(missing_ok=True)
    
    def test_group_registration_store_invalid_status_raises(self):
        """Invalid status raises ValueError."""
        with tempfile.NamedTemporaryFile(delete=False, mode='w') as f:
            f.write("{}\n")
            path = f.name
        try:
            store = RegistrationStore(path, group=True)
            with pytest.raises(ValueError, match="invalid registration status"):
                store.set_status(99999, "invalid")
        finally:
            Path(path).unlink(missing_ok=True)
