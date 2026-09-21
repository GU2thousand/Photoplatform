export interface UploadSession {
  uploadId: string
  mediaId: number
  status: string
  embeddingStatus: string
  uploadUrl: string | null
  headers: Record<string, string>
  expiresAt: string
  errorCode: string | null
}

export async function uploadDirect(
  file: File,
  metadata: Record<string, unknown>,
  idempotencyKey: string,
  request: <T>(path: string, options: RequestInit) => Promise<T>,
  onStage: (stage: string) => void,
  signal: AbortSignal,
): Promise<UploadSession> {
  signal.throwIfAborted()
  onStage('Checking image…')
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer())
  const sha256 = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('')
  signal.throwIfAborted()
  const session = await request<UploadSession>('/api/uploads', {
    method: 'POST', signal, headers: { 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify({ ...metadata, filename: file.name, contentType: file.type, size: file.size, sha256 }),
  })
  signal.throwIfAborted()
  if (session.status !== 'UPLOADING') return session
  if (!session.uploadUrl) throw new Error('Upload session expired. Select the image again to start a new upload.')
  onStage('Uploading image…')
  const uploaded = await fetch(session.uploadUrl, {
    method: 'PUT', body: file, headers: session.headers, credentials: 'omit', signal,
  })
  // A retry of the same checksum-bound session may encounter an already-present object.
  if (!uploaded.ok && uploaded.status !== 412) throw new Error(`Image upload failed (${uploaded.status}). You can retry.`)
  signal.throwIfAborted()
  onStage('Verifying upload…')
  return request<UploadSession>(`/api/uploads/${session.uploadId}/complete`, { method: 'POST', signal })
}
