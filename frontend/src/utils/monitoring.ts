import type { MonitorStatus } from "../api/types";
import type { PpeKey } from "../api/ppe";

export function monitoringProblem(status: MonitorStatus | null): string | null {
  if (!status) return "Sem conexão com o servidor";
  if (!status.running) return "Monitoramento parado";
  if (status.video?.modo === "fixture") return "Vídeo de demonstração";
  if (status.diagnostico?.fonte_sem_imagem) return "Imagem sem visibilidade";
  if (status.last_error) return "Falha na análise";
  if (status.video?.state !== "live") return "Câmera sem sinal";
  if (status.frame_counter === 0 || status.diagnostico?.imagem_desatualizada) return "Aguardando imagem atualizada";
  return null;
}

export function ppeChipState(status: MonitorStatus | null, key: PpeKey, violated: boolean): "ok" | "miss" | "waiting" {
  if (monitoringProblem(status)) return "waiting";
  if (violated) return "miss";
  if (!status?.model?.supported_ppe?.[key]) return "waiting";
  const check = status.compliance?.ppe?.[key];
  if (check?.status === "missing" || check?.status === "critical") return "miss";
  return check?.status === "ok" ? "ok" : "waiting";
}
