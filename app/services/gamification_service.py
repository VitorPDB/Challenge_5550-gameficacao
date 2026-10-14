"""Team progression derived from verified preventive actions, never from alert absence."""
from collections import Counter, deque
from datetime import timedelta, timezone

LEVELS = [(0, "Primeiros passos"), (300, "Equipe atenta"), (800, "Equipe preventiva"),
          (1500, "Guardiões do setor"), (2500, "Referência em prevenção")]
CATEGORIES = {
    "helmet": "Capacete", "vest": "Colete", "gloves": "Luvas", "glasses": "Óculos",
    "mask": "Máscara", "safety_shoe": "Calçado", "ear_protection": "Proteção auditiva",
    "posture": "Postura e ergonomia", "prevention": "Prevenção geral", "coverage": "Cobertura das câmeras",
    "circulation": "Circulação e zonas", "falls": "Prevenção de quedas", "risk_area": "Área de risco",
}


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def build_team_progress(missions, now):
    now = aware(now)
    teams = {}
    for mission in missions:
        teams.setdefault(mission.team, []).append(mission)
    output = []
    for name, actions in sorted(teams.items()):
        verified = sorted([m for m in actions if m.status == "verified" and m.verified_at and aware(m.verified_at) <= now],
                          key=lambda m: aware(m.verified_at))
        categories = Counter(m.category for m in verified if m.category in CATEGORIES)
        points = len(verified)*100
        level = max(i for i, (threshold, _) in enumerate(LEVELS) if points >= threshold)
        floor = LEVELS[level][0]
        next_xp = LEVELS[level+1][0] if level+1 < len(LEVELS) else None
        recent = [m for m in verified if aware(m.verified_at) >= now-timedelta(days=7)]
        weekly_window = deque()
        weekly_earned = None
        for mission in verified:
            at = aware(mission.verified_at)
            while weekly_window and at-weekly_window[0] > timedelta(days=7):
                weekly_window.popleft()
            weekly_window.append(at)
            if len(weekly_window) >= 3:
                weekly_earned = at
                break
        badges = [
            ("first_action", "Primeira ação preventiva", "Uma ação verificada", len(verified), 1),
            ("three_fronts", "Prevenção em três frentes", "Ações em três categorias diferentes", len(categories), 3),
            ("ten_actions", "Prevenção em prática", "Dez ações verificadas", len(verified), 10),
            ("coverage_care", "Olhos da prevenção", "Uma revisão de cobertura verificada", categories["coverage"], 1),
            ("ergonomic_care", "Cuidado com a postura", "Uma ação de ergonomia verificada", categories["posture"], 1),
            ("action_week", "Semana de ação", "Três ações verificadas em até sete dias", 3 if weekly_earned else len(recent), 3),
        ]
        achievement_details = [{"id": key, "name": label, "description": description,
                                "progress": min(progress, target), "target": target, "unlocked": progress >= target}
                               for key, label, description, progress, target in badges]
        challenges = [
            {"id": "weekly_actions", "name": "Colocar prevenção em prática", "description": "Verificar três ações nos últimos sete dias.",
             "progress": min(len(recent), 3), "target": 3},
            {"id": "weekly_diversity", "name": "Ampliar o cuidado", "description": "Agir em duas categorias nos últimos sete dias.",
             "progress": min(len({m.category for m in recent if m.category in CATEGORIES}), 2), "target": 2},
        ]
        output.append({"team": name, "points": points, "verified": len(verified),
                       "open": sum(m.status == "open" for m in actions),
                       "awaiting_review": sum(m.status == "submitted" for m in actions),
                       "level": level+1, "level_name": LEVELS[level][1], "next_level_points": next_xp,
                       "points_to_next_level": max(0, next_xp-points) if next_xp else 0,
                       "level_progress": round(min(1, (points-floor)/(next_xp-floor)) if next_xp else 1, 4),
                       "weekly_verified": len(recent), "challenges": challenges,
                       "achievements": [b["name"] for b in achievement_details if b["unlocked"]],
                       "achievement_details": achievement_details,
                       "categories": [{"key": key, "label": CATEGORIES.get(key, key), "verified": count}
                                      for key, count in sorted(categories.items())]})
    return output
