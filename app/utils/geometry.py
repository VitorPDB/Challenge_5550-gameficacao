from __future__ import annotations


def point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    """Inclui a borda e funciona nas duas orientações do polígono."""
    if len(polygon) < 3:
        return False
    x, y = point
    inside = False
    xj, yj = polygon[-1]
    for xi, yi in polygon:
        cross = (x - xi) * (yj - yi) - (y - yi) * (xj - xi)
        if abs(cross) <= 1e-12 and min(xi, xj) <= x <= max(xi, xj) and min(yi, yj) <= y <= max(yi, yj):
            return True
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        xj, yj = xi, yi
    return inside
