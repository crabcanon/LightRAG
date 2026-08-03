import type { KnowledgeBase } from '@/api/lightrag'

export const NEW_KNOWLEDGE_BASE_UPLOAD_TARGET = '__new_knowledge_base__'

export type KnowledgeBaseUploadOption = {
  kind: 'existing'
  value: string
  label: string
  knowledgeBase: KnowledgeBase
}

export type NewKnowledgeBaseUploadOption = {
  kind: 'create'
  value: typeof NEW_KNOWLEDGE_BASE_UPLOAD_TARGET
}

export type KnowledgeBaseUploadTargetOption =
  | NewKnowledgeBaseUploadOption
  | KnowledgeBaseUploadOption

export function formatKnowledgeBaseUploadLabel(knowledgeBase: KnowledgeBase): string {
  return `${knowledgeBase.name} (${knowledgeBase.id})`
}

export function buildKnowledgeBaseUploadOptions(
  knowledgeBases: KnowledgeBase[]
): KnowledgeBaseUploadOption[] {
  return knowledgeBases.map((knowledgeBase) => ({
    kind: 'existing',
    value: knowledgeBase.id,
    label: formatKnowledgeBaseUploadLabel(knowledgeBase),
    knowledgeBase
  }))
}

export function buildKnowledgeBaseUploadTargetOptions(
  knowledgeBases: KnowledgeBase[],
  allowCreate: boolean
): KnowledgeBaseUploadTargetOption[] {
  const existing = buildKnowledgeBaseUploadOptions(knowledgeBases)
  return allowCreate
    ? [{ kind: 'create', value: NEW_KNOWLEDGE_BASE_UPLOAD_TARGET }, ...existing]
    : existing
}
