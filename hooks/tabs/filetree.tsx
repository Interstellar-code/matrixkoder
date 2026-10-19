// The /mk pane's Files tab view (plan filetree-tab-ralplan, phase F3, task F3.1).
// Pure render: root chips, card with counts and search, Expand/Collapse all,
// tree rows with carets and live icons, j/k/h/l/s keyboard actions, and footer.
import type { Elements, RenderElement } from 'claude-code'
import type { RootItem, Row } from './filetree-data'

type Els = Elements[keyof Elements]

/** Colours and glyphs contract from Filetree.dc.html artboard */
export const FT = {
  cardFill: '#020d05',
  cardBorder: '#17b543',
  neon: '#19e35a',
  fileText: '#b9e8c4',
  dim: '#5f9a70',
  hidden: '#3c6a4a',
  chipFill: '#0f2a17',
  activeChip: '#1f5a33',
  white: '#ffffff',
  selectedRow: '#0f2a17',
  limeBar: '#c6f135',
  rowHover: '#0b2412',
  write: '#e8b04a',
  writeRowTint: '#221b08',
  read: '#4fd1c5',
  readRowTint: '#071f1d',
  paneBg: '#020a04',
} as const

export type FilesTabUi = {
  get(key: string): string | undefined
  set(key: string, value: string): void
  reveal?(key: string): void
  focus?(key: string): void
  /** list one folder the walk skipped, and keep it listed */
  scan?(path: string): void
}

export type FilesView = {
  roots: readonly RootItem[]
  root: string | RootItem
  rows: readonly Row[]
  dirs: readonly string[]
  nWrite: number
  nRead: number
  nFolders: number
  nFiles: number
  pulse: boolean
  capped: boolean
  note: string | null
  /** git status marks by path relative to the root; absent when git is not read */
  git?: ReadonlyMap<string, string>
  q?: string
  sel?: string
}

function clip(str: string, maxLen: number): string {
  if (str.length <= maxLen) return str
  return maxLen <= 1 ? '…' : `${str.slice(0, maxLen - 1)}…`
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const SIZE_W = 7
const DATE_W = 9
const ICON_W = 2 // the file or folder glyph (1 cell) and a space
const GIT_W = 4 // the git mark column, between the name and the size
const ACT_W = 5 // the writing / reading dot, always reserved when the columns show
const COLS_MIN_W = 56 // narrower than this the Size and Modified columns are left out

/** bytes as `812 B`, `3.4 K`, `12 M`; empty for a folder */
function fmtSize(bytes: number | undefined): string {
  if (bytes == null) return ''
  if (bytes < 1024) return `${bytes} B`
  const units = ['K', 'M', 'G', 'T']
  let n = bytes / 1024
  let u = 0
  while (n >= 1024 && u < units.length - 1) { n /= 1024; u++ }
  return `${n < 10 ? n.toFixed(1) : Math.round(n)} ${units[u]}`
}

/** `Oct 18` for this year, `Oct 2025` for an older one */
function fmtDate(ms: number | undefined): string {
  if (ms == null) return ''
  const d = new Date(ms)
  const mon = MONTHS[d.getMonth()] ?? ''
  return d.getFullYear() === new Date().getFullYear() ? `${mon} ${String(d.getDate()).padStart(2, '0')}` : `${mon} ${d.getFullYear()}`
}

/** one coloured glyph per kind, so a folder or a file type reads at a glance without emoji */
const EXT_COLOR: Record<string, string> = {
  ts: '#5b9bd5', tsx: '#5b9bd5', js: '#e5c07b', jsx: '#e5c07b', mjs: '#e5c07b', py: '#e8d44d',
  md: '#7ec699', txt: '#7ec699', json: '#e8b04a', yaml: '#e8b04a', yml: '#e8b04a', toml: '#e8b04a',
  sh: '#c678dd', zsh: '#c678dd', bash: '#c678dd', png: '#e06c9f', jpg: '#e06c9f', jpeg: '#e06c9f',
  gif: '#e06c9f', svg: '#e06c9f', webp: '#e06c9f', lock: '#8a8a8a',
}

/** the glyph and its colour in front of a name: folder (open or shut), link, or a file by its extension */
function glyphOf(row: Row): { glyph: string; color: string } {
  if (row.dir) return { glyph: row.open ? '▾' : '■', color: row.hidden ? FT.hidden : FT.neon }
  if (row.link) return { glyph: '↪', color: FT.dim }
  const dot = row.name.lastIndexOf('.')
  const color = (dot > 0 ? EXT_COLOR[row.name.slice(dot + 1).toLowerCase()] : undefined) ?? FT.dim
  return { glyph: '◆', color }
}

/** the git mark of a row: its own, or a dot for a folder that holds a changed file */
function gitMarkOf(row: Row, rootPath: string, git: ReadonlyMap<string, string> | undefined): { mark: string; color: string } {
  if (!git || git.size === 0) return { mark: '', color: FT.dim }
  const rel = row.path.startsWith(`${rootPath}/`) ? row.path.slice(rootPath.length + 1) : row.path
  let mark = git.get(rel) ?? git.get(`${rel}/`) ?? ''
  if (mark === '' && row.dir) {
    for (const k of git.keys()) if (k.startsWith(`${rel}/`)) { mark = '•'; break }
  }
  const color = mark === 'D' ? '#e06c6c' : mark === '?' || mark === 'A' ? FT.read : FT.write
  return { mark, color }
}

function getParent(p: string): string {
  const cut = p.lastIndexOf('/')
  return cut <= 0 ? '/' : p.slice(0, cut)
}

/** The latest press handler: a row element is cached across renders, so its `onPress` may not close
 *  over one render's view. Every render repoints this before rows are drawn. */
let rowPress: (row: Row) => void = () => {}

/** One drawn row per `row.key`, reused while its signature is unchanged: a pulse that changes no
 *  visible prop on a row (it has no icon) hands back the same element object instead of rebuilding
 *  up to 600 of them. Pruned to the rows of the latest render, so it never grows. */
const rowCache = new Map<string, { sig: string; el: RenderElement }>()

function parseOpenList(view: FilesView, ui: FilesTabUi): string[] {
  try {
    const raw = ui.get('files:open')
    if (raw) {
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) return parsed as string[]
    }
  } catch {}
  return view.rows.filter(r => r.dir && r.open).map(r => r.path)
}

/**
 * FilesTab draws the Files tab from a plain view value following the Filetree artboard.
 */
export function FilesTab(
  els: Els,
  view: FilesView,
  cols: number,
  ui: FilesTabUi,
): RenderElement {
  const { Box, Button, Text } = els
  const W = Math.max(12, cols)

  try {
    const rootId = typeof view.root === 'string' ? view.root : view.root.id
    const activeRoot = view.roots.find(r => r.id === rootId) ?? (typeof view.root === 'object' ? view.root : { id: 'main', label: 'repo', path: String(view.root) })
    const selKey = ui.get('files:sel') ?? view.sel ?? ''
    const pathRows = view.rows.filter(r => !r.note)
    const selIndex = pathRows.findIndex(r => r.key === selKey)
    const selectedRow = view.rows.find(r => r.key === selKey)

    // the cached row elements call through this, so it must be this render's view and ui
    rowPress = (row: Row): void => {
      if (row.dir) {
        const currentOpen = parseOpenList(view, ui)
        let next: string[]
        if (row.open) {
          next = currentOpen.filter(p => p !== row.path)
        } else {
          next = Array.from(new Set([...currentOpen, row.path]))
        }
        ui.set('files:open', JSON.stringify(next))
        ui.set('files:sel', row.key)
        ui.reveal?.(row.key)
      } else {
        ui.set('files:sel', row.key)
        ui.reveal?.(row.key)
      }
    }

    // Action handlers for j, k, h, l keys
    const moveSel = (delta: number) => {
      if (pathRows.length === 0) return
      let nextIndex = 0
      if (selIndex === -1) {
        nextIndex = 0
      } else {
        nextIndex = Math.max(0, Math.min(pathRows.length - 1, selIndex + delta))
      }
      const nextRow = pathRows[nextIndex]
      if (nextRow) {
        ui.set('files:sel', nextRow.key)
        ui.reveal?.(nextRow.key)
      }
    }

    const handleH = () => {
      if (!selectedRow) return
      if (selectedRow.dir && selectedRow.open) {
        const currentOpen = parseOpenList(view, ui)
        const next = currentOpen.filter(p => p !== selectedRow.path)
        ui.set('files:open', JSON.stringify(next))
        return
      }
      // a file or an already folded folder: step up to the parent folder (a depth-0 row has none)
      const parentPath = getParent(selectedRow.path)
      const parentRow = pathRows.find(r => r.dir && r.path === parentPath)
      if (parentRow) {
        ui.set('files:sel', parentRow.key)
        ui.reveal?.(parentRow.key)
      }
    }

    const handleL = () => {
      if (selectedRow?.dir) {
        const currentOpen = parseOpenList(view, ui)
        const next = Array.from(new Set([...currentOpen, selectedRow.path]))
        ui.set('files:open', JSON.stringify(next))
      }
    }

    // 1. Root chips row
    const chipRow = (
      <Box flexDirection="row" gap={1} flexWrap="wrap" marginBottom={1}>
        {view.roots.map(r => {
          const isChosen = r.id === rootId
          return (
            <Box key={`files-root-box:${r.id}`} borderStyle="round" borderColor={isChosen ? FT.neon : FT.hidden} backgroundColor={isChosen ? FT.activeChip : FT.chipFill} paddingX={1}>
              {Button({
                key: `files-root:${r.id}`,
                plain: true,
                onPress: () => ui.set('files:root', r.id),
                children: [
                  <Text color={isChosen ? FT.white : r.idle ? FT.hidden : FT.fileText} bold={true}>
                    {clip(r.label, Math.max(4, W - 8))}
                  </Text>,
                ],
              })}
            </Box>
          )
        })}
      </Box>
    )

    // 2. Card header with counts, root path, search box and expand/collapse buttons
    const innerCardW = Math.max(4, W - 4)
    const dirty = view.git?.size ?? 0
    const countText = `${view.nFolders} folders · ${view.nFiles} files${view.git ? (dirty > 0 ? ` · ● ${dirty} dirty` : ' · ✓ clean') : ''}`
    const leftLabelBudget = Math.max(4, innerCardW - countText.length - 1)
    const clippedRootLabel = clip(activeRoot.id === 'main' ? activeRoot.label : `${activeRoot.label} · worktree`, leftLabelBudget)
    const home = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env?.HOME
    const tilde = (p: string): string => (home && p.startsWith(`${home}/`) ? `~${p.slice(home.length)}` : p)
    // the line under the label names the selected row, else the root
    const clippedRootPath = clip(tilde(selectedRow && !selectedRow.note ? selectedRow.path : activeRoot.path), innerCardW)

    let searchInputEl: RenderElement
    if ('Input' in els) {
      const { Input } = els as unknown as { Input: (props: Record<string, unknown>) => RenderElement }
      searchInputEl = Input({
        key: 'files-q',
        placeholder: 'filter files',
        value: view.q ?? '',
        onInput: (v: string) => ui.set('files:q', v),
        onSubmit: (v: string) => {
          ui.set('files:q', v)
          const available = view.rows.filter(r => !r.note)
          let targetKey = 'files-s'
          if (selKey && available.some(r => r.key === selKey)) {
            targetKey = selKey
          } else if (available.length > 0 && available[0]) {
            targetKey = available[0].key
          }
          ui.focus?.(targetKey)
        },
      })
    } else {
      searchInputEl = <Text dimColor={true}>{clip(view.q ? view.q : 'filter files', Math.max(4, innerCardW - 3))}</Text>
    }

    const card = (
      <Box
        flexDirection="column"
        backgroundColor={FT.cardFill}
        borderColor={FT.cardBorder}
        borderStyle="round"
        paddingX={1}
      >
        <Box flexDirection="row" justifyContent="space-between">
          <Text bold={true} color={FT.neon}>{clippedRootLabel}</Text>
          <Box flexDirection="row">
            <Text dimColor={true} color={FT.dim}>{`${view.nFolders} folders · ${view.nFiles} files`}</Text>
            {view.git
              ? (dirty > 0
                ? <Text color={FT.write}>{` · ● ${dirty} dirty`}</Text>
                : <Text color={FT.neon}>{' · ✓ clean'}</Text>)
              : []}
          </Box>
        </Box>
        <Text dimColor={true} color={FT.dim} wrap="truncate">{clippedRootPath}</Text>
        <Box flexDirection="row" gap={1} borderColor={FT.cardBorder} borderStyle="round" paddingX={1}>
          {Button({
            key: 'files-s',
            hotkey: 's',
            plain: true,
            label: 'search',
            onPress: () => ui.focus?.('files-q'),
          })}
          {searchInputEl}
        </Box>
        <Box flexDirection="row" gap={1} justifyContent="flex-end">
          {Button({
            key: 'files-expand',
            plain: true,
            children: [<Text backgroundColor={FT.chipFill} color={FT.fileText}>{' ▾ Expand all '}</Text>],
            onPress: () => ui.set('files:open', JSON.stringify(view.dirs ?? [])),
          })}
          {Button({
            key: 'files-collapse',
            plain: true,
            children: [<Text backgroundColor={FT.chipFill} color={FT.fileText}>{' ▸ Collapse all '}</Text>],
            onPress: () => ui.set('files:open', JSON.stringify([])),
          })}
        </Box>
      </Box>
    )

    // 3. Tree rows
    const innerTreeW = Math.max(4, W - 2)
    const showCols = W >= COLS_MIN_W
    const sortBy = ui.get('files:sort') === 'size' || ui.get('files:sort') === 'date' ? ui.get('files:sort') : 'name'
    const sortDesc = ui.get('files:dir') === 'desc'
    const pressSort = (by: string) => {
      if (by === sortBy) {
        ui.set('files:dir', sortDesc ? 'asc' : 'desc')
      } else {
        ui.set('files:sort', by)
        ui.set('files:dir', by === 'name' ? 'asc' : 'desc')
      }
    }
    const headCell = (by: 'name' | 'size' | 'date', label: string, width: number, right: boolean) => {
      const text = `${label}${sortBy === by ? (sortDesc ? ' ▼' : ' ▲') : ''}`
      return Button({
        key: `files-sort-${by}`,
        plain: true,
        onPress: () => pressSort(by),
        children: [<Text bold={sortBy === by} color={sortBy === by ? FT.neon : FT.dim}>{right ? text.padStart(width) : text.padEnd(width)}</Text>],
      })
    }
    const headRow = (
      <Box key="files-head" flexDirection="row">
        <Text>{' '.repeat(3)}</Text>
        {headCell('name', 'Name', showCols ? innerTreeW - 3 - GIT_W - SIZE_W - DATE_W - ACT_W : innerTreeW - 3, false)}
        {showCols ? <Text color={FT.dim}>{'Git'.padStart(GIT_W - 1).padEnd(GIT_W)}</Text> : []}
        {showCols ? headCell('size', 'Size', SIZE_W, true) : []}
        {showCols ? headCell('date', 'Modified', DATE_W, true) : []}
        {showCols ? <Text>{' '.repeat(ACT_W)}</Text> : []}
      </Box>
    )
    const drawnRows: RenderElement[] = []
    if (view.rows.length === 0) {
      drawnRows.push(
        <Text key="files-no-matches" dimColor={true} color={FT.dim}>{clip(view.q ? `no file matches “${view.q}”` : 'no file matches', innerTreeW)}</Text>,
      )
    } else {
      for (const row of view.rows) {
        const indentStr = ' '.repeat(Math.max(0, row.depth * 2))
        const isSelected = row.key === selKey

        // Note rows are rendered as non-selectable Text
        if (row.note === 'empty') {
          drawnRows.push(
            <Text key={row.key} dimColor={true} color={FT.dim}>
              {clip(` ${indentStr}    (empty)`, innerTreeW)}
            </Text>,
          )
          continue
        }
        if (row.note === 'unlisted') {
          drawnRows.push(
            <Box key={row.key} flexDirection="row" gap={1}>
              <Text dimColor={true} color={FT.dim}>{clip(` ${indentStr}    (not scanned)`, Math.max(4, innerTreeW - 10))}</Text>
              {ui.scan
                ? Button({
                    key: `files-scan:${row.path}`,
                    plain: true,
                    onPress: () => ui.scan?.(row.path),
                    children: [<Text backgroundColor={FT.chipFill} color={FT.fileText}>{' ⟳ scan '}</Text>],
                  })
                : []}
            </Box>,
          )
          continue
        }
        if (row.note === 'more') {
          drawnRows.push(
            <Text key={row.key} dimColor={true} color={FT.dim}>
              {clip(` ${indentStr}    ${row.count} more`, innerTreeW)}
            </Text>,
          )
          continue
        }

        // Regular path row
        const leftBar = isSelected ? '▌' : ' '
        const caret = row.dir ? (row.open ? '▾' : '▸') : ' '
        const displayName = row.link ? `${row.name} →` : row.dir ? `${row.name}/` : row.name

        // Right icon column (4 characters wide, right-aligned)
        let iconSymbol = ''
        let iconCountStr = ''
        if (row.icon === 'write') {
          iconSymbol = '●'
          if (row.dir && !row.open && row.count > 1) iconCountStr = ` ${row.count}`
        } else if (row.icon === 'read') {
          iconSymbol = '◉'
          if (row.dir && !row.open && row.count > 1) iconCountStr = ` ${row.count}`
        }
        // 4 columns for the icon plus 1 of right padding, as in the artboard
        const rightIconWidth = (iconSymbol + iconCountStr).length > 0 ? 5 : 0

        // Calculate name budget so entire line fits inside innerTreeW (W - 2)
        const prefixWidth = 1 + row.depth * 2 + 1 + 1 + ICON_W // bar (1) + indent + caret (1) + space (1) + file glyph
        const nameBudget = showCols
          ? Math.max(3, innerTreeW - prefixWidth - GIT_W - SIZE_W - DATE_W - ACT_W)
          : Math.max(3, innerTreeW - prefixWidth - rightIconWidth - (rightIconWidth > 0 ? 1 : 0))
        const clippedName = clip(displayName, nameBudget)

        // Text colour
        let nameColor: string = FT.fileText
        let isBold = false
        if (row.icon === 'write') {
          nameColor = FT.write
        } else if (row.icon === 'read') {
          nameColor = FT.read
        } else if (row.link) {
          nameColor = FT.dim
        } else if (row.hidden) {
          nameColor = FT.hidden
        } else if (row.dir) {
          nameColor = FT.neon
          isBold = true
        }

        // Row background
        let rowBg: string | undefined
        if (isSelected) {
          rowBg = FT.selectedRow
        } else if (row.icon === 'write') {
          rowBg = FT.writeRowTint
        } else if (row.icon === 'read') {
          rowBg = FT.readRowTint
        }

        // a Button holds only strings and Text, so the row background is the Box and the
        // icon column is Text inside the Button, pushed right by padding the name with spaces
        const nameCell = showCols ? clippedName.padEnd(nameBudget) : rightIconWidth > 0 ? clippedName.padEnd(nameBudget + 1) : clippedName.padEnd(innerTreeW - prefixWidth)
        const git = gitMarkOf(row, activeRoot.path, view.git)
        const glyph = glyphOf(row)
        // everything the drawn row depends on. A pulse that changes nothing visible on this row
        // (no icon) leaves the signature alone, so the element object is reused.
        const sig = [
          row.key, row.path, row.depth, row.dir, row.open, row.hidden, row.link, row.icon, row.count,
          row.size ?? '', row.mtimeMs ?? '', innerTreeW, showCols, isSelected, git.mark, git.color,
          row.icon === 'write' ? view.pulse : '',
        ].join('\u0000')
        const cached = rowCache.get(row.key)
        if (cached != null && cached.sig === sig) {
          drawnRows.push(cached.el)
          continue
        }
        const el = (
          <Box key={`files-line:${row.path}`} flexDirection="row" backgroundColor={rowBg}>
            {Button({
              key: row.key,
              plain: true,
              onPress: () => rowPress(row),
              children: [
                <Text color={isSelected ? FT.limeBar : undefined}>{leftBar}</Text>,
                <Text>{indentStr}</Text>,
                <Text color={FT.dim}>{caret}</Text>,
                <Text> </Text>,
                <Text color={glyph.color}>{`${glyph.glyph} `}</Text>,
                <Text color={nameColor} bold={isBold}>{nameCell}</Text>,
                ...(showCols
                  ? [
                      <Text color={git.color}>{git.mark.padStart(GIT_W - 1).padEnd(GIT_W)}</Text>,
                      <Text color={FT.dim}>{fmtSize(row.dir ? undefined : row.size).padStart(SIZE_W)}</Text>,
                      <Text color={FT.dim}>{fmtDate(row.mtimeMs).padStart(DATE_W)}</Text>,
                      <Text
                        color={row.icon === 'write' ? FT.write : FT.read}
                        dimColor={row.icon === 'write' ? !view.pulse : false}
                      >
                        {(rightIconWidth > 0 ? `${iconSymbol}${iconCountStr}`.padStart(4) + ' ' : '').padEnd(ACT_W)}
                      </Text>,
                    ]
                  : [])
                ,
                ...(!showCols && rightIconWidth > 0
                  ? [
                      <Text
                        color={row.icon === 'write' ? FT.write : FT.read}
                        dimColor={row.icon === 'write' ? !view.pulse : false}
                      >
                        {`${iconSymbol}${iconCountStr}`.padStart(4) + ' '}
                      </Text>,
                    ]
                  : []),
              ],
            })}
          </Box>
        )
        rowCache.set(row.key, { sig, el })
        drawnRows.push(el)
      }
      if (rowCache.size > view.rows.length) {
        const live = new Set(view.rows.map(r => r.key))
        for (const key of Array.from(rowCache.keys())) if (!live.has(key)) rowCache.delete(key)
      }
    }

    // 4. Note or Capped banner
    const banners: RenderElement[] = []
    if (view.note) {
      banners.push(
        <Text key="files-view-note" dimColor={true} color={FT.dim}>{clip(view.note, W)}</Text>,
      )
    }
    if (view.capped) {
      banners.push(
        <Text key="files-view-capped" dimColor={true} color={FT.dim}>{clip('tree stopped at its limit', W)}</Text>,
      )
    }

    // 6. Footer: legend and hotkeys j, k, h, l; one line when wide, two lines (legend, then keys) otherwise
    const footerRow = W >= 64
    const footer = (
      <Box flexDirection={footerRow ? 'row' : 'column'} justifyContent="space-between" marginTop={1}>
        <Box flexDirection="row" gap={1}>
          <Text color={FT.write}>●</Text>
          <Text dimColor={true} color={FT.dim}>writing ·</Text>
          <Text color={FT.read}>◉</Text>
          <Text dimColor={true} color={FT.dim}>reading · fades 3s</Text>
        </Box>
        <Box flexDirection="row" gap={1} justifyContent="flex-end">
          {Button({ key: 'files-key-j', hotkey: 'j', plain: true, label: 'down', onPress: () => moveSel(1) })}
          {Button({ key: 'files-key-k', hotkey: 'k', plain: true, label: 'up', onPress: () => moveSel(-1) })}
          {Button({ key: 'files-key-h', hotkey: 'h', plain: true, label: 'fold', onPress: handleH })}
          {Button({ key: 'files-key-l', hotkey: 'l', plain: true, label: 'open', onPress: handleL })}
        </Box>
      </Box>
    )

    return (
      <Box flexDirection="column" width={W} backgroundColor={FT.paneBg}>
        {chipRow}
        {card}
        <Box flexDirection="column" borderColor={FT.cardBorder} borderStyle="round" backgroundColor={FT.cardFill}>
          {headRow}
          {drawnRows}
        </Box>
        {banners.length > 0 ? <Box flexDirection="column" marginTop={1}>{banners}</Box> : []}
        {footer}
      </Box>
    )
  } catch (err) {
    return <Text color={FT.dim}>Files could not draw: {String(err)}</Text>
  }
}
