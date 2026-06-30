// Daemon JSON-RPC response types (docs/prod/desktop-plan.md "JSON-RPC"). No runtime schema validation —
// the daemon lives in the same repo and ships version-locked with this renderer.

/** status: granted | denied | not_determined | unsupported, etc. */
export interface PermissionEntry {
  status: string
  detail?: string
}

export interface PermissionsReport {
  permissions: Record<string, PermissionEntry>
  collectors: Record<string, PermissionEntry>
  missingRequiredPermissions: string[]
}

/** service.status response. */
export interface ServiceStatus {
  platform: string
  collectorsSupported: boolean
  running: boolean
  paused: boolean
  pid: number | null
  dbPath: string
  migrationVersion: number
  permissions: PermissionsReport
}

export type ScreenTextState = 'off' | 'blocked' | 'ready' | 'indexing' | 'error'

export interface ScreenTextProviderStatus {
  name: string
  available: boolean
  reason: string | null
  detail: string | null
}

export interface ScreenTextLastError {
  jobId: string
  jobType: string
  status: string
  message: string
  type: string | null
  symbol: string | null
  updatedAt: string
}

export interface ScreenTextStatus {
  state: ScreenTextState
  reason: string | null
  settings: {
    enabled: boolean
    retainScreenshots: boolean
  }
  enabled: boolean
  effectiveEnabled: boolean
  screenshotCollectorEnabled: boolean
  workersEnabled: boolean
  developmentOverrides: {
    screenshotCollector: boolean
    workers: boolean
  }
  screenRecordingPermission: PermissionEntry
  requiredPermissions: string[]
  provider: ScreenTextProviderStatus
  backlogCount: number
  pendingJobCount: number
  runningJobCount: number
  retryableJobCount: number
  deadJobCount: number
  latestOcrAt: string | null
  latestIndexedAt: string | null
  lastError: ScreenTextLastError | null
  statusError: string | null
  screenshotRetention: 'retain' | 'delete_after_indexing' | string
}

/** context.current response — any field is null when nothing is recorded. */
export interface CurrentContext {
  app: string | null
  window: string | null
  url: string | null
  activity: string | null
}

/** One context.rank row. */
export interface RankedContext {
  score: number
  visits: number
  kind: string
  label: string
}

/** context.search row — context substring or screen text match; uri is normalized only for kind=url. */
export interface SearchResult {
  key: string
  kind: string
  label: string
  uri: string | null
  score: number
  visits: number
  lastSeenAt: string
  matchSource?: 'context' | 'ocr' | 'context+ocr'
  snippet?: string | null
  /** base64 JPEG thumbnail of a representative screenshot; null when none on disk. */
  image?: string | null
}

/** Time span where the keyword appeared in context metadata or screen text. */
export interface SearchEpisode {
  startedAt: string
  endedAt: string | null
  app: string | null
  window: string | null
  url: string | null
  matchSource?: 'context' | 'ocr'
  snippet?: string | null
}

export interface SearchResponse {
  results: SearchResult[]
  episodes: SearchEpisode[]
}

/** screen.previews row — the first retained screenshot of a recent scene. */
export interface ScenePreview {
  key: string
  frameId: string
  label: string
  appName: string | null
  windowTitle: string | null
  url: string | null
  kind: string
  capturedAt: string
  lastSeenAt: string
  /** base64 JPEG data URL of a downscaled thumbnail. */
  image: string
}

export interface ScenePreviewResponse {
  previews: ScenePreview[]
}

/** storage.stats — local storage footprint for the Stats page. */
export interface StorageStats {
  databaseBytes: number
  screenshotBytes: number
  screenshotCount: number
  logBytes: number
  totalBytes: number
  sessions: number
  frames: number
  retainedScreenshots: number
  indexedChunks: number
  scenesCaptured: number
  scenesWithOcr: number
}

/** One log row inside a scene (raw activity event). */
export interface SceneLog {
  timestamp: string
  type: string
  app: string | null
  window: string | null
  url: string | null
}

/** scene.timeline row — one scene (session) with its keyframe and logs. */
export interface Scene {
  id: string
  label: string
  kind: string
  appName: string | null
  windowTitle: string | null
  url: string | null
  file: string | null
  startedAt: string
  endedAt: string | null
  /** base64 JPEG keyframe thumbnail; null when the scene has no retained shot. */
  image: string | null
  ocrShots: number
  recordCount: number
  logs: SceneLog[]
}

export interface SceneTimeline {
  scenes: Scene[]
}

/** One context.timeline row. */
export interface TimelineEvent {
  timestamp: string
  type: string
  app: string | null
  window: string | null
  url: string | null
}

/** mcp.status entry — enabled === null means the config file failed to parse (error: "parse_error"). */
export interface McpTargetStatus {
  detected: boolean
  enabled: boolean | null
  configPath: string
  error?: string
}

export interface McpStatus {
  claudeCode: McpTargetStatus
  codex: McpTargetStatus
}

/** mcp.enable / mcp.disable target param. */
export type McpSetupTarget = 'claude-code' | 'codex'

/** mcp.enable / mcp.disable response. */
export interface McpSetupResult {
  enabled: boolean
  backupPath: string | null
}

// Renderer-side mirror of src/main/bridge-request.ts (manual sync — this file
// is also type-checked under tsconfig.node via test imports, so it cannot use
// env.d.ts globals or the DOM lib).
export interface BridgeErrorPayload {
  code: number
  /** Machine-readable symbol (e.g. "INVALID_PARAMS") for renderer branching. */
  message: string
  /** Human-readable (Korean) text from the daemon. */
  data?: unknown
}

export type BridgeRequestEnvelope =
  | { ok: true; result: unknown }
  | { ok: false; error: BridgeErrorPayload }

/** Daemon bridge error with the machine symbol preserved for branching. */
export class DaemonError extends Error {
  readonly code: number
  /** Stable machine symbol (envelope error.message, e.g. "INVALID_PARAMS"). */
  readonly symbol: string
  /** Human-readable text (envelope error.data, falling back to the symbol). */
  readonly human: string

  constructor(payload: BridgeErrorPayload) {
    const human =
      typeof payload.data === 'string' && payload.data !== '' ? payload.data : payload.message
    super(human)
    this.name = 'DaemonError'
    this.code = payload.code
    this.symbol = payload.message
    this.human = human
  }
}

/** Unwrap a preload envelope: result on ok, throw DaemonError otherwise. */
export function unwrapEnvelope(envelope: BridgeRequestEnvelope): unknown {
  if (envelope.ok) return envelope.result
  throw new DaemonError(envelope.error)
}

// Module-scoped view of window.melone — shadows the DOM global so this module
// compiles without the DOM lib.
declare const window: {
  melone: {
    request: (method: string, params?: Record<string, unknown>) => Promise<BridgeRequestEnvelope>
  }
}

/** Shared sinceMinutes/limit window for the context.* methods. */
export interface TimeWindowParams {
  sinceMinutes?: number
  limit?: number
}

/** Wire contract per method — a typo in a method name is a compile error. */
export interface RpcMethodMap {
  'app.ping': { params: undefined; result: { version: string } }
  'service.status': { params: undefined; result: ServiceStatus }
  'service.start': { params: undefined; result: { started: boolean; pid: number | null } }
  'service.stop': { params: undefined; result: { stopped: boolean } }
  'service.pause': { params: undefined; result: { paused: boolean } }
  'service.resume': { params: undefined; result: { paused: boolean } }
  'screenText.status': { params: undefined; result: ScreenTextStatus }
  'screenText.updateSettings': { params: { enabled: boolean }; result: ScreenTextStatus }
  'context.current': { params: undefined; result: CurrentContext }
  'context.rank': { params: TimeWindowParams | undefined; result: RankedContext[] }
  'context.search': {
    params: { query: string } & TimeWindowParams
    result: SearchResponse
  }
  'context.timeline': { params: TimeWindowParams | undefined; result: TimelineEvent[] }
  'screen.previews': { params: { limit?: number } | undefined; result: ScenePreviewResponse }
  'scene.timeline': { params: { before?: string; limit?: number } | undefined; result: SceneTimeline }
  'storage.stats': { params: undefined; result: StorageStats }
  'mcp.status': { params: undefined; result: McpStatus }
  'mcp.enable': { params: { target: McpSetupTarget }; result: McpSetupResult }
  'mcp.disable': { params: { target: McpSetupTarget }; result: McpSetupResult }
  'events.addSample': { params: undefined; result: { eventId: string } }
  'events.seedDemo': { params: undefined; result: { inserted: number } }
}

/** Typed daemon call: methods without params take none, the rest are checked. */
export async function requestDaemon<M extends keyof RpcMethodMap>(
  method: M,
  ...args: undefined extends RpcMethodMap[M]['params']
    ? [params?: RpcMethodMap[M]['params']]
    : [params: RpcMethodMap[M]['params']]
): Promise<RpcMethodMap[M]['result']> {
  const envelope = await window.melone.request(
    method,
    args[0] as Record<string, unknown> | undefined
  )
  return unwrapEnvelope(envelope) as RpcMethodMap[M]['result']
}

/** Human-readable message for captions — DaemonError already carries it in message. */
export function humanErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
