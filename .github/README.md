# 100% Streaks

A daily page of Kalshi player-prop and goal-line streaks for the NBA, NHL, NFL, EPL, La Liga, Ligue 1 and MLS, plus a Games view with each team's last-10 scoring ranges next to Kalshi's game total.

`streaks.py` pulls open markets from Kalshi's public API and game logs from ESPN, then writes a single HTML page. A GitHub Action rebuilds it at about 10:00 and 16:00 CET and publishes it on GitHub Pages.

```bash
python3 streaks.py             # build streaks.html locally (Python 3.9+, no extra libraries)
python3 streaks.py --selftest  # check the streak math
```

To rebuild the live page now, open the Actions tab, pick **Streaks page** and click **Run workflow**.

Streaks show past results against Kalshi lines. They are not predictions or betting advice.
