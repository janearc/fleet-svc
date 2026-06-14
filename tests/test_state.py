import pytest
import os
import json
from pathlib import Path
from fleet.state import PauseJournal

@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "test.db"
    return PauseJournal(db_path=db_path)

def test_record_intent_and_mark_applied(temp_db):
    temp_db.record_intent("test-service", "docker", json.dumps({"status": "running"}))
    
    stale = temp_db.get_stale_intents()
    assert len(stale) == 1
    assert stale[0]["service_name"] == "test-service"
    
    temp_db.mark_applied("test-service")
    stale = temp_db.get_stale_intents()
    assert len(stale) == 0
    
    paused = temp_db.get_paused_services()
    assert len(paused) == 1
    assert paused[0]["service_name"] == "test-service"

def test_mark_resumed(temp_db):
    temp_db.record_intent("test-service", "docker", "{}")
    temp_db.mark_applied("test-service")
    temp_db.mark_resumed("test-service")
    
    paused = temp_db.get_paused_services()
    assert len(paused) == 0

def test_reconcile_applied(temp_db):
    temp_db.record_intent("web", "kube", "{}")
    temp_db.mark_applied("web")
    
    # Needs to stay paused
    temp_db.reconcile("web", True)
    paused = temp_db.get_paused_services()
    assert len(paused) == 1
    
    # It was manually resumed
    temp_db.reconcile("web", False)
    paused = temp_db.get_paused_services()
    assert len(paused) == 0
