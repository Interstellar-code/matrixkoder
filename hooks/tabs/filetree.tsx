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
  q?: string
  sel?: string
}

function clip(str: string, maxLen: number): string {
  if (str.length <= maxLen) return str
  return maxLen <= 1 ? '…' : `${str.slice(0, maxLen - 1)}…`
}

function getParent(p: string): string {
  const cut = p.lastIndexOf('/')
  return cut <= 0 ? '/' : p.slice(0, cut)
}

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
      if (selectedRow.dir) {
        const currentOpen = parseOpenList(view, ui)
        const next = currentOpen.filter(p => p !== selectedRow.path)
        ui.set('files:open', JSON.stringify(next))
      } else {
        const parentPath = getParent(selectedRow.path)
        const parentRow = pathRows.find(r => r.dir && r.path === parentPath)
        if (parentRow) {
          ui.set('files:sel', parentRow.key)
          ui.reveal?.(parentRow.key)
        }
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
      <Box flexDirection="row" gap={1} flexWrap="wrap">
        {view.roots.map(r => {
          const isChosen = r.id === rootId
          return Button({
            key: `files-root:${r.id}`,
            plain: true,
            onPress: () => ui.set('files:root', r.id),
            children: [
              <Text
                backgroundColor={isChosen ? FT.activeChip : FT.chipFill}
                color={isChosen ? FT.white : FT.fileText}
                bold={isChosen}
              >
                {` ${clip(r.label, Math.max(4, W - 4))} `}
              </Text>,
            ],
          })
        })}
      </Box>
    )

    // 2. Card header with counts, root path, search box and expand/collapse buttons
    const innerCardW = Math.max(4, W - 4)
    const countText = `● ${view.nWrite} ◉ ${view.nRead}`
    const leftLabelBudget = Math.max(4, innerCardW - countText.length - 1)
    const clippedRootLabel = clip(activeRoot.label, leftLabelBudget)
    const clippedRootPath = clip(activeRoot.path, innerCardW)

    let searchInputEl: RenderElement
    if ('Input' in els) {
      const { Input } = els as unknown as { Input: (props: Record<string, unknown>) => RenderElement }
      searchInputEl = Input({
        key: 'files-q',
        placeholder: 's : search',
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
      searchInputEl = <Text dimColor={true}>{clip(view.q ? view.q : 's : search', Math.max(4, innerCardW - 3))}</Text>
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
          <Box flexDirection="row" gap={1}>
            <Text color={FT.write} dimColor={!view.pulse}>●</Text>
            <Text color={FT.fileText}>{String(view.nWrite)}</Text>
            <Text color={FT.read}>◉</Text>
            <Text color={FT.fileText}>{String(view.nRead)}</Text>
          </Box>
        </Box>
        <Text dimColor={true} color={FT.dim} wrap="truncate">{clippedRootPath}</Text>
        <Box flexDirection="row" gap={1} marginTop={1}>
          {searchInputEl}
          {Button({
            key: 'files-s',
            hotkey: 's',
            plain: true,
            label: 's',
            onPress: () => ui.focus?.('files-q'),
          })}
        </Box>
        <Box flexDirection="row" gap={1} justifyContent="flex-end" marginTop={1}>
          {Button({
            key: 'files-expand',
            plain: true,
            label: '▾ Expand all',
            onPress: () => ui.set('files:open', JSON.stringify(view.dirs ?? [])),
          })}
          {Button({
            key: 'files-collapse',
            plain: true,
            label: '▸ Collapse all',
            onPress: () => ui.set('files:open', JSON.stringify([])),
          })}
        </Box>
      </Box>
    )

    // 3. Tree rows
    const innerTreeW = Math.max(4, W - 2)
    const drawnRows: RenderElement[] = []
    if (view.rows.length === 0) {
      drawnRows.push(
        <Text key="files-no-matches" dimColor={true} color={FT.dim}>{clip('no file matches', innerTreeW)}</Text>,
      )
    } else {
      for (const row of view.rows) {
        const indentStr = ' '.repeat(Math.max(0, row.depth * 2))
        const isSelected = row.key === selKey

        // Note rows are rendered as non-selectable Text
        if (row.note === 'empty') {
          drawnRows.push(
            <Text key={row.key} dimColor={true} color={FT.dim}>
              {clip(`${indentStr}  (empty)`, innerTreeW)}
            </Text>,
          )
          continue
        }
        if (row.note === 'unlisted') {
          drawnRows.push(
            <Text key={row.key} dimColor={true} color={FT.dim}>
              {clip(`${indentStr}  (not listed)`, innerTreeW)}
            </Text>,
          )
          continue
        }
        if (row.note === 'more') {
          drawnRows.push(
            <Text key={row.key} dimColor={true} color={FT.dim}>
              {clip(`${indentStr}  ${row.count} more`, innerTreeW)}
            </Text>,
          )
          continue
        }

        // Regular path row
        const leftBar = isSelected ? '▌' : ' '
        const caret = row.dir ? (row.open ? '▾' : '▸') : ' '
        const displayName = row.link ? `${row.name} →` : row.name

        // Icon column (4 characters wide, right-aligned)
        let iconSymbol = ''
        let iconCountStr = ''
        if (row.icon === 'write') {
          iconSymbol = '●'
          if (row.dir && !row.open && row.count > 1) iconCountStr = ` ${row.count}`
        } else if (row.icon === 'read') {
          iconSymbol = '◉'
          if (row.dir && !row.open && row.count > 1) iconCountStr = ` ${row.count}`
        }
        const rightIconWidth = (iconSymbol + iconCountStr).length > 0 ? 4 : 0

        // Calculate name budget so entire line fits inside innerTreeW (W - 2)
        const prefixWidth = 1 + row.depth * 2 + 1 + 1 // bar (1) + indent + caret (1) + space (1)
        const nameBudget = Math.max(3, innerTreeW - prefixWidth - rightIconWidth - (rightIconWidth > 0 ? 1 : 0))
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

        const onRowPress = () => {
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

        drawnRows.push(
          Button({
            key: row.key,
            plain: true,
            onPress: onRowPress,
            children: [
              <Box flexDirection="row" backgroundColor={rowBg}>
                <Text color={isSelected ? FT.limeBar : undefined}>{leftBar}</Text>
                <Text>{indentStr}</Text>
                <Text color={FT.dim}>{caret}</Text>
                <Text> </Text>
                <Text color={nameColor} bold={isBold}>{clippedName}</Text>
                {rightIconWidth > 0 && (
                  <Box flexDirection="row" justifyContent="flex-end" flexGrow={1}>
                    <Text
                      color={row.icon === 'write' ? FT.write : FT.read}
                      dimColor={row.icon === 'write' ? !view.pulse : false}
                    >
                      {`${iconSymbol}${iconCountStr}`.padStart(4)}
                    </Text>
                  </Box>
                )}
              </Box>,
            ],
          }),
        )
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

    // 5. Under the tree line
    const countsStr = `${view.nFolders} folders · ${view.nFiles} files`
    const selPathStr = selectedRow ? selectedRow.path : ''
    const underBudget = Math.max(4, W - countsStr.length - 2)
    const underTree = (
      <Box flexDirection="row" justifyContent="space-between">
        <Text dimColor={true} color={FT.dim}>{countsStr}</Text>
        <Text dimColor={true} color={FT.dim}>{clip(selPathStr, underBudget)}</Text>
      </Box>
    )

    // 6. Footer line with legend and hotkeys j, k, h, l
    const footer = (
      <Box flexDirection="row" justifyContent="space-between" marginTop={1}>
        <Box flexDirection="row" gap={1}>
          <Text color={FT.write}>●</Text>
          <Text dimColor={true} color={FT.dim}>writing ·</Text>
          <Text color={FT.read}>◉</Text>
          <Text dimColor={true} color={FT.dim}>reading · fades 3s</Text>
        </Box>
        <Box flexDirection="row" gap={1}>
          {Button({ key: 'files-key-j', hotkey: 'j', plain: true, label: 'j', onPress: () => moveSel(1) })}
          {Button({ key: 'files-key-k', hotkey: 'k', plain: true, label: 'k', onPress: () => moveSel(-1) })}
          {Button({ key: 'files-key-h', hotkey: 'h', plain: true, label: 'h', onPress: handleH })}
          {Button({ key: 'files-key-l', hotkey: 'l', plain: true, label: 'l', onPress: handleL })}
        </Box>
      </Box>
    )

    return (
      <Box flexDirection="column" gap={1} width={W} backgroundColor={FT.paneBg}>
        {chipRow}
        {card}
        <Box flexDirection="column" borderColor={FT.cardBorder} borderStyle="round" backgroundColor={FT.cardFill}>
          {drawnRows}
        </Box>
        {banners}
        {underTree}
        {footer}
      </Box>
    )
  } catch (err) {
    return <Text color={FT.dim}>Files could not draw: {String(err)}</Text>
  }
}
