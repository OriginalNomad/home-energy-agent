import { createClient } from '@supabase/supabase-js'

const supabase = createClient(
  process.env.SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!
)

export async function POST(req: Request) {
  try {
    const { name, email, goalProfile } = await req.json()

    if (!name || !email || !goalProfile) {
      return Response.json({ error: 'Missing required fields' }, { status: 400 })
    }

    // Upsert by email — update goal profile if email already exists
    const { data, error } = await supabase
      .from('profiles')
      .upsert(
        { name, email, goal_profile: goalProfile },
        { onConflict: 'email' }
      )
      .select('id, name, email, created_at')
      .single()

    if (error) {
      console.error('Supabase error:', error)
      return Response.json({ error: error.message }, { status: 500 })
    }

    return Response.json({ success: true, profile: data })
  } catch (err) {
    console.error('Profile save error:', err)
    return Response.json({ error: 'Internal server error' }, { status: 500 })
  }
}
