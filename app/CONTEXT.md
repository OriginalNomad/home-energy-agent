# Sol Prototype — Session Context

*Read this at the start of every session. Then read ADVISOR.md and DESIGN.md if you're working on the conversation or UI respectively.*

---

## What this project is

Sol is a web prototype for a goal-driven home battery optimisation product. The core idea: instead of users configuring rules or schedules, they have a short conversation with an AI advisor that understands what they care about — then the system derives the optimisation policy from that.

This prototype demonstrates the onboarding flow: the conversational goal elicitation, site configuration, and profile saving. It is **not yet** connected to a live battery or solver — it's a UX/product prototype for testing the conversation design and saving user data for research.

The companion home automation system (the real Powerwall being controlled today) lives at `/Users/simonmonk/homeassistant/`. Read that project's `CONTEXT.md` for that side of the work.

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| Framework | Next.js 16.2.6, App Router, Turbopack |
| Language | TypeScript |
| Styling | Tailwind CSS v4 |
| AI | Vercel AI SDK v6 (`ai` ^6.0.185), `@ai-sdk/anthropic` ^3.0.78, `@ai-sdk/react` ^3.0.187 |
| LLM | Claude (claude-opus-4-5) via Anthropic API |
| Database | Supabase (Postgres) |
| Dev server | `npm run dev --port 3001` |

**Critical AI SDK v6 notes** (v6 is completely different from v4/v5):
- `useChat` from `@ai-sdk/react` returns `{ messages, sendMessage, status }` — not `handleSubmit`, `append`, or `isLoading`
- `messages` are `UIMessage` with `parts: [{type:'text', text:string}]` array — not a `content` string
- `status` is `'submitted' | 'streaming' | 'ready' | 'error'` — use `status === 'submitted' || status === 'streaming'` for loading state
- Transport is `TextStreamChatTransport` from `ai`, instantiated at module level (not inside component)
- `convertToModelMessages()` is async — must be awaited
- Server route returns `result.toTextStreamResponse()` (not `toDataStreamResponse`)

**Critical CSS note**: Never use `@theme inline` in `globals.css` — Tailwind v4 + Turbopack injects a `@import url()` mid-file which breaks CSS parsing. Use CSS custom properties in `:root` instead.

---

## Environment variables

In `.env.local` (gitignored):
```
ANTHROPIC_API_KEY=...
SUPABASE_URL=https://swahlijqvwqppmxmifgo.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...
```

**Shell environment gotcha**: If your shell has `ANTHROPIC_API_KEY=` (empty) or `ANTHROPIC_BASE_URL=https://api.anthropic.com` (missing `/v1`) set, these override `.env.local` and break the API. The chat route uses `createAnthropic({ baseURL: 'https://api.anthropic.com/v1', apiKey: process.env.ANTHROPIC_API_KEY })` to defend against this. Start the dev server with env vars set explicitly if in doubt:
```bash
ANTHROPIC_API_KEY="sk-ant-..." ANTHROPIC_BASE_URL="" npm run dev -- --port 3001
```

---

## What's built

### Pages

| Route | File | Status |
|-------|------|--------|
| `/` | `src/app/page.tsx` | ✅ Landing page — 3-canvas design (indigo hero / white body / teal CTA) |
| `/goals` | `src/app/goals/page.tsx` | ✅ Full conversational onboarding — setup + goals + config in one flow |

### API routes

| Route | File | What it does |
|-------|------|-------------|
| `POST /api/chat` | `src/app/api/chat/route.ts` | Streams Claude responses for the advisor conversation |
| `POST /api/profile` | `src/app/api/profile/route.ts` | Upserts user profile (name + email) into Supabase `profiles` table |
| `POST /api/conversation` | `src/app/api/conversation/route.ts` | Saves full conversation + goal profile JSON to `conversations` table |
| `PATCH /api/conversation` | `src/app/api/conversation/route.ts` | Links a conversation to a profile via `profile_id` |

### Supabase tables

| Table | Columns | Purpose |
|-------|---------|---------|
| `profiles` | `id, name, email, created_at` | User profiles created after goal conversation |
| `conversations` | `id, messages (jsonb), goal_profile (jsonb), profile_id (fk), created_at` | Every conversation saved for product research |

---

## The conversation flow

The advisor has **three phases** in one continuous conversation — no page breaks, no buttons between phases:

**Phase 1 — Setup**: What hardware do they have? (solar, battery, EV, heat pump, other loads). What are they thinking about adding?

**Phase 2 — Goals**: The bill, risk appetite, battery longevity, solar/export preference, EV (if applicable), lifestyle/peak times, calibration close ($500 variable vs $300 certain).

**Phase 3 — Site config**: Technical details — retailer, tariff type, solar size/inverter, battery model, EV model/departure time, controllable loads.

At the end of Phase 2, Claude outputs a `<GOAL_PROFILE>` JSON block (hidden from the UI, parsed client-side). At the end of Phase 3, it outputs a `<SITE_CONFIG>` JSON block.

After the goal profile is detected, the UI prompts the user to save their profile (name + email). This saves to Supabase in the background. The conversation then continues naturally into Phase 3.

---

## Design system

Superhuman-inspired. All design tokens are CSS custom properties in `globals.css`:

| Token | Value | Use |
|-------|-------|-----|
| `--color-primary` | `#1b1938` | Nav, avatars, buttons, dark elements |
| `--color-violet-soft` | `#c9b4fa` | Accent, hero badge |
| `--color-teal-deep` | `#0e3030` | CTA band background, user avatar |
| `--color-teal-mid` | `#155555` | CTA buttons |
| `--color-canvas` | `#ffffff` | Page background |
| `--color-canvas-soft` | `#fafaf8` | Sidebar, advisor message bubbles |
| `--color-ink` | `#292827` | Body text |
| `--color-hairline` | `#e8e4dd` | Borders |
| User message bg | `#0080CB` | User chat bubbles (hardcoded, not a token yet) |

Font: Inter variable (loaded via Google Fonts `<link>` in `layout.tsx` — not via CSS import).

Full details in `DESIGN.md`.

---

## Key files

| File | What it contains |
|------|-----------------|
| `ADVISOR.md` | Sol's voice, tone, conversation structure, goal profile schema, what Sol never does |
| `DESIGN.md` | Full Superhuman-inspired design system reference |
| `src/app/goals/page.tsx` | The entire goals/onboarding UI — read this to understand the current state |
| `src/app/api/chat/route.ts` | System prompt + streaming API route — edit this to change advisor behaviour |

---

## Known issues / to-do

- [ ] **Login**: users who create a profile should be able to log back in with just their email (magic link via Supabase Auth). Profile record already exists — just needs auth wired up.
- [ ] `<SITE_CONFIG>` JSON is output by Claude but not yet parsed/saved client-side (only `<GOAL_PROFILE>` is parsed today)
- [ ] No `/config` page — deliberately removed; config is collected in the conversation
- [ ] No dashboard yet — the product stops at the end of onboarding for now
- [ ] Shell env var `ANTHROPIC_BASE_URL` can break the API if set incorrectly — see note above

---

## Product context

Sol is the product version of the home automation rules in `/Users/simonmonk/homeassistant/`. The HA system is a hand-crafted rule-based approximation; Sol will eventually replace it with a proper MPC solver. The goal elicitation conversation is the differentiator — competitors hardcode their own goals into the optimiser; Sol asks users what they actually want.

The first real user is Simon Monk (simon@sol.io) — the person building it. His site: Powerwall 2 + SolarEdge 5kW + Polestar 4 + Zappi + Daikin AC, Amber Electric EA116 tariff, Glebe Sydney.
