import { describe, expect, test } from 'bun:test'

import type { KnowledgeBase } from '@/api/lightrag'
import {
  buildKnowledgeBaseUploadOptions,
  buildKnowledgeBaseUploadTargetOptions,
  formatKnowledgeBaseUploadLabel,
  NEW_KNOWLEDGE_BASE_UPLOAD_TARGET
} from './uploadKnowledgeBaseOptions'

const knowledgeBase = (id: string, name: string): KnowledgeBase => ({
  id,
  name,
  effective_workspace: id,
  isolation_level: 'logical',
  storage_profile_id: null,
  created_at: '2026-07-21T00:00:00Z',
  updated_at: '2026-07-21T00:00:00Z'
})

describe('knowledge-base upload targets', () => {
  test('reserves a sentinel that cannot collide with catalog IDs', () => {
    expect(NEW_KNOWLEDGE_BASE_UPLOAD_TARGET).toBe('__new_knowledge_base__')
  })

  test('keeps every catalog entry and displays both name and immutable ID', () => {
    const options = buildKnowledgeBaseUploadOptions([
      knowledgeBase('default', 'Default'),
      knowledgeBase('kb_a', 'Project'),
      knowledgeBase('kb_b', 'Project')
    ])

    expect(options.map((option) => option.value)).toEqual(['default', 'kb_a', 'kb_b'])
    expect(options.map((option) => option.label)).toEqual([
      'Default (default)',
      'Project (kb_a)',
      'Project (kb_b)'
    ])
    expect(formatKnowledgeBaseUploadLabel(knowledgeBase('kb_c', 'Archive'))).toBe(
      'Archive (kb_c)'
    )
  })

  test('puts create first in multi-workspace mode and removes it in legacy mode', () => {
    const records = [
      knowledgeBase('default', 'Default'),
      knowledgeBase('kb_a', 'Project A')
    ]

    expect(
      buildKnowledgeBaseUploadTargetOptions(records, true).map((option) => option.value)
    ).toEqual([NEW_KNOWLEDGE_BASE_UPLOAD_TARGET, 'default', 'kb_a'])
    expect(
      buildKnowledgeBaseUploadTargetOptions(records, false).map((option) => option.value)
    ).toEqual(['default', 'kb_a'])
  })
})
