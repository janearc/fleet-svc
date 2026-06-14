import pytest
from fleet.display import render_fleet_state, render_pause_result, render_selfcheck
from unittest.mock import patch

def test_render_fleet_state_json(sample_fleet_state, capsys):
    render_fleet_state(sample_fleet_state, as_json=True)
    captured = capsys.readouterr()
    assert "app-web" in captured.out
    assert "delightd" in captured.out

def test_render_fleet_state_table(sample_fleet_state, capsys):
    render_fleet_state(sample_fleet_state, as_json=False)
    captured = capsys.readouterr()
    assert "Fleet State" in captured.out
    assert "app-web" in captured.out

def test_render_selfcheck(sample_source_health, capsys):
    render_selfcheck(sample_source_health)
    captured = capsys.readouterr()
    assert "Selfcheck" in captured.out
    assert "docker" in captured.out
    assert "kube" in captured.out
