import ChatSection from '../components/ChatSection'
import ParticleNetwork from '../components/ParticleNetwork'

export default function Home() {
  return (
    <main className="min-h-screen font-sans antialiased">

      {/* ── Hero ─────────────────────────────────────────────────────────── */}
      <div className="relative overflow-hidden" style={{ background: 'var(--color-primary)' }}>
        <ParticleNetwork />
        <div className="relative grid grid-cols-12 gap-4 px-6 pt-20 pb-28">

          {/* Hero content — 8 cols centred */}
          <div className="col-span-12 md:col-span-8 md:col-start-3 text-center">

            <div className="mb-10 text-sm" style={{ fontVariationSettings: '"wght" 540', letterSpacing: '0.08em' }}>
              <span className="text-white">sol</span>
              <span className="ml-1.5" style={{ color: 'var(--color-violet-soft)' }}>(BETA)</span>
            </div>

            <h1
              className="text-white mb-6 leading-none tracking-tight"
              style={{
                fontSize: 'clamp(40px, 6vw, 64px)',
                fontVariationSettings: '"wght" 540',
                letterSpacing: '-1px',
              }}
            >
              Every home electrification journey is different. What's yours?
            </h1>

            <p
              className="mx-auto mb-12 max-w-xl"
              style={{
                fontSize: '18px',
                lineHeight: '1.5',
                color: 'var(--color-on-dark-mute)',
                fontVariationSettings: '"wght" 460',
              }}
            >
              Most battery systems optimise for one goal. Sol asks what <em>you</em> care
              about — then builds the policy that achieves it.
            </p>

            <a
              href="#chat"
              className="animate-pulse inline-flex items-center rounded-full px-8 py-3 hover:opacity-90"
              style={{
                background: 'rgba(201,180,250,0.12)',
                border: '1px solid rgba(201,180,250,0.2)',
                color: 'var(--color-violet-soft)',
                fontSize: '16px',
                fontVariationSettings: '"wght" 700',
                letterSpacing: '0.08em',
              }}
            >
              START
            </a>

            <p className="mt-4 text-sm" style={{ color: 'var(--color-on-dark-faint)', fontVariationSettings: '"wght" 460' }}>
              It's free
            </p>

          </div>
        </div>
      </div>

      {/* ── Chat ─────────────────────────────────────────────────────────── */}
      <div id="chat" style={{ background: 'var(--color-canvas)' }}>
        <div className="grid grid-cols-12 gap-4 px-6 py-16">

          {/* Chat — 10 cols centred */}
          <div className="col-span-12 md:col-span-10 md:col-start-2 lg:col-span-6 lg:col-start-4">
            <ChatSection />
          </div>

        </div>
      </div>

      {/* ── Footer ───────────────────────────────────────────────────────── */}
      <footer style={{ background: 'var(--color-canvas)', borderTop: '1px solid var(--color-hairline)' }}>
        <div className="grid grid-cols-12 gap-4 px-6 py-8">
          <div className="col-span-12 flex items-center justify-between">
            <span style={{ color: 'var(--color-ink-mute)', fontSize: '14px', fontVariationSettings: '"wght" 460' }}>
              sol.io · 2026
            </span>
            <span style={{ color: 'var(--color-ink-faint)', fontSize: '14px', fontVariationSettings: '"wght" 460' }}>
              Energy optimisation
            </span>
          </div>
        </div>
      </footer>

    </main>
  )
}
