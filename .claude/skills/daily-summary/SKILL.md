---
name: daily-summary
description: End-of-day performance summary for your broker-agency outbound. Covers dials, connects, submissions, quotes, binds and appointments, plus a data-driven read on how the day went. Use when the user says "daily summary", "end of day", "how did I do today", "wrap up the day", or runs /daily-summary [YYYY-MM-DD].
---

# Daily summary

Produces a report of one day's outbound performance from the pipeline tracker in this folder.
The argument is optional: a date as `YYYY-MM-DD`, or words like "yesterday". The default is today.

## Steps

1. **Pull the latest calls.** If the tracker is running, sync Trellus first so the last calls are counted:
   `curl -s -X POST http://localhost:8765/api/sync`
   If that fails because the tracker isn't running, carry on and mention it in one line. Don't start the server.

2. **Get the numbers.** Run `python3 daily_report.py [YYYY-MM-DD]` from this folder. It only reads `pipeline.db`.
   It prints markdown with: headline numbers against the 7-day average and the last working day, conversion
   rates, Trellus dispositions, an hour-by-hour connect rate, positive conversations, touches on existing
   (inherited) accounts, stage moves, and pipeline health.

3. **Write the summary** for the user, in this order:

   **Scoreboard**: one compact table with Dials, Connects (and connect %), Submissions, Quotes, Binds,
   Appointments, plus Interested and Info sent. Show each against the 7-day average with ↑ / ↓.
   - Submissions = `Submissions`, Quotes = `Quotes`, Binds and Appointments = `Binds / Appointed`.
     The tracker has a single "Appointed" stage (bound & appointed), so binds and appointments are the same
     count. Say so in one short line. Don't invent a split.

   **How the day went**: 4 to 6 bullets of real analysis. Each one should give a number and what it means. Look for:
   - Connect rate vs. the 7-day average, and dials per connect.
   - Where calls died: the share of no-answer vs. gatekeeper vs. wrong number. Is the list quality or the gatekeeper the problem?
   - Gatekeeper pass-through rate (decision makers reached out of human answers).
   - Best and worst hours by connect %. Only call out an hour with 10 or more dials, and flag small samples.
   - Conversion from connects to Interested, and pipeline movement (moves into Quoted or Appointed).
   - Talk time and average connect length. Longer conversations usually mean better pitches.
   - If the report notes backfill (more leads than connects), say the funnel rates are inflated today and
     lean on the dial and connect metrics instead.

   **Wins**: the positive conversations and any cards moved forward, named by agency.

   **Existing accounts**: from the "Existing accounts" section. This is account-management work on the book
   you inherited, so it isn't in the funnel numbers, but it still counts as real work. List the
   conversations that reached a person: who, what came out of it (intro as point of contact, personal
   number or WhatsApp shared, more work promised, quote status, objections such as "not rated"). Sum up the
   voicemails and gatekeepers in one line. Name any risk to a quote.

   **Tomorrow**: follow-ups due, stale cards, Needs review items, and one concrete focus based on the data
   (for example, "dial more in the 18:00 slot, it converted at 2× your average").

   Keep it tight and skimmable. Use plain language and no filler. Don't paste the raw script output.

4. **Atlas (only if the user asks to check Atlas):** Atlas is strictly read-only. Only view and extract,
   and never click save, submit, approve, send or delete. Use Claude in Chrome to read today's submissions, quotes
   and binds, then compare them with the tracker counts and list any mismatches.

## Notes
- Connect = Connected, Interested, Not interested, Callback or DQ (gatekeepers aren't connects). This rule lives in `trellus_sync.CONNECT`.
- Funnel counts follow the Stats tab rules: each agency counts once per stage, and later stages credit earlier ones.
- The 7-day average only counts days with dials, so weekends don't drag it down.
- Inherited accounts (cards with `inherited_stage` set, shown with an "Inherited" tag on the board) only count
  in the funnel once they move past the stage they were taken over in. If submissions, quotes or binds look
  too high, check for cards created directly in a later stage that aren't flagged as inherited.
