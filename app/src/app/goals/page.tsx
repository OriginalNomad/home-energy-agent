'use client'

import Link from 'next/link'
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
  if (!content.includes('</GOAL_PROFILE>')) return ''
  return content.replace(/<GOAL_PROFILE>[\s\S]*?<\/GOAL_PROFILE>/, '').trim()
}

const TOPIC_LABELS = [
  'Your setup',
  'The bill',
  'Risk appetite',
  'Battery',
  'Solar & export',
  'Your EV',
  'Lifestyle',
  'Calibration',
]

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

  const assetIcons: Record<string, string> = {
    solar: '☀️', battery: '🔋', ev: '🚗', heat_pump: '🌡️',
  }
  const activeAssets = profile.assets
    ? Object.entries(profile.assets)
        .filter(([k, v]) => k !== 'other_loads' && v === true)
        .map(([k]) => k)
    : []

  return (
    <div className="rounded-2xl p-6" style={{ background: 'var(--color-canvas-soft)', border: '1px solid var(--color-hairline)' }}>
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
        <div className="flex items-center justify-between">
          <span className="text-sm" style={{ color: 'var(--color-ink-mute)', fontVariationSettings: '"wght" 460' }}>Demand protection</span>
          <PriorityBadge value={profile.demand_penalty} variants={demandVariants} />
        </div>
        <div className="flex items-center justify-between">
          <span className="text-sm" style={{ color: 'var(--color-ink-mute)', fontVariationSettings: '"wght" 460' }}>Risk appetite</span>
          <PriorityBadge value={profile.risk_aversion} variants={riskVariants} />
        </div>
        <div className="flex items-center justify-between">
          <span className="text-sm" style={{ color: 'var(--color-ink-mute)', fontVariationSettings: '"wght" 460' }}>Battery longevity</span>
          <PriorityBadge value={profile.cycle_cost_sensitivity} variants={levelVariants} />
        </div>
        <div className="flex items-center justify-between">
          <span className="text-sm" style={{ color: 'var(--color-ink-mute)', fontVariationSettings: '"wght" 460' }}>EV priority</span>
          <PriorityBadge value={profile.ev_priority} variants={levelVariants} />
        </div>
        <div className="flex items-center justify-between">
          <span className="text-sm" style={{ color: 'var(--color-ink-mute)', fontVariationSettings: '"wght" 460' }}>Feed-in preference</span>
          <PriorityBadge value={profile.feedin_preference} variants={feedinVariants} />
        </div>
        <div className="flex items-center justify-between">
          <span className="text-sm" style={{ color: 'var(--color-ink-mute)', fontVariationSettings: '"wght" 460' }}>Load shedding OK</span>
          <span className={`text-sm font-medium ${profile.load_shedding_consent ? 'text-emerald-600' : 'text-slate-400'}`}>
            {profile.load_shedding_consent ? 'Yes' : 'No'}
          </span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-sm" style={{ color: 'var(--color-ink-mute)', fontVariationSettings: '"wght" 460' }}>Backup reserve</span>
          <span className={`text-sm font-medium ${profile.backup_reserve_enabled ? 'text-emerald-600' : 'text-slate-400'}`}>
            {profile.backup_reserve_enabled ? 'Enabled' : 'Minimised'}
          </span>
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
      if (!res.ok) {
        setError(json.error ?? 'Something went wrong — please try again.')
        setLoading(false)
        return
      }

      // Link the conversation to this profile in the background
      if (conversationId && json.profile?.id) {
        fetch('/api/conversation', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ conversationId, profileId: json.profile.id }),
        }).catch(() => { /* non-critical */ })
      }

      onSaved(name.trim(), email.trim())
    } catch {
      setError('Could not reach the server. Check your connection.')
      setLoading(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-3 mt-1">
      <input
        value={name}
        onChange={e => setName(e.target.value)}
        placeholder="Your name"
        autoFocus
        className="w-full px-4 py-3 outline-none"
        style={{ fontSize: '16px', background: '#ffffff', border: '1px solid var(--color-hairline)', borderRadius: 'var(--radius-md)', color: 'var(--color-ink)', fontVariationSettings: '"wght" 460' }}
      />
      <input
        value={email}
        onChange={e => setEmail(e.target.value)}
        placeholder="Email address"
        type="email"
        className="w-full px-4 py-3 outline-none"
        style={{ fontSize: '16px', background: '#ffffff', border: '1px solid var(--color-hairline)', borderRadius: 'var(--radius-md)', color: 'var(--color-ink)', fontVariationSettings: '"wght" 460' }}
      />
      {error && (
        <p className="text-xs px-1" style={{ color: '#dc2626' }}>{error}</p>
      )}
      <button
        type="submit"
        disabled={!name.trim() || !email.trim() || loading}
        className="w-full py-3 transition-opacity hover:opacity-90 disabled:opacity-30 disabled:cursor-not-allowed"
        style={{ fontSize: '15px', background: 'var(--color-primary)', color: '#ffffff', borderRadius: 'var(--radius-md)', fontVariationSettings: '"wght" 700' }}
      >
        {loading ? 'Saving…' : 'Save profile →'}
      </button>
    </form>
  )
}

const transport = new TextStreamChatTransport({ api: '/api/chat' })

export default function GoalsPage() {
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


  // Derive goal profile from messages
  const goalProfile = useMemo(() => {
    for (const msg of messages) {
      if (msg.role === 'assistant') {
        const text = getMessageText(msg)
        const profile = parseGoalProfile(text)
        if (profile) return profile
      }
    }
    return null
  }, [messages])

  // When profile first appears: auto-save conversation + trigger the "save?" prompt
  const profilePromptShown = useRef(false)
  useEffect(() => {
    if (goalProfile && !profilePromptShown.current) {
      profilePromptShown.current = true

      // Save conversation in the background (fire and forget, but capture the id)
      const conversationMessages = messages.filter(
        (m) => !(m.role === 'user' && getMessageText(m) === '__start__')
      )
      fetch('/api/conversation', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages: conversationMessages, goalProfile }),
      })
        .then((r) => r.json())
        .then((json) => { if (json.conversationId) setConversationId(json.conversationId) })
        .catch(() => { /* non-critical, don't surface to user */ })

      const visibleCount = messages.filter(
        (m) => !(m.role === 'user' && getMessageText(m) === '__start__')
      ).length
      setProfilePromptAfterIndex(visibleCount - 1)
      setTimeout(() => setPostProfileStep('ask'), 900)
    }
  }, [goalProfile, messages])

  // Rough topic progress estimation
  const topicProgress = useMemo(() => {
    const count = messages.filter((m) => m.role === 'user').length
    return Math.min(count, TOPIC_LABELS.length - 1)
  }, [messages])

  // Kick off: show typing indicator immediately, then fetch first message after a pause
  // NOTE: hasStarted is set inside the timer callback, not before it, so that
  // React Strict Mode's double-invocation (which cancels the first timer) doesn't
  // prevent the second run from firing.
  useEffect(() => {
    if (hasStarted.current) return
    const timer = setTimeout(() => {
      hasStarted.current = true
      setShowIntroTyping(false)
      sendMessage({ text: '__start__' })
    }, 1800)
    return () => clearTimeout(timer)
  }, [sendMessage])

  // Scroll to bottom on new messages or new steps
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, postProfileStep])

  // Filter out the invisible start trigger for display
  const visibleMessages = messages.filter((m) => {
    if (m.role !== 'user') return true
    return getMessageText(m) !== '__start__'
  })

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const text = inputValue.trim()
    if (!text || isLoading) return
    sendMessage({ text })
    setInputValue('')
  }

  return (
    <div className="min-h-screen flex flex-col" style={{ background: 'var(--color-canvas)' }}>
      {/* Nav */}
      <nav className="px-6 py-4 flex items-center justify-between flex-shrink-0" style={{ background: 'var(--color-primary)', borderBottom: '1px solid var(--color-primary)' }}>
        <Link href="/" className="text-white text-lg tracking-tight" style={{ fontVariationSettings: '"wght" 540' }}>sol</Link>
        <span className="text-sm text-white/70" style={{ fontVariationSettings: '"wght" 460' }}>Goal assessment</span>
      </nav>

      <div className="flex flex-1 overflow-hidden">
        {/* Sidebar */}
        <aside className="w-64 flex-shrink-0 p-6 hidden lg:flex flex-col gap-6 overflow-y-auto" style={{ background: 'var(--color-canvas-soft)', borderRight: '1px solid var(--color-hairline)' }}>
          <div>
            <p className="text-xs uppercase tracking-widest mb-4" style={{ color: 'var(--color-ink-faint)', fontVariationSettings: '"wght" 540' }}>Topics</p>
            <div className="space-y-2">
              {TOPIC_LABELS.map((label, i) => (
                <div key={label} className="flex items-center gap-3">
                  <div
                    className="w-5 h-5 rounded-full flex items-center justify-center flex-shrink-0"
                    style={{
                      background: i < topicProgress ? 'var(--color-primary)' : i === topicProgress ? 'rgba(27,25,56,0.1)' : 'var(--color-canvas-soft)',
                      border: i === topicProgress ? '1px solid var(--color-primary)' : i > topicProgress ? '1px solid var(--color-hairline)' : 'none',
                    }}
                  >
                    {i < topicProgress && (
                      <svg className="w-3 h-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                      </svg>
                    )}
                    {i === topicProgress && (
                      <div className="w-2 h-2 rounded-full animate-pulse" style={{ background: 'var(--color-primary)' }} />
                    )}
                  </div>
                  <span className="text-sm" style={{ color: i <= topicProgress ? 'var(--color-ink)' : 'var(--color-ink-faint)', fontVariationSettings: '"wght" 460' }}>
                    {label}
                  </span>
                </div>
              ))}
            </div>
          </div>

          {goalProfile && <GoalCard profile={goalProfile} />}

        </aside>

        {/* Chat area */}
        <div className="flex-1 flex flex-col min-h-0">
          <div className="flex-1 overflow-y-auto px-6 py-8 space-y-6">

            {/* Typing indicator — shown during intro pause AND while first message loads */}
            {visibleMessages.length === 0 && (showIntroTyping || isLoading) && (
              <div className="flex gap-4">
                <AdvisorAvatar />
                <TypingIndicator />
              </div>
            )}

            {/* Messages + inline post-profile UI at the right position */}
            {visibleMessages.map((message, index) => {
              const isAdvisor = message.role === 'assistant'
              const rawText = getMessageText(message)
              const displayContent = isAdvisor ? stripGoalProfile(rawText) : rawText

              return (
                <div key={message.id}>
                  {displayContent && (
                    <div className={`flex gap-4 ${isAdvisor ? '' : 'flex-row-reverse'}`}>
                      {isAdvisor ? <AdvisorAvatar /> : <UserAvatar />}
                      <div className={`max-w-xl flex flex-col ${isAdvisor ? 'items-start' : 'items-end'}`}>
                        <div
                          className="px-5 py-4 rounded-2xl leading-relaxed whitespace-pre-wrap"
                          style={isAdvisor ? {
                            fontSize: '24px',
                            background: 'var(--color-canvas-soft)',
                            border: '1px solid var(--color-hairline)',
                            color: 'var(--color-ink)',
                            borderTopLeftRadius: '4px',
                            fontVariationSettings: '"wght" 460',
                          } : {
                            fontSize: '24px',
                            background: '#0080CB',
                            color: '#ffffff',
                            borderTopRightRadius: '4px',
                            fontVariationSettings: '"wght" 460',
                          }}
                        >
                          {displayContent}
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Post-profile UI — anchored to the message that triggered it */}
                  {index === profilePromptAfterIndex && postProfileStep === 'ask' && (
                    <div className="flex gap-4 mt-6">
                      <AdvisorAvatar />
                      <div className="max-w-xl flex flex-col items-start gap-3">
                        <div className="px-5 py-4 leading-relaxed" style={{ fontSize: '24px', background: 'var(--color-canvas-soft)', border: '1px solid var(--color-hairline)', borderRadius: 'var(--radius-lg)', borderTopLeftRadius: 'var(--radius-xs)', color: 'var(--color-ink)', fontVariationSettings: '"wght" 460' }}>
                          Would you like to save your profile? That way we can pick up where you left off
                          on any device, and adjust your goals over time as your situation changes.
                        </div>
                        <div className="flex gap-3">
                          <button
                            onClick={() => setPostProfileStep('profile-setup')}
                            className="px-5 py-2.5 transition-opacity hover:opacity-90"
                            style={{ fontSize: '15px', background: 'var(--color-primary)', color: '#ffffff', borderRadius: 'var(--radius-md)', fontVariationSettings: '"wght" 700' }}
                          >
                            Yes
                          </button>
                          <button
                            onClick={() => {
                              setPostProfileStep(null)
                              sendMessage({ text: "No thanks, let's continue." })
                            }}
                            className="px-5 py-2.5 transition-opacity hover:opacity-90"
                            style={{ fontSize: '15px', background: 'var(--color-canvas-soft)', color: 'var(--color-ink)', border: '1px solid var(--color-hairline)', borderRadius: 'var(--radius-md)', fontVariationSettings: '"wght" 540' }}
                          >
                            No
                          </button>
                        </div>
                      </div>
                    </div>
                  )}

                  {index === profilePromptAfterIndex && postProfileStep === 'profile-setup' && (
                    <div className="mt-6 space-y-6">
                      <div className="flex gap-4 flex-row-reverse">
                        <UserAvatar />
                        <div className="max-w-xl flex flex-col items-end">
                          <div className="px-5 py-4" style={{ fontSize: '24px', background: '#0080CB', color: '#fff', borderRadius: 'var(--radius-lg)', borderTopRightRadius: 'var(--radius-xs)', fontVariationSettings: '"wght" 460' }}>
                            Yes — set up profile
                          </div>
                        </div>
                      </div>
                      <div className="flex gap-4">
                        <AdvisorAvatar />
                        <div className="max-w-sm flex flex-col items-start gap-3">
                          <div className="px-5 py-4 leading-relaxed" style={{ fontSize: '24px', background: 'var(--color-canvas-soft)', border: '1px solid var(--color-hairline)', borderRadius: 'var(--radius-lg)', borderTopLeftRadius: 'var(--radius-xs)', color: 'var(--color-ink)', fontVariationSettings: '"wght" 460' }}>
                            Just your name and email — that's all we need for now.
                          </div>
                          <div className="w-full px-1">
                            <ProfileSetupForm
                              goalProfile={goalProfile!}
                              conversationId={conversationId}
                              onSaved={(name) => {
                                setSavedName(name)
                                setPostProfileStep('profile-saved')
                              }}
                            />
                          </div>
                        </div>
                      </div>
                    </div>
                  )}

                  {index === profilePromptAfterIndex && postProfileStep === 'profile-saved' && (
                    <div className="flex gap-4 mt-6">
                      <AdvisorAvatar />
                      <div className="max-w-xl">
                        <div className="px-5 py-4 leading-relaxed" style={{ fontSize: '24px', background: 'var(--color-canvas-soft)', border: '1px solid var(--color-hairline)', borderRadius: 'var(--radius-lg)', borderTopLeftRadius: 'var(--radius-xs)', color: 'var(--color-ink)', fontVariationSettings: '"wght" 460' }}>
                          Done. Your profile is saved, {savedName.split(' ')[0]} — you can pick this conversation up on any device using your email.
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              )
            })}

            {/* Typing indicator while streaming */}
            {isLoading && visibleMessages.length > 0 && (
              <div className="flex gap-4">
                <AdvisorAvatar />
                <TypingIndicator />
              </div>
            )}

            {/* Mobile: goal profile card */}
            {goalProfile && (
              <div className="lg:hidden mt-4">
                <GoalCard profile={goalProfile} />
              </div>
            )}

            <div ref={messagesEndRef} />
          </div>

          {/* Input */}
          <div className="px-6 py-4 flex-shrink-0" style={{ borderTop: '1px solid var(--color-hairline)' }}>
            <form onSubmit={handleSubmit}>
              <div className="flex flex-col gap-2">
                <textarea
                  value={inputValue}
                  onChange={(e) => setInputValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      handleSubmit(e as unknown as React.FormEvent)
                    }
                  }}
                  placeholder={isLoading ? 'Sol is thinking…' : 'Type your answer… (Enter to send, Shift+Enter for new line)'}
                  disabled={isLoading}
                  rows={4}
                  className="w-full px-4 py-3 outline-none disabled:opacity-50 transition-all resize-none"
                  style={{
                    fontSize: '20px',
                    background: '#ffffff',
                    border: '1px solid var(--color-hairline)',
                    borderRadius: 'var(--radius-md)',
                    color: 'var(--color-ink)',
                    fontVariationSettings: '"wght" 460',
                    lineHeight: '1.5',
                  }}
                  autoComplete="off"
                />
                <div className="flex justify-end">
                  <button
                    type="submit"
                    disabled={isLoading || !inputValue.trim()}
                    className="px-6 py-2.5 flex-shrink-0 transition-opacity hover:opacity-90 disabled:opacity-30 disabled:cursor-not-allowed"
                    style={{
                      fontSize: '15px',
                      background: 'var(--color-primary)',
                      color: '#ffffff',
                      borderRadius: 'var(--radius-md)',
                      fontVariationSettings: '"wght" 700',
                    }}
                  >
                    Send
                  </button>
                </div>
              </div>
            </form>
          </div>
        </div>
      </div>
    </div>
  )
}

// Small shared components

function AdvisorAvatar() {
  return (
    <div
      className="w-14 h-14 rounded-full flex items-center justify-center flex-shrink-0 mt-1"
      style={{ background: 'var(--color-primary)', color: '#ffffff' }}
    >
      <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor">
        <path d="M13 2L4.5 13.5H11L10 22L19.5 10.5H13L13 2Z" />
      </svg>
    </div>
  )
}

function UserAvatar() {
  return (
    <div
      className="w-14 h-14 rounded-full flex items-center justify-center flex-shrink-0 mt-1 text-xs"
      style={{ background: 'var(--color-teal-deep)', color: '#fff', fontVariationSettings: '"wght" 540' }}
    >
      Me
    </div>
  )
}

function TypingIndicator() {
  return (
    <div className="px-5 py-4 max-w-xl" style={{ background: 'var(--color-canvas-soft)', border: '1px solid var(--color-hairline)', borderRadius: 'var(--radius-lg)', borderTopLeftRadius: 'var(--radius-xs)' }}>
      <div className="flex gap-1 items-center h-5">
        <div className="w-1.5 h-1.5 rounded-full animate-bounce" style={{ background: 'var(--color-ink-faint)', animationDelay: '0ms' }} />
        <div className="w-1.5 h-1.5 rounded-full animate-bounce" style={{ background: 'var(--color-ink-faint)', animationDelay: '150ms' }} />
        <div className="w-1.5 h-1.5 rounded-full animate-bounce" style={{ background: 'var(--color-ink-faint)', animationDelay: '300ms' }} />
      </div>
    </div>
  )
}
