'use client'

import { useChat } from '@ai-sdk/react'
import { TextStreamChatTransport, type UIMessage } from 'ai'
import { useEffect, useRef, useState, useMemo } from 'react'

type GoalProfile = {
  assets?: {
    solar?: boolean
    battery?: boolean
    ev?: boolean
    heat_pump?: boolean
    other_loads?: string | null
  }
  planned_additions?: string | null
  demand_penalty: string
  risk_aversion: string
  cycle_cost_sensitivity: string
  ev_priority: string
  feedin_preference: string
  load_shedding_consent: boolean
  backup_reserve_enabled: boolean
  notes: string
}

type PostProfileStep = 'ask' | 'profile-setup' | 'profile-saved' | null

function getMessageText(message: UIMessage): string {
  return message.parts
    .filter((p) => p.type === 'text')
    .map((p) => (p.type === 'text' ? p.text : ''))
    .join('')
}

function parseGoalProfile(content: string): GoalProfile | null {
  const match = content.match(/<GOAL_PROFILE>([\s\S]*?)<\/GOAL_PROFILE>/)
  if (!match) return null
  try {
    return JSON.parse(match[1].trim())
  } catch {
    return null
  }
}

function stripGoalProfile(content: string): string {
  if (!content.includes('<GOAL_PROFILE>')) return content
  // Still streaming — hide entire message until closing tag arrives
  if (!content.includes('</GOAL_PROFILE>')) return ''
  return content.replace(/<GOAL_PROFILE>[\s\S]*?<\/GOAL_PROFILE>/, '').trim()
}

function PriorityBadge({ value, variants }: { value: string; variants: Record<string, string> }) {
  const cls = variants[value] ?? 'bg-slate-100 text-slate-600 border border-slate-200'
  return (
    <span className={`inline-block px-2.5 py-0.5 rounded-full text-xs font-semibold ${cls}`}>
      {value}
    </span>
  )
}

function GoalCard({ profile }: { profile: GoalProfile }) {
  const demandVariants: Record<string, string> = {
    critical: 'bg-red-50 text-red-700 border border-red-200',
    high: 'bg-orange-50 text-orange-700 border border-orange-200',
    medium: 'bg-yellow-50 text-yellow-700 border border-yellow-200',
    low: 'bg-slate-100 text-slate-600 border border-slate-200',
    none: 'bg-slate-100 text-slate-500 border border-slate-200',
  }
  const riskVariants: Record<string, string> = {
    conservative: 'bg-blue-50 text-blue-700 border border-blue-200',
    balanced: 'bg-slate-100 text-slate-600 border border-slate-200',
    aggressive: 'bg-purple-50 text-purple-700 border border-purple-200',
  }
  const levelVariants: Record<string, string> = {
    high: 'bg-emerald-50 text-emerald-700 border border-emerald-200',
    medium: 'bg-slate-100 text-slate-600 border border-slate-200',
    low: 'bg-slate-100 text-slate-500 border border-slate-200',
    critical: 'bg-red-50 text-red-700 border border-red-200',
    none: 'bg-slate-100 text-slate-500 border border-slate-200',
  }
  const feedinVariants: Record<string, string> = {
    maximise: 'bg-emerald-50 text-emerald-700 border border-emerald-200',
    moderate: 'bg-slate-100 text-slate-600 border border-slate-200',
    absorb_only: 'bg-slate-100 text-slate-500 border border-slate-200',
  }
  const assetIcons: Record<string, string> = { solar: '☀️', battery: '🔋', ev: '🚗', heat_pump: '🌡️' }
  const activeAssets = profile.assets
    ? Object.entries(profile.assets).filter(([k, v]) => k !== 'other_loads' && v === true).map(([k]) => k)
    : []

  return (
    <div className="rounded-2xl p-6 my-2" style={{ background: 'var(--color-canvas-soft)', border: '1px solid var(--color-hairline)' }}>
      <div className="flex items-center gap-2 mb-5">
        <div className="w-2 h-2 rounded-full" style={{ background: 'var(--color-primary)' }} />
        <h3 className="text-sm" style={{ color: 'var(--color-ink)', fontVariationSettings: '"wght" 540' }}>Goal profile derived</h3>
      </div>
      {activeAssets.length > 0 && (
        <div className="flex flex-wrap gap-1.5 mb-5">
          {activeAssets.map((a) => (
            <span key={a} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs" style={{ background: 'var(--color-canvas)', border: '1px solid var(--color-hairline)', color: 'var(--color-ink-mute)', fontVariationSettings: '"wght" 460' }}>
              {assetIcons[a]} {a.replace('_', ' ')}
            </span>
          ))}
          {profile.assets?.other_loads && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs" style={{ background: 'var(--color-canvas)', border: '1px solid var(--color-hairline)', color: 'var(--color-ink-mute)', fontVariationSettings: '"wght" 460' }}>
              ⚡ {profile.assets.other_loads}
            </span>
          )}
        </div>
      )}
      <div className="space-y-4">
        {[
          { label: 'Demand protection', value: profile.demand_penalty, variants: demandVariants },
          { label: 'Risk appetite', value: profile.risk_aversion, variants: riskVariants },
          { label: 'Battery longevity', value: profile.cycle_cost_sensitivity, variants: levelVariants },
          { label: 'EV priority', value: profile.ev_priority, variants: levelVariants },
          { label: 'Feed-in preference', value: profile.feedin_preference, variants: feedinVariants },
        ].map(({ label, value, variants }) => (
          <div key={label} className="flex items-center justify-between">
            <span className="text-sm" style={{ color: 'var(--color-ink-mute)', fontVariationSettings: '"wght" 460' }}>{label}</span>
            <PriorityBadge value={value} variants={variants} />
          </div>
        ))}
        <div className="flex items-center justify-between">
          <span className="text-sm" style={{ color: 'var(--color-ink-mute)', fontVariationSettings: '"wght" 460' }}>Load shedding OK</span>
          <span className={`text-sm font-medium ${profile.load_shedding_consent ? 'text-emerald-600' : 'text-slate-400'}`}>{profile.load_shedding_consent ? 'Yes' : 'No'}</span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-sm" style={{ color: 'var(--color-ink-mute)', fontVariationSettings: '"wght" 460' }}>Backup reserve</span>
          <span className={`text-sm font-medium ${profile.backup_reserve_enabled ? 'text-emerald-600' : 'text-slate-400'}`}>{profile.backup_reserve_enabled ? 'Enabled' : 'Minimised'}</span>
        </div>
        {profile.planned_additions && (
          <div className="pt-3" style={{ borderTop: '1px solid var(--color-hairline)' }}>
            <p className="text-xs mb-1" style={{ color: 'var(--color-ink-faint)', fontVariationSettings: '"wght" 540' }}>Planning to add</p>
            <p className="text-sm leading-relaxed" style={{ color: 'var(--color-ink-mute)', fontVariationSettings: '"wght" 460' }}>{profile.planned_additions}</p>
          </div>
        )}
        {profile.notes && (
          <div className="pt-3" style={{ borderTop: '1px solid var(--color-hairline)' }}>
            <p className="text-xs mb-1" style={{ color: 'var(--color-ink-faint)', fontVariationSettings: '"wght" 540' }}>Notes</p>
            <p className="text-sm leading-relaxed" style={{ color: 'var(--color-ink-mute)', fontVariationSettings: '"wght" 460' }}>{profile.notes}</p>
          </div>
        )}
      </div>
    </div>
  )
}

function ProfileSetupForm({
  goalProfile,
  conversationId,
  onSaved,
}: {
  goalProfile: GoalProfile
  conversationId: string | null
  onSaved: (name: string, email: string) => void
}) {
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!name.trim() || !email.trim()) return
    setLoading(true)
    setError('')
    try {
      const res = await fetch('/api/profile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim(), email: email.trim(), goalProfile }),
      })
      const json = await res.json()
      if (!res.ok) { setError(json.error ?? 'Something went wrong.'); setLoading(false); return }
      if (conversationId && json.profile?.id) {
        fetch('/api/conversation', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ conversationId, profileId: json.profile.id }),
        }).catch(() => {})
      }
      onSaved(name.trim(), email.trim())
    } catch {
      setError('Could not reach the server.')
      setLoading(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-3 mt-1">
      <input value={name} onChange={e => setName(e.target.value)} placeholder="Your name" autoFocus
        className="w-full px-4 py-3 outline-none"
        style={{ fontSize: '16px', background: '#ffffff', border: '1px solid var(--color-hairline)', borderRadius: 'var(--radius-md)', color: 'var(--color-ink)', fontVariationSettings: '"wght" 460' }} />
      <input value={email} onChange={e => setEmail(e.target.value)} placeholder="Email address" type="email"
        className="w-full px-4 py-3 outline-none"
        style={{ fontSize: '16px', background: '#ffffff', border: '1px solid var(--color-hairline)', borderRadius: 'var(--radius-md)', color: 'var(--color-ink)', fontVariationSettings: '"wght" 460' }} />
      {error && <p className="text-xs px-1" style={{ color: '#dc2626' }}>{error}</p>}
      <button type="submit" disabled={!name.trim() || !email.trim() || loading}
        className="w-full py-3 transition-opacity hover:opacity-90 disabled:opacity-30 disabled:cursor-not-allowed"
        style={{ fontSize: '15px', background: 'var(--color-primary)', color: '#ffffff', borderRadius: 'var(--radius-md)', fontVariationSettings: '"wght" 700' }}>
        {loading ? 'Saving…' : 'Save profile →'}
      </button>
    </form>
  )
}

function AdvisorAvatar() {
  return (
    <div className="w-9 h-9 rounded-full flex items-center justify-center flex-shrink-0 mt-1" style={{ background: 'var(--color-primary)', color: '#ffffff' }}>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
        <path d="M13 2L4.5 13.5H11L10 22L19.5 10.5H13L13 2Z" />
      </svg>
    </div>
  )
}

function UserAvatar() {
  return (
    <div className="w-9 h-9 rounded-full flex items-center justify-center flex-shrink-0 mt-1 text-xs" style={{ background: 'var(--color-teal-deep)', color: '#fff', fontVariationSettings: '"wght" 540' }}>
      Me
    </div>
  )
}

function TypingIndicator() {
  return (
    <div className="px-4 py-3 max-w-xl" style={{ background: 'var(--color-canvas-soft)', border: '1px solid var(--color-hairline)', borderRadius: 'var(--radius-lg)', borderTopLeftRadius: 'var(--radius-xs)' }}>
      <div className="flex gap-1 items-center h-4">
        {[0, 150, 300].map((delay) => (
          <div key={delay} className="w-1.5 h-1.5 rounded-full animate-bounce" style={{ background: 'var(--color-ink-faint)', animationDelay: `${delay}ms` }} />
        ))}
      </div>
    </div>
  )
}

const transport = new TextStreamChatTransport({ api: '/api/chat' })

export default function ChatSection() {
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const hasStarted = useRef(false)
  const [inputValue, setInputValue] = useState('')
  const [postProfileStep, setPostProfileStep] = useState<PostProfileStep>(null)
  const [profilePromptAfterIndex, setProfilePromptAfterIndex] = useState<number>(-1)
  const [savedName, setSavedName] = useState('')
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [showIntroTyping, setShowIntroTyping] = useState(true)

  const { messages, sendMessage, status } = useChat({ transport })
  const isLoading = status === 'submitted' || status === 'streaming'

  const goalProfile = useMemo(() => {
    for (const msg of messages) {
      if (msg.role === 'assistant') {
        const profile = parseGoalProfile(getMessageText(msg))
        if (profile) return profile
      }
    }
    return null
  }, [messages])

  const profilePromptShown = useRef(false)
  useEffect(() => {
    if (goalProfile && !profilePromptShown.current) {
      profilePromptShown.current = true
      const conversationMessages = messages.filter((m) => !(m.role === 'user' && getMessageText(m) === '__start__'))
      fetch('/api/conversation', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: conversationMessages, goalProfile }),
      })
        .then((r) => r.json())
        .then((json) => { if (json.conversationId) setConversationId(json.conversationId) })
        .catch(() => {})
      const visibleCount = messages.filter((m) => !(m.role === 'user' && getMessageText(m) === '__start__')).length
      setProfilePromptAfterIndex(visibleCount - 1)
      setTimeout(() => setPostProfileStep('ask'), 900)
    }
  }, [goalProfile, messages])

  // After profile is saved, auto-start Phase 3
  useEffect(() => {
    if (postProfileStep !== 'profile-saved') return
    const timer = setTimeout(() => sendMessage({ text: '__continue__' }), 2500)
    return () => clearTimeout(timer)
  }, [postProfileStep, sendMessage])

  useEffect(() => {
    if (hasStarted.current) return
    const timer = setTimeout(() => {
      hasStarted.current = true
      setShowIntroTyping(false)
      sendMessage({ text: '__start__' })
    }, 1800)
    return () => clearTimeout(timer)
  }, [sendMessage])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, postProfileStep])

  const visibleMessages = messages.filter((m) => {
    if (m.role !== 'user') return true
    const t = getMessageText(m)
    return t !== '__start__' && t !== '__continue__'
  })

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const text = inputValue.trim()
    if (!text || isLoading) return
    sendMessage({ text })
    setInputValue('')
  }

  return (
    <div className="flex flex-col rounded-2xl overflow-hidden" style={{ border: '1px solid var(--color-hairline)', minHeight: '560px', height: '60vh', maxHeight: '720px' }}>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-6 py-6 space-y-5" style={{ background: 'var(--color-canvas)' }}>

        {visibleMessages.length === 0 && (showIntroTyping || isLoading) && (
          <div className="flex gap-3">
            <AdvisorAvatar />
            <TypingIndicator />
          </div>
        )}

        {visibleMessages.map((message, index) => {
          const isAdvisor = message.role === 'assistant'
          const rawText = getMessageText(message)
          const displayContent = isAdvisor ? stripGoalProfile(rawText) : rawText

          return (
            <div key={message.id}>
              {displayContent && (
                <div className={`flex gap-3 ${isAdvisor ? '' : 'flex-row-reverse'}`}>
                  {isAdvisor ? <AdvisorAvatar /> : <UserAvatar />}
                  <div className={`max-w-2xl flex flex-col ${isAdvisor ? 'items-start' : 'items-end'}`}>
                    <div
                      className="px-4 py-3 rounded-2xl leading-relaxed whitespace-pre-wrap"
                      style={isAdvisor ? {
                        fontSize: '18px',
                        background: 'var(--color-canvas-soft)',
                        border: '1px solid var(--color-hairline)',
                        color: 'var(--color-ink)',
                        borderTopLeftRadius: '4px',
                        fontVariationSettings: '"wght" 460',
                        lineHeight: '1.6',
                      } : {
                        fontSize: '18px',
                        background: '#0080CB',
                        color: '#ffffff',
                        borderTopRightRadius: '4px',
                        fontVariationSettings: '"wght" 460',
                        lineHeight: '1.6',
                      }}
                    >
                      {displayContent}
                    </div>
                  </div>
                </div>
              )}

              {index === profilePromptAfterIndex && postProfileStep === 'ask' && (
                <div className="flex gap-3 mt-5">
                  <AdvisorAvatar />
                  <div className="max-w-2xl flex flex-col items-start gap-3">
                    <div className="px-4 py-3 leading-relaxed" style={{ fontSize: '18px', background: 'var(--color-canvas-soft)', border: '1px solid var(--color-hairline)', borderRadius: 'var(--radius-lg)', borderTopLeftRadius: 'var(--radius-xs)', color: 'var(--color-ink)', fontVariationSettings: '"wght" 460', lineHeight: '1.6' }}>
                      Would you like to save your profile? That way we can pick up where you left off on any device, and adjust your goals over time as your situation changes.
                    </div>
                    <div className="flex gap-3">
                      <button onClick={() => setPostProfileStep('profile-setup')}
                        className="px-5 py-2.5 transition-opacity hover:opacity-90"
                        style={{ fontSize: '15px', background: 'var(--color-primary)', color: '#ffffff', borderRadius: 'var(--radius-md)', fontVariationSettings: '"wght" 700' }}>
                        Yes
                      </button>
                      <button
                        onClick={() => { setPostProfileStep(null); sendMessage({ text: "No thanks, let's continue." }) }}
                        className="px-5 py-2.5 transition-opacity hover:opacity-90"
                        style={{ fontSize: '15px', background: 'var(--color-canvas-soft)', color: 'var(--color-ink)', border: '1px solid var(--color-hairline)', borderRadius: 'var(--radius-md)', fontVariationSettings: '"wght" 540' }}>
                        No
                      </button>
                    </div>
                  </div>
                </div>
              )}

              {index === profilePromptAfterIndex && postProfileStep === 'profile-setup' && (
                <div className="mt-5 space-y-5">
                  <div className="flex gap-3 flex-row-reverse">
                    <UserAvatar />
                    <div className="max-w-2xl flex flex-col items-end">
                      <div className="px-4 py-3" style={{ fontSize: '18px', background: '#0080CB', color: '#fff', borderRadius: 'var(--radius-lg)', borderTopRightRadius: 'var(--radius-xs)', fontVariationSettings: '"wght" 460' }}>
                        Yes — set up profile
                      </div>
                    </div>
                  </div>
                  <div className="flex gap-3">
                    <AdvisorAvatar />
                    <div className="max-w-sm flex flex-col items-start gap-3">
                      <div className="px-4 py-3 leading-relaxed" style={{ fontSize: '18px', background: 'var(--color-canvas-soft)', border: '1px solid var(--color-hairline)', borderRadius: 'var(--radius-lg)', borderTopLeftRadius: 'var(--radius-xs)', color: 'var(--color-ink)', fontVariationSettings: '"wght" 460' }}>
                        Just your name and email — that's all we need for now.
                      </div>
                      <div className="w-full px-1">
                        <ProfileSetupForm goalProfile={goalProfile!} conversationId={conversationId}
                          onSaved={(name) => { setSavedName(name); setPostProfileStep('profile-saved') }} />
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {index === profilePromptAfterIndex && postProfileStep === 'profile-saved' && (
                <div className="flex gap-3 mt-5">
                  <AdvisorAvatar />
                  <div className="max-w-2xl">
                    <div className="px-4 py-3 leading-relaxed" style={{ fontSize: '18px', background: 'var(--color-canvas-soft)', border: '1px solid var(--color-hairline)', borderRadius: 'var(--radius-lg)', borderTopLeftRadius: 'var(--radius-xs)', color: 'var(--color-ink)', fontVariationSettings: '"wght" 460' }}>
                      Done. Your profile is saved, {savedName.split(' ')[0]} — you can pick this conversation up on any device using your email.
                    </div>
                  </div>
                </div>
              )}
            </div>
          )
        })}

        {isLoading && visibleMessages.length > 0 && (
          <div className="flex gap-3">
            <AdvisorAvatar />
            <TypingIndicator />
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="px-5 py-4 flex-shrink-0" style={{ borderTop: '1px solid var(--color-hairline)', background: 'var(--color-canvas-soft)' }}>
        <form onSubmit={handleSubmit}>
          <div className="flex gap-3 items-end">
            <textarea
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(e as unknown as React.FormEvent) }
              }}
              placeholder={isLoading ? 'Sol is thinking…' : 'Type your answer…'}
              disabled={isLoading}
              rows={2}
              className="flex-1 px-4 py-3 outline-none disabled:opacity-50 resize-none"
              style={{ fontSize: '16px', background: '#ffffff', border: '1px solid var(--color-hairline)', borderRadius: 'var(--radius-md)', color: 'var(--color-ink)', fontVariationSettings: '"wght" 460', lineHeight: '1.5' }}
              autoComplete="off"
            />
            <button
              type="submit"
              disabled={isLoading || !inputValue.trim()}
              className="px-5 py-3 flex-shrink-0 transition-opacity hover:opacity-90 disabled:opacity-30 disabled:cursor-not-allowed"
              style={{ fontSize: '15px', background: 'var(--color-primary)', color: '#ffffff', borderRadius: 'var(--radius-md)', fontVariationSettings: '"wght" 700' }}
            >
              Send
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
