import { afterEach, beforeAll, describe, expect, test } from 'bun:test'

type DocumentsRequest = {
  status_filter?: 'pending' | 'processing' | 'preprocessed' | 'processed' | 'failed' | null
  page: number
  page_size: number
  sort_field: 'created_at' | 'updated_at' | 'id' | 'file_path'
  sort_direction: 'asc' | 'desc'
}

type LightragApiModule = typeof import('./lightrag')
type SettingsModule = typeof import('@/stores/settings')
type KnowledgeBase = import('./lightrag').KnowledgeBase

const storageMock = () => {
  const data = new Map<string, string>()

  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value)
    },
    removeItem: (key: string) => {
      data.delete(key)
    },
    clear: () => {
      data.clear()
    }
  }
}

let apiModule: LightragApiModule
let settingsModule: SettingsModule

beforeAll(async () => {
  Object.defineProperty(globalThis, 'localStorage', {
    value: storageMock(),
    configurable: true
  })
  Object.defineProperty(globalThis, 'sessionStorage', {
    value: storageMock(),
    configurable: true
  })

  apiModule = await import('./lightrag')
  settingsModule = await import('@/stores/settings')
})

afterEach(() => {
  apiModule.__resetPaginatedDocumentRequestsForTests()
  settingsModule.useSettingsStore.getState().setSelectedKnowledgeBaseId('default')
})

describe('knowledge-base request routing', () => {
  test('omits the header for the backward-compatible default library', () => {
    settingsModule.useSettingsStore.getState().setSelectedKnowledgeBaseId('default')
    expect(apiModule.buildKnowledgeBaseHeaders()).toEqual({})
  })

  test('adds the selected knowledge-base ID to every data request', () => {
    settingsModule.useSettingsStore.getState().setSelectedKnowledgeBaseId('kb_project_a')
    expect(apiModule.buildKnowledgeBaseHeaders()).toEqual({
      'LIGHTRAG-KNOWLEDGE-BASE': 'kb_project_a'
    })
  })

  test('lets an explicit batch target override the globally selected library', () => {
    settingsModule.useSettingsStore.getState().setSelectedKnowledgeBaseId('kb_current')
    expect(apiModule.buildKnowledgeBaseHeaders('kb_upload_target')).toEqual({
      'LIGHTRAG-KNOWLEDGE-BASE': 'kb_upload_target'
    })
    expect(apiModule.buildKnowledgeBaseHeaders('default')).toEqual({})
  })

  test('classifies only data-plane URLs for automatic selector injection', () => {
    for (const url of [
      '/documents',
      '/documents/supported_file_types?live=true',
      '/query',
      '/query/stream',
      '/graph/entity/exists#result',
      '/graphs'
    ]) {
      expect(apiModule.isKnowledgeBaseDataPlaneUrl(url)).toBe(true)
    }
    for (const url of [
      '/health',
      '/ready',
      '/auth-status',
      '/login',
      '/knowledge-bases',
      '/knowledge-bases/operations/op_1',
      '/api/tags',
      undefined
    ]) {
      expect(apiModule.isKnowledgeBaseDataPlaneUrl(url)).toBe(false)
    }
  })
})

const knowledgeBase = (id: string, name: string): KnowledgeBase => ({
  id,
  name,
  effective_workspace: id,
  isolation_level: 'logical',
  storage_profile_id: null,
  created_at: '2026-08-03T00:00:00Z',
  updated_at: '2026-08-03T00:00:00Z',
  lifecycle_state: 'ACTIVE'
})

describe('knowledge-base catalog lifecycle', () => {
  test('collects every deterministic catalog page', async () => {
    const requestedCursors: Array<string | undefined> = []
    const result = await apiModule.collectKnowledgeBasePages(async (cursor) => {
      requestedCursors.push(cursor)
      if (!cursor) {
        return {
          default_id: 'default',
          knowledge_bases: [knowledgeBase('default', 'Default')],
          storage_profiles: [],
          next_cursor: 'default',
          multi_workspace_enabled: true,
          admin_key_required: true
        }
      }
      return {
        default_id: 'default',
        knowledge_bases: [knowledgeBase('kb_a', 'Project A')],
        storage_profiles: [],
        next_cursor: null,
        multi_workspace_enabled: true,
        admin_key_required: true
      }
    })

    expect(requestedCursors).toEqual([undefined, 'default'])
    expect(result.knowledge_bases.map((item) => item.id)).toEqual([
      'default',
      'kb_a'
    ])
    expect(result.next_cursor).toBeNull()
    expect(result.admin_key_required).toBe(true)
  })

  test('rejects a repeated catalog cursor instead of looping forever', async () => {
    await expect(
      apiModule.collectKnowledgeBasePages(async () => ({
        default_id: 'default',
        knowledge_bases: [],
        storage_profiles: [],
        next_cursor: 'same'
      }))
    ).rejects.toThrow('repeated page cursor')
  })

  test('unwraps successful lifecycle responses and surfaces failures', () => {
    const record = knowledgeBase('kb_a', 'Project A')
    const operation = {
      operation_id: 'op_1',
      workspace_id: record.id,
      state: 'SUCCEEDED' as const
    }
    expect(apiModule.resolveKnowledgeBaseMutation(record)).toEqual(record)
    expect(
      apiModule.resolveKnowledgeBaseMutation({
        knowledge_base: record,
        operation
      })
    ).toEqual(record)
    expect(
      apiModule.resolveKnowledgeBaseMutation({
        knowledge_base: record,
        operation: { ...operation, state: 'RUNNING' }
      })
    ).toBeNull()
    expect(() =>
      apiModule.resolveKnowledgeBaseMutation({
        knowledge_base: { ...record, error_message: 'safe failure' },
        operation: { ...operation, state: 'FAILED' }
      })
    ).toThrow('safe failure')
  })
})

describe('getDocumentsPaginated', () => {
  test('issues a fresh request after aborting a timed-out in-flight request', async () => {
    const request: DocumentsRequest = {
      status_filter: null,
      page: 1,
      page_size: 20,
      sort_field: 'updated_at',
      sort_direction: 'desc'
    }

    let callCount = 0
    const resolvers: Array<(value: any) => void> = []

    apiModule.__setPaginatedDocumentsPostForTests((_request, controller) => {
      callCount += 1

      return new Promise((resolve, reject) => {
        resolvers.push(resolve)
        controller.signal.addEventListener(
          'abort',
          () => reject(new DOMException('Aborted', 'AbortError')),
          { once: true }
        )
      })
    })

    const firstRequest = apiModule.getDocumentsPaginated(request)
    const secondRequest = apiModule.getDocumentsPaginated(request)

    expect(callCount).toBe(1)

    apiModule.abortDocumentsPaginated(request)
    const [firstResult, secondResult] = await Promise.allSettled([
      firstRequest,
      secondRequest
    ])
    expect(firstResult.status).toBe('rejected')
    expect(secondResult.status).toBe('rejected')

    const thirdRequest = apiModule.getDocumentsPaginated(request)
    expect(callCount).toBe(2)

    resolvers[1]({
      documents: [],
      pagination: {
        page: 1,
        page_size: 20,
        total_count: 0,
        total_pages: 0,
        has_next: false,
        has_prev: false
      },
      status_counts: { all: 0 }
    })

    await expect(thirdRequest).resolves.toEqual({
      documents: [],
      pagination: {
        page: 1,
        page_size: 20,
        total_count: 0,
        total_pages: 0,
        has_next: false,
        has_prev: false
      },
      status_counts: { all: 0 }
    })
  })

  test('times out hanging requests and allows a fresh retry', async () => {
    const request: DocumentsRequest = {
      status_filter: null,
      page: 1,
      page_size: 20,
      sort_field: 'updated_at',
      sort_direction: 'desc'
    }

    let callCount = 0
    const resolvers: Array<(value: any) => void> = []

    apiModule.__setPaginatedDocumentsPostForTests((_request, controller) => {
      callCount += 1

      return new Promise((resolve, reject) => {
        resolvers.push(resolve)
        controller.signal.addEventListener(
          'abort',
          () => reject(new DOMException('Aborted', 'AbortError')),
          { once: true }
        )
      })
    })

    await expect(
      apiModule.getDocumentsPaginatedWithTimeout(request, 1)
    ).rejects.toThrow('Document fetch timeout')

    expect(callCount).toBe(1)

    const retryRequest = apiModule.getDocumentsPaginated(request)
    expect(callCount).toBe(2)

    resolvers[1]({
      documents: [],
      pagination: {
        page: 1,
        page_size: 20,
        total_count: 0,
        total_pages: 0,
        has_next: false,
        has_prev: false
      },
      status_counts: { all: 0 }
    })

    await expect(retryRequest).resolves.toEqual({
      documents: [],
      pagination: {
        page: 1,
        page_size: 20,
        total_count: 0,
        total_pages: 0,
        has_next: false,
        has_prev: false
      },
      status_counts: { all: 0 }
    })
  })

  test('does not abort a shared request when only one timeout subscriber expires', async () => {
    const request: DocumentsRequest = {
      status_filter: null,
      page: 1,
      page_size: 20,
      sort_field: 'updated_at',
      sort_direction: 'desc'
    }

    let callCount = 0
    let resolveSharedRequest: ((value: any) => void) | undefined
    let abortCount = 0

    apiModule.__setPaginatedDocumentsPostForTests((_request, controller) => {
      callCount += 1

      return new Promise((resolve, reject) => {
        resolveSharedRequest = resolve
        controller.signal.addEventListener(
          'abort',
          () => {
            abortCount += 1
            reject(new DOMException('Aborted', 'AbortError'))
          },
          { once: true }
        )
      })
    })

    const shortTimeoutRequest = apiModule.getDocumentsPaginatedWithTimeout(request, 1)
    const longTimeoutRequest = apiModule.getDocumentsPaginatedWithTimeout(request, 100)

    await expect(shortTimeoutRequest).rejects.toThrow('Document fetch timeout')

    expect(callCount).toBe(1)
    expect(abortCount).toBe(0)

    resolveSharedRequest?.({
      documents: [],
      pagination: {
        page: 1,
        page_size: 20,
        total_count: 0,
        total_pages: 0,
        has_next: false,
        has_prev: false
      },
      status_counts: { all: 0 }
    })

    await expect(longTimeoutRequest).resolves.toEqual({
      documents: [],
      pagination: {
        page: 1,
        page_size: 20,
        total_count: 0,
        total_pages: 0,
        has_next: false,
        has_prev: false
      },
      status_counts: { all: 0 }
    })
  })
})

describe('isUserAbortError', () => {
  // Regression: the Stop button must suppress query cancellation everywhere it
  // surfaces — both the main stream catch and the guest-token retry catch (which
  // otherwise redirects an aborting guest to the login page). Both sites share
  // this predicate, so locking down its behavior guards both fixes.
  test('treats an aborted signal as a user abort regardless of the error', () => {
    const controller = new AbortController()
    controller.abort()
    expect(apiModule.isUserAbortError(controller.signal, new Error('boom'))).toBe(true)
  })

  test('treats an AbortError as a user abort even when the signal is absent', () => {
    const abortError = new DOMException('Aborted', 'AbortError')
    expect(apiModule.isUserAbortError(undefined, abortError)).toBe(true)
  })

  test('does not treat a real failure on a live signal as a user abort', () => {
    const controller = new AbortController()
    expect(apiModule.isUserAbortError(controller.signal, new Error('network down'))).toBe(false)
    expect(apiModule.isUserAbortError(undefined, new Error('network down'))).toBe(false)
  })
})
