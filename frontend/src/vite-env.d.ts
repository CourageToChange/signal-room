/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DATA_SOURCE: 'api' | 'fixture'
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
