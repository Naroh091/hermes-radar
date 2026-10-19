/**
 * Radar — Hermes Desktop panel (unified package: this file + dashboard/plugin_api.py).
 *
 * One page under /radar: the tasks Radar anticipates with their proposals (same buttons as Telegram),
 * drafts, context of the mail thread, the task report embedded in a sandboxed frame, detected patterns
 * and the owner's preferences. All data comes from the plugin's own backend (ctx.rest) and refreshes on
 * the /events socket or every 30 s.
 */
import {
  ROUTES_AREA, SIDEBAR_NAV_AREA, SandboxedFrame, Badge, Button, ScrollArea, Separator, EmptyState, Skeleton,
  useQuery, queryClient, relativeTime, cn,
} from '@hermes/plugin-sdk'
import { jsx, jsxs, Fragment } from 'react/jsx-runtime'
import { useState, useEffect, useMemo } from 'react'

const BOARD_KEY = ['radar', 'board']
let rest = null

const TEXT = {
  es: {
    title: 'Radar', empty: 'Nada en el radar ahora mismo.', loading: 'Cargando…', error: 'No se pudo cargar el panel: ',
    tasks: 'Tareas', patterns: 'Patrones', preferences: 'Preferencias', proposals: 'Propuestas', checklist: 'Pendiente',
    draft: 'Propongo responder', context: 'Correo', later: 'Más tarde', mute: 'No avisar', done: 'Ya está hecho', report: 'Informe',
    hideReport: 'Ocultar informe', waiting: 'en espera', executing: 'en marcha', nothingPending: 'Sin propuestas pendientes.',
    due: 'Vence', jobsRunning: 'job en marcha', forget: 'Olvidar', dismiss: 'Descartar', add: 'Guardar', instruction: 'Instrucción…',
    sender: 'remitente (opcional)', effects: { silent: 'no avisar', digest: 'solo resumen', notify: 'avisar siempre', prefer: 'preferir' },
    status: { needs_user: 'Te toca', in_progress: 'En marcha', open: 'Abiertas', waiting: 'Esperando', snoozed: 'Pospuestas' },
    kinds: { trip: 'Viaje', purchase: 'Compra', work_item: 'Trabajo', commitment: 'Compromiso', check: 'Comprobación', routine: 'Rutina', other: 'Otra' },
    sent: 'Hecho: ', notDone: 'No ejecutado: ', mode: 'modo', trust: 'confianza', refresh: 'Actualizar', detail: 'Detalle', history: 'Historial',
    noPatterns: 'Aún no hay patrones: hacen falta unas semanas de señales.', noPrefs: 'Sin preferencias guardadas.',
  },
  en: {
    title: 'Radar', empty: 'Nothing on the radar right now.', loading: 'Loading…', error: 'The panel could not load: ',
    tasks: 'Tasks', patterns: 'Patterns', preferences: 'Preferences', proposals: 'Proposals', checklist: 'Pending',
    draft: 'Suggested reply', context: 'Email', later: 'Later', mute: 'Mute', done: 'Already done', report: 'Report',
    hideReport: 'Hide report', waiting: 'waiting', executing: 'running', nothingPending: 'No pending proposals.',
    due: 'Due', jobsRunning: 'job running', forget: 'Forget', dismiss: 'Dismiss', add: 'Save', instruction: 'Instruction…',
    sender: 'sender (optional)', effects: { silent: 'never notify', digest: 'digest only', notify: 'always notify', prefer: 'prefer' },
    status: { needs_user: 'Your call', in_progress: 'In progress', open: 'Open', waiting: 'Waiting', snoozed: 'Snoozed' },
    kinds: { trip: 'Trip', purchase: 'Purchase', work_item: 'Work item', commitment: 'Commitment', check: 'Check', routine: 'Routine', other: 'Other' },
    sent: 'Done: ', notDone: 'Not executed: ', mode: 'mode', trust: 'trust', refresh: 'Refresh', detail: 'Detail', history: 'History',
    noPatterns: 'No patterns yet: a few weeks of signals are needed.', noPrefs: 'No preferences saved.',
  },
}
function ago(value) {
  const ms = value ? Date.parse(value) : NaN
  if (!Number.isFinite(ms) || typeof relativeTime !== 'function') return ''
  try { return relativeTime(ms) } catch (_err) { return '' }
}

const KIND_ICON = { trip: '✈️', purchase: '🛒', work_item: '🛠️', commitment: '📌', check: '🔎', routine: '🔁', other: '•' }
const STATUS_ORDER = ['needs_user', 'in_progress', 'open', 'waiting', 'snoozed']

function useBoard() {
  return useQuery({ queryKey: BOARD_KEY, queryFn: () => rest('/board'), refetchInterval: 30000, staleTime: 5000 })
}

function invalidate() {
  queryClient.invalidateQueries({ queryKey: BOARD_KEY })
}

async function post(path, body) {
  const result = await rest(path, { method: 'POST', body })
  invalidate()
  return result
}

function Section({ title, children, right }) {
  return jsxs('div', {
    className: 'mb-4',
    children: [
      jsxs('div', {
        className: 'mb-1 flex items-center justify-between text-xs font-medium uppercase tracking-wide text-(--ui-text-tertiary)',
        children: [jsx('span', { children: title }), right || null],
      }),
      children,
    ],
  })
}

function Pill({ children, tone }) {
  return jsx(Badge, { variant: tone === 'accent' ? 'default' : 'secondary', className: 'mr-1 text-[10px]', children })
}

function TaskRow({ task, selected, onSelect, t }) {
  const pending = (task.proposals || []).filter((p) => p.status === 'pending' || p.status === 'sent').length
  const waiting = (task.proposals || []).some((p) => p.status === 'waiting' || p.status === 'executing')
  return jsxs('button', {
    type: 'button',
    onClick: () => onSelect(task.id),
    className: cn(
      'flex w-full flex-col gap-0.5 rounded-md border px-2 py-1.5 text-left text-sm transition-colors',
      selected ? 'border-(--ui-accent) bg-(--ui-accent)/10' : 'border-(--ui-stroke-secondary) hover:bg-(--ui-stroke-secondary)/30',
    ),
    children: [
      jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx('span', { className: 'shrink-0', children: KIND_ICON[task.kind] || KIND_ICON.other }),
          jsx('span', { className: 'truncate font-medium', children: task.title }),
        ],
      }),
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-1 text-[11px] text-(--ui-text-tertiary)',
        children: [
          pending ? jsx(Pill, { tone: 'accent', children: `${pending} ${t.proposals.toLowerCase()}` }) : null,
          waiting ? jsx(Pill, { children: t.waiting }) : null,
          task.draft ? jsx(Pill, { children: t.draft }) : null,
          task.entities && task.entities.pr ? jsx(Pill, { children: 'PR' }) : null,
          task.jobs_running ? jsx(Pill, { children: `${task.jobs_running} ${t.jobsRunning}` }) : null,
          task.due_at ? jsx('span', { children: `${t.due} ${String(task.due_at).slice(0, 10)}` }) : null,
          jsx('span', { children: ago(task.updated_at) }),
        ],
      }),
    ],
  })
}

function Context({ context, t }) {
  if (!context || !(context.sender || context.excerpt)) return null
  const head = [context.sender, context.when, context.count > 1 ? `${context.count} msgs` : null].filter(Boolean).join(' · ')
  return jsxs('div', {
    className: 'rounded-md border border-(--ui-stroke-secondary) p-2 text-xs',
    children: [
      jsxs('div', { className: 'mb-1 text-(--ui-text-secondary)', children: [jsx('b', { children: `${t.context}: ` }), head] }),
      context.subject ? jsx('div', { className: 'italic text-(--ui-text-tertiary)', children: context.subject }) : null,
      context.excerpt ? jsx('blockquote', { className: 'mt-1 border-l-2 border-(--ui-stroke-secondary) pl-2 text-(--ui-text-secondary)', children: context.excerpt }) : null,
    ],
  })
}

function Proposal({ proposal, t, onFeedback }) {
  const [busy, setBusy] = useState(false)
  const [detail, setDetail] = useState('')
  const options = proposal.options && proposal.options.length ? proposal.options : [{ id: 'do', label: '✅' }, { id: 'no', label: '❌' }]
  const act = async (option) => {
    setBusy(true)
    try {
      const result = await post(`/proposals/${proposal.id}/respond`, { option })
      if (result.outcome === 'review' && result.detail) setDetail(result.detail)
      else onFeedback(result)
    } catch (err) {
      onFeedback({ outcome: 'error', text: String(err && err.message ? err.message : err) })
    } finally {
      setBusy(false)
    }
  }
  const inFlight = proposal.status === 'waiting' || proposal.status === 'executing' || proposal.status === 'accepted'
  return jsxs('div', {
    className: 'mb-2 rounded-md border border-(--ui-stroke-secondary) p-2 text-sm',
    children: [
      jsxs('div', {
        className: 'flex items-start justify-between gap-2',
        children: [
          jsxs('div', {
            children: [
              jsx('div', { className: 'font-medium', children: proposal.title }),
              proposal.reason ? jsx('div', { className: 'text-xs text-(--ui-text-secondary)', children: proposal.reason }) : null,
            ],
          }),
          jsxs('div', {
            className: 'flex shrink-0 flex-col items-end gap-0.5 text-[10px] text-(--ui-text-tertiary)',
            children: [
              jsx(Pill, { children: proposal.action_class }),
              inFlight ? jsx('span', { children: proposal.status === 'waiting' ? `${t.waiting}${proposal.wait_reason ? ': ' + proposal.wait_reason : ''}` : t.executing }) : null,
            ],
          }),
        ],
      }),
      proposal.draft ? jsxs('div', {
        className: 'mt-1 rounded bg-(--ui-stroke-secondary)/30 p-2 text-xs whitespace-pre-wrap',
        children: [jsx('b', { children: `${t.draft}: ` }), proposal.draft, proposal.draft_truncated ? '…' : ''],
      }) : null,
      detail ? jsx('div', { className: 'mt-1 rounded border border-(--ui-stroke-secondary) p-2 text-xs whitespace-pre-wrap', children: detail }) : null,
      !inFlight ? jsx('div', {
        className: 'mt-2 flex flex-wrap gap-1',
        children: options.slice(0, 3).map((option, index) => jsx(Button, {
          size: 'sm', variant: index === 0 ? 'default' : 'outline', disabled: busy, onClick: () => act(option.id), children: option.label,
        }, option.id)),
      }) : null,
    ],
  })
}

function ReportFrame({ taskId, t }) {
  const [open, setOpen] = useState(false)
  const [src, setSrc] = useState('')
  useEffect(() => {
    let alive = true
    if (open && !src) {
      rest(`/tasks/${taskId}/report`).then((r) => { if (alive) setSrc(r.data_url || '') }).catch(() => {})
    }
    return () => { alive = false }
  }, [open, taskId])
  return jsxs('div', {
    children: [
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setOpen(!open), children: open ? t.hideReport : t.report }),
      open && src ? jsx('div', { className: 'mt-2 h-[480px] overflow-hidden rounded-md border border-(--ui-stroke-secondary)',
        children: jsx(SandboxedFrame, { src, title: `Radar ${taskId}`, className: 'h-full w-full' }) }) : null,
    ],
  })
}

function TaskDetail({ task, t, onFeedback }) {
  const proposals = task.proposals || []
  const missing = (task.checklist || []).filter((c) => c.status === 'missing')
  const taskAction = (action) => post(`/tasks/${task.id}/action`, { action }).then(onFeedback).catch((err) => onFeedback({ outcome: 'error', text: String(err) }))
  return jsxs('div', {
    className: 'flex flex-col gap-3',
    children: [
      jsxs('div', {
        children: [
          jsxs('div', { className: 'text-base font-semibold', children: [KIND_ICON[task.kind] || '', ' ', task.title] }),
          jsxs('div', {
            className: 'text-xs text-(--ui-text-tertiary)',
            children: [
              (t.kinds[task.kind] || task.kind), ' · ', (t.status[task.status] || task.status),
              task.due_at ? ` · ${t.due} ${String(task.due_at).slice(0, 16).replace('T', ' ')}` : '',
              task.entities && task.entities.repo ? ` · ${task.entities.repo}` : '',
            ],
          }),
        ],
      }),
      task.summary ? jsx('p', { className: 'text-sm text-(--ui-text-secondary)', children: task.summary }) : null,
      jsx(Context, { context: task.context, t }),
      missing.length ? jsxs('div', { className: 'text-xs', children: [jsx('b', { children: `${t.checklist}: ` }), missing.map((c) => c.item).join('; ')] }) : null,
      task.entities && task.entities.pr ? jsx('a', { className: 'text-xs underline', href: task.entities.pr, target: '_blank', rel: 'noreferrer', children: task.entities.pr }) : null,
      jsx(Separator, {}),
      jsxs(Section, {
        title: t.proposals,
        children: [proposals.length ? proposals.map((p) => jsx(Proposal, { proposal: p, t, onFeedback }, p.id)) : jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: t.nothingPending })],
      }),
      task.draft && !proposals.some((p) => p.draft) ? jsxs('div', {
        className: 'rounded bg-(--ui-stroke-secondary)/30 p-2 text-xs whitespace-pre-wrap',
        children: [jsx('b', { children: `${t.draft}: ` }), task.draft],
      }) : null,
      jsxs('div', {
        className: 'flex flex-wrap gap-1',
        children: [
          jsx(Button, { size: 'sm', variant: 'outline', onClick: () => taskAction('later'), children: `⏰ ${t.later}` }),
          jsx(Button, { size: 'sm', variant: 'outline', onClick: () => taskAction('mute'), children: `🔕 ${t.mute}` }),
          jsx(Button, { size: 'sm', variant: 'outline', onClick: () => taskAction('done'), children: `✔️ ${t.done}` }),
        ],
      }),
      jsx(ReportFrame, { taskId: task.id, t }),
    ],
  })
}

function Patterns({ patterns, t, onFeedback }) {
  if (!patterns.length) return jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: t.noPatterns })
  return jsx('div', {
    className: 'flex flex-col gap-1',
    children: patterns.map((p) => jsxs('div', {
      className: 'flex items-start justify-between gap-2 rounded-md border border-(--ui-stroke-secondary) p-2 text-xs',
      children: [
        jsxs('div', {
          children: [
            jsx('div', { className: 'font-medium', children: p.name || p.summary }),
            jsx('div', { className: 'text-(--ui-text-tertiary)', children: `${p.kind} · ${p.status} · x${p.support}` }),
            p.description ? jsx('div', { className: 'text-(--ui-text-secondary)', children: p.description }) : null,
          ],
        }),
        p.status === 'proposed' || p.status === 'candidate' ? jsx(Button, {
          size: 'sm', variant: 'ghost', onClick: () => post(`/patterns/${p.id}/dismiss`, {}).then(onFeedback).catch(() => {}), children: t.dismiss,
        }) : null,
      ],
    }, p.id)),
  })
}

function Preferences({ preferences, t, onFeedback }) {
  const [instruction, setInstruction] = useState('')
  const [effect, setEffect] = useState('silent')
  const [sender, setSender] = useState('')
  const save = async () => {
    if (!instruction.trim()) return
    const scope = sender.trim() ? { sender: sender.trim().toLowerCase() } : {}
    try {
      await post('/preferences', { instruction: instruction.trim(), effect, scope })
      setInstruction(''); setSender('')
    } catch (err) {
      onFeedback({ outcome: 'error', text: String(err) })
    }
  }
  return jsxs('div', {
    className: 'flex flex-col gap-2 text-xs',
    children: [
      preferences.length ? preferences.map((p) => jsxs('div', {
        className: 'flex items-center justify-between gap-2 rounded-md border border-(--ui-stroke-secondary) p-2',
        children: [
          jsx('span', { children: p.summary }),
          jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => post(`/preferences/${p.id}/forget`, {}).catch(() => {}), children: t.forget }),
        ],
      }, p.id)) : jsx('div', { className: 'text-(--ui-text-tertiary)', children: t.noPrefs }),
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-1',
        children: [
          jsx('input', { className: 'min-w-[200px] flex-1 rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1', placeholder: t.instruction, value: instruction, onChange: (e) => setInstruction(e.target.value) }),
          jsx('input', { className: 'w-[180px] rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1', placeholder: t.sender, value: sender, onChange: (e) => setSender(e.target.value) }),
          jsx('select', {
            className: 'rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1', value: effect, onChange: (e) => setEffect(e.target.value),
            children: Object.keys(t.effects).map((key) => jsx('option', { value: key, children: t.effects[key] }, key)),
          }),
          jsx(Button, { size: 'sm', onClick: save, children: t.add }),
        ],
      }),
    ],
  })
}

function RadarPage() {
  const { data, isLoading, error, refetch } = useBoard()
  const [selectedId, setSelectedId] = useState(null)
  const [feedback, setFeedback] = useState(null)
  const language = data && data.language && TEXT[data.language] ? data.language : 'es'
  const t = TEXT[language]
  const tasks = (data && data.tasks) || []
  const selected = useMemo(() => tasks.find((task) => task.id === selectedId) || tasks[0] || null, [tasks, selectedId])
  const groups = useMemo(() => STATUS_ORDER.map((status) => [status, tasks.filter((task) => task.status === status)]).filter(([, items]) => items.length), [tasks])
  const onFeedback = (result) => {
    if (!result) return
    const text = result.text || ''
    const suffix = result.job_id ? '' : result.waiting_reason ? ` (${t.waiting}: ${result.waiting_reason})` : ''
    setFeedback({ tone: result.outcome === 'error' ? 'error' : 'ok', text: (result.outcome === 'error' ? t.error : '') + text + suffix })
  }
  if (isLoading && !data) return jsx('div', { className: 'p-4', children: jsx(Skeleton, { className: 'h-6 w-48' }) })
  if (error && !data) return jsx('div', { className: 'p-4 text-sm text-(--ui-text-secondary)', children: t.error + String(error.message || error) })
  const stats = (data && data.stats) || {}
  return jsxs('div', {
    className: 'flex h-full flex-col',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between border-b border-(--ui-stroke-secondary) px-3 py-2',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2 text-sm',
            children: [
              jsx('span', { className: 'font-semibold', children: t.title }),
              jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: `${stats.tasks_open || 0} ${t.tasks.toLowerCase()} · ${stats.proposals_pending || 0} ${t.proposals.toLowerCase()} · ${stats.jobs_running || 0} jobs · ${t.mode} ${data.mode} · ${t.trust} ${data.trust_level}` }),
            ],
          }),
          jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => refetch(), children: t.refresh }),
        ],
      }),
      feedback ? jsx('div', { className: cn('px-3 py-1 text-xs', feedback.tone === 'error' ? 'text-red-400' : 'text-(--ui-text-secondary)'), children: feedback.text }) : null,
      jsxs('div', {
        className: 'flex min-h-0 flex-1',
        children: [
          jsx('div', {
            className: 'w-[340px] shrink-0 border-r border-(--ui-stroke-secondary)',
            children: jsx(ScrollArea, {
              className: 'h-full',
              children: jsx('div', {
                className: 'flex flex-col gap-3 p-2',
                children: tasks.length ? groups.map(([status, items]) => jsx(Section, {
                  title: `${t.status[status] || status} (${items.length})`,
                  children: jsx('div', { className: 'flex flex-col gap-1', children: items.map((task) => jsx(TaskRow, { task, selected: selected && selected.id === task.id, onSelect: setSelectedId, t }, task.id)) }),
                }, status)) : jsx(EmptyState, { title: t.empty }),
              }),
            }),
          }),
          jsx('div', {
            className: 'min-w-0 flex-1',
            children: jsx(ScrollArea, {
              className: 'h-full',
              children: jsxs('div', {
                className: 'flex flex-col gap-4 p-3',
                children: [
                  selected ? jsx(TaskDetail, { task: selected, t, onFeedback }, selected.id) : null,
                  jsx(Separator, {}),
                  jsx(Section, { title: t.patterns, children: jsx(Patterns, { patterns: (data && data.patterns) || [], t, onFeedback }) }),
                  jsx(Section, { title: t.preferences, children: jsx(Preferences, { preferences: (data && data.preferences) || [], t, onFeedback }) }),
                ],
              }),
            }),
          }),
        ],
      }),
    ],
  })
}

export default {
  id: 'radar',
  name: 'Radar',
  defaultEnabled: true,
  register(ctx) {
    rest = ctx.rest
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/radar' }, render: () => jsx(RadarPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/radar', label: 'Radar', codicon: 'radio-tower' } },
    ])
    const stop = ctx.socket('/events', () => invalidate())
    ctx.onDispose(stop)
  },
}
