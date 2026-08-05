import { useCallback, useEffect, useState } from 'react'
import { DatabaseIcon, RefreshCwIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

import { KnowledgeBase, listKnowledgeBases } from '@/api/lightrag'
import Button from '@/components/ui/Button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue
} from '@/components/ui/Select'
import { errorMessage } from '@/lib/utils'
import { useSettingsStore } from '@/stores/settings'

export default function KnowledgeBaseSelector() {
  const { t } = useTranslation()
  const apiKey = useSettingsStore.use.apiKey()
  const selectedKnowledgeBaseId = useSettingsStore.use.selectedKnowledgeBaseId()
  const setSelectedKnowledgeBaseId = useSettingsStore.use.setSelectedKnowledgeBaseId()
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([])
  const [loading, setLoading] = useState(false)

  const loadKnowledgeBases = useCallback(async () => {
    const credentialAtRequest = apiKey
    setLoading(true)
    try {
      const response = await listKnowledgeBases()
      if (useSettingsStore.getState().apiKey !== credentialAtRequest) return
      setKnowledgeBases(response.knowledge_bases)
      if (!response.knowledge_bases.some((item) => item.id === selectedKnowledgeBaseId)) {
        setSelectedKnowledgeBaseId(response.default_id)
        // The previous library may have been deleted in another session.
        // Remount consumers so stale polling and graph requests cannot keep
        // sending the removed ID after the store falls back to default.
        window.location.reload()
      }
    } catch (error) {
      if (useSettingsStore.getState().apiKey !== credentialAtRequest) return
      toast.error(
        t('knowledgeBases.loadError', {
          defaultValue: 'Failed to load knowledge bases: {{error}}',
          error: errorMessage(error)
        })
      )
    } finally {
      setLoading(false)
    }
  }, [apiKey, selectedKnowledgeBaseId, setSelectedKnowledgeBaseId, t])

  useEffect(() => {
    const initialLoadTimer = window.setTimeout(() => void loadKnowledgeBases(), 0)
    const handleCatalogChange = () => void loadKnowledgeBases()
    window.addEventListener('lightrag:knowledge-bases-changed', handleCatalogChange)
    return () => {
      window.clearTimeout(initialLoadTimer)
      window.removeEventListener('lightrag:knowledge-bases-changed', handleCatalogChange)
    }
  }, [loadKnowledgeBases])

  const handleChange = (knowledgeBaseId: string) => {
    if (knowledgeBaseId === selectedKnowledgeBaseId) return
    setSelectedKnowledgeBaseId(knowledgeBaseId)
    // A full remount clears document polling, graph WebGL state, and any
    // in-flight stream before the new request header becomes authoritative.
    window.location.reload()
  }

  return (
    <div className="ml-3 flex min-w-0 items-center gap-1">
      <DatabaseIcon className="size-3.5 shrink-0 text-emerald-500" aria-hidden="true" />
      <Select
        value={selectedKnowledgeBaseId}
        onValueChange={handleChange}
        disabled={loading || knowledgeBases.length === 0}
      >
        <SelectTrigger
          className="h-7 w-64 text-xs"
          aria-label={t('knowledgeBases.selector', 'Knowledge base')}
        >
          <SelectValue placeholder={t('knowledgeBases.loading', 'Loading knowledge bases…')} />
        </SelectTrigger>
        <SelectContent>
          {knowledgeBases.map((knowledgeBase) => (
            <SelectItem key={knowledgeBase.id} value={knowledgeBase.id}>
              {knowledgeBase.name} ({knowledgeBase.id})
              {knowledgeBase.isolation_level === 'physical' ? ' · P' : ''}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Button
        variant="ghost"
        size="icon"
        className="size-7"
        onClick={() => void loadKnowledgeBases()}
        disabled={loading}
        tooltip={t('knowledgeBases.refresh', 'Refresh knowledge bases')}
        aria-label={t('knowledgeBases.refresh', 'Refresh knowledge bases')}
      >
        <RefreshCwIcon className={loading ? 'size-3.5 animate-spin' : 'size-3.5'} />
      </Button>
    </div>
  )
}
