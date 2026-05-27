Read the following files in order:
1. CLAUDE.md
2. CONTEXT.md
3. app/CONTEXT.md
4. todo.md
5. The last 50 lines of energy_log.md

Then give me a 3-part summary:
- **System status** — agent, automations, anything to watch today
- **App status** — Sol prototype, where it's up to
- **Today's priorities** — from todo.md and anything flagged in the log

## Standing instructions for the session

After giving the morning summary, apply these rules for the rest of the session without needing to be asked:

- **Update `energy_rules.md` immediately** whenever a rule changes, is clarified, or a new one is added — don't wait to be prompted
- **Update `energy_log.md`** with a dated entry at the end of any meaningful work block (not just end of day)
- **Update `CONTEXT.md`** if automation count, agent behaviour, or system architecture changes
- When editing `agent/energy_agent.py` system prompt, mirror the change in `energy_rules.md` in the same response
