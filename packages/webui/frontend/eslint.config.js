import vue from "eslint-plugin-vue";
import vueParser from "vue-eslint-parser";
import tsParser from "@typescript-eslint/parser";
import tsPlugin from "@typescript-eslint/eslint-plugin";

const GLOBALS = {
  window: "readonly",
  document: "readonly",
  localStorage: "readonly",
  navigator: "readonly",
  performance: "readonly",
  requestAnimationFrame: "readonly",
  cancelAnimationFrame: "readonly",
  setTimeout: "readonly",
  clearTimeout: "readonly",
  fetch: "readonly",
  console: "readonly",
  process: "readonly",
  __APP_VERSION__: "readonly",
  Element: "readonly",
  HTMLElement: "readonly",
  HTMLCanvasElement: "readonly",
  HTMLInputElement: "readonly",
  CanvasRenderingContext2D: "readonly",
  MouseEvent: "readonly",
  KeyboardEvent: "readonly",
  Event: "readonly",
};

export default [
  { ignores: ["dist", "node_modules", "../src/linling_webui/static/**"] },
  ...vue.configs["flat/recommended"],
  {
    files: ["**/*.ts", "**/*.tsx"],
    languageOptions: {
      parser: tsParser,
      parserOptions: { ecmaVersion: 2022, sourceType: "module" },
      globals: GLOBALS,
    },
    plugins: { "@typescript-eslint": tsPlugin },
    rules: {
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      "no-unused-vars": "off",
    },
  },
  {
    files: ["**/*.vue"],
    languageOptions: {
      parser: vueParser,
      parserOptions: {
        parser: tsParser,
        ecmaVersion: 2022,
        sourceType: "module",
        extraFileExtensions: [".vue"],
      },
      globals: GLOBALS,
    },
    plugins: { "@typescript-eslint": tsPlugin },
    rules: {
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      "no-unused-vars": "off",
      "vue/multi-word-component-names": "off",
      "vue/no-v-html": "warn",
      "vue/html-self-closing": "off",
      "vue/max-attributes-per-line": "off",
      "vue/singleline-html-element-content-newline": "off",
      "vue/html-indent": "off",
      "vue/attributes-order": "off",
      "vue/html-closing-bracket-newline": "off",
      "vue/first-attribute-linebreak": "off",
    },
  },
  {
    /*
     * 测试里经常需要在一个 .ts 文件中现搓多个 defineComponent 当 host
     * (Mount Adapter), 不是真正的组件文件 — 关掉 one-component-per-file。
     */
    files: ["tests/**/*.ts", "tests/**/*.tsx"],
    rules: {
      "vue/one-component-per-file": "off",
    },
  },
];
