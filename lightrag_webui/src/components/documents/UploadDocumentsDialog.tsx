import { useState, useCallback, useEffect } from 'react'
import { FileRejection } from 'react-dropzone'
import Button from '@/components/ui/Button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger
} from '@/components/ui/Dialog'
import FileUploader from '@/components/ui/FileUploader'
import { toast } from 'sonner'
import { supportedFileTypes } from '@/lib/constants'
import {
  deriveUploaderInputs,
  flattenAcceptExtensions,
  formatFileTypesLabel,
  normalizeSupportedFileTypes,
  type FileTypesState
} from '@/lib/fileTypes'
import { errorMessage } from '@/lib/utils'
import {
  createKnowledgeBase,
  getSupportedFileTypes,
  KnowledgeBase,
  listKnowledgeBases,
  StorageProfileSummary,
  uploadDocument
} from '@/api/lightrag'
import Input from '@/components/ui/Input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue
} from '@/components/ui/Select'
import { useSettingsStore } from '@/stores/settings'
import { useGraphStore } from '@/stores/graph'
import {
  buildKnowledgeBaseUploadOptions,
  NEW_KNOWLEDGE_BASE_UPLOAD_TARGET
} from './uploadKnowledgeBaseOptions'

import { UploadIcon } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface UploadDocumentsDialogProps {
  onDocumentsUploaded?: () => Promise<void>
  /**
   * Fired once per batch as soon as the first file is accepted by the server.
   * Lets the parent start its activity probe as early as possible (rather
   * than waiting for the whole sequential batch to finish).
   */
  onUploadBatchAccepted?: () => void
}

export default function UploadDocumentsDialog({
  onDocumentsUploaded,
  onUploadBatchAccepted
}: UploadDocumentsDialogProps) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const [isUploading, setIsUploading] = useState(false)
  const [progresses, setProgresses] = useState<Record<string, number>>({})
  const [fileErrors, setFileErrors] = useState<Record<string, string>>({})
  const [uploadTarget, setUploadTarget] = useState(NEW_KNOWLEDGE_BASE_UPLOAD_TARGET)
  const [newKnowledgeBaseName, setNewKnowledgeBaseName] = useState('')
  const [newIsolationLevel, setNewIsolationLevel] = useState<'logical' | 'physical'>('logical')
  const [newStorageProfileId, setNewStorageProfileId] = useState('')
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([])
  const [storageProfiles, setStorageProfiles] = useState<StorageProfileSummary[]>([])
  const [loadingTargets, setLoadingTargets] = useState(false)
  const selectedKnowledgeBaseId = useSettingsStore.use.selectedKnowledgeBaseId()
  const setSelectedKnowledgeBaseId = useSettingsStore.use.setSelectedKnowledgeBaseId()
  const uploadOptions = buildKnowledgeBaseUploadOptions(knowledgeBases)

  useEffect(() => {
    if (!open) return

    let active = true
    void listKnowledgeBases()
      .then((response) => {
        if (!active) return
        setKnowledgeBases(response.knowledge_bases)
        setStorageProfiles(
          response.storage_profiles.filter((profile) => profile.available && profile.dedicated)
        )
      })
      .catch((error) => {
        if (!active) return
        toast.error(
          t('knowledgeBases.loadError', {
            defaultValue: 'Failed to load knowledge bases: {{error}}',
            error: errorMessage(error)
          })
        )
      })
      .finally(() => {
        if (active) setLoadingTargets(false)
      })

    return () => {
      active = false
    }
  }, [open, t])

  const [fileTypes, setFileTypes] = useState<FileTypesState>({ status: 'idle' })

  // Fetch the live allowlist + engine capability matrix while the dialog is
  // open. `loading` is entered synchronously in onOpenChange (not here) so
  // the very first open render already has the uploader disabled.
  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    getSupportedFileTypes(controller.signal)
      .then((res) => {
        if (controller.signal.aborted) return
        const data = normalizeSupportedFileTypes(res)
        setFileTypes(data ? { status: 'ready', data } : { status: 'fallback' })
      })
      .catch((err) => {
        if (controller.signal.aborted) return
        // Old backend (404) or transient failure: fall back to the static
        // allowlist and let the server judge hinted filenames.
        console.warn('Failed to fetch supported file types:', errorMessage(err))
        setFileTypes({ status: 'fallback' })
      })
    return () => controller.abort()
  }, [open])

  const handleRejectedFiles = useCallback(
    (rejectedFiles: FileRejection[]) => {
      // Process rejected files and add them to fileErrors
      rejectedFiles.forEach(({ file, errors }) => {
        // Get the first error message
        let errorMsg = errors[0]?.message || t('documentPanel.uploadDocuments.fileUploader.fileRejected', { name: file.name })

        // Simplify error message for unsupported file types
        if (errorMsg.includes('file-invalid-type')) {
          errorMsg = t('documentPanel.uploadDocuments.fileUploader.unsupportedType')
        }

        // Set progress to 100% to display error message
        setProgresses((pre) => ({
          ...pre,
          [file.name]: 100
        }))

        // Add error message to fileErrors
        setFileErrors(prev => ({
          ...prev,
          [file.name]: errorMsg
        }))
      })
    },
    [setProgresses, setFileErrors, t]
  )

  const handleDocumentsUpload = useCallback(
    async (filesToUpload: File[]) => {
      setIsUploading(true)
      let hasSuccessfulUpload = false

      // Only clear errors for files that are being uploaded, keep errors for rejected files
      setFileErrors(prev => {
        const newErrors = { ...prev };
        filesToUpload.forEach(file => {
          delete newErrors[file.name];
        });
        return newErrors;
      });

      // Show uploading toast
      const toastId = toast.loading(t('documentPanel.uploadDocuments.batch.uploading'))

      try {
        let targetKnowledgeBaseId = uploadTarget
        let createdKnowledgeBase: KnowledgeBase | null = null
        const selectedKnowledgeBaseAtStart = selectedKnowledgeBaseId

        if (uploadTarget === NEW_KNOWLEDGE_BASE_UPLOAD_TARGET) {
          const name = newKnowledgeBaseName.trim()
          if (!name) {
            throw new Error(
              t('knowledgeBases.nameRequired', 'Enter a name for the new knowledge base')
            )
          }
          if (newIsolationLevel === 'physical' && !newStorageProfileId) {
            throw new Error(
              t('knowledgeBases.profileRequired', 'Select an available storage profile')
            )
          }
          createdKnowledgeBase = await createKnowledgeBase({
            name,
            isolation_level: newIsolationLevel,
            storage_profile_id:
              newIsolationLevel === 'physical' ? newStorageProfileId : null
          })
          targetKnowledgeBaseId = createdKnowledgeBase.id
          window.dispatchEvent(new CustomEvent('lightrag:knowledge-bases-changed'))
        }

        // Track errors locally to ensure we have the final state
        const uploadErrors: Record<string, string> = {}
        let batchProbeTriggered = false

        // Create a collator that supports Chinese sorting
        const collator = new Intl.Collator(['zh-CN', 'en'], {
          sensitivity: 'accent',  // consider basic characters, accents, and case
          numeric: true           // enable numeric sorting, e.g., "File 10" will be after "File 2"
        });
        const sortedFiles = [...filesToUpload].sort((a, b) =>
          collator.compare(a.name, b.name)
        );

        // Upload files in sequence, not parallel
        for (const file of sortedFiles) {
          try {
            // Initialize upload progress
            setProgresses((pre) => ({
              ...pre,
              [file.name]: 0
            }))

            const result = await uploadDocument(
              file,
              (percentCompleted: number) => {
                console.debug(t('documentPanel.uploadDocuments.single.uploading', { name: file.name, percent: percentCompleted }))
                setProgresses((pre) => ({
                  ...pre,
                  [file.name]: percentCompleted
                }))
              },
              targetKnowledgeBaseId
            )

            if (result.status !== 'success') {
              uploadErrors[file.name] = result.message
              setFileErrors(prev => ({
                ...prev,
                [file.name]: result.message
              }))
            } else {
              // Mark that we had at least one successful upload
              hasSuccessfulUpload = true
              if (!batchProbeTriggered) {
                batchProbeTriggered = true
                if (targetKnowledgeBaseId === selectedKnowledgeBaseAtStart) {
                  onUploadBatchAccepted?.()
                }
              }
            }
          } catch (err) {
            console.error(`Upload failed for ${file.name}:`, err)

            // Handle HTTP errors, including 400 errors
            let errorMsg = errorMessage(err)
            const duplicateFileMsg = t('documentPanel.uploadDocuments.fileUploader.duplicateFile')

            // If it's an axios error with response data, try to extract more detailed error info
            if (err && typeof err === 'object' && 'response' in err) {
              const axiosError = err as { response?: { status: number, data?: { detail?: string } } }
              const status = axiosError.response?.status
              const detail = axiosError.response?.data?.detail
              if (status === 409) {
                // Server now rejects same-name uploads with HTTP 409 instead of
                // returning a 200 ``status="duplicated"`` payload.  Map the most
                // common cases (existing record / file in INPUT dir) back to the
                // dedicated "duplicate file" UI affordance, and surface other
                // 409 reasons (pipeline busy / scanning) verbatim from the
                // server detail so users can tell why they were rejected.
                if (
                  typeof detail === 'string' &&
                  (/already contains/i.test(detail) || /Status:/i.test(detail))
                ) {
                  errorMsg = duplicateFileMsg
                } else {
                  errorMsg = detail || errorMsg
                }
              } else if (status === 400) {
                errorMsg = detail || errorMsg
              }

              // Set progress to 100% to display error message
              setProgresses((pre) => ({
                ...pre,
                [file.name]: 100
              }))
            }

            // Record error message in both local tracking and state
            uploadErrors[file.name] = errorMsg
            setFileErrors(prev => ({
              ...prev,
              [file.name]: errorMsg
            }))
          }
        }

        // Check if any files failed to upload using our local tracking
        const hasErrors = Object.keys(uploadErrors).length > 0

        // Update toast status
        if (hasErrors) {
          toast.error(t('documentPanel.uploadDocuments.batch.error'), { id: toastId })
        } else {
          toast.success(t('documentPanel.uploadDocuments.batch.success'), { id: toastId })
        }

        // Only update if at least one file was uploaded successfully
        if (hasSuccessfulUpload) {
          if (targetKnowledgeBaseId !== selectedKnowledgeBaseAtStart) {
            setSelectedKnowledgeBaseId(targetKnowledgeBaseId)
            useGraphStore.getState().reset()
            window.location.reload()
          } else if (onDocumentsUploaded) {
            onDocumentsUploaded().catch(err => {
              console.error('Error refreshing documents:', err)
            })
          }
        } else if (createdKnowledgeBase) {
          toast.warning(
            t(
              'knowledgeBases.createdWithoutUpload',
              'The knowledge base was created, but no file was accepted. You can select it and retry.'
            )
          )
        }
      } catch (err) {
        console.error('Unexpected error during upload:', err)
        toast.error(t('documentPanel.uploadDocuments.generalError', { error: errorMessage(err) }), { id: toastId })
      } finally {
        setIsUploading(false)
      }
    },
    [
      setIsUploading,
      setProgresses,
      setFileErrors,
      t,
      onDocumentsUploaded,
      onUploadBatchAccepted,
      uploadTarget,
      newKnowledgeBaseName,
      newIsolationLevel,
      newStorageProfileId,
      selectedKnowledgeBaseId,
      setSelectedKnowledgeBaseId
    ]
  )

  const uploaderInputs = deriveUploaderInputs(fileTypes)

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (isUploading) {
          return
        }
        if (nextOpen) {
          setLoadingTargets(true)
          // Enter loading synchronously so the first open render already has
          // the uploader disabled — no window where a hinted file could start
          // uploading before the capability matrix arrives.
          setFileTypes({ status: 'loading' })
        } else {
          setProgresses({})
          setFileErrors({})
          setUploadTarget(NEW_KNOWLEDGE_BASE_UPLOAD_TARGET)
          setNewKnowledgeBaseName('')
          setNewIsolationLevel('logical')
          setNewStorageProfileId('')
          setFileTypes({ status: 'idle' })
        }
        setOpen(nextOpen)
      }}
    >
      <DialogTrigger asChild>
        <Button variant="default" side="bottom" tooltip={t('documentPanel.uploadDocuments.tooltip')} size="sm">
          <UploadIcon /> {t('documentPanel.uploadDocuments.button')}
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-xl" onCloseAutoFocus={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>{t('documentPanel.uploadDocuments.title')}</DialogTitle>
          <DialogDescription>
            {t('documentPanel.uploadDocuments.description')}
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-2">
          <label className="text-sm font-medium" htmlFor="upload-knowledge-base-target">
            {t('knowledgeBases.uploadTarget', 'Upload destination')}
          </label>
          <Select value={uploadTarget} onValueChange={setUploadTarget} disabled={isUploading || loadingTargets}>
            <SelectTrigger id="upload-knowledge-base-target">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={NEW_KNOWLEDGE_BASE_UPLOAD_TARGET}>
                {t('knowledgeBases.createIsolated', 'Create an isolated knowledge base')}
              </SelectItem>
              {uploadOptions.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {uploadTarget === NEW_KNOWLEDGE_BASE_UPLOAD_TARGET && (
            <div className="grid gap-2">
              <Input
                value={newKnowledgeBaseName}
                onChange={(event) => setNewKnowledgeBaseName(event.target.value)}
                placeholder={t('knowledgeBases.namePlaceholder', 'Knowledge base name')}
                maxLength={128}
                disabled={isUploading}
                autoFocus
              />
              <Select
                value={newIsolationLevel}
                onValueChange={(value) => {
                  setNewIsolationLevel(value as 'logical' | 'physical')
                  if (value === 'logical') setNewStorageProfileId('')
                }}
                disabled={isUploading}
              >
                <SelectTrigger aria-label={t('knowledgeBases.isolationLevel', 'Isolation level')}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="logical">
                    {t('knowledgeBases.logicalIsolation', 'Logical isolation')}
                  </SelectItem>
                  <SelectItem value="physical">
                    {t('knowledgeBases.physicalIsolation', 'Physical isolation')}
                  </SelectItem>
                </SelectContent>
              </Select>
              {newIsolationLevel === 'physical' && (
                <Select
                  value={newStorageProfileId}
                  onValueChange={setNewStorageProfileId}
                  disabled={isUploading || storageProfiles.length === 0}
                >
                  <SelectTrigger aria-label={t('knowledgeBases.storageProfile', 'Storage profile')}>
                    <SelectValue
                      placeholder={t('knowledgeBases.selectStorageProfile', 'Select a storage profile')}
                    />
                  </SelectTrigger>
                  <SelectContent>
                    {storageProfiles.map((profile) => (
                      <SelectItem key={profile.id} value={profile.id}>
                        {profile.id}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </div>
          )}
          <p className="text-muted-foreground text-xs">
            {uploadTarget === NEW_KNOWLEDGE_BASE_UPLOAD_TARGET
              ? t(
                'knowledgeBases.isolatedHint',
                'A new RAG, graph, cache, document directory, and pipeline namespace will be created.'
              )
              : t(
                'knowledgeBases.incrementalHint',
                'The files will incrementally update the selected RAG, graph, and cache.'
              )}
          </p>
        </div>
        <FileUploader
          maxFileCount={Infinity}
          maxSize={200 * 1024 * 1024}
          description={t('documentPanel.uploadDocuments.fileTypes', {
            types: formatFileTypesLabel(
              uploaderInputs.acceptedExtensions ?? flattenAcceptExtensions(supportedFileTypes)
            )
          })}
          onUpload={handleDocumentsUpload}
          onReject={handleRejectedFiles}
          progresses={progresses}
          fileErrors={fileErrors}
          disabled={isUploading || uploaderInputs.disabled}
          acceptedExtensions={uploaderInputs.acceptedExtensions}
          engineCapabilities={uploaderInputs.engineCapabilities}
        />
      </DialogContent>
    </Dialog>
  )
}
