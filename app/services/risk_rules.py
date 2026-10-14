from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.services.feature_manager import FeatureManager
from app.utils.geometry import point_in_polygon
from app.vision.person_compliance_matcher import PPE_KEYS, PersonComplianceMatcher
from app.vision.schemas import Detection, PoseResult

# feature -> (mensagem, severidade). Fonte única das regras de EPI: a mesma
# lista de tuplas aparecia duplicada em dois pontos deste arquivo.
# Torso considerado "deitado": quanto o deslocamento horizontal
# ombro->quadril precisa superar o vertical. Medido em PIXELS (ver _is_fallen).
FALLEN_TORSO_RATIO = 1.6
# Cabeca projetada a frente, em fracao da altura do torso da propria pessoa.
HEAD_FORWARD_RATIO = 0.45

PPE_RULES: dict[str, tuple[str, str]] = {
    "helmet": ("Sem capacete", "critical"),
    "vest": ("Sem colete", "high"),
    "gloves": ("Sem luvas", "medium"),
    "glasses": ("Sem óculos de proteção", "high"),
    "mask": ("Sem máscara", "medium"),
    "safety_shoe": ("Sem calçado de segurança", "medium"),
    "ear_protection": ("Sem protetor auricular", "medium"),
}

# Mensagem do alerta quando o EPI está PRESENTE mas fora da posição esperada
# (ex.: capacete na mão) — ver `PersonComplianceMatcher._assign_incorretos`.
# Mesma severidade da ausência: uso incorreto não protege mais que a ausência.
PPE_INCORRECT_MESSAGES: dict[str, str] = {
    "helmet": "Capacete em uso incorreto",
    "vest": "Colete em uso incorreto",
    "gloves": "Luvas em uso incorreto",
    "glasses": "Óculos em uso incorreto",
    "mask": "Máscara em uso incorreto",
    "safety_shoe": "Calçado em uso incorreto",
    "ear_protection": "Protetor auricular em uso incorreto",
}


@dataclass(frozen=True)
class RuleEvaluation:
    """Resultado de UMA passada do motor de regras sobre o frame.

    `people` é o estado de conformidade por pessoa que o matcher já produziu
    para decidir os alertas. Ele volta junto porque o ComplianceService precisa
    exatamente do mesmo dado: antes ele chamava `evaluate()` de novo e refazia
    o matching, então cada frame rodava o RuleEngine 2x e o
    PersonComplianceMatcher 3x.
    """

    alerts: list[RuleAlert]
    people: list[dict[str, Any]]
    # Uma pose por pessoa (so a global quando nao ha caixa de pessoa).
    poses: list[PoseResult] = field(default_factory=list)


@dataclass(frozen=True)
class RuleAlert:
    rule: str
    severity: str
    message: str
    feature: str
    metadata: dict[str, Any]

    @property
    def key(self) -> str:
        subject = self.metadata.get("person_id") or self.metadata.get("subject") or "global"
        return f"{self.rule}:{self.feature}:{subject}"


class RuleEngine:
    def __init__(
        self,
        feature_manager: FeatureManager,
        cooldown_seconds: int,
        risk_polygon: list[tuple[float, float]],
        supported_ppe_getter: Callable[[], set[str]] | None = None,
    ) -> None:
        self.feature_manager = feature_manager
        self.cooldown_seconds = cooldown_seconds
        self.risk_polygon = risk_polygon
        self.supported_ppe_getter = supported_ppe_getter
        self.person_matcher = PersonComplianceMatcher()

    def analyze(
        self,
        detections: list[Detection],
        pose: PoseResult | None,
        frame_shape: tuple[int, int, int],
        poses: list[PoseResult] | None = None,
    ) -> RuleEvaluation:
        """Passada única: monta o estado por pessoa uma vez e deriva todos os
        alertas dele. Use isto no loop de captura; `evaluate()` continua
        existindo para quem só quer os alertas."""
        people = self.build_people(detections, frame_shape)
        poses_avaliadas = list(poses) if poses else ([pose] if pose else [])
        alerts: list[RuleAlert] = []
        alerts.extend(self._evaluate_ppe(detections, pose, people))
        alerts.extend(self._evaluate_pose(poses_avaliadas, frame_shape))
        alerts.extend(self._evaluate_risk_area(people, pose))
        return RuleEvaluation(alerts=alerts, people=people, poses=poses_avaliadas)

    def evaluate(self, detections: list[Detection], pose: PoseResult | None, frame_shape: tuple[int, int, int]) -> list[RuleAlert]:
        return self.analyze(detections, pose, frame_shape).alerts

    def supported_ppe(self) -> set[str]:
        return self.supported_ppe_getter() if self.supported_ppe_getter else set(PPE_KEYS)

    def build_people(self, detections: list[Detection], frame_shape: tuple[int, int, int]) -> list[dict[str, Any]]:
        """Estado de conformidade por pessoa. Fonte ÚNICA — RuleEngine e
        ComplianceService leem daqui em vez de cada um construir o seu."""
        supported = self.supported_ppe()
        ppe_enabled = self.feature_manager.is_enabled("ppe")
        return self.person_matcher.build(
            detections,
            supported_ppe={key: key in supported for key in PPE_KEYS},
            enabled_ppe={key: ppe_enabled and self.feature_manager.is_enabled(key) for key in PPE_KEYS},
            risk_polygon=self.risk_polygon if self.feature_manager.is_enabled("risk_area") else None,
            frame_shape=frame_shape,
        )

    def _evaluate_ppe(self, detections: list[Detection], pose: PoseResult | None, people: list[dict[str, Any]]) -> list[RuleAlert]:
        if not self.feature_manager.is_enabled("ppe"):
            return []

        supported_ppe = self.supported_ppe()
        labels = {item.label for item in detections}
        person_present = bool(people) or bool(pose and pose.found)
        alerts: list[RuleAlert] = []

        if people:
            for person_state in people:
                for feature_key, (message, severity) in PPE_RULES.items():
                    if not self.feature_manager.is_enabled(feature_key) or feature_key not in supported_ppe:
                        continue
                    status = person_state["ppe"][feature_key]["status"]
                    if status == "missing":
                        rule, texto = f"missing_{feature_key}", message
                    elif status == "incorrect":
                        rule, texto = f"incorrect_{feature_key}", PPE_INCORRECT_MESSAGES[feature_key]
                    else:
                        continue
                    alerts.append(
                        RuleAlert(
                            rule=rule,
                            severity=severity,
                            message=f"{texto} — {person_state['label']}",
                            feature=feature_key,
                            metadata={
                                "person_id": person_state["id"],
                                "person_label": person_state["label"],
                                "person_box": person_state["box"],
                                "present_labels": sorted(labels),
                                "supported_ppe": sorted(supported_ppe),
                            },
                        )
                    )
            return alerts

        # Sem nenhuma caixa de pessoa: cai para o sinal global da pose, que não
        # distingue indivíduos (é uma pose só por frame).
        for feature_key, (message, severity) in PPE_RULES.items():
            if not self.feature_manager.is_enabled(feature_key) or feature_key not in supported_ppe:
                continue
            if person_present and feature_key not in labels:
                alerts.append(
                    RuleAlert(
                        rule=f"missing_{feature_key}",
                        severity=severity,
                        message=message,
                        feature=feature_key,
                        metadata={"subject": "global", "present_labels": sorted(labels), "supported_ppe": sorted(supported_ppe)},
                    )
                )
        return alerts

    def _evaluate_pose(self, poses: list[PoseResult], frame_shape: tuple[int, int, int]) -> list[RuleAlert]:
        """Avalia queda e postura POR POSE.

        Com uma pose por pessoa, cada alerta sai atribuido ao `person_id` do
        PersonTracker. Antes era sempre `subject: "global_pose"` e, com mais de
        uma pessoa em cena, "pessoa caida" nao dizia qual. Quando so existe a
        pose global (nenhuma caixa de pessoa), o comportamento antigo continua.
        """
        if not self.feature_manager.is_enabled("pose"):
            return []

        alerts: list[RuleAlert] = []
        for pose in poses:
            if not pose or not pose.found:
                continue
            sujeito = self._pose_subject(pose)
            rotulo = self._pose_label(pose)

            if self.feature_manager.is_enabled("falls") and self._is_fallen(pose, frame_shape):
                alerts.append(
                    RuleAlert(
                        rule="fallen_person",
                        severity="critical",
                        message=f"Pessoa caída detectada{rotulo}",
                        feature="falls",
                        metadata={**sujeito, "method": "torso_orientation"},
                    )
                )
            if self.feature_manager.is_enabled("posture") and self._is_bad_posture(pose, frame_shape):
                alerts.append(
                    RuleAlert(
                        rule="suspicious_posture",
                        severity="medium",
                        message=f"Postura suspeita detectada{rotulo}",
                        feature="posture",
                        metadata={**sujeito, "method": "shoulder_hip_alignment"},
                    )
                )
        return alerts

    @staticmethod
    def _pose_subject(pose: PoseResult) -> dict[str, Any]:
        if pose.person_id:
            return {"person_id": pose.person_id, "person_label": f"Pessoa {pose.track_id}"}
        return {"subject": "global_pose"}

    @staticmethod
    def _pose_label(pose: PoseResult) -> str:
        return f" — Pessoa {pose.track_id}" if pose.person_id and pose.track_id is not None else ""

    def _torso_px(self, pose: PoseResult, frame_shape: tuple[int, int, int]):
        """Centro dos ombros e dos quadris, em pixels. None se nao der pra confiar."""
        nomes = ("left_shoulder", "right_shoulder", "left_hip", "right_hip")
        for nome in nomes:
            landmark = pose.by_name(nome)
            if landmark is None or landmark.visibility < 0.4:
                return None
        pontos = {nome: pose.point_px(nome, frame_shape) for nome in nomes}
        if any(valor is None for valor in pontos.values()):
            return None
        ombro = (
            (pontos["left_shoulder"][0] + pontos["right_shoulder"][0]) / 2,
            (pontos["left_shoulder"][1] + pontos["right_shoulder"][1]) / 2,
        )
        quadril = (
            (pontos["left_hip"][0] + pontos["right_hip"][0]) / 2,
            (pontos["left_hip"][1] + pontos["right_hip"][1]) / 2,
        )
        return ombro, quadril

    def _is_fallen(self, pose: PoseResult, frame_shape: tuple[int, int, int]) -> bool:
        """Torso mais horizontal que vertical.

        Em PIXELS, nao em coordenadas normalizadas. Num frame 960x540, 0.1 em x
        sao 96 px e 0.1 em y sao 54 px — comparar os dois normalizados fazia o
        limiar declarado de 1.6 valer 2.84 na pratica. Com pose por pessoa seria
        pior: num recorte de pessoa em pe (80x300) o mesmo 1.6 viraria 0.43,
        marcando como caida qualquer pessoa levemente inclinada.
        """
        torso = self._torso_px(pose, frame_shape)
        if torso is None:
            return False
        (ombro_x, ombro_y), (quadril_x, quadril_y) = torso
        return abs(ombro_x - quadril_x) > abs(ombro_y - quadril_y) * FALLEN_TORSO_RATIO

    def _is_bad_posture(self, pose: PoseResult, frame_shape: tuple[int, int, int]) -> bool:
        """Cabeca projetada a frente do eixo dos ombros.

        Medido em fracao da ALTURA DO TORSO da propria pessoa, nao do frame:
        assim o limiar independe de a pessoa estar perto ou longe da camera — o
        que a versao anterior, normalizada pelo frame, nao garantia.
        """
        torso = self._torso_px(pose, frame_shape)
        nariz_lm = pose.by_name("nose")
        nariz = pose.point_px("nose", frame_shape)
        if torso is None or nariz is None or nariz_lm is None or nariz_lm.visibility < 0.4:
            return False

        (ombro_x, ombro_y), (_quadril_x, quadril_y) = torso
        altura_torso = abs(quadril_y - ombro_y)
        if altura_torso < 1.0:
            # Torso com menos de 1 px de altura: pessoa dobrada ao meio ou
            # deteccao ruim. Nos dois casos vale sinalizar.
            return True
        return abs(nariz[0] - ombro_x) / altura_torso > HEAD_FORWARD_RATIO

    def _evaluate_risk_area(self, people: list[dict[str, Any]], pose: PoseResult | None) -> list[RuleAlert]:
        if not self.feature_manager.is_enabled("risk_area"):
            return []

        # `people` já traz risk_area.status calculado pelo matcher — sem
        # recomputar ponto-em-polígono nem reordenar as detecções aqui.
        alerts = [
            RuleAlert(
                rule="risk_area_presence",
                severity="high",
                message=f"Pessoa em área de risco — {person['label']}",
                feature="risk_area",
                metadata={
                    "person_id": person["id"],
                    "person_label": person["label"],
                    "person_box": person["box"],
                    "point": person["risk_area"].get("point"),
                    "polygon": self.risk_polygon,
                },
            )
            for person in people
            if person.get("risk_area", {}).get("status") == "inside"
        ]
        if alerts:
            return alerts

        if pose and pose.found:
            left_hip = pose.by_name("left_hip")
            right_hip = pose.by_name("right_hip")
            if left_hip and right_hip:
                point = ((left_hip.x + right_hip.x) / 2, (left_hip.y + right_hip.y) / 2)
                if self._point_in_polygon(point, self.risk_polygon):
                    return [
                        RuleAlert(
                            rule="risk_area_presence",
                            severity="high",
                            message="Pessoa em área de risco",
                            feature="risk_area",
                            metadata={
                                "subject": "global_pose",
                                "point": {"x": round(point[0], 4), "y": round(point[1], 4)},
                                "polygon": self.risk_polygon,
                            },
                        )
                    ]
        return []

    @staticmethod
    def _point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
        return point_in_polygon(point, polygon)
