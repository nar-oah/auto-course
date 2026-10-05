import extractorSvelte from '@unocss/extractor-svelte';
import { defineConfig, presetWind3 } from 'unocss';

export default defineConfig({
  presets: [presetWind3()],
  extractors: [extractorSvelte()]
});
