import type { KnowledgeBase } from '@/api/lightrag'

export const NEW_KNOWLEDGE_BASE_UPLOAD_TARGET = '__new_knowledge_base__'

export type KnowledgeBaseUploadOption = {
  value: string
  label: string
  knowledgeBase: KnowledgeBase
}

export function formatKnowledgeBaseUploadLabel(knowledgeBase: KnowledgeBase): string {
  return `${knowledgeBase.name} (${knowledgeBase.id})`
}

export function buildKnowledgeBaseUploadOptions(
  knowledgeBases: KnowledgeBase[]
): KnowledgeBaseUploadOption[] {
  return knowledgeBases.map((knowledgeBase) => ({
    value: knowledgeBase.id,
    label: formatKnowledgeBaseUploadLabel(knowledgeBase),
    knowledgeBase
  }))
}
