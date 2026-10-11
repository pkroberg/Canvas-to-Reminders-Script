# Canvas → Reminders

Sync your Canvas assignments, quizzes and exams into the macOS **Reminders** app, with a real
Reminders tag for each course. It keeps them up to date by syncing hourly in the background.

The script reads Canvas's personal **calendar feed** (`.ics`), so it works even when your school has
turned off student API tokens. It was built for Colorado State University and works with any Canvas school.

## Features
- **Imports every assignment with a due date.** That includes quizzes, exams and graded discussions, plus due dates set specifically for your section.
- **One list, one tag per course.** Everything goes into one Reminders list, with a tag for each course, e.g. `#SystemsSecurity`, `#C++`.
- **No duplicates.** Each reminder is matched to its Canvas assignment ID.
- **Due-date changes update the existing reminder.** Checked-off reminders stay checked off.
- **Reminders you delete stay deleted.**
- **Assignments removed from Canvas** get a note in the reminder ("⚠️ Removed from Canvas calendar on DATE") instead of being deleted.
- **Hourly sync with launchd.** If the Mac was asleep, the missed run happens on wake.

## Requirements
- macOS 14 Sonoma or later (tested on macOS 26)
- Python 3.11+ (`tomllib`). Homebrew's `python3` at `/opt/homebrew/bin/python3` is what `install.sh` uses.
- The **Shortcuts** app, to set tags. See [the Shortcut section](#2-build-the-canvas-add-reminder-shortcut).
- A Canvas account with the Calendar feed available. Most schools have it.

## Setup

### 1. Configure
```sh
git clone <this repo> && cd <repo>
cp config.example.toml config.toml
```
Open `config.toml` and set:
- **`feed_url`:** in Canvas, go to **Calendar**, then click **Calendar Feed** (bottom right) and copy the link.
- **`[course_tags]`:** the tag for each course. Keys are the department and number from the course code:
  `[2026FA-CS-356-001]` becomes `CS356`.
  ```toml
  [course_tags]
  CS356   = "SystemsSecurity"
  CS314   = "SoftwareEngineering"
  CS320   = "Algorithms"
  CT301   = "C++"
  PHIL201 = "Ethics"
  STAT301 = "Statistics"
  ```
  A course you don't map uses its key as the tag, e.g. `#CS314`.

> ⚠️ **Keep your feed URL private.** It contains a token that gives anyone read access to your
> Canvas calendar. `config.toml` is in `.gitignore`. Never commit it, and never paste the URL into issues.
> If it leaks, reset it in Canvas: Account → Settings → *Reset Calendar Feed URL*, if your school allows it.

### 2. Build the "Canvas Add Reminder" Shortcut
Apple doesn't let scripts set Reminders **tags**: EventKit and AppleScript have no API for them,
and `#Tag` typed into a title by a script stays plain text. The Shortcuts action
**Add New Reminder** can set tags, so the script creates each reminder through a Shortcut. It then
sets the due date and alert itself. Later edits keep the tag.

The Tags field won't accept a variable, so the Shortcut has **one If branch per tag**, with each tag typed in by hand.

1. Open **Shortcuts**, then **File → New Shortcut**, and name it exactly **`Canvas Add Reminder`**.
2. Open the details panel (ⓘ) and turn on **Use as Quick Action**. This lets the Shortcut receive input
   from the command line. Make sure **Files** is among the accepted input types.
3. Add the actions in this order:
   ```
   Get Dictionary from [Shortcut Input]
   Get [Value] for [tag] in [Dictionary]
   Text: [Dictionary Value]
   If [Text] is [SystemsSecurity]
       Add New Reminder   Title: Dictionary → title
                          List:  Canvas Script
                          Notes: Dictionary → notes
                          Tags:  SystemsSecurity        ← typed in
   Otherwise
       If [Text] is [SoftwareEngineering]
           Add New Reminder … (Tags: SoftwareEngineering)
       Otherwise
           … one branch per course …
               Otherwise
                   Add New Reminder … (no tag)          ← catch-all for unmapped courses
   ```
   - **Step by step:**
     1. Directly under **Get Dictionary Value** (key `tag`), add a **Text** action and put the
        *Dictionary Value* variable inside it.
     2. In each **If**, click the first token and choose the **Text** variable from step 1.
     3. Set the condition to **is**, then type the class tag, e.g. `SystemsSecurity`.
   - To fill **Title** and **Notes**, insert the *Dictionary* variable, click it, choose
     *Get Value for Key*, and type `title` or `notes`.
   - **The conditions must match the tags in `config.toml` exactly**, including capitalization. With
     the example above, those are `SystemsSecurity`, `SoftwareEngineering`, `Algorithms`, `C++`, `Ethics` and `Statistics`.
   - **Include the catch-all.** The innermost *Otherwise* must still add a reminder, without a tag. If
     nothing is created, the script waits, logs `not visible yet; retrying next run`, and tries again
     every hour.
4. Test it from Terminal:
   ```sh
   echo '{"title":"Shortcut test","tag":"SystemsSecurity","notes":"test"}' > /tmp/t.json
   shortcuts run "Canvas Add Reminder" --input-path /tmp/t.json
   ```
   A reminder named *Shortcut test* should appear in **Canvas Script** with a real, tappable
   `#SystemsSecurity` tag. Delete it afterward.

### 3. Install
```sh
./install.sh
```
Try `./run.sh --dry-run` first to preview what will be created.

This creates `.venv`, builds a small `CanvasReminders.app` wrapper, installs the launchd job, and runs
the first sync. **Approve the Reminders permission prompt** when it appears. The first sync takes a
few minutes, because every new reminder goes through the Shortcut.

## Usage
```sh
./run.sh --dry-run          # preview changes, write nothing
./run.sh                    # sync now
./run.sh -v                 # verbose
.venv/bin/python canvas_to_reminders.py --parse-only   # print the parsed feed (no Reminders access needed)
tail -f ~/Library/Logs/canvas-reminders.log            # watch syncs
./install.sh --uninstall    # stop the hourly job (your reminders are kept)
```

**New semester:** add the new courses to `[course_tags]`, then add a matching If branch to the Shortcut.

## Configuration (`config.toml`)
| Key | Default | Meaning |
|---|---|---|
| `feed_url` | (required) | Your Canvas calendar feed URL |
| `list_name` | `Canvas Script` | Reminders list to sync into. It's created if missing. |
| `shortcut_name` | `Canvas Add Reminder` | Name of the Shortcut above |
| `use_shortcut` | `true` | If `false`, or if the Shortcut isn't installed, reminders are created directly with `#Tag` written in the notes |
| `alert_minutes_before` | `0` | Alert offset for timed due dates. All-day items use Reminders' own all-day notification time. |
| `import_past_days` | `1` | On the first sync, skip assignments that were due more than this many days ago |
| `removed_grace_days` | `7` | Only add the "removed" note if the item was due within this many days |
| `[course_tags]` | | Course key → tag |

## How it works
- **What gets imported:** only `event-assignment-*` entries in the feed, which covers quizzes and exams.
  Section due-date overrides are mapped back to the real assignment ID. Plain calendar events, like
  holidays and demo days, are skipped.
- **Times:** due times are converted to your local time zone. Canvas exports 11:59 PM deadlines as date-only,
  so those become all-day reminders.
- **Clean reminders:** each one has only a title, a tag and a due date. The script leaves the notes and URL
  empty, and never touches anything you type into the notes yourself.
- **State:** `~/Library/Application Support/CanvasReminders/state.json` maps each Canvas ID to a reminder.
  The Shortcut writes `canvas-assignment-id: N` into the notes so the script can find the new reminder;
  the script erases it a second later. If the state file is lost, the next run relinks existing reminders
  by **title + due date** instead of creating duplicates. One exception: if an assignment was renamed or
  re-dated in Canvas at the same time the state was lost, that single assignment gets a duplicate.
- **Why the `.app` wrapper:** macOS only gives Reminders access to an app with a usage description. A bare
  `python` binary can't show the permission prompt. `install.sh` generates an ad-hoc-signed
  bundle, and `run.sh` and launchd both run the script through it.
- **Why EventKit:** AppleScript's date setters for Reminders hang or fail on recent macOS.

## Troubleshooting
- **"Reminders access denied"**: go to System Settings → Privacy & Security → Reminders and enable *CanvasReminders*.
- **Moved the folder?** Re-run `./install.sh`. The app wrapper and the launchd job store absolute paths.
- **Upgraded Python or Homebrew?** Re-run `./install.sh`, and re-approve the permission if asked.
- **Nothing happening?** Check `~/Library/Logs/canvas-reminders.log`, and
  `launchctl print gui/$UID/com.pkroberg.canvas-reminders`.

## Privacy
Everything runs locally. The script only contacts your school's Canvas server to download the feed.
Your feed URL and sync state never leave your Mac.

## Files
| File | Purpose |
|---|---|
| `canvas_to_reminders.py` | The sync script |
| `config.example.toml` | Config template. Copy it to `config.toml`, which is gitignored. |
| `install.sh` | Creates the venv, builds the app wrapper, and installs or uninstalls the launchd job |
| `run.sh` | Runs a sync by hand through the app wrapper |
| `com.pkroberg.canvas-reminders.plist` | launchd job template. `install.sh` fills in the paths. |
| `requirements.txt` | `icalendar`, `pyobjc-framework-EventKit` |

