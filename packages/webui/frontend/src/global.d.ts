declare const __APP_VERSION__: string;

declare module "pinia-plugin-persistedstate" {
  import type { PiniaPluginContext } from "pinia";
  const plugin: (ctx: PiniaPluginContext) => void;
  export default plugin;
}
