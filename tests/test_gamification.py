from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from app.services.gamification_service import build_team_progress

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def action(category="prevention", days=0, status="verified", team="Turno A"):
    return SimpleNamespace(team=team, category=category, status=status,
                           verified_at=NOW-timedelta(days=days) if status == "verified" else None)


@pytest.mark.parametrize("count,level,remaining", [(0, 1, 300), (2, 1, 100), (3, 2, 500),
                                                  (8, 3, 700), (15, 4, 1000), (25, 5, 0), (30, 5, 0)])
def test_level_boundaries_and_progress(count, level, remaining):
    missions = [action() for _ in range(count)] + [action(status="open")]
    progress = build_team_progress(missions, NOW)[0]
    assert progress["points"] == count*100
    assert progress["level"] == level
    assert progress["points_to_next_level"] == remaining
    assert 0 <= progress["level_progress"] <= 1


def test_queued_actions_and_future_or_missing_verification_do_not_earn_xp():
    missing_date = action()
    missing_date.verified_at = None
    progress = build_team_progress([action(status="open"), action(status="submitted"), action(days=-1), missing_date], NOW)[0]
    assert progress["points"] == 0
    assert progress["open"] == progress["awaiting_review"] == 1
    assert progress["achievements"] == []


def test_earned_week_badge_persists_after_weekly_goal_resets():
    missions = [action(days=30), action(days=29), action(days=28)]
    progress = build_team_progress(missions, NOW)[0]
    assert "Semana de ação" in progress["achievements"]
    assert progress["weekly_verified"] == 0
    assert progress["challenges"][0]["progress"] == 0


def test_repeated_category_cannot_unlock_diversity_and_teams_are_independent():
    progress = build_team_progress([action("helmet") for _ in range(3)] + [action("coverage", team="Turno B")], NOW)
    assert "Prevenção em três frentes" not in progress[0]["achievements"]
    assert progress[0]["challenges"][1]["progress"] == 1
    assert progress[1]["points"] == 100
    assert "Olhos da prevenção" in progress[1]["achievements"]


def test_known_categories_unlock_badges_and_naive_sqlite_dates_are_supported():
    missions = [action("helmet"), action("coverage"), action("posture")]
    missions[0].verified_at = missions[0].verified_at.replace(tzinfo=None)
    progress = build_team_progress(missions, NOW)[0]
    assert "Prevenção em três frentes" in progress["achievements"]
    assert "Cuidado com a postura" in progress["achievements"]
    assert progress["challenges"][0]["progress"] == 3
    assert progress["points"] == 300
