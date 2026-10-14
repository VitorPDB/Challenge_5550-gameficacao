import pytest
from app.services.risk_rules import RuleEngine
from app.vision.person_compliance_matcher import PersonComplianceMatcher


@pytest.mark.parametrize("implementation", [RuleEngine._point_in_polygon, PersonComplianceMatcher._point_in_polygon])
@pytest.mark.parametrize("point, expected", [((0.1, 0.1), True), ((0.8, 0.8), False), ((0.5, 0.5), True)])
def test_triangle_is_independent_of_vertex_order(implementation, point, expected):
    triangle = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)]
    assert implementation(point, triangle) is expected
    assert implementation(point, triangle[::-1]) is expected


@pytest.mark.parametrize("implementation", [RuleEngine._point_in_polygon, PersonComplianceMatcher._point_in_polygon])
def test_concave_polygon_does_not_include_cutout(implementation):
    polygon = [(0, 0), (1, 0), (1, 0.3), (0.3, 0.3), (0.3, 1), (0, 1)]
    for vertices in [polygon, polygon[::-1]]:
        assert implementation((0.2, 0.8), vertices)
        assert not implementation((0.8, 0.8), vertices)
