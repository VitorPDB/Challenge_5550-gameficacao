import { useEffect, useState } from "react";
import { Panel } from "./common";
import { PPE_KEYS, PPE_LABELS } from "../api/ppe";
import type { PpeKey } from "../api/ppe";
import { createMission, fetchMissions, fetchPrevention, fetchScenario, fetchScenarioList, reviewMission, savePrevention,
  submitMission, uploadScenario } from "../api/prevention";
import type { MissionResponse, PreventionConfig, PreventionResponse, PreventionZone, ScenarioRun } from "../api/prevention";
import { useDashboardStore } from "../store/dashboardStore";
import { monitoringProblem } from "../utils/monitoring";

const REASONS: Record<string, string> = {
  dark_image: "Imagem escura", overexposed_image: "Imagem excessivamente clara",
  low_contrast_possible_obstruction: "Pouco contraste: verifique iluminação e possível obstrução",
  person_too_small_for_detailed_ppe: "Pessoa pequena na imagem para avaliar detalhes de EPI",
};
const CHECKS = [
  ["enabled", "Ativar prevenção"], ["trajectory", "Prever aproximação de zonas"],
  ["proximity", "Observar convergência entre pessoas"], ["ergonomics", "Acumular exposição postural"],
  ["coverage", "Verificar qualidade da cobertura"],
] as const;
const NUMBERS = [
  ["horizon_s", "Horizonte de previsão (s)", 0.2, 5], ["proximity_ratio", "Proximidade relativa à altura na imagem", 0.05, 2],
  ["exposure_threshold_s", "Limiar de exposição (s)", 1, 600], ["exposure_window_s", "Janela de exposição (s)", 10, 1800],
] as const;

export function PreventionPanel({ cameraId }: { cameraId: number }) {
  const mode = useDashboardStore(s => s.mode);
  const [data, setData] = useState<PreventionResponse | null>(null);
  const [draft, setDraft] = useState<PreventionConfig | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    let active = true;
    setData(null); setDraft(null); setError(""); setMessage("");
    const refresh = async () => {
      try {
        const response = await fetchPrevention(cameraId);
        if (active) { setData(response); setDraft(old => old ?? response.config); setError(""); }
      } catch (err) { if (active) { setData(null); setError(String(err)); } }
    };
    void refresh();
    const timer = setInterval(refresh, 2500);
    return () => { active = false; clearInterval(timer); };
  }, [cameraId]);
  const problem = data?.status ? monitoringProblem(data.status) : "Sem observação atual";
  const current = problem ? null : data?.state;
  return <Panel title="Prevenção" description="Trajetórias e exposição estimadas por câmera; identidade limitada à sessão.">
    {error && <p role="alert">{error}</p>}
    {message && <p role="status">{message}</p>}
    {problem && <p role="status">{problem}. Resultados anteriores não confirmam a situação atual.</p>}
    {current && <>
      <p>Cobertura: {current.coverage.status}. {(current.coverage.reasons ?? []).map(r => REASONS[r] ?? r).join(" · ")}</p>
      {!current.enabled && <p>Prevenção desativada.</p>}
      {current.observation_reused && <p>Última observação reaproveitada; aguardando nova detecção.</p>}
      {(current.warnings ?? []).map((warning, i) => <p key={i} role="status">{warning}</p>)}
      {current.people.map(person => <div key={person.person_id} className="card">
        <strong>{person.person_id.replace("person_", "Pessoa ")}</strong>
        <p>Exposição postural observada: {person.exposure_s.toFixed(1)} s.</p>
        {person.prediction_status !== "estimated" && <p>Histórico insuficiente ou movimento incerto para previsão.</p>}
        {person.zones.filter(z => z.inside || z.approaching).map(zone => <p key={zone.name}>
          {zone.name}: {zone.inside ? "dentro da zona" : `entrada estimada em ${zone.eta_s} s`}.
          {zone.unconfirmed_ppe.length > 0 && ` EPIs não confirmados: ${zone.unconfirmed_ppe.join(", ")}.`}
        </p>)}
      </div>)}
      {current.encounters.map((encounter, i) => <p key={i}>
        Convergência: {encounter.people.join(" e ")}, em aproximadamente {encounter.eta_s} s. Distância relativa à imagem.
      </p>)}
    </>}
    {mode !== "operator" && draft && <fieldset disabled={saving}>
      <legend>Configuração desta câmera</legend>
      {CHECKS.map(([key, label]) => <label key={key}><input type="checkbox" checked={draft[key]}
        onChange={e => setDraft({ ...draft, [key]: e.target.checked })} />{label}</label>)}
      {NUMBERS.map(([key, label, min, max]) => <label key={key}>{label}<input type="number" min={min} max={max} step="0.1"
        value={draft[key]} onChange={e => setDraft({ ...draft, [key]: Number(e.target.value) })} /></label>)}
      <ZoneEditor cameraId={cameraId} zones={draft.zones} onChange={zones => setDraft({ ...draft, zones })} />
      <button type="button" disabled={saving} onClick={async () => {
        setSaving(true); setMessage("");
        try { await savePrevention(cameraId, draft); setMessage("Configuração salva; histórico de previsão reiniciado."); }
        catch (err) { setError(String(err)); }
        finally { setSaving(false); }
      }}>{saving ? "Salvando…" : "Salvar prevenção"}</button>
    </fieldset>}
    <p>A projeção assume continuidade do movimento. Não mede metros, não identifica pessoas entre câmeras e não é diagnóstico ergonômico.</p>
  </Panel>;
}

function ZoneEditor({ cameraId, zones, onChange }: { cameraId: number; zones: PreventionZone[]; onChange: (z: PreventionZone[]) => void }) {
  const [points, setPoints] = useState<number[][]>([]);
  const [name, setName] = useState("");
  const [required, setRequired] = useState<PpeKey[]>([]);
  const [restricted, setRestricted] = useState(true);
  useEffect(() => { setPoints([]); setName(""); setRequired([]); }, [cameraId]);
  return <div>
    <p>Desenhe uma zona clicando nos seus vértices sobre a imagem corrigida da câmera.</p>
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="Editor de zonas de prevenção"
      style={{ width: "100%", height: 220, cursor: "crosshair" }} onClick={e => {
        if (points.length >= 32) return;
        const rect = e.currentTarget.getBoundingClientRect();
        setPoints([...points, [(e.clientX-rect.left)/rect.width, (e.clientY-rect.top)/rect.height]]);
      }}>
      <image href={`/api/cameras/${cameraId}/frame.jpg`} width="100" height="100" preserveAspectRatio="none" />
      {zones.map(zone => <polygon key={zone.id} points={zone.polygon.map(p => `${p[0]*100},${p[1]*100}`).join(" ")}
        fill="rgba(255,165,0,.15)" stroke="orange" strokeWidth="0.5" />)}
      <polyline points={points.map(p => `${p[0]*100},${p[1]*100}`).join(" ")} fill="rgba(0,150,255,.2)" stroke="cyan" strokeWidth="0.5" />
      {points.map((p, i) => <circle key={i} cx={p[0]*100} cy={p[1]*100} r="0.8" fill="cyan" />)}
    </svg>
    <button type="button" onClick={() => setPoints(points.slice(0, -1))}>Desfazer ponto</button>
    <button type="button" onClick={() => setPoints([])}>Limpar desenho</button>
    <label>Nome da zona<input maxLength={80} value={name} onChange={e => setName(e.target.value)} /></label>
    <label><input type="checkbox" checked={restricted} onChange={e => setRestricted(e.target.checked)} />Zona restrita mesmo com EPI</label>
    <p>EPIs exigidos para entrar:</p>
    {PPE_KEYS.map(key => <label key={key}><input type="checkbox" checked={required.includes(key)} onChange={e =>
      setRequired(e.target.checked ? [...required, key] : required.filter(k => k !== key))} />{PPE_LABELS[key]}</label>)}
    <button type="button" disabled={points.length < 3 || !name.trim() || zones.length >= 12} onClick={() => {
      onChange([...zones, { id: crypto.randomUUID(), name: name.trim(), polygon: points, required_ppe: required, restricted }]);
      setPoints([]); setName(""); setRequired([]);
    }}>Adicionar zona à configuração</button>
    {zones.map(zone => <p key={zone.id}>{zone.name} · {zone.required_ppe.map(k => PPE_LABELS[k]).join(", ") || "Sem EPI adicional"}
      <button type="button" onClick={() => { setPoints(zone.polygon); setName(zone.name); setRequired(zone.required_ppe);
        setRestricted(zone.restricted); onChange(zones.filter(z => z.id !== zone.id)); }}>Editar</button>
      <button type="button" onClick={() => onChange(zones.filter(z => z.id !== zone.id))}>Remover</button></p>)}
  </div>;
}

export function MissionsPanel({ cameraId }: { cameraId: number }) {
  const mode = useDashboardStore(s => s.mode);
  const [data, setData] = useState<MissionResponse | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [team, setTeam] = useState("");
  const [title, setTitle] = useState("");
  const [category, setCategory] = useState("prevention");
  const [evidence, setEvidence] = useState<Record<number, string>>({});
  const [teamFilter, setTeamFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [feedback, setFeedback] = useState("");
  const refresh = async () => setData(await fetchMissions(cameraId));
  useEffect(() => {
    let active = true; setData(null); setError(""); setEvidence({}); setTeamFilter(""); setFeedback("");
    fetchMissions(cameraId).then(value => { if (active) setData(value); }).catch(err => { if (active) setError(String(err)); });
    return () => { active = false; };
  }, [cameraId]);
  const act = async (operation: () => Promise<unknown>, success = "") => {
    setBusy(true); setError(""); setFeedback("");
    try { await operation(); await refresh(); setFeedback(success); } catch (err) { setError(String(err)); } finally { setBusy(false); }
  };
  const statusLabels: Record<string, string> = { open: "A fazer", submitted: "Aguardando revisão", verified: "Concluída e verificada" };
  const visibleTeams = data?.teams.filter(t => !teamFilter || t.team === teamFilter) ?? [];
  const visibleMissions = data?.missions.filter(m => (!teamFilter || m.team === teamFilter)
    && (statusFilter === "all" || m.status === statusFilter)) ?? [];
  return <Panel title="Jornada de prevenção" description="Missões reais, evolução coletiva e conquistas por ações verificadas.">
    {error && <p role="alert">{error}</p>}
    {feedback && <p role="status">{feedback}</p>}
    <button type="button" disabled={busy} onClick={() => void act(async () => {})}>Atualizar missões</button>
    <label>Equipe<select value={teamFilter} onChange={e => setTeamFilter(e.target.value)}>
      <option value="">Todas as equipes deste setor</option>{data?.teams.map(t => <option key={t.team} value={t.team}>{t.team}</option>)}
    </select></label>
    {data && data.teams.length === 0 && <p>Comece criando uma missão para sua equipe. A jornada evolui quando uma ação é verificada.</p>}
    <div className="journey-teams">{visibleTeams.map(t => <section key={t.team} className="journey-team" aria-label={`Jornada de ${t.team}`}>
      <div className="journey-heading"><h3>{t.team}</h3><span className="chip">Nível {t.level}</span></div>
      <p><strong>{t.level_name}</strong> · {t.points} XP</p>
      <progress className="journey-progress" max={1} value={t.level_progress} aria-label={`Progresso de nível de ${t.team}`} />
      <p>{t.next_level_points === null ? "Nível máximo alcançado. Continue contribuindo com ações preventivas."
        : `Faltam ${t.points_to_next_level} XP para o próximo nível (${t.next_level_points} XP).`}</p>
      <p>{t.verified} ações verificadas · {t.awaiting_review} aguardando revisão · {t.open} a fazer</p>
      <h4>Metas dos últimos sete dias</h4>
      {t.challenges.map(challenge => <div key={challenge.id} className="journey-goal">
        <strong>{challenge.name} · {challenge.progress}/{challenge.target}</strong><p>{challenge.description}</p>
        <progress className="journey-progress" max={challenge.target} value={challenge.progress} aria-label={`${challenge.name}: ${t.team}`} />
        {challenge.progress >= challenge.target && <span className="chip">Meta alcançada</span>}
      </div>)}
      <details><summary>Conquistas · {t.achievements.length}/{t.achievement_details.length} desbloqueadas</summary>
        <div className="journey-badges">{t.achievement_details.map(badge => <div key={badge.id}
          className={`journey-badge ${badge.unlocked ? "earned" : "locked"}`}>
          <strong>{badge.name}</strong><p>{badge.description}</p>
          <span>{badge.unlocked ? "Conquistada" : `Em progresso: ${badge.progress}/${badge.target}`}</span>
        </div>)}</div>
      </details>
      <p>Frentes trabalhadas: {t.categories.map(c => `${c.label} (${c.verified})`).join(" · ") || "Aguardando primeira ação verificada"}</p>
    </section>)}</div>
    {mode !== "operator" && <fieldset disabled={busy}>
      <legend>Nova missão da equipe</legend>
      <label>Equipe<input list={`mission-teams-${cameraId}`} value={team} maxLength={120} onChange={e => setTeam(e.target.value)} /></label>
      <datalist id={`mission-teams-${cameraId}`}>{data?.teams.map(t => <option key={t.team} value={t.team} />)}</datalist>
      <label>Título<input value={title} maxLength={200} onChange={e => setTitle(e.target.value)} /></label>
      <label>Categoria<select value={category} onChange={e => setCategory(e.target.value)}>
        {data?.categories.map(c => <option key={c.key} value={c.key}>{c.label}</option>)}
      </select></label>
      {data?.suggestions.map(s => <button type="button" key={s.category} onClick={() => { setTitle(s.title); setCategory(s.category); }}>
        {s.title} ({s.occurrences} ocorrências)</button>)}
      <button type="button" disabled={!team.trim() || !title.trim() || !category.trim()}
        onClick={() => void act(() => createMission(cameraId, team, category, title), "Missão criada. Realize a ação e envie a evidência para revisão.")}>Criar missão</button>
    </fieldset>}
    <label>Missões<select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
      <option value="all">Todas</option><option value="open">A fazer</option><option value="submitted">Aguardando revisão</option>
      <option value="verified">Concluídas</option></select></label>
    {data && visibleMissions.length === 0 && <p>Nenhuma missão neste filtro.</p>}
    {visibleMissions.map(m => <div key={m.id} className="card"><strong>{m.title}</strong><p>{m.team} · {statusLabels[m.status] ?? m.status}
      · {m.status === "verified" ? `${m.points} XP conquistados` : "100 XP após verificação"}</p>
      {m.evidence && <p>Ação informada: {m.evidence}</p>}
      {m.status === "open" && <><label>Descreva a ação realizada<textarea maxLength={1000} value={evidence[m.id] ?? ""}
        onChange={e => setEvidence({ ...evidence, [m.id]: e.target.value })} /></label>
        <button type="button" disabled={busy || (evidence[m.id]?.trim().length ?? 0) < 10}
          onClick={() => void act(() => submitMission(m.id, evidence[m.id]), "Ação enviada. Os pontos serão liberados após revisão de outro supervisor.")}>Enviar para revisão</button></>}
      {mode === "supervisor" && m.status === "submitted" && <>
        <button type="button" disabled={busy} onClick={() => void act(() => reviewMission(m.id, true), "Ação verificada: 100 XP para a equipe. Jornada e conquistas atualizadas.")}>Verificar ação</button>
        <button type="button" disabled={busy} onClick={() => void act(() => reviewMission(m.id, false), "Missão devolvida para revisão, sem concessão de pontos.")}>Solicitar revisão</button>
      </>}
    </div>)}
    <p>{data?.basis}</p>
  </Panel>;
}

export function ScenarioPanel({ cameraId }: { cameraId: number }) {
  const [file, setFile] = useState<File | null>(null);
  const [run, setRun] = useState<ScenarioRun | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [alternative, setAlternative] = useState(3);
  const [selected, setSelected] = useState(0);
  const [recent, setRecent] = useState<ScenarioRun[]>([]);
  useEffect(() => { setRun(null); setFile(null); setError(""); setSelected(0); }, [cameraId]);
  useEffect(() => {
    let active = true;
    fetchScenarioList(cameraId).then(r => { if (active) setRecent(r.runs); }).catch(err => { if (active) setError(String(err)); });
    return () => { active = false; };
  }, [cameraId, run?.status]);
  useEffect(() => {
    if (!run || !["queued", "running"].includes(run.status)) return;
    let active = true;
    const timer = setInterval(() => fetchScenario(run.id).then(r => { if (active) setRun(r); })
      .catch(err => { if (active) setError(String(err)); }), 1500);
    return () => { active = false; clearInterval(timer); };
  }, [run?.id, run?.status]);
  return <Panel title="Laboratório de cenários" description="Reprocessamento offline com detectores reais; não gera alertas ou pontos operacionais.">
    <p>Salve as zonas em Prevenção antes de enviar o vídeo. Use o mesmo enquadramento e rotação da câmera.
      Uma execução por vez, até 120 s de vídeo e 600 observações. Pesos locais são necessários.</p>
    <p>O laboratório compartilha o recurso de inferência com as câmeras e pode reduzir seu desempenho durante o teste.</p>
    {error && <p role="alert">{error}</p>}
    {recent.length > 0 && <label>Reabrir execução recente<select value={run?.id ?? ""} onChange={async e => {
      if (!e.target.value) return;
      try { setSelected(0); setRun(await fetchScenario(e.target.value)); } catch (err) { setError(String(err)); }
    }}><option value="">Selecione</option>{recent.map(r => <option key={r.id} value={r.id}>{r.id.slice(0, 8)} · {r.status}</option>)}</select></label>}
    <label>Vídeo MP4 (até 128 MB)<input type="file" accept="video/mp4,.mp4" onChange={e => setFile(e.target.files?.[0] ?? null)} /></label>
    <label>Comparar com horizonte alternativo (s)<input type="number" min="0.2" max="5" step="0.1" value={alternative}
      onChange={e => setAlternative(Number(e.target.value))} /></label>
    <button type="button" disabled={!file || busy || (run !== null && ["queued", "running"].includes(run.status))} onClick={async () => {
      if (!file) return;
      setBusy(true); setError(""); setSelected(0);
      try { const { config } = await fetchPrevention(cameraId);
        setRun(await uploadScenario(cameraId, file, [config, { ...config, horizon_s: alternative }])); }
      catch (err) { setError(String(err)); } finally { setBusy(false); }
    }}>{busy ? "Enviando…" : "Processar e comparar"}</button>
    {run && <><p>Execução {run.id} · {run.status} · {run.progress?.analyzed_frames ?? run.result?.analyzed_frames ?? 0} observações</p>
      {run.error && <p role="alert">{run.error}</p>}
      {run.result && <>
        {run.result.truncated && <p>Processamento limitado: relatório cobre somente o trecho analisado.</p>}
        {run.result.comparisons.map((comparison, i) => <div key={i} className="card"><strong>Configuração {i+1}</strong>
          <p>{comparison.entries.length} entradas observadas · {comparison.anticipated_entries} antecipadas</p>
          <p>Antecedências (s): {comparison.lead_times_s.join(", ") || "Nenhuma"}</p>
          <p>{comparison.unconfirmed_warning_episodes} avisos sem entrada observada no horizonte · {comparison.censored_warnings} inconclusivos no fim do vídeo</p>
          <p>{comparison.basis}</p>
        </div>)}
        <button type="button" onClick={() => {
          const url = URL.createObjectURL(new Blob([JSON.stringify(run.result, null, 2)], { type: "application/json" }));
          const link = document.createElement("a"); link.href = url; link.download = `scenario-${run.id}.json`; link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        }}>Exportar relatório JSON</button>
        {run.result.samples.length > 0 && <>
          <label>Observação<select value={selected} onChange={e => setSelected(Number(e.target.value))}>
            {run.result.samples.map((sample, i) => <option key={i} value={i}>{sample.at_s.toFixed(1)} s</option>)}
          </select></label>
          <img style={{ width: "100%" }} src={`/api/scenarios/${run.id}/images/${run.result.samples[selected].image}`}
            alt={`Trajetórias e zonas no laboratório em ${run.result.samples[selected].at_s} s`} />
          {run.result.samples[selected].states.map((state, i) => <p key={i}>Configuração {i+1}: {(state.warnings ?? []).join(" · ") || "Sem avisos nesta observação"}</p>)}
        </>}
      </>}
    </>}
  </Panel>;
}
