import { setActivePinia, createPinia } from "pinia";
import { beforeEach, describe, expect, it } from "vitest";

import { useAuthStore } from "@/store/auth";

describe("auth store", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("starts unauthenticated", () => {
    const auth = useAuthStore();
    expect(auth.isAuthed).toBe(false);
  });

  it("setTokens flips isAuthed", () => {
    const auth = useAuthStore();
    auth.setTokens("a", "b");
    expect(auth.isAuthed).toBe(true);
    expect(auth.accessToken).toBe("a");
  });

  it("clear resets tokens", () => {
    const auth = useAuthStore();
    auth.setTokens("a", "b");
    auth.setProfile({ sub: "x", role: "superadmin", bots: [] });
    auth.clear();
    expect(auth.isAuthed).toBe(false);
    expect(auth.profile).toBeNull();
  });
});


describe("auth canWrite", () => {
  beforeEach(() => setActivePinia(createPinia()));

  it("is false without a profile", () => {
    const auth = useAuthStore();
    expect(auth.canWrite).toBe(false);
  });

  it("is true for superadmin and bot_admin", () => {
    const auth = useAuthStore();
    auth.setProfile({ sub: "x", role: "superadmin", bots: [] });
    expect(auth.canWrite).toBe(true);
    auth.setProfile({ sub: "x", role: "bot_admin", bots: ["b1"] });
    expect(auth.canWrite).toBe(true);
  });

  it("is false for readonly", () => {
    const auth = useAuthStore();
    auth.setProfile({ sub: "x", role: "readonly", bots: ["b1"] });
    expect(auth.canWrite).toBe(false);
  });
});
