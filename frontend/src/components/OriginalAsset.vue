<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { buildAssetUrl, fetchProtectedAsset } from '../api'

const props = defineProps<{ path: string; title: string; token?: string }>()
const dialog = ref<HTMLDialogElement | null>(null)
const source = ref('')
const error = ref('')
const loading = ref(false)
let controller: AbortController | undefined

function close() {
  controller?.abort()
  controller = undefined
  dialog.value?.close()
  if (source.value.startsWith('blob:')) URL.revokeObjectURL(source.value)
  source.value = ''
  error.value = ''
  loading.value = false
}

async function open() {
  close()
  dialog.value?.showModal()
  const request = new AbortController()
  controller = request
  loading.value = true
  try {
    const url = await fetchProtectedAsset(props.path, props.token ?? '', request.signal)
    if (request.signal.aborted) {
      if (url.startsWith('blob:')) URL.revokeObjectURL(url)
    }
    else source.value = url
  } catch (cause) {
    if (!request.signal.aborted) error.value = cause instanceof Error ? cause.message : 'Image could not be loaded.'
  } finally {
    if (controller === request) loading.value = false
  }
}

function imageFailed() {
  if (source.value.startsWith('blob:')) URL.revokeObjectURL(source.value)
  source.value = ''
  error.value = 'Image could not be loaded. The access link may have expired.'
}

watch(() => [props.path, props.token], close)
onBeforeUnmount(close)
</script>

<template>
  <a v-if="!token" class="inline-link" :href="buildAssetUrl(path)" target="_blank" rel="noreferrer">Open original</a>
  <template v-else>
    <button class="inline-link" type="button" @click="open">Open original</button>
    <dialog ref="dialog" class="original-dialog" :aria-label="`${title} original image`" @cancel.prevent="close">
      <div class="original-dialog__header">
        <strong>{{ title }}</strong>
        <button class="button button--ghost" type="button" @click="close">Close</button>
      </div>
      <p v-if="loading" role="status">Loading original…</p>
      <p v-if="error" role="alert">{{ error }}</p>
      <button v-if="error" class="inline-link" type="button" @click="open">Retry image</button>
      <img v-if="source" :src="source" :alt="title" @error="imageFailed" />
      <a v-if="source" class="inline-link" :href="source" :download="title">Download original</a>
    </dialog>
  </template>
</template>
