/*
 * Daemon JSON-RPC method names. Never inline a method string at a call site —
 * reference RPC.* so the wire surface is enumerable in one place and a typo is a
 * type error. Mirrors the methods the Python daemon serves (melone_service.rpc).
 */
export const RPC = {
  context: {
    rank: 'context.rank',
    search: 'context.search'
  },
  scene: {
    timeline: 'scene.timeline'
  },
  screen: {
    previews: 'screen.previews'
  },
  screenText: {
    status: 'screenText.status',
    updateSettings: 'screenText.updateSettings'
  },
  service: {
    status: 'service.status',
    start: 'service.start',
    stop: 'service.stop',
    pause: 'service.pause',
    resume: 'service.resume'
  },
  mcp: {
    status: 'mcp.status',
    enable: 'mcp.enable',
    disable: 'mcp.disable'
  },
  storage: {
    stats: 'storage.stats'
  },
  events: {
    seedDemo: 'events.seedDemo'
  }
} as const
