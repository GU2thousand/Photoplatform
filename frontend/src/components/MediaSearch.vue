<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { apiRequest } from '../api'
import type { ImageAsset } from '../types'
import AssetImage from './AssetImage.vue'
import OriginalAsset from './OriginalAsset.vue'

const props = defineProps<{ token?: string; semantic: boolean }>()
const query = ref('')
const mode = ref('keyword')
const scope = ref('public')
const results = ref<ImageAsset[]>([])
const loading = ref(false)
const searched = ref(false)
const error = ref('')
let request: AbortController | undefined
watch(() => props.token, () => {
  request?.abort()
  request = undefined
  results.value = []
  searched.value = false
  loading.value = false
  error.value = ''
  scope.value = 'public'
}, { flush: 'sync' })
watch(() => props.semantic, enabled => { if (!enabled) mode.value = 'keyword' })
onBeforeUnmount(() => request?.abort())

async function search() {
  request?.abort()
  const controller = new AbortController()
  request = controller
  loading.value = true
  error.value = ''
  searched.value = false
  results.value = []
  try {
    const parameters = new URLSearchParams({ q: query.value.trim(), mode: mode.value, scope: scope.value, limit: '24' })
    const response = await apiRequest<{ items: Array<{ image: ImageAsset }> }>(`/api/search?${parameters}`, { signal: controller.signal }, props.token)
    if (controller.signal.aborted || request !== controller) return
    results.value = response.items.map(hit => hit.image)
    searched.value = true
  } catch (cause) {
    if (!controller.signal.aborted) error.value = cause instanceof Error ? cause.message : 'Search is unavailable.'
  } finally { if (request === controller) loading.value = false }
}
</script>

<template>
  <details class="surface media-search">
    <summary>Find images by keywords or description</summary>
    <form class="search-bar" @submit.prevent="search">
      <input v-model="query" required maxlength="300" aria-label="Image search" placeholder="A yellow sports car at night" />
      <select v-model="mode" aria-label="Search method">
        <option value="keyword">Keywords</option>
        <option v-if="semantic" value="semantic">Meaning (English)</option>
        <option v-if="semantic" value="hybrid">Combined (English)</option>
      </select>
      <select v-model="scope" aria-label="Search scope">
        <option value="public">Public gallery</option>
        <option v-if="token" value="mine">My images</option>
        <option v-if="token" value="accessible">All images I can access</option>
      </select>
      <button class="button" :disabled="loading || !query.trim()">{{ loading ? 'Searching…' : 'Find images' }}</button>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="searched && !results.length && !error" role="status">No matching images. Try another description or keywords.</p>
    <div class="gallery-grid">
      <article v-for="image in results" :key="image.id" class="image-card">
        <AssetImage :path="image.thumbnailUrl" :alt="image.title" :token="token" />
        <div class="image-card__body">
          <h3>{{ image.title }}</h3>
          <p>{{ image.description }}</p>
          <OriginalAsset :path="image.imageUrl" :title="image.title" :token="token" />
        </div>
      </article>
    </div>
  </details>
</template>
