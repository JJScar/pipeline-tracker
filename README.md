# Pipeline Tracker

A local dashboard for tracking broker-agency outbound, from first call to appointment.

**Start it:** double-click `start.command`, or run `python3 server.py`. Then open http://localhost:8765.
**Stop it:** press Ctrl+C in that terminal window.
**Your data:** `pipeline.db` in this folder (SQLite). Back it up by copying the file. Use **Export CSV** to get a spreadsheet.

## Using it with the team
The tracker runs on the company platform, and you sign in there with your work email. Then:
1. Click **⚙ Settings** (top right). It opens by itself the first time.
2. **Trellus:** paste your Trellus API key (ask your team lead if you don't have it). Click **Test**, then **Save**.
3. **Work email:** follow the 3 steps shown to create a Google app password, paste it, click **Test**, then **Save**.

That's it. Your calls and emails start logging on your agency cards within a few minutes. Only you can see your pipeline and your keys. Managers see a **Team** tab with everyone's numbers, and can open anyone's pipeline read-only.

Setting the tracker up on the platform is covered in **[DEPLOY.md](DEPLOY.md)**.

## Running it on your own Mac (optional)
You only need Python 3, which comes with macOS.
```
git clone https://github.com/JJScar/pipeline-tracker
cd pipeline-tracker
make start
```
Then open http://localhost:8765 and add your keys under **⚙ Settings**. Your tracker starts empty, and a fresh `pipeline.db` is created on first run. A `.env` file (see `.env.example`) still works too.

## Privacy: what's shared and what isn't
- **Shared in GitHub:** only the app's code and this README.
- **Never shared:** your clients, contacts, notes, calls, emails, to-dos, stats and keys. On your Mac they live in `pipeline.db` (and `.env` if you use one). On the team server each person has their own database that only they can open.
- `.gitignore` blocks `*.db`, `.env`, `data/`, `backups/`, logs and CSV exports, so `git add .` can't pick them up by accident. Don't remove those lines, and never paste client data or keys into code or README files.
- On your Mac the tracker only listens on your own computer (127.0.0.1), so nobody else on the network can open it.

## Stages
1. **Follow Up**: the outbound call went well and needs a follow-up
2. **Info Sent**: you emailed the info and asked for a submission or ACORD, no reply yet
3. **Submission In**: they replied on the thread with a submission and you need to quote it
4. **Quoted**: you sent the quote back on the thread
5. **Appointed**: they bound and got appointed, so the account is active
6. **Lost**: not a fit or went dark (hidden unless "Show lost" is checked)

## Performance tracking
- **Dials and Connects boxes** sit in the header. Type today's number and press Enter. Enter in Dials jumps to Connects. `D` / `C` jump straight to a box.
- Everything else is counted automatically from the pipeline: **Interested** (a new lead was added), **Info sent**, **Submissions**, **Quoted**, **Appointed**, **Lost**, **Touches** (calls, voicemails, emails and meetings you logged on an agency) and **Bound premium**.
- The **Stats** tab shows any date range and compares it with the period before. It includes the full funnel with conversion % at each step, a chart for each day (or each week on long ranges), average days between stages, and a table by day.
- You can fix the dials or connects for any day in the table, or add a day you forgot.
- An agency counts once per stage. Moving a card back and forth doesn't inflate the numbers. Adding one straight into a later stage also credits the earlier stages.
- **Deleting** an agency removes it from your stats. Use **Mark lost** instead.

## Should I call? (their local time)
When you open an agency, the top of its panel shows **their local time** and whether they're open: green **Open**, amber (lunch 12–1, closing within 30 min, or a holiday when many agencies close or have fewer staff), grey (closed, not open yet, or a major holiday). Hover the badge for details.
- **Time zone** comes from the phone's area code, then from State. You can set it yourself in the agency's drawer.
- **Opening hours** default to Mon–Fri 9–5. Type their real hours in the drawer, e.g. `8:30-5`, `Mon-Thu 8-5, Fri 8-12`, `M-F 9-5; Sat 9-12`.
- **Holidays**: New Year's, Memorial Day, July 4, Labor Day, Thanksgiving and Christmas count as closed. MLK, Presidents', Juneteenth, Columbus and Veterans Day, the day after Thanksgiving, and Christmas Eve and New Year's Eve show amber.

## Asked for, we don't have
Each agency's panel has an **Asked for, we don't have** box. Use it for anything they wanted that we can't offer yet, like a commercial auto market. Search covers it, so typing "commercial auto" in the search box lists every agency that asked for it.

## To-dos per agency
Open an agency and click **☐ To-dos** at the top right, next to Close (or press `T`). The list pops up in the middle of the screen. Type what needs doing and press Enter. `Esc` closes the pop-up and leaves the agency open. The number on the button turns red when something is overdue, due today or blocked.
- **Due** date (quick buttons: Today, Tmrw, +3d, +1w), and **Follow up with** (who you're chasing).
- Click **+ Blocker** to add what's in the way and **who can unblock it**. Blocked to-dos get a red edge.
- Tick the box when it's done. It moves to "Show done" and leaves a "✓ Done" note on the timeline. That note doesn't count as a touch. Untick to reopen.
- The **To-dos** box on the Follow-ups tab lists every open to-do across all agencies, soonest due first. You can tick them off right there, and clicking one opens that agency's to-do pop-up. Overdue and due-today to-dos count toward the red number on the tab.
- Board cards show how many to-dos are open, and turn red when one is overdue or blocked. Search covers to-do text too, so searching "loss runs" or a person's name finds them.

## Arrange things your way
- **Board:** drag a card up or down a column to set your own order. Dragging it to another column still moves it to that stage, and it lands where you drop it.
- **Follow-ups tab:** drag agencies up or down inside a box. To move a whole box, drag it by its title.
- Your order is saved in `pipeline.db`. New cards show at the top of their list until you place them.

## Shortcuts
`N` new lead · `/` search · `D` / `C` jump to the dials / connects box · `1`–`4` switch to Follow-ups / Board / List / Stats · `T` to-dos (in an open agency) · `,` settings · `5` team (managers) · `Esc` close · `⌘↵` save or log

Stale flags: Follow Up after 7 days, Info Sent after 4, Submission after 2, Quoted after 7.
To change them, edit `STALE` in `static/index.html`.

## Trellus sync
Runs inside the tracker every 5 minutes (only reads from Trellus, never writes to it). Click **Trellus ✓** in the header to sync now.
- **Dials / Connects** fill in from your Trellus calls. A Connect means you reached the decision maker: Connected, Interested, Not interested, Callback or DQ. On days with Trellus calls, the sync replaces anything you typed by hand.
- **Calls are logged on agency cards** when they're a conversation or a left voicemail and clearly match an agency (by phone, agency name and contact name).
- **Needs review** (top of Follow-ups) lists unclear matches and "Interested" calls for agencies you haven't added yet. Each one can be logged on an agency, added as a new lead, or dismissed.
- Click **Transcript** on a Needs review item to read the call (You / Them) and play the recording. Click a line's time to play from there. It's loaded from Trellus when you click, and recordings past Trellus's retention period won't play.
- Conversations with no match aren't shown but are kept, and get logged automatically once you add that agency.
- Connect it under **⚙ Settings** (or `TRELLUS_API_KEY` / `TRELLUS_USER_EMAIL` in `.env` on your Mac). Code is in `trellus_sync.py`. A backup from before the sync was added is in `backups/`.

## Email sync
Runs inside the tracker every 5 minutes. It only reads your work mailbox: nothing is sent, moved, deleted or marked read. Click **Email ✓** in the header to sync now.
- **Every email to or from a tracked agency is logged on its card**, with the subject, attachments and the first lines you or they wrote. Each one counts as a touch. An email matches an agency by the exact address in its Email field, or by the company domain from its email or website. Gmail/Yahoo-type addresses and shared captive domains (farmersagent.com, statefarm.com…) only match the exact address.
- **Cards move forward on their own:**
  - **Follow Up → Info Sent** when you email them. If the card has no follow-up date, it gets one 4 business days later, same as moving it by hand.
  - **Follow Up / Info Sent → Submission In** when they email you a file (a PDF, doc or spreadsheet; images like signature logos don't count).
  - **Submission In → Quoted** when you email them a file, or write something like "quote attached" or "here's the quote". Files you send everyone don't count: anything named like an appetite guide, brochure, flyer, W-9, license or agency agreement.
- Cards never move backwards, and never to Appointed or Lost. Only emails newer than a card's last stage change can move it, so if you drag a card back by hand it stays there. The timeline says "moved … automatically" when an email did it.
- An email that fits two agencies (same domain) is skipped. Emails from agencies that aren't in the tracker yet are skipped too, and aren't picked up later. Log those by hand.
- The first sync goes back 14 days.
- Connect it under **⚙ Settings** (or `GOOGLE_APP_PASSWORD` and `EMAIL_ADDRESS` in `.env` on your Mac). Code is in `email_sync.py`. A backup from before the sync was added is in `backups/`.

## Daily summary
Run `/daily-summary` in Claude Code from this folder (or `/daily-summary 2026-11-03` for another day). It syncs Trellus, runs `daily_report.py` (read-only on `pipeline.db`), and writes up the day: dials, connects, submissions, quotes, binds/appointments, plus an analysis of where calls died, your best hours, and what to focus on tomorrow. To see just the raw numbers, run `python3 daily_report.py`.
