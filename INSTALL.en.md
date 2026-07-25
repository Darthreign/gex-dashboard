# Step-by-step installation guide

*[Version française](INSTALL.md)*

This guide is for someone who has **never installed a program like this
before**. No prior knowledge is assumed. Allow about 15 minutes.

By the end, you'll have the dashboard running in your browser. **No account, no
key, no payment**: the data comes from a free public source.

> 💡 Read each step fully before doing it. If you get stuck, the
> [Troubleshooting](#troubleshooting) section at the bottom covers the most
> common errors.

---

## Step 1 — Install Python

Python is the language the program is written in. You install it once.

### On Windows

1. Go to **https://www.python.org/downloads/**
2. Click the big yellow **"Download Python 3.x"** button.
3. Open the downloaded file (bottom of your browser, or your *Downloads*
   folder).
4. **⚠️ MOST IMPORTANT POINT**: on the installer's first screen, tick the box
   **"Add python.exe to PATH"** at the bottom of the window, **before** clicking
   *Install Now*. If you skip this box, nothing will work afterwards.
5. Click **Install Now** and let it finish.

### On Mac

1. Go to **https://www.python.org/downloads/**
2. Click **"Download Python 3.x"**.
3. Open the downloaded `.pkg` file and follow the installer (click
   *Continue* / *Install* to the end).

---

## Step 2 — Download the program

No need to know Git. You just grab a ZIP file.

1. Go to **https://github.com/Darthreign/gex-dashboard**
2. Click the green **"Code"** button (top right of the file list).
3. In the menu that opens, click **"Download ZIP"**.
4. Once downloaded, **unzip it**:
   - **Windows**: right-click the file → *Extract All* → *Extract*.
   - **Mac**: double-click the file, it unzips itself.
5. You get a folder named **`gex-dashboard-main`**. Move it wherever you like
   (your *Desktop*, for example) so you can find it again.

---

## Step 3 — Open the terminal INSIDE the folder

The "terminal" is a window where you type commands. The tricky part is opening
it **in the right place**: inside the program's folder.

### On Windows

1. Open the **`gex-dashboard-main`** folder (double-click). Inside you should
   see files like `run.py`, `requirements.txt`, a `gex` folder…
2. Click once in the **address bar** at the top of the window (where the folder
   path is shown). The text highlights in blue.
3. Type **`powershell`** over it and press **Enter**.
4. A dark blue or black window opens: that's the terminal, already placed in
   the right folder.

### On Mac

1. Open the **Terminal** app (search "Terminal" in Spotlight with
   *Cmd + Space*).
2. Type **`cd `** (with a space after `cd`), **without pressing Enter**.
3. **Drag the folder** `gex-dashboard-main` from Finder straight into the
   Terminal window: its path writes itself.
4. Press **Enter**.

---

## Step 4 — Install and run

Type the following commands **one at a time**, pressing **Enter** after each,
and waiting for each to finish before moving on.

### On Windows

Create the environment (a few seconds):

```
python -m venv .venv
```

Install the required components (1 to 3 minutes; lots of text scrolls by,
that's normal):

```
.venv\Scripts\python -m pip install -r requirements.txt
```

Start the dashboard:

```
.venv\Scripts\python run.py
```

### On Mac

```
python3 -m venv .venv
```

```
.venv/bin/python -m pip install -r requirements.txt
```

```
.venv/bin/python run.py
```

### What you should see

After the last command, something like this stays on screen:

```
Dash is running on http://127.0.0.1:8050/
```

Good sign: **the program is running**. Leave this window open — it's what keeps
the dashboard alive.

---

## Step 5 — Open the dashboard

1. Open your usual browser (Chrome, Edge, Firefox, Safari…).
2. In the address bar, type exactly: **`127.0.0.1:8050`** then Enter.
3. The dashboard appears. 🎉

It starts collecting data immediately. The first values show within seconds;
the history charts fill in over the days you use it.

---

## Day-to-day use

- **Stop the program**: go back to the terminal window and press **Ctrl + C**
  (Windows and Mac). Or just close the window.
- **Restart it later**: reopen the terminal in the folder (step 3) and type a
  **single** command — the install does not need repeating:
  - Windows: `.venv\Scripts\python run.py`
  - Mac: `.venv/bin/python run.py`
- **When should you leave it running?** Start it during US market hours on days
  you want to follow the data. Outside those hours it sleeps and uses nothing.
  You can close it overnight and on weekends without losing anything important.

---

## Troubleshooting

| Message / symptom | Cause | Fix |
|---|---|---|
| `python is not recognized…` (Windows) | The *Add to PATH* box was not ticked in step 1 | Reinstall Python, making sure to **tick the box**, then close and reopen the terminal |
| `command not found: python` (Mac) | On Mac the command is `python3` | Use **`python3`** instead of `python` |
| The address bar won't accept `powershell` | Window not in focus | Click in the folder again, then in the address bar, retype `powershell` |
| `pip install` stops on a red error | Network hiccup during download | Just run the same `pip install …` command again |
| Browser shows "can't reach this site" | The terminal was closed, or you typed the wrong address | Check the terminal window is still open and shows *Dash is running*, and that you typed `127.0.0.1:8050` |
| A firewall window asks for permission on first launch | Windows asks whether the program may access the network | Allow it (needed to fetch the data) |

If you're truly stuck, note the exact error message and ask the person who
shared the program with you.

---

## Going further (optional)

- **English / French version**: *FR / EN* button at the top of the dashboard.
- **Understanding the indicators**: *FAQ* button at the top of the dashboard,
  or the [FAQ.en.md](FAQ.en.md) file.
- **Updating the program** later: re-download the ZIP (step 2) and redo the
  install in the new folder. Your data stays in the old `data/` folder if you
  want to keep it.
- **Claude Code assistant**: if you use Claude Code, the [README](README.en.md)
  offers a prompt that does the whole install for you.

Enjoy — and remember it's an **analysis tool**, not investment advice (see the
[disclaimer](DISCLAIMER.md)).
