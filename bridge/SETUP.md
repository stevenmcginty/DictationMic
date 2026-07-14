# DictationMic calendar bridge — setup

This is a tiny Google Apps Script that runs inside **your own Google account**.
Once a minute it checks your Google Calendar for new or changed events and
writes them straight into DictationMic, so a calendar event shows up as a note
on all your devices **even when your laptop is switched off**.

It only ever *reads* your calendar — it never changes or deletes events.

> **Important:** you must set this up while signed in to the **same Google
> account that owns the DictationMic sync project** (the account that set up
> phone sync). If you use a different Google account it won't have permission to
> write your notes.

You don't need to install anything or understand any code. Just copy, paste,
and run.

---

## Set it up (about 5 minutes, once)

1. Go to **https://script.google.com** and sign in with your Google account.

2. Click **New project** (top-left). A code editor opens with a file called
   `Code.gs` containing a sample `myFunction`.

3. **Replace the code.** Select everything in the editor (Ctrl+A) and delete it,
   then open the `Code.gs` file from this folder, copy all of it, and paste it in.

4. **Show the settings file.** Click the gear icon **Project Settings** on the
   left, and tick **"Show 'appsscript.json' manifest file in editor"**.

5. Go back to the **Editor** (the `< >` icon on the left). You'll now see an
   `appsscript.json` file in the file list. Click it, select everything in it
   (Ctrl+A, Delete), then copy all of the `appsscript.json` from this folder and
   paste it in.

6. Press **Ctrl+S** to save. (Give the project a name if it asks — anything is
   fine, e.g. "DictationMic bridge".)

7. At the top of the editor there's a **function dropdown** (it probably says
   `poll` or `setup`). Choose **`setup`**, then click **Run**.

8. **Approve the permissions.** The first time, Google asks you to allow the
   script:
   - Click **Review permissions** and pick your Google account.
   - You'll see a scary-looking screen that says **"Google hasn't verified this
     app."** This is normal for a script you wrote yourself. Click
     **Advanced**, then **"Go to (project name) (unsafe)"**.
   - Click **Allow** on the permissions list (Calendar, plus permission to make
     web requests and set up the timer).

9. That's it. `setup()` installs the every-minute timer for you. You should see
   a green "Execution completed" message and a log line like
   *"DictationMic bridge is ON."*

From now on, new calendar events become notes automatically.

---

## Check it's working

- In the Apps Script editor, click **Executions** (the clock/list icon on the
  left). You'll see `poll` running roughly once a minute. A tick means it ran
  fine; click any row to see its log.
- To test end-to-end: add a new event to your Google Calendar (on your phone,
  say) for later today. Within a minute or two a matching note should appear in
  DictationMic on your phone and, next time it's on, your laptop.

---

## Turn it off

Any of these will stop it:

- **Easiest:** in the editor, choose **`teardown`** in the function dropdown and
  click **Run**. That deletes the timer.
- Or click **Triggers** (the alarm-clock icon on the left), and delete the
  `poll` trigger.

To turn it back on later, run **`setup`** again.

---

## Notes / troubleshooting

- **Nothing appears.** Open **Executions** and click a recent `poll` row to read
  its log. Errors are logged there. The most common cause is being signed in
  with the wrong Google account (see the warning at the top).
- **Old events didn't get imported.** That's on purpose — `setup()` starts the
  clock at the moment you run it, so only events created/changed *after* setup
  are imported. Your existing calendar is left alone.
- **Duplicate-looking notes.** The bridge and your laptop both try to import new
  events, but they check for each other so you should get one note per event.
  The bridge is the main importer; the laptop is the backup for when the bridge
  is briefly behind.
