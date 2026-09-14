# Saturday Line — repo scaffold

## What's here

```
index.html                        the app (move your cfb_predictor_app.html here, rename to index.html)
data/injuries.json                weekly injury report, overwritten by the scheduled workflow
scripts/fetch_injuries.py         the pipeline: CFBD matchups -> ESPN headlines -> LLM parse -> JSON
scripts/requirements.txt          Python deps for the pipeline
.github/workflows/injury-report.yml   the actual "Thu/Fri/Sat job" — GitHub Actions cron
```

## One-time setup

1. **Create the repo and push this scaffold**, with your existing app file
   renamed to `index.html` at the repo root.

2. **Add two repository secrets** (Settings → Secrets and variables →
   Actions → New repository secret):
   - `OPENAI_API_KEY` — your OpenAI key, for the structured-output parse
   - `CFBD_API_KEY` — your CollegeFootballData key, for figuring out the
     current week and matchups

3. **Connect the repo to Netlify** (Netlify → Add new site → Import from
   Git) instead of using Drop. This also gives you push-to-deploy and
   rollback, not just the injury pipeline.

4. **Test the workflow manually before trusting the schedule.** Go to the
   Actions tab → "Weekly injury report" → "Run workflow" — this uses the
   same `workflow_dispatch` trigger in the YAML, so you don't have to
   wait for Thursday to see if it works. Check the Actions log and the
   resulting `data/injuries.json` diff.

## Wiring the app to read the report

The app's Overview tab currently calls ESPN live, client-side, per game.
Add one more fetch alongside it:

```js
async function getWeeklyInjuryReport() {
  try {
    const resp = await fetch('/data/injuries.json', { cache: 'no-store' });
    if (!resp.ok) return null;
    return await resp.json();
  } catch (e) {
    return null;
  }
}
```

Call this once per page load (not per-game — it's one file covering the
whole week), and in the Overview tab's `renderEspnResult`-style section,
look up `report.teams[teamName].injuries` alongside the existing live
spot-check. Show both — the live check is more current, the weekly
report may have caught something from Thursday that's since scrolled
off ESPN's live feed.

Worth deciding explicitly: does a `CONFIDENCE_THRESHOLD`-passing entry
here get **shown only**, or does it **pre-check the manual QB-out
toggle** we already built? I'd start with "shown only" — let the human
glance at it and flip the toggle themselves — since auto-applying an
LLM's parse of a headline directly into the point spread is a bigger
trust jump than surfacing it for a person to confirm.

## Honest expectations

This will often find few or zero real injuries in a given week — ESPN's
college football coverage is thin, which is the actual bottleneck (see
the pipeline script's docstring). The value here isn't "comprehensive
injury coverage" — it's "whatever's out there, extracted automatically
on a schedule, instead of manually re-checking three times a week."
