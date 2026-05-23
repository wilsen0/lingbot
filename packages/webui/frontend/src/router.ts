import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";

import { useAuthStore } from "@/store/auth";

const routes: RouteRecordRaw[] = [
  {
    path: "/login",
    name: "login",
    component: () => import("@/pages/Login.vue"),
    meta: { title: "缘起", layout: "bare" },
  },
  {
    path: "/",
    component: () => import("@/layouts/AppShell.vue"),
    children: [
      {
        path: "",
        name: "chat",
        component: () => import("@/pages/Chat.vue"),
        meta: { title: "对话", showBack: false, showTitle: false },
      },
      {
        path: "观测",
        name: "observatory",
        component: () => import("@/pages/Observatory.vue"),
        meta: { title: "观测", showBack: true },
      },
      {
        path: "设置",
        name: "settings",
        component: () => import("@/pages/Settings.vue"),
        meta: { title: "设置", showBack: true },
      },
    ],
  },
  { path: "/:pathMatch(.*)*", redirect: "/" },
];

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior(_to, _from, savedPosition) {
    return savedPosition ?? { top: 0 };
  },
});

router.beforeEach((to) => {
  if (to.name === "login") return true;
  const auth = useAuthStore();
  if (!auth.isAuthed) {
    return { name: "login", query: { next: to.fullPath } };
  }
  return true;
});
