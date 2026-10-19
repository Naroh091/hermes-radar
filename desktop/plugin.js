/**
 * Radar — Hermes Desktop panel (unified package: this file + dashboard/plugin_api.py).
 *
 * Every task is drawn as the object of its world: a trip is a boarding pass, a purchase a receipt, a
 * work item a PR ticket, a commitment a calendar leaf, a check a caution tag, a routine a dial. The
 * shape carries real fields (route, amount, branch, due date) that the backend derives per task
 * (`artifact`). Kinds the model designs for repeated "other" tasks arrive in `card_kinds` and reuse
 * these layouts with their own hue and icon. Theme surfaces and text stay the app's; each kind only
 * adds a hue.
 */
import {
  ROUTES_AREA, SIDEBAR_NAV_AREA, SandboxedFrame, Badge, Button, ScrollArea, Skeleton,
  useQuery, queryClient, relativeTime, cn,
} from '@hermes/plugin-sdk'
import { jsx, jsxs, Fragment } from 'react/jsx-runtime'
import { useState, useEffect, useMemo } from 'react'

const BOARD_KEY = ['radar', 'board']
let rest = null

const TEXT = {
  es: {
    title: 'Radar', empty: 'Nada en el radar.', emptyHint: 'Cuando llegue algo que merezca tu atención, aparecerá aquí.',
    error: 'El panel no se pudo cargar: ', tasks: 'tareas', proposals: 'Propuestas', checklist: 'Pendiente', draft: 'Propongo responder',
    context: 'Correo', later: 'Más tarde', mute: 'No avisar', done: 'Ya está hecho', report: 'Ver informe', hideReport: 'Ocultar informe',
    waiting: 'en espera', executing: 'en marcha', nothingPending: 'Nada que decidir ahora.', due: 'Vence', jobRunning: 'trabajando',
    forget: 'Olvidar', dismiss: 'Descartar', save: 'Guardar', instruction: 'Qué quieres que cambie…', sender: 'remitente (opcional)',
    effects: { silent: 'no avisar', digest: 'solo resumen', notify: 'avisar siempre', prefer: 'preferir' },
    status: { needs_user: 'Te toca', in_progress: 'En marcha', open: 'Abiertas', waiting: 'Esperando', snoozed: 'Pospuestas' },
    mutedGroup: 'Silenciadas', unmute: 'Reactivar', muted: 'Silenciada: no volverá a avisar.', unmuted: 'Reactivada.',
    patterns: 'Patrones', preferences: 'Preferencias', kinds: 'Tipos de tarjeta', refresh: 'Actualizar', close: 'Cerrar',
    noPatterns: 'Aún no hay patrones: hacen falta unas semanas de señales.', noPrefs: 'Sin preferencias guardadas.',
    daysLeft: (n) => (n === 0 ? 'hoy' : n === 1 ? 'mañana' : n < 0 ? `hace ${-n} días` : `faltan ${n} días`),
    legs: 'Trayectos', total: 'Total', order: 'Pedido', vendor: 'Vendedor', delivery: 'Entrega', branch: 'rama', pr: 'PR en borrador',
    draftReady: 'borrador listo', automated: 'automatizada', every: 'Horario', question: 'Comprobar', signals: 'Señales', history: 'Historial',
    designed: 'diseñado por el modelo', kindsHint: 'Los tipos nuevos salen de tareas repetidas que no encajan en los siete fijos.',
  },
  en: {
    title: 'Radar', empty: 'Nothing on the radar.', emptyHint: 'When something deserves your attention, it will show up here.',
    error: 'The panel could not load: ', tasks: 'tasks', proposals: 'Proposals', checklist: 'Pending', draft: 'Suggested reply',
    context: 'Email', later: 'Later', mute: 'Mute', done: 'Already done', report: 'Show report', hideReport: 'Hide report',
    waiting: 'waiting', executing: 'running', nothingPending: 'Nothing to decide right now.', due: 'Due', jobRunning: 'working',
    forget: 'Forget', dismiss: 'Dismiss', save: 'Save', instruction: 'What should change…', sender: 'sender (optional)',
    effects: { silent: 'never notify', digest: 'digest only', notify: 'always notify', prefer: 'prefer' },
    status: { needs_user: 'Your call', in_progress: 'In progress', open: 'Open', waiting: 'Waiting', snoozed: 'Snoozed' },
    mutedGroup: 'Muted', unmute: 'Unmute', muted: 'Muted: it will not notify again.', unmuted: 'Unmuted.',
    patterns: 'Patterns', preferences: 'Preferences', kinds: 'Card kinds', refresh: 'Refresh', close: 'Close',
    noPatterns: 'No patterns yet: a few weeks of signals are needed.', noPrefs: 'No preferences saved.',
    daysLeft: (n) => (n === 0 ? 'today' : n === 1 ? 'tomorrow' : n < 0 ? `${-n} days ago` : `${n} days left`),
    legs: 'Legs', total: 'Total', order: 'Order', vendor: 'Vendor', delivery: 'Delivery', branch: 'branch', pr: 'draft PR',
    draftReady: 'draft ready', automated: 'automated', every: 'Schedule', question: 'Check', signals: 'Signals', history: 'History',
    designed: 'designed by the model', kindsHint: 'New kinds come from repeated tasks that fit none of the seven built-in ones.',
  },
}
const STATUS_ORDER = ['needs_user', 'in_progress', 'open', 'waiting', 'snoozed']

const STYLE = `
.rd-root { --rd-h: 0; --rd-tint: hsl(var(--rd-h) 70% 55% / 0.10); --rd-line: hsl(var(--rd-h) 60% 55% / 0.45); --rd-ink: hsl(var(--rd-h) 75% 58%); }
.rd-card { position: relative; border: 1px solid var(--rd-line); border-radius: 10px; background: var(--rd-tint); overflow: hidden; cursor: pointer; transition: transform .12s ease, box-shadow .12s ease; text-align: left; width: 100%; }
.rd-card:hover { transform: translateY(-1px); box-shadow: 0 6px 18px -10px hsl(var(--rd-h) 70% 40% / .55); }
.rd-card:focus-visible, .rd-row:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: 2px; }
.rd-card[data-selected="true"] { box-shadow: 0 0 0 2px var(--rd-ink) inset; }
.rd-eyebrow { font-size: 10px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; color: var(--rd-ink); }
.rd-title { font-size: 14px; font-weight: 650; letter-spacing: -0.01em; line-height: 1.25; }
.rd-big { font-size: 26px; font-weight: 750; letter-spacing: -0.03em; line-height: 1; font-variant-numeric: tabular-nums; }
.rd-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }
.rd-stub { display: flex; flex-direction: column; align-items: center; justify-content: space-between; padding: 10px 6px; border-right: 2px dashed var(--rd-line); background: hsl(var(--rd-h) 70% 55% / .12); writing-mode: vertical-rl; text-orientation: mixed; }
.rd-barcode { height: 14px; margin-top: 8px; opacity: .55; background: repeating-linear-gradient(90deg, currentColor 0 2px, transparent 2px 4px, currentColor 4px 5px, transparent 5px 9px, currentColor 9px 12px, transparent 12px 14px); }
.rd-leaders { display: flex; align-items: baseline; gap: 6px; }
.rd-leaders .dots { flex: 1; border-bottom: 1px dotted var(--rd-line); transform: translateY(-3px); }
.rd-receipt-edge { border-bottom: 3px dotted var(--rd-line); }
.rd-rail { position: absolute; left: 0; top: 0; bottom: 0; width: 6px; background: repeating-linear-gradient(180deg, hsl(140 60% 45%) 0 10px, transparent 10px 14px, hsl(0 70% 55%) 14px 20px, transparent 20px 26px, hsl(140 60% 45%) 26px 40px, transparent 40px 44px); opacity: .7; }
.rd-leaf { display: flex; flex-direction: column; align-items: center; justify-content: center; min-width: 64px; padding: 10px 8px; border-right: 1px solid var(--rd-line); background: hsl(var(--rd-h) 70% 55% / .14); position: relative; }
.rd-leaf::before, .rd-leaf::after { content: ''; position: absolute; top: -4px; width: 7px; height: 7px; border-radius: 50%; background: var(--rd-line); }
.rd-leaf::before { left: 14px; } .rd-leaf::after { right: 14px; }
.rd-hazard { position: absolute; left: 0; top: 0; bottom: 0; width: 8px; background: repeating-linear-gradient(135deg, hsl(48 95% 55%) 0 8px, hsl(0 0% 10% / .8) 8px 16px); }
.rd-dial { width: 48px; height: 48px; border-radius: 50%; display: grid; place-items: center; background: conic-gradient(var(--rd-ink) 0 25%, hsl(var(--rd-h) 60% 55% / .25) 25% 100%); position: relative; flex: none; }
.rd-dial::after { content: ''; position: absolute; inset: 6px; border-radius: 50%; background: hsl(var(--rd-h) 70% 55% / .18); }
.rd-dial span { position: relative; z-index: 1; font-size: 18px; }
.rd-chip { display: inline-flex; align-items: center; gap: 4px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--rd-line); font-size: 10px; color: var(--ui-text-secondary); }
.rd-chip[data-live="true"]::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: hsl(140 60% 45%); animation: rd-pulse 1.6s ease-in-out infinite; }
.rd-sweep { width: 30px; height: 30px; border-radius: 50%; border: 1px solid var(--ui-stroke-secondary); position: relative; overflow: hidden; background: radial-gradient(circle at center, transparent 0 30%, var(--ui-stroke-secondary) 30% 31%, transparent 31% 60%, var(--ui-stroke-secondary) 60% 61%, transparent 61%); }
.rd-sweep::after { content: ''; position: absolute; inset: 0; background: conic-gradient(from 0deg, hsl(160 70% 55% / .55) 0 40deg, transparent 40deg 360deg); animation: rd-spin 6s linear infinite; }
.rd-drawer { position: absolute; top: 0; right: 0; bottom: 0; width: min(440px, 80%); border-left: 1px solid var(--ui-stroke-secondary); background: var(--ui-bg, inherit); box-shadow: -12px 0 30px -20px rgb(0 0 0 / .6); backdrop-filter: blur(6px); animation: rd-slide .16s ease-out; }
.rd-banner { border: 1px solid var(--ui-stroke-secondary); border-left: 3px solid var(--ui-accent); border-radius: 8px; }
@keyframes rd-spin { to { transform: rotate(360deg); } }
@keyframes rd-pulse { 0%, 100% { opacity: .4; } 50% { opacity: 1; } }
@keyframes rd-slide { from { transform: translateX(16px); opacity: 0; } to { transform: none; opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .rd-sweep::after, .rd-chip[data-live="true"]::before, .rd-drawer { animation: none; } .rd-card { transition: none; } }
`

function ago(value) {
  const ms = value ? Date.parse(value) : NaN
  if (!Number.isFinite(ms) || typeof relativeTime !== 'function') return ''
  try { return relativeTime(ms) } catch (_err) { return '' }
}

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

function kindOf(board, task) {
  const kinds = (board && board.card_kinds) || {}
  return kinds[task.card_kind] || kinds[task.kind] || kinds.other || { label: {}, icon: '•', hue: 0, layout: 'note' }
}

function label(kind, language) {
  return (kind.label && (kind.label[language] || kind.label.es || kind.label.en)) || ''
}

function hueStyle(kind) {
  return { '--rd-h': String(kind.hue || 0) }
}

function Chips({ task, t }) {
  const pending = (task.proposals || []).filter((p) => p.status === 'pending' || p.status === 'sent').length
  const waiting = (task.proposals || []).some((p) => p.status === 'waiting' || p.status === 'executing')
  return jsxs('div', {
    className: 'flex flex-wrap items-center gap-1',
    children: [
      pending ? jsx('span', { className: 'rd-chip', children: `${pending} ${t.proposals.toLowerCase()}` }) : null,
      task.jobs_running ? jsx('span', { className: 'rd-chip', 'data-live': 'true', children: t.jobRunning }) : null,
      waiting && !task.jobs_running ? jsx('span', { className: 'rd-chip', children: t.waiting }) : null,
      task.draft ? jsx('span', { className: 'rd-chip', children: t.draftReady }) : null,
    ],
  })
}

function Shell({ task, kind, t, selected, onSelect, children, className }) {
  return jsx('button', {
    type: 'button', className: cn('rd-card', className), style: hueStyle(kind), 'data-selected': selected ? 'true' : 'false', onClick: () => onSelect(task.id),
    children,
  })
}

function BoardingPass({ task, kind, t, language, selected, onSelect }) {
  const a = task.artifact || {}
  const from = a.from || '—'
  const to = a.to && a.to !== a.from ? a.to : ''
  return jsx(Shell, {
    task, kind, t, selected, onSelect,
    children: jsxs('div', {
      className: 'flex min-h-[112px]',
      children: [
        jsxs('div', { className: 'rd-stub rd-eyebrow', children: [jsx('span', { children: label(kind, language) }), jsx('span', { style: { writingMode: 'horizontal-tb', fontSize: '16px' }, children: kind.icon })] }),
        jsxs('div', {
          className: 'flex flex-1 flex-col gap-1 p-3',
          children: [
            jsxs('div', { className: 'flex items-center justify-between gap-2', children: [jsx('span', { className: 'rd-eyebrow', children: a.code ? `PNR ${a.code}` : t.title }), jsx(Chips, { task, t })] }),
            jsxs('div', { className: 'rd-big flex items-baseline gap-2', children: [jsx('span', { children: from }), to ? jsx('span', { className: 'text-(--ui-text-tertiary)', style: { fontSize: '16px' }, children: '→' }) : null, to ? jsx('span', { children: to }) : null] }),
            jsx('div', { className: 'rd-title', children: task.title }),
            jsxs('div', {
              className: 'mt-1 flex gap-4 text-xs text-(--ui-text-secondary)',
              children: [
                a.depart && a.depart.day ? jsxs('span', { children: [jsx('b', { children: `${a.depart.day} ${a.depart.month}` }), a.depart.time && a.depart.time !== '00:00' ? ` ${a.depart.time}` : ''] }) : null,
                a.return && a.return.day ? jsxs('span', { children: ['↩ ', jsx('b', { children: `${a.return.day} ${a.return.month}` })] }) : null,
              ],
            }),
            jsx('div', { className: 'rd-barcode', style: { color: 'var(--rd-ink)' } }),
          ],
        }),
      ],
    }),
  })
}

function Receipt({ task, kind, t, language, selected, onSelect }) {
  const a = task.artifact || {}
  const amount = a.amount && a.amount.value != null ? `${a.amount.value} ${a.amount.currency || ''}`.trim() : ''
  return jsx(Shell, {
    task, kind, t, selected, onSelect, className: 'rd-receipt-edge',
    children: jsxs('div', {
      className: 'flex flex-col gap-1.5 p-3 rd-mono',
      children: [
        jsxs('div', { className: 'flex items-center justify-between', children: [jsx('span', { className: 'rd-eyebrow', children: `${kind.icon} ${a.vendor || label(kind, language)}` }), jsx(Chips, { task, t })] }),
        jsx('div', { className: 'rd-title', style: { fontFamily: 'inherit' }, children: task.title }),
        ...(a.lines || []).slice(0, 3).map((line, i) => jsxs('div', { className: 'rd-leaders text-(--ui-text-secondary)', children: [jsx('span', { className: 'truncate', children: line }), jsx('span', { className: 'dots' })] }, `l${i}`)),
        a.order ? jsxs('div', { className: 'rd-leaders text-(--ui-text-tertiary)', children: [jsx('span', { children: t.order }), jsx('span', { className: 'dots' }), jsx('span', { children: a.order })] }) : null,
        amount ? jsxs('div', { className: 'rd-leaders', children: [jsx('span', { className: 'rd-eyebrow', children: t.total }), jsx('span', { className: 'dots' }), jsx('span', { className: 'rd-big', style: { fontSize: '22px' }, children: amount })] }) : null,
        a.delivery && a.delivery.day ? jsx('div', { className: 'text-(--ui-text-tertiary)', children: `${t.delivery}: ${a.delivery.day} ${a.delivery.month}` }) : null,
      ],
    }),
  })
}

function Ticket({ task, kind, t, language, selected, onSelect }) {
  const a = task.artifact || {}
  return jsx(Shell, {
    task, kind, t, selected, onSelect,
    children: jsxs('div', {
      className: 'relative flex flex-col gap-1.5 p-3 pl-5',
      children: [
        jsx('div', { className: 'rd-rail' }),
        jsxs('div', { className: 'flex items-center justify-between', children: [jsx('span', { className: 'rd-eyebrow', children: `${kind.icon} ${a.repo || label(kind, language)}` }), jsx(Chips, { task, t })] }),
        jsx('div', { className: 'rd-title', children: task.title }),
        jsxs('div', {
          className: 'flex flex-wrap items-center gap-1 rd-mono text-(--ui-text-secondary)',
          children: [
            a.number ? jsx('span', { className: 'rd-chip', children: a.number }) : null,
            a.branch ? jsx('span', { className: 'rd-chip', children: `⎇ ${a.branch}` }) : null,
            a.pr ? jsx('span', { className: 'rd-chip', children: t.pr }) : null,
            a.ref && !a.number ? jsx('span', { className: 'rd-chip', children: a.ref }) : null,
          ],
        }),
        task.summary ? jsx('div', { className: 'line-clamp-2 text-xs text-(--ui-text-tertiary)', children: task.summary }) : null,
      ],
    }),
  })
}

function CalendarLeaf({ task, kind, t, language, selected, onSelect }) {
  const a = task.artifact || {}
  const due = a.due || {}
  return jsx(Shell, {
    task, kind, t, selected, onSelect,
    children: jsxs('div', {
      className: 'flex min-h-[96px]',
      children: [
        jsxs('div', { className: 'rd-leaf', children: [jsx('span', { className: 'rd-eyebrow', children: due.month || label(kind, language) }), jsx('span', { className: 'rd-big', children: due.day != null ? String(due.day) : kind.icon }), jsx('span', { className: 'text-[10px] text-(--ui-text-tertiary)', children: due.weekday || '' })] }),
        jsxs('div', {
          className: 'flex flex-1 flex-col gap-1 p-3',
          children: [
            jsxs('div', { className: 'flex items-center justify-between', children: [jsx('span', { className: 'rd-eyebrow', children: a.days_left != null ? t.daysLeft(a.days_left) : label(kind, language) }), jsx(Chips, { task, t })] }),
            jsx('div', { className: 'rd-title', children: task.title }),
            (a.who || []).length ? jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: a.who.join(', ') }) : null,
            task.summary ? jsx('div', { className: 'line-clamp-2 text-xs text-(--ui-text-secondary)', children: task.summary }) : null,
          ],
        }),
      ],
    }),
  })
}

function Tag({ task, kind, t, language, selected, onSelect }) {
  const a = task.artifact || {}
  return jsx(Shell, {
    task, kind, t, selected, onSelect,
    children: jsxs('div', {
      className: 'relative flex flex-col gap-1 p-3 pl-5',
      children: [
        jsx('div', { className: 'rd-hazard' }),
        jsxs('div', { className: 'flex items-center justify-between', children: [jsx('span', { className: 'rd-eyebrow', children: `${kind.icon} ${t.question}` }), jsx(Chips, { task, t })] }),
        jsx('div', { className: 'rd-title', children: task.title }),
        a.question ? jsx('div', { className: 'line-clamp-3 text-xs text-(--ui-text-secondary)', children: a.question }) : null,
      ],
    }),
  })
}

function Dial({ task, kind, t, language, selected, onSelect }) {
  const a = task.artifact || {}
  return jsx(Shell, {
    task, kind, t, selected, onSelect,
    children: jsxs('div', {
      className: 'flex items-center gap-3 p-3',
      children: [
        jsx('div', { className: 'rd-dial', children: jsx('span', { children: kind.icon }) }),
        jsxs('div', {
          className: 'flex min-w-0 flex-1 flex-col gap-1',
          children: [
            jsxs('div', { className: 'flex items-center justify-between', children: [jsx('span', { className: 'rd-eyebrow', children: a.schedule_label || label(kind, language) }), jsx(Chips, { task, t })] }),
            jsx('div', { className: 'rd-title', children: task.title }),
            task.summary ? jsx('div', { className: 'line-clamp-2 text-xs text-(--ui-text-secondary)', children: task.summary }) : null,
          ],
        }),
      ],
    }),
  })
}

function Note({ task, kind, t, language, selected, onSelect }) {
  const a = task.artifact || {}
  return jsx(Shell, {
    task, kind, t, selected, onSelect,
    children: jsxs('div', {
      className: 'flex flex-col gap-1 p-3',
      children: [
        jsxs('div', { className: 'flex items-center justify-between', children: [jsx('span', { className: 'rd-eyebrow', children: `${kind.icon} ${a.eyebrow || label(kind, language)}` }), jsx(Chips, { task, t })] }),
        a.headline ? jsx('div', { className: 'rd-big', style: { fontSize: '20px' }, children: a.headline }) : null,
        jsx('div', { className: 'rd-title', children: task.title }),
        task.summary ? jsx('div', { className: 'line-clamp-2 text-xs text-(--ui-text-secondary)', children: task.summary }) : null,
      ],
    }),
  })
}

const LAYOUTS = { 'boarding-pass': BoardingPass, receipt: Receipt, ticket: Ticket, calendar: CalendarLeaf, tag: Tag, dial: Dial, note: Note }

function Card(props) {
  const layout = (props.task.artifact && props.task.artifact.layout) || props.kind.layout || 'note'
  const Component = LAYOUTS[layout] || Note
  return jsx(Component, props)
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
  const isNews = proposal.operation === 'inform'
  return jsxs('div', {
    className: 'mb-2 rounded-md border border-(--ui-stroke-secondary) p-2 text-sm',
    children: [
      jsxs('div', {
        className: 'flex items-start justify-between gap-2',
        children: [
          jsxs('div', { children: [jsx('div', { className: 'font-medium', children: isNews ? (proposal.message || proposal.title) : proposal.title }), proposal.reason ? jsx('div', { className: 'text-xs text-(--ui-text-secondary)', children: proposal.reason }) : null] }),
          jsxs('div', { className: 'flex shrink-0 flex-col items-end gap-0.5 text-[10px] text-(--ui-text-tertiary)', children: [isNews ? null : jsx(Badge, { variant: 'secondary', className: 'text-[10px]', children: proposal.action_class }), inFlight ? jsx('span', { children: proposal.status === 'waiting' ? `${t.waiting}${proposal.wait_reason ? ': ' + proposal.wait_reason : ''}` : t.executing }) : null] }),
        ],
      }),
      proposal.draft ? jsxs('div', { className: 'mt-1 rounded bg-(--ui-stroke-secondary)/30 p-2 text-xs whitespace-pre-wrap', children: [jsx('b', { children: `${t.draft}: ` }), proposal.draft, proposal.draft_truncated ? '…' : ''] }) : null,
      detail ? jsx('div', { className: 'mt-1 rounded border border-(--ui-stroke-secondary) p-2 text-xs whitespace-pre-wrap', children: detail }) : null,
      !inFlight ? jsx('div', { className: 'mt-2 flex flex-wrap gap-1', children: options.slice(0, 3).map((option, index) => jsx(Button, { size: 'sm', variant: index === 0 ? 'default' : 'outline', disabled: busy, onClick: () => act(option.id), children: option.label }, option.id)) }) : null,
    ],
  })
}

function ReportFrame({ taskId, t }) {
  const [open, setOpen] = useState(false)
  const [src, setSrc] = useState('')
  useEffect(() => {
    let alive = true
    if (open && !src) rest(`/tasks/${taskId}/report`).then((r) => { if (alive) setSrc(r.data_url || '') }).catch(() => {})
    return () => { alive = false }
  }, [open, taskId])
  return jsxs('div', {
    children: [
      jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => setOpen(!open), children: open ? t.hideReport : t.report }),
      open && src ? jsx('div', { className: 'mt-2 h-[480px] overflow-hidden rounded-md border border-(--ui-stroke-secondary)', children: jsx(SandboxedFrame, { src, title: `Radar ${taskId}`, className: 'h-full w-full' }) }) : null,
    ],
  })
}

function Drawer({ task, kind, t, language, onClose, onFeedback }) {
  const proposals = task.proposals || []
  const missing = (task.checklist || []).filter((c) => c.status === 'missing')
  const taskAction = (action) => post(`/tasks/${task.id}/action`, { action })
    .then((result) => { onFeedback({ ...result, text: result.text || (action === 'mute' ? t.muted : '') }); if (action !== 'later') onClose() })
    .catch((err) => onFeedback({ outcome: 'error', text: String(err) }))
  return jsx('div', {
    className: 'rd-drawer rd-root', style: hueStyle(kind),
    children: jsx(ScrollArea, {
      className: 'h-full',
      children: jsxs('div', {
        className: 'flex flex-col gap-3 p-3',
        children: [
          jsxs('div', {
            className: 'flex items-start justify-between gap-2',
            children: [
              jsxs('div', { children: [jsx('div', { className: 'rd-eyebrow', children: `${kind.icon} ${label(kind, language)} · ${t.status[task.status] || task.status}` }), jsx('div', { className: 'text-base font-semibold leading-tight', children: task.title })] }),
              jsx(Button, { size: 'sm', variant: 'ghost', onClick: onClose, children: '✕' }),
            ],
          }),
          task.summary ? jsx('p', { className: 'text-sm text-(--ui-text-secondary)', children: task.summary }) : null,
          jsx(Context, { context: task.context, t }),
          missing.length ? jsxs('div', { className: 'text-xs', children: [jsx('b', { children: `${t.checklist}: ` }), missing.map((c) => c.item).join('; ')] }) : null,
          task.entities && task.entities.pr ? jsx('a', { className: 'text-xs underline', href: task.entities.pr, target: '_blank', rel: 'noreferrer', children: task.entities.pr }) : null,
          jsxs('div', { children: [jsx('div', { className: 'rd-eyebrow mb-1', children: t.proposals }), proposals.length ? proposals.map((p) => jsx(Proposal, { proposal: p, t, onFeedback }, p.id)) : jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: t.nothingPending })] }),
          task.draft && !proposals.some((p) => p.draft) ? jsxs('div', { className: 'rounded bg-(--ui-stroke-secondary)/30 p-2 text-xs whitespace-pre-wrap', children: [jsx('b', { children: `${t.draft}: ` }), task.draft] }) : null,
          jsxs('div', { className: 'flex flex-wrap gap-1', children: [
            jsx(Button, { size: 'sm', variant: 'outline', onClick: () => taskAction('later'), children: `⏰ ${t.later}` }),
            jsx(Button, { size: 'sm', variant: 'outline', onClick: () => taskAction('mute'), children: `🔕 ${t.mute}` }),
            jsx(Button, { size: 'sm', variant: 'outline', onClick: () => taskAction('done'), children: `✔️ ${t.done}` }),
          ] }),
          jsx(ReportFrame, { taskId: task.id, t }),
        ],
      }),
    }),
  })
}

function Patterns({ patterns, t, onFeedback }) {
  if (!patterns.length) return jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: t.noPatterns })
  return jsx('div', { className: 'grid gap-1', style: { gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))' }, children: patterns.map((p) => jsxs('div', {
    className: 'rd-root rd-card flex items-start justify-between gap-2 p-2 text-xs', style: { '--rd-h': '160', cursor: 'default' },
    children: [
      jsxs('div', { children: [jsx('div', { className: 'font-medium', children: p.name || p.summary }), jsx('div', { className: 'text-(--ui-text-tertiary)', children: `${p.kind} · ${p.status} · x${p.support}` }), p.description ? jsx('div', { className: 'text-(--ui-text-secondary)', children: p.description }) : null] }),
      p.status === 'proposed' || p.status === 'candidate' ? jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => post(`/patterns/${p.id}/dismiss`, {}).then(onFeedback).catch(() => {}), children: t.dismiss }) : null,
    ],
  }, p.id)) })
}

function Preferences({ preferences, t, onFeedback }) {
  const [instruction, setInstruction] = useState('')
  const [effect, setEffect] = useState('silent')
  const [sender, setSender] = useState('')
  const save = async () => {
    if (!instruction.trim()) return
    try {
      await post('/preferences', { instruction: instruction.trim(), effect, scope: sender.trim() ? { sender: sender.trim().toLowerCase() } : {} })
      setInstruction(''); setSender('')
    } catch (err) { onFeedback({ outcome: 'error', text: String(err) }) }
  }
  return jsxs('div', {
    className: 'flex flex-col gap-2 text-xs',
    children: [
      preferences.length ? preferences.map((p) => jsxs('div', { className: 'flex items-center justify-between gap-2 rounded-md border border-(--ui-stroke-secondary) p-2', children: [jsx('span', { children: p.summary }), jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => post(`/preferences/${p.id}/forget`, {}).catch(() => {}), children: t.forget })] }, p.id)) : jsx('div', { className: 'text-(--ui-text-tertiary)', children: t.noPrefs }),
      jsxs('div', { className: 'flex flex-wrap items-center gap-1', children: [
        jsx('input', { className: 'min-w-[220px] flex-1 rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1', placeholder: t.instruction, value: instruction, onChange: (e) => setInstruction(e.target.value) }),
        jsx('input', { className: 'w-[180px] rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1', placeholder: t.sender, value: sender, onChange: (e) => setSender(e.target.value) }),
        jsx('select', { className: 'rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1', value: effect, onChange: (e) => setEffect(e.target.value), children: Object.keys(t.effects).map((key) => jsx('option', { value: key, children: t.effects[key] }, key)) }),
        jsx(Button, { size: 'sm', onClick: save, children: t.save }),
      ] }),
    ],
  })
}

function Kinds({ kinds, t, language }) {
  const generated = Object.entries(kinds).filter(([, k]) => k.origin === 'generated')
  return jsxs('div', {
    className: 'flex flex-wrap gap-1 text-xs',
    children: [
      ...Object.entries(kinds).map(([id, k]) => jsxs('span', { className: 'rd-root rd-chip', style: hueStyle(k), title: k.rationale || '', children: [k.icon, ' ', label(k, language), k.origin === 'generated' ? jsx(Button, { size: 'sm', variant: 'ghost', className: 'h-4 px-1', onClick: () => post(`/card-kinds/${id}/disable`, {}).catch(() => {}), children: '✕' }) : null] }, id)),
      generated.length ? null : jsx('span', { className: 'text-(--ui-text-tertiary)', children: t.kindsHint }),
    ],
  })
}

function Section({ title, children, hint }) {
  return jsxs('div', { className: 'mb-5', children: [jsxs('div', { className: 'mb-2 flex items-baseline gap-2', children: [jsx('span', { className: 'rd-eyebrow', style: { color: 'var(--ui-text-tertiary)' }, children: title }), hint ? jsx('span', { className: 'text-[10px] text-(--ui-text-quaternary)', children: hint }) : null] }), children] })
}

function RadarPage() {
  const { data, isLoading, error, refetch } = useBoard()
  const [selectedId, setSelectedId] = useState(null)
  const [feedback, setFeedback] = useState(null)
  const language = data && data.language && TEXT[data.language] ? data.language : 'es'
  const t = TEXT[language]
  const tasks = (data && data.tasks) || []
  const mutedTasks = (data && data.muted_tasks) || []
  const selected = useMemo(() => tasks.find((task) => task.id === selectedId) || null, [tasks, selectedId])
  const groups = useMemo(() => STATUS_ORDER.map((status) => [status, tasks.filter((task) => task.status === status)]).filter(([, items]) => items.length), [tasks])
  const onFeedback = (result) => {
    if (!result) return
    const suffix = result.job_id ? '' : result.waiting_reason ? ` (${t.waiting}: ${result.waiting_reason})` : ''
    setFeedback({ tone: result.outcome === 'error' ? 'error' : 'ok', text: (result.outcome === 'error' ? t.error : '') + (result.text || '') + suffix })
  }
  if (isLoading && !data) return jsx('div', { className: 'p-4', children: jsx(Skeleton, { className: 'h-6 w-48' }) })
  if (error && !data) return jsx('div', { className: 'p-4 text-sm text-(--ui-text-secondary)', children: t.error + String(error.message || error) })
  const stats = (data && data.stats) || {}
  const kinds = (data && data.card_kinds) || {}
  return jsxs('div', {
    className: 'rd-root relative flex h-full flex-col',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between border-b border-(--ui-stroke-secondary) px-3 py-2',
        children: [
          jsxs('div', { className: 'flex items-center gap-3', children: [
            jsx('div', { className: 'rd-sweep', 'aria-hidden': 'true' }),
            jsxs('div', { children: [
              jsx('div', { className: 'text-sm font-semibold tracking-tight', children: t.title }),
              jsx('div', { className: 'text-[11px] text-(--ui-text-tertiary)', children: `${stats.tasks_open || 0} ${t.tasks} · ${stats.proposals_pending || 0} ${t.proposals.toLowerCase()} · ${stats.jobs_running || 0} jobs` }),
            ] }),
          ] }),
          jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => refetch(), children: t.refresh }),
        ],
      }),
      feedback && feedback.text ? jsx('div', { className: cn('rd-banner mx-3 my-2 px-3 py-1.5 text-xs', feedback.tone === 'error' ? 'text-red-400' : 'text-(--ui-text-secondary)'), children: feedback.text }) : null,
      jsx(ScrollArea, {
        className: 'min-h-0 flex-1',
        children: jsxs('div', {
          className: 'p-3', style: { paddingRight: selected ? 'min(452px, 82%)' : undefined },
          children: [
            tasks.length ? groups.map(([status, items]) => jsx(Section, {
              title: `${t.status[status] || status} · ${items.length}`,
              children: jsx('div', { className: 'grid gap-2', style: { gridTemplateColumns: 'repeat(auto-fill, minmax(270px, 1fr))' }, children: items.map((task) => jsx(Card, { task, kind: kindOf(data, task), t, language, selected: selected && selected.id === task.id, onSelect: setSelectedId }, task.id)) }),
            }, status)) : jsxs('div', { className: 'py-10 text-center', children: [jsx('div', { className: 'text-sm font-medium', children: t.empty }), jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: t.emptyHint })] }),
            mutedTasks.length ? jsx(Section, { title: `${t.mutedGroup} · ${mutedTasks.length}`, children: jsx('div', { className: 'flex flex-col gap-1', children: mutedTasks.map((task) => jsxs('div', { className: 'flex items-center justify-between gap-2 rounded-md border border-(--ui-stroke-secondary) px-2 py-1 text-xs text-(--ui-text-tertiary)', children: [jsxs('span', { className: 'truncate', children: [kindOf(data, task).icon, ' ', task.title] }), jsx(Button, { size: 'sm', variant: 'ghost', onClick: () => post(`/tasks/${task.id}/action`, { action: 'unmute' }).then(() => onFeedback({ outcome: 'unmuted', text: t.unmuted })).catch(() => {}), children: t.unmute })] }, task.id)) }) }) : null,
            jsx(Section, { title: t.patterns, children: jsx(Patterns, { patterns: (data && data.patterns) || [], t, onFeedback }) }),
            jsx(Section, { title: t.preferences, children: jsx(Preferences, { preferences: (data && data.preferences) || [], t, onFeedback }) }),
            jsx(Section, { title: t.kinds, hint: t.kindsHint, children: jsx(Kinds, { kinds, t, language }) }),
          ],
        }),
      }),
      selected ? jsx(Drawer, { task: selected, kind: kindOf(data, selected), t, language, onClose: () => setSelectedId(null), onFeedback }, selected.id) : null,
    ],
  })
}

export default {
  id: 'radar',
  name: 'Radar',
  defaultEnabled: true,
  register(ctx) {
    rest = ctx.rest
    const style = document.createElement('style')
    style.setAttribute('data-radar-panel', '1')
    style.textContent = STYLE
    document.head.appendChild(style)
    ctx.onDispose(() => { if (style.parentNode) style.parentNode.removeChild(style) })
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/radar' }, render: () => jsx(RadarPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/radar', label: 'Radar', codicon: 'radio-tower' } },
    ])
    const stop = ctx.socket('/events', () => invalidate())
    ctx.onDispose(stop)
  },
}
