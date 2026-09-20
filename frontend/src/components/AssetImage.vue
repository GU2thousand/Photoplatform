<script setup lang="ts">
import { ref, watch } from 'vue'
import { buildAssetUrl, fetchProtectedAsset } from '../api'

const props = defineProps<{ path: string; alt: string; token?: string }>()
const source = ref('')
const error = ref('')
const attempt = ref(0)

watch(() => [props.path, props.token, attempt.value], async (_, __, onCleanup) => {
  const controller = new AbortController()
  let blobUrl = ''
  source.value = ''
  error.value = ''
  onCleanup(() => {
    controller.abort()
    if (blobUrl) URL.revokeObjectURL(blobUrl)
  })
  if (!props.token) {
    source.value = buildAssetUrl(props.path)
    return
  }
  try {
    blobUrl = await fetchProtectedAsset(props.path, props.token, controller.signal)
    if (controller.signal.aborted) {
      URL.revokeObjectURL(blobUrl)
      return
    }
    source.value = blobUrl
  } catch (cause) {
    if (!controller.signal.aborted) {
      error.value = cause instanceof Error ? cause.message : 'Image could not be loaded.'
    }
  }
}, { immediate: true })
</script>

<template>
  <img v-if="source && !error" :src="source" :alt="alt" loading="lazy" @error="error = 'Image could not be loaded.'" />
  <div v-else class="asset-placeholder" :aria-busy="!error">
    <span>{{ error || 'Loading image…' }}</span>
    <button v-if="error" class="inline-link" type="button" @click="attempt++">Retry image</button>
  </div>
</template>
