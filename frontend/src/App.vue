<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { apiRequest, buildAssetUrl, buildWebSocketUrl } from './api'
import type {
  AuthResponse,
  DashboardStats,
  ImageAsset,
  ModerationStatus,
  TeamEvent,
  TeamSummary,
  UserProfile,
  ViewKey,
  Visibility,
} from './types'

const storageKey = 'generate-cloud.session'

const session = reactive<{ token: string; currentUser: UserProfile | null }>({
  token: localStorage.getItem(storageKey) ?? '',
  currentUser: null,
})

const activeView = ref<ViewKey>('public')
const stats = ref<DashboardStats | null>(null)
const publicImages = ref<ImageAsset[]>([])
const personalImages = ref<ImageAsset[]>([])
const teamImages = ref<ImageAsset[]>([])
const pendingImages = ref<ImageAsset[]>([])
const teams = ref<TeamSummary[]>([])
const liveFeed = ref<TeamEvent[]>([])
const activeTeamId = ref<number | null>(null)
const teamSocket = ref<WebSocket | null>(null)

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
  await refreshPublicGallery()
  if (session.token) {
    await restoreSession()
  }
})

onBeforeUnmount(() => {
  disconnectTeamSocket()
  clearTimeout(noticeTimer)
})

watch(activeTeamId, async (teamId) => {
  disconnectTeamSocket()
  liveFeed.value = []
  if (teamId && isAuthenticated.value) {
    await loadTeamImages(teamId)
    connectTeamSocket(teamId)
  } else {
    teamImages.value = []
  }
})

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
  try {
    session.currentUser = await apiRequest<UserProfile>('/api/auth/me', {}, session.token)
    await hydratePrivateData()
  } catch {
    clearSession(false)
    setNotice('Previous session expired. Please sign in again.', 'error')
  }
}

async function refreshPublicGallery() {
  busy.gallery = true
  try {
    const params = new URLSearchParams()
    if (filters.query.trim()) {
      params.set('query', filters.query.trim())
    }
    if (filters.tag.trim()) {
      params.set('tag', filters.tag.trim())
    }

    const querySuffix = params.toString() ? `?${params.toString()}` : ''
    const [images, summary] = await Promise.all([
      apiRequest<ImageAsset[]>(`/api/public/images${querySuffix}`),
      apiRequest<DashboardStats>('/api/public/summary'),
    ])
    publicImages.value = images
    if (!isAdmin.value) {
      stats.value = summary
    }
  } catch (error) {
    setNotice(asMessage(error), 'error')
  } finally {
    busy.gallery = false
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
  session.token = ''
  session.currentUser = null
  localStorage.removeItem(storageKey)
  personalImages.value = []
  teamImages.value = []
  pendingImages.value = []
  teams.value = []
  liveFeed.value = []
  activeTeamId.value = null
  stats.value = null
  activeView.value = 'public'
  disconnectTeamSocket()
  if (showMessage) {
    setNotice('Signed out.', 'info')
  }
}

async function loadPersonalImages() {
  personalImages.value = await apiRequest<ImageAsset[]>('/api/images/me', {}, session.token)
}

async function loadTeams() {
  busy.team = true
  try {
    teams.value = await apiRequest<TeamSummary[]>('/api/teams', {}, session.token)
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
  teamImages.value = await apiRequest<ImageAsset[]>(`/api/teams/${teamId}/images`, {}, session.token)
}

async function loadAdminData() {
  busy.admin = true
  try {
    const [summary, pending] = await Promise.all([
      apiRequest<DashboardStats>('/api/admin/stats', {}, session.token),
      apiRequest<ImageAsset[]>('/api/admin/images/pending', {}, session.token),
    ])
    stats.value = summary
    pendingImages.value = pending
  } finally {
    busy.admin = false
  }
}

async function submitUpload() {
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

  busy.upload = true
  try {
    const image = await apiRequest<ImageAsset>('/api/images', {
      method: 'POST',
      body: payload,
    }, session.token)

    resetUploadForm()
    await refreshPublicGallery()
    await loadPersonalImages()
    if (teamId) {
      await loadTeamImages(teamId)
    }
    if (isAdmin.value) {
      await loadAdminData()
    }

    setNotice(
      image.moderationStatus === 'PENDING'
        ? 'Upload received. It is waiting for admin approval.'
        : 'Image uploaded successfully.',
      'success',
    )
  } catch (error) {
    setNotice(asMessage(error), 'error')
  } finally {
    busy.upload = false
  }
}

function resetUploadForm() {
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

function handleFileChange(event: Event) {
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

function connectTeamSocket(teamId: number) {
  if (!session.token) {
    return
  }

  const socket = new WebSocket(
    `${buildWebSocketUrl(`/ws/teams/${teamId}`)}?token=${encodeURIComponent(session.token)}`,
  )

  socket.onmessage = (event) => {
    const payload = JSON.parse(event.data) as TeamEvent
    liveFeed.value = [payload, ...liveFeed.value].slice(0, 12)
    if (payload.type === 'IMAGE_UPLOADED') {
      void loadTeamImages(teamId)
    }
  }

  socket.onclose = () => {
    if (teamSocket.value === socket) {
      teamSocket.value = null
    }
  }

  teamSocket.value = socket
}

function disconnectTeamSocket() {
  if (teamSocket.value) {
    teamSocket.value.close()
    teamSocket.value = null
  }
}

function sendTeamNote() {
  if (!noteDraft.value.trim() || teamSocket.value?.readyState !== WebSocket.OPEN) {
    return
  }
  teamSocket.value.send(noteDraft.value.trim())
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
                <input v-model="loginForm.email" type="email" placeholder="admin@generatecloud.local" />
              </label>
              <label>
                <span>Password</span>
                <input v-model="loginForm.password" type="password" placeholder="••••••••" />
              </label>
              <button class="button" type="submit" :disabled="busy.auth">
                {{ busy.auth ? 'Signing In...' : 'Sign In' }}
              </button>
            </form>

            <div class="divider"><span>or create a new account</span></div>

            <form class="stack-form" @submit.prevent="submitRegister">
              <label>
                <span>Name</span>
                <input v-model="registerForm.name" type="text" placeholder="Morgan Lee" />
              </label>
              <label>
                <span>Email</span>
                <input v-model="registerForm.email" type="email" placeholder="morgan@example.com" />
              </label>
              <label>
                <span>Password</span>
                <input v-model="registerForm.password" type="password" placeholder="At least 6 characters" />
              </label>
              <button class="button button--ghost" type="submit" :disabled="busy.auth">
                {{ busy.auth ? 'Creating...' : 'Create Account' }}
              </button>
            </form>
          </template>
        </aside>
      </div>
    </header>

    <p v-if="notice.text" class="notice" :class="`notice--${notice.tone}`">
      {{ notice.text }}
    </p>

    <main class="workspace">
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
            <img :src="buildAssetUrl(image.thumbnailUrl, session.token || undefined)" :alt="image.title" loading="lazy" />
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
                <a
                  class="inline-link"
                  :href="buildAssetUrl(image.imageUrl, session.token || undefined)"
                  target="_blank"
                  rel="noreferrer"
                >
                  Open original
                </a>
              </div>
            </div>
          </article>
        </div>

        <p v-if="!publicImages.length" class="empty-state">
          No public images match the current filters.
        </p>
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
              <input v-model="uploadForm.title" type="text" placeholder="Campaign cover shot" required />
            </label>
            <label>
              <span>Description</span>
              <textarea v-model="uploadForm.description" rows="3" placeholder="What is this image used for?" />
            </label>
            <div class="form-grid">
              <label>
                <span>Category</span>
                <input v-model="uploadForm.category" type="text" placeholder="Marketing" />
              </label>
              <label>
                <span>Tags</span>
                <input v-model="uploadForm.tags" type="text" placeholder="launch, hero, product" />
              </label>
            </div>
            <div class="form-grid">
              <label>
                <span>Visibility</span>
                <select v-model="uploadForm.visibility">
                  <option value="PRIVATE">Private</option>
                  <option value="PUBLIC">Public</option>
                  <option value="TEAM">Team</option>
                </select>
              </label>
              <label>
                <span>Team</span>
                <select v-model="uploadForm.teamId" :disabled="uploadForm.visibility !== 'TEAM'">
                  <option :value="null">Select team</option>
                  <option v-for="team in teams" :key="team.id" :value="team.id">
                    {{ team.name }}
                  </option>
                </select>
              </label>
            </div>
            <label>
              <span>Image File</span>
              <input ref="uploadInput" type="file" accept="image/*" @change="handleFileChange" />
            </label>
            <button class="button" type="submit" :disabled="busy.upload">
              {{ busy.upload ? 'Uploading...' : 'Upload Image' }}
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
              <img :src="buildAssetUrl(image.thumbnailUrl, session.token || undefined)" :alt="image.title" />
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
              <img :src="buildAssetUrl(image.thumbnailUrl, session.token || undefined)" :alt="image.title" />
              <div class="image-card__body">
                <div class="image-card__meta">
                  <span>{{ image.category }}</span>
                  <span>{{ image.uploader.name }}</span>
                </div>
                <h3>{{ image.title }}</h3>
                <p>{{ image.description }}</p>
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
              <span class="socket-status" :class="{ 'socket-status--live': teamSocket }">
                {{ teamSocket ? 'Connected' : 'Offline' }}
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

            <form class="feed-input" @submit.prevent="sendTeamNote">
              <input v-model="noteDraft" type="text" placeholder="Post a quick collaboration note" />
              <button class="button" type="submit">Send</button>
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
            <img :src="buildAssetUrl(image.thumbnailUrl, session.token || undefined)" :alt="image.title" />
            <div>
              <div class="library-row__meta">
                <span class="tag">{{ image.category }}</span>
                <span class="tag tag--warning">{{ image.moderationStatus }}</span>
              </div>
              <h3>{{ image.title }}</h3>
              <p>{{ image.description }}</p>
              <small>{{ image.uploader.name }} · {{ image.uploader.email }}</small>
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
