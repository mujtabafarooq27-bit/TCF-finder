# TCF exam page watcher

Checks the Alliance Française TCF pages for Vancouver, Calgary, and
Edmonton on a schedule, and emails you when a real slot looks open.

- **Vancouver & Edmonton**: parses the spots-left table on each page.
  Flags any row that has spots left and isn't "Sold Out" / "Opens in ...".
- **Calgary**: reads the registration-process page, follows every
  "Registrations" link it finds (since a month can show that button
  while the page behind it is actually sold out), and flags any link
  that's new or that flipped from sold-out to not-sold-out.
- As a safety net, every page's full text is also hashed. If the page
  changes in some way the structured parser doesn't recognize, you
  still get an email telling you to go check manually rather than
  silence.

It does not register or pay for anything automatically — it just
watches and alerts, so you can go book it yourself.

**Heads up:** these three sites' robots.txt says they'd rather not be
crawled by bots. This script is a low-frequency personal checker (by
default every 30 min, well under what would look like scraping) rather
than bulk data collection, but it's still worth knowing that it runs
against their stated preference. If you'd rather not, the alternative
is checking manually or using a paid "watch this page" service instead.

## Setup (15 minutes, no local install needed)

1. **Create a GitHub repo** (free account works). Go to github.com → New
   repository → name it something like `tcf-watcher` → Create.

2. **Upload these files** to the repo: `check_tcf.py`, `requirements.txt`,
   `.github/workflows/check-tcf.yml`, and this `README.md`. Easiest way:
   on the repo page, click "Add file" → "Upload files" and drag them in
   (keep the `.github/workflows/` folder structure intact).

3. **URLs are already filled in** for all three centres at the top of
   `check_tcf.py` (`VANCOUVER_URL`, `CALGARY_URL`, `EDMONTON_URL`). No
   editing needed unless a page moves.

4. **Set up an email sender.** The simplest free option is a Gmail
   account with an "app password":
   - Turn on 2-Step Verification on the Gmail account (Google Account →
     Security).
   - Go to Google Account → Security → App passwords, create one for
     "Mail", and copy the 16-character password.

5. **Add GitHub Secrets** (repo → Settings → Secrets and variables →
   Actions → New repository secret):
   - `EMAIL_ADDRESS`: the Gmail address you'll send *from*
   - `EMAIL_PASSWORD`: the app password from step 4
   - `EMAIL_TO`: the address you want alerts sent *to* (can be the same
     address, or a different one)

6. **First run.** Go to the repo's "Actions" tab → "Check TCF exam
   pages" → "Run workflow" to trigger it manually once. This establishes
   the baseline snapshot (no email on this first run, since there's
   nothing to compare against yet) and commits `state.json` back to the
   repo.

7. From then on it runs automatically every 30 minutes and emails you
   only when something on one of the three pages changes.

## About "Opens in ..." timing

Spots on these exams reportedly fill fast, so knowing the *exact* moment
registration opens matters. I could not fetch these three pages myself
to confirm their exact wording (their robots.txt blocks automated
fetching, including my own tools) — everything about their table format
is inferred from a sister Alliance Française site using the same
booking platform, so treat it as a strong guess, not a fact, until you
verify it against the live page yourself.

What the script does about it:
- Every run, it prints every row it found (spots, status, and any
  detected "Register within ..." window) to the run's log — check the
  Actions tab any time, not just when an email fires.
- For each row, if there's a link to that session's own booking page, it
  follows it and looks for a "Register within &lt;start&gt; - &lt;end&gt;" phrase,
  which on a related site appeared as plain static text (an exact date
  and time), not something drawn in by JavaScript after the page loads.
- If the summary table's own "Opens in ..." cell turns out to be filled
  in by JavaScript (i.e. the script only ever sees the words "Opens in"
  with no number after it), the per-session page is your best bet for
  the exact time — check the first run's log for `Registration window:`
  lines.

**Please verify this against the real site before relying on it** for
something as time-sensitive as a seat that fills in minutes. If the
first run's log doesn't show clean registration windows, tell me what
it does print (or the actual page's HTML) and I'll adjust the parsing.

## Notes and limitations

- **Check the first run's log output** (Actions tab → the run → the
  "Run checker" step) against what you see in your own browser. It
  prints how many table rows / registration links it found for each
  centre. If that number is 0 when you can clearly see rows on the
  site, the table is likely being filled in by JavaScript after the
  page loads, which a plain script can't see — in that case tell me
  and I can switch that page to a headless-browser fetch instead.
- The Vancouver/Edmonton table parser looks for any cell that's a bare
  number (spots left) and flags it as open unless another cell in the
  same row says "Sold Out" or "Opens in ...". If the real page phrases
  status differently, you'll still get the fallback "page changed,
  go check" email rather than silence — just less precise.
- Calgary: the script only finds registration links whose visible text
  is "Registrations" / "Register" / "Register now". If the site's
  wording differs, adjust the match list near the top of `check_calgary()`.
- Keep the check interval reasonable (30 min is a sensible default) —
  no need to hit these pages every minute.
