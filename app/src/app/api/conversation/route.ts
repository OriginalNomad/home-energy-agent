import { createClient } from '@supabase/supabase-js'

const supabase = createClient(
  process.env.SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!
)

// POST — save a new conversation when goal profile is derived
export async function POST(req: Request) {
  try {
    const { messages, goalProfile } = await req.json()

    if (!messages || !goalProfile) {
      return Response.json({ error: 'Missing required fields' }, { status: 400 })
    }

    const { data, error } = await supabase
      .from('conversations')
      .insert({ messages, goal_profile: goalProfile })
      .select('id')
      .single()

    if (error) {
      console.error('Supabase error:', error)
      return Response.json({ error: error.message }, { status: 500 })
    }

    return Response.json({ success: true, conversationId: data.id })
  } catch (err) {
    console.error('Conversation save error:', err)
    return Response.json({ error: 'Internal server error' }, { status: 500 })
  }
}

// PATCH — link a conversation to a profile after the user saves their profile
export async function PATCH(req: Request) {
  try {
    const { conversationId, profileId } = await req.json()

    if (!conversationId || !profileId) {
      return Response.json({ error: 'Missing required fields' }, { status: 400 })
    }

    const { error } = await supabase
      .from('conversations')
      .update({ profile_id: profileId })
      .eq('id', conversationId)

    if (error) {
      console.error('Supabase error:', error)
      return Response.json({ error: error.message }, { status: 500 })
    }

    return Response.json({ success: true })
  } catch (err) {
    console.error('Conversation link error:', err)
    return Response.json({ error: 'Internal server error' }, { status: 500 })
  }
}
