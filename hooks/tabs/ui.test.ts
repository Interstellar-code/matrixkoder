// Pure unit tests for the /mk pane's shared UI helpers (S0.3): bar cell math,
// the quota-floor mark, legend fitting and column padding — exact-string pins
// because every tab module inherits these glyphs and widths.
import { test, expect } from 'claude-code/testing'
import { NARROW, WIDE, bar, fitLegend, leftCol, rightCol } from './ui'

test('bar rounds the used share into full and empty cells', () => {
  expect(bar(0, 4)).toBe('░░░░')
  expect(bar(100, 4)).toBe('████')
  expect(bar(50, 4)).toBe('██░░')
  expect(bar(null, 2)).toBe('░░')
  expect(bar(62, 7)).toBe('████░░░')
})

test('bar clamps pct above 100 instead of overflowing the width', () => {
  expect(bar(120, 4)).toBe('████')
})

test('bar stamps the floor mark at its column over fill and empty alike', () => {
  expect(bar(50, 8, 25)).toBe('██│█░░░░') // mark carved into the fill, cells elsewhere untouched
  expect(bar(50, 4, 100)).toBe('██░│') // index clamped to the last cell
  expect(bar(0, 4, 0)).toBe('│░░░')
  expect(bar(30, 10, 70)).toBe('███░░░░│░░')
})

test('bar without a floor never shows the mark', () => {
  expect(bar(50, 8)).toBe('████░░░░')
})

test('fitLegend drops items from the end until the line fits the width', () => {
  expect(fitLegend(['a', 'bb', 'ccc'], 6)).toBe('a · bb')
})

test('fitLegend keeps a line that fits exactly', () => {
  expect(fitLegend(['a', 'bb'], 6)).toBe('a · bb')
})

test('fitLegend never drops the last item even when it alone is too wide', () => {
  expect(fitLegend(['only'], 1)).toBe('only')
})

test('fitLegend renders an empty legend as an empty line', () => {
  expect(fitLegend([], 40)).toBe('')
})

test('leftCol pads right, rightCol pads left, both truncate overlong text', () => {
  expect(leftCol('ab', 4)).toBe('ab  ')
  expect(rightCol('ab', 4)).toBe('  ab')
  expect(leftCol('abcdef', 3)).toBe('abc')
  expect(rightCol('abcdef', 3)).toBe('abc')
})

test('NARROW and WIDE pin the pane widths the tabs render into', () => {
  expect(NARROW).toBe(40)
  expect(WIDE).toBe(120)
})
