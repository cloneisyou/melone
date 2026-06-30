import { describe, expect, it } from 'vitest'
import { LineBuffer } from '../src/main/rpc-client'

// Newline framing parser — transport contract: one line = one message, UTF-8, \n-terminated.
describe('LineBuffer', () => {
  it('accumulates partial chunks and joins them into one line at \\n', () => {
    const buffer = new LineBuffer()
    expect(buffer.push(Buffer.from('{"jsonrpc":'))).toEqual([])
    expect(buffer.push(Buffer.from('"2.0","id":1}'))).toEqual([])
    expect(buffer.push(Buffer.from('\n'))).toEqual(['{"jsonrpc":"2.0","id":1}'])
  })

  it('splits multiple messages packed into one chunk', () => {
    const buffer = new LineBuffer()
    expect(buffer.push(Buffer.from('{"id":1}\n{"id":2}\n{"id":3}\n'))).toEqual([
      '{"id":1}',
      '{"id":2}',
      '{"id":3}'
    ])
  })

  it('joins a message cut at the chunk boundary with the next chunk', () => {
    const buffer = new LineBuffer()
    expect(buffer.push(Buffer.from('{"id":1}\n{"id'))).toEqual(['{"id":1}'])
    expect(buffer.push(Buffer.from('":2}\n'))).toEqual(['{"id":2}'])
  })

  it('survives a UTF-8 multibyte character split across chunks', () => {
    const buffer = new LineBuffer()
    const encoded = Buffer.from('{"label":"한글 컨텍스트"}\n', 'utf8')
    // Cut right after the first byte of '한' (3 bytes).
    const cut = encoded.indexOf(Buffer.from('한', 'utf8')) + 1
    expect(buffer.push(encoded.subarray(0, cut))).toEqual([])
    expect(buffer.push(encoded.subarray(cut))).toEqual(['{"label":"한글 컨텍스트"}'])
  })

  it('strips \\r from CRLF line endings', () => {
    const buffer = new LineBuffer()
    expect(buffer.push(Buffer.from('{"id":1}\r\n{"id":2}\n'))).toEqual(['{"id":1}', '{"id":2}'])
  })

  it('reset drops any partially accumulated message', () => {
    const buffer = new LineBuffer()
    expect(buffer.push(Buffer.from('{"id'))).toEqual([])
    buffer.reset()
    expect(buffer.push(Buffer.from('{"id":1}\n'))).toEqual(['{"id":1}'])
  })
})
