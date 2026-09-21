<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { apiRequest, buildWebSocketUrl, expireLegacyMediaCookie } from './api'
import AssetImage from './components/AssetImage.vue'
import MediaSearch from './components/MediaSearch.vue'
import { uploadDirect } from './directUpload'
import OriginalAsset from './components/OriginalAsset.vue'
import { createTeamConnection, type ConnectionState } from './teamConnection'
import type {
  AuthResponse,
  DashboardStats,
  ImageAsset,
  ImagePage,
  ModerationStatus,
  TeamEvent,
  TeamSummary,
  UserProfile,
  ViewKey,
  Visibility,
} from './types'

const capabilities = ref({ directUpload: false, semanticSearch: false, maxUploadBytes: 15728640 })
const uploadStage = ref('Uploading…')
const uploadKey = ref(crypto.randomUUID())
let uploadController: AbortController | undefined
let refreshTimer: number | undefined
let refreshingProcessing = false
const duplicateNotice = ref('')

const storageKey = 'generate-cloud.session'

const session = reactive<{ token: string; currentUser: UserProfile | null }>({
  token: localStorage.getItem(storageKey) ?? '',
  currentUser: null,
})

const activeView = ref<ViewKey>('public')
const stats = ref<DashboardStats | null>(null)
const publicImages = ref<ImageAsset[]>([])
const publicPage = ref(0)
const publicTotal = ref(0)
const publicPages = ref(0)
const galleryError = ref('')
let galleryRequest: AbortController | undefined
let appliedFilters = { query: '', tag: '' }
const personalImages = ref<ImageAsset[]>([])
const teamImages = ref<ImageAsset[]>([])
const pendingImages = ref<ImageAsset[]>([])
const teams = ref<TeamSummary[]>([])
const liveFeed = ref<TeamEvent[]>([])
const activeTeamId = ref<number | null>(null)
const socketState = ref<ConnectionState>('offline')
const socketStatus = computed(() => ({
  offline: 'Offline', connecting: 'Connecting…', connected: 'Connected', reconnecting: 'Reconnecting…',
})[socketState.value])
const teamConnection = createTeamConnection({
  async requestTicket(teamId, signal) {
    const response = await apiRequest<{ ticket: string; expiresAt: string }>(
      `/api/teams/${teamId}/socket-ticket`, { method: 'POST', signal }, session.token,
    )
    return response.ticket
  },
  openSocket: (teamId, ticket) => new WebSocket(
    `${buildWebSocketUrl(`/ws/teams/${teamId}`)}?ticket=${encodeURIComponent(ticket)}`,
  ),
  onState(state) {
    socketState.value = state
    if (state === 'connected' && activeTeamId.value) {
      // Recover library changes that may have happened while disconnected.
      void loadTeamImages(activeTeamId.value).catch((error) => setNotice(asMessage(error), 'error'))
    }
  },
  onMessage(event) {
    liveFeed.value = [event, ...liveFeed.value].slice(0, 12)
    if (event.type.startsWith('IMAGE_') && activeTeamId.value) {
      void loadTeamImages(activeTeamId.value).catch((error) => setNotice(asMessage(error), 'error'))
    }
  },
  onUnavailable: (error) => setNotice(`Live updates unavailable: ${asMessage(error)}`, 'error'),
})

const loginForm = reactive({
  email: 'avery@generatecloud.local',
  password: 'creator123',
})

const registerForm = reactive({
  name: '',
  email: '',
  password: '',
})

const uploadForm = reactive<{
  title: string
  description: string
  category: string
  tags: string
  visibility: Visibility
  teamId: number | null
  file: File | null
}>({
  title: '',
  description: '',
  category: 'General',
  tags: '',
  visibility: 'PRIVATE',
  teamId: null,
  file: null,
})

const createTeamForm = reactive({
  name: '',
  description: '',
})

const inviteForm = reactive({
  email: 'sam@generatecloud.local',
})

const filters = reactive({
  query: '',
  tag: '',
})

const noteDraft = ref('')
const uploadInput = ref<HTMLInputElement | null>(null)

const notice = reactive<{
  tone: 'info' | 'success' | 'error'
  text: string
}>({
  tone: 'info',
  text: '',
})

const busy = reactive({
  auth: false,
  gallery: false,
  upload: false,
  team: false,
  admin: false,
})

const isAuthenticated = computed(() => Boolean(session.currentUser && session.token))
const isAdmin = computed(() => session.currentUser?.role === 'ADMIN')
const activeTeam = computed(() => teams.value.find((team) => team.id === activeTeamId.value) ?? null)
const availableViews = computed(() => {
  const views: Array<{ key: ViewKey; label: string }> = [{ key: 'public', label: 'Public Gallery' }]
  if (isAuthenticated.value) {
    views.push({ key: 'personal', label: 'My Space' })
    views.push({ key: 'team', label: 'Team Space' })
  }
  if (isAdmin.value) {
    views.push({ key: 'admin', label: 'Admin' })
  }
  return views
})

let noticeTimer: number | undefined

onMounted(async () => {
  try {
    capabilities.value = await apiRequest('/api/public/capabilities')
  } catch {
    setNotice('Cloud upload features are unavailable. Standard uploads are still available.', 'error')
  }
  refreshTimer = window.setInterval(refreshProcessingImages, 4000)
  expireLegacyMediaCookie()
  teamConnection.setOnline(navigator.onLine)
  window.addEventListener('online', updateNetworkState)
  window.addEventListener('offline', updateNetworkState)
  await refreshPublicGallery()
  if (session.token) {
    await restoreSession()
  }
})

onBeforeUnmount(() => {
  teamConnection.stop()
  clearInterval(refreshTimer)
  uploadController?.abort()
  galleryRequest?.abort()
  window.removeEventListener('online', updateNetworkState)
  window.removeEventListener('offline', updateNetworkState)
  clearTimeout(noticeTimer)
})

watch(activeTeamId, async (teamId) => {
  teamConnection.stop()
  liveFeed.value = []
  teamImages.value = []
  noteDraft.value = ''
  if (teamId && isAuthenticated.value) {
    teamConnection.start(teamId)
    try {
      await loadTeamImages(teamId)
    } catch (error) {
      setNotice(asMessage(error), 'error')
    }
  }
})

function updateNetworkState() {
  teamConnection.setOnline(navigator.onLine)
}

function assetToken(image: ImageAsset) {
  return image.visibility === 'PUBLIC' && image.moderationStatus === 'APPROVED'
    ? undefined : session.token
}

function setNotice(text: string, tone: 'info' | 'success' | 'error' = 'info') {
  notice.text = text
  notice.tone = tone
  clearTimeout(noticeTimer)
  noticeTimer = window.setTimeout(() => {
    notice.text = ''
  }, 3800)
}

function formatDate(value: string) {
  return new Date(value).toLocaleString()
}

async function restoreSession() {
  const token = session.token
  try {
    const user = await apiRequest<UserProfile>('/api/auth/me', {}, token)
    if (session.token !== token) return
    session.currentUser = user
    await hydratePrivateData()
  } catch {
    if (session.token !== token) return
    clearSession(false)
    setNotice('Previous session expired. Please sign in again.', 'error')
  }
}

async function refreshPublicGallery() {
  await loadPublicPage(false)
}

async function loadPublicPage(append: boolean) {
  if (append && (busy.gallery || publicPage.value + 1 >= publicPages.value)) return
  galleryRequest?.abort()
  const request = new AbortController()
  galleryRequest = request
  busy.gallery = true
  galleryError.value = ''
  if (!append) {
    appliedFilters = { query: filters.query.trim(), tag: filters.tag.trim() }
    publicImages.value = []
    publicTotal.value = 0
    publicPages.value = 0
  }
  const params = new URLSearchParams({
    ...appliedFilters, page: String(append ? publicPage.value + 1 : 0), size: '24',
  })
  try {
    const [result, summary] = await Promise.all([
      apiRequest<ImagePage>(`/api/public/images?${params}`, { signal: request.signal }),
      apiRequest<DashboardStats>('/api/public/summary', { signal: request.signal }),
    ])
    if (request.signal.aborted) return
    publicImages.value = append ? [...publicImages.value, ...result.items] : result.items
    publicPage.value = result.page
    publicTotal.value = result.totalElements
    publicPages.value = result.totalPages
    if (!isAdmin.value) stats.value = summary
  } catch (error) {
    if (!request.signal.aborted) galleryError.value = asMessage(error)
  } finally {
    if (galleryRequest === request) busy.gallery = false
  }
}

async function hydratePrivateData() {
  const work = [loadPersonalImages(), loadTeams()]
  if (isAdmin.value) {
    work.push(loadAdminData())
  }
  await Promise.all(work)
}

async function submitLogin() {
  busy.auth = true
  try {
    const response = await apiRequest<AuthResponse>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify(loginForm),
    })
    await applyAuth(response)
    setNotice(`Welcome back, ${response.user.name}.`, 'success')
  } catch (error) {
    setNotice(asMessage(error), 'error')
  } finally {
    busy.auth = false
  }
}

async function submitRegister() {
  busy.auth = true
  try {
    const response = await apiRequest<AuthResponse>('/api/auth/register', {
      method: 'POST',
      body: JSON.stringify(registerForm),
    })
    await applyAuth(response)
    setNotice('Account created. Your personal workspace is ready.', 'success')
  } catch (error) {
    setNotice(asMessage(error), 'error')
  } finally {
    busy.auth = false
  }
}

async function applyAuth(response: AuthResponse) {
  session.token = response.token
  session.currentUser = response.user
  localStorage.setItem(storageKey, response.token)
  registerForm.name = ''
  registerForm.email = ''
  registerForm.password = ''
  activeView.value = response.user.role === 'ADMIN' ? 'admin' : 'personal'
  await refreshPublicGallery()
  await hydratePrivateData()
}

function clearSession(showMessage = true) {
  uploadController?.abort()
  uploadController = undefined
  busy.upload = false
  resetUploadForm()
  duplicateNotice.value = ''
  session.token = ''
  session.currentUser = null
  localStorage.removeItem(storageKey)
  expireLegacyMediaCookie()
  personalImages.value = []
  teamImages.value = []
  pendingImages.value = []
  teams.value = []
  liveFeed.value = []
  activeTeamId.value = null
  stats.value = null
  activeView.value = 'public'
  teamConnection.stop()
  if (showMessage) {
    setNotice('Signed out.', 'info')
  }
}

async function loadPersonalImages() {
  const token = session.token
  const images = await apiRequest<ImageAsset[]>('/api/images/me', {}, token)
  if (session.token === token) personalImages.value = images
}

async function loadTeams() {
  busy.team = true
  try {
    const token = session.token
    const result = await apiRequest<TeamSummary[]>('/api/teams', {}, token)
    if (session.token !== token) return
    teams.value = result
    const firstTeam = teams.value[0]
    if (!activeTeamId.value && firstTeam) {
      activeTeamId.value = firstTeam.id
      uploadForm.teamId = firstTeam.id
    }
    if (teams.value.length === 0) {
      activeTeamId.value = null
    }
  } finally {
    busy.team = false
  }
}

async function loadTeamImages(teamId: number) {
  const token = session.token
  const images = await apiRequest<ImageAsset[]>(`/api/teams/${teamId}/images`, {}, token)
  if (session.token === token && activeTeamId.value === teamId) teamImages.value = images
}

async function loadAdminData() {
  busy.admin = true
  try {
    const token = session.token
    const [summary, pending] = await Promise.all([
      apiRequest<DashboardStats>('/api/admin/stats', {}, token),
      apiRequest<ImageAsset[]>('/api/admin/images/pending', {}, token),
    ])
    if (session.token !== token) return
    stats.value = summary
    pendingImages.value = pending
  } finally {
    busy.admin = false
  }
}

async function submitUpload() {
  if (busy.upload || !isAuthenticated.value) return
  if (!uploadForm.file) {
    setNotice('Select an image before uploading.', 'error')
    return
  }

  const teamId = uploadForm.visibility === 'TEAM' ? uploadForm.teamId ?? activeTeamId.value : null
  if (uploadForm.visibility === 'TEAM' && !teamId) {
    setNotice('Choose a team for shared uploads.', 'error')
    return
  }

  const payload = new FormData()
  payload.append('file', uploadForm.file)
  payload.append('title', uploadForm.title)
  payload.append('description', uploadForm.description)
  payload.append('category', uploadForm.category)
  payload.append('tags', uploadForm.tags)
  payload.append('visibility', uploadForm.visibility)
  if (teamId) {
    payload.append('teamId', String(teamId))
  }

  const token = session.token
  const controller = new AbortController()
  uploadController = controller
  busy.upload = true
  uploadStage.value = 'Uploading…'
  try {
    let pendingModeration = false
    let processing = false
    if (capabilities.value.directUpload) {
      if (uploadForm.file.size > capabilities.value.maxUploadBytes) throw new Error('Image exceeds the upload size limit.')
      const result = await uploadDirect(uploadForm.file, {
        title: uploadForm.title, description: uploadForm.description, category: uploadForm.category,
        tags: uploadForm.tags, visibility: uploadForm.visibility, teamId,
      }, uploadKey.value, (path, options) => apiRequest(path, options, token),
      stage => { if (!controller.signal.aborted) uploadStage.value = stage }, controller.signal)
      if (['FAILED', 'ABORTED', 'DELETING', 'DELETED'].includes(result.status)) {
        throw new Error('This upload is no longer active. Select the file again to start a new upload.')
      }
      processing = result.status !== 'READY'
      pendingModeration = uploadForm.visibility === 'PUBLIC' && !isAdmin.value
    } else {
      const image = await apiRequest<ImageAsset>('/api/images', { method: 'POST', body: payload, signal: controller.signal }, token)
      pendingModeration = image.moderationStatus === 'PENDING'
    }

    if (controller.signal.aborted || session.token !== token) return
    resetUploadForm()
    await refreshPublicGallery()
    if (controller.signal.aborted || session.token !== token) return
    await loadPersonalImages()
    if (controller.signal.aborted || session.token !== token) return
    if (teamId) {
      await loadTeamImages(teamId)
    }
    if (controller.signal.aborted || session.token !== token) return
    if (isAdmin.value) {
      await loadAdminData()
    }

    if (controller.signal.aborted || session.token !== token) return
    setNotice(
      processing ? 'Upload received. Your image is being processed.' : pendingModeration
        ? 'Upload received. It is waiting for admin approval.'
        : 'Image uploaded successfully.',
      'success',
    )
  } catch (error) {
    if (!controller.signal.aborted && session.token === token) setNotice(asMessage(error), 'error')
  } finally {
    if (uploadController === controller) {
      busy.upload = false
      uploadController = undefined
    }
  }
}

async function refreshProcessingImages() {
  if (!isAuthenticated.value || refreshingProcessing) return
  const processing = [...personalImages.value, ...teamImages.value, ...pendingImages.value].some(image =>
    ['UPLOADING', 'PROCESSING'].includes(image.processingStatus) || image.embeddingStatus === 'QUEUED')
  // Team uploads may originate in another browser. Refresh the active library periodically.
  if (!processing && !['team', 'admin'].includes(activeView.value)) return
  refreshingProcessing = true
  const token = session.token
  try {
    await loadPersonalImages()
    if (session.token !== token) return
    if (activeTeamId.value) await loadTeamImages(activeTeamId.value)
    if (session.token !== token) return
    if (isAdmin.value) await loadAdminData()
  } catch { /* Explicit retry remains available in the library. */ }
  finally { refreshingProcessing = false }
}

async function retryProcessing(id: number) {
  const token = session.token
  try {
    await apiRequest(`/api/images/${id}/retry`, { method: 'POST' }, token)
    if (session.token !== token) return
    await loadPersonalImages()
    if (session.token !== token) return
    if (activeTeamId.value) await loadTeamImages(activeTeamId.value)
    if (session.token === token && isAdmin.value) await loadAdminData()
  } catch (error) { setNotice(asMessage(error), 'error') }
}

async function findDuplicates(id: number) {
  const token = session.token
  duplicateNotice.value = 'Finding similar images…'
  try {
    const matches = await apiRequest<Array<{ title: string; exact: boolean }>>(`/api/images/${id}/duplicates`, {}, token)
    if (session.token !== token) return
    duplicateNotice.value = matches.length ? `Possible matches: ${matches.map(match => `${match.title} (${match.exact ? 'identical' : 'similar'})`).join(', ')}. Nothing has been deleted.` : 'No similar images found in images you can access.'
  } catch (error) {
    if (session.token === token) {
      duplicateNotice.value = ''
      setNotice(asMessage(error), 'error')
    }
  }
}

function resetUploadForm() {
  uploadKey.value = crypto.randomUUID()
  uploadForm.title = ''
  uploadForm.description = ''
  uploadForm.category = 'General'
  uploadForm.tags = ''
  uploadForm.visibility = 'PRIVATE'
  uploadForm.file = null
  if (uploadInput.value) {
    uploadInput.value.value = ''
  }
}

watch(() => [uploadForm.title, uploadForm.description, uploadForm.category, uploadForm.tags, uploadForm.visibility, uploadForm.teamId], () => {
  uploadKey.value = crypto.randomUUID()
}, { flush: 'sync' })

function handleFileChange(event: Event) {
  uploadKey.value = crypto.randomUUID()
  const input = event.target as HTMLInputElement
  uploadForm.file = input.files?.[0] ?? null
}

async function submitCreateTeam() {
  try {
    const team = await apiRequest<TeamSummary>('/api/teams', {
      method: 'POST',
      body: JSON.stringify(createTeamForm),
    }, session.token)
    createTeamForm.name = ''
    createTeamForm.description = ''
    await loadTeams()
    activeTeamId.value = team.id
    activeView.value = 'team'
    setNotice(`Team ${team.name} created.`, 'success')
  } catch (error) {
    setNotice(asMessage(error), 'error')
  }
}

async function submitInvite() {
  if (!activeTeamId.value) {
    setNotice('Select a team before inviting members.', 'error')
    return
  }

  try {
    const team = await apiRequest<TeamSummary>(`/api/teams/${activeTeamId.value}/members`, {
      method: 'POST',
      body: JSON.stringify(inviteForm),
    }, session.token)
    teams.value = teams.value.map((item) => (item.id === team.id ? team : item))
    inviteForm.email = ''
    setNotice('Member invited to the team space.', 'success')
  } catch (error) {
    setNotice(asMessage(error), 'error')
  }
}

async function deleteImage(imageId: number) {
  try {
    await apiRequest<void>(`/api/images/${imageId}`, { method: 'DELETE' }, session.token)
    await loadPersonalImages()
    if (activeTeamId.value) {
      await loadTeamImages(activeTeamId.value)
    }
    await refreshPublicGallery()
    if (isAdmin.value) {
      await loadAdminData()
    }
    setNotice('Image deleted.', 'success')
  } catch (error) {
    setNotice(asMessage(error), 'error')
  }
}

async function moderateImage(imageId: number, status: ModerationStatus) {
  try {
    await apiRequest<ImageAsset>(`/api/admin/images/${imageId}`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    }, session.token)
    await Promise.all([loadAdminData(), refreshPublicGallery(), loadPersonalImages()])
    setNotice(`Image ${status.toLowerCase()}.`, 'success')
  } catch (error) {
    setNotice(asMessage(error), 'error')
  }
}

function sendTeamNote() {
  const message = noteDraft.value.trim()
  if (!message) return
  if (!teamConnection.send(message)) {
    setNotice('Your note was not sent. Wait for the connection to return and try again.', 'error')
    return
  }
  noteDraft.value = ''
}

function asMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Unexpected error'
}
</script>

<template>
  <div class="page-shell">
    <div class="ambient ambient-one" />
    <div class="ambient ambient-two" />

    <header class="masthead">
      <div class="eyebrow">Intelligent Collaborative Cloud Image Platform</div>
      <div class="masthead__grid">
        <div class="masthead__copy">
          <h1>Generate Cloud</h1>
          <p class="lead">
            A full-stack image platform with public discovery, private asset management, team collaboration,
            moderation, and live team activity.
          </p>
          <div class="stat-strip">
            <article class="stat-card">
              <span>Users</span>
              <strong>{{ stats?.userCount ?? 0 }}</strong>
            </article>
            <article class="stat-card">
              <span>Images</span>
              <strong>{{ stats?.imageCount ?? 0 }}</strong>
            </article>
            <article class="stat-card">
              <span>Public</span>
              <strong>{{ stats?.publicImageCount ?? 0 }}</strong>
            </article>
            <article class="stat-card">
              <span>Teams</span>
              <strong>{{ stats?.teamCount ?? 0 }}</strong>
            </article>
            <article v-if="isAdmin" class="stat-card stat-card--warning">
              <span>Pending</span>
              <strong>{{ stats?.pendingModerationCount ?? 0 }}</strong>
            </article>
          </div>
        </div>

        <aside class="session-panel">
          <div class="panel-header">
            <span>{{ isAuthenticated ? 'Workspace Session' : 'Demo Access' }}</span>
            <strong>{{ isAuthenticated ? session.currentUser?.name : 'Sign in or register' }}</strong>
          </div>

          <template v-if="isAuthenticated">
            <p class="session-meta">
              {{ session.currentUser?.email }} · {{ session.currentUser?.role }}
            </p>
            <div class="nav-pills">
              <button
                v-for="view in availableViews"
                :key="view.key"
                class="pill"
                :class="{ 'pill--active': activeView === view.key }"
                type="button"
                @click="activeView = view.key"
              >
                {{ view.label }}
              </button>
            </div>
            <button class="button button--ghost" type="button" @click="clearSession()">
              Sign Out
            </button>
          </template>

          <template v-else>
            <p class="session-meta">
              Demo accounts: `admin@generatecloud.local / admin123`, `avery@generatecloud.local / creator123`,
              `sam@generatecloud.local / team123`
            </p>
            <form class="stack-form" @submit.prevent="submitLogin">
              <label>
                <span>Email</span>
                <input v-model="loginForm.email" type="email" autocomplete="username" placeholder="admin@generatecloud.local" />
              </label>
              <label>
                <span>Password</span>
                <input v-model="loginForm.password" type="password" autocomplete="current-password" placeholder="••••••••" />
              </label>
              <button class="button" type="submit" :disabled="busy.auth">
                {{ busy.auth ? 'Signing In...' : 'Sign In' }}
              </button>
            </form>

            <div class="divider"><span>or create a new account</span></div>

            <form class="stack-form" @submit.prevent="submitRegister">
              <label>
                <span>Name</span>
                <input v-model="registerForm.name" type="text" autocomplete="name" placeholder="Morgan Lee" />
              </label>
              <label>
                <span>Email</span>
                <input v-model="registerForm.email" type="email" autocomplete="email" placeholder="morgan@example.com" />
              </label>
              <label>
                <span>Password</span>
                <input v-model="registerForm.password" type="password" autocomplete="new-password" placeholder="At least 6 characters" />
              </label>
              <button class="button button--ghost" type="submit" :disabled="busy.auth">
                {{ busy.auth ? 'Creating...' : 'Create Account' }}
              </button>
            </form>
          </template>
        </aside>
      </div>
    </header>

    <p v-if="notice.text" role="status" class="notice" :class="`notice--${notice.tone}`">
      {{ notice.text }}
    </p>

    <p v-if="duplicateNotice" role="status" class="notice">{{ duplicateNotice }}</p>
    <main class="workspace">
      <MediaSearch v-if="capabilities.directUpload" :token="session.token || undefined" :semantic="capabilities.semanticSearch" />
      <section v-show="activeView === 'public'" class="surface">
        <div class="section-header">
          <div>
            <div class="eyebrow">Public Image Gallery</div>
            <h2>Browse approved image assets</h2>
          </div>
          <form class="search-bar" @submit.prevent="refreshPublicGallery">
            <input v-model="filters.query" type="text" placeholder="Search by title, category, or tags" />
            <input v-model="filters.tag" type="text" placeholder="Filter by tag" />
            <button class="button" type="submit" :disabled="busy.gallery">
              {{ busy.gallery ? 'Loading...' : 'Search' }}
            </button>
          </form>
        </div>

        <div class="gallery-grid">
            <article v-for="image in publicImages" :key="image.id" class="image-card">
            <AssetImage :path="image.thumbnailUrl" :alt="image.title" />
            <div class="image-card__body">
              <div class="image-card__meta">
                <span>{{ image.category }}</span>
                <span>{{ formatDate(image.createdAt) }}</span>
              </div>
              <h3>{{ image.title }}</h3>
              <p>{{ image.description }}</p>
              <div class="tag-row">
                <span v-for="tag in image.tags" :key="tag" class="tag">{{ tag }}</span>
              </div>
              <div class="image-card__footer">
                <span>{{ image.uploader.name }}</span>
                <OriginalAsset :path="image.imageUrl" :title="image.title" />
              </div>
            </div>
          </article>
        </div>

        <p v-if="galleryError" class="notice notice--error" role="alert">
          {{ galleryError }}
          <button class="inline-link" type="button" @click="loadPublicPage(publicImages.length > 0)">Retry</button>
        </p>
        <p v-if="busy.gallery" class="empty-state" role="status">Loading images…</p>
        <p v-else-if="!galleryError && !publicImages.length" class="empty-state">
          No public images match the current filters.
        </p>
        <div v-if="publicImages.length" class="gallery-pagination">
          <span>{{ publicImages.length }} of {{ publicTotal }} images</span>
          <button v-if="publicPage + 1 < publicPages" class="button button--ghost" type="button" :disabled="busy.gallery" @click="loadPublicPage(true)">
            {{ busy.gallery ? 'Loading…' : 'Load more' }}
          </button>
        </div>
      </section>

      <section v-if="isAuthenticated && activeView === 'personal'" class="surface surface--split">
        <div>
          <div class="section-header">
            <div>
              <div class="eyebrow">Personal Image Space</div>
              <h2>Upload and manage your private or shared assets</h2>
            </div>
          </div>

          <form class="upload-panel" @submit.prevent="submitUpload">
            <label>
              <span>Title</span>
              <input :disabled="busy.upload" v-model="uploadForm.title" type="text" placeholder="Campaign cover shot" required />
            </label>
            <label>
              <span>Description</span>
              <textarea :disabled="busy.upload" v-model="uploadForm.description" rows="3" placeholder="What is this image used for?" />
            </label>
            <div class="form-grid">
              <label>
                <span>Category</span>
                <input :disabled="busy.upload" v-model="uploadForm.category" type="text" placeholder="Marketing" />
              </label>
              <label>
                <span>Tags</span>
                <input :disabled="busy.upload" v-model="uploadForm.tags" type="text" placeholder="launch, hero, product" />
              </label>
            </div>
            <div class="form-grid">
              <label>
                <span>Visibility</span>
                <select v-model="uploadForm.visibility" :disabled="busy.upload">
                  <option value="PRIVATE">Private</option>
                  <option value="PUBLIC">Public</option>
                  <option value="TEAM">Team</option>
                </select>
              </label>
              <label>
                <span>Team</span>
                <select v-model="uploadForm.teamId" :disabled="busy.upload || uploadForm.visibility !== 'TEAM'">
                  <option :value="null">Select team</option>
                  <option v-for="team in teams" :key="team.id" :value="team.id">
                    {{ team.name }}
                  </option>
                </select>
              </label>
            </div>
            <label>
              <span>Image File</span>
              <input :disabled="busy.upload" ref="uploadInput" type="file" accept="image/jpeg,image/png,image/webp,image/gif,image/bmp" @change="handleFileChange" />
            </label>
            <button class="button" type="submit" :disabled="busy.upload">
              {{ busy.upload ? uploadStage : 'Upload Image' }}
            </button>
          </form>
        </div>

        <div>
          <div class="section-header">
            <div>
              <div class="eyebrow">My Library</div>
              <h2>{{ personalImages.length }} assets in your workspace</h2>
            </div>
          </div>

          <div class="stack-list">
            <article v-for="image in personalImages" :key="image.id" class="library-row">
              <AssetImage v-if="image.processingStatus === 'READY'" :path="image.thumbnailUrl" :alt="image.title" :token="assetToken(image)" />
              <div v-else class="asset-placeholder" role="status">{{ image.processingStatus.toLowerCase().replace(/_/g, ' ') }}</div>
              <div>
                <div class="library-row__meta">
                  <span class="tag">{{ image.visibility }}</span>
                  <span class="tag" :class="{ 'tag--warning': image.moderationStatus === 'PENDING' }">
                    {{ image.moderationStatus }}
                  </span>
                </div>
                <h3>{{ image.title }}</h3>
                <p>{{ image.description }}</p>
                <small>{{ formatDate(image.createdAt) }}</small>
                <OriginalAsset v-if="image.processingStatus === 'READY'" :path="image.imageUrl" :title="image.title" :token="assetToken(image)" />
                <button v-if="image.processingStatus === 'FAILED' && (isAdmin || image.uploader.id === session.currentUser?.id)" class="inline-link" type="button" @click="retryProcessing(image.id)">Retry processing</button>
                <button v-if="capabilities.directUpload && image.processingStatus === 'READY'" class="inline-link" type="button" @click="findDuplicates(image.id)">Find similar images</button>
              </div>
              <button class="button button--ghost" type="button" @click="deleteImage(image.id)">
                Delete
              </button>
            </article>
          </div>
        </div>
      </section>

      <section v-if="isAuthenticated && activeView === 'team'" class="surface team-layout">
        <div class="team-sidebar">
          <div class="section-header">
            <div>
              <div class="eyebrow">Team Collaboration</div>
              <h2>Shared image libraries and live updates</h2>
            </div>
          </div>

          <div class="team-list">
            <button
              v-for="team in teams"
              :key="team.id"
              class="team-chip"
              :class="{ 'team-chip--active': activeTeamId === team.id }"
              :aria-pressed="activeTeamId === team.id"
              type="button"
              @click="activeTeamId = team.id"
            >
              <strong>{{ team.name }}</strong>
              <span>{{ team.memberCount }} members</span>
            </button>
          </div>

          <form class="stack-form team-form" @submit.prevent="submitCreateTeam">
            <label>
              <span>Create team</span>
              <input v-model="createTeamForm.name" type="text" placeholder="Atlas Studio" />
            </label>
            <label>
              <span>Description</span>
              <textarea v-model="createTeamForm.description" rows="3" placeholder="Shared creative mission" />
            </label>
            <button class="button" type="submit">Create Team</button>
          </form>

          <form v-if="activeTeam" class="stack-form team-form" @submit.prevent="submitInvite">
            <label>
              <span>Invite member by email</span>
              <input v-model="inviteForm.email" type="email" placeholder="sam@generatecloud.local" />
            </label>
            <button class="button button--ghost" type="submit">Invite Member</button>
          </form>
        </div>

        <div class="team-main">
          <div v-if="activeTeam" class="team-headline">
            <div>
              <div class="eyebrow">Active Team</div>
              <h2>{{ activeTeam.name }}</h2>
              <p>{{ activeTeam.description }}</p>
              <p class="team-note">
                `Atlas Studio` is the seeded demo team for the PRD's Team Collaboration Space:
                shared library, member invites, and realtime collaboration feed.
              </p>
            </div>
            <div class="member-pillbox">
              <span v-for="member in activeTeam.members" :key="member.id" class="tag">
                {{ member.name }} · {{ member.teamRole }}
              </span>
            </div>
          </div>

          <div class="gallery-grid gallery-grid--compact">
            <article v-for="image in teamImages" :key="image.id" class="image-card">
              <AssetImage v-if="image.processingStatus === 'READY'" :path="image.thumbnailUrl" :alt="image.title" :token="assetToken(image)" />
              <div v-else class="asset-placeholder" role="status">{{ image.processingStatus.toLowerCase().replace(/_/g, ' ') }}</div>
              <div class="image-card__body">
                <div class="image-card__meta">
                  <span>{{ image.category }}</span>
                  <span>{{ image.uploader.name }}</span>
                </div>
                <h3>{{ image.title }}</h3>
                <p>{{ image.description }}</p>
                <OriginalAsset v-if="image.processingStatus === 'READY'" :path="image.imageUrl" :title="image.title" :token="assetToken(image)" />
                <button v-if="image.processingStatus === 'FAILED' && (isAdmin || image.uploader.id === session.currentUser?.id)" class="inline-link" type="button" @click="retryProcessing(image.id)">Retry processing</button>
                <button v-if="capabilities.directUpload && image.processingStatus === 'READY'" class="inline-link" type="button" @click="findDuplicates(image.id)">Find similar images</button>
                <div class="tag-row">
                  <span v-for="tag in image.tags" :key="tag" class="tag">{{ tag }}</span>
                </div>
              </div>
            </article>
          </div>

          <div class="feed-panel">
            <div class="feed-panel__header">
              <div>
                <div class="eyebrow">Realtime Feed</div>
                <h3>Live team activity</h3>
              </div>
              <span class="socket-status" role="status" :class="{ 'socket-status--live': socketState === 'connected' }">
                {{ socketStatus }}
              </span>
            </div>

            <div class="feed-stream">
              <article v-for="event in liveFeed" :key="`${event.occurredAt}-${event.message}`" class="feed-item">
                <strong>{{ event.type }}</strong>
                <p>{{ event.message }}</p>
                <small>{{ formatDate(event.occurredAt) }}</small>
              </article>
              <p v-if="!liveFeed.length" class="empty-state">
                Open a team to start receiving realtime updates.
              </p>
            </div>

            <p id="connection-help" class="connection-help" role="status">
              {{ socketState === 'connected' ? 'Live updates are connected. Your team can receive notes.' : 'Notes can be sent when connected. Your draft will stay here while reconnecting.' }}
            </p>
            <form class="feed-input" @submit.prevent="sendTeamNote">
              <input v-model="noteDraft" type="text" maxlength="240" aria-label="Collaboration note" aria-describedby="connection-help" placeholder="Post a quick collaboration note (up to 240 characters)" />
              <button class="button" type="submit" :disabled="socketState !== 'connected' || !noteDraft.trim()">Send</button>
            </form>
          </div>
        </div>
      </section>

      <section v-if="isAdmin && activeView === 'admin'" class="surface">
        <div class="section-header">
          <div>
            <div class="eyebrow">Admin Management</div>
            <h2>Review uploads and monitor platform status</h2>
          </div>
        </div>

        <div class="stack-list">
          <article v-for="image in pendingImages" :key="image.id" class="review-row">
            <AssetImage v-if="image.processingStatus === 'READY'" :path="image.thumbnailUrl" :alt="image.title" :token="assetToken(image)" />
              <div v-else class="asset-placeholder" role="status">{{ image.processingStatus.toLowerCase().replace(/_/g, ' ') }}</div>
            <div>
              <div class="library-row__meta">
                <span class="tag">{{ image.category }}</span>
                <span class="tag tag--warning">{{ image.moderationStatus }}</span>
              </div>
              <h3>{{ image.title }}</h3>
              <p>{{ image.description }}</p>
              <small>{{ image.uploader.name }}</small>
              <OriginalAsset v-if="image.processingStatus === 'READY'" :path="image.imageUrl" :title="image.title" :token="assetToken(image)" />
                <button v-if="image.processingStatus === 'FAILED' && (isAdmin || image.uploader.id === session.currentUser?.id)" class="inline-link" type="button" @click="retryProcessing(image.id)">Retry processing</button>
                <button v-if="capabilities.directUpload && image.processingStatus === 'READY'" class="inline-link" type="button" @click="findDuplicates(image.id)">Find similar images</button>
            </div>
            <div class="review-actions">
              <button class="button" type="button" @click="moderateImage(image.id, 'APPROVED')">
                Approve
              </button>
              <button class="button button--ghost" type="button" @click="moderateImage(image.id, 'REJECTED')">
                Reject
              </button>
            </div>
          </article>
          <p v-if="!pendingImages.length" class="empty-state">
            Nothing is waiting for moderation.
          </p>
        </div>
      </section>
    </main>
  </div>
</template>
